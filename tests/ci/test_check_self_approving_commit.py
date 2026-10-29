"""scripts/ci/check_self_approving_commit.py on real temp git repos: a commit may not change
a guard test and the source it guards in one go.

A guard test is a test that fails when the production code it watches misbehaves. Widening such
a test in the same commit as the code it watches makes the commit approve itself: the widened
test can no longer catch the change the commit allows. The registry
(``scripts/ci/guard_test_pairs.json``) says which test guards which source; the check then reads
that registry and nothing else, so its verdict never flips on an unrelated edit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci" / "check_self_approving_commit.py"

GUARD_TEST = "tests/guard/test_thing.py"
GUARD_SOURCE = "src/thing.py"
REGISTRY_NAME = "guard_test_pairs.json"


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, env=_env(), capture_output=True, text=True,
                          encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60, check=False)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str = "step") -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, branch: str, resolution: dict[str, str], msg: str = "merge") -> str:
    """Merge ``branch`` into HEAD and commit; ``resolution`` is the merge's own content.

    ``resolution`` holds the files the merge itself changes: the content the merge writes for
    them, staged over whatever the merge put in the worktree (conflict markers included). An
    empty resolution means the merge combines the branches and changes nothing itself.
    """
    proc = subprocess.run(["git", "merge", "--no-ff", "--no-commit", branch], cwd=repo, env=_env(),
                          capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
                          timeout=60, check=False)
    assert proc.returncode in (0, 1), (proc.stdout, proc.stderr)  # 1 = conflicts, resolved below
    for rel, body in resolution.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _registry(repo: Path, pairs: list[dict[str, object]]) -> Path:
    path = repo / REGISTRY_NAME
    path.write_text(json.dumps({"version": 1, "description": "which test guards which source",
                                "pairs": pairs}, indent=2), encoding="utf-8")
    return path


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo),
                           "--registry", str(repo / REGISTRY_NAME), *extra],
                          cwd=repo, env=_env(), capture_output=True, text=True, encoding="utf-8",
                          stdin=subprocess.DEVNULL, timeout=60, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _commit(root, {GUARD_TEST: "def test_thing():\n    assert True\n",
                    GUARD_SOURCE: "VALUE = 1\n"}, "base")
    _registry(root, [{"test": GUARD_TEST, "sources": [GUARD_SOURCE]}])
    return root


def test_commit_that_changes_the_guard_test_and_its_source_is_flagged(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n",
                           GUARD_SOURCE: "VALUE = 2\n"}, "widen the guard and the code")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert GUARD_TEST in proc.stdout
    assert GUARD_SOURCE in proc.stdout
    assert head[:7] in proc.stdout
    assert "split the commit" in proc.stdout


def test_commit_that_changes_only_the_guard_test_is_not_flagged(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_commit_that_changes_only_the_source_is_not_flagged(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {GUARD_SOURCE: "VALUE = 2\n"}, "change the code")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_guard_test_and_source_in_separate_commits_are_not_flagged(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    head = _commit(repo, {GUARD_SOURCE: "VALUE = 2\n"}, "change the code")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ordinary_commit_that_changes_both_sides_is_still_flagged(repo):
    # The merge rule must not change how an ordinary commit is judged.
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n",
                           GUARD_SOURCE: "VALUE = 2\n"}, "widen the guard and the code")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert head[:7] in proc.stdout


def test_ordinary_commit_that_changes_one_side_is_still_not_flagged(repo):
    # The merge rule must not change how an ordinary commit is judged.
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_merge_commit_that_changes_the_guard_test_and_its_source_is_flagged(repo):
    # A merge is judged on what it changes itself: if its own resolution widens the guard test
    # and changes the guarded source, the merge approves itself and must be reported.
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    _commit(repo, {GUARD_SOURCE: "VALUE = 2\n"}, "change the code")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {GUARD_TEST: "def test_thing():\n    assert 0\n"}, "widen the guard")
    _commit(repo, {GUARD_SOURCE: "VALUE = 3\n"}, "change the code")
    merge = _merge(repo, "feature", {GUARD_TEST: "def test_thing():\n    assert True\n",
                                      GUARD_SOURCE: "VALUE = 4\n"}, "merge with its own resolution")
    proc = _run(repo, "--base", base, "--head", merge)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert GUARD_TEST in proc.stdout
    assert GUARD_SOURCE in proc.stdout
    assert merge[:7] in proc.stdout


def test_merge_commit_that_only_combines_two_separately_good_commits_is_not_flagged(repo):
    # The merge itself changed neither side: one commit widened the guard, another changed the
    # source. Combining them is not self-approving, so the merge must not be reported.
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {GUARD_SOURCE: "VALUE = 2\n"}, "change the code")
    merge = _merge(repo, "feature", {}, "merge the branch")
    proc = _run(repo, "--base", base, "--head", merge)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_test_the_registry_does_not_name_is_not_judged(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"tests/other/test_other.py": "def test_other():\n    pass\n",
                           GUARD_SOURCE: "VALUE = 2\n"}, "an unregistered test and the source")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_stale_registry_entry_is_an_error(repo):
    _registry(repo, [{"test": "tests/guard/test_missing.py", "sources": [GUARD_SOURCE]}])
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {GUARD_SOURCE: "VALUE = 2\n"}, "change the code")
    proc = _run(repo, "--base", base, "--head", head)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "tests/guard/test_missing.py" in proc.stdout


def test_head_commit_alone_judges_only_that_commit(repo):
    _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    head = _commit(repo, {GUARD_SOURCE: "VALUE = 2\n"}, "change the code")
    proc = _run(repo, "--head", head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    bad = _commit(repo, {GUARD_TEST: "def test_thing():\n    assert 0\n",
                         GUARD_SOURCE: "VALUE = 3\n"}, "widen the guard and the code")
    proc = _run(repo, "--head", bad)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert bad[:7] in proc.stdout


def test_without_base_or_head_the_merge_base_with_main_is_the_base(repo):
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, {GUARD_TEST: "def test_thing():\n    assert False\n"}, "widen the guard")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    head = _commit(repo, {GUARD_TEST: "def test_thing():\n    assert 0\n",
                           GUARD_SOURCE: "VALUE = 2\n"}, "widen the guard and the code")
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert head[:7] in proc.stdout


def test_scripts_check_runs_the_guard(repo):
    # The guard only counts if the surface that decides whether a change is acceptable runs it.
    head = _git(REPO, "rev-parse", "HEAD")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "check"), "--only", "self-approving",
                           "--base", head], cwd=REPO, env=_env(), capture_output=True, text=True,
                          encoding="utf-8", stdin=subprocess.DEVNULL, timeout=600, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
