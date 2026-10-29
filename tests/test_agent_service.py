"""Unit tests for the pure-function parts of the agent runtime.

Scope note: a full end-to-end integration test requires a test database
(Postgres + Alembic apply + Agent/AgentPrompt seed + mocked Anthropic
SDK). The repo has no existing pytest DB harness, so wiring one up
sits outside Commit 1 scope. These tests cover the bits that don't
need DB/API:

  • _parse_final_response — tag extraction + JSON parsing edge cases
  • _compute_cost — pricing-table math
  • record-number extraction — confirms every live numbering style is
    picked up whole and prose / non-record codes are not

End-to-end agent invocation (tool loop driving the model + DB writes)
is gated by a follow-up commit that lands a DB test harness. The
agent_service code that touches DB/API is reviewed by reading it; the
parser + pattern matchers are reviewed by these tests passing.

Run from the backend root with:
    pytest tests/test_agent_service.py -v
"""

from __future__ import annotations

import re

import pytest

from app.services.agents.agent_service import (
    _RECORD_TOKEN,
    _classify_record_token,
    _compute_cost,
    _parse_final_response,
)


# ─── _parse_final_response ─────────────────────────────────────────────


def test_parse_extracts_well_formed_response() -> None:
    text = """<reasoning>
The incident appears to be a rotating equipment failure.
</reasoning>
<suggestion>
{"recommendedMethod": "FISHBONE", "proposedRootCauses": ["a", "b"]}
</suggestion>
<confidence>0.78</confidence>
"""
    result = _parse_final_response(text)
    assert "rotating equipment failure" in result["reasoning"]
    assert result["suggestion"] == {
        "recommendedMethod": "FISHBONE",
        "proposedRootCauses": ["a", "b"],
    }
    assert result["confidence"] == 0.78


def test_parse_handles_missing_blocks_gracefully() -> None:
    # An agent that emits free prose without our tags shouldn't crash —
    # we just return Nones so the UI can render the raw text fallback.
    result = _parse_final_response("Just some prose without tags.")
    assert result == {"reasoning": None, "suggestion": None, "confidence": None}


def test_parse_unparsed_suggestion_is_preserved_under_marker() -> None:
    # If the agent emits malformed JSON inside <suggestion>, we keep
    # the raw text under "_unparsed" so prompt engineers can see what
    # the model produced and tune accordingly.
    text = "<suggestion>not valid json {missing brace</suggestion>"
    result = _parse_final_response(text)
    assert result["suggestion"] == {"_unparsed": "not valid json {missing brace"}


def test_parse_confidence_out_of_range_is_dropped() -> None:
    # The contract says confidence is 0..1. Values outside this range
    # are suspicious — drop them rather than store noise.
    assert _parse_final_response("<confidence>1.5</confidence>")["confidence"] is None
    assert _parse_final_response("<confidence>-0.2</confidence>")["confidence"] is None
    assert _parse_final_response("<confidence>0.5</confidence>")["confidence"] == 0.5


def test_parse_confidence_non_numeric_is_dropped() -> None:
    assert _parse_final_response("<confidence>high</confidence>")["confidence"] is None


def test_parse_first_block_wins() -> None:
    # Non-greedy regex picks the first <suggestion>...</suggestion>
    # block. The current implementation is fine with one block; this
    # test pins the behaviour so a future "support multiple" change
    # is a deliberate decision.
    text = '<suggestion>{"x": 1}</suggestion><suggestion>{"x": 2}</suggestion>'
    assert _parse_final_response(text)["suggestion"] == {"x": 1}


# ─── _compute_cost ─────────────────────────────────────────────────────


def test_compute_cost_haiku() -> None:
    # Haiku: $1/M input + $5/M output. 10K input + 2K output should be
    # 0.01 + 0.01 = $0.02. Rounded to 6 dp.
    cost = _compute_cost("claude-haiku-4-5-20251001", 10_000, 2_000)
    assert cost == pytest.approx(0.02)


def test_compute_cost_opus() -> None:
    # Opus 4.7: $5/M input + $25/M output. 10K input + 2K output should be
    # 0.05 + 0.05 = $0.10.
    cost = _compute_cost("claude-opus-4-7", 10_000, 2_000)
    assert cost == pytest.approx(0.10)


def test_compute_cost_unknown_model_uses_default() -> None:
    # Sonnet pricing is the conservative default ($3/M in, $15/M out).
    cost_default = _compute_cost("claude-something-new", 10_000, 2_000)
    cost_sonnet = _compute_cost("claude-sonnet-4-6", 10_000, 2_000)
    assert cost_default == cost_sonnet


def test_compute_cost_zero_tokens() -> None:
    assert _compute_cost("claude-opus-4-7", 0, 0) == 0.0


# ─── record-number extraction (hallucination check) ────────────────────


def _record_refs(text: str) -> list[str]:
    return [
        m.group(1)
        for m in _RECORD_TOKEN.finditer(text)
        if _classify_record_token(m.group(1)) is not None
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        # Every numbering style present in the live data, whole token each.
        ("See INC-2026-NW-0045 for context.", ["INC-2026-NW-0045"]),
        ("Compare INC-PFI-002 and INC-SW-HIST-02-01.", ["INC-PFI-002", "INC-SW-HIST-02-01"]),
        ("Permit PTW-AXM-2026-0841 was active.", ["PTW-AXM-2026-0841"]),
        ("Site-prefixed FACROLL-PTW-AMD-01 too.", ["FACROLL-PTW-AMD-01"]),
        ("Observations OBS-SCD-NW-00066 and SO-2026-AGB-0001.", ["OBS-SCD-NW-00066", "SO-2026-AGB-0001"]),
        ("Near miss NM-2026-SW-0075 preceded it.", ["NM-2026-SW-0075"]),
        ("CAPA-MAG-MAG-TIR1-01 and CAPA-Q-2026-NW-019 were raised.", ["CAPA-MAG-MAG-TIR1-01", "CAPA-Q-2026-NW-019"]),
        ("Study HIRA-2026-SW-DEMO-001, FLRA-AGB-0002.", ["HIRA-2026-SW-DEMO-001", "FLRA-AGB-0002"]),
    ],
)
def test_record_refs_match_live_formats(text: str, expected: list[str]) -> None:
    assert _record_refs(text) == expected


def test_record_refs_never_take_a_fragment() -> None:
    # The old fixed regex matched "PTW-AXM-2026" out of PTW-AXM-2026-0841 and
    # flagged a real permit as invented.
    assert _record_refs("PTW-AXM-2026-0841") == ["PTW-AXM-2026-0841"]


def test_record_refs_skip_prose_and_non_record_codes() -> None:
    prose = """
    The INCIDENT was severe, but the previous OBSERVATION did warn about it.
    PTW process and NM tracking; ISO-45001, IS-14489, XCORR-019, AINV-2026-000023,
    PTW-NW (no number) and CAPA_ASSISTANT are not record numbers.
    """
    assert _record_refs(prose) == []


def test_record_refs_catch_invented_ids_with_real_shape() -> None:
    # The DB lookup then rejects these; the extractor must still pick them up.
    assert _record_refs("Similar to INC-2099-FAKE-9999 and CAPA-ZZ-0000.") == [
        "INC-2099-FAKE-9999",
        "CAPA-ZZ-0000",
    ]
