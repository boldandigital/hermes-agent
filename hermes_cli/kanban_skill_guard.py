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
  ``agent.skill_commands._load_skill_blocks``, checked BEFORE resolution exactly as the loader does;
* identifier normalization — :func:`agent.skill_utils.normalize_skill_lookup_name`, which
  ``_load_skill_payload`` applies to the raw pin BEFORE calling ``skill_view``. It is what turns a
  trusted absolute skill path into the relative spelling the lookup accepts, so mirroring the
  pre-flight WITHOUT it would convict a pin the worker loads fine.

The mirrored steps run on the normalized spelling; operator-facing messages keep the ORIGINAL pin,
because that is the string on the card.

Deliberately NOT used: ``skill_view`` / ``build_preloaded_skills_prompt``. They read the full body,
mark the skill read, and run ``pm.ensure`` for declared tool deps — a validator must have no side
effects on the thing it is validating.

The home is bound with ``set_hermes_home_override`` (the seam
``agent.skill_commands.build_auto_load_prompt`` already uses for cross-home reads) so the config, the
disabled list and the ``<home>/skills`` tree all resolve under the ASSIGNEE's home, not the
dispatcher's.

The card's WORKSPACE is bound with ``set_session_cwd`` for the same reason: the project tier
resolves through ``find_project_root()`` → ``resolve_agent_cwd()``, i.e. session cwd, then
``TERMINAL_CWD``, then the process cwd. A worker is launched with ``cwd=workspace`` AND
``env["TERMINAL_CWD"] = workspace``, while the dispatch-time check runs inside the dispatcher
process — so without this binding the two halves answered one question from two different project
roots, and a live project-tier pin was convicted and the card parked in ``blocked`` with a false
"Unknown skill(s)". Binding is the fix; skipping the tier would only hide the disagreement.

Every degrade path (no seat, unreadable config, resolver error, per-name failure) logs a WARNING and
returns the SAME verdict it always did. A guard rail whose seats silently stopped being guarded is
indistinguishable from a healthy one, which is the failure mode decision item 5 was written against.

Deliberately NOT mirrored: the DECLARED-NAME gate (``skill_view`` checks the resolved frontmatter
``name:``, not only the identifier a card pins) and the PLATFORM gate (``skill_matches_platform``).
Both are therefore FALSE NEGATIVES — a pin this guard passes but the loader refuses to serve, which
is the lenient direction this module is happy to fail in. Do not assume they are covered here.

This module answers "can this seat load these names"; it never decides policy. Callers decide
whether an unresolvable pin is fatal.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


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
        # Missing/tombstoned profile, or a name that is not a valid profile id at all. Warned,
        # because a seat that cannot be resolved is a seat that is no longer being guarded.
        logger.warning(
            "kanban skill guard: seat %r does not resolve to a profile home — validation skipped",
            assignee, exc_info=True)
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


@contextlib.contextmanager
def _workspace_scope(workspace: Optional[str | Path]):
    """Bind *workspace* as the agent's logical cwd for the duration of the check.

    ``resolve_agent_cwd`` reads the session cwd FIRST, then the scope's ``TERMINAL_CWD``, then the
    process cwd — so pinning the card's workspace here makes the guard's project tier resolve
    exactly like the worker's, which is launched with ``cwd=workspace`` and
    ``env["TERMINAL_CWD"] = workspace``.

    A falsy *workspace* binds NOTHING (``set_session_cwd`` is not called at all): callers that
    genuinely do not know the workspace — a scratch card at create time — must keep today's
    behaviour, and a binding that pointed at a not-yet-created directory would only add a
    misleading "does not exist" warning before falling through to the same answer.
    """
    if not workspace:
        yield
        return
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    token = set_session_cwd(str(workspace))
    try:
        yield
    finally:
        reset_session_cwd(token)


def unresolvable_skills(
    names: Iterable[str], *, seat_home: Optional[Path],
    workspace: Optional[str | Path] = None,
) -> Optional[tuple[list[str], list[str]]]:
    """Split *names* into ``(disabled, unknown)`` as *seat_home*'s own loader would see them.

    *workspace* is the card's workspace: binding it (see ``_workspace_scope``) is what puts the
    guard's project tier on the same root the worker resolves it from. Callers that know the
    workspace must pass it; omitting it does not degrade to a different verdict for local skills,
    only for project-local ones.

    Returns None when the check could not be run at all (no seat, unreadable config, a resolver
    error). None is NOT an all-clear and NOT a rejection — callers treat it as "no information,
    carry on", because this is a guard rail and a flaky scan must never stop the fleet. Every one
    of those paths logs a WARNING: a fleet whose seats silently stopped being guarded must be
    distinguishable from a healthy one.

    *disabled* names exist on disk but are switched off in that seat's ``skills.disabled``;
    *unknown* names do not resolve there at all. They are kept apart because the operator's fix is
    different: one is a config flip, the other is a typo or a category-vs-skill mix-up.
    """
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names or seat_home is None or not Path(seat_home).is_dir():
        if not names:
            reason = "no skill names to check"
        elif seat_home is None:
            reason = "assignee seat does not resolve to a profile home"
        else:
            reason = f"seat directory does not exist: {seat_home}"
        logger.warning(
            "kanban skill guard: no verdict for %d pin(s) (%s) — validation skipped",
            len(names), reason)
        return None
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(_seat_scope(Path(seat_home)))
            stack.enter_context(_workspace_scope(workspace))
            if not _seat_config_usable(Path(seat_home)):
                return None
            return _classify(names)
    except Exception:
        # Any resolver/config failure degrades to "no information" rather than a wrong verdict.
        logger.warning(
            "kanban skill guard: scan failed for seat %s — validation skipped (no verdict)",
            seat_home, exc_info=True)
        return None


