"""Prometheus metrics for FinOps dashboards and SRE health."""
from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from tracker import SpendTracker

SPEND_USD = Gauge(
    "budget_guard_spend_usd",
    "Estimated Bedrock spend so far this UTC day, per project",
    ["project"],
)
BUDGET_USD = Gauge(
    "budget_guard_budget_usd",
    "Configured daily budget in USD, per project",
    ["project"],
)
SPEND_RATIO = Gauge(
    "budget_guard_spend_ratio",
    "Spend / budget for the UTC day (0 when budget is 0)",
    ["project"],
)
BLOCKED = Gauge(
    "budget_guard_blocked",
    "1 if the project currently has a managed Bedrock Deny",
    ["project"],
)
ENFORCE = Gauge(
    "budget_guard_enforce",
    "1 if config says this project should auto-block",
    ["project"],
)
UNCONFIGURED_SPEND = Gauge(
    "budget_guard_unconfigured_spend_usd",
    "Spend for project-tagged roles with no budget in config",
    ["project"],
)
UNPRICED_EVENTS = Counter(
    "budget_guard_unpriced_events_total",
    "Invocation events skipped because modelId has no pricing",
    ["model_id"],
)
IS_LEADER = Gauge(
    "budget_guard_is_leader",
    "1 if this process currently holds the leader lease",
)
POLL_DURATION = Histogram(
    "budget_guard_poll_duration_seconds",
    "Wall time of one poll iteration",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
EVENTS_APPLIED = Gauge(
    "budget_guard_events_applied",
    "Billed events applied in the last successful poll",
)
WATERMARK_LAG = Gauge(
    "budget_guard_watermark_lag_seconds",
    "Now minus last processed CloudWatch event timestamp",
)
LOG_FETCH_ERRORS = Counter(
    "budget_guard_log_fetch_errors_total",
    "FilterLogEvents failures (fail-open: last spend is kept)",
)
IAM_PUT_FAILURES = Counter(
    "budget_guard_iam_put_failures_total",
    "put_role_policy failures while trying to attach Deny",
)
STATE_SAVE_ERRORS = Counter(
    "budget_guard_state_save_errors_total",
    "Failed compact-state writes (file or ConfigMap)",
)
LAST_POLL_OK = Gauge(
    "budget_guard_last_poll_ok",
    "1 if the last poll's log fetch succeeded",
)


def set_leader(is_leader: bool) -> None:
    IS_LEADER.set(1 if is_leader else 0)


def unpriced_inc(model_id: str) -> None:
    UNPRICED_EVENTS.labels(model_id=model_id).inc()


def log_fetch_error() -> None:
    LOG_FETCH_ERRORS.inc()


def iam_put_failures(count: int) -> None:
    if count > 0:
        IAM_PUT_FAILURES.inc(count)


def state_save_error() -> None:
    STATE_SAVE_ERRORS.inc()


def observe_poll(
    *,
    cfg: dict[str, Any],
    tracker: SpendTracker,
    blocked_projects: set[str],
    watermark_ms: int,
    applied: int,
    duration_s: float,
    fetch_ok: bool,
    now_ms: int,
) -> None:
    """Replace per-project gauges after a poll (leader only)."""
    LAST_POLL_OK.set(1 if fetch_ok else 0)
    EVENTS_APPLIED.set(applied)
    POLL_DURATION.observe(duration_s)
    WATERMARK_LAG.set(max(0.0, (now_ms - watermark_ms) / 1000.0))

    projects_cfg = cfg.get("projects") or {}
    configured = set()
    SPEND_USD.clear()
    BUDGET_USD.clear()
    SPEND_RATIO.clear()
    BLOCKED.clear()
    ENFORCE.clear()
    UNCONFIGURED_SPEND.clear()

    for name, pcfg in projects_cfg.items():
        if not isinstance(pcfg, dict):
            continue
        configured.add(name)
        budget = float(pcfg.get("budget_usd") or 0)
        spend = tracker.get_spend(name)
        ratio = (spend / budget) if budget > 0 else 0.0
        SPEND_USD.labels(project=name).set(spend)
        BUDGET_USD.labels(project=name).set(budget)
        SPEND_RATIO.labels(project=name).set(ratio)
        BLOCKED.labels(project=name).set(1 if name in blocked_projects else 0)
        ENFORCE.labels(project=name).set(1 if pcfg.get("enforce", True) else 0)

    for name, spend in tracker.spend_usd.items():
        if name not in configured:
            UNCONFIGURED_SPEND.labels(project=name).set(spend)
