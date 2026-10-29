"""Reopening an ancestor FREEZES its finished (``done``) descendants.

``kanban_db.invalidate_descendants_for_parent_reopen`` used to demote every
``done`` descendant back to ``todo``, which re-queued work that had already
shipped and asked the worker to redo it. Under the new contract a ``done``
descendant is FROZEN:

* it keeps its ``done`` status, its ``result`` and its completion stamp,
* it is never re-queued and never becomes dispatchable again,
* the freeze is recorded as an event on the descendant, and
* the reopened ancestor gets a comment naming the frozen descendants, so the
  human who reopened it can see which finished work may now be out of date.

``ready``/``review``/``running`` descendants keep the old behaviour — those
tests guard the change against a regression in the cascade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_reopen as kbr


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _reopen_ancestor(conn, task_id: str) -> None:
    """Minimal stand-in for a reopen surface: flip done -> todo."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
            (task_id,),
        )


def test_done_descendant_is_frozen_and_keeps_its_result(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    child_id = kb.create_task(
        conn, title="child", assignee="builder", parents=[parent_id],
    )
    assert kb.complete_task(conn, child_id, result="child shipped")
    grandchild_id = kb.create_task(
        conn, title="grandchild", assignee="writer", parents=[child_id],
    )
    assert kb.complete_task(conn, grandchild_id, result="grandchild shipped")

    _reopen_ancestor(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    assert {entry["id"] for entry in result["frozen"]} == {child_id, grandchild_id}
    assert result["invalidated"] == []
    assert result["terminations"] == []

    for tid, expected in ((child_id, "child shipped"), (grandchild_id, "grandchild shipped")):
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done", "a finished descendant must stay done"
        assert task.result == expected, "a frozen descendant keeps its result"
        assert task.completed_at is not None, "a frozen descendant keeps its completion stamp"

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "descendant_invalidated" not in kinds, "a frozen descendant is not invalidated"
        frozen = [e for e in kb.list_events(conn, tid) if e.kind == "descendant_frozen"]
        assert len(frozen) == 1, f"expected one freeze event on {tid}, got {kinds}"
        payload = frozen[0].payload
        assert payload["ancestor"] == parent_id
        assert payload["descendant"] == tid
        assert payload["reason"] == "frozen_by_ancestor_reopen"


def test_reopened_ancestor_gets_comment_listing_frozen_descendants(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    child_id = kb.create_task(
        conn, title="child", assignee="builder", parents=[parent_id],
    )
    assert kb.complete_task(conn, child_id, result="child shipped")
    grandchild_id = kb.create_task(
        conn, title="grandchild", assignee="writer", parents=[child_id],
    )
    assert kb.complete_task(conn, grandchild_id, result="grandchild shipped")

    _reopen_ancestor(conn, parent_id)
    kbr.invalidate_descendants_for_parent_reopen(conn, parent_id, author="operator")

    comments = kb.list_comments(conn, parent_id)
    listing = [
        c for c in comments
        if child_id in c.body and grandchild_id in c.body
    ]
    assert listing, (
        "no comment on the reopened ancestor listing the frozen descendant ids"
    )
    assert listing[0].author == "operator"


def test_ready_descendant_is_still_requeued(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    child_id = kb.create_task(
        conn, title="ready child", assignee="builder", parents=[parent_id],
    )
    ready = kb.get_task(conn, child_id)
    assert ready is not None and ready.status == "ready"

    _reopen_ancestor(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    child = kb.get_task(conn, child_id)
    assert child is not None
    assert child.status == "todo"
    assert child.completed_at is None
    assert [entry["id"] for entry in result["invalidated"]] == [child_id]
    assert result["frozen"] == []
    kinds = [e.kind for e in kb.list_events(conn, child_id)]
    assert "descendant_invalidated" in kinds


def test_running_descendant_is_still_reclaimed(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    child_id = kb.create_task(
        conn, title="running child", assignee="builder", parents=[parent_id],
    )
    claimed = kb.claim_task(conn, child_id)
    assert claimed is not None and claimed.status == "running"

    _reopen_ancestor(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    child = kb.get_task(conn, child_id)
    assert child is not None
    assert child.status == "todo"
    assert [entry["id"] for entry in result["invalidated"]] == [child_id]
    assert result["frozen"] == []
    run = kb.latest_run(conn, child_id)
    assert run is not None and run.outcome == "reclaimed"
