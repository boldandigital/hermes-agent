"""Prompt-contract tests for the triage specifier and the decomposer.

The system prompts are a data contract: they decide what the model is told to
put on a card. These assertions stay literal and small on purpose — they pin
the rules that must survive any rewording of the surrounding prose.
"""

from __future__ import annotations

from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


def _flat(prompt: str) -> str:
    """Collapse the prompt's hard line wrapping so a marker survives a newline."""
    return " ".join(prompt.split())


_SAME_ANSWER_MARKERS = (
    "the same output every time for the same input",
    "CODE, not a model call",
    "Reserve model steps for judgement calls",
)

_RISK_MARKERS = (
    "RISK: high",
    "RISK: normal",
    "RISK: low",
    "money",
    "auth",
    "secrets",
    "live-client",
    "irreversible",
    "destructive",
)


def test_specifier_prompt_asks_the_same_answer_question():
    prompt = _flat(spec._SYSTEM_PROMPT)
    for marker in _SAME_ANSWER_MARKERS:
        assert marker in prompt, f"specifier prompt is missing {marker!r}"


def test_decomposer_prompt_asks_the_same_answer_question():
    prompt = _flat(decomp._SYSTEM_PROMPT)
    for marker in _SAME_ANSWER_MARKERS:
        assert marker in prompt, f"decomposer prompt is missing {marker!r}"


def test_specifier_prompt_requires_executable_acceptance_criteria():
    assert "acceptance criterion names a command" in _flat(spec._SYSTEM_PROMPT)


def test_both_prompts_carry_the_risk_taxonomy():
    for prompt in (spec._SYSTEM_PROMPT, decomp._SYSTEM_PROMPT):
        flat = _flat(prompt)
        for marker in _RISK_MARKERS:
            assert marker in flat, f"prompt is missing risk marker {marker!r}"


def test_decomposer_prompt_requires_one_risk_line_per_child_body():
    flat = _flat(decomp._SYSTEM_PROMPT)
    assert "RISK: high|normal|low" in flat
    assert "each child" in flat.lower()
