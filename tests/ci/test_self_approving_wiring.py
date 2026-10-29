"""The self-approving-commit guard has to reach the merge gate.

``scripts/check`` runs the guard locally and in the git hooks, but the gate
that blocks a merge is ``all-checks-pass`` in ``.github/workflows/ci.yaml``.
A guard that no CI job runs cannot fail a PR, so these tests pin the wiring
end to end: a reusable workflow that judges the PR's own commits, a PR-only
job in ci.yaml that calls it, that job inside the aggregate gate's ``needs``,
and a release run allowed to skip it.

The workflows are read as configuration and their expressions evaluated with
the shared Actions evaluator, so the assertions are about what the wiring
DOES, not about the text it happens to spell it with.
"""

from __future__ import annotations

import importlib.util
import shlex
from functools import cache
from pathlib import Path

import pytest

from tests.ci import _gha_expr as gha
from tests.ci import workflow_steps

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = ".github/workflows/self-approving-check.yml"
_BLOB_SIZE_WORKFLOW = ".github/workflows/blob-size-check.yml"
_CI = ".github/workflows/ci.yaml"
_CHECKER = "scripts/ci/check_self_approving_commit.py"


@cache
def _yaml(rel: str) -> dict:
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load((_REPO / rel).read_text(encoding="utf-8-sig"))


def _on(workflow: dict) -> dict:
    # YAML 1.1 reads the `on:` key as the boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def _ci_jobs() -> dict:
    return _yaml(_CI)["jobs"]


def _caller() -> str:
    """The ci.yaml job that calls the guard's reusable workflow."""
    callers = [name for name, body in _ci_jobs().items()
               if body.get("uses") == f"./{_WORKFLOW}"]
    assert len(callers) == 1, f"expected one ci.yaml job calling {_WORKFLOW}, got {callers}"
    return callers[0]


def _context(event_name: str = "pull_request") -> dict:
    """A pull request's expressions, as the workflows under test see them."""
    return {
        "github": {
            "event_name": event_name,
            "base_ref": "main",
            "ref_type": "branch",
            "event": {"pull_request": {"number": 7}},
        },
        "inputs": {"commit": "abc1234"},
        "needs": {"detect": {"outputs": {"event_name": event_name}}},
    }


def _rendered_steps(rel: str = _WORKFLOW) -> list[str]:
    """Every step's env and run text, with the pull request's expressions filled in."""
    ctx = _context()
    rendered = []
    for body in _yaml(rel)["jobs"].values():
        for step in body.get("steps") or []:
            env = {k: gha.render(v, ctx) for k, v in (step.get("env") or {}).items()}
            text = gha.render(step.get("run", ""), ctx)
            rendered.append("\n".join(f"{k}={v}" for k, v in sorted(env.items())) + "\n" + text)
    return rendered


def _logical_lines(text: str) -> list[str]:
    """Shell lines, with backslash continuations joined into one."""
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        lines.append(buffer + line)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def _guard_command() -> list[str]:
    """The argv of the command that runs the guard."""
    for text in _rendered_steps():
        for line in _logical_lines(text):
            if _CHECKER in line:
                return shlex.split(line)
    raise AssertionError(f"no step in {_WORKFLOW} runs {_CHECKER}")


def _checkout_uses(rel: str) -> str:
    for body in _yaml(rel)["jobs"].values():
        for step in body.get("steps") or []:
            if workflow_steps.is_checkout(step):
                return str(step["uses"])
    raise AssertionError(f"{rel} has no checkout step")


# -- the reusable workflow -------------------------------------------------------------


def test_the_guard_has_a_reusable_workflow():
    assert (_REPO / _WORKFLOW).is_file(), (
        f"{_WORKFLOW} is missing: the guard runs locally, but no workflow makes it block a merge")
    call = _on(_yaml(_WORKFLOW)).get("workflow_call") or {}
    inputs = call.get("inputs") or {}
    assert "commit" in inputs, f"{_WORKFLOW} must accept the commit ci.yaml passes it"


def test_the_workflow_checks_out_with_the_same_pinned_action_as_the_blob_size_check():
    # The same pinned action and SHA: one checkout pin to maintain, not two.
    assert _checkout_uses(_WORKFLOW) == _checkout_uses(_BLOB_SIZE_WORKFLOW)


# -- the ci.yaml job -------------------------------------------------------------------


def test_ci_calls_the_guard_workflow_with_the_triggering_commit():
    given = _ci_jobs()[_caller()].get("with") or {}
    assert "commit" in given, "the guard job must pass the commit it should judge"
    assert gha.render(given["commit"], _context()) == "abc1234"


def test_the_calling_job_blocks_a_merge():
    needs = _ci_jobs()["all-checks-pass"].get("needs") or []
    assert _caller() in needs, (
        "the guard job is not part of the aggregate gate, so it cannot fail a PR")


def test_the_calling_job_only_runs_on_a_pull_request():
    # The rule is about the commits a reviewer sees. A squash-merge folds a correctly
    # split PR back into one commit touching both sides, so judging main would be a
    # false positive.
    condition = _ci_jobs()[_caller()].get("if")
    assert gha.condition(condition, _context("pull_request")) is True
    assert gha.condition(condition, _context("push")) is False


def test_a_release_run_may_skip_the_guard_job():
    spec = importlib.util.spec_from_file_location(
        "required_results_wiring", _REPO / "scripts" / "ci" / "required_results.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert _caller() in module.EXCLUDED_JOBS, (
        "a release run skips the PR-only job; the strict gate must tolerate that skip")


# -- what the workflow actually judges -------------------------------------------------


def test_the_workflow_runs_the_guard_over_the_base_and_the_pull_request_head():
    argv = _guard_command()
    assert _CHECKER in argv
    assert "--base" in argv and "--head" in argv
    base = argv[argv.index("--base") + 1]
    head = argv[argv.index("--head") + 1]
    assert base == "origin/main", base
    assert head == "origin/pr-head", head


def test_the_workflow_judges_the_pull_requests_own_commits():
    # The synthetic refs/pull/N/merge commit merges both sides of a split commit back
    # together and its diff-tree is empty: it would judge nothing.
    text = "\n".join(_rendered_steps())
    assert "refs/pull/7/head" in text, "the PR head is never fetched"
    assert "refs/remotes/origin/pr-head" in text, "the PR head is never fetched into a ref"
    assert "refs/pull/7/merge" not in text, "the synthetic merge commit must not be judged"
