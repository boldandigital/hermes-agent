"""Install identity is canonical: a linked git worktree resolves to its OWNING checkout.

``pm.environments.install_key`` hashed the checkout's own path, so every linked worktree
of a repo got a private install key -- and with it a private ``installs/<key>`` state dir
that never holds a committed dependency environment. A worker booted inside a worktree
then died with "no dependency environment is committed for this install", while
``hermes pm repair`` only ever repaired the main checkout.

The key must name the OWNING checkout instead: every worktree of a repo then shares the
one committed environment. The main checkout's own key is unchanged by this and MUST stay
byte-identical -- a moved key would orphan the live install's state dir and leave the
running gateway without its environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pm.environments
import pytest
from hermes_cli.update_channel import channel_record, install_id
from hermes_cli.update_owning_install import owning_install_root
from pm.environments import (
    activation_inputs_dir,
    committed_venv,
    install_key,
    install_state_dir,
    record_activation_inputs,
)
from pm.install_states import orphan_install_states

# The live checkout of this repo, when this host has one. Its install key is pinned: a
# changed key would orphan ``~/.hermes/installs/a343bb46827f1667`` and break the running
# gateway.
LIVE_CHECKOUT = Path("/Users/lars/.hermes/hermes-agent")
LIVE_INSTALL_KEY = "a343bb46827f1667"

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Hermes Test",
    "GIT_AUTHOR_EMAIL": "hermes-test@example.invalid",
    "GIT_COMMITTER_NAME": "Hermes Test",
    "GIT_COMMITTER_EMAIL": "hermes-test@example.invalid",
}


def _git(cwd: Path, *args: str) -> None:
    """Run git in *cwd* with an identity, so no global git config is needed."""
    env = dict(os.environ)
    env.update(_GIT_IDENTITY)
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _key_of(path: Path) -> str:
    """The install key a plain checkout of *path* gets: sha16 of its resolved path."""
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


@pytest.fixture
def git_repo(tmp_path):
    """A primary checkout with one empty commit."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "owner"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


@pytest.fixture
def linked_worktree(git_repo):
    """A linked worktree of ``git_repo`` (``.git`` is a file pointing into the owner's gitdir)."""
    path = git_repo / ".worktrees" / "wt"
    _git(git_repo, "worktree", "add", "-q", str(path), "-b", "wt/branch")
    return path


def _write_committed_environment(state: Path) -> Path:
    """Publish a committed environment under *state* and return its venv path."""
    generation = state / "environments" / "gen" / "venv"
    generation.mkdir(parents=True)
    (generation / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (state / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(generation.resolve())}}}),
        encoding="utf-8",
    )
    return generation


def test_worktree_key_matches_owning_checkout(git_repo, linked_worktree):
    assert install_key(linked_worktree) == install_key(git_repo)


@pytest.mark.allow_real_home_io
def test_main_checkout_key_unchanged(git_repo):
    # The portable invariant: a primary checkout keys on its own resolved path.
    assert install_key(git_repo) == _key_of(git_repo)

    if not LIVE_CHECKOUT.is_dir():
        pytest.skip("this host has no live Hermes checkout")
    # The live checkout is read here, never written: its key is the one the
    # running gateway's state dir lives under.
    assert install_key(LIVE_CHECKOUT) == LIVE_INSTALL_KEY


def test_non_repo_path_is_distinct(git_repo, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert install_key(plain) == _key_of(plain)
    assert install_key(plain) != install_key(git_repo)


def test_plain_clone_keeps_its_own_key(git_repo, tmp_path):
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(git_repo), str(clone))

    assert install_key(clone) != install_key(git_repo)


def test_worktree_resolves_owner_committed_environment(git_repo, linked_worktree):
    state = install_state_dir(git_repo)
    generation = _write_committed_environment(state)

    assert install_state_dir(linked_worktree) == state
    assert committed_venv(linked_worktree) == committed_venv(git_repo) == generation.resolve()


def test_shared_state_survives_worktree_deletion(git_repo, linked_worktree):
    """The shared state dir belongs to the OWNER: deleting a worktree must not reclaim it."""
    stamps = activation_inputs_dir(linked_worktree)
    record_activation_inputs(stamps, {}, linked_worktree, test_environment=False)
    state = install_state_dir(linked_worktree)
    assert state == install_state_dir(git_repo)
    assert (stamps / ".project-root").read_text(encoding="utf-8").strip() == str(git_repo.resolve())

    _git(git_repo, "worktree", "remove", "--force", str(linked_worktree))

    assert orphan_install_states(pm.environments.installs_root()) == []
    assert state.is_dir()


def test_update_channel_install_id_matches_owner(git_repo, linked_worktree):
    assert install_id(linked_worktree) == install_id(git_repo)

    config = {
        "update": {
            "installs": {
                install_id(git_repo): {"path": str(git_repo), "channel": "stable"},
            }
        }
    }
    # One owning install: the worktree reads the owner's record, never a second one.
    assert channel_record(config, linked_worktree) == channel_record(config, git_repo)
    assert channel_record(config, linked_worktree) == {"path": str(git_repo), "channel": "stable"}


def test_update_owning_install_identifies_the_owner(git_repo, linked_worktree, monkeypatch, tmp_path):
    state = install_state_dir(git_repo)
    generation = _write_committed_environment(state)
    (state / "inputs").mkdir(parents=True, exist_ok=True)
    (state / "inputs" / ".project-root").write_text(str(git_repo.resolve()), encoding="utf-8")
    (git_repo / "hermes_cli").mkdir()
    (git_repo / "hermes_cli" / "main.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "prefix", str(generation))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "system-python"))
    monkeypatch.delenv("PYTHONPATH", raising=False)

    owner = owning_install_root(linked_worktree)
    assert owner == git_repo.resolve()


def test_canonical_checkout_root_of_primary_checkout_is_itself(git_repo):
    assert pm.environments.canonical_checkout_root(git_repo) == git_repo.resolve()


def test_canonical_checkout_root_of_linked_worktree_is_the_owner(git_repo, linked_worktree):
    assert pm.environments.canonical_checkout_root(linked_worktree) == git_repo.resolve()


def test_canonical_checkout_root_of_non_repo_dir_is_itself(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert pm.environments.canonical_checkout_root(plain) == plain.resolve()


def test_canonical_checkout_root_of_separate_git_dir_clone_is_itself(git_repo, tmp_path):
    gitdir = tmp_path / "elsewhere-gitdir"
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--separate-git-dir", str(gitdir), str(git_repo), str(clone))

    assert pm.environments.canonical_checkout_root(clone) == clone.resolve()


def test_canonical_checkout_root_of_unparsable_git_file_is_itself(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nonexistent/gitdir\n", encoding="utf-8")

    assert pm.environments.canonical_checkout_root(broken) == broken.resolve()
