"""A pinned skill the ASSIGNEE's own seat cannot resolve must fail loudly where the pin is
assembled — not silently at worker init.

Two layers, because the pin has two sources:

* **DISPATCH** (the layer that owns the real fleet failure). ``_dispatch_lane_task`` force-injects
  ``sdlc-review`` on the ``review`` lane (``kanban_db_dispatch.py``: ``if lane == "review": ... claimed.skills = ... "sdlc-review"``),
  so the set that must be checked is ``claimed.skills`` AFTER that injection. Measured on this
  fleet: 30 card logs, 117 ``Unknown skill(s): sdlc-review`` error lines — and ``skills`` is
  NULL/``[]`` on every one of those cards. No card ever pinned it. A create-only validator passes
  all 30 and catches none, which is the "safety net with no teeth" failure this whole thread is
  about.
* **CREATE** (the layer that guards genuine operator pins). ``create_task`` accepts whatever names
  it is handed; a typo'd or seat-incompatible pin is discovered minutes later by the worker.

Both layers answer the same question with the same mechanism: *would the loader this seat will
actually use resolve this name?* A validator that disagrees with the loader is worse than none, so
both resolve through :func:`agent.skill_commands.build_preloaded_skills_prompt` under the assignee's
own ``HERMES_HOME`` — the very call the worker makes at init — instead of re-implementing
resolution. The ``disabled_out`` seam keeps a present-but-off pin from being called "unknown"
(wording owned by card t_717b821e; this asserts the *classification*, not the prose).

Validation is a guard rail, not a gate: if the scan cannot run (unreadable config, broken profile
dir, unknown assignee) creation and dispatch both proceed untouched. A flaky scan must never stop
the fleet.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _write_skill(skills_dir: Path, name: str, *, category: str | None = None) -> Path:
    """A minimal on-disk skill. *category* nests it (``research/arxiv``); no category is flat."""
    skill_dir = skills_dir / category / name if category else skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: fixture skill {name}\n---\n\nbody\n", encoding="utf-8")
    return skill_dir


@pytest.fixture
def seats(tmp_path, monkeypatch):
    """Two isolated Hermes homes so ``profiles/<seat>/`` never resolves to the live install.

    ``launch`` is the dispatcher's own home; ``alpha`` and ``beta`` are assignee seats. Each gets
    its own ``skills/`` tree and ``config.yaml``, so ``skills.disabled`` is genuinely per-seat —
    which is the whole point of the split that caused the incident.

    ``_get_profiles_root`` is patched rather than only exporting ``HERMES_HOME``: that resolver is
    anchored to ``get_default_hermes_root()``, which memoizes on the process home, so a tmp_path
    ``HERMES_HOME`` outside the real ``~/.hermes`` is honoured only by luck of ordering. Relying on
    that made these tests pass alone and fail in a full run.
    """
    launch = tmp_path / "fakehome" / ".hermes"
    launch.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    # Imported INSIDE the fixture: another test in this suite purges ``hermes_cli`` and
    # ``hermes_constants`` from ``sys.modules``, so a module-level binding can be a different
    # module object than the one the guard resolves later — patching a stale copy has no effect.
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: launch / "profiles")
    # ...and that same purge leaves ``agent.skill_utils`` holding a STALE ``hermes_constants``
    # module, so a home override set through the fresh module is invisible to it and every seat
    # reads the launch home's config instead of its own (disabled list silently empty). Drop the
    # stale ``agent.skill_utils`` so it re-imports against the current ``hermes_constants``.
    for stale in ("agent.skill_utils", "tools.skills_tool"):
        monkeypatch.delitem(sys.modules, stale, raising=False)
    made: dict[str, Path] = {}
    for seat in ("alpha", "beta"):
        home = launch / "profiles" / seat
        (home / "skills").mkdir(parents=True)
        (home / "config.yaml").write_text("skills:\n  disabled: []\n", encoding="utf-8")
        made[seat] = home
    made["launch"] = launch
    return made


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated kanban.db, on the same fake home as the seats."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "fakehome" / ".hermes" / "kanban.db"))
    return tmp_path / "fakehome" / ".hermes" / "kanban.db"


def _disable(home: Path, *names: str) -> None:
    (home / "config.yaml").write_text(
        "skills:\n  disabled:\n" + "".join(f"    - {n}\n" for n in names), encoding="utf-8")


def _stage_review(conn, assignee: str) -> str:
    """A card parked in the ``review`` lane — the lane that injects ``sdlc-review``."""
    task_id = kb.create_task(conn, title="review me", assignee=assignee)
    kb.request_review(conn, task_id, summary="ready for review")
    assert (kb.get_task(conn, task_id) or kb.Task()).status == "review"
    return task_id


# ---------------------------------------------------------------------------
# CREATE layer — genuine operator pins
# ---------------------------------------------------------------------------


def test_create_rejects_skill_disabled_in_the_assignees_own_seat(seats, board):
    """AC1: the pin exists on disk but that seat's ``skills.disabled`` switches it off.

    This is the split that produced the 117 deaths — present for the creator, disabled for the
    assignee — so a creator-seat check would wave it straight through.
    """
    _write_skill(seats["alpha"] / "skills", "sdlc-review")
    _write_skill(seats["beta"] / "skills", "sdlc-review")
    _disable(seats["beta"], "sdlc-review")  # only beta is off

    with kbc.connect(board) as conn, pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="t", assignee="beta", skills=["sdlc-review"])
    message = str(exc.value)
    assert "sdlc-review" in message
    assert "beta" in message, "the message must name the seat that cannot resolve it"
    assert "isabled" in message, "a present-but-off pin must not be reported as unknown"


def test_create_rejects_wholly_absent_skill(seats, board):
    """AC2: nothing on disk anywhere — the genuine unknown, reported as unknown."""
    _write_skill(seats["alpha"] / "skills", "present-skill")

    with kbc.connect(board) as conn, pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="t", assignee="alpha", skills=["no-such-skill"])
    message = str(exc.value)
    assert "no-such-skill" in message
    assert "nknown" in message, "an absent skill must be reported as unknown"


def test_create_reports_every_bad_name_in_one_message(seats, board):
    """AC3: an operator fixing five pins learns about all five in one round trip."""
    _write_skill(seats["alpha"] / "skills", "kept-skill")
    _write_skill(seats["alpha"] / "skills", "off-skill")
    _disable(seats["alpha"], "off-skill")

    with kbc.connect(board) as conn, pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="t", assignee="alpha",
                       skills=["kept-skill", "off-skill", "ghost-one", "ghost-two"])
    message = str(exc.value)
    for name in ("off-skill", "ghost-one", "ghost-two"):
        assert name in message, f"{name} missing from the combined message: {message}"
    assert "kept-skill" not in message, "a resolvable pin must not be reported"


def test_create_accepts_skills_that_resolve_in_the_assignees_seat(seats, board):
    """AC4: the happy path still creates — validation must not become a wall."""
    _write_skill(seats["alpha"] / "skills", "kept-skill")
    _write_skill(seats["alpha"] / "skills", "sdlc-review")

    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee="alpha",
                              skills=["kept-skill", "sdlc-review"])


def test_unassigned_card_still_creates_with_any_pins(seats, board):
    """AC5: no assignee means no seat to resolve from, so validation is skipped, not guessed."""
    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee=None,
                              skills=["definitely-not-a-real-skill"])


def test_broken_profile_config_does_not_block_creation(seats, board):
    """AC6: validation is a guard rail, not a gate — a flaky scan must never stop the fleet."""
    (seats["alpha"] / "config.yaml").write_text("skills: [this is not a mapping\n", encoding="utf-8")
    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee="alpha", skills=["ghost-skill"])


def test_unknown_assignee_seat_does_not_block_creation(seats, board):
    """A name that is not a live profile has no seat to resolve from; degrade to no-op."""
    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee="reviewer", skills=["ghost-skill"])


def test_create_validation_is_scoped_to_the_assignee_not_the_creator(seats, board):
    """A pin valid for the creator but absent from the assignee's seat is still a bad pin."""
    _write_skill(seats["launch"] / "skills", "creator-only-skill")
    with kbc.connect(board) as conn, pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="t", assignee="alpha", skills=["creator-only-skill"])
    assert "creator-only-skill" in str(exc.value)


