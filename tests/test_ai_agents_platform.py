"""Unit tests for agent-platform helpers fixed on 2026-10-24.

Pure functions only (no DB / API): context redaction, model-id validation,
calibration status sets, HIRA relevance tokeniser, pricing.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.agents_config import _validate_model_id
from app.services.agents.agent_service import _compute_cost
from app.services.agents.calibration import _COMPLETED_STATUSES, _compute_calibration_score
from app.services.agents.context_builders._redact import Redactor
from app.services.agents.context_builders.hira import _words


# ─── Redactor ──────────────────────────────────────────────────────────


def test_redactor_gives_stable_placeholders_per_kind() -> None:
    r = Redactor()
    assert r.name("Ravi Kumar") == "Person 1"
    assert r.name("ravi kumar ") == "Person 1"  # same person, same placeholder
    assert r.name("Asha Rao", "Witness") == "Witness 1"
    assert r.name("Mohan Lal") == "Person 2"
    assert r.name(None) is None and r.name("  ") is None


def test_redactor_scrubs_known_names_emails_and_phones() -> None:
    r = Redactor()
    r.name("Ravi Kumar")
    r.name("Ravi")
    text = "Ravi Kumar told Ravi's supervisor (ravi.k@plant.in, +91 98765 43210) at 10:30."
    out = r.text(text)
    assert "Ravi" not in out and "ravi.k@" not in out and "98765" not in out
    assert "Person 1" in out and "[email]" in out and "[phone]" in out
    # Dates, times and record numbers survive.
    assert r.text("INC-2026-NW-0045 on 2026-08-04 at 10:30") == "INC-2026-NW-0045 on 2026-08-04 at 10:30"


def test_redactor_leaves_non_text_alone() -> None:
    r = Redactor()
    assert r.text(None) is None
    assert r.text(42) == 42


# ─── Model-id validation ───────────────────────────────────────────────


@pytest.mark.parametrize("model", ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-7", " claude-opus-5-5 "])
def test_valid_model_ids_pass(model: str) -> None:
    assert _validate_model_id(model, "primaryModelId") == model.strip()


@pytest.mark.parametrize("model", ["", "gpt-4o", "Claude Sonnet", "claude sonnet 4", "claude-opus-4-7; drop"])
def test_invalid_model_ids_are_rejected(model: str) -> None:
    with pytest.raises(HTTPException) as e:
        _validate_model_id(model, "primaryModelId")
    assert e.value.status_code == 400


# ─── Calibration ───────────────────────────────────────────────────────


def test_completed_statuses_include_pending_review() -> None:
    # Latency/cost averages must count runs nobody has ruled on yet.
    assert "PENDING_REVIEW" in _COMPLETED_STATUSES
    assert {"ACCEPTED", "MODIFIED", "REJECTED"} <= set(_COMPLETED_STATUSES)
    assert "ERRORED" not in _COMPLETED_STATUSES and "RUNNING" not in _COMPLETED_STATUSES


def test_calibration_score_formula() -> None:
    assert _compute_calibration_score(acc=0, mod=0, rej=0, exp=0) is None
    assert _compute_calibration_score(acc=2, mod=2, rej=0, exp=0) == 0.75


# ─── HIRA relevance tokens ─────────────────────────────────────────────


def test_hira_words_drop_stopwords_and_short_tokens() -> None:
    assert _words("Welding of the pressure vessel in Bay 3") == {"welding", "pressure", "vessel", "bay"}


# ─── Pricing ───────────────────────────────────────────────────────────


def test_haiku_alias_priced() -> None:
    assert _compute_cost("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0)
