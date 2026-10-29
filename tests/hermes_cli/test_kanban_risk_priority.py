"""RISK class decides priority: a card body that says ``RISK: high`` outranks
one that says ``RISK: low``, on every creation path.

Two paths create cards: ``create_task`` (CLI, dashboard, tools) and the
decomposed-child INSERT in ``kanban_db_graph`` (which bypasses
``create_task`` entirely). Both must honour the RISK line, and the
dispatcher must schedule the higher-priority card first.
"""

from __future__ import annotations

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


def _priority_of(conn, task_id: str) -> int:
    row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None, f"task {task_id} missing"
    return int(row["priority"])


# ── the mapping itself ──────────────────────────────────────────────────────

def test_risk_line_maps_to_priority():
    assert kb.priority_from_risk_line("RISK: high") == 90
    assert kb.priority_from_risk_line("RISK: normal") == 50
    assert kb.priority_from_risk_line("RISK: low") == 10


def test_risk_line_parser_tolerates_whitespace_and_case():
    assert kb.priority_from_risk_line("   risk:   HIGH  ") == 90
    assert kb.priority_from_risk_line("\tRISK:\tnormal\t") == 50


def test_risk_line_parser_ignores_a_mangled_value():
    assert kb.priority_from_risk_line("**RISK: Low**") is None


def test_risk_line_parser_takes_the_first_matching_line():
    body = "RISK: low\nsome prose\nRISK: high\n"
    assert kb.priority_from_risk_line(body) == 10


def test_risk_line_parser_ignores_unknown_values_and_non_risk_lines():
    assert kb.priority_from_risk_line("RISK: catastrophic") is None
    assert kb.priority_from_risk_line("no risk here") is None
    assert kb.priority_from_risk_line("") is None
    assert kb.priority_from_risk_line(None) is None


def test_risk_priority_constant_is_the_single_source():
    assert kb.RISK_PRIORITY == {"high": 90, "normal": 50, "low": 10}


# ── create_task path ────────────────────────────────────────────────────────

def test_create_task_uses_the_risk_line_priority(conn):
    tid = kb.create_task(conn, title="wire the refund path", body="Goal\nRISK: high\n")
    assert _priority_of(conn, tid) == 90


def test_create_task_without_a_risk_line_defaults_to_normal(conn):
    tid = kb.create_task(conn, title="no risk line", body="Goal\njust prose\n")
    assert _priority_of(conn, tid) == 50


def test_create_task_without_a_risk_line_keeps_an_explicit_priority(conn):
    tid = kb.create_task(conn, title="explicit priority", body="Goal\n", priority=7)
    assert _priority_of(conn, tid) == 7


def test_a_risk_line_overrides_an_explicit_priority(conn):
    tid = kb.create_task(conn, title="risk wins", body="Goal\nRISK: low\n", priority=90)
    assert _priority_of(conn, tid) == 10


# ── decomposed-child path (raw INSERT, no create_task) ──────────────────────

def test_decomposed_child_gets_the_risk_line_priority(conn):
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
    assert _priority_of(conn, child_ids[0]) == 90
    assert _priority_of(conn, child_ids[1]) == 50


# ── the dispatcher honours the priority ─────────────────────────────────────

def test_dispatcher_spawns_the_higher_priority_ready_task_first(conn, all_assignees_spawnable):
    low = kb.create_task(conn, title="low risk card", body="RISK: low\n", assignee="alice")
    high = kb.create_task(conn, title="high risk card", body="RISK: high\n", assignee="alice")
    for tid in (low, high):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()

    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, max_spawn=1)
    spawned = [row[0] for row in result.spawned]
    assert spawned == [high], f"expected the high-priority card first, spawned {spawned}"