# ---------------------------------------------------------------------------
# DISPATCH layer — the pins Hermes itself assembles (owns the real incident)
# ---------------------------------------------------------------------------


def test_review_lane_refuses_to_spawn_when_the_seat_cannot_resolve_sdlc_review(
        seats, board, monkeypatch):
    """THE regression guard for the 117 deaths.

    ``lane == "review"`` force-injects ``sdlc-review`` into ``claimed.skills``. When the claiming
    seat has it under ``skills.disabled``, the worker used to spawn anyway and die at
    ``Initializing agent...`` — repeatedly, on a card that pinned nothing at all. The dispatcher
    must refuse BEFORE the spawn and say which skill and which seat.
    """
    _write_skill(seats["alpha"] / "skills", "sdlc-review")
    _disable(seats["alpha"], "sdlc-review")

    spawned: list[str] = []

    def _spy_spawn(task, workspace, **kwargs):
        spawned.append(task.id)
        return 4242

    with kbc.connect(board) as conn:
        task_id = _stage_review(conn, "alpha")
        monkeypatch.setattr(kbd, "profile_exists", lambda name: True, raising=False)
        monkeypatch.setattr(kbd._kb, "profile_exists", lambda name: True, raising=False)
        kbd.dispatch_once(conn, spawn_fn=_spy_spawn)
        task = kb.get_task(conn, task_id)
        assert task is not None

    assert not spawned, "no worker may be spawned for a seat that cannot load its forced skill"
    failure = task.last_failure_error or ""
    assert "sdlc-review" in failure, f"the failure must name the skill, got {failure!r}"
    assert "alpha" in failure, f"the failure must name the seat, got {failure!r}"


