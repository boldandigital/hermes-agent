"""Risk lanes: a card's risk class is durable, and dispatch honours per-lane capacity.

One signal: the ``RISK:`` line a card body already carries (the same line the
specify/decompose paths emit and ``kanban_db.priority_from_risk_line`` reads).
The class is stored ON THE TASK ROW, so the dispatcher never re-parses the body
on a tick, and ``kanban.lanes`` can reserve capacity per class.

``kanban.max_in_progress`` stays the hard ceiling; lanes only subdivide it.
With ``kanban.lanes`` unset the dispatcher behaves exactly as it did before.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kbc.connect()
    try:
        yield c
    finally:
        c.close()


def _lane_of(conn, task_id: str):
    row = conn.execute("SELECT risk_lane FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None, f"task {task_id} missing"
    return row["risk_lane"]


# ── the classification is stored on the row ─────────────────────────────────

def test_create_task_stores_the_risk_lane(conn):
    tid = kb.create_task(conn, title="risky card", body="Goal\nRISK: high\n")
    assert _lane_of(conn, tid) == "high"


def test_create_task_stores_the_low_risk_lane(conn):
    tid = kb.create_task(conn, title="cosmetic card", body="Goal\nRISK: low\n")
    assert _lane_of(conn, tid) == "low"


def test_create_task_without_a_risk_line_stores_the_normal_lane(conn):
    tid = kb.create_task(conn, title="plain card", body="Goal\njust prose\n")
    assert _lane_of(conn, tid) == "normal"


def test_decomposed_child_stores_the_risk_lane(conn):
    root = kb.create_task(conn, title="triage root", triage=True)
    child_ids = decompose_triage_task(
        conn, root,
        root_assignee="alice",
        children=[
            {"title": "risky child", "body": "Goal\nRISK: high\n"},
            {"title": "plain child", "body": "Goal\nno risk line\n"},
        ],
    )
    assert child_ids is not None
    assert _lane_of(conn, child_ids[0]) == "high"
    assert _lane_of(conn, child_ids[1]) == "normal"


def test_the_stored_lane_survives_an_edit_of_the_body(conn):
    """The lane is read from the row, not re-parsed from the body per tick."""
    tid = kb.create_task(conn, title="risky card", body="Goal\nRISK: high\n")
    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Goal\nno risk line\n", tid))
    conn.commit()
    assert _lane_of(conn, tid) == "high"


def test_a_board_created_before_the_lane_column_is_migrated(tmp_path, monkeypatch):
    """A legacy board gains the lane column instead of failing every query."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = tmp_path / "kanban.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, "
        "assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0, "
        "created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER, "
        "completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch', "
        "workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER)"
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_legacy', 'old card', 'todo', 1)"
    )
    legacy.commit()
    legacy.close()

    conn = kbc.connect(db_path)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "risk_lane" in cols, "legacy board was not migrated"
        row = conn.execute("SELECT risk_lane FROM tasks WHERE id = 't_legacy'").fetchone()
        assert row["risk_lane"] is None, "legacy rows keep no lane; they read as normal"
    finally:
        conn.close()


# ── the parser stays the single source ─────────────────────────────────────

def test_the_risk_line_parser_reports_the_class():
    assert kb.risk_from_risk_line("RISK: high") == "high"
    assert kb.risk_from_risk_line("   risk:   LOW  ") == "low"
    assert kb.risk_from_risk_line("**RISK: Low**") is None
    assert kb.risk_from_risk_line(None) is None


def test_the_lane_vocabulary_is_the_risk_vocabulary():
    assert kb.RISK_LANES == tuple(kb.RISK_PRIORITY)
