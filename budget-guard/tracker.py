"""UTC-day spend tracker with eventId dedup and threshold fire-once state."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_day_key(dt: datetime | None = None) -> str:
    """YYYY-MM-DD for the current (or given) moment in UTC."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def parse_event_timestamp_ms(raw) -> int | None:
    """Parse CloudWatch event timestamp (ms) or ISO string from the message."""
    if isinstance(raw, (int, float)):
        # CloudWatch FilterLogEvents uses epoch ms; message body uses ISO.
        val = int(raw)
        # Heuristic: values before year ~2001 in ms are likely seconds.
        if val < 1_000_000_000_000:
            return val * 1000
        return val
    if isinstance(raw, str):
        try:
            # Accept trailing Z
            s = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


@dataclass
class SpendTracker:
    """In-memory per-project spend for the current UTC day.

    Dedups CloudWatch eventIds so overlapping FilterLogEvents windows
    do not double-count. Seen IDs are kept only for the overlap window
    (not persisted). Tracks which alert thresholds already fired.
    """

    day_key: str = field(default_factory=utc_day_key)
    spend_usd: dict[str, float] = field(default_factory=dict)
    # event_id → timestamp_ms; pruned to the watermark overlap window.
    seen_event_ids: dict[str, int] = field(default_factory=dict)
    fired_thresholds: dict[str, set[float]] = field(default_factory=dict)
    warned_unconfigured: set[str] = field(default_factory=set)
    warned_unknown_models: set[str] = field(default_factory=set)

    def reset_for_new_day(self, day_key: str | None = None) -> None:
        self.day_key = day_key or utc_day_key()
        self.spend_usd.clear()
        self.seen_event_ids.clear()
        self.fired_thresholds.clear()
        self.warned_unconfigured.clear()
        self.warned_unknown_models.clear()

    def maybe_roll_day(self) -> bool:
        """If UTC day changed, reset and return True."""
        today = utc_day_key()
        if today != self.day_key:
            self.reset_for_new_day(today)
            return True
        return False

    def already_seen(self, event_id: str) -> bool:
        return event_id in self.seen_event_ids

    def mark_seen(self, event_id: str, ts_ms: int = 0) -> None:
        self.seen_event_ids[event_id] = int(ts_ms)

    def prune_seen(self, keep_after_ms: int) -> None:
        """Drop IDs older than the overlap window."""
        stale = [k for k, ts in self.seen_event_ids.items() if ts < keep_after_ms]
        for k in stale:
            del self.seen_event_ids[k]

    def add_spend(self, project: str, amount_usd: float) -> float:
        self.spend_usd[project] = self.spend_usd.get(project, 0.0) + amount_usd
        return self.spend_usd[project]

    def get_spend(self, project: str) -> float:
        return self.spend_usd.get(project, 0.0)

    def thresholds_to_fire(
        self, project: str, budget_usd: float, thresholds: list[float],
    ) -> list[float]:
        """Return thresholds newly crossed (once per project per day)."""
        if budget_usd <= 0:
            return []
        ratio = self.get_spend(project) / budget_usd
        fired = self.fired_thresholds.setdefault(project, set())
        newly: list[float] = []
        for t in sorted(thresholds):
            if ratio >= t and t not in fired:
                newly.append(t)
                fired.add(t)
        return newly

    def mark_unconfigured_warned(self, project: str) -> bool:
        """Return True if this is the first warn for project today."""
        if project in self.warned_unconfigured:
            return False
        self.warned_unconfigured.add(project)
        return True

    def mark_unknown_model_warned(self, model_id: str) -> bool:
        """Return True if this is the first unknown-model warn today."""
        if model_id in self.warned_unknown_models:
            return False
        self.warned_unknown_models.add(model_id)
        return True
