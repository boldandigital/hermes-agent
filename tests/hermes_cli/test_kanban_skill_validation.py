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
both delegate to :mod:`hermes_cli.kanban_skill_guard`, which MIRRORS the loader's own steps
(``_skill_search_dirs`` → ``_locate_skill``, the ``skills.disabled`` pre-load gate, the identifier
normalization) under the assignee's own ``HERMES_HOME`` and the card's workspace — deliberately
NOT ``skill_view``/``build_preloaded_skills_prompt``, which read bodies and mark skills read, so a
validator has no side effects on the thing it validates.

Ground truth for every test here is :func:`agent.skill_commands._load_skill_payload` — the worker's
own call, bound the way the worker is bound (seat home, session cwd = the card's workspace). Each
test asserts THAT loads (or refuses) BEFORE asserting anything about the guard: a fixture the
loader cannot serve would otherwise read exactly like a guard defect, and a guard that returns the
right answer for the wrong reason would pass.

Validation is a guard rail, not a gate: if the scan cannot run (unreadable config, broken profile
dir, unknown assignee) creation and dispatch both proceed untouched, loudly — every degrade path
logs a WARNING. A flaky scan must never stop the fleet, and a fleet that silently stopped being
guarded must never look like a healthy one.
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


def _register_plugin_skill(monkeypatch, tmp_path, namespace: str, bare: str) -> Path:
    """Register ``namespace:bare`` in the plugin registry the way a real plugin does.

    Follows the pattern the repo's own ``tests/tools/test_plugin_skills.py`` uses: a FRESH
    ``PluginManager`` monkeypatched over the module singleton, with the entry keyed the way a
    plugin's ``register()`` keys it (``path``/``plugin``/``bare_name``/``description``).

    Mutating the live singleton instead does not survive: ``_resolve_plugin_skill`` calls
    ``discover_plugins()``, whose scan replaces the registry, so the entry vanished mid-test and
    the guard and the worker both (correctly) reported the pin as unloadable — a fixture artifact
    that looked exactly like a guard defect. monkeypatch restores the singleton afterwards, so a
    registration cannot leak into an unrelated test.
    """
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager

    skill_dir = tmp_path / "plugins" / namespace / "skills" / bare
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(f"---\nname: {bare}\ndescription: {bare} desc\n---\n\n{bare} body.\n", encoding="utf-8")

    pm = PluginManager()
    pm._plugin_skills[f"{namespace}:{bare}"] = {
        "path": md, "plugin": namespace, "bare_name": bare, "description": "",
    }
    # _discovered=True: the fixture IS the registry, so the scan must not rescan over it.
    pm._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", pm)
    return md


def _trust_project_root(home: Path, root: Path) -> None:
    """Add *root* to the seat's ``skills.trusted_project_dirs``.

    That is the exact key ``agent.skill_utils._project_trusted_dirs_from_config`` reads
    (``skills.trusted_project_dirs``, absolute paths, ``~``/``${VAR}`` expanded). Project-local
    skills are searched ONLY when the nearest ``.git`` ancestor of the agent cwd is trusted
    (``_current_project_root(trusted=True)`` → ``get_project_skills_dirs``), so without this line
    the ground-truth probe below would fail and every assertion after it would be vacuous.
    """
    (home / "config.yaml").write_text(
        "skills:\n  disabled: []\n  trusted_project_dirs:\n    - " + str(root) + "\n",
        encoding="utf-8")


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


def test_create_accepts_namespaced_category_colon_form_that_resolves(
        seats, board, monkeypatch, tmp_path):
    """AC4 regression: the ``category:skill`` spelling is the SAME skill, not an unknown one.

    ``skill_view`` dispatches any name containing ``:`` to the plugin registry first, and when no
    such plugin exists it returns ``(None, "category/skill")`` — a fall-through the flat-tree scan
    then joins onto each search dir. The guard used to pass ``local_category_name=None``, so it
    scanned only the literal ``:`` spelling, found nothing, and convicted a skill the worker loads
    fine. Eight cards already pin this spelling (``research:arxiv``), and the operator's error
    message said "Unknown" — sending them hunting for a typo that does not exist.
    """
    _write_skill(seats["alpha"] / "skills", "arxiv", category="research")

    # Ground truth FIRST: the worker must genuinely load both spellings, or this test proves
    # nothing. The slash form passing while the colon form failed is what isolated the defect.
    from agent.skill_commands import _load_skill_payload
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(seats["alpha"]))
    try:
        assert _load_skill_payload("research/arxiv"), "slash form must load"
        assert _load_skill_payload("research:arxiv"), (
            "fixture is wrong, not the guard: the loader must serve the colon form too")
    finally:
        reset_hermes_home_override(token)

    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee="alpha", skills=["research:arxiv"]), (
            "the loader serves research:arxiv; refusing it violates AC4 and, at dispatch, parks a "
            "healthy card in `blocked` with a false 'Unknown skill(s)'")


