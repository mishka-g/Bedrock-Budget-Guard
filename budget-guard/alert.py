"""Human-readable stdout alerts and STATUS heartbeats (not JSON)."""
from __future__ import annotations

from datetime import datetime, timezone


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log_line(kind: str, message: str) -> None:
    """Print one plain-text line, e.g. [14:52:01] ALERT   ..."""
    print(f"[{_ts()}] {kind:<8} {message}", flush=True)


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
