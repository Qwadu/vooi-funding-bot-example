"""Safety guards для probe-script.

ВСЕ probe writes должны проходить через эти guards.
Цель: гарантировать что probe не приведёт к неожиданному исполнению ордера.

Принципы:
1. Maximum notional per write = $PROBE_MAX_NOTIONAL_USD (default $5).
2. LIMIT alo цена ДОЛЖНА быть DALEKO от рынка:
   - Для BUY (long): price < mid * (1 - SAFE_DISTANCE_PCT/100)  [низкая цена → не fill]
   - Для SELL (short): price > mid * (1 + SAFE_DISTANCE_PCT/100) [высокая цена → не fill]
3. Total notional накапливается per-run; превышение лимита → exception.
4. Любой call с timeInForce НЕ "alo" в probe-write → запрет (за исключением q7
   cross-margin test, который явно использует ioc/market — там guard не вызывается).
5. **BUG-04 fix**: размер ордера квантуется по `baseDecimals` биржи через
   `quantize_size_by_base_decimals` с `ROUND_DOWN` — никогда не превышаем
   заявленный notional.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Literal

SAFE_DISTANCE_PCT = Decimal("50")  # цена ставится на 50% ниже/выше mid → точно не fill


class ProbeSafetyError(Exception):
    """Raised when probe operation would violate safety constraints."""


class SafetyBudget:
    def __init__(self, max_total_notional_usd: Decimal) -> None:
        self.max_total = max_total_notional_usd
        self.spent = Decimal("0")

    def reserve(self, notional_usd: Decimal) -> None:
        if self.spent + notional_usd > self.max_total:
            raise ProbeSafetyError(
                f"Probe budget exceeded: spent={self.spent}, "
                f"requesting={notional_usd}, max={self.max_total}",
            )
        self.spent += notional_usd


def safe_limit_price(mid_price: Decimal, side: Literal["buy", "sell"]) -> Decimal:
    """Компонует limit-цену DALEKO от mid, гарантируя maker-only behavior.

    BUY (long): далёкая низкая цена → не fill, висит в книге.
    SELL (short): далёкая высокая цена → не fill, висит в книге.
    """
    factor = SAFE_DISTANCE_PCT / Decimal("100")
    if side == "buy":
        return mid_price * (Decimal("1") - factor)
    return mid_price * (Decimal("1") + factor)


def quantize_price(price: Decimal, price_decimals: int) -> Decimal:
    """Округление цены до tick биржи (priceDecimals).

    Для probe LIMIT-ордеров знак направления и так гарантирует maker-only,
    округление просто чтобы биржа не отклонила по тиксайзу.
    """
    if price_decimals < 0:
        raise ProbeSafetyError(f"invalid price_decimals: {price_decimals}")
    step = Decimal(1).scaleb(-price_decimals)
    return price.quantize(step, rounding=ROUND_DOWN)


def quantize_size_by_base_decimals(
    notional_usd: Decimal,
    mid_price: Decimal,
    base_decimals: int,
) -> Decimal:
    """**BUG-04 fix**: квантуем base size по `baseDecimals` биржи с `ROUND_DOWN`.

    `ROUND_DOWN` гарантирует что effective notional = quantized_size × mid
    НЕ превысит requested notional. Это закрывает дыру в SafetyBudget,
    где hardcoded `quantize(Decimal("0.0001"))` мог округлить вверх и
    создать ордер на $7.6 при заявленном cap $5.

    Edge cases:
    - mid_price <= 0 → ProbeSafetyError (нет цены — нет ордера).
    - notional / mid < step → ProbeSafetyError (требуется minimum step,
      выбираем не молча округлять до step и тем самым превышать notional).
    """
    if mid_price <= 0:
        raise ProbeSafetyError(f"invalid mid_price: {mid_price}")
    if base_decimals < 0:
        raise ProbeSafetyError(f"invalid base_decimals: {base_decimals}")

    step = Decimal(1).scaleb(-base_decimals)  # 10^-baseDecimals
    raw = notional_usd / mid_price
    if raw < step:
        raise ProbeSafetyError(
            f"notional too small: $\u200b{notional_usd} / ${mid_price} = {raw} < tick {step}; "
            f"increase PROBE_MAX_NOTIONAL_USD",
        )
    return raw.quantize(step, rounding=ROUND_DOWN)


def assert_alo(time_in_force: str) -> None:
    """**§4.3 fix**: вызывать с РЕАЛЬНЫМ time_in_force из тела ордера.

    Раньше вызывалось как `assert_alo("alo")` — литерал, который никогда
    не триггерил исключение. Теперь все Q-write должны передавать
    `order_body["timeInForce"]`.
    """
    if time_in_force != "alo":
        raise ProbeSafetyError(
            f"probe writes must use timeInForce='alo' (post-only), got {time_in_force!r}",
        )


def assert_small_notional(notional_usd: Decimal, max_usd: Decimal) -> None:
    if notional_usd > max_usd:
        raise ProbeSafetyError(
            f"probe notional {notional_usd} exceeds per-call cap {max_usd}",
        )


def assert_effective_notional(
    quantized_size: Decimal,
    mid_price: Decimal,
    max_usd: Decimal,
) -> None:
    """**BUG-04 fix**: дополнительная страховка — проверяем effective notional
    (size × mid) ПОСЛЕ квантования. С `ROUND_DOWN` это всегда ≤ заявленного,
    но guard явный, для документации и защиты от будущих изменений.
    """
    effective = quantized_size * mid_price
    if effective > max_usd:
        raise ProbeSafetyError(
            f"effective notional after quantize ({effective}) exceeds cap ({max_usd}); "
            f"probable rounding mistake (size={quantized_size}, mid={mid_price})",
        )