def test_create_accepts_plugin_served_colon_form_that_resolves(seats, board, monkeypatch, tmp_path):
    """AC4 regression: a registry-served ``plugin:skill`` is answered BEFORE ``_locate_skill``.

    The registry never touches the skills tree, so no search-dir scan can find it. Convicting it
    is the same false positive as the category form, one layer deeper.
    """
    _register_plugin_skill(monkeypatch, tmp_path, "mem", "recall")

    # Ground truth FIRST: if the worker's own loader cannot load this pin, the assertion below
    # would be proving nothing (a broken fixture reads exactly like a guard defect).
    from agent.skill_commands import _load_skill_payload
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(seats["alpha"]))
    try:
        assert _load_skill_payload("mem:recall"), "fixture must serve the pin the loader way"
    finally:
        reset_hermes_home_override(token)

    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee="alpha", skills=["mem:recall"]), (
            "the plugin registry serves mem:recall; the guard must not convict it")


def test_create_accepts_absolute_path_pin_the_loader_normalizes(seats, board):
    """AC4 regression: an ABSOLUTE PATH pin loads, so the guard must not convict it.

    ``_load_skill_payload`` normalizes the identifier FIRST
    (``agent/skill_commands.py``: ``normalized = normalize_skill_lookup_name(raw_identifier)``),
    and ``normalize_skill_lookup_name`` exists precisely to translate a trusted absolute skill
    path — the form slash commands and cron store — into the relative spelling ``skill_view``
    accepts. The guard mirrored the check that FOLLOWS that normalization
    (``_skill_lookup_path_error``) without mirroring the normalization itself, so it convicted a
    pin the worker loads fine. That is AC4 violated, and it is a REGRESSION: the round-1 guard had
    no pre-flight at all and answered "fine" for this same pin.

    It matters more than the blast radius suggests (0 of 148 live cards use this form): the
    dispatch layer does not merely refuse. ``_validate_worker_skills`` calls
    ``_record_task_failure(outcome="spawn_failed")``, so the first genuinely-abs-path card parks
    itself in ``blocked`` carrying a false "Unknown skill(s)" in ``last_failure_error`` — an
    unfalsifiable lie about a healthy pin, produced by the very layer meant to keep the fleet from
    being stranded by bad pins.
    """
    skill_dir = _write_skill(seats["alpha"] / "skills", "arxiv", category="research")

    # Ground truth FIRST: the worker's own loader is the reference. If it cannot load this pin,
    # the assertion below would prove nothing (a broken fixture reads like a guard defect).
    from agent.skill_commands import _load_skill_payload
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(seats["alpha"]))
    try:
        assert _load_skill_payload(str(skill_dir)), (
            "fixture is wrong, not the guard: the loader must load the absolute-path pin")
    finally:
        reset_hermes_home_override(token)

    with kbc.connect(board) as conn:
        assert kb.create_task(conn, title="t", assignee="alpha", skills=[str(skill_dir)]), (
            "the loader normalizes this absolute path and loads it; refusing violates AC4 and, at "
            "dispatch, parks a healthy card in `blocked` with a false 'Unknown skill(s)'")


