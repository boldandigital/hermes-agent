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
import time
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


# ── the lane capacity configuration ─────────────────────────────────────────

def _lanes():
    from hermes_cli import kanban_db_lanes as kbl
    return kbl


def test_a_valid_lane_mapping_is_accepted():
    caps, error = _lanes().parse_lane_capacities({"high": 1, "normal": 2, "low": 3})
    assert error is None
    assert caps == {"high": 1, "normal": 2, "low": 3}


def test_no_lane_configuration_means_no_lanes():
    assert _lanes().parse_lane_capacities(None) == (None, None)


@pytest.mark.parametrize("raw", [
    [1, 2],            # wrong type
    "high",            # wrong type
    {"high": -1},      # negative capacity
    {"urgent": 1},     # unknown lane name
    {"high": 0},       # zero capacity
    {"high": "1"},     # capacity is not a number
    {},                # no lane at all
], ids=["list", "string", "negative", "unknown-lane", "zero", "non-number", "empty"])
def test_a_malformed_lane_mapping_is_refused(raw):
    """One bad entry disables the whole mapping — never a partial lane plan."""
    caps, error = _lanes().parse_lane_capacities(raw)
    assert caps is None
    assert error, "a malformed lane mapping must name the problem"


# ── dispatch honours the lane capacity ──────────────────────────────────────

def _card(conn, *, title: str, risk: str, assignee: str = "alice") -> str:
    tid = kb.create_task(conn, title=title, body=f"Goal\nRISK: {risk}\n", assignee=assignee)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    conn.commit()
    return tid


def _hold_a_slot(conn, task_id: str) -> None:
    """Park a card in ``running`` so it occupies one concurrency slot."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'lock-holder', "
            "claim_expires = ? WHERE id = ?",
            (int(time.time()) + 3600, task_id),
        )


def _spawned_ids(result) -> list[str]:
    return [row[0] for row in result.spawned]


def test_a_card_of_a_full_lane_is_deferred_while_another_lane_runs(
    conn, all_assignees_spawnable,
):
    """The teeth of a lane cap: it holds BACK its own cards, not the board.

    Without lanes both ready cards would spawn (the ceiling leaves room for
    both); with ``low`` already at capacity only the high-risk card runs.
    """
    low = _card(conn, title="low risk card", risk="low")
    _hold_a_slot(conn, low)
    high = _card(conn, title="high risk card", risk="high")
    low2 = _card(conn, title="second low risk card", risk="low")

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=3,
        lane_capacities={"high": 1, "normal": 1, "low": 1},
    )

    assert _spawned_ids(result) == [high]
    assert result.skipped_lane_capped == [(low2, "low", 1)]


def test_without_lane_capacities_the_board_behaves_exactly_as_before(
    conn, all_assignees_spawnable,
):
    """``kanban.lanes`` unset = one shared pool, unchanged."""
    low = _card(conn, title="low risk card", risk="low")
    _hold_a_slot(conn, low)
    high = _card(conn, title="high risk card", risk="high")
    low2 = _card(conn, title="second low risk card", risk="low")

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=3,
    )

    assert _spawned_ids(result) == [high, low2]
    assert result.skipped_lane_capped == []


def test_a_high_risk_card_dispatches_while_a_low_risk_card_holds_the_pool(
    conn, all_assignees_spawnable,
):
    """The acceptance case: low-risk work holds a slot, high-risk work still runs."""
    low = _card(conn, title="low risk card", risk="low")
    _hold_a_slot(conn, low)
    high = _card(conn, title="high risk card", risk="high")

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=2,
        lane_capacities={"high": 1, "normal": 1, "low": 1},
    )

    assert _spawned_ids(result) == [high]
    running = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
    ).fetchone()[0]
    assert running == 2, "the high-risk card runs beside the low-risk one"


def test_a_lane_capacity_never_exceeds_the_hard_ceiling(conn, all_assignees_spawnable):
    """``kanban.max_in_progress`` stays the ceiling; a lane only subdivides it."""
    high1 = _card(conn, title="high one", risk="high")
    _card(conn, title="high two", risk="high")

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=1,
        lane_capacities={"high": 5},
    )

    assert _spawned_ids(result) == [high1]


def test_a_lane_capacity_of_one_defers_the_second_card_of_that_lane(
    conn, all_assignees_spawnable,
):
    high1 = _card(conn, title="high one", risk="high")
    high2 = _card(conn, title="high two", risk="high")

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=3,
        lane_capacities={"high": 1},
    )

    assert _spawned_ids(result) == [high1]
    assert result.skipped_lane_capped == [(high2, "high", 1)]


def test_a_running_card_without_a_stored_lane_counts_as_normal(
    conn, all_assignees_spawnable,
):
    """A card from before the lane column reads as ``normal``, not as uncapped."""
    legacy = _card(conn, title="legacy card", risk="normal")
    conn.execute("UPDATE tasks SET risk_lane = NULL WHERE id = ?", (legacy,))
    conn.commit()
    _hold_a_slot(conn, legacy)
    normal = _card(conn, title="normal card", risk="normal")

    result = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=3,
        lane_capacities={"high": 1, "normal": 1, "low": 1},
    )

    assert _spawned_ids(result) == []
    assert result.skipped_lane_capped == [(normal, "normal", 1)]


def test_a_card_deferred_by_its_lane_runs_on_a_later_tick(
    conn, all_assignees_spawnable,
):
    """A lane cap is per-tick state, not a permanent block."""
    low = _card(conn, title="low risk card", risk="low")
    _hold_a_slot(conn, low)
    low2 = _card(conn, title="second low risk card", risk="low")

    first = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=3,
        lane_capacities={"high": 1, "normal": 1, "low": 1},
    )
    assert first.skipped_lane_capped == [(low2, "low", 1)]

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'done', claim_lock = NULL, claim_expires = NULL "
            "WHERE id = ?", (low,),
        )

    second = kbd.dispatch_once(
        conn, spawn_fn=lambda *a, **k: 4242,
        max_in_progress=3,
        lane_capacities={"high": 1, "normal": 1, "low": 1},
    )
    assert _spawned_ids(second) == [low2]


# ── the operator-facing configuration ───────────────────────────────────────

@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    """A fresh HERMES_HOME for the CLI / daemon entry points."""
    home = tmp_path / ".hermes"
    (home / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_the_lane_configuration_is_unset_by_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert "lanes" in DEFAULT_CONFIG["kanban"]
    assert DEFAULT_CONFIG["kanban"]["lanes"] is None


def test_the_dispatcher_reads_the_lane_capacities_from_the_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"lanes": {"high": 1, "normal": 1, "low": 1}}},
    )
    assert kbd.configured_lane_capacities() == {"high": 1, "normal": 1, "low": 1}


def test_a_malformed_lane_configuration_falls_back_to_one_shared_pool(monkeypatch, caplog):
    """A typo must not wedge the board — and it must not pass unnoticed."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"lanes": {"high": 0}}},
    )
    with caplog.at_level("WARNING"):
        assert kbd.configured_lane_capacities() is None
    assert any("kanban.lanes" in r.getMessage() for r in caplog.records)


