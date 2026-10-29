"""Regressions for domain-layer descendant invalidation on ancestor reopen.

``hermes_cli.kanban_db_reopen.invalidate_descendants_for_parent_reopen`` is the single
implementation of "a done ancestor was reopened, retract everything that
assumed its result" (M3). These tests pin:

* a ``done`` descendant is FROZEN, not demoted: it keeps its status, its
  result and its completion stamp, it is never re-queued, it records a
  ``descendant_frozen`` event naming the ancestor, and the reopened ancestor
  gets a comment naming the frozen ids (non-silent),
* unfinished (``ready``/``review``/``running``) descendants are still demoted to
  ``todo`` with a ``descendant_invalidated`` event AND a comment naming the
  ancestor,
* running descendants have their audit trail committed BEFORE their worker
  is terminated, and the kill routes through ``_terminate_reclaimed_worker``
  (the same helper the reclaim paths use),
* ``consecutive_failures`` resets to 0 on the retracted descendants
  (deliberate operator action — opposite of the review-loop rule pinned in
  M2), and
* the dashboard ``_set_status_direct`` reopen path and the DB function
  produce identical descendant outcomes.

WHY THE OLD EXPECTATIONS WERE REPLACED: the cascade used to demote a finished
descendant back to ``todo``. That re-queued work that had already shipped and
asked the worker to redo it. Finished work is now frozen instead — it stays
``done``, keeps its result and its completion stamp, and never becomes
dispatchable again. The demotion assertions therefore moved onto the
unfinished descendants, and the finished ones are pinned by the freeze
contract: the demotion of a ``done`` descendant is no longer a valid
expectation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_reopen as kbr


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _done_parent_with_done_child(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    child_id = kb.create_task(
        conn, title="child", assignee="builder", parents=[parent_id],
    )
    assert kb.complete_task(conn, child_id, result="child shipped")
    return parent_id, child_id


def _reopen_parent_directly(conn, parent_id: str) -> None:
    """Minimal stand-in for a reopen surface: flip done -> todo."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
            (parent_id,),
        )