def test_create_rejects_pins_that_only_the_path_preflight_convicts(seats, board):
    """The pre-flight is load-bearing: normalizing must not become a licence to load anything.

    Measured by neutering ``_skill_lookup_path_error`` and diffing verdicts, these two pins are
    the ones it decides ALONE — without it the guard answers "fine" for both while the worker's
    loader refuses them. Both are FALSE NEGATIVES (a missed check), which is the direction this
    guard is happy to fail in; the point of the test is that removing the pre-flight is not a
    silent no-op, so a future refactor that drops it gets caught rather than shipped.

    * ``../skills/inside-skill`` — a traversal that resolves *back inside* the trusted root. The
      flat-tree scan would happily find the skill; only the path check refuses it.
    * ``/etc/passwd`` — an absolute path outside every trusted root. ``normalize_skill_lookup_name``
      passes it through unchanged (its own docstring: "skill_view will reject it"), and
      ``_locate_skill`` cannot even join it (``rglob`` rejects non-relative patterns), so without
      the pre-flight the name drops out of both lists and reads as fine.
    """
    skill_dir = _write_skill(seats["alpha"] / "skills", "inside-skill")

    # Ground truth FIRST, per pin: the loader refuses both, so the guard must too.
    from agent.skill_commands import _load_skill_payload
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(seats["alpha"]))
    try:
        assert not _load_skill_payload("../skills/inside-skill"), (
            "fixture is wrong: the loader must refuse a traversal that re-enters the root")
        assert not _load_skill_payload("/etc/passwd"), (
            "fixture is wrong: the loader must refuse an absolute path outside the trusted roots")
        # And the traversal really does resolve back inside the root — otherwise it would be
        # caught by the scan finding nothing, and the pre-flight would not be what convicts it.
        assert (skill_dir / "SKILL.md").exists()
    finally:
        reset_hermes_home_override(token)

    for pin in ("../skills/inside-skill", "/etc/passwd"):
        with kbc.connect(board) as conn, pytest.raises(ValueError) as exc:
            kb.create_task(conn, title="t", assignee="alpha", skills=[pin])
        assert pin in str(exc.value), f"the message must name the pin as given: {pin}"


def test_create_rejects_colon_form_the_registry_refuses(seats, board, monkeypatch, tmp_path):
    """The registry's NO counts too — ``served is not None`` is not the same as "resolved".

    ``_resolve_plugin_skill`` returns its JSON verdict for refusals as well as successes (plugin
    disabled, stale entry whose SKILL.md was deleted, skill absent from a plugin that exists). A
    guard that treats any non-None answer as resolved passes a pin the loader genuinely cannot
    load — a missed death, which is the failure this whole guard exists to prevent.
    """
    md = _register_plugin_skill(monkeypatch, tmp_path, "mem", "recall")
    md.unlink()  # the registered SKILL.md is gone: the registry's stale-entry refusal

    from agent.skill_commands import _load_skill_payload
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(seats["alpha"]))
    try:
        assert not _load_skill_payload("mem:recall"), (
            "fixture is wrong: the loader must FAIL on a stale registry entry")
    finally:
        reset_hermes_home_override(token)

    # The stale-entry refusal is SELF-CLEANING (``_resolve_plugin_skill`` calls
    # ``remove_plugin_skill``), so that probe above consumed the registry entry. Re-register it, or
    # the guard finds an empty registry and reports unknown via the DISK scan instead — the test
    # would still pass, but it would no longer be exercising ``_served_ok`` at all.
    _register_plugin_skill(monkeypatch, tmp_path, "mem", "recall")
    (tmp_path / "plugins" / "mem" / "skills" / "recall" / "SKILL.md").unlink()

    with kbc.connect(board) as conn, pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="t", assignee="alpha", skills=["mem:recall"])
    assert "mem:recall" in str(exc.value)


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


