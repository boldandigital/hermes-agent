"""pm doctor must see a drifted dependency environment, not only intact tool bytes.

The recorded digest proves a TOOL tree is intact. It says nothing about the
Python environment a worker spawns from: that tree drifts silently -- a stray
install, a hand-edited site-packages -- and the drift surfaces much later as a
crash inside a worker, long after the check that could have caught it ran
green. Doctor compares the REALIZED distributions against the lock the
environment was resolved from and names every divergent package and version.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm import paths
from tests.pm._fixtures import served as served  # noqa: PLC0414 -- the self-alias is load-bearing: it registers the fixture for this module
from tests.pm.test_pm_core import pm_env  # the pm environment fixture this module reuses


def _environment(root: Path) -> Path:
    """A dependency environment tree: a pyvenv.cfg and the site-packages it implies."""
    venv = root / "venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14.10\n", encoding="utf-8")
    return venv


def _install(site_packages: Path, name: str, version: str) -> None:
    """Record a distribution as installed in ``site_packages``."""
    dist = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")


def _lock(path: Path, packages: dict[str, str]) -> Path:
    """A uv.lock resolving ``packages`` (name -> version)."""
    blocks = ["version = 1", "revision = 5"]
    for name, version in packages.items():
        blocks.append(f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
                      'source = { registry = "https://pypi.org/simple" }\n')
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _record(facts: Path, environment: Path, resolved_lock: Path) -> Path:
    """The runtime fact PM commits when it selects a dependency environment."""
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps({"schema": 1, "packages": {"venv": {
        "stamp": "stamp", "extras": [],
        "environment": str(environment), "resolved_lock": str(resolved_lock),
    }}}), encoding="utf-8")
    return facts


@pytest.fixture
def committed(tmp_path, monkeypatch):
    """Wire a runtime fact onto a synthetic environment + lock; return the pieces."""
    from pm.environments import site_packages

    environment = _environment(tmp_path / "env")
    site = site_packages(environment)
    site.mkdir(parents=True)
    lock = _lock(tmp_path / "uv.lock", {})
    facts = _record(tmp_path / "facts.json", environment, lock)
    monkeypatch.setattr(paths, "runtime_facts_path", lambda: facts)
    return {"site": site, "lock": lock, "environment": environment, "facts": facts}


@pytest.mark.usefixtures("pm_env")
def test_doctor_names_the_drifted_package_and_both_versions(committed, capsys):
    """A drifted environment must fail the check and name the package, not just
    report that something mismatched."""
    from pm.cli import cmd_doctor
    from pm.install import ensure

    ensure("faketool", base_env={})
    _install(committed["site"], "httpx", "0.27.0")
    _lock(committed["lock"], {"httpx": "0.28.1"})

    assert cmd_doctor(None) == 1
    out = capsys.readouterr().out
    assert "httpx" in out
    assert "0.27.0" in out
    assert "0.28.1" in out


@pytest.mark.usefixtures("pm_env")
def test_doctor_passes_when_the_realized_tree_matches_the_lock(committed, capsys):
    """The clean path: the same environment, matching versions, is a pass."""
    from pm.cli import cmd_doctor
    from pm.install import ensure

    ensure("faketool", base_env={})
    _install(committed["site"], "httpx", "0.27.0")
    _lock(committed["lock"], {"httpx": "0.27.0"})

    assert cmd_doctor(None) == 0
    assert "match the recorded lock" in capsys.readouterr().out


@pytest.mark.usefixtures("pm_env")
def test_doctor_reports_a_package_the_lock_does_not_mention(committed, capsys):
    """An installed package the lock never names is drift too."""
    from pm.cli import cmd_doctor
    from pm.install import ensure

    ensure("faketool", base_env={})
    _install(committed["site"], "httpx", "0.27.0")
    _lock(committed["lock"], {"other": "1.0"})

    assert cmd_doctor(None) == 1
    out = capsys.readouterr().out
    assert "httpx" in out
    assert "0.27.0" in out


@pytest.mark.usefixtures("pm_env")
def test_doctor_ignores_a_locked_package_that_is_not_installed(committed, capsys):
    """A lock carries every platform; an environment installs the subset that
    applies to it. A package the lock names but the environment does not have
    is not drift."""
    from pm.cli import cmd_doctor
    from pm.install import ensure

    ensure("faketool", base_env={})
    _install(committed["site"], "httpx", "0.27.0")
    _lock(committed["lock"], {"httpx": "0.27.0", "cuda-only": "1.0"})

    assert cmd_doctor(None) == 0
    out = capsys.readouterr().out
    assert "match the recorded lock" in out
    assert "cuda-only" not in out


@pytest.mark.usefixtures("pm_env")
def test_doctor_without_a_recorded_environment_is_not_a_failure(tmp_path, monkeypatch, capsys):
    """Before the first install there is nothing to compare: doctor stays usable."""
    from pm.cli import cmd_doctor
    from pm.install import ensure

    ensure("faketool", base_env={})
    monkeypatch.setattr(paths, "runtime_facts_path", lambda: tmp_path / "absent.json")

    assert cmd_doctor(None) == 0
    assert "no dependency environment committed" in capsys.readouterr().out


def test_compare_trees_reports_only_the_two_drift_classes():
    """The comparison itself: version drift and unlocked packages, nothing else."""
    from pm.doctor import compare_trees

    divergences = compare_trees(
        {"httpx": "0.27.0", "stray": "1.2.3", "kept": "2.0"},
        {"httpx": {"0.28.1"}, "kept": {"2.0"}, "absent": {"1.0"}},
    )
    assert divergences == [
        "httpx 0.27.0 installed, lock says 0.28.1",
        "stray 1.2.3 installed, not in the lockfile",
    ]


def test_expected_tree_keeps_every_locked_version_of_a_name(tmp_path):
    """One name can carry several versions in a lock (markers, platforms)."""
    from pm.doctor import expected_tree

    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\nrevision = 5\n'
        '[[package]]\nname = "httpx"\nversion = "0.28.1"\n'
        '[[package]]\nname = "httpx"\nversion = "0.28.2"\n'
        '[[package]]\nname = "httpx-core"\nversion = "0.28.1"\n',
        encoding="utf-8",
    )
    assert expected_tree(lock) == {"httpx": {"0.28.1", "0.28.2"}, "httpx-core": {"0.28.1"}}
