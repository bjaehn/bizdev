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


def test_haiku_5_5_charges_the_higher_rate_for_a_request_whose_prompt_is_over_100k_tokens(cfg):
    short = Usage(input_tokens=1_000_000, output_tokens=1_000_000)                      # requests with short prompts
    assert estimate_cost(short, "claude-haiku-5-5", cfg) == pytest.approx(0.10 + 0.50)
    long = Usage(long_input_tokens=1_000_000, long_output_tokens=1_000_000)             # requests over the threshold
    assert estimate_cost(long, "claude-haiku-5-5", cfg) == pytest.approx(0.50 + 2.50)
    mixed = Usage(input_tokens=1_000_000, long_input_tokens=1_000_000, long_cache_read_tokens=1_000_000, long_cache_creation_tokens=1_000_000)
    assert estimate_cost(mixed, "claude-haiku-5-5", cfg) == pytest.approx(0.10 + 0.50 + 0.05 + 0.625)
    assert long.total_tokens == 2_000_000 and mixed.total_tokens == 4_000_000


def test_a_model_with_one_rate_prices_long_prompt_tokens_at_that_rate(cfg):
    assert estimate_cost(Usage(long_input_tokens=1_000_000), "claude-sonnet-5-5", cfg) == pytest.approx(2.0)


def test_usage_adds_and_moves_a_request_to_the_long_buckets():
    one = Usage(input_tokens=90_000, cache_read_tokens=20_000, output_tokens=500, web_searches=2)
    assert one.prompt_tokens == 110_000
    moved = one.as_long()
    assert (moved.input_tokens, moved.long_input_tokens, moved.long_cache_read_tokens, moved.long_output_tokens, moved.web_searches) == (0, 90_000, 20_000, 500, 2)
    total = Usage()
    total.add(one)
    total.add(moved)
    assert total.input_tokens == 90_000 and total.long_input_tokens == 90_000 and total.web_searches == 4 and total.total_tokens == 221_000
