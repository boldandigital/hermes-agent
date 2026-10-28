"""Probe-budget guard: the "one more probe round, then I write" failure mode.

The preambles below are VERBATIM from a real looping session
(``20261027_112715_f26388``) that ran six probe rounds and never wrote a file.
"""

from __future__ import annotations

import pytest

from agent.probe_budget import (
    DEFAULT_PROBE_HALT_AFTER_ROUNDS,
    DEFAULT_PROBE_NOTICE_AFTER_ROUNDS,
    announces_final_batch,
    append_probe_notice,
    resolve_probe_budget,
    round_produced_deliverable,
)


# --- announces_final_batch ------------------------------------------------

# Captured from the looping session — every one of these promised to act next
# and then gathered again.
LOOPING_PREAMBLES = [
    "All seams mapped. Three last facts to write an exact brief — then I cut policy + code in parallel:",
    "All seams mapped. Now I read the exact text I must patch (source + policy) in one pass, then I write:",
    "Seams mapped — now the cut. Three last facts (config pattern, event retention, tool error surfacing), then I write:",
    "All seams mapped. Final four facts, then the patch lands:",
    "All seams mapped — now the cut. Final read batch (exact text to patch), then every file lands:",
    "Executing now. Anchors first (one batch), then the patch lands — no more rounds.",
    "Ahoy — aye, executing NOW. Last exact-text batch (one round), then every edit lands:",
    "Anchors in hand. Executing — final probe batch (one round), then every file lands:",
]


@pytest.mark.parametrize("text", LOOPING_PREAMBLES)
def test_detects_captured_looping_preambles(text):
    assert announces_final_batch(text) is True


@pytest.mark.parametrize("text", [
    "The final answer is 42.",
    "Here is the summary of the changes you asked for.",
    "I read the file and found three bugs in the parser.",
    "Let me check the logs now.",
    "Last, one more consideration: the tests pass.",
    "Final batch of reads complete. Now writing the patch.",
    "All seams mapped.",
    "",
    None,
])
def test_ignores_ordinary_prose(text):
    assert announces_final_batch(text) is False


# --- deliverable classification -------------------------------------------

def test_write_and_patch_are_deliverables():
    assert round_produced_deliverable(["write_file"]) is True
    assert round_produced_deliverable(["patch"]) is True
    assert round_produced_deliverable(["read_file", "write_file"]) is True


def test_probe_tools_are_not_deliverables():
    # execute_code/terminal are the probe vector: a shell-out read looks like work.
    for names in (["read_file"], ["execute_code"], ["terminal"], ["search_files"], []):
        assert round_produced_deliverable(names) is False


# --- config resolution ----------------------------------------------------

def test_defaults_when_section_absent():
    enabled, notice_after, halt_after = resolve_probe_budget(None)
    assert enabled is False
    assert notice_after == DEFAULT_PROBE_NOTICE_AFTER_ROUNDS
    assert halt_after == DEFAULT_PROBE_HALT_AFTER_ROUNDS


def test_explicit_mapping():
    enabled, notice_after, halt_after = resolve_probe_budget(
        {"enabled": True, "notice_after_rounds": 3, "halt_after_rounds": 9}
    )
    assert (enabled, notice_after, halt_after) == (True, 3, 9)


def test_zero_halt_keeps_notice_only():
    enabled, notice_after, halt_after = resolve_probe_budget(
        {"enabled": True, "notice_after_rounds": 2, "halt_after_rounds": 0}
    )
    assert enabled is True
    assert notice_after == 2
    assert halt_after == 0


def test_garbage_values_fall_back_to_defaults():
    enabled, notice_after, halt_after = resolve_probe_budget(
        {"enabled": True, "notice_after_rounds": "nonsense", "halt_after_rounds": None}
    )
    assert enabled is True
    assert notice_after == DEFAULT_PROBE_NOTICE_AFTER_ROUNDS
    assert halt_after == DEFAULT_PROBE_HALT_AFTER_ROUNDS


# --- notice injection -----------------------------------------------------

def test_append_probe_notice_targets_the_newest_tool_result():
    messages = [{"role": "user", "content": "go"}, {"role": "tool", "content": "result"}]
    assert append_probe_notice(messages, "NOTICE") is True
    assert messages[-1]["content"] == "result\n\nNOTICE"
    # Older rows are untouched (cache-safe contract).
    assert messages[0]["content"] == "go"


def test_append_probe_notice_refuses_non_tool_tail():
    messages = [{"role": "assistant", "content": "thinking"}]
    assert append_probe_notice(messages, "NOTICE") is False
    assert messages[-1]["content"] == "thinking"


def test_append_probe_notice_handles_multimodal_content():
    messages = [{"role": "tool", "content": [{"type": "text", "text": "result"}]}]
    assert append_probe_notice(messages, "NOTICE") is True
    assert messages[-1]["content"][-1] == {"type": "text", "text": "NOTICE"}


