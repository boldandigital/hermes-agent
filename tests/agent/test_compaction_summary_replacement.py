"""Regression tests for the compounding-summary bug.

A compaction summary must REPLACE the previous summary, never embed it. Before the
fix, the summary was written back as a user row and the next compaction merged it
with the freshly generated summary, so the compressed context grew ~2x per pass:

    6201 -> 9423 -> 18366 -> 33994 -> 73962 -> 148008 -> 222054 -> 443936 chars

The final summary alone (~111K tokens) exceeded the compaction trigger, so the
compressor could not compress further and the turn died with
"Context length exceeded ... Cannot compress further".

The doubling happens in the USER-ROW MERGE path, not only in summary generation:
``_merge_user_content`` concatenated two adjacent user rows unconditionally, and
the summary lives in a user row.
"""

from agent.agent_runtime_helpers import _merge_user_content, drop_thinking_only_and_merge_users
from agent.context_compressor import (
    LEGACY_SUMMARY_PREFIX,
    SUMMARY_PREFIX,
    _HISTORICAL_SUMMARY_PREFIXES,
)


def _summary_text(marker: str) -> str:
    """A realistic summary user row: the prefix, then the compacted content."""
    return f"{SUMMARY_PREFIX}\n\n### Historical Task Snapshot\n{marker}"


def test_merge_does_not_duplicate_a_summary_row():
    """A summary row merged with a continuation must not repeat the summary body."""
    prev = _summary_text("ORIGINAL-SUMMARY-BODY")
    cur = "the user's follow-up question"

    merged = _merge_user_content(prev, cur)

    assert merged.count("ORIGINAL-SUMMARY-BODY") == 1
    assert "follow-up question" in merged


def test_merge_of_two_summaries_keeps_only_the_newest():
    """Summary replaces summary: the OLD summary body must be gone entirely."""
    old = _summary_text("OLD-SUMMARY-BODY")
    new = _summary_text("NEW-SUMMARY-BODY")

    merged = _merge_user_content(old, new)

    assert "OLD-SUMMARY-BODY" not in merged
    assert merged.count("NEW-SUMMARY-BODY") == 1


def test_merge_of_two_plain_user_rows_still_joins():
    """The normal path must be unchanged: two ordinary user rows still join."""
    merged = _merge_user_content("first question", "second question")

    assert "first question" in merged
    assert "second question" in merged


def test_adjacent_user_rows_merge_drops_the_older_summary(monkeypatch):
    """End to end through the sanitizer: the older summary body does not survive.

    The sanitizer asks the runtime agent whether a row is a thinking-only turn.
    Stub that agent so the test never reads the real Hermes home.
    """
    class _StubAgent:
        @staticmethod
        def _is_thinking_only_assistant(m, **kwargs):
            return False

    import logging

    class _StubRuntime:
        AIAgent = _StubAgent
        logger = logging.getLogger("test.stub")

    monkeypatch.setattr("agent.agent_runtime_helpers._ra", lambda: _StubRuntime())

    messages = [
        {"role": "user", "content": _summary_text("STALE-SUMMARY-BODY")},
        {"role": "user", "content": _summary_text("CURRENT-SUMMARY-BODY")},
    ]

    merged = drop_thinking_only_and_merge_users(list(messages))

    assert len(merged) == 1
    assert "STALE-SUMMARY-BODY" not in merged[0]["content"]
    assert "CURRENT-SUMMARY-BODY" in merged[0]["content"]


def test_historical_prefixes_are_recognised_as_summaries():
    """Every known summary prefix counts, not just the current one."""
    assert SUMMARY_PREFIX
    assert LEGACY_SUMMARY_PREFIX
    assert _HISTORICAL_SUMMARY_PREFIXES

    for prefix in {SUMMARY_PREFIX, LEGACY_SUMMARY_PREFIX, *_HISTORICAL_SUMMARY_PREFIXES}:
        old = f"{prefix}\n\nOLD-BODY"
        new = f"{prefix}\n\nNEW-BODY"
        merged = _merge_user_content(old, new)
        assert "OLD-BODY" not in merged, f"prefix not treated as a summary: {prefix[:40]!r}"


def test_successive_compactions_do_not_double_the_context():
    """The core regression: N successive compactions must not grow the context.

    Each pass merges the stored summary with a freshly generated one. Without the
    fix the merge concatenates them, so the stored summary grows by the size of a
    whole summary every pass (the live session grew ~2x per pass). With the fix
    the newest summary REPLACES the stored one, so the size stays constant.
    """
    body = "B" * 1000
    stored = _summary_text(body)
    sizes = []

    for _ in range(4):
        fresh = _summary_text(body)
        merged = _merge_user_content(stored, fresh)
        sizes.append(len(merged))
        stored = merged

    assert len(set(sizes)) == 1, f"context still compounds: {sizes}"
    # A single summary is the prefix plus the body - it must NOT grow pass to pass.
    assert sizes[0] == len(_summary_text("B" * 1000)), f"unexpected summary size: {sizes}"


def test_minimax_context_window_is_one_million():
    """MiniMax's real window is 1,000,000 tokens - never the overflow amount."""
    from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS

    assert DEFAULT_CONTEXT_LENGTHS["minimax-m3"] == 1_000_000


def test_minimax_trigger_is_not_clamped_by_the_absolute_cap():
    """The 1M window's ratio trigger survives the config's absolute cap.

    ``compression.threshold_tokens`` is an absolute ceiling applied as
    ``min(ratio x window, cap)``. On a 1,000,000-token window a cap of 350000
    must NOT bind below the ratio value, so the ratio still decides.
    """
    from agent.context_compressor import ContextCompressor

    def trigger(cap):
        c = ContextCompressor.__new__(ContextCompressor)
        c.threshold_tokens_cap = cap
        c._config_threshold_percent = 0.35
        c.model_thresholds = {}
        c.max_tokens = 8192
        return c._derive_trigger("minimax-m3", 1_000_000, "minimax")[2]

    assert trigger(350_000) == trigger(None) == 347_132
    # The old 96,000 cap DID bind, and that is the bug this pins.
    assert trigger(96_000) == 96_000
