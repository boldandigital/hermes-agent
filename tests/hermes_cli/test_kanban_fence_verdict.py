"""The FENCE verdict record: one parseable review verdict, and the next
attempt starting from it.

``request_changes`` sends a card back to the implementer. The only trace of
the review was a free-text ``reason`` plus a closed run summary, so the next
attempt could not tell what a passing result looked like, nor compare this
failure with the last one. The FENCE record fixes both: the verdict travels
as a COMMENT whose body starts with ``FENCE:`` and holds one field per line::

    FENCE:
    EXPECTED: <what a passing result looks like>
    SEEN: <what actually happened>
    VERDICT: pass|fail

``SEEN:`` is ONE line — the failing command and its error — so two failures
of the same class compare across cards, and the next attempt's worker context
carries the record as an explicit, parseable section.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

FENCE_PREFIX = "FENCE:"
VERDICT_SECTION = "## Previous verdict (FENCE)"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _comments(conn, tid):
    return kb.list_comments(conn, tid)


def _fence_comments(conn, tid):
    """The FENCE comments on the card, oldest first."""
    return [c for c in _comments(conn, tid) if c.body.startswith(FENCE_PREFIX)]


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None) for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _send_back(conn, tid, *, reason, verdict=None):
    """Run a card through implement -> review -> changes-requested."""
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.request_review(
        conn, tid, summary="v1", reviewer="reviewer",
        expected_run_id=claimed.current_run_id,
    ) is True
    reviewing = kb.claim_review_task(conn, tid)
    assert reviewing is not None
    return kb.request_changes(
        conn, tid, reason=reason, verdict=verdict,
        expected_run_id=reviewing.current_run_id,
    )


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_request_changes_stores_a_fence_comment(kanban_home: Path) -> None:
    """The verdict is stored as a COMMENT with the fixed prefix, not as prose
    buried in the reason."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        ok, implementer = _send_back(conn, tid, reason="the build is broken")
        assert (ok, implementer) == (True, "builder")

        fences = _fence_comments(conn, tid)
        assert len(fences) == 1, "exactly one FENCE record per send-back"
        body = fences[0].body
        assert body.startswith(FENCE_PREFIX)
        assert "EXPECTED: " in body
        assert "SEEN: " in body
        assert "VERDICT: " in body
        # The reviewer is the author of the record, like on the event.
        assert fences[0].author == "reviewer"


def test_request_changes_records_the_verdict_on_the_event(kanban_home: Path) -> None:
    """The parsed verdict rides the ``changes_requested`` event next to the
    existing reason/implementer/reviewer/status keys."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(conn, tid, reason="the build is broken")

        events = _events(conn, tid, kind="changes_requested")
        assert len(events) == 1
        payload = events[0][1]
        # The pre-existing keys are untouched.
        assert payload["reason"] == "the build is broken"
        assert payload["implementer"] == "builder"
        assert payload["reviewer"] == "reviewer"
        assert payload["status"]
        # The verdict travels in its own key, in the fixed shape.
        assert payload["verdict"]["verdict"] == "fail"
        assert payload["verdict"]["seen"] == "the build is broken"
        assert payload["verdict"]["expected"]


def test_fence_seen_line_is_one_line(kanban_home: Path) -> None:
    """``SEEN:`` states the failure in ONE line so two failures of the same
    class compare across cards."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(
            conn, tid,
            reason="pytest failed\n  File x.py, line 3\nAssertionError: boom",
        )

        body = _fence_comments(conn, tid)[0].body
        lines = body.split("\n")
        # Exactly the four record lines: prefix + one per field.
        assert len(lines) == 4, lines
        assert lines[0] == FENCE_PREFIX
        assert lines[1].startswith("EXPECTED: ")
        assert lines[2].startswith("SEEN: ")
        assert lines[3].startswith("VERDICT: ")
        # The flattened failure still says everything the reason said.
        assert "pytest failed" in lines[2]
        assert "AssertionError: boom" in lines[2]


def test_reason_carrying_the_three_lines_is_stored_verbatim(kanban_home: Path) -> None:
    """A reviewer who already wrote the three lines gets them stored as they
    are — the record is not re-worded."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(
            conn, tid,
            reason="EXPECTED: all tests green\nSEEN: pytest failed: 3 errors\nVERDICT: fail",
        )

        body = _fence_comments(conn, tid)[0].body
        assert body == (
            "FENCE:\n"
            "EXPECTED: all tests green\n"
            "SEEN: pytest failed: 3 errors\n"
            "VERDICT: fail"
        )


def test_explicit_verdict_is_recorded_verbatim(kanban_home: Path) -> None:
    """The explicit ``verdict=`` argument is the reviewer's own record."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(
            conn, tid,
            reason="please fix",
            verdict="EXPECTED: docs updated\nSEEN: docs stale\nVERDICT: fail",
        )

        body = _fence_comments(conn, tid)[0].body
        assert body == (
            "FENCE:\n"
            "EXPECTED: docs updated\n"
            "SEEN: docs stale\n"
            "VERDICT: fail"
        )


def test_missing_verdict_is_synthesized_honestly(kanban_home: Path) -> None:
    """No verdict from the reviewer: the record says so instead of inventing
    an expectation."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(conn, tid, reason="the tests are red")

        body = _fence_comments(conn, tid)[0].body
        assert "EXPECTED: (not stated by reviewer)" in body
        assert "SEEN: the tests are red" in body
        assert "VERDICT: fail" in body


def test_fence_body_is_redacted_like_the_reason(kanban_home: Path) -> None:
    """The record holds reviewer text, so it goes through the same redaction
    the reason does — same treatment, not a second, stricter rule."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(conn, tid, reason="the login broke\npassword: hunter2")

        body = _fence_comments(conn, tid)[0].body
        assert "hunter2" not in body
        assert "***" in body
        # The reason itself is masked the same way — the record is no leakier.
        payload = _events(conn, tid, kind="changes_requested")[0][1]
        assert payload["reason"] == "the login broke\npassword: ***"


# ---------------------------------------------------------------------------
# The next attempt starts from the record
# ---------------------------------------------------------------------------


def test_next_attempt_context_carries_the_previous_verdict(kanban_home: Path) -> None:
    """The worker context of the NEXT attempt states the previous verdict as
    an explicit section, before the prior attempts."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")
        _send_back(
            conn, tid,
            reason="EXPECTED: all tests green\nSEEN: pytest failed: 3 errors\nVERDICT: fail",
        )

        ctx = kb.build_worker_context(conn, tid)
        assert VERDICT_SECTION in ctx
        assert "EXPECTED: all tests green" in ctx
        assert "SEEN: pytest failed: 3 errors" in ctx
        assert "VERDICT: fail" in ctx
        # The verdict leads: the worker reads it before the attempt history.
        assert ctx.index(VERDICT_SECTION) < ctx.index("## Prior attempts on this task")


def test_card_without_prior_review_has_no_verdict_section(kanban_home: Path) -> None:
    """A card that was never reviewed gets NO verdict section — an empty
    section would teach the worker that nothing was expected of it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="build a thing", assignee="builder")

        ctx = kb.build_worker_context(conn, tid)
        assert VERDICT_SECTION not in ctx
        assert "EXPECTED:" not in ctx
        assert "VERDICT:" not in ctx
