#!/usr/bin/env python3
"""Reject a commit that changes a guard test and the source it guards in one go.

A guard test is a test whose failure means the production code it watches misbehaves. Widening
such a test in the same commit as the code it watches makes the commit approve itself: the
widened test can no longer fail the change the commit allows, so the change ships unreviewed by
the very check that was supposed to catch it. The remedy is to split the commit -- the guard
test first, the source change second -- and the guard says so.

The mapping from guard test to guarded source lives in one reviewed file,
``scripts/ci/guard_test_pairs.json``. Nothing is inferred from names, imports or file layout, so
the verdict of a commit never flips because of an unrelated edit: a commit is self-approving if,
and only if, one commit changes both sides of a registered pair.

A merge commit is judged on the changes it introduces itself -- its own resolution, read from
the combined diff -- so a merge that widens a guard test AND changes the guarded source is a
self-approving change and is reported like any other commit. A merge that only combines two
separately good commits (one commit widens the guard, another changes the source) introduces
neither change itself and is therefore not a finding. The remedy is the same in both cases:
split the work into separate commits -- the guard test first, the source change second.

    python scripts/ci/check_self_approving_commit.py                    # merge-base with main..HEAD
    python scripts/ci/check_self_approving_commit.py --base A --head B  # any two revisions
    python scripts/ci/check_self_approving_commit.py --head REV         # one commit

Exit codes: 0 = no self-approving commit, 1 = one or more findings (or a stale registry entry),
2 = usage or git error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.code_health import gitio

REGISTRY = Path(__file__).resolve().parent / "guard_test_pairs.json"
VERSION = 1


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Pair:
    """A guard test and the production sources it guards."""

    test: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Judgment:
    """One commit judged on its own: the paths it changes and the tree they were read from."""

    label: str
    paths: frozenset[str]
    tree: str


@dataclass(frozen=True)
class Finding:
    commit: str
    test: str
    source: str


def load_registry(path: Path) -> list[Pair]:
    """The registry as the reviewed list of guard-test -> guarded-source pairs."""
    if not path.is_file():
        raise RegistryError(f"registry not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry {path} is not valid JSON: {exc}") from exc
    if data.get("version") != VERSION:
        raise RegistryError(f"registry {path} has version {data.get('version')!r}, expected {VERSION}")
    pairs = []
    for entry in data.get("pairs", ()):
        test = entry.get("test")
        sources = tuple(entry.get("sources", ()))
        if not test or not sources:
            raise RegistryError(f"registry {path} has an entry without a test or sources: {entry!r}")
        pairs.append(Pair(test, sources))
    return pairs


def _commit_paths(repo: Path, commit: str) -> frozenset[str]:
    """Every path the commit touches: added, modified, deleted or renamed to or from.

    The combined diff (``--cc``) is the one form that answers the same question for every kind
    of commit: for an ordinary commit and for a root commit it lists exactly the paths that
    commit changed, and for a merge commit it lists the paths the merge changed itself, relative
    to all of its parents -- its own resolution. A plain ``diff-tree`` prints nothing at all for
    a merge, which would judge a self-approving merge as touching nothing.
    """
    out = gitio.git(repo, "diff-tree", "--cc", "--no-commit-id", "--name-only", "-r", "--root",
                    "--no-renames", "-z", commit)
    return frozenset(path for path in out.split("\0") if path)


def _diff_paths(repo: Path, base: str, head: str) -> frozenset[str]:
    out = gitio.git(repo, "diff", "--name-only", "--no-renames", "-z", base, head)
    return frozenset(path for path in out.split("\0") if path)


def _commits_in_range(repo: Path, base: str, head: str) -> list[str]:
    out = gitio.git(repo, "rev-list", f"{base}..{head}")
    return [commit for commit in out.split() if commit]


def plan(repo: Path, base: str | None, head: str | None) -> tuple[list[Judgment], str]:
    """The commits to judge, each on its own, and the tree they are judged against.

    A commit head is judged alone (``head^..head``); a range is judged commit by commit, so a
    guard test in one commit and its source in the next one is not a finding. A tree-ish head
    (what ``scripts/check --staged`` passes) is judged as one prospective commit against the
    base.
    """
    head_commit = gitio.commit_or_none(repo, head or "HEAD")
    if head and head_commit is None:  # a tree: the bytes that are about to be committed
        tree = gitio.resolve_tree(repo, head)
        paths = _diff_paths(repo, base or "HEAD", tree)
        return [Judgment("prospective commit", paths, tree)], tree
    if head_commit is None:
        raise RuntimeError("no commit to judge (HEAD is unborn); pass --base and --head")
    if base is None:
        if head is None:
            base = gitio.default_base(repo, "HEAD")
            commits = _commits_in_range(repo, base, head_commit)
        else:
            parent = gitio.commit_or_none(repo, f"{head_commit}^")
            commits = [head_commit] if parent is None else _commits_in_range(repo, parent, head_commit)
    else:
        commits = _commits_in_range(repo, base, head_commit)
    tree = gitio.resolve_tree(repo, head_commit)
    return [Judgment(commit, _commit_paths(repo, commit), tree) for commit in commits], tree


def judge(judgment: Judgment, pairs: list[Pair]) -> list[Finding]:
    """The pairs this one commit breaks: the guard test and a source it guards, together."""
    findings = []
    for pair in pairs:
        if pair.test not in judgment.paths:
            continue
        findings += [Finding(judgment.label, pair.test, source) for source in pair.sources
                     if source in judgment.paths]
    return findings


def stale_entries(pairs: list[Pair], tree_paths: set[str]) -> list[str]:
    """Registered paths that the judged tree does not have: the registry is out of date."""
    stale = set()
    for pair in pairs:
        for path in (pair.test, *pair.sources):
            if path not in tree_paths:
                stale.add(path)
    return sorted(stale)


def render(findings: list[Finding], stale: list[str], registry: Path) -> list[str]:
    lines = [f"::error file={registry.name}::stale registry entry: {path} is not in the judged tree"
             for path in stale]
    lines += [f"::error file={f.test}::commit {f.commit[:7]} changes the guard test {f.test} and the "
              f"source it guards ({f.source}) in one commit; split the commit" for f in findings]
    if findings:
        lines += [
            "",
            f"{len(findings)} self-approving change(s) found. A commit that changes a guard test and "
            "the source it guards approves itself: the widened test can no longer fail the change "
            "the commit allows. Split the commit -- the guard test first, the source change second.",
        ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_self_approving_commit.py",
                                     description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--base", help="base revision (default: merge-base with main)")
    parser.add_argument("--head", help="head revision or tree (default: HEAD)")
    parser.add_argument("--registry", type=Path, default=REGISTRY,
                        help="guard test -> guarded source mapping (default: next to this script)")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    try:
        pairs = load_registry(args.registry)
        judgments, tree = plan(repo, args.base, args.head)
        tree_paths = set(gitio.tracked_files(repo, tree))
        stale = stale_entries(pairs, tree_paths)
        findings = [finding for judgment in judgments for finding in judge(judgment, pairs)]
    except (RegistryError, RuntimeError, OSError) as exc:
        print(f"check_self_approving_commit: {exc}", file=sys.stderr)
        return 2
    if stale or findings:
        print("\n".join(render(findings, stale, args.registry)))
        return 1
    print(f"check_self_approving_commit: no self-approving commit in {len(judgments)} judged commit(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
