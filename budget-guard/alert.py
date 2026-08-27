"""Human-readable stdout alerts and optional Slack webhooks (not JSON)."""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Reloaded from config each poll via configure().
_slack_enabled: bool = False
_slack_webhook_url: str = ""
_slack_events: set[str] = {"ALERT", "BLOCKED", "UNBLOCKED"}
_DEFAULT_SLACK_EVENTS = ("ALERT", "BLOCKED", "UNBLOCKED")


def configure(cfg: dict[str, Any] | None) -> None:
    """Apply alerts.slack from config. Env SLACK_WEBHOOK_URL overrides webhook_url."""
    global _slack_enabled, _slack_webhook_url, _slack_events

    alerts = (cfg or {}).get("alerts") or {}
    slack = alerts.get("slack") if isinstance(alerts, dict) else None
    if not isinstance(slack, dict):
        _slack_enabled = False
        _slack_webhook_url = ""
        _slack_events = set(_DEFAULT_SLACK_EVENTS)
        return

    env_url = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
    cfg_url = str(slack.get("webhook_url") or "").strip()
    _slack_webhook_url = env_url or cfg_url
    _slack_enabled = bool(slack.get("enabled")) and bool(_slack_webhook_url)

    raw_events = slack.get("events")
    if raw_events is None:
        _slack_events = set(_DEFAULT_SLACK_EVENTS)
    elif isinstance(raw_events, list):
        # Explicit empty list means "mute all" — distinct from omitting the key.
        _slack_events = {str(e).strip().upper() for e in raw_events if str(e).strip()}
    else:
        _slack_events = set(_DEFAULT_SLACK_EVENTS)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _send_slack(url: str, body: bytes) -> None:
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        # Any failure here (bad URL, timeout, DNS, HTTP error) is soft —
        # stdout only, never let it recurse into Slack or reach the caller.
        print(f"[{_ts()}] {'WARN':<8} Slack webhook failed: {exc}", flush=True)


def _post_slack(kind: str, message: str) -> None:
    if not _slack_enabled or kind.upper() not in _slack_events:
        return
    body = json.dumps({"text": f"*[{kind}]* {message}"}).encode("utf-8")
    # Off the main poll/enforcement thread: a slow or unreachable webhook
    # must never delay IAM enforcement for other projects.
    threading.Thread(
        target=_send_slack, args=(_slack_webhook_url, body), daemon=True,
    ).start()


def log_line(kind: str, message: str) -> None:
    """Print one plain-text line, e.g. [14:52:01] ALERT   ..."""
    print(f"[{_ts()}] {kind:<8} {message}", flush=True)
    _post_slack(kind, message)


def status_line(projects_cfg: dict, spend: dict[str, float]) -> None:
    """One STATUS line summarizing configured projects' spend vs budget."""
    parts: list[str] = []
    for name in sorted(projects_cfg.keys()):
        cfg = projects_cfg[name] or {}
        budget = float(cfg.get("budget_usd") or 0)
        used = float(spend.get(name, 0.0))
        pct = int(round((used / budget) * 100)) if budget > 0 else 0
        parts.append(f"{name} ${used:.2f} / ${budget:.2f} ({pct}%)")
    log_line("STATUS", "  ".join(parts) if parts else "(no configured projects)")


def alert_threshold(
    project: str, threshold: float, spend: float, budget: float,
) -> None:
    pct = int(round(threshold * 100))
    log_line(
        "ALERT",
        f"Project {project} reached {pct}% of its daily budget "
        f"(${spend:.2f} of ${budget:.2f}).",
    )


def blocked(project: str, role_names: list[str]) -> None:
    roles = ", ".join(role_names) if role_names else "(none)"
    log_line(
        "BLOCKED",
        f"Project {project} — Bedrock access paused for roles: {roles}",
    )


def unblocked(project: str, reason: str) -> None:
    log_line(
        "UNBLOCKED",
        f"Project {project} — reason: {reason}",
    )


def warn(message: str) -> None:
    log_line("WARN", message)


def info(message: str) -> None:
    log_line("INFO", message)