def test_review_lane_spawns_normally_when_the_seat_can_resolve_sdlc_review(
        seats, board, monkeypatch):
    """The guard must not swallow the healthy path — the same card on a healthy seat spawns."""
    _write_skill(seats["alpha"] / "skills", "sdlc-review")

    spawned: list[str] = []

    def _spy_spawn(task, workspace, **kwargs):
        spawned.append(task.id)
        return 4242

    with kbc.connect(board) as conn:
        task_id = _stage_review(conn, "alpha")
        monkeypatch.setattr(kbd._kb, "profile_exists", lambda name: True, raising=False)
        kbd.dispatch_once(conn, spawn_fn=_spy_spawn)

    assert spawned == [task_id]


def test_dispatch_guard_covers_card_pinned_skills_too(seats, board, monkeypatch):
    """The dispatcher validates the WHOLE assembled set, not only its own injection.

    The card is created while the pin still resolves, then the seat's own ``skills.disabled``
    changes under it — the exact race create-time validation cannot cover, and the reason the
    dispatch check exists as a separate layer. (A pin that is already broken at create time is
    rejected by ``create_task`` itself, before a card ever exists.)
    """
    _write_skill(seats["alpha"] / "skills", "sdlc-review")
    _write_skill(seats["alpha"] / "skills", "pinned-skill")

    spawned: list[str] = []
    with kbc.connect(board) as conn:
        task_id = kb.create_task(conn, title="t", assignee="alpha", skills=["pinned-skill"])
        _disable(seats["alpha"], "pinned-skill")  # seat changes after the card exists
        monkeypatch.setattr(kbd._kb, "profile_exists", lambda name: True, raising=False)
        kbd.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id) or 4242)
        failure = ((kb.get_task(conn, task_id) or kb.Task()).last_failure_error or "")

    assert not spawned, "the card's own pin is disabled now; no worker may start"
    assert "pinned-skill" in failure, f"the failure must name the card's own pin, got {failure!r}"


def test_dispatch_guard_does_not_block_when_the_scan_cannot_run(seats, board, monkeypatch):
    """A seat whose config cannot be read must still get its worker — the rail, not the gate."""
    (seats["alpha"] / "config.yaml").write_text("skills: [unparseable\n", encoding="utf-8")
    _write_skill(seats["alpha"] / "skills", "sdlc-review")

    spawned: list[str] = []
    with kbc.connect(board) as conn:
        task_id = _stage_review(conn, "alpha")
        monkeypatch.setattr(kbd._kb, "profile_exists", lambda name: True, raising=False)
        kbd.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id) or 4242)

    assert spawned == [task_id]