"""Integration: the probe-budget guard inside the real ``run_tool_round``.

Drives the production function with a stubbed agent so the guard is proven to
halt a real turn, not just the pure helper.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.probe_budget import PROBE_BUDGET_HALT_RESPONSE

# ``run_tool_round`` lazily imports ``agent.conversation_loop`` -> ``hermes_bootstrap``
# -> ``pm.environments.activate_dependencies``, which reads ``<repo>/../manifest.json``
# (i.e. the real ``~/.hermes/manifest.json``) in a default-install layout. The
# test-only home I/O guard refuses that read; nothing in this test writes to
# the home, so the guard is bypassed explicitly rather than worked around.
pytestmark = pytest.mark.allow_real_home_io


def _tool_call(name: str, call_id: str = "c1"):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def _agent():
    """Minimal agent with the attributes ``run_tool_round`` touches.

    Built on a real (uninitialised) ``AIAgent`` so the tool-call id/serialisation
    paths are the production ones.
    """
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "nous", "MiniMax-M3.1-Flash", "s1", []
    agent.valid_tool_names = {"execute_code", "read_file", "write_file"}
    agent.log_prefix = ""
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent._invalid_tool_retries = 0
    agent._invalid_json_retries = 0
    agent._tool_guardrail_halt_decision = None
    agent._incremental_persistence_failed = False
    agent._last_persistence_error_cause = None
    agent.stream_delta_callback = None
    agent._probe_budget_enabled = True
    agent._probe_notice_after = 4
    agent._probe_halt_after = 3
    agent._probe_rounds_without_deliverable = 0
    agent._probe_notice_level = 0
    agent.iteration_budget = SimpleNamespace(refund=lambda: None, remaining=10, used=0)
    agent._vprint = lambda *a, **k: None
    agent._safe_print = lambda *a, **k: None
    agent._emit_diagnostic_status = lambda *a, **k: None
    agent._emit_interim_assistant_message = lambda *a, **k: None
    agent._flush_messages_to_session_db = lambda *a, **k: True
    agent._execute_tool_calls = lambda *a, **k: None
    return agent


@pytest.fixture
def _stub_executor(monkeypatch):
    """Neutralize the parts of ``run_tool_round`` that need a live runtime."""
    import agent.turn_tool_round as round_module
    import hermes_cli.observability.shared_metrics_harness as harness

    monkeypatch.setattr(harness, "finish_tool_round", lambda *a, **k: None)

    def _stage(agent, *, assistant_message, finish_reason, messages):
        return (
            {
                "role": "assistant",
                "content": assistant_message.content or "",
                "tool_calls": assistant_message.tool_calls or [],
            },
            False,
        )

    monkeypatch.setattr(round_module, "stage_tool_call_message", _stage)
    # Post-tool compression is irrelevant here and needs a full compressor stub.
    monkeypatch.setattr(
        round_module, "compress_after_tool_results",
        lambda *a, **k: SimpleNamespace(
            end_turn=False, messages=None, active_system_prompt=None,
            conversation_history=None, compression_attempts=0,
            final_response=None, turn_exit_reason=None, current_turn_user_idx=0,
        ),
    )
    return round_module


def _run(round_module, agent, tool_name, preamble=""):
    assistant_message = SimpleNamespace(content=preamble, tool_calls=[_tool_call(tool_name)])
    return round_module.run_tool_round(
        agent,
        assistant_message=assistant_message,
        finish_reason="tool_calls",
        messages=[{"role": "user", "content": "do the thing"}],
        conversation_history=[],
        api_call_count=1,
        effective_task_id="t",
        user_message="do the thing",
        system_message="",
        active_system_prompt="",
        compression_attempts=0,
        max_compression_attempts=1,
        final_response=None,
        failed=False,
        _turn_exit_reason=None,
        truncated_tool_call_retries=0,
        current_turn_user_idx=0,
    )


def test_probe_rounds_halt_the_turn_once_the_budget_is_spent(_stub_executor):
    agent = _agent()
    agent.context_compressor = SimpleNamespace(compress=lambda **k: None)
    agent.iteration_budget = SimpleNamespace(refund=lambda: None, remaining=10, used=0)

    verdicts = [
        _run(_stub_executor, agent, "execute_code", "All seams mapped. Final four facts, then the patch lands:")
        for _ in range(3)
    ]

    halted = [v for v in verdicts if v._turn_exit_reason == "probe_budget_halt"]
    assert len(halted) == 1, "the third probe round must halt the turn"
    assert halted[0].action == "break"
    assert str(halted[0].final_response) == PROBE_BUDGET_HALT_RESPONSE.format(rounds=3)


def test_a_deliverable_round_never_halts(_stub_executor):
    agent = _agent()
    agent.context_compressor = SimpleNamespace(compress=lambda **k: None)
    for _ in range(10):
        verdict = _run(_stub_executor, agent, "write_file")
    assert verdict._turn_exit_reason != "probe_budget_halt"


def test_disabled_budget_never_halts(_stub_executor):
    agent = _agent()
    agent.context_compressor = SimpleNamespace(compress=lambda **k: None)
    agent._probe_budget_enabled = False
    for _ in range(10):
        verdict = _run(_stub_executor, agent, "execute_code", "One more read batch, then I write:")
    assert verdict._turn_exit_reason != "probe_budget_halt"
