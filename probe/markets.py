"""Market classification utilities.

Funding-arb bot работает **только с crypto-perps**. На Hyperliquid (и потенциально
других биржах) есть отдельный класс инструментов:

- `xyz:*` — stocks/commodities (`xyz:AAPL`, `xyz:BRENTOIL`, `xyz:CORN`, ...).
  На уровне аккаунта это отдельный sub-account `account.type == "xyz"`
  с собственным изолированным margin pool. Нельзя открывать crypto-позиции
  под xyz-маржой и наоборот.
- `alias:*` — синтетические/альтернативные пары (например `alias:gold`).
  Опасно использовать для funding-arb: спред-логика, fundingInterval и
  liquidationPrice могут отличаться от обычных perps.

Эмпирически (см. `docs/api-probe-results.md`):

    HL: 298 markets total = 230 crypto-perps + 68 `xyz:*`
    accounts: type=perps (~$34) — для крипто; type=xyz (~$0) — для стоков.

Эти константы и хелперы — единый source of truth для probe (и будущего
`fundbot/strategy/filter.py` в Phase 1, см. C9 в plan).
"""

from __future__ import annotations

from typing import Any, Final

# Префиксы baseSymbol (с двоеточием), которые **должны** быть исключены из
# crypto-arb сканирования. Расширяемо: при появлении новых классов добавляем сюда.
NON_CRYPTO_PERPS_BASE_PREFIXES: Final[tuple[str, ...]] = ("xyz:", "alias:")

# Тип account, используемый для crypto perps (default).
CRYPTO_PERPS_ACCOUNT_TYPE: Final[str] = "perps"

# Префикс baseSymbol → `account.type` в VOOI GET /exchange/accounts.
# `alias:*` — кросс-биржевые алиасы на HL; маржа с основного perps-пула (как crypto-perps).
BASE_PREFIX_TO_ACCOUNT_TYPE: Final[dict[str, str]] = {
    "xyz:": "xyz",
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
    - `baseSymbol` начинается с `xyz:` (stocks/commodities, отдельный margin pool);
    - `baseSymbol` начинается с `alias:` (синтетика);
    - `baseSymbol` отсутствует / пустой;
    - `open == False` (рынок закрыт — для arb бесполезен).

    Не проверяет volume / leverage / прочие торговые ограничения — это уровень
    `strategy/filter.py` (Phase 1).
    """
    base = market.get("baseSymbol")
    if not isinstance(base, str) or not base:
        return False
    if is_non_crypto_prefix(base):
        return False
    # `open` иногда отсутствует — считаем default True; явное False → reject.
    return market.get("open") is not False


def expected_account_type_for_base_symbol(base_symbol: str) -> str:
    """Какой `account.type` нужно использовать для торговли этим baseSymbol.

    `xyz:AAPL` → `"xyz"`, `alias:gold` / `BTC` → `"perps"`.
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