def test_reopen_freezes_done_descendants_with_events_and_comments(conn):
    """A finished descendant is frozen: kept as-is, never re-queued."""
    parent_id, child_id = _done_parent_with_done_child(conn)
    grandchild_id = kb.create_task(
        conn, title="grandchild", assignee="writer", parents=[child_id],
    )
    assert kb.complete_task(conn, grandchild_id, result="grandchild shipped")

    _reopen_parent_directly(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    # Nothing is demoted: finished work stays finished.
    assert result["invalidated"] == []
    frozen = {entry["id"]: entry for entry in result["frozen"]}
    assert set(frozen) == {child_id, grandchild_id}
    assert frozen[child_id]["prior_status"] == "done"
    assert frozen[child_id]["new_status"] == "done"
    assert result["terminations"] == []

    for tid, expected_result in (
        (child_id, "child shipped"),
        (grandchild_id, "grandchild shipped"),
    ):
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done", "a finished descendant stays done"
        assert task.result == expected_result, "a frozen descendant keeps its result"
        assert task.completed_at is not None, "a frozen descendant keeps its stamp"

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "descendant_invalidated" not in kinds
        freeze = [e for e in events if e.kind == "descendant_frozen"]
        assert len(freeze) == 1, f"expected one freeze event on {tid}, got {kinds}"
        payload = freeze[0].payload
        assert payload["ancestor"] == parent_id
        assert payload["descendant"] == tid
        assert payload["prior_status"] == "done"
        assert payload["new_status"] == "done"

    # The reopened ancestor carries the list of finished work that may now be
    # out of date — the freeze is never silent.
    listing = [
        c for c in kb.list_comments(conn, parent_id)
        if child_id in c.body and grandchild_id in c.body
    ]
    assert listing, (
        f"no comment on the reopened ancestor naming the frozen descendants "
        f"{child_id}, {grandchild_id}"
    )
    assert listing[0].author == "operator"


def test_unfinished_descendants_are_still_demoted_to_todo(conn):
    """``ready`` and ``review`` descendants keep the retraction behaviour."""
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    ready_id = kb.create_task(
        conn, title="ready child", assignee="builder", parents=[parent_id],
    )
    review_id = kb.create_task(
        conn, title="review child", assignee="builder", parents=[parent_id],
    )
    assert kb.claim_task(conn, review_id) is not None
    assert kb.request_review(conn, review_id, summary="please look") is True
    assert kb.get_task(conn, review_id).status == "review"

    _reopen_parent_directly(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    assert {entry["id"] for entry in result["invalidated"]} == {ready_id, review_id}
    assert result["frozen"] == []
    for tid in (ready_id, review_id):
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "todo", "an unfinished descendant is retracted to todo"
        assert task.completed_at is None
        events = kb.list_events(conn, tid)
        inval = [e for e in events if e.kind == "descendant_invalidated"]
        assert len(inval) == 1
        assert inval[0].payload["ancestor"] == parent_id
        assert inval[0].payload["new_status"] == "todo"
        comments = kb.list_comments(conn, tid)
        assert any(
            parent_id in c.body and c.author == "operator" for c in comments
        ), f"no invalidation comment naming {parent_id} on {tid}"


def test_running_descendant_event_precedes_termination_via_reclaim_helper(
    conn, tmp_path, monkeypatch,
):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    child_id = kb.create_task(
        conn, title="running child", assignee="builder", parents=[parent_id],
    )
    claimed = kb.claim_task(conn, child_id)
    assert claimed is not None and claimed.status == "running"
    kbd._set_worker_pid(conn, child_id, 424242)

    kills: list[tuple] = []

    def fake_terminate(pid, claim_lock, started_at=None, **kwargs):
        # The audit trail must already be durable when the kill fires:
        # standalone calls commit before terminating.
        side = kbc.connect(tmp_path / "kanban.db")
        try:
            kinds = [e.kind for e in kb.list_events(side, child_id)]
        finally:
            side.close()
        assert "descendant_invalidated" in kinds
        kills.append((pid, claim_lock, started_at))
        return {"terminated": True}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_terminate)

    _reopen_parent_directly(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    assert kills and kills[0][0] == 424242
    assert result["terminations"] == kills
    child = kb.get_task(conn, child_id)
    assert child is not None
    assert child.status == "todo"
    assert child.current_run_id is None
    run = kb.latest_run(conn, child_id)
    assert run is not None and run.outcome == "reclaimed"


def test_counter_reset_on_invalidated_descendants(conn):
    """The breaker resets on the RETRACTED descendants (the ones re-queued)."""
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id, result="parent shipped")
    ready_id = kb.create_task(
        conn, title="ready child", assignee="builder", parents=[parent_id],
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 4 WHERE id = ?",
            (ready_id,),
        )

    _reopen_parent_directly(conn, parent_id)
    result = kbr.invalidate_descendants_for_parent_reopen(conn, parent_id, author="op")

    assert [entry["id"] for entry in result["invalidated"]] == [ready_id]
    child = kb.get_task(conn, ready_id)
    assert child is not None
    assert child.status == "todo"
    # Deliberate operator action = fresh start with the breaker; contrast
    # with reopen_review_task, which PRESERVES the counter (M2 rule).
    assert child.consecutive_failures == 0


def test_dashboard_and_db_paths_produce_identical_outcomes(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import importlib.util
    import sys

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_m3_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    def build_graph(tag: str):
        with kbc.connect() as c:
            parent = kb.create_task(c, title=f"{tag}-parent", assignee="planner")
            assert kb.complete_task(c, parent, result="parent shipped")
            child = kb.create_task(
                c, title=f"{tag}-child", assignee="builder", parents=[parent],
            )
            assert kb.complete_task(c, child, result="child shipped")
            ready = kb.create_task(
                c, title=f"{tag}-ready", assignee="builder", parents=[parent],
            )
            assert kb.get_task(c, ready).status == "ready"
        return parent, child, ready

    dash_parent, dash_child, dash_ready = build_graph("dash")
    db_parent, db_child, db_ready = build_graph("db")

    # Surface 1: dashboard drag (done -> todo) via _set_status_direct.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{dash_parent}", json={"status": "todo"},
    )
    assert r.status_code == 200, r.text

    # Surface 2: DB function directly (the single domain implementation).
    with kbc.connect() as c:
        with kb.write_txn(c):
            c.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL "
                "WHERE id = ?",
                (db_parent,),
            )
        kbr.invalidate_descendants_for_parent_reopen(
            c, db_parent, author="dashboard",
        )

    with kbc.connect() as c:
        def snapshot(tid: str):
            t = kb.get_task(c, tid)
            assert t is not None
            kinds = sorted(e.kind for e in kb.list_events(c, tid))
            n_comments = len(kb.list_comments(c, tid))
            return (
                t.status,
                t.completed_at,
                t.current_run_id,
                t.consecutive_failures,
                kinds,
                n_comments,
            )

        # The finished descendant: frozen by both surfaces, identically.
        assert snapshot(dash_child) == snapshot(db_child)
        status, completed_at, _run, failures, kinds, n_comments = snapshot(db_child)
        assert status == "done", "a finished descendant is frozen, not demoted"
        assert completed_at is not None
        assert failures == 0
        assert "descendant_frozen" in kinds
        assert "descendant_invalidated" not in kinds

        # The unfinished descendant: retracted by both surfaces, identically.
        assert snapshot(dash_ready) == snapshot(db_ready)
        status, completed_at, _run, failures, kinds, n_comments = snapshot(db_ready)
        assert status == "todo"
        assert completed_at is None
        assert failures == 0
        assert "descendant_invalidated" in kinds
        assert n_comments >= 1
