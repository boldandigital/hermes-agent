"""Risk lanes: how many cards of each risk class may run at the same time.

One signal, one mapping point. The lane names are the keys of
``kanban_db.RISK_PRIORITY`` and the classification comes from
``kanban_db.resolve_task_risk_lane`` — the ``RISK:`` line the specify and
decompose paths already emit and that ``priority_from_risk_line`` already
reads. Nothing here re-parses a card body: the lane is stored on the task row,
so a dispatch tick never has to re-derive it.

``kanban.lanes`` maps a lane name to its capacity. ``kanban.max_in_progress``
stays the hard ceiling — lanes only subdivide it, they never exceed it. With
no ``kanban.lanes`` configured the dispatcher keeps its single shared pool,
exactly as before.

Only the lanes an operator names are capped; a lane that is not named is
unbounded (the ceiling still applies). An unnamed lane must not read as
"capacity 0" — that would silently wedge the board.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any
from typing import Optional


def lane_names() -> tuple[str, ...]:
    """The lane vocabulary — the risk classes, one source of truth."""
    from hermes_cli import kanban_db as _kb
    return _kb.RISK_LANES


def normalize_lane(value: Any) -> str:
    """The lane a stored value belongs to.

    Unknown values and rows written before the lane column existed read as
    ``normal``: they are ordinary work, not urgent and not cosmetic.
    """
    from hermes_cli import kanban_db as _kb
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _kb.RISK_PRIORITY:
            return candidate
    return _kb.DEFAULT_RISK_LANE


def parse_lane_capacities(raw: Any) -> tuple[Optional[dict[str, int]], Optional[str]]:
    """Validate the ``kanban.lanes`` config value.

    Returns ``(capacities, None)`` when the value is usable and
    ``(None, error)`` when it is not; ``raw`` of ``None`` means "not
    configured" and returns ``(None, None)`` so the dispatcher keeps its
    single shared pool.

    The check is all-or-nothing: one bad entry disables the whole mapping, so
    a typo can never half-apply a lane plan and silently drop or double-count
    work. A malformed value is the operator's problem to see, not the board's
    problem to wedge on.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, Mapping):
        return None, (
            "kanban.lanes must be a mapping of lane name to capacity, got "
            f"{type(raw).__name__}"
        )
    if not raw:
        return None, "kanban.lanes is empty — no lane capacity declared"
    known = lane_names()
    capacities: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            return None, f"kanban.lanes key {name!r} is not a lane name"
        lane = name.strip().lower()
        if lane not in known:
            return None, (
                f"kanban.lanes names an unknown lane {name!r} "
                f"(expected one of {', '.join(known)})"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            return None, (
                f"kanban.lanes[{lane!r}] must be an integer capacity, got {value!r}"
            )
        if value < 1:
            return None, f"kanban.lanes[{lane!r}] must be at least 1, got {value}"
        capacities[lane] = value
    return capacities, None


def running_counts_by_lane(conn: sqlite3.Connection) -> dict[str, int]:
    """How many cards of each lane are running right now."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT risk_lane, COUNT(*) AS n FROM tasks "
        "WHERE status = 'running' GROUP BY risk_lane"
    ):
        lane = normalize_lane(row["risk_lane"])
        counts[lane] = counts.get(lane, 0) + int(row["n"])
    return counts
