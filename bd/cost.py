"""Estimated cost of a scan call from token usage. Prices live in config.yaml (pricing)."""

from __future__ import annotations

from bd.config import Config
from bd.llm import Usage


def estimate_cost(usage: Usage, model: str | None, cfg: Config) -> float | None:
    """USD estimate, or None if the model has no price in config (never guessed).

    Anthropic reports cache reads/writes separately from input_tokens, so the four token
    counts are disjoint. Web searches are charged per search on top of tokens."""
    price = cfg.pricing.models.get(model or "")
    if price is None:
        return None
    per_tok = 1 / 1_000_000
    return round(
        usage.input_tokens * price.input * per_tok
        + usage.output_tokens * price.output * per_tok
        + usage.cache_creation_tokens * price.cache_write_5m * per_tok
        + usage.cache_read_tokens * price.cache_read * per_tok
        + usage.web_searches * cfg.pricing.web_search_per_1000 / 1000,
        6,
    )
