"""Probe-budget guard — bound the "one more probe round" failure mode.

The failure this module exists for: a turn keeps gathering information (reads,
searches, ``execute_code`` probes) round after round, announcing each batch as
"the last one", and never produces the deliverable. Every round re-sends the
whole transcript, so the cost compounds quadratically while the work product
stays at zero.

Two signals drive the guard:

* **Probe rounds** — tool rounds that produced nothing (``write_file`` /
  ``patch`` / … never ran). A turn that racks up rounds without a deliverable
  gets a notice, and eventually a controlled halt that hands the turn back to
  the user instead of burning the budget.
* **"Final batch" announcements** — the model says "three last facts, then I
  write" and then reads again. The announcement is the tell; the counter says
  how many times it has already been made.

Everything here is pure (no I/O beyond appending to a message list) so it can
be unit-tested without an agent.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# A round containing one of these tools produced something. Everything else is
# information gathering. ``terminal`` / ``execute_code`` are deliberately NOT
# here: they are the probe vector (a shell-out read looks like work), and the
# deliverable in this workflow is a file write.
DELIVERABLE_TOOL_NAMES = frozenset({
    "write_file", "patch", "skill_manage", "send_message", "cronjob_manage",
    "create_goal", "update_goal",
})

# Appended to the newest tool result when the turn has spent too many rounds
# gathering instead of producing. Cache-safe: it mutates the current turn's
# tool-result tail only (same contract as the iteration-budget warning).
PROBE_BUDGET_NOTICE = (
    "[SYSTEM NOTICE — probe budget] {rounds} tool rounds on this turn have produced no "
    "change. Stop gathering and act now: write or patch the deliverable from what you "
    "already have. If you are genuinely blocked, reply in one sentence with exactly what "
    "is missing — do not start another read round."
)

# The escalated variant, fired once the first notice was ignored.
PROBE_BUDGET_NOTICE_ESCALATED = (
    "[SYSTEM NOTICE — probe budget, final warning] {rounds} tool rounds on this turn have "
    "still produced no change, and the previous notice was ignored. You are out of "
    "exploration budget: make the edit or write the answer NOW from the information "
    "already in context, or state the blocker in one sentence. Another read round ends "
    "this turn."
)

# The controlled-halt reply. Shown to the user as the turn's answer.
PROBE_BUDGET_HALT_RESPONSE = (
    "I stopped after {rounds} tool rounds because the turn was still gathering "
    "information and had not produced the change yet. Every round re-sent the whole "
    "conversation, so continuing would burn tokens without moving the task forward.\n\n"
    "What I already have: the context gathered above. Say the word and I will apply the "
    "change from it — or tell me what to look at instead and I will run exactly one "
    "targeted pass."
)

# Default thresholds. ``notice_after_rounds`` is deliberately low: the notice is
# cheap (one appended paragraph) and the failure mode is expensive.
DEFAULT_PROBE_NOTICE_AFTER_ROUNDS = 4
DEFAULT_PROBE_HALT_AFTER_ROUNDS = 15

# "Final batch" announcement detector.
#
# The observed failure shape (captured from a real looping session):
#   "All seams mapped. Three last facts to write an exact brief — then I cut policy + code in parallel:"
#   "All seams mapped. Final four facts, then the patch lands:"
#   "Ahoy — aye, executing NOW. Last exact-text batch (one round), then every edit lands:"
#   "Anchors in hand. Executing — final probe batch (one round), then every file lands:"
#   "Executing now. Anchors first (one batch), then the patch lands — no more rounds."
#
# Shape: a finality / limited-quantity marker, then a gathering noun, then
# "then" (the promise to act). The promise is what makes it a loop signature:
# the model commits to stopping and then keeps going.
_FINAL_MARKER = (
    r"(?:last|final|one\s+(?:more|last|batch|round|pass|go|sweep|read|look)"
    r"|exact(?:\s*[-_]?\s*(?:text|batch|read))?|remaining"
    r"|anchors?\s+(?:first|in\s+hand)"
    r"|no\s+more\s+(?:rounds|batches|reads|passes))"
)
_GATHERING_NOUN = (
    r"(?:facts?|batches|batch|rounds?|probes?|reads?|readings?|passes|pass|anchors?"
    r"|checks?|sweeps?|seams?|text|lookups?|details?)"
)
_FINAL_BATCH_RE = re.compile(
    rf"\b{_FINAL_MARKER}\b[^.!?\n]{{0,90}}?\b{_GATHERING_NOUN}\b[^.!?\n]{{0,90}}?\bthen\b",
    re.IGNORECASE,
)


def announces_final_batch(text: str | None) -> bool:
    """Whether ``text`` promises a final gathering round ("…last batch, then I write").

    Narrow on purpose: it only fires when the announcement carries BOTH a
    finality marker and a "then <act>" promise, so ordinary prose ("the final
    answer is 42") does not match.
    """
    t = (text or "").strip()
    if not t:
        return False
    return bool(_FINAL_BATCH_RE.search(t))


def round_produced_deliverable(tool_names: Iterable[str]) -> bool:
    """Whether a tool round produced something the user can actually see."""
    return any(name in DELIVERABLE_TOOL_NAMES for name in (tool_names or ()))


def resolve_probe_budget(raw: Any) -> tuple[bool, int, int]:
    """Resolve ``agent.probe_budget`` into ``(enabled, notice_after, halt_after)``.

    Accepts a mapping (``enabled`` / ``notice_after_rounds`` / ``halt_after_rounds``)
    or ``False``/``None`` to disable. A halt threshold of 0 disables the halt but
    keeps the notice.
    """
    if raw is None or raw is False:
        return (False, DEFAULT_PROBE_NOTICE_AFTER_ROUNDS, DEFAULT_PROBE_HALT_AFTER_ROUNDS)
    if raw is True:
        return (True, DEFAULT_PROBE_NOTICE_AFTER_ROUNDS, DEFAULT_PROBE_HALT_AFTER_ROUNDS)
    if not isinstance(raw, Mapping):
        return (True, DEFAULT_PROBE_NOTICE_AFTER_ROUNDS, DEFAULT_PROBE_HALT_AFTER_ROUNDS)

    enabled = raw.get("enabled", True)
    enabled = True if enabled is None else bool(enabled)

    def _int(name: str, default: int, minimum: int) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return n if n >= minimum else default

    notice_after = _int("notice_after_rounds", DEFAULT_PROBE_NOTICE_AFTER_ROUNDS, 1)
    halt_after = _int("halt_after_rounds", DEFAULT_PROBE_HALT_AFTER_ROUNDS, 0)
    return (enabled, notice_after, halt_after)


def append_probe_notice(messages: Any, notice: str) -> bool:
    """Append ``notice`` to the newest tool result (cache-safe).

    Mirrors the iteration-budget warning contract: only the CURRENT turn's
    tool-result tail is mutable; an older turn may already be cached, so a
    persisted row is left alone.
    """
    from agent.context_compressor import _DB_PERSISTED_MARKER

    if not messages:
        return False
    msg = messages[-1]
    if not isinstance(msg, dict) or msg.get("role") != "tool" or msg.get(_DB_PERSISTED_MARKER):
        return False
    content = msg.get("content", "")
    if isinstance(content, str):
        msg["content"] = content + f"\n\n{notice}"
        return True
    if isinstance(content, list) or content is None:
        msg["content"] = [*(content or []), {"type": "text", "text": notice}]
        return True
    return False


__all__ = [
    "DEFAULT_PROBE_HALT_AFTER_ROUNDS",
    "DEFAULT_PROBE_NOTICE_AFTER_ROUNDS",
    "DELIVERABLE_TOOL_NAMES",
    "PROBE_BUDGET_HALT_RESPONSE",
    "PROBE_BUDGET_NOTICE",
    "PROBE_BUDGET_NOTICE_ESCALATED",
    "announces_final_batch",
    "append_probe_notice",
    "resolve_probe_budget",
    "round_produced_deliverable",
]
