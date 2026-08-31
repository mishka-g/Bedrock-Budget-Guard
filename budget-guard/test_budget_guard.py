"""Unit tests for budget-guard (no AWS required)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import enforce
import main
import metrics
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
    tracker.mark_seen("evt-9", ts_ms=42_000)
    tracker.thresholds_to_fire("alpha", 2.0, [0.5, 0.8])  # 1.25/2 = 62.5% → 0.5
    blocked = {"alpha"}
    assert persist.save_state(path, tracker, blocked, watermark_ms=42_000)

    payload = json.loads(path.read_text())
    assert "seen_event_ids" not in payload

    loaded = persist.load_state(path)
    t2 = SpendTracker()
    b2: set[str] = set()
    wm = persist.apply_state(t2, b2, loaded)
    assert wm == 42_000
    assert t2.get_spend("alpha") == 1.25
    # Seen IDs are in-memory only (overlap window); compact state omits them.
    assert not t2.already_seen("evt-9")
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


# --- slack alerts ---

def test_slack_configure_disabled_without_webhook(monkeypatch):
    import alert as alert_mod

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    alert_mod.configure({
        "alerts": {"slack": {"enabled": True, "webhook_url": "", "events": ["ALERT"]}},
    })
    assert alert_mod._slack_enabled is False


def test_slack_env_overrides_config_url(monkeypatch):
    import alert as alert_mod

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/env")
    alert_mod.configure({
        "alerts": {
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.example/cfg",
                "events": ["ALERT", "BLOCKED"],
            },
        },
    })
    assert alert_mod._slack_enabled is True
    assert alert_mod._slack_webhook_url == "https://hooks.example/env"
    assert alert_mod._slack_events == {"ALERT", "BLOCKED"}


def test_slack_posts_for_alert_not_status(monkeypatch):
    import alert as alert_mod

    posts: list[bytes] = []

    class FakeResp:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=5):
        posts.append(req.data)
        return FakeResp()

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(alert_mod.urllib.request, "urlopen", fake_urlopen)
    alert_mod.configure({
        "alerts": {
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.example/test",
                "events": ["ALERT", "BLOCKED", "UNBLOCKED"],
            },
        },
    })
    alert_mod.alert_threshold("alpha", 0.8, 1.6, 2.0)
    alert_mod.status_line({"alpha": {"budget_usd": 2.0}}, {"alpha": 1.6})
    assert len(posts) == 1
    assert b"ALERT" in posts[0]
    assert b"STATUS" not in posts[0]


def test_slack_failure_is_soft(monkeypatch, capsys):
    import alert as alert_mod
    import urllib.error

    def boom(req, timeout=5):
        raise urllib.error.URLError("down")

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(alert_mod.urllib.request, "urlopen", boom)
    alert_mod.configure({
        "alerts": {
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.example/test",
                "events": ["BLOCKED"],
            },
        },
    })
    alert_mod.blocked("alpha", ["proj-alpha-app"])
    err = capsys.readouterr().out
    assert "BLOCKED" in err
    assert "Slack webhook failed" in err


def test_slack_empty_events_list_mutes_all(monkeypatch):
    """An explicit `events: []` means mute, not 'use the default set'."""
    import alert as alert_mod

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    alert_mod.configure({
        "alerts": {
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.example/test",
                "events": [],
            },
        },
    })
    assert alert_mod._slack_events == set()


def test_slack_malformed_url_does_not_crash(monkeypatch, capsys):
    """A bad webhook URL (e.g. missing scheme) must not raise into the caller."""
    import alert as alert_mod

    class SyncThread:
        """Run the target inline so the test can assert deterministically."""

        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(alert_mod.threading, "Thread", SyncThread)
    alert_mod.configure({
        "alerts": {
            "slack": {
                "enabled": True,
                "webhook_url": "hooks.slack.com/services/T00/B00/XXX",  # no scheme
                "events": ["ALERT"],
            },
        },
    })
    alert_mod.alert_threshold("alpha", 0.8, 1.6, 2.0)  # must not raise
    out = capsys.readouterr().out
    assert "ALERT" in out
    assert "Slack webhook failed" in out


def test_slack_post_does_not_block_caller(monkeypatch):
    """A slow/hanging webhook must not delay the enforcement loop."""
    import alert as alert_mod
    import threading as real_threading

    release = real_threading.Event()

    class FakeResp:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def slow_urlopen(req, timeout=5):
        release.wait(2)
        return FakeResp()

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(alert_mod.urllib.request, "urlopen", slow_urlopen)
    alert_mod.configure({
        "alerts": {
            "slack": {
                "enabled": True,
                "webhook_url": "https://hooks.example/test",
                "events": ["ALERT"],
            },
        },
    })
    start = time.monotonic()
    alert_mod.alert_threshold("alpha", 0.8, 1.6, 2.0)
    elapsed = time.monotonic() - start
    release.set()
    assert elapsed < 1.0


# --- overlap-window dedup / compact state ---

def test_prune_seen_drops_ids_older_than_window():
    t = SpendTracker()
    t.mark_seen("old", ts_ms=1000)
    t.mark_seen("new", ts_ms=9000)
    t.prune_seen(keep_after_ms=5000)
    assert not t.already_seen("old")
    assert t.already_seen("new")


def test_fetch_start_ms_uses_floor_when_seen_empty():
    assert main.fetch_start_ms(10_000, fetch_floor_ms=10_001, seen_event_ids={}) == 10_001


def test_fetch_start_ms_clamps_overlap_to_floor():
    seen = {"e1": 12_000}
    # watermark 12000, overlap 5000 → 7000, but floor is 10001
    start = main.fetch_start_ms(
        12_000, fetch_floor_ms=10_001, seen_event_ids=seen, overlap_ms=5_000,
    )
    assert start == 10_001


def test_fetch_start_ms_overlap_after_floor():
    seen = {"e1": 20_000}
    start = main.fetch_start_ms(
        20_000, fetch_floor_ms=10_001, seen_event_ids=seen, overlap_ms=5_000,
    )
    assert start == 15_000


def test_unknown_model_warns_once_per_day(capsys):
    tracker = SpendTracker()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "pricing_per_million_usd": PRICING,
    }
    role_map = {"proj-alpha-app": "alpha"}
    raw = [
        _invocation_event(event_id="u1", role="proj-alpha-app", model_id="nope.model"),
        _invocation_event(event_id="u2", role="proj-alpha-app", model_id="nope.model"),
    ]
    assert main.process_events(raw, cfg, tracker, role_map) == 0
    out = capsys.readouterr().out
    assert out.count("Unknown modelId") == 1


# --- fail-open fetch ---

def test_fetch_log_events_failure_returns_not_ok():
    from botocore.exceptions import ClientError

    logs = MagicMock()
    logs.filter_log_events.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow"}},
        "FilterLogEvents",
    )
    events, ok = main.fetch_log_events(logs, "/g", 0, page_cap=None)
    assert events == []
    assert ok is False


def test_fetch_log_events_respects_ministack_page_cap():
    logs = MagicMock()
    logs.filter_log_events.return_value = {
        "events": [{"timestamp": 1}] * 1000,
        "nextToken": "more",
    }
    events, ok = main.fetch_log_events(logs, "/g", 0, page_cap=1000)
    assert ok is True
    assert len(events) == 1000
    assert logs.filter_log_events.call_count == 1


def test_fetch_log_events_paginates_without_cap():
    logs = MagicMock()

    def pages(**kwargs):
        if kwargs.get("nextToken") == "t":
            return {"events": [{"timestamp": 2}], "nextToken": None}
        return {"events": [{"timestamp": 1}] * 1000, "nextToken": "t"}

    logs.filter_log_events.side_effect = pages
    events, ok = main.fetch_log_events(logs, "/g", 0, page_cap=None)
    assert ok is True
    assert len(events) == 1001
    assert logs.filter_log_events.call_count == 2


# --- leader fence ---

def test_reconcile_fence_skips_iam_when_not_leader():
    iam = MagicMock()
    tracker = SpendTracker()
    tracker.add_spend("alpha", 5.0)
    blocked: set[str] = set()
    cfg = {
        "projects": {"alpha": {"budget_usd": 2.0, "enforce": True}},
        "alert_thresholds": [1.0],
    }
    role_map = {"proj-alpha-app": "alpha"}
    main.reconcile_enforcement(
        iam, cfg, tracker, role_map, blocked, still_leader_fn=lambda: False,
    )
    iam.put_role_policy.assert_not_called()
    assert blocked == set()


# --- HTTP ---

def test_http_health_ready_status_metrics():
    import urllib.request

    import httpapi

    st = httpapi.RuntimeStatus()
    st.set_ready(True)
    st.set_leader(True)
    st.update_poll(
        day_key="2026-08-31",
        last_poll_ok=True,
        last_error=None,
        watermark_ms=1_000,
        projects={"alpha": {
            "spend_usd": 1.0, "budget_usd": 2.0, "ratio": 0.5,
            "blocked": False, "enforce": True,
        }},
    )
    httpd = httpapi.start_http_server(0, st)
    port = httpd.server_address[1]
    try:
        health = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3)
        assert health.read() == b"ok\n"
        ready = urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3)
        assert ready.status == 200
        status = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3).read(),
        )
        assert status["leader"] is True
        assert status["last_poll_ok"] is True
        assert status["projects"]["alpha"]["spend_usd"] == 1.0
        metrics_body = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/metrics", timeout=3,
        ).read().decode()
        assert "budget_guard_is_leader" in metrics_body
    finally:
        httpd.shutdown()


def test_http_readyz_503_when_not_ready():
    import urllib.error
    import urllib.request

    import httpapi

    st = httpapi.RuntimeStatus()
    st.set_ready(False)
    httpd = httpapi.start_http_server(0, st)
    port = httpd.server_address[1]
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3)
            raise AssertionError("expected HTTP 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
    finally:
        httpd.shutdown()


# --- ConfigMap state ---

def test_configmap_missing_is_fail_open_empty():
    from k8s_state import ConfigMapStateStore

    class Fake404(Exception):
        status = 404

    api = MagicMock()
    api.read_namespaced_config_map.side_effect = Fake404()
    store = ConfigMapStateStore("budget-guard-state", "default", api=api)
    loaded = store.load()
    assert loaded["spend_usd"] == {}
    assert loaded["watermark_ms"] is None
    assert loaded["day_key"] == utc_day_key()


def test_configmap_roundtrip():
    from k8s_state import ConfigMapStateStore, DATA_KEY

    tracker = SpendTracker()
    tracker.add_spend("alpha", 3.5)
    tracker.thresholds_to_fire("alpha", 10.0, [0.3])
    payload = persist.compact_snapshot(tracker, {"alpha"}, 99)

    cm = MagicMock()
    cm.data = {DATA_KEY: json.dumps(payload)}
    cm.metadata.resource_version = "7"
    api = MagicMock()
    api.read_namespaced_config_map.return_value = cm
    api.replace_namespaced_config_map.return_value = cm

    store = ConfigMapStateStore("budget-guard-state", "ns", api=api)
    loaded = store.load()
    t2 = SpendTracker()
    blocked: set[str] = set()
    wm = persist.apply_state(t2, blocked, loaded)
    assert wm == 99
    assert t2.get_spend("alpha") == 3.5
    assert blocked == {"alpha"}

    tracker.add_spend("alpha", 0.5)
    assert store.save(tracker, blocked, 100) is True
    api.replace_namespaced_config_map.assert_called()
    written = api.replace_namespaced_config_map.call_args[0][2]
    saved = json.loads(written.data[DATA_KEY])
    assert "seen_event_ids" not in saved
    assert saved["watermark_ms"] == 100


# --- leader election flag ---

def test_election_enabled_env_and_incluster(monkeypatch):
    import leader as leader_mod

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("BUDGET_GUARD_LEADER_ELECTION", "true")
    assert leader_mod.election_enabled() is True

    monkeypatch.setenv("BUDGET_GUARD_LEADER_ELECTION", "false")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert leader_mod.election_enabled() is False

    monkeypatch.delenv("BUDGET_GUARD_LEADER_ELECTION", raising=False)
    assert leader_mod.election_enabled() is True

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert leader_mod.election_enabled() is False


def test_lease_is_expired():
    from datetime import datetime, timedelta, timezone

    import leader as leader_mod

    lease = MagicMock()
    lease.spec.renew_time = datetime.now(timezone.utc) - timedelta(seconds=20)
    assert leader_mod.lease_is_expired(lease, 15) is True
    lease.spec.renew_time = datetime.now(timezone.utc)
    assert leader_mod.lease_is_expired(lease, 15) is False
    lease.spec.renew_time = None
    assert leader_mod.lease_is_expired(lease, 15) is True


def test_lease_acquire_creates_when_missing():
    import leader as leader_mod

    class Fake404(Exception):
        status = 404

    api = MagicMock()
    api.read_namespaced_lease.side_effect = Fake404()
    api.create_namespaced_lease.return_value = MagicMock()
    el = leader_mod.LeaseElector("budget-guard", "ns", "pod-a", api=api)
    assert el.try_acquire_or_renew() is True
    api.create_namespaced_lease.assert_called()


def test_lease_held_by_other_is_not_stolen():
    from datetime import datetime, timezone

    import leader as leader_mod

    lease = MagicMock()
    lease.spec.holder_identity = "other-pod"
    lease.spec.renew_time = datetime.now(timezone.utc)
    lease.spec.lease_transitions = 0
    api = MagicMock()
    api.read_namespaced_lease.return_value = lease
    el = leader_mod.LeaseElector("budget-guard", "ns", "pod-a", api=api)
    assert el.try_acquire_or_renew() is False
    api.replace_namespaced_lease.assert_not_called()


def test_role_map_cache_ttl(monkeypatch):
    iam = MagicMock()
    calls = {"n": 0}

    def fake_load(_iam):
        calls["n"] += 1
        return {"r": "p"}

    monkeypatch.setattr(roles, "load_role_project_map", fake_load)
    cache = main.RoleMapCache(ttl_s=60)
    monotonic = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: monotonic["t"])
    assert cache.get(iam) == {"r": "p"}
    assert cache.get(iam) == {"r": "p"}
    assert calls["n"] == 1
    monotonic["t"] = 1061.0
    cache.get(iam)
    assert calls["n"] == 2
    cache.get(iam, force=True)
    assert calls["n"] == 3


def test_observe_poll_emits_prometheus_gauges():
    from prometheus_client import generate_latest

    tracker = SpendTracker()
    tracker.add_spend("alpha", 1.25)
    now = int(time.time() * 1000)
    metrics.observe_poll(
        cfg={"projects": {"alpha": {"budget_usd": 2.0, "enforce": True}}},
        tracker=tracker,
        blocked_projects=set(),
        watermark_ms=now - 5_000,
        applied=4,
        duration_s=0.05,
        fetch_ok=True,
        now_ms=now,
    )
    body = generate_latest().decode()
    assert "budget_guard_spend_usd" in body
    assert "budget_guard_events_applied" in body
