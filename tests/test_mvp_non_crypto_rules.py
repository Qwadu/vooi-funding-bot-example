"""Tests for HL non-crypto opportunity gate (``opportunity_passes_non_crypto_rules``)."""

from __future__ import annotations

from fundbot.mvp import opportunity_passes_non_crypto_rules


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
