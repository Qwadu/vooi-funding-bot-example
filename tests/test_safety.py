"""Unit-тесты на probe.safety: SafetyBudget, safe_limit_price, assert_alo, assert_small_notional.

Эти тесты — чистая логика без сети.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from probe.safety import (
    SAFE_DISTANCE_PCT,
    ProbeSafetyError,
    SafetyBudget,
    assert_alo,
    assert_small_notional,
    safe_limit_price,
)


class TestSafetyBudget:
    def test_reserve_within_budget_succeeds(self) -> None:
        b = SafetyBudget(Decimal("100"))
        b.reserve(Decimal("30"))
        assert b.spent == Decimal("30")
        b.reserve(Decimal("70"))
        assert b.spent == Decimal("100")

    def test_reserve_exceeding_budget_raises(self) -> None:
        b = SafetyBudget(Decimal("50"))
        b.reserve(Decimal("30"))
        with pytest.raises(ProbeSafetyError, match="exceeded"):
            b.reserve(Decimal("21"))
        # Partial state is preserved — spent didn't increment on failure
        assert b.spent == Decimal("30")

    def test_zero_budget_blocks_all_writes(self) -> None:
        b = SafetyBudget(Decimal("0"))
        with pytest.raises(ProbeSafetyError):
            b.reserve(Decimal("0.01"))

    def test_exact_budget_match_ok(self) -> None:
        b = SafetyBudget(Decimal("5"))
        b.reserve(Decimal("5"))
        assert b.spent == Decimal("5")
        with pytest.raises(ProbeSafetyError):
            b.reserve(Decimal("0.01"))


class TestSafeLimitPrice:
    def test_buy_far_below_mid(self) -> None:
        # Покупаем дёшево → не fill, висит в книге.
        p = safe_limit_price(Decimal("100"), "buy")
        assert p < Decimal("100")
        assert p == Decimal("50")  # 50% от mid

    def test_sell_far_above_mid(self) -> None:
        p = safe_limit_price(Decimal("100"), "sell")
        assert p > Decimal("100")
        assert p == Decimal("150")

    def test_distance_uses_module_constant(self) -> None:
        # Если SAFE_DISTANCE_PCT поменяют на 30%, тест должен сработать
        # без хардкода ожидания, кроме как сам процент.
        mid = Decimal("1000")
        buy_p = safe_limit_price(mid, "buy")
        expected = mid * (Decimal("1") - SAFE_DISTANCE_PCT / Decimal("100"))
        assert buy_p == expected

    def test_zero_mid_returns_zero_safely(self) -> None:
        # Граничный случай — не должен делить на ноль и т.д.
        assert safe_limit_price(Decimal("0"), "buy") == Decimal("0")
        assert safe_limit_price(Decimal("0"), "sell") == Decimal("0")


class TestAssertAlo:
    def test_alo_passes(self) -> None:
        assert_alo("alo")  # no exception

    @pytest.mark.parametrize("tif", ["gtc", "ioc", "fok", "GTC", "ALO", "", "limit"])
    def test_non_alo_blocked(self, tif: str) -> None:
        with pytest.raises(ProbeSafetyError, match="alo"):
            assert_alo(tif)


class TestAssertSmallNotional:
    def test_within_cap_ok(self) -> None:
        assert_small_notional(Decimal("5"), Decimal("5"))
        assert_small_notional(Decimal("4.99"), Decimal("5"))

    def test_exceeds_cap_raises(self) -> None:
        with pytest.raises(ProbeSafetyError, match="exceeds"):
            assert_small_notional(Decimal("5.01"), Decimal("5"))

    def test_zero_notional_passes(self) -> None:
        assert_small_notional(Decimal("0"), Decimal("5"))