def _seat_config_usable(seat_home: Path) -> bool:
    """False when the seat's own config.yaml cannot be parsed, so no verdict can be trusted.

    ``load_config`` does NOT raise on a malformed file — it logs a corruption warning and falls back
    to defaults, so ``skills.disabled`` silently reads as empty. Verdict on that basis would be
    actively wrong: a pin that the operator disabled in the very file we failed to read would be
    reported as fine. Degrade to "no information" instead — loudly, since an unparsed seat config
    means that seat's ``skills.disabled`` is currently unenforced everywhere, not just here.
    """
    config_path = Path(seat_home) / "config.yaml"
    if not config_path.is_file():
        return True  # no config: the defaults genuinely apply
    from agent.skill_utils import yaml_load
    try:
        return isinstance(yaml_load(config_path.read_text(encoding="utf-8-sig")), dict)
    except Exception:
        logger.warning(
            "kanban skill guard: seat config %s is unreadable — validation skipped (no verdict)",
            config_path, exc_info=True)
        return False


def _served_ok(served: str) -> bool:
    """Did the plugin registry actually SERVE this name?

    ``_resolve_plugin_skill`` returns the registry's JSON verdict, not a yes/no: it is non-None both
    when a plugin serves the skill and when the registry refuses it (plugin disabled, stale entry
    pointing at a deleted file, skill absent from a plugin that exists). Only the former loads, so
    the guard must read ``success`` rather than treat any non-None answer as resolved — otherwise a
    genuinely unloadable pin passes as fine.

    An unreadable payload is treated as served: this guard must never convict on a verdict it could
    not actually read.
    """
    try:
        return bool(json.loads(served).get("success"))
    except Exception:
        return True


def _normalized_lookup(name: str) -> str:
    """The spelling ``skill_view`` is actually called with, exactly as ``_load_skill_payload`` gets it.

    ``_load_skill_payload`` (``agent/skill_commands.py``) does ``normalize_skill_lookup_name(raw)``
    FIRST and hands only the result to ``skill_view``. That normalization is what turns a trusted
    ABSOLUTE skill path — the form slash commands and cron store — into the relative spelling the
    lookup path pre-flight accepts. A guard that mirrors the pre-flight without mirroring the
    normalization convicts a pin the worker loads fine.

    Falls back to *name* on any failure: the pre-flight downstream still gets its say, so a
    normalization that cannot answer degrades to "no extra information", never to a conviction.
    """
    try:
        from agent.skill_utils import normalize_skill_lookup_name
        return normalize_skill_lookup_name(name) or name
    except Exception:
        return name


def _classify(names: list[str]) -> tuple[list[str], list[str]]:
    """(disabled, unknown) for *names* under the currently-bound home."""
    from agent.skill_commands import _names_disabled
    from agent.skill_utils import get_disabled_skill_names
    from tools.skills_tool import (
        _locate_skill, _resolve_plugin_skill, _skill_lookup_path_error, _skill_search_dirs,
    )

    try:
        disabled_names = get_disabled_skill_names()
    except Exception:
        disabled_names = set()
    roots, _active = _skill_search_dirs()

    disabled: list[str] = []
    unknown: list[str] = []
    for name in names:
        # Order matches _load_skill_blocks: the disabled gate runs BEFORE load, never after.
        # The disabled gate takes the ORIGINAL identifier — _names_disabled normalizes internally
        # for exactly this reason (its docstring cites the absolute-path pin).
        if _names_disabled(disabled_names, name):
            disabled.append(name)
            continue
        try:
            # Resolution runs on the NORMALIZED spelling, because that is what _load_skill_payload
            # hands skill_view. Every step below therefore sees the same string the loader sees.
            # Messages keep the ORIGINAL pin — that is what the operator wrote on the card.
            lookup = _normalized_lookup(name)
            # Same pre-flight skill_view runs, so a name the loader refuses on sight (traversal,
            # absolute path outside every trusted root) is not "resolved" here either.
            if _skill_lookup_path_error(lookup):
                unknown.append(name)
                continue
            # Mirrors skill_view's ':' dispatch. A ':' name is answered by the plugin registry
            # FIRST; when no such plugin exists the registry hands back the on-disk `category/skill`
            # spelling, which the flat-tree scan must also try. Passing that fall-through through is
            # what keeps this guard agreeing with the loader: without it, the `category:skill`
            # spelling of a live skill scanned as nothing and was reported "Unknown" — a false
            # positive that, at dispatch, parks a healthy card and burns the breaker.
            local_category_name: str | None = None
            if ":" in lookup:
                served, local_category_name = _resolve_plugin_skill(lookup, None, None, False)
                if served is not None:
                    # The registry answered definitively; the loader never falls through to disk.
                    if not _served_ok(served):
                        unknown.append(name)
                    continue
                if local_category_name and _skill_lookup_path_error(local_category_name):
                    unknown.append(name)
                    continue
            error, _skill_dir, skill_md = _locate_skill(lookup, local_category_name, roots)
            if error is not None or skill_md is None:
                unknown.append(name)
        except Exception:
            # A resolver blow-up on one name must not condemn the whole set. LENIENT BY DESIGN:
            # this drops the name from BOTH lists, so a bad pin reads as "no information" rather
            # than as a conviction. A missed check is recoverable; a false one is not — it refuses
            # a card whose skills all load, and at dispatch that parks the card in `blocked`.
            # The name vanishing from BOTH lists is exactly what makes this a silent hole, so say
            # so out loud: "checked, no verdict" must never look identical to "checked, fine".
            logger.warning(
                "kanban skill guard: resolution of pin %r raised — dropping it from both lists "
                "(no verdict for this pin)", name, exc_info=True)
            continue
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