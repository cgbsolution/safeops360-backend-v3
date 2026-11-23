"""Integration connector framework: offline unit tests (no DB, no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import integrations as svc

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


# ── transforms ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,transform,arg,expected",
    [
        ("abc", "upper", None, "ABC"),
        ("ABC", "lower", None, "abc"),
        ("  x ", "trim", None, "x"),
        (42, "string", None, "42"),
        ("7.9", "int", None, 7),
        (True, "bool_yn", None, "Y"),
        ("false", "bool_yn", None, "N"),
        (False, "bool_10", None, 0),
        ("UNSAFE_ACT", "map", "UNSAFE_ACT=UA;UNSAFE_CONDITION=UC", "UA"),
        ("SAFE_ACT", "map", "UNSAFE_ACT=UA;*=OT", "OT"),
        ("SAFE_ACT", "map", "UNSAFE_ACT=UA", "SAFE_ACT"),  # unmapped passes through
        ("42", "prefix", "SO-", "SO-42"),
        ("42", "suffix", "/HSR", "42/HSR"),
        ("abcdef", "truncate", "3", "abc"),
        (None, "default", "NA", "NA"),
        ("", "default", "NA", "NA"),
        ("set", "default", "NA", "set"),
        ("ignored", "constant", "SAFEOPS", "SAFEOPS"),
        (None, "constant", "SAFEOPS", "SAFEOPS"),
        (None, "upper", None, None),
    ],
)
def test_apply_transform(value, transform, arg, expected):
    assert svc.apply_transform(value, transform, arg) == expected


def test_date_transforms():
    d = datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc)
    assert svc.apply_transform(d, "date") == "2026-11-23"
    assert svc.apply_transform(d, "datetime") == "2026-11-23T04:30:00+00:00"
    assert svc.apply_transform(d, "epoch_ms") == int(d.timestamp() * 1000)
    # naive DB datetimes are treated as UTC, ISO strings are parsed
    assert svc.apply_transform(d.replace(tzinfo=None), "date") == "2026-11-23"
    assert svc.apply_transform("2026-11-23T04:30:00Z", "date") == "2026-11-23"
    assert svc.apply_transform("not a date", "date") is None


def test_reverse_transforms_invert_map_prefix_and_dates():
    assert svc.reverse_transform("UA", "map", "UNSAFE_ACT=UA;UNSAFE_CONDITION=UC") == "UNSAFE_ACT"
    assert svc.reverse_transform("SO-42", "prefix", "SO-") == "42"
    assert svc.reverse_transform("Y", "bool_yn") is True
    assert svc.reverse_transform("23-11-2026", "date") == datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert svc.reverse_transform("whatever", "constant", "X") is None


def test_build_payload_nested_targets_and_direction():
    record = {"number": "SO-2026-HSR-00001", "type": "UNSAFE_ACT", "areaName": "SMS", "externalId": "SAS-1",
              "date": datetime(2026, 11, 23, tzinfo=timezone.utc), "severity": "HIGH"}
    mapping = [
        {"source": "number", "target": "ref"},
        {"source": "type", "target": "kind", "transform": "map", "arg": "UNSAFE_ACT=UA"},
        {"source": "areaName", "target": "location.area", "transform": "upper"},
        {"source": "externalId", "target": "sasId", "direction": "IN"},  # inbound-only: not sent
        {"source": "date", "target": "when"},  # raw datetime is serialised
        {"source": "severity", "target": "meta.source", "transform": "constant", "arg": "SAFEOPS"},
    ]
    out = svc.build_payload(record, mapping)
    assert out == {"ref": "SO-2026-HSR-00001", "kind": "UA", "location": {"area": "SMS"},
                   "when": "2026-11-23T00:00:00+00:00", "meta": {"source": "SAFEOPS"}}


def test_build_payload_without_mapping_sends_flat_record():
    out = svc.build_payload({"a": 1, "d": datetime(2026, 1, 1, tzinfo=timezone.utc)}, [])
    assert out == {"a": 1, "d": "2026-01-01T00:00:00+00:00"}


def test_map_inbound_reads_nested_and_skips_outbound_only_rows():
    mapping = [
        {"source": "externalId", "target": "sasId"},
        {"source": "type", "target": "kind", "transform": "map", "arg": "UNSAFE_ACT=UA;UNSAFE_CONDITION=UC"},
        {"source": "areaName", "target": "location.area"},
        {"source": "status", "target": "status", "direction": "OUT"},
        {"source": "number", "target": "ref"},  # not an inbound-capable field
    ]
    body = {"sasId": "SAS-9", "kind": "UC", "location": {"area": "Cold rolling"}, "status": "CLOSED", "ref": "X"}
    assert svc.map_inbound(body, mapping) == {"externalId": "SAS-9", "type": "UNSAFE_CONDITION", "areaName": "Cold rolling"}


def test_template_mapping_is_valid_config():
    cfg = {"code": "JSL_SAS", "direction": "BOTH", "method": "POST", "authType": "API_KEY",
           "secretEnvVar": "INTEGRATION_JSL_SAS_KEY", "inboundSecretEnvVar": "INTEGRATION_JSL_SAS_INBOUND_KEY",
           "fieldMapping": svc.JSL_SAS_TEMPLATE_MAPPING, "timeoutSeconds": 15}
    assert svc.validate_config(cfg) == []


# ── config validation (secrets never in the DB) ──────────────────────────────
def test_validate_rejects_secret_values_and_foreign_env_names():
    base = {"code": "JSL_SAS", "direction": "OUTBOUND", "authType": "BEARER", "timeoutSeconds": 15}
    errs = svc.validate_config({**base, "secretEnvVar": "DATABASE_URL"})
    assert any("INTEGRATION_" in e for e in errs)
    errs = svc.validate_config({**base, "secretEnvVar": "sk-live-abc123"})
    assert any("INTEGRATION_" in e for e in errs)
    errs = svc.validate_config({**base, "secretEnvVar": "INTEGRATION_SAS", "headers": {"Authorization": "Bearer x"}})
    assert any("credential" in e for e in errs)
    assert svc.validate_config({**base, "secretEnvVar": "INTEGRATION_SAS"}) == []


def test_validate_mapping_rows():
    base = {"code": "X1", "direction": "OUTBOUND", "timeoutSeconds": 15}
    assert svc.validate_config({**base, "fieldMapping": [{"source": "nope", "target": "a"}]})
    assert svc.validate_config({**base, "fieldMapping": [{"source": "number", "target": "bad target"}]})
    assert svc.validate_config({**base, "fieldMapping": [{"source": "number", "target": "a", "transform": "truncate", "arg": "x"}]})
    assert svc.validate_config({**base, "fieldMapping": [{"source": "number", "target": "a"}, {"source": "id", "target": "a"}]})
    assert svc.validate_config({**base, "baseUrl": "ftp://x"})
    assert svc.validate_config({**base, "direction": "INBOUND"})  # inbound needs a key env var
    assert svc.validate_config({**base, "code": "lower"})


def test_resolve_secret_only_reads_integration_vars(monkeypatch):
    monkeypatch.setenv("INTEGRATION_TEST_KEY", "  s3cret ")
    monkeypatch.setenv("JWT_SECRET_FOR_TEST", "nope")
    assert svc.resolve_secret("INTEGRATION_TEST_KEY") == "s3cret"
    assert svc.resolve_secret("JWT_SECRET_FOR_TEST") is None
    assert svc.resolve_secret(None) is None


# ── API key check ────────────────────────────────────────────────────────────
def test_api_key_matches_constant_time_and_fails_closed(monkeypatch):
    calls = []
    real = svc.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(svc.hmac, "compare_digest", spy)
    assert svc.api_key_matches("k-123", "k-123")
    assert not svc.api_key_matches("k-124", "k-123")
    assert calls, "comparison must go through hmac.compare_digest"
    assert not svc.api_key_matches("", "k-123")
    assert not svc.api_key_matches("anything", None)  # unset key refuses everyone
    assert not svc.api_key_matches(None, None)


def test_auth_headers():
    assert svc.auth_headers("NONE", "x") == {}
    assert svc.auth_headers("API_KEY", "x") == {"X-API-Key": "x"}
    assert svc.auth_headers("API_KEY", "x", "X-Sas-Key") == {"X-Sas-Key": "x"}
    assert svc.auth_headers("BEARER", "t") == {"Authorization": "Bearer t"}
    assert svc.auth_headers("BASIC", "u:p") == {"Authorization": "Basic dTpw"}
    assert svc.auth_headers("BEARER", None) == {}


# ── back-off ─────────────────────────────────────────────────────────────────
def test_backoff_schedule_doubles_and_caps():
    assert [svc.backoff_seconds(n) for n in range(1, 8)] == [60, 120, 240, 480, 960, 1920, 3840]
    assert svc.backoff_seconds(20) == svc.BACKOFF_CAP_SECONDS
    assert svc.backoff_seconds(0) == 60


def test_next_attempt_stops_after_budget():
    assert svc.next_attempt_at(1, NOW) == NOW + timedelta(minutes=1)
    assert svc.next_attempt_at(3, NOW) == NOW + timedelta(minutes=4)
    assert svc.next_attempt_at(svc.MAX_ATTEMPTS, NOW) is None


def test_retryable_statuses():
    assert svc.is_retryable_status(None)  # network error
    assert svc.is_retryable_status(503)
    assert svc.is_retryable_status(429)
    assert not svc.is_retryable_status(400)
    assert not svc.is_retryable_status(401)


# ── change capture decision ──────────────────────────────────────────────────
def test_decide_event():
    base = NOW - timedelta(days=1)
    assert svc.decide_event(last_status=None, current_status="OPEN", created_at=NOW, baseline=base) == svc.EVENT_CREATED
    # pre-existing record: no silent backfill
    assert svc.decide_event(last_status=None, current_status="OPEN", created_at=base - timedelta(days=5), baseline=base) is None
    assert svc.decide_event(last_status="OPEN", current_status="CLOSED", created_at=NOW, baseline=base) == svc.EVENT_STATUS_CHANGED
    assert svc.decide_event(last_status="OPEN", current_status="OPEN", created_at=NOW, baseline=base) is None


def test_skip_reasons():
    ep = SimpleNamespace(isDeleted=False, enabled=True, dryRun=False, baseUrl="https://sas.example", path="/obs",
                         authType="NONE", secretEnvVar=None)
    assert svc.skip_reason(ep) is None
    assert "Dry run" in svc.skip_reason(SimpleNamespace(**{**ep.__dict__, "dryRun": True}))
    assert "awaiting JSL SAS" in svc.skip_reason(SimpleNamespace(**{**ep.__dict__, "baseUrl": None}))
    assert "disabled" in svc.skip_reason(SimpleNamespace(**{**ep.__dict__, "enabled": False}))
    assert svc.skip_reason(None)


def test_target_url_and_get_path():
    assert svc.target_url("https://a.b/", "/x/y") == "https://a.b/x/y"
    assert svc.target_url("https://a.b", None) == "https://a.b"
    assert svc.target_url("  ", "x") is None
    assert svc.get_path({"data": {"items": [{"id": 7}]}}, "data.items.0.id") == 7
    assert svc.get_path({"a": 1}, "b.c") is None
