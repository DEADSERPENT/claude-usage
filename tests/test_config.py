import pytest
from config import calc_cost, get_pricing_for_model, PRICING


class TestCalcCost:
    def test_zero_tokens(self):
        assert calc_cost("claude-sonnet-4-6", 0, 0, 0, 0) == 0.0

    def test_one_million_input_sonnet(self):
        cost = calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(PRICING["claude-sonnet-4-6"]["input"])

    def test_one_million_output_opus(self):
        cost = calc_cost("claude-opus-4-6", 0, 1_000_000, 0, 0)
        assert cost == pytest.approx(PRICING["claude-opus-4-6"]["output"])

    def test_cache_read_haiku(self):
        cost = calc_cost("claude-haiku-4-5", 0, 0, 1_000_000, 0)
        assert cost == pytest.approx(PRICING["claude-haiku-4-5"]["cache_read"])

    def test_cache_creation_sonnet(self):
        cost = calc_cost("claude-sonnet-4-6", 0, 0, 0, 1_000_000)
        assert cost == pytest.approx(PRICING["claude-sonnet-4-6"]["cache_write"])

    def test_combined_tokens(self):
        inp, out, cr, cc = 100_000, 50_000, 10_000, 5_000
        p = PRICING["claude-sonnet-4-6"]
        expected = (
            inp * p["input"] / 1_000_000
            + out * p["output"] / 1_000_000
            + cr * p["cache_read"] / 1_000_000
            + cc * p["cache_write"] / 1_000_000
        )
        assert calc_cost("claude-sonnet-4-6", inp, out, cr, cc) == pytest.approx(expected)

    def test_unknown_model_uses_default(self):
        cost_default = calc_cost("default", 100_000, 50_000, 0, 0)
        cost_unknown = calc_cost("completely-unknown-model-xyz", 100_000, 50_000, 0, 0)
        assert cost_default == pytest.approx(cost_unknown)

    def test_none_model_uses_default(self):
        cost_none = calc_cost(None, 100_000, 0, 0, 0)
        cost_default = calc_cost("default", 100_000, 0, 0, 0)
        assert cost_none == pytest.approx(cost_default)

    def test_all_models_priced(self):
        for model in PRICING:
            cost = calc_cost(model, 1_000_000, 1_000_000, 0, 0)
            assert cost > 0, f"Zero cost for model {model}"


class TestGetPricingForModel:
    def test_exact_match(self):
        for key in PRICING:
            assert get_pricing_for_model(key) == PRICING[key]

    def test_prefix_match(self):
        p = get_pricing_for_model("claude-opus-4-6-20251001")
        assert p == PRICING["claude-opus-4-6"]

    def test_fuzzy_opus(self):
        p = get_pricing_for_model("my-custom-opus-model")
        assert p == PRICING.get("claude-opus-4-6")

    def test_fuzzy_sonnet(self):
        p = get_pricing_for_model("fast-sonnet-variant")
        assert p == PRICING.get("claude-sonnet-4-6")

    def test_fuzzy_haiku(self):
        p = get_pricing_for_model("haiku-custom")
        assert p == PRICING.get("claude-haiku-4-5")

    def test_none_returns_default(self):
        assert get_pricing_for_model(None) == PRICING["default"]

    def test_empty_string_returns_default(self):
        assert get_pricing_for_model("") == PRICING["default"]

    def test_completely_unknown_returns_default(self):
        assert get_pricing_for_model("gpt-4-turbo") == PRICING["default"]

    def test_result_has_required_keys(self):
        for model in list(PRICING) + ["unknown-model"]:
            p = get_pricing_for_model(model)
            assert "input" in p
            assert "output" in p
            assert "cache_read" in p
            assert "cache_write" in p
