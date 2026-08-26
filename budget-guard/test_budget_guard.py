"""Unit tests for budget-guard (no AWS required)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import enforce
import main
import roles
import state as persist
from cost import event_cost_usd
from tracker import SpendTracker, parse_event_timestamp_ms, utc_day_key


# --- cost ---

HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
PRICING = {
    HAIKU: {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.1,
        "cache_write": 1.25,
    },
}


def test_cost_includes_cache_tokens():
    msg = {
        "modelId": HAIKU,
        "input": {
            "inputTokenCount": 1_000_000,
            "cacheReadInputTokenCount": 1_000_000,
            "cacheWriteInputTokenCount": 1_000_000,
        },
        "output": {"outputTokenCount": 1_000_000},
    }
    # 1.0 + 5.0 + 0.1 + 1.25 = 7.35
    assert event_cost_usd(msg, PRICING) == 7.35


def test_cost_unknown_model_returns_none():
    msg = {
        "modelId": "unknown.model",
        "input": {"inputTokenCount": 100},
        "output": {"outputTokenCount": 10},
    }
    assert event_cost_usd(msg, PRICING) is None


def test_cost_missing_cache_defaults_zero():
    msg = {
        "modelId": HAIKU,
        "input": {"inputTokenCount": 1_000_000},
        "output": {"outputTokenCount": 0},
    }
    assert event_cost_usd(msg, PRICING) == 1.0


# --- ARN parse ---

def test_role_from_assumed_role_arn():
    arn = "arn:aws:sts::000000000000:assumed-role/proj-alpha-app/session-1"
    assert roles.role_name_from_arn(arn) == "proj-alpha-app"


def test_role_from_iam_role_arn():
    arn = "arn:aws:iam::000000000000:role/proj-beta-app"
    assert roles.role_name_from_arn(arn) == "proj-beta-app"


def test_role_from_bad_arn():
    assert roles.role_name_from_arn(None) is None
    assert roles.role_name_from_arn("not-an-arn") is None


# --- tracker / dedup / thresholds / day ---

def test_event_id_dedup():
    t = SpendTracker()
    assert not t.already_seen("e1")
    t.mark_seen("e1")
    assert t.already_seen("e1")
    t.add_spend("alpha", 1.0)
    assert t.get_spend("alpha") == 1.0


def test_dedup_key_fallback_without_event_id():
    ev = {
        "logStreamName": "bedrock-simulator",
        "timestamp": 123,
        "message": '{"modelId":"x"}',
    }
    key = main._dedup_key(ev)
    assert "bedrock-simulator" in key
    assert main._dedup_key(ev) == key
    assert main._dedup_key({**ev, "eventId": "real-id"}) == "real-id"


def test_threshold_fires_once():
    t = SpendTracker()
    t.add_spend("alpha", 1.6)  # 80% of $2
    first = t.thresholds_to_fire("alpha", 2.0, [0.8, 1.0])
    assert first == [0.8]
    second = t.thresholds_to_fire("alpha", 2.0, [0.8, 1.0])
    assert second == []
    t.add_spend("alpha", 0.5)  # now over 100%
    third = t.thresholds_to_fire("alpha", 2.0, [0.8, 1.0])
    assert third == [1.0]


def test_day_boundary_reset_clears_state():
    t = SpendTracker()
    t.add_spend("alpha", 9.0)
    t.mark_seen("evt-1")
    t.thresholds_to_fire("alpha", 2.0, [0.8, 1.0])
    t.mark_unconfigured_warned("gamma")
    t.reset_for_new_day("2099-01-01")
    assert t.day_key == "2099-01-01"
    assert t.get_spend("alpha") == 0.0
    assert not t.already_seen("evt-1")
    assert t.thresholds_to_fire("alpha", 2.0, [0.8]) == []
    assert t.mark_unconfigured_warned("gamma") is True


def test_parse_iso_timestamp():
    ms = parse_event_timestamp_ms("2026-07-13T09:15:04Z")
    assert ms is not None
    assert utc_day_key.__module__
    assert ms > 0


def test_deny_policy_name_constant():
    assert enforce.DENY_POLICY_NAME == "BudgetGuardDenyBedrock"
    assert any(
        a.startswith("bedrock:") for a in enforce.DENY_POLICY_DOC["Statement"][0]["Action"]
    )


# --- watermark ---

def test_advance_watermark_ignores_events_without_timestamp():
    assert main.advance_watermark(1000, [{"message": "x"}]) == 1000
    assert main.advance_watermark(1000, []) == 1000


def test_advance_watermark_takes_newest():
    raw = [{"timestamp": 50}, {"timestamp": 200}, {"timestamp": 150}]
    assert main.advance_watermark(100, raw) == 200
    assert main.advance_watermark(500, raw) == 500


# --- process_events ---

def _invocation_event(
    *,
    event_id: str,
    role: str,
    model_id: str = HAIKU,
    input_tokens: int = 1_000_000,
    output_tokens: int = 0,
    ts_ms: int | None = None,
) -> dict:
    if ts_ms is None:
        # "now" in UTC ms so _event_in_today_utc passes
        from datetime import datetime, timezone
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    message = {
        "modelId": model_id,
        "identity": {
            "arn": f"arn:aws:sts::000000000000:assumed-role/{role}/sess",
        },
        "input": {"inputTokenCount": input_tokens},
        "output": {"outputTokenCount": output_tokens},
        "timestamp": "2026-08-26T12:00:00Z",
    }
    return {
        "eventId": event_id,
        "timestamp": ts_ms,
        "message": json.dumps(message),
    }


def test_process_events_accumulates_spend_for_configured_project():
    tracker = SpendTracker()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "pricing_per_million_usd": PRICING,
    }
    role_map = {"proj-alpha-app": "alpha"}
    raw = [_invocation_event(event_id="e1", role="proj-alpha-app")]
    applied = main.process_events(raw, cfg, tracker, role_map)
    assert applied == 1
    assert tracker.get_spend("alpha") == 1.0
    assert tracker.already_seen("e1")


def test_process_events_dedups_by_event_id():
    tracker = SpendTracker()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "pricing_per_million_usd": PRICING,
    }
    role_map = {"proj-alpha-app": "alpha"}
    raw = [
        _invocation_event(event_id="e1", role="proj-alpha-app"),
        _invocation_event(event_id="e1", role="proj-alpha-app"),
    ]
    assert main.process_events(raw, cfg, tracker, role_map) == 1
    assert tracker.get_spend("alpha") == 1.0


def test_process_events_tracks_unconfigured_without_enforce_candidate():
    tracker = SpendTracker()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "pricing_per_million_usd": PRICING,
    }
    role_map = {"proj-gamma-app": "gamma"}
    raw = [_invocation_event(event_id="g1", role="proj-gamma-app")]
    assert main.process_events(raw, cfg, tracker, role_map) == 1
    assert tracker.get_spend("gamma") == 1.0
    assert "gamma" not in cfg["projects"]


# --- reconcile_enforcement (IAM mocks) ---

def test_reconcile_blocks_when_over_budget_and_put_succeeds():
    iam = MagicMock()
    iam.put_role_policy.return_value = {}
    tracker = SpendTracker()
    tracker.add_spend("alpha", 2.5)
    blocked: set[str] = set()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "alert_thresholds": [0.8, 1.0],
    }
    role_map = {"proj-alpha-app": "alpha", "proj-alpha-batch": "alpha"}

    main.reconcile_enforcement(iam, cfg, tracker, role_map, blocked)

    assert blocked == {"alpha"}
    assert iam.put_role_policy.call_count == 2


def test_reconcile_does_not_mark_blocked_when_put_fails():
    iam = MagicMock()
    iam.put_role_policy.side_effect = RuntimeError("IAM down")
    tracker = SpendTracker()
    tracker.add_spend("alpha", 5.0)
    blocked: set[str] = set()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "alert_thresholds": [1.0],
    }
    role_map = {"proj-alpha-app": "alpha"}

    main.reconcile_enforcement(iam, cfg, tracker, role_map, blocked)

    assert blocked == set()


def test_reconcile_enforce_false_lifts_deny():
    iam = MagicMock()
    iam.delete_role_policy.return_value = {}
    tracker = SpendTracker()
    tracker.add_spend("alpha", 9.0)
    blocked = {"alpha"}
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": False}},
        "alert_thresholds": [1.0],
    }
    role_map = {"proj-alpha-app": "alpha"}

    main.reconcile_enforcement(iam, cfg, tracker, role_map, blocked)

    assert blocked == set()
    iam.delete_role_policy.assert_called()


def test_reconcile_raises_budget_lifts_when_previously_blocked():
    iam = MagicMock()
    iam.delete_role_policy.return_value = {}
    tracker = SpendTracker()
    tracker.add_spend("alpha", 1.0)
    blocked = {"alpha"}
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "alert_thresholds": [1.0],
    }
    role_map = {"proj-alpha-app": "alpha"}

    main.reconcile_enforcement(iam, cfg, tracker, role_map, blocked)

    assert blocked == set()
    iam.delete_role_policy.assert_called()
    iam.put_role_policy.assert_not_called()


# --- persistence ---

def test_state_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    tracker = SpendTracker()
    tracker.add_spend("alpha", 1.25)
    tracker.mark_seen("evt-9")
    tracker.thresholds_to_fire("alpha", 2.0, [0.5, 0.8])  # 1.25/2 = 62.5% → 0.5
    blocked = {"alpha"}
    persist.save_state(path, tracker, blocked, watermark_ms=42_000)

    loaded = persist.load_state(path)
    t2 = SpendTracker()
    b2: set[str] = set()
    wm = persist.apply_state(t2, b2, loaded)
    assert wm == 42_000
    assert t2.get_spend("alpha") == 1.25
    assert t2.already_seen("evt-9")
    assert b2 == {"alpha"}
    assert 0.5 in t2.fired_thresholds["alpha"]


def test_state_wrong_day_starts_fresh(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "day_key": "1999-01-01",
            "spend_usd": {"alpha": 99.0},
            "seen_event_ids": ["old"],
            "fired_thresholds": {},
            "warned_unconfigured": [],
            "blocked_projects": ["alpha"],
            "watermark_ms": 1,
        }),
    )
    loaded = persist.load_state(path)
    assert loaded["spend_usd"] == {}
    assert loaded["blocked_projects"] == []
    assert loaded["day_key"] == utc_day_key()


def test_discover_blocked_projects_from_iam():
    iam = MagicMock()

    def get_policy(RoleName, PolicyName):
        if RoleName == "proj-alpha-app" and PolicyName == enforce.DENY_POLICY_NAME:
            return {"PolicyDocument": "{}"}
        raise type("Err", (Exception,), {"response": {"Error": {"Code": "NoSuchEntity"}}})()

    iam.get_role_policy.side_effect = get_policy
    role_map = {
        "proj-alpha-app": "alpha",
        "proj-beta-app": "beta",
    }
    assert enforce.discover_blocked_projects(iam, role_map) == {"alpha"}
