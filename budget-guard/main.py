"""Bedrock Budget Guard — poll loop.

Every poll_interval_seconds:
  1. Hot-reload config.yaml (fail soft on parse errors)
  2. UTC day roll → zero spend, clear dedup/fired, lift all managed Denys
  3. Refresh IAM role → project map (cached)
  4. FilterLogEvents from watermark (incremental; ministack 1000-cap)
  5. Dedup by eventId; price events; accumulate spend
  6. Alert thresholds once; enforce or lift Deny per project
  7. Persist compact spend / watermark / blocked set
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import yaml
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import alert
import cost
import enforce
import httpapi
import leader
import metrics
import roles
import state as persist
from tracker import SpendTracker, parse_event_timestamp_ms, utc_day_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("budget-guard")

CONFIG_PATH = Path(os.environ.get("BUDGET_GUARD_CONFIG", "/app/config.yaml"))
STATE_PATH = persist.DEFAULT_STATE_PATH
# Overlap window so we do not miss events at the watermark boundary.
WATERMARK_OVERLAP_MS = 5_000
ROLE_MAP_TTL_S = 60.0
HTTP_PORT = int(os.environ.get("BUDGET_GUARD_HTTP_PORT", "8080"))

STATUS = httpapi.RuntimeStatus()
_shutdown = threading.Event()
_loop_stop = threading.Event()
_leader = threading.Event()


def still_leader() -> bool:
    return (
        _leader.is_set()
        and not _loop_stop.is_set()
        and not _shutdown.is_set()
    )


class _SaveCtx:
    store: persist.StateStore | None = None
    tracker: SpendTracker | None = None
    blocked: set[str] | None = None
    watermark_ms: int = 0


_save_ctx = _SaveCtx()


class RoleMapCache:
    """Refresh IAM role → project tags at most once per ROLE_MAP_TTL_S."""

    def __init__(self, ttl_s: float = ROLE_MAP_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._map: dict[str, str] = {}
        self._loaded_at = 0.0

    def get(self, iam, *, force: bool = False) -> dict[str, str]:
        now = time.monotonic()
        if force or (now - self._loaded_at) >= self.ttl_s:
            self._map = roles.load_role_project_map(iam)
            self._loaded_at = now
        return self._map


def _boto(service: str):
    # Local demo sets AWS_ENDPOINT_URL (ministack). Real AWS: leave it unset.
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or None
    return boto3.client(
        service,
        endpoint_url=endpoint,
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )


def load_config(path: Path) -> dict[str, Any] | None:
    """Load and lightly validate config.yaml. Returns None on failure."""
    try:
        with path.open() as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config root must be a mapping")
        if "projects" not in cfg or not isinstance(cfg["projects"], dict):
            raise ValueError("projects must be a mapping")
        if "pricing_per_million_usd" not in cfg:
            raise ValueError("pricing_per_million_usd required")
        cfg.setdefault("poll_interval_seconds", 15)
        cfg.setdefault("log_group", "/aws/bedrock/modelinvocations")
        cfg.setdefault("alert_thresholds", [0.8, 1.0])
        alert.configure(cfg)
        return cfg
    except Exception as exc:
        logger.warning("Failed to load config from %s: %s", path, exc)
        return None


def _event_in_today_utc(ts_ms: int, day_key: str) -> bool:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return utc_day_key(dt) == day_key


def _ministack_page_cap() -> int | None:
    """Ministack FilterLogEvents is capped at 1000; real AWS paginates fully."""
    if (os.environ.get("AWS_ENDPOINT_URL") or "").strip():
        return 1000
    return None


def fetch_log_events(
    logs,
    log_group: str,
    start_ms: int,
    page_cap: int | None = None,
) -> tuple[list[dict], bool]:
    """Pull FilterLogEvents from start_ms.

    Returns (events, ok). ok=False means the fetch failed — caller must
    fail-open (keep last spend, do not advance watermark).
    """
    if page_cap is None:
        page_cap = _ministack_page_cap()
    events: list[dict] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": start_ms,
            "limit": 1000,
        }
        if token:
            kwargs["nextToken"] = token
        try:
            resp = logs.filter_log_events(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("FilterLogEvents failed: %s", exc)
            return [], False
        batch = resp.get("events") or []
        events.extend(batch)
        token = resp.get("nextToken")
        if not token or not batch:
            break
        if page_cap is not None and len(events) >= page_cap:
            break
    return events, True


def _dedup_key(ev: dict) -> str:
    """CloudWatch eventId, or a stable fallback when ministack omits it."""
    event_id = ev.get("eventId")
    if event_id:
        return str(event_id)
    # ministack 1.4.1 often returns no eventId; synthesize from stream+ts+body.
    return (
        f"{ev.get('logStreamName', '')}|"
        f"{ev.get('timestamp', '')}|"
        f"{ev.get('message', '')}"
    )


def process_events(
    raw_events: list[dict],
    cfg: dict[str, Any],
    tracker: SpendTracker,
    role_map: dict[str, str],
) -> int:
    """Apply new (deduped) events to the tracker. Returns count applied."""
    pricing = cfg.get("pricing_per_million_usd") or {}
    projects_cfg = cfg.get("projects") or {}
    applied = 0

    for ev in raw_events:
        event_id = _dedup_key(ev)
        if tracker.already_seen(event_id):
            continue

        # Prefer CloudWatch event timestamp (ms); fall back to message body.
        ts_ms = ev.get("timestamp")
        if not isinstance(ts_ms, (int, float)):
            ts_ms = None

        try:
            message = json.loads(ev.get("message") or "")
        except (json.JSONDecodeError, TypeError):
            tracker.mark_seen(event_id, int(ts_ms or 0))
            continue
        if not isinstance(message, dict):
            tracker.mark_seen(event_id, int(ts_ms or 0))
            continue

        if ts_ms is None:
            ts_ms = parse_event_timestamp_ms(message.get("timestamp"))
        else:
            ts_ms = int(ts_ms)

        if ts_ms is None or not _event_in_today_utc(ts_ms, tracker.day_key):
            tracker.mark_seen(event_id, int(ts_ms or 0))
            continue

        role_name, project = roles.project_for_message(message, role_map)
        if not role_name:
            tracker.mark_seen(event_id, ts_ms)
            continue

        if project is None:
            # Role has no project tag — skip quietly.
            tracker.mark_seen(event_id, ts_ms)
            continue

        usd = cost.event_cost_usd(message, pricing)
        tracker.mark_seen(event_id, ts_ms)
        if usd is None:
            mid = message.get("modelId")
            if isinstance(mid, str) and mid:
                metrics.unpriced_inc(mid)
                if tracker.mark_unknown_model_warned(mid):
                    alert.warn(
                        f"Unknown modelId {mid!r} — no pricing in config; skipping cost",
                    )
            continue

        if project not in projects_cfg:
            if tracker.mark_unconfigured_warned(project):
                alert.warn(
                    f"Project {project} has usage but no budget in config — not enforcing",
                )
            # Still track spend so STATUS can show it if we add it later;
            # do not enforce (enforcement loop only considers configured projects).
            tracker.add_spend(project, usd)
            applied += 1
            continue

        tracker.add_spend(project, usd)
        applied += 1

    return applied


def reconcile_enforcement(
    iam,
    cfg: dict[str, Any],
    tracker: SpendTracker,
    role_map: dict[str, str],
    blocked_projects: set[str],
    still_leader_fn: Callable[[], bool] | None = None,
) -> None:
    """Alert on new thresholds; put or lift Deny for each configured project."""
    if still_leader_fn is None:
        still_leader_fn = lambda: True  # noqa: E731
    if not still_leader_fn():
        return

    projects_cfg = cfg.get("projects") or {}
    thresholds = [float(t) for t in (cfg.get("alert_thresholds") or [0.8, 1.0])]

    for project, pcfg in sorted(projects_cfg.items()):
        if not still_leader_fn():
            return
        if not isinstance(pcfg, dict):
            continue
        budget = float(pcfg.get("budget_usd") or 0)
        enforce_flag = bool(pcfg.get("enforce", True))
        spend = tracker.get_spend(project)
        role_names = roles.roles_for_project(role_map, project)

        for t in tracker.thresholds_to_fire(project, budget, thresholds):
            alert.alert_threshold(project, t, spend, budget)

        should_block = enforce_flag and budget > 0 and spend >= budget

        if should_block:
            if not still_leader_fn():
                return
            # Idempotent refresh also covers roles that appear mid-day.
            denied = enforce.block_project_roles(iam, role_names)
            failed = [n for n in role_names if n not in denied]
            if failed:
                metrics.iam_put_failures(len(failed))
                alert.warn(
                    f"Failed to attach Deny for {project} on roles: "
                    f"{', '.join(failed)}",
                )
            if denied:
                if project not in blocked_projects:
                    blocked_projects.add(project)
                    alert.blocked(project, denied)
            elif role_names:
                # Do not claim BLOCKED or remember the project as blocked
                # when every put_role_policy failed — retry next poll.
                alert.warn(
                    f"Could not block {project}: no Deny attached "
                    f"(will retry)",
                )
        elif not enforce_flag:
            if not still_leader_fn():
                return
            # Ops same-day unblock: always try to lift our Deny, even if we
            # did not track this project as blocked (e.g. after a restart).
            enforce.unblock_project_roles(iam, role_names)
            if project in blocked_projects:
                blocked_projects.discard(project)
                alert.unblocked(project, "enforce set to false in config")
        elif project in blocked_projects:
            if not still_leader_fn():
                return
            # Lift when spend/budget no longer warrants a block (e.g. raised
            # budget). Persisted spend + Deny discovery keep this accurate
            # across restarts.
            enforce.unblock_project_roles(iam, role_names)
            blocked_projects.discard(project)
            alert.unblocked(project, "budget raised or spend below budget")


def lift_all_managed_denys(iam, role_map: dict[str, str], blocked_projects: set[str]) -> None:
    """UTC day reset: remove our Deny from every known role."""
    for role_name in sorted(role_map.keys()):
        enforce.delete_deny(iam, role_name)
    for project in sorted(blocked_projects):
        alert.unblocked(project, "UTC day reset")
    blocked_projects.clear()


def advance_watermark(watermark_ms: int, raw_events: list[dict]) -> int:
    """Advance watermark to newest event timestamp; no-op if none present."""
    stamps = [int(e["timestamp"]) for e in raw_events if "timestamp" in e]
    if not stamps:
        return watermark_ms
    return max(watermark_ms, max(stamps))


def fetch_start_ms(
    watermark_ms: int,
    fetch_floor_ms: int,
    seen_event_ids: dict[str, int],
    overlap_ms: int = WATERMARK_OVERLAP_MS,
) -> int:
    """FilterLogEvents startTime, clamped so we never re-price pre-restore events.

    Within a process, overlap covers CloudWatch boundary duplicates (deduped
    by in-memory seen IDs). After failover the seen set is empty, so we start
    at fetch_floor_ms (restored watermark + 1, or now−60s on a fresh start).
    """
    if seen_event_ids:
        return max(fetch_floor_ms, watermark_ms - overlap_ms)
    return fetch_floor_ms


def _persist(
    store: persist.StateStore,
    tracker: SpendTracker,
    blocked_projects: set[str],
    watermark_ms: int,
) -> None:
    _save_ctx.store = store
    _save_ctx.tracker = tracker
    _save_ctx.blocked = blocked_projects
    _save_ctx.watermark_ms = watermark_ms
    if not store.save(tracker, blocked_projects, watermark_ms):
        metrics.state_save_error()


def run_loop(
    stop_event: threading.Event | None = None,
    still_leader_fn: Callable[[], bool] | None = None,
    store: persist.StateStore | None = None,
) -> None:
    if stop_event is None:
        stop_event = _loop_stop
    if still_leader_fn is None:
        still_leader_fn = still_leader
    if store is None:
        store = persist.get_store()

    logs = _boto("logs")
    iam = _boto("iam")
    role_cache = RoleMapCache()

    cfg = load_config(CONFIG_PATH)
    if cfg is None:
        alert.configure(None)
        alert.warn("No usable config on startup; retrying each poll")
        cfg = {
            "poll_interval_seconds": 15,
            "log_group": "/aws/bedrock/modelinvocations",
            "alert_thresholds": [0.8, 1.0],
            "projects": {},
            "pricing_per_million_usd": {},
        }

    tracker = SpendTracker()
    blocked_projects: set[str] = set()
    loaded = store.load()
    restored_wm = persist.apply_state(tracker, blocked_projects, loaded)
    # Start slightly in the past so we catch early generator events.
    watermark_ms = restored_wm if restored_wm is not None else (
        int(time.time() * 1000) - 60_000
    )
    # Never FilterLogEvents before this: avoids double-counting spend that
    # was already restored (seen IDs are not persisted).
    fetch_floor_ms = (restored_wm + 1) if restored_wm is not None else watermark_ms

    # Safety net if state file is empty/missing but Denys linger in IAM.
    role_map = role_cache.get(iam, force=True)
    discovered = enforce.discover_blocked_projects(iam, role_map)
    newly = discovered - blocked_projects
    if newly:
        alert.info(
            "Recovered blocked projects from IAM Deny discovery: "
            + ", ".join(sorted(newly)),
        )
        blocked_projects.update(discovered)

    alert.info(
        f"budget-guard started; config={CONFIG_PATH} "
        f"day={tracker.day_key} poll={cfg.get('poll_interval_seconds')}s "
        f"blocked={sorted(blocked_projects) or '[]'}",
    )
    STATUS.set_ready(True)
    _persist(store, tracker, blocked_projects, watermark_ms)

    while not stop_event.is_set():
        poll_t0 = time.monotonic()
        fetch_ok = True
        last_error: str | None = None
        applied = 0

        new_cfg = load_config(CONFIG_PATH)
        if new_cfg is not None:
            cfg = new_cfg
        else:
            alert.warn("Keeping last good config after reload failure")

        if tracker.maybe_roll_day():
            alert.info(f"UTC day rolled to {tracker.day_key}; resetting spend and Denys")
            role_map = role_cache.get(iam, force=True)
            if still_leader_fn():
                lift_all_managed_denys(iam, role_map, blocked_projects)
            watermark_ms = int(time.time() * 1000)
            fetch_floor_ms = watermark_ms

        role_map = role_cache.get(iam)

        start_ms = fetch_start_ms(watermark_ms, fetch_floor_ms, tracker.seen_event_ids)
        raw, fetch_ok = fetch_log_events(
            logs, cfg.get("log_group", "/aws/bedrock/modelinvocations"), start_ms,
        )
        if not fetch_ok:
            metrics.log_fetch_error()
            last_error = "FilterLogEvents failed"
            # Fail-open: keep last spend, do not advance watermark.
            raw = []
        else:
            applied = process_events(raw, cfg, tracker, role_map)
            watermark_ms = advance_watermark(watermark_ms, raw)
            tracker.prune_seen(max(0, watermark_ms - WATERMARK_OVERLAP_MS))

        if still_leader_fn():
            reconcile_enforcement(
                iam, cfg, tracker, role_map, blocked_projects,
                still_leader_fn=still_leader_fn,
            )
        alert.status_line(cfg.get("projects") or {}, tracker.spend_usd)
        _persist(store, tracker, blocked_projects, watermark_ms)

        now_ms = int(time.time() * 1000)
        metrics.observe_poll(
            cfg=cfg,
            tracker=tracker,
            blocked_projects=blocked_projects,
            watermark_ms=watermark_ms,
            applied=applied,
            duration_s=time.monotonic() - poll_t0,
            fetch_ok=fetch_ok,
            now_ms=now_ms,
        )
        STATUS.update_poll(
            day_key=tracker.day_key,
            last_poll_ok=fetch_ok,
            last_error=last_error,
            watermark_ms=watermark_ms,
            projects=httpapi.project_status_map(
                cfg.get("projects") or {}, tracker.spend_usd, blocked_projects,
            ),
        )

        if applied:
            logger.debug("Applied %d new billed events this poll", applied)

        interval = float(cfg.get("poll_interval_seconds") or 15)
        # Wake early on lease loss or SIGTERM (stop_event may be _loop_stop).
        deadline = time.monotonic() + max(1.0, interval)
        while time.monotonic() < deadline:
            if stop_event.is_set() or _shutdown.is_set():
                break
            stop_event.wait(timeout=0.25)


def _flush_state() -> None:
    if _save_ctx.store is None or _save_ctx.tracker is None or _save_ctx.blocked is None:
        return
    _save_ctx.store.save(_save_ctx.tracker, _save_ctx.blocked, _save_ctx.watermark_ms)


def _handle_signal(signum: int, _frame: Any) -> None:
    logger.info("Received signal %s; shutting down", signum)
    _shutdown.set()
    _loop_stop.set()
    _flush_state()
    sys.exit(0)


def _run_as_leader() -> None:
    if not _shutdown.is_set():
        _loop_stop.clear()
    _leader.set()
    metrics.set_leader(True)
    STATUS.set_leader(True)
    try:
        run_loop(stop_event=_loop_stop, still_leader_fn=still_leader)
    finally:
        _leader.clear()
        metrics.set_leader(False)
        STATUS.set_leader(False)
        _flush_state()


def _on_stopped_leading() -> None:
    logger.info("Lost leader lease; stopping poll loop")
    _leader.clear()
    metrics.set_leader(False)
    STATUS.set_leader(False)
    _loop_stop.set()
    _flush_state()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    httpapi.start_http_server(HTTP_PORT, STATUS)
    STATUS.set_ready(True)
    metrics.set_leader(False)
    STATUS.set_leader(False)

    if leader.election_enabled():
        alert.info("Leader election enabled; serving HTTP as standby until elected")
        while not _shutdown.is_set():
            _leader.clear()
            metrics.set_leader(False)
            STATUS.set_leader(False)
            try:
                leader.run_election(_run_as_leader, _on_stopped_leading)
            except Exception as exc:
                logger.warning("Leader election error: %s; retrying", exc)
            if _shutdown.is_set():
                break
            time.sleep(2)
    else:
        _run_as_leader()


if __name__ == "__main__":
    main()
