import pytest

from bd.cost import estimate_cost
from bd.llm import Usage


def test_sonnet_5_5_cost_arithmetic(cfg):
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000,
              cache_creation_tokens=100_000, web_searches=3)
    # 2.00 in + 1.00 out + 0.10 cache read + 0.25 cache write + 0.03 searches
    assert estimate_cost(u, "claude-sonnet-5-5", cfg) == pytest.approx(3.38)


def test_other_models_use_their_own_rates(cfg):
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost(u, "claude-opus-5-5", cfg) == pytest.approx(24.0)
    assert estimate_cost(u, "claude-fable-5-1", cfg) == pytest.approx(60.0)


def test_searches_only(cfg):
    assert estimate_cost(Usage(web_searches=8), "claude-sonnet-5-5", cfg) == pytest.approx(0.08)


def test_unknown_or_missing_model_is_unpriced_not_guessed(cfg):
    assert estimate_cost(Usage(input_tokens=5), "claude-future-9", cfg) is None
    assert estimate_cost(Usage(input_tokens=5), None, cfg) is None