def test_project_tier_pin_resolves_from_the_card_workspace_not_the_dispatcher_cwd(
        seats, board, monkeypatch, tmp_path):
    """The guard resolved TIER_PROJECT from the DISPATCHER's cwd; the worker resolves it from the
    card's workspace — so a healthy pin was convicted and the card parked.

    ``_skill_search_dirs`` includes the project tier, and its roots come from
    ``get_project_skills_dirs()`` → ``find_project_root()`` → ``resolve_agent_cwd()``: session cwd,
    then ``TERMINAL_CWD``, then the PROCESS cwd. The worker is spawned with ``cwd=workspace`` AND
    ``env["TERMINAL_CWD"] = workspace`` (``kanban_db_dispatch.py``: ``if workspace and
    os.path.isabs(workspace) and os.path.isdir(workspace): env["TERMINAL_CWD"] = workspace``) — a
    contract the repo already pins in ``test_kanban_worker_terminal_cwd.py``. The guard runs INSIDE
    the dispatcher process, so it answered from the dispatcher's own project root, found no
    project tier at all, and reported a live pin as ``Unknown skill(s)``.

    That is not a warning at dispatch: ``_validate_worker_skills`` records it via
    ``_record_task_failure(outcome="spawn_failed")``, so the card parks itself in ``blocked``
    carrying a false verdict — the guard rail stranding the fleet on exactly the pin it exists to
    protect. This suite had ZERO project-tier coverage, which is why 17 green tests sat on top of
    it.

    Shape: the card is created from INSIDE the project (an operator's cwd), so create-time
    validation sees the project tier and lets the pin through; the dispatcher then runs from its
    own cwd, which is not the workspace. Both halves must agree.
    """
    workspace = tmp_path / "card-workspace"
    (workspace / ".git").mkdir(parents=True)  # a bare .git DIR is what find_project_root accepts
    _write_skill(workspace / ".hermes" / "skills", "workspace-skill")
    _trust_project_root(seats["alpha"], workspace)

    # Ground truth FIRST: bind the workspace exactly as the worker is bound (session cwd, which
    # resolve_agent_cwd reads BEFORE TERMINAL_CWD and before the process cwd) plus the seat home
    # `hermes -p alpha` gives the worker, then ask the WORKER's own loader. If that cannot load the
    # pin, every assertion below proves nothing.
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from agent.skill_commands import _load_skill_payload
    from agent.skill_utils import get_project_skills_dirs
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    cwd_token = set_session_cwd(str(workspace))
    home_token = set_hermes_home_override(str(seats["alpha"]))
    try:
        assert [Path(p) for p in get_project_skills_dirs()] == [workspace / ".hermes" / "skills"], (
            "fixture is wrong: the trusted project root must expose its skills dir, or the loader "
            "cannot serve the pin the way a worker launched into this workspace would")
        assert _load_skill_payload("workspace-skill"), (
            "fixture is wrong, not the guard: a worker with cwd/TERMINAL_CWD=workspace loads this "
            "pin, so the guard must not call it unknown")
    finally:
        reset_hermes_home_override(home_token)
        reset_session_cwd(cwd_token)

    # The CREATE layer must agree, or the operator is refused while standing OUTSIDE the project. A
    # ``dir`` card's path is already final at create time, so ``_create_time_workspace`` binds it
    # for that check too (scratch/worktree pass None by design — their path is only known at
    # dispatch, and the dispatcher's check is the authority for those). Archived straight away so
    # this probe card cannot reach the dispatch ticks below.
    with kbc.connect(board) as conn:
        probe = kb.create_task(conn, title="t-create", assignee="alpha",
                               workspace_kind="dir", workspace_path=str(workspace),
                               skills=["workspace-skill"])
        kb.archive_task(conn, probe)

    spawned: list[str] = []
    with kbc.connect(board) as conn:
        cwd_token = set_session_cwd(str(workspace))
        try:
            task_id = kb.create_task(conn, title="t", assignee="alpha",
                                     workspace_kind="dir", workspace_path=str(workspace),
                                     skills=["workspace-skill"])
        finally:
            reset_session_cwd(cwd_token)

        monkeypatch.setattr(kbd._kb, "profile_exists", lambda name: True, raising=False)
        kbd.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id) or 4242)
        parked = kb.get_task(conn, task_id)

        assert spawned == [task_id], (
            "the worker loads 'workspace-skill' from its own workspace, so it must spawn; parking "
            f"the card in {getattr(parked, 'status', None)!r} with "
            f"{(parked.last_failure_error if parked else None)!r} is the false positive this guards")
        assert "Unknown skill(s)" not in ((parked.last_failure_error if parked else None) or "")

        # Teeth, on the same tier and through the same layer: a second card's pin STOPPED resolving
        # after it was created, so the guard must still refuse it. Without this the test would be
        # satisfied by a guard that passes every project-tier pin unconditionally — the no-op
        # validator that satisfies any suite which never exercised it.
        import shutil
        cwd_token = set_session_cwd(str(workspace))
        try:
            doomed_dir = _write_skill(workspace / ".hermes" / "skills", "doomed-skill")
            doomed = kb.create_task(conn, title="t2", assignee="alpha",
                                    workspace_kind="dir", workspace_path=str(workspace),
                                    skills=["doomed-skill"])
        finally:
            reset_session_cwd(cwd_token)
        shutil.rmtree(doomed_dir)
        kbd.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id) or 4242)
        refused = kb.get_task(conn, doomed)

    assert doomed not in spawned, (
        "a project-tier pin that no longer resolves must still be refused — the workspace binding "
        "must not turn the project tier into a blanket pass")
    failure = (refused.last_failure_error if refused else None) or ""
    assert "doomed-skill" in failure, f"the refusal must name the pin, got {failure!r}"