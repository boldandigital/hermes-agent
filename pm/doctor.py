"""Dependency drift detection for ``pm doctor``.

A recorded digest proves a TOOL's bytes are intact. It says nothing about the
Python environment a worker spawns from: that tree drifts silently -- a stray
install, a hand-edited site-packages -- and the drift surfaces much later, as a
crash inside a worker, long after the check that could have caught it ran green.
Doctor compares the REALIZED distributions against the lock the environment was
resolved from, and names every divergent package and version.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path


def _normalized(name: str) -> str:
    """PEP 503 name form: ``httpx_core`` and ``httpx-core`` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def realized_tree(environment: Path) -> dict[str, str]:
    """The distributions actually installed in ``environment`` (package -> version)."""
    from pm.environments import site_packages

    tree: dict[str, str] = {}
    for dist in importlib.metadata.distributions(path=[str(site_packages(environment))]):
        name = dist.metadata["Name"]
        version = dist.version
        if name and version:
            tree[_normalized(name)] = version
    return tree


def expected_tree(lock_path: Path) -> dict[str, set[str]]:
    """The packages the lock resolves to (package -> every version it may install).

    One name can carry several versions: a lock records an entry per platform and
    marker combination, and an environment installs the subset that applies to it.
    """
    data = tomllib.loads(lock_path.read_text(encoding="utf-8-sig"))
    expected: dict[str, set[str]] = {}
    for package in data.get("package", ()):
        name = package.get("name")
        version = package.get("version")
        if name and version:
            expected.setdefault(_normalized(name), set()).add(version)
    return expected


def compare_trees(realized: dict[str, str], expected: dict[str, set[str]]) -> list[str]:
    """The divergences between the installed tree and the lock, one message per package.

    Two classes are drift: a package the lock does not mention, and a package
    installed at a version the lock does not allow. A locked package that is NOT
    installed is not drift -- the lock carries every platform, and an environment
    holds the subset that applies to it.
    """
    divergences = []
    for name in sorted(realized):
        version = realized[name]
        allowed = expected.get(name)
        if allowed is None:
            divergences.append(f"{name} {version} installed, not in the lockfile")
        elif version not in allowed:
            divergences.append(f"{name} {version} installed, lock says {', '.join(sorted(allowed))}")
    return divergences


def recorded_fact() -> dict | None:
    """The dependency environment PM committed for this install, as the facts record it."""
    from pm import paths
    from pm.lock import Facts

    return Facts(paths.runtime_facts_path()).get("venv")


def check_environment(fact: dict | None) -> tuple[bool, list[str]]:
    """Verify the recorded dependency environment against the lock it was resolved from.

    Returns (ok, lines). ``ok`` is False when the trees diverge, and the caller
    fails the check. A machine with no committed environment is not a failure:
    doctor has to stay usable before the first install.
    """
    if not fact or not fact.get("environment"):
        return True, ["? venv: no dependency environment committed"]
    environment = Path(fact["environment"])
    resolved_lock = fact.get("resolved_lock")
    if not resolved_lock:
        return True, ["? venv: no recorded lock for the dependency environment"]
    lock_path = Path(resolved_lock)
    if not environment.is_dir():
        return False, [f"✗ venv: the recorded dependency environment is missing: {environment}"]
    if not lock_path.is_file():
        return False, [f"✗ venv: the recorded lock is missing: {lock_path}"]
    realized = realized_tree(environment)
    expected = expected_tree(lock_path)
    divergences = compare_trees(realized, expected)
    if divergences:
        return False, [f"✗ venv: {divergence}" for divergence in divergences]
    return True, [f"✓ venv: {len(realized)} distributions match the recorded lock"]
