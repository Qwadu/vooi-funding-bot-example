"""Unit + integration тесты на market-classification (C9 hard-reject xyz/alias).

См. `docs/plan.md §4.A.9` и `docs/api-probe-results.md §6.1`.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from probe.client import ProbeClient
from probe.markets import (
    BASE_PREFIX_TO_ACCOUNT_TYPE,
    CRYPTO_PERPS_ACCOUNT_TYPE,
    NON_CRYPTO_PERPS_BASE_PREFIXES,
    expected_account_type_for_base_symbol,
    filter_crypto_perps,
    is_crypto_perps_market,
    is_non_crypto_prefix,
    margin_bucket_key,
)
from probe.questions import _get_market_info


class TestIsNonCryptoPrefix:
    def test_xyz_prefix_detected(self) -> None:
        assert is_non_crypto_prefix("xyz:AAPL")
        assert is_non_crypto_prefix("xyz:GOLD")
        assert is_non_crypto_prefix("xyz:BRENTOIL")

    def test_alias_prefix_detected(self) -> None:
        assert is_non_crypto_prefix("alias:gold")

    def test_crypto_symbol_not_detected(self) -> None:
        assert not is_non_crypto_prefix("BTC")
        assert not is_non_crypto_prefix("ETH")
        assert not is_non_crypto_prefix("AAPL")  # без префикса = crypto, не stocks

    def test_empty_or_none_safe(self) -> None:
        assert not is_non_crypto_prefix("")
        assert not is_non_crypto_prefix(None)

    def test_case_sensitive_xyz_only_lower(self) -> None:
        # На VOOI мы видели только lowercase `xyz:` — не маскируем `XYZ:` и `Xyz:`
        # потому что это могут быть будущие крипто-токены вида `XYZTOKEN`.
        assert not is_non_crypto_prefix("XYZ:AAPL")  # предполагаемое будущее: иной класс
        assert not is_non_crypto_prefix("Xyz:GOLD")


class TestIsCryptoPerpsMarket:
    def test_normal_crypto_market_passes(self) -> None:
        m: dict[str, Any] = {
            "baseSymbol": "BTC",
            "exchange": "hyperliquid",
            "open": True,
            "price": "76480.0",
        }
        assert is_crypto_perps_market(m)

    def test_xyz_market_rejected(self) -> None:
        m: dict[str, Any] = {
            "baseSymbol": "xyz:AAPL",
            "exchange": "hyperliquid",
            "open": True,
            "price": "180.5",
        }
        assert not is_crypto_perps_market(m)

    def test_alias_market_rejected(self) -> None:
        m: dict[str, Any] = {"baseSymbol": "alias:gold", "exchange": "hyperliquid", "open": True}
        assert not is_crypto_perps_market(m)

    def test_closed_market_rejected(self) -> None:
        m: dict[str, Any] = {"baseSymbol": "BTC", "exchange": "hyperliquid", "open": False}
        assert not is_crypto_perps_market(m)

    def test_open_field_missing_treated_as_open(self) -> None:
        # Если `open` отсутствует — считаем что market торгуется (default permissive).
        m: dict[str, Any] = {"baseSymbol": "BTC", "exchange": "hyperliquid"}
        assert is_crypto_perps_market(m)

    def test_missing_basesymbol_rejected(self) -> None:
        assert not is_crypto_perps_market({"exchange": "hyperliquid", "open": True})
        assert not is_crypto_perps_market({"baseSymbol": "", "exchange": "hyperliquid"})

    def test_basesymbol_non_string_rejected(self) -> None:
        assert not is_crypto_perps_market({"baseSymbol": None, "exchange": "hyperliquid"})
        assert not is_crypto_perps_market({"baseSymbol": 123, "exchange": "hyperliquid"})  # type: ignore[dict-item]


class TestExpectedAccountType:
    def test_crypto_returns_perps(self) -> None:
        assert expected_account_type_for_base_symbol("BTC") == CRYPTO_PERPS_ACCOUNT_TYPE
        assert expected_account_type_for_base_symbol("ETH") == "perps"

    def test_xyz_returns_xyz(self) -> None:
        assert expected_account_type_for_base_symbol("xyz:AAPL") == "xyz"
        assert expected_account_type_for_base_symbol("xyz:CORN") == "xyz"

    def test_alias_maps_explicit_perps(self) -> None:
        assert expected_account_type_for_base_symbol("alias:gold") == CRYPTO_PERPS_ACCOUNT_TYPE


class TestFilterCryptoPerps:
    def test_filters_out_xyz_and_alias(self) -> None:
        markets = [
            {"baseSymbol": "BTC", "open": True},
            {"baseSymbol": "xyz:AAPL", "open": True},
            {"baseSymbol": "ETH", "open": True},
            {"baseSymbol": "alias:gold", "open": True},
            {"baseSymbol": "SOL", "open": False},  # closed
        ]
        out = filter_crypto_perps(markets)
        symbols = {m["baseSymbol"] for m in out}
        assert symbols == {"BTC", "ETH"}


class TestConstants:
    def test_xyz_in_prefixes(self) -> None:
        assert "xyz:" in NON_CRYPTO_PERPS_BASE_PREFIXES

    def test_alias_in_prefixes(self) -> None:
        assert "alias:" in NON_CRYPTO_PERPS_BASE_PREFIXES

    def test_xyz_maps_to_xyz_account_type(self) -> None:
        assert BASE_PREFIX_TO_ACCOUNT_TYPE["xyz:"] == "xyz"

    def test_alias_maps_to_perps_account_type(self) -> None:
        assert BASE_PREFIX_TO_ACCOUNT_TYPE["alias:"] == "perps"


class TestMarginBucketKey:
    def test_hyperliquid_crypto_usdc(self) -> None:
        assert margin_bucket_key("hyperliquid", "BTC", "USDC") == "hyperliquid:perps:USDC"

    def test_hyperliquid_crypto_usdh(self) -> None:
        assert margin_bucket_key("hyperliquid", "km:US500", "USDH") == "hyperliquid:perps:USDH"

    def test_hyperliquid_xyz(self) -> None:
        assert margin_bucket_key("hyperliquid", "xyz:AAPL", "USDC") == "hyperliquid:xyz:USDC"

    def test_hyperliquid_alias_uses_perps_pool(self) -> None:
        assert margin_bucket_key("hyperliquid", "alias:gold", "USDC") == "hyperliquid:perps:USDC"

    def test_hyperliquid_default_quote_is_usdc(self) -> None:
        assert margin_bucket_key("hyperliquid", "BTC") == "hyperliquid:perps:USDC"

    def test_other_exchanges_unsuffixed(self) -> None:
        assert margin_bucket_key("lighter", "BTC") == "lighter"
        assert margin_bucket_key("lighter", "xyz:FOO") == "lighter"


# =============================================================================
# Integration с probe._get_market_info — assert не возвращает xyz пары
# =============================================================================


@pytest.mark.asyncio
async def test_get_market_info_skips_xyz_when_searching_crypto() -> None:
    """`_get_market_info(client, "hyperliquid", "AAPL")` НЕ должен вернуть
    `xyz:AAPL` markets. Только обычный crypto AAPL (если был бы) или
    `RuntimeError("market not found")`.
    """
    fake_markets = [
        {"baseSymbol": "BTC", "exchange": "hyperliquid", "id": "0", "price": "76000"},
        {"baseSymbol": "xyz:AAPL", "exchange": "hyperliquid", "id": "200", "price": "180"},
        {"baseSymbol": "xyz:CORN", "exchange": "hyperliquid", "id": "201", "price": "5"},
    ]
    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock(return_value=None)
    fake_resp.json = MagicMock(return_value=fake_markets)

    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)

    # Поиск крипто `BTC` — должно вернуть BTC.
    btc = await _get_market_info(fake_client, "hyperliquid", "BTC")
    assert btc["baseSymbol"] == "BTC"

    # Поиск `AAPL` (без префикса) — НЕ должно вернуть `xyz:AAPL`.
    with pytest.raises(RuntimeError, match="not found"):
        await _get_market_info(fake_client, "hyperliquid", "AAPL")

    # А вот явный `xyz:AAPL` — допустим, найдёт его (probe для stocks).
    aapl_xyz = await _get_market_info(fake_client, "hyperliquid", "xyz:AAPL")
    assert aapl_xyz["baseSymbol"] == "xyz:AAPL"


# =============================================================================
# Integration с live API — если есть VOOI_BEARER_TOKEN, реально проверяем
# =============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("VOOI_BEARER_TOKEN"), reason="needs live bearer token",
)
@pytest.mark.asyncio
async def test_integration_hl_has_xyz_markets() -> None:
    """LIVE: подтвердить что HL действительно содержит xyz:* markets и они
    исключаются `is_crypto_perps_market`.

    На дату QA (2026-04-30): 298 markets total, из них 68 xyz:*.
    Если число поменяется — ОК, тест проверяет лишь факт наличия и работу фильтра.
    """
    base_url = os.environ.get("VOOI_API_BASE_URL", "https://perps-api.vooi.io")
    token = os.environ["VOOI_BEARER_TOKEN"]
    async with ProbeClient(base_url, token) as client:
        resp = await client.get("/exchange/markets", params={"exchanges": "hyperliquid"})
        assert resp.status_code == 200
        markets = resp.json()
        assert isinstance(markets, list)
        assert len(markets) > 100, "HL обычно >100 markets"

        xyz_markets = [m for m in markets if (m.get("baseSymbol") or "").startswith("xyz:")]
        assert len(xyz_markets) > 0, "ожидали хотя бы 1 xyz: market на HL"

        crypto_only = filter_crypto_perps(markets)
        assert len(crypto_only) < len(markets)
        for m in crypto_only:
            assert not (m.get("baseSymbol") or "").startswith("xyz:")
            assert not (m.get("baseSymbol") or "").startswith("alias:")
