"""Pure scenario engine: turns scenarios.yaml into deterministic
per-minute batches of Bedrock invocation-log records.

Determinism contract: the RNG for minute m is seeded with
(config.seed * 1_000_003 + m) using only integers, so the same
config produces the exact same stream in any process on any machine
(no dependence on Python hash randomization or wall clock).
"""
import random
from dataclasses import dataclass

import yaml

ACCOUNT_ID = "000000000000"
REGION = "eu-west-1"


@dataclass(frozen=True)
class TokenDist:
    mean: int
    spread: int


@dataclass(frozen=True)
class Traffic:
    role: str
    model: str
    calls_per_minute: float
    input_tokens: TokenDist
    output_tokens: TokenDist
    cache_read_tokens: TokenDist
    cache_write_tokens: TokenDist


@dataclass(frozen=True)
class TimelineEvent:
    at_minute: int
    duration_minutes: int
    role: str
    rate_multiplier: float


@dataclass(frozen=True)
class Config:
    seed: int
    baseline: tuple[Traffic, ...]
    timeline: tuple[TimelineEvent, ...]


def _dist(raw: dict) -> TokenDist:
    return TokenDist(mean=int(raw["mean"]), spread=int(raw["spread"]))


def load_config(path: str) -> Config:
    with open(path) as f:
        doc = yaml.safe_load(f)
    baseline = tuple(
        Traffic(
            role=b["role"],
            model=b["model"],
            calls_per_minute=float(b["callsPerMinute"]),
            input_tokens=_dist(b["tokens"]["input"]),
            output_tokens=_dist(b["tokens"]["output"]),
            cache_read_tokens=_dist(b["tokens"]["cacheRead"]),
            cache_write_tokens=_dist(b["tokens"]["cacheWrite"]),
        )
        for b in doc["baseline"]
    )
    timeline = tuple(
        TimelineEvent(
            at_minute=int(t["atMinute"]),
            duration_minutes=int(t["durationMinutes"]),
            role=t["role"],
            rate_multiplier=float(t["rateMultiplier"]),
        )
        for t in doc.get("timeline") or []
    )
    return Config(seed=int(doc["seed"]), baseline=baseline, timeline=timeline)


def _rate_multiplier(config: Config, role: str, minute: int) -> float:
    mult = 1.0
    for ev in config.timeline:
        if ev.role == role and ev.at_minute <= minute < ev.at_minute + ev.duration_minutes:
            mult *= ev.rate_multiplier
    return mult


def _sample_tokens(rng: random.Random, dist: TokenDist) -> int:
    return max(0, int(rng.gauss(dist.mean, dist.spread)))


def events_for_minute(config: Config, minute: int) -> list[tuple[float, dict]]:
    """Return [(offset_seconds, record)] for one minute of simulated traffic,
    sorted by offset. Records carry every log field EXCEPT the timestamp,
    which the delivery loop stamps from (run start + minute + offset)."""
    rng = random.Random(config.seed * 1_000_003 + minute)
    out: list[tuple[float, dict]] = []
    for traffic in config.baseline:
        rate = traffic.calls_per_minute * _rate_multiplier(config, traffic.role, minute)
        calls = int(rate)
        if rng.random() < rate - calls:  # fractional rates fire probabilistically
            calls += 1
        for i in range(calls):
            offset = rng.uniform(0.0, 60.0)
            record = {
                "region": REGION,
                "modelId": traffic.model,
                "identity": {
                    "arn": f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/{traffic.role}/session-{i}",
                },
                "input": {
                    "inputTokenCount": _sample_tokens(rng, traffic.input_tokens),
                    "cacheReadInputTokenCount": _sample_tokens(rng, traffic.cache_read_tokens),
                    "cacheWriteInputTokenCount": _sample_tokens(rng, traffic.cache_write_tokens),
                },
                "output": {
                    "outputTokenCount": _sample_tokens(rng, traffic.output_tokens),
                },
            }
            out.append((offset, record))
    out.sort(key=lambda pair: pair[0])
    return out