# --- runtime wiring -------------------------------------------------------

class _FakeAgent:
    def __init__(self, **kw):
        self._probe_budget_enabled = True
        self._probe_notice_after = 4
        self._probe_halt_after = 15
        self._probe_rounds_without_deliverable = 0
        self._probe_notice_level = 0
        self.messages = [{"role": "tool", "content": "r"}]
        for k, v in kw.items():
            setattr(self, k, v)


def _budget():
    from agent.turn_tool_round import _apply_probe_budget
    return _apply_probe_budget


def test_probe_rounds_accumulate_and_notice_fires():
    agent = _FakeAgent()
    apply = _budget()
    for _ in range(3):
        assert apply(agent, messages=agent.messages, round_tool_names={"execute_code"},
                     preamble="Reading the next file.") == 0
    assert agent._probe_rounds_without_deliverable == 3
    assert agent._probe_notice_level == 0  # notice fires at round 4

    assert apply(agent, messages=agent.messages, round_tool_names={"execute_code"},
                 preamble="") == 0
    assert agent._probe_notice_level == 1
    assert "probe budget" in agent.messages[-1]["content"]


def test_deliverable_resets_the_counter():
    agent = _FakeAgent()
    apply = _budget()
    apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="")
    apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="")
    assert agent._probe_rounds_without_deliverable == 2
    apply(agent, messages=agent.messages, round_tool_names={"write_file"}, preamble="")
    assert agent._probe_rounds_without_deliverable == 0
    assert agent._probe_notice_level == 0


def test_final_batch_announcement_escalates_early():
    agent = _FakeAgent()
    apply = _budget()
    # Round 2 with a "last batch, then I write" promise fires the notice early.
    apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="")
    apply(agent, messages=agent.messages, round_tool_names={"execute_code"},
          preamble="All seams mapped. Final read batch, then every file lands:")
    assert agent._probe_notice_level == 1


def test_halt_fires_at_the_threshold():
    agent = _FakeAgent(_probe_halt_after=3)
    apply = _budget()
    assert apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="") == 0
    assert apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="") == 0
    assert apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="") == 3


def test_disabled_guard_is_inert():
    agent = _FakeAgent(_probe_budget_enabled=False, _probe_halt_after=1)
    apply = _budget()
    assert apply(agent, messages=agent.messages, round_tool_names={"execute_code"}, preamble="") == 0
    assert agent._probe_rounds_without_deliverable == 0


# --- prompt counterweight -------------------------------------------------

def test_ship_discipline_block_is_present_and_directive():
    from agent.prompt_builder import SHIP_DISCIPLINE_GUIDANCE
    text = SHIP_DISCIPLINE_GUIDANCE
    assert "ship_the_ask" in text
    assert "ONE read batch" in text
    assert "verification is ONE pass" in text


def test_ship_discipline_leads_the_guidance_stack():
    """The counterweight must come BEFORE the mandate stack, not after it.

    A counterweight that arrives after the mandate does not win: the model reads
    "keep working until you have actually exercised the code" last and keeps
    probing. This is the exact regression that produced the looping session.
    """
    from types import SimpleNamespace

    from agent.system_prompt import _guidance_parts

    agent = SimpleNamespace(
        valid_tool_names=["read_file", "write_file", "execute_code"],
        _task_completion_guidance=True,
        _parallel_tool_call_guidance=True,
        _tool_use_enforcement="auto",
        _execution_guidance=False,
        _ship_discipline=True,
        model="MiniMax-M3.1-Flash",
    )
    parts = [p for p in _guidance_parts(agent) if p]
    joined = "\n\n".join(parts)

    ship_idx = joined.find("ship_the_ask")
    assert ship_idx >= 0, "ship discipline block missing from the guidance stack"
    # "Finishing the job" is the keep-probing mandate it has to counter.
    finishing_idx = joined.find("Finishing the job")
    assert finishing_idx >= 0, "expected the task-completion mandate to be present"
    assert ship_idx < finishing_idx, (
        "ship discipline must lead the guidance stack, not trail it"
    )
    # And it must appear exactly once.
    assert joined.count("ship_the_ask") == 2  # opening + closing XML tag


def test_ship_discipline_can_be_disabled():
    from types import SimpleNamespace

    from agent.system_prompt import _guidance_parts

    agent = SimpleNamespace(
        valid_tool_names=["read_file"],
        _task_completion_guidance=True,
        _parallel_tool_call_guidance=True,
        _tool_use_enforcement="auto",
        _execution_guidance=False,
        _ship_discipline=False,
        model="MiniMax-M3.1-Flash",
    )
    joined = "\n\n".join(p for p in _guidance_parts(agent) if p)
    assert "ship_the_ask" not in joined
