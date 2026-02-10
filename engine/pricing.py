"""Canonical model pricing and cost computation for ClawTrace."""

# Pricing per million tokens (as of 2026-02)
MODEL_PRICING = {
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-opus-4-5-20251101": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-sonnet-4-5-20250929": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.0,
        "cache_read": 0.08,
        "cache_write": 1.0,
    },
}

# Fallback pricing for unknown models — use Sonnet pricing as default
DEFAULT_PRICING = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.30,
    "cache_write": 3.75,
}

# Zero-cost pricing for free providers (e.g., NVIDIA)
FREE_PRICING = {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
}


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    pricing_overrides: dict | None = None,
) -> tuple[float, dict]:
    """Compute cost from token counts and model pricing.

    Override resolution order:
    1. pricing_overrides[model] — exact model match
    2. pricing_overrides["*"] — provider wildcard
    3. MODEL_PRICING[model] — global known model
    4. DEFAULT_PRICING — Sonnet fallback
    """
    pricing = None
    if pricing_overrides:
        if model in pricing_overrides:
            pricing = pricing_overrides[model]
        elif "*" in pricing_overrides:
            pricing = pricing_overrides["*"]
    if pricing is None:
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)

    breakdown = {
        "input": input_tokens * pricing["input"] / 1_000_000,
        "output": output_tokens * pricing["output"] / 1_000_000,
        "cache_read": cache_read_tokens * pricing["cache_read"] / 1_000_000,
        "cache_write": cache_write_tokens * pricing["cache_write"] / 1_000_000,
    }
    total = sum(breakdown.values())
    return total, breakdown
