"""Tests for HL non-crypto opportunity gate (``opportunity_passes_non_crypto_rules``)."""

from __future__ import annotations

import pytest

from fundbot import mvp
from fundbot.mvp import (
    ALL_SUPPORTED_EXCHANGES,
    ALLOWED_TRADING_EXCHANGES,
    MarketMeta,
    make_coid,
    opportunity_passes_non_crypto_rules,
)


def test_c9_off_rejects_any_non_crypto_leg_or_asset() -> None:
    assert not opportunity_passes_non_crypto_rules(
        False, "xyz:AAPL", "BTC", "SOME", "hyperliquid", "lighter",
    )
    assert not opportunity_passes_non_crypto_rules(
        False, "BTC", "BTC", "alias:gold", "hyperliquid", "lighter",
    )
    assert opportunity_passes_non_crypto_rules(
        False, "BTC", "ETH", "BTC", "hyperliquid", "lighter",
    )


def test_c9_on_requires_hyperliquid_for_non_crypto() -> None:
    assert opportunity_passes_non_crypto_rules(
        True, "xyz:AAPL", "BTC", "SOME", "hyperliquid", "lighter",
    )
    assert not opportunity_passes_non_crypto_rules(
        True, "xyz:AAPL", "BTC", "SOME", "lighter", "aster",
    )


# --- category-based non-crypto gate (multi-venue) -----------------------------


def _meta(category: str | None) -> MarketMeta:
    return MarketMeta(
        base_decimals=4,
        price_decimals=4,
        quote_decimals=6,
        open=True,
        funding_interval_h=1,
        max_leverage=10,
        category=category,
    )


@pytest.fixture(autouse=True)
def _clear_markets_cache():
    mvp.markets_cache.clear()
    yield
    mvp.markets_cache.clear()


def test_category_gate_detects_non_crypto_on_any_venue() -> None:
    """ondo AAPL / robinhood equities have no xyz:/alias: prefix — only
    `category` in /exchange/markets marks them as non-crypto."""
    mvp.markets_cache[("ondo", "AAPL")] = _meta("stocks-us")
    mvp.markets_cache[("robinhood", "AAPL")] = _meta("stocks-us")
    assert mvp._leg_is_non_crypto_by_category("ondo", "AAPL")
    assert mvp._leg_is_non_crypto_by_category("robinhood", "AAPL")


def test_category_gate_passes_crypto() -> None:
    mvp.markets_cache[("binance", "BTC")] = _meta("crypto")
    mvp.markets_cache[("bybit", "ETH")] = _meta("crypto")
    assert not mvp._leg_is_non_crypto_by_category("binance", "BTC")
    assert not mvp._leg_is_non_crypto_by_category("bybit", "ETH")


def test_category_gate_unknown_meta_or_category_is_crypto() -> None:
    # no cache entry at all
    assert not mvp._leg_is_non_crypto_by_category("gate", "SOMEMEME")
    # meta present but category field absent (old API response shape)
    mvp.markets_cache[("mexc", "AAA")] = _meta(None)
    assert not mvp._leg_is_non_crypto_by_category("mexc", "AAA")


def test_allowlist_covers_all_vooi_venues() -> None:
    expected = {
        "hyperliquid", "lighter", "aster", "extended", "robinhood",
        "binance", "bybit", "mexc", "gate", "ondo",
    }
    assert expected <= ALLOWED_TRADING_EXCHANGES
    assert set(ALL_SUPPORTED_EXCHANGES) == set(ALLOWED_TRADING_EXCHANGES)


def test_make_coid_compact_for_cex() -> None:
    for ex in ("binance", "bybit", "mexc", "gate", "aster", "lighter", "extended", "robinhood", "ondo"):
        coid = make_coid("abcd1234ef56", "abcd1234ef56-deadbeef", "long", ex)
        assert len(coid) <= 36, f"{ex}: coid too long for CEX limits"
    # deterministic
    assert make_coid("i", "i-a1", "long", "bybit") == make_coid("i", "i-a1", "long", "bybit")
    assert make_coid("i", "i-a1", "long", "bybit") != make_coid("i", "i-a1", "short", "bybit")
    # HL format unchanged
    hl = make_coid("i", "i-a1", "long", "hyperliquid")
    assert hl.startswith("0x") and len(hl) == 34