def test_the_daemon_passes_the_configured_lane_capacities(cli_home, monkeypatch):
    import threading

    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kbd, "configured_lane_capacities", lambda: {"high": 1})

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda res: stop.set())

    assert captured.get("lane_capacities") == {"high": 1}


def test_the_cli_passes_the_configured_lane_capacities_to_the_dispatcher(cli_home, monkeypatch):
    import argparse

    from hermes_cli import kanban_ops

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"lanes": {"high": 2, "normal": 1, "low": 1}}},
    )
    captured: dict = {}
    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kb.DispatchResult())[1],
    )

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kanban_ops._cmd_dispatch(args)

    assert captured.get("lane_capacities") == {"high": 2, "normal": 1, "low": 1}


def test_the_cli_refuses_a_malformed_lane_configuration(cli_home, monkeypatch, capsys):
    import argparse

    from hermes_cli import kanban_ops

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"lanes": {"urgent": 1}}},
    )
    captured: dict = {}
    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kb.DispatchResult())[1],
    )

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kanban_ops._cmd_dispatch(args)

    assert captured.get("lane_capacities") is None
    assert "kanban.lanes" in capsys.readouterr().err


def test_the_gateway_resolves_the_lane_capacities():
    from gateway import kanban_watchers_dispatcher as gw

    settings = gw._resolve_dispatcher_settings({"lanes": {"high": 1, "low": 1}}, kb)
    assert settings.lane_capacities == {"high": 1, "low": 1}


def test_the_gateway_refuses_a_malformed_lane_configuration(caplog):
    from gateway import kanban_watchers_dispatcher as gw

    with caplog.at_level("WARNING"):
        settings = gw._resolve_dispatcher_settings({"lanes": {"high": -1}}, kb)
    assert settings.lane_capacities is None
    assert any("kanban.lanes" in r.getMessage() for r in caplog.records)


def test_the_suppression_summary_names_a_full_lane():
    """The gateway's "why is nothing spawning" line must name a full lane."""
    result = kbd.DispatchResult(skipped_lane_capped=[("t_1", "high", 1)])
    assert "lane_capped=1" in kbd.describe_suppression([result])
