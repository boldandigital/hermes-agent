"""The FENCE verdict record: a review verdict in one fixed, parseable shape.

A card that is sent back for changes carries the reviewer's verdict as a
COMMENT whose body starts with ``FENCE:`` and holds one field per line::

    FENCE:
    EXPECTED: <what a passing result looks like>
    SEEN: <what actually happened>
    VERDICT: pass|fail

``SEEN:`` is ONE line — the failing command and its error — so two failures of
the same class compare across cards. The next attempt starts from the record:
the worker context renders it as an explicit section, so the attempt knows
what a passing result looked like instead of re-reading the whole card.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` so monkeypatching ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

FENCE_PREFIX = "FENCE:"
EXPECTED_LABEL = "EXPECTED:"
SEEN_LABEL = "SEEN:"
VERDICT_LABEL = "VERDICT:"
NOT_STATED = "(not stated by reviewer)"
_FIELDS = ("expected", "seen", "verdict")
_LABELS = {"expected": EXPECTED_LABEL, "seen": SEEN_LABEL, "verdict": VERDICT_LABEL}


def _one_line(value: Any) -> str:
    """Flatten to ONE line: the record compares failures across cards."""
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in text.split("\n")]
    return " ".join(part for part in parts if part)


def parse_verdict(text: Optional[str]) -> Optional[dict]:
    """The ``EXPECTED``/``SEEN``/``VERDICT`` fields of a verdict block.

    The three labels are matched case-insensitively, one field per line. A
    block that is absent, incomplete or carries an empty field is malformed
    and parses to None — half a record must never pass for a verdict.
    """
    if not text:
        return None
    found: dict[str, str] = {}
    for raw in str(text).split("\n"):
        line = raw.strip()
        for field, label in _LABELS.items():
            if field in found:
                continue
            if line.lower().startswith(label.lower()):
                value = line[len(label):].strip()
                if value:
                    found[field] = value
                break
    if any(field not in found for field in _FIELDS):
        return None
    return {field: found[field] for field in _FIELDS}


def build_fence_body(expected: str, seen: str, verdict: str) -> str:
    """The stored record: the prefix, then one field per line."""
    return "\n".join((
        FENCE_PREFIX,
        f"{EXPECTED_LABEL} {_one_line(expected)}",
        f"{SEEN_LABEL} {_one_line(seen)}",
        f"{VERDICT_LABEL} {_one_line(verdict)}",
    ))


def latest_fence(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The newest FENCE comment on the card, parsed.

    Returns ``{"expected", "seen", "verdict", "body"}`` or None when the card
    carries no FENCE record.
    """
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (task_id,),
    ).fetchall()
    for row in rows:
        body = row["body"] or ""
        if not body.strip().upper().startswith(FENCE_PREFIX):
            continue
        parsed = parse_verdict(body)
        if parsed is None:
            return None
        return {**parsed, "body": body}
    return None


def latest_verdict(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The verdict the next attempt starts from: the newest FENCE record, or —
    when that comment is gone — the newest ``changes_requested`` event payload.
    """
    fence = latest_fence(conn, task_id)
    if fence is not None:
        return fence
    event = _kb._latest_event(conn, task_id, "changes_requested")
    payload = _kb._json_dict(_kb._row_get(event, "payload"))
    block = payload.get("verdict")
    if not isinstance(block, dict):
        return None
    fields = {field: str(block.get(field) or "").strip() for field in _FIELDS}
    if not all(fields.values()):
        return None
    return fields


def record_fence(
    conn: sqlite3.Connection, task_id: str, *, reviewer: str, reason: str,
    verdict: Optional[str] = None,
) -> dict[str, str]:
    """Store the FENCE record for a card that is being sent back for changes.

    The reviewer's own verdict wins — the explicit ``verdict`` argument, else
    the three lines inside the reason. Without either the record is
    synthesized honestly: the expectation is marked as unstated, the reason
    becomes the one-line failure and the verdict is ``fail``.

    The record holds reviewer text, so it goes through the same redaction as
    the reason. Returns the stored verdict ``{"expected", "seen", "verdict"}``.
    """
    block = parse_verdict(verdict) or parse_verdict(reason)
    if block is None:
        block = {"expected": NOT_STATED, "seen": _one_line(reason), "verdict": "fail"}
    redacted = {
        field: str(_kb.redact_review_value(value) or "") for field, value in block.items()
    }
    body = build_fence_body(redacted["expected"], redacted["seen"], redacted["verdict"])
    _kb._insert_comment(conn, task_id, reviewer, body, int(time.time()))
    return redacted


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
