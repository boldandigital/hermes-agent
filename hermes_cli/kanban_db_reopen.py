"""The ancestor-reopen cascade: what a reopened done task retracts.

:func:`invalidate_descendants_for_parent_reopen` is the single implementation
of "a done ancestor was reopened, retract everything that assumed its result"
(M3). Every surface that reopens a done task (dashboard PATCH/drag) routes
here.

A descendant that is itself ``done`` is FROZEN, not retracted: finished work
is not re-queued. It keeps its status, its result and its completion stamp, it
never becomes dispatchable again, and the freeze is recorded so the human who
reopened the ancestor can see which finished work may now be out of date.
``ready``/``review``/``running`` descendants keep the retraction behaviour.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional


def invalidate_descendants_for_parent_reopen(
    conn: sqlite3.Connection, task_id: str, *, author: str,
) -> dict[str, Any]:
    """THE done-reopen invalidation: every ``ready``/``review``/``running``
    descendant of a reopened ancestor is demoted to ``todo`` and re-gated; every
    ``done`` descendant is FROZEN (finished work is never re-queued). Every
    surface that reopens a done task (dashboard PATCH/drag) routes here.

    Composes under the caller's txn (``allow_nested=True``) so the flip and the
    retractions commit atomically. Each retracted descendant gets a
    ``descendant_invalidated`` event, the legacy ``status`` event the live feed
    renders, and a comment naming the ancestor; each frozen descendant gets a
    ``descendant_frozen`` event and the reopened ancestor gets one comment
    listing the frozen ids. Running descendants are closed ``reclaimed`` and
    their workers killed strictly post-commit (audit trail before death) — when
    composed, the CALLER must drain ``terminations`` after its own commit.
    ``consecutive_failures`` resets (deliberate operator action), the opposite
    of :func:`hermes_cli.kanban_db.reopen_review_task`.

    Returns ``{"invalidated": [{id, prior_status, new_status, resume_status}],
    "frozen": [{id, prior_status, new_status}], "terminations":
    [(worker_pid, claim_lock, worker_started_at)]}``.
    """
    from hermes_cli.kanban_db import (
        _append_event,
        _end_run,
        _insert_comment,
        _retry_status_for_run,
        _terminate_reclaimed_worker,
    )
    from hermes_cli.kanban_db_connect import write_txn

    caller_owns_txn = bool(conn.in_transaction)
    now = int(time.time())
    invalidated: list[dict[str, Any]] = []
    frozen: list[dict[str, Any]] = []
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with write_txn(conn, allow_nested=True):
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_id FROM task_links WHERE parent_id = ?
                UNION
                SELECT l.child_id
                FROM task_links l
                JOIN descendants d ON d.id = l.parent_id
            )
            SELECT t.id, t.status, t.current_run_id, t.worker_pid, t.claim_lock, t.worker_started_at
            FROM descendants d
            JOIN tasks t ON t.id = d.id
            ORDER BY t.id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            previous_status = row["status"]
            if previous_status not in {"ready", "review", "running", "done"}:
                continue
            if previous_status == "done":
                # Finished work is frozen, not retracted: the row keeps its
                # status, its result and its completion stamp, so the
                # dispatcher can never pick it up again.
                frozen.append(
                    {"id": row["id"], "prior_status": "done", "new_status": "done"},
                )
                _append_event(
                    conn, row["id"], "descendant_frozen",
                    {
                        "ancestor": task_id, "descendant": row["id"],
                        "prior_status": "done", "new_status": "done",
                        "reason": "frozen_by_ancestor_reopen",
                    },
                )
                continue
            resume_status = "ready"
            run_id = None
            if previous_status == "review":
                resume_status = "review"
            elif previous_status == "running":
                resume_status = _retry_status_for_run(conn, row["id"], row["current_run_id"])
                terminations.append((row["worker_pid"], row["claim_lock"], row["worker_started_at"]))
                run_id = _end_run(
                    conn, row["id"], outcome="reclaimed", status="todo",
                    summary=f"ancestor {task_id} reopened",
                )
            # consecutive_failures = 0: deliberate operator reset — see
            # docstring for why this diverges from reopen_review_task.
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "current_run_id = NULL, consecutive_failures = 0 WHERE id = ?", (row["id"],),
            )
            entry = {
                "id": row["id"], "prior_status": previous_status,
                "new_status": "todo", "resume_status": resume_status,
            }
            _append_event(
                conn, row["id"], "descendant_invalidated",
                {"ancestor": task_id, **{k: v for k, v in entry.items() if k != "id"}},
                run_id=run_id,
            )
            # Legacy 'status' event so existing live-feed consumers still see
            # the move without learning the new event kind.
            _append_event(
                conn, row["id"], "status",
                {
                    "status": "todo", "reason": "ancestor_reopened", "parent": task_id,
                    "previous_status": previous_status, "resume_status": resume_status,
                },
                run_id=run_id,
            )
            _insert_comment(
                conn, row["id"], author, f"Invalidated: ancestor {task_id} was reopened; "
                f"retracted from '{previous_status}' to 'todo' "
                f"(will resume via '{resume_status}').", now,
            )
            invalidated.append(entry)
        if frozen:
            # The ancestor carries the list: the worker and the human who
            # reopened it must see which finished work may now be out of date.
            _insert_comment(
                conn, task_id, author,
                "Frozen: ancestor reopen left finished descendant(s) done — their "
                "results may now be out of date: "
                + ", ".join(entry["id"] for entry in frozen), now,
            )
    if not caller_owns_txn:
        # Standalone: committed above, audit trail durable, safe to kill now.
        # Composed calls leave this to the caller post-commit.
        for pid, claim_lock, started_at in terminations:
            _terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    return {"invalidated": invalidated, "frozen": frozen, "terminations": terminations}
