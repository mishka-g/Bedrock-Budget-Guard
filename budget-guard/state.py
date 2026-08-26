"""Persist spend / watermark / blocked set so restarts resume safely."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from tracker import SpendTracker, utc_day_key

logger = logging.getLogger("budget-guard")

STATE_VERSION = 1
DEFAULT_STATE_PATH = Path(
    os.environ.get("BUDGET_GUARD_STATE", "/app/state/state.json"),
)


def empty_snapshot(day_key: str | None = None) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "day_key": day_key or utc_day_key(),
        "spend_usd": {},
        "seen_event_ids": [],
        "fired_thresholds": {},
        "warned_unconfigured": [],
        "blocked_projects": [],
        "watermark_ms": None,
    }


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load persisted state, or a fresh snapshot if missing/corrupt/wrong day."""
    today = utc_day_key()
    if not path.is_file():
        return empty_snapshot(today)
    try:
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state root must be a mapping")
        if data.get("day_key") != today:
            logger.info(
                "Persisted state day %s != today %s; starting fresh",
                data.get("day_key"),
                today,
            )
            return empty_snapshot(today)
        return {
            "version": STATE_VERSION,
            "day_key": today,
            "spend_usd": {
                str(k): float(v)
                for k, v in (data.get("spend_usd") or {}).items()
            },
            "seen_event_ids": [str(x) for x in (data.get("seen_event_ids") or [])],
            "fired_thresholds": {
                str(proj): {float(t) for t in (vals or [])}
                for proj, vals in (data.get("fired_thresholds") or {}).items()
            },
            "warned_unconfigured": [
                str(x) for x in (data.get("warned_unconfigured") or [])
            ],
            "blocked_projects": [
                str(x) for x in (data.get("blocked_projects") or [])
            ],
            "watermark_ms": (
                int(data["watermark_ms"])
                if data.get("watermark_ms") is not None
                else None
            ),
        }
    except Exception as exc:
        logger.warning("Failed to load state from %s: %s", path, exc)
        return empty_snapshot(today)


def apply_state(
    tracker: SpendTracker,
    blocked_projects: set[str],
    data: dict[str, Any],
) -> int | None:
    """Hydrate tracker + blocked set from a loaded snapshot. Returns watermark_ms."""
    tracker.day_key = str(data.get("day_key") or utc_day_key())
    tracker.spend_usd = dict(data.get("spend_usd") or {})
    tracker.seen_event_ids = set(data.get("seen_event_ids") or [])
    raw_fired = data.get("fired_thresholds") or {}
    tracker.fired_thresholds = {
        proj: (set(vals) if isinstance(vals, set) else {float(t) for t in vals})
        for proj, vals in raw_fired.items()
    }
    tracker.warned_unconfigured = set(data.get("warned_unconfigured") or [])
    blocked_projects.clear()
    blocked_projects.update(data.get("blocked_projects") or [])
    wm = data.get("watermark_ms")
    return int(wm) if wm is not None else None


def save_state(
    path: Path,
    tracker: SpendTracker,
    blocked_projects: set[str],
    watermark_ms: int,
) -> None:
    """Atomically write current in-memory state to disk."""
    payload = {
        "version": STATE_VERSION,
        "day_key": tracker.day_key,
        "spend_usd": {k: float(v) for k, v in tracker.spend_usd.items()},
        "seen_event_ids": sorted(tracker.seen_event_ids),
        "fired_thresholds": {
            proj: sorted(vals)
            for proj, vals in tracker.fired_thresholds.items()
        },
        "warned_unconfigured": sorted(tracker.warned_unconfigured),
        "blocked_projects": sorted(blocked_projects),
        "watermark_ms": int(watermark_ms),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Failed to save state to %s: %s", path, exc)
