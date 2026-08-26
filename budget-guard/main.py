"""Bedrock Budget Guard — poll loop.

Every poll_interval_seconds:
  1. Hot-reload config.yaml (fail soft on parse errors)
  2. UTC day roll → zero spend, clear dedup/fired, lift all managed Denys
  3. Refresh IAM role → project map
  4. FilterLogEvents from watermark (incremental; 1000-cap aware)
  5. Dedup by eventId; price events; accumulate spend
  6. Alert thresholds once; enforce or lift Deny per project
  7. Persist spend / watermark / blocked set to state.json
"""
from __future__ import annotations

import json
import logging
import os
import time
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
import roles
import state as persist
from tracker import SpendTracker, parse_event_timestamp_ms, utc_day_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("budget-guard")

CONFIG_PATH = Path(os.environ.get("BUDGET_GUARD_CONFIG", "/app/config.yaml"))
STATE_PATH = persist.DEFAULT_STATE_PATH
# Overlap window so we do not miss events at the watermark boundary.
WATERMARK_OVERLAP_MS = 5_000


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
        return cfg
    except Exception as exc:
        logger.warning("Failed to load config from %s: %s", path, exc)
        return None


def _event_in_today_utc(ts_ms: int, day_key: str) -> bool:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return utc_day_key(dt) == day_key


def fetch_log_events(logs, log_group: str, start_ms: int) -> list[dict]:
    """Pull FilterLogEvents from start_ms. Respects ministack 1000-cap.

    Does not re-scan the whole day — callers advance a watermark.
    """
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
            break
        batch = resp.get("events") or []
        events.extend(batch)
        token = resp.get("nextToken")
        if not token or not batch:
            break
        # Safety: if emulator ever paginates past 1000, stop after one full page
        # of incremental work; watermark advances on what we got.
        if len(events) >= 1000:
            break
    return events


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
            tracker.mark_seen(event_id)
            continue
        if not isinstance(message, dict):
            tracker.mark_seen(event_id)
            continue

        if ts_ms is None:
            ts_ms = parse_event_timestamp_ms(message.get("timestamp"))
        else:
            ts_ms = int(ts_ms)

        if ts_ms is None or not _event_in_today_utc(ts_ms, tracker.day_key):
            tracker.mark_seen(event_id)
            continue

        role_name, project = roles.project_for_message(message, role_map)
        if not role_name:
            tracker.mark_seen(event_id)
            continue

        if project is None:
            # Role has no project tag — skip quietly.
            tracker.mark_seen(event_id)
            continue

        usd = cost.event_cost_usd(message, pricing)
        tracker.mark_seen(event_id)
        if usd is None:
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
) -> None:
    """Alert on new thresholds; put or lift Deny for each configured project."""
    projects_cfg = cfg.get("projects") or {}
    thresholds = [float(t) for t in (cfg.get("alert_thresholds") or [0.8, 1.0])]

    for project, pcfg in sorted(projects_cfg.items()):
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
            # Idempotent refresh also covers roles that appear mid-day.
            denied = enforce.block_project_roles(iam, role_names)
            failed = [n for n in role_names if n not in denied]
            if failed:
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
            # Ops same-day unblock: always try to lift our Deny, even if we
            # did not track this project as blocked (e.g. after a restart).
            enforce.unblock_project_roles(iam, role_names)
            if project in blocked_projects:
                blocked_projects.discard(project)
                alert.unblocked(project, "enforce set to false in config")
        elif project in blocked_projects:
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


def run_loop() -> None:
    logs = _boto("logs")
    iam = _boto("iam")

    cfg = load_config(CONFIG_PATH)
    if cfg is None:
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
    loaded = persist.load_state(STATE_PATH)
    restored_wm = persist.apply_state(tracker, blocked_projects, loaded)
    # Start slightly in the past so we catch early generator events.
    watermark_ms = restored_wm if restored_wm is not None else (
        int(time.time() * 1000) - 60_000
    )

    # Safety net if state file is empty/missing but Denys linger in IAM.
    role_map = roles.load_role_project_map(iam)
    discovered = enforce.discover_blocked_projects(iam, role_map)
    newly = discovered - blocked_projects
    if newly:
        alert.info(
            "Recovered blocked projects from IAM Deny discovery: "
            + ", ".join(sorted(newly)),
        )
        blocked_projects.update(discovered)

    alert.info(
        f"budget-guard started; config={CONFIG_PATH} state={STATE_PATH} "
        f"day={tracker.day_key} poll={cfg.get('poll_interval_seconds')}s "
        f"blocked={sorted(blocked_projects) or '[]'}",
    )

    while True:
        new_cfg = load_config(CONFIG_PATH)
        if new_cfg is not None:
            cfg = new_cfg
        else:
            alert.warn("Keeping last good config after reload failure")

        if tracker.maybe_roll_day():
            alert.info(f"UTC day rolled to {tracker.day_key}; resetting spend and Denys")
            role_map = roles.load_role_project_map(iam)
            lift_all_managed_denys(iam, role_map, blocked_projects)

        role_map = roles.load_role_project_map(iam)

        start_ms = max(0, watermark_ms - WATERMARK_OVERLAP_MS)
        raw = fetch_log_events(logs, cfg.get("log_group", "/aws/bedrock/modelinvocations"), start_ms)
        applied = process_events(raw, cfg, tracker, role_map)

        watermark_ms = advance_watermark(watermark_ms, raw)

        reconcile_enforcement(iam, cfg, tracker, role_map, blocked_projects)
        alert.status_line(cfg.get("projects") or {}, tracker.spend_usd)
        persist.save_state(STATE_PATH, tracker, blocked_projects, watermark_ms)

        if applied:
            logger.debug("Applied %d new billed events this poll", applied)

        interval = float(cfg.get("poll_interval_seconds") or 15)
        time.sleep(max(1.0, interval))


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
