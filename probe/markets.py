"""Market classification utilities.

Funding-arb bot defaults to **crypto-perps only**. Hyperliquid (and potentially
other venues) also list non-crypto instruments through HIP-3 dexes, distinguished
by a `baseSymbol` prefix:

- `xyz:*` — equities / commodities / FX (e.g. `xyz:AAPL`, `xyz:BRENTOIL`,
  `xyz:HYUNDAI`, `xyz:PALLADIUM`).
- `alias:*` — synthetic / aliased pairs that resolve to a different
  `baseSymbol` on the venue side (e.g. `alias:gold` → `xyz:GOLD`).
- `km:*` — additional HIP-3 alias namespace used for some equities
  (e.g. `km:PLTR`).

These markets are filtered out of crypto-only scanning by `is_crypto_perps_market`.
Set `BOT_INCLUDE_HL_NON_CRYPTO=true` to also consider them.

Margin pool on the current VOOI API (verified live 2026-05-22 against
`https://perps-api.vooi.io`; swagger UI: https://perps-api.vooi.io/docs):

    GET /exchange/accounts?exchanges=hyperliquid
        → only `type="spot"` records, one per `token` (USDC, USDH).
          There is no separate `type="xyz"` bucket.

    GET /exchange/markets?exchanges=hyperliquid
        → every `xyz:*` market reports `quoteSymbol="USDC"`, with a
          `marginTiers` structure identical to crypto-perps. No
          per-market `marginToken` / `marginGroup` field.

    GET /exchange/quotes?exchanges=hyperliquid&asset=xyz:HYUNDAI&...
        → 200 OK with a valid `liquidationPrice` / `baseSize` — the API
          accepts xyz orders against the shared USDC margin pool.

In other words: xyz/alias/km legs on Hyperliquid share the same USDC
margin pool as crypto-perps. `margin_bucket_key()` therefore routes
all prefixes to the same `hyperliquid:perps:<quote>` bucket that
`fetch_accounts` actually populates.

This module is the single source of truth for prefix → bucket routing
used by both `fundbot/mvp.py` (opener) and `probe/`.
"""

from __future__ import annotations

from typing import Any, Final

# baseSymbol prefixes (with the trailing colon) that are NOT plain crypto-perps.
# `is_crypto_perps_market` rejects them from crypto-only scanning; when
# `BOT_INCLUDE_HL_NON_CRYPTO=true`, the opener routes them to the shared
# perps margin bucket (see `BASE_PREFIX_TO_ACCOUNT_TYPE` below).
NON_CRYPTO_PERPS_BASE_PREFIXES: Final[tuple[str, ...]] = ("xyz:", "alias:")

# `account.type` for the crypto-perps margin pool (default).
CRYPTO_PERPS_ACCOUNT_TYPE: Final[str] = "perps"

# baseSymbol prefix → `account.type` used to build the margin-bucket key.
# All currently-known HL prefixes (xyz, alias) share the same USDC margin
# pool as crypto-perps — see the module docstring for the live evidence.
BASE_PREFIX_TO_ACCOUNT_TYPE: Final[dict[str, str]] = {
    "xyz:": CRYPTO_PERPS_ACCOUNT_TYPE,
    "alias:": CRYPTO_PERPS_ACCOUNT_TYPE,
}


def is_non_crypto_prefix(base_symbol: str | None) -> bool:
    """True если baseSymbol начинается с любого из не-crypto префиксов."""
    if not base_symbol:
        return False
    return any(base_symbol.startswith(p) for p in NON_CRYPTO_PERPS_BASE_PREFIXES)


def is_crypto_perps_market(market: dict[str, Any]) -> bool:
    """True если market — обычная crypto-perps пара, пригодная для funding-arb.

    False для:
    - `baseSymbol` начинается с `xyz:` (HIP-3 equities/commodities;
      shared USDC pool but excluded from crypto-only scanning by default);
    - `baseSymbol` начинается с `alias:` (синтетика);
    - `baseSymbol` отсутствует / пустой;
    - `open == False` (рынок закрыт — для arb бесполезен).

    Не проверяет volume / leverage / прочие торговые ограничения.
    """
    base = market.get("baseSymbol")
    if not isinstance(base, str) or not base:
        return False
    if is_non_crypto_prefix(base):
        return False
    # `open` иногда отсутствует — считаем default True; явное False → reject.
    return market.get("open") is not False


def expected_account_type_for_base_symbol(base_symbol: str) -> str:
    """Какой `account.type` использовать для торговли этим baseSymbol.

    All known prefixes on Hyperliquid (`xyz:`, `alias:`) share the crypto-perps
    USDC pool, so this returns ``"perps"`` for every input today. See the
    module docstring for the empirical reasoning.
    """
    for prefix, acct_type in BASE_PREFIX_TO_ACCOUNT_TYPE.items():
        if base_symbol.startswith(prefix):
            return acct_type
    return CRYPTO_PERPS_ACCOUNT_TYPE


def margin_bucket_key(exchange: str, base_symbol: str, quote_symbol: str = "USDC") -> str:
    """Ключ пула маржи для капов (совпадает с ключами из ``fetch_accounts`` MVP).

    Hyperliquid: ``hyperliquid:{acct_type}:{token}`` — тип аккаунта по префиксу
    ``baseSymbol``, токен по ``quoteSymbol`` маркета (USDC vs USDH).
    Остальные биржи — одно имя биржи (как в ``target_exchanges``).
    """
    ex = (exchange or "").strip().lower()
    if ex == "hyperliquid":
        acct_type = expected_account_type_for_base_symbol(base_symbol)
        return f"hyperliquid:{acct_type}:{quote_symbol}"
    return ex


def filter_crypto_perps(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Удобный helper для фильтрации списка markets."""
    return [m for m in markets if is_crypto_perps_market(m)]
