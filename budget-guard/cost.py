"""Convert Bedrock invocation token counts into USD cost.

Rates come from config (USD per 1 million tokens). Unknown models are
skipped with a warning — we never invent a price.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("budget-guard")


def event_cost_usd(message: dict[str, Any], pricing: dict[str, dict[str, float]]) -> float | None:
    """Return USD cost for one invocation-log message, or None if unpriced.

    Formula (matches generator scenario comments):
      cost = (input*input_rate + output*output_rate
              + cacheRead*cache_read + cacheWrite*cache_write) / 1e6
    """
    model_id = message.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        logger.warning("Skipping event with missing modelId")
        return None

    rates = pricing.get(model_id)
    if rates is None:
        logger.warning(
            "Unknown modelId %r — no pricing in config; skipping cost",
            model_id,
        )
        return None

    inp = message.get("input") or {}
    out = message.get("output") or {}
    if not isinstance(inp, dict):
        inp = {}
    if not isinstance(out, dict):
        out = {}

    input_tok = float(inp.get("inputTokenCount") or 0)
    output_tok = float(out.get("outputTokenCount") or 0)
    cache_read = float(inp.get("cacheReadInputTokenCount") or 0)
    cache_write = float(inp.get("cacheWriteInputTokenCount") or 0)

    cost = (
        input_tok * float(rates.get("input", 0))
        + output_tok * float(rates.get("output", 0))
        + cache_read * float(rates.get("cache_read", 0))
        + cache_write * float(rates.get("cache_write", 0))
    ) / 1_000_000.0
    return cost
