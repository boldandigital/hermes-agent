"""Would a pinned skill actually LOAD in the assignee's own seat?

A Kanban card's ``skills`` are pinned by identifier, not by capability, and ``skills.disabled`` is
PER-PROFILE. A name that resolves on the seat that created the card can be absent — or switched off
— on the seat that will run it, and the worker then dies at ``Initializing agent...`` before it ever
reads the card. The dispatcher makes this reachable without any pin at all: the ``review`` lane
force-injects ``sdlc-review`` into ``claimed.skills``.

Both call sites (create and dispatch) ask this module the same question, and the answer is produced
by the SAME machinery the loader uses:

* search roots — :func:`tools.skills_tool._skill_search_dirs`, exactly what ``skill_view`` calls, so
  tier precedence (project > local > create_dir > external) cannot diverge between check and load;
* resolution — :func:`tools.skills_tool._locate_skill`, the very step ``skill_view`` runs before it
  reads a body, so ambiguity, shadowing and cross-tier ties answer identically here and at load;
* the disabled gate — :func:`agent.skill_commands._names_disabled` against
  :func:`agent.skill_utils.get_disabled_skill_names`, the pre-load gate in
  ``agent.skill_commands._load_skill_blocks``, checked BEFORE resolution exactly as the loader does.

Deliberately NOT used: ``skill_view`` / ``build_preloaded_skills_prompt``. They read the full body,
mark the skill read, and run ``pm.ensure`` for declared tool deps — a validator must have no side
effects on the thing it is validating.

The home is bound with ``set_hermes_home_override`` (the seam
``agent.skill_commands.build_auto_load_prompt`` already uses for cross-home reads) so the config, the
disabled list and the ``<home>/skills`` tree all resolve under the ASSIGNEE's home, not the
dispatcher's.

This module answers "can this seat load these names"; it never decides policy. Callers decide
whether an unresolvable pin is fatal.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterable, Optional


def resolve_seat_home(assignee: Optional[str]) -> Optional[Path]:
    """The assignee's own ``HERMES_HOME``, or None when there is no seat to resolve from.

    None is a deliberate answer, not a failure: an unassigned card cannot be dispatched, and a name
    that is not a live profile has no directory to read. Callers skip validation on None rather than
    guessing a seat — a wrong guess would reject cards for skills their real assignee can load.
    """
    if not assignee:
        return None
    try:
        from hermes_cli.profiles import resolve_profile_env
        return Path(resolve_profile_env(assignee))
    except Exception:
        # Missing/tombstoned profile, or a name that is not a valid profile id at all.
        return None


@contextlib.contextmanager
def _seat_scope(home: Path):
    """Bind config + skills-tree reads to *home* for the duration of the check."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def unresolvable_skills(
    names: Iterable[str], *, seat_home: Optional[Path],
) -> Optional[tuple[list[str], list[str]]]:
    """Split *names* into ``(disabled, unknown)`` as *seat_home*'s own loader would see them.

    Returns None when the check could not be run at all (no seat, unreadable config, a resolver
    error). None is NOT an all-clear and NOT a rejection — callers treat it as "no information,
    carry on", because this is a guard rail and a flaky scan must never stop the fleet.

    *disabled* names exist on disk but are switched off in that seat's ``skills.disabled``;
    *unknown* names do not resolve there at all. They are kept apart because the operator's fix is
    different: one is a config flip, the other is a typo or a category-vs-skill mix-up.
    """
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names or seat_home is None or not Path(seat_home).is_dir():
        return None
    try:
        with _seat_scope(Path(seat_home)):
            if not _seat_config_usable(Path(seat_home)):
                return None
            return _classify(names)
    except Exception:
        # Any resolver/config failure degrades to "no information" rather than a wrong verdict.
        return None


def _seat_config_usable(seat_home: Path) -> bool:
    """False when the seat's own config.yaml cannot be parsed, so no verdict can be trusted.

    ``load_config`` does NOT raise on a malformed file — it logs a corruption warning and falls back
    to defaults, so ``skills.disabled`` silently reads as empty. Verdict on that basis would be
    actively wrong: a pin that the operator disabled in the very file we failed to read would be
    reported as fine. Degrade to "no information" instead.
    """
    config_path = Path(seat_home) / "config.yaml"
    if not config_path.is_file():
        return True  # no config: the defaults genuinely apply
    from agent.skill_utils import yaml_load
    try:
        return isinstance(yaml_load(config_path.read_text(encoding="utf-8-sig")), dict)
    except Exception:
        return False


def _classify(names: list[str]) -> tuple[list[str], list[str]]:
    """(disabled, unknown) for *names* under the currently-bound home."""
    from agent.skill_commands import _names_disabled
    from agent.skill_utils import get_disabled_skill_names
    from tools.skills_tool import _locate_skill, _skill_search_dirs

    try:
        disabled_names = get_disabled_skill_names()
    except Exception:
        disabled_names = set()
    roots, _active = _skill_search_dirs()

    disabled: list[str] = []
    unknown: list[str] = []
    for name in names:
        # Order matches _load_skill_blocks: the disabled gate runs BEFORE load, never after.
        if _names_disabled(disabled_names, name):
            disabled.append(name)
            continue
        try:
            error, _skill_dir, skill_md = _locate_skill(name, None, roots)
        except Exception:
            # A resolver blow-up on one name must not condemn the whole set.
            continue
        if error is not None or skill_md is None:
            unknown.append(name)
    return disabled, unknown


def format_unresolvable(
    disabled: Iterable[str], unknown: Iterable[str], *, seat: Optional[str],
) -> str:
    """One message naming every unresolvable pin and the seat that cannot load it.

    Reuses ``format_missing_skills`` for the disabled/unknown split so the wording stays identical to
    what a worker reports at init (owned by card t_717b821e — this calls it, never edits it), and
    prepends the seat, because the same name resolves fine elsewhere.
    """
    from agent.skill_commands import format_missing_skills

    disabled = [n for n in disabled if n]
    unknown = [n for n in unknown if n]
    if not disabled and not unknown:
        return ""
    where = f" for profile '{seat}'" if seat else ""
    return f"cannot load pinned skill(s){where}: {format_missing_skills(disabled + unknown, disabled)}"