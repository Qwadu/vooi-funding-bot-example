"""Phase 0 — 11 эмпирических вопросов к VOOI API.

Каждая функция = один subcommand в probe/probe.py. Все функции async, принимают
ProbeClient + ProbeLogger, возвращают dict с findings (для записи в api-probe-results.md).

Безопасность: все mutating-вызовы идут через probe.safety guards, размер ордера
квантуется по `baseDecimals` биржи через `quantize_size_by_base_decimals` с
`ROUND_DOWN` (BUG-04 fix), что исключает превышение заявленного notional cap.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx

from probe.client import ProbeClient, summarize_response
from probe.log import ProbeLogger
from probe.markets import is_crypto_perps_market, is_non_crypto_prefix
from probe.safety import (
    SafetyBudget,
    assert_alo,
    assert_effective_notional,
    assert_small_notional,
    quantize_price,
    quantize_size_by_base_decimals,
    safe_limit_price,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_PRICE_DECIMALS = 2  # fallback если у market нет priceDecimals


def _coid(suffix: str = "") -> str:
    """Probe clientOrderId — отдельный namespace, чтобы не пересекаться с production."""
    return f"vooi-funding-arb-probe-{uuid.uuid4().hex[:12]}{suffix}"


async def _get_market_info(
    client: ProbeClient,
    exchange: str,
    asset: str,
) -> dict[str, Any]:
    """Достаёт текущий market price, decimals и пр. для asset на exchange.

    Поведение:
    - Если `asset` имеет non-crypto префикс (`xyz:`, `alias:`) — ищем точное
      совпадение, ничего не фильтруем.
    - Если `asset` — обычный crypto символ (`BTC`/`ETH`/...) — **отбрасываем**
      все `xyz:*` / `alias:*` markets, чтобы не ошибиться: `BTC` ≠ `xyz:BTC`
      (последний вообще не существует, но логика защищает от случайных
      коллизий, например `AAPL` vs `xyz:AAPL` если когда-нибудь биржа
      добавит крипто-токен `AAPL`).

    BUG-08 fix: явный cast возвращаемого dict, чтобы mypy --strict не ругался
    на no-any-return от `markets[i]` (который Any из json()).
    """
    resp = await client.get("/exchange/markets", params={"exchanges": exchange})
    resp.raise_for_status()
    markets = resp.json()

    asset_upper = asset.upper()
    asset_is_non_crypto = is_non_crypto_prefix(asset)

    for m in markets:
        if m.get("exchange") != exchange:
            continue
        base = m.get("baseSymbol") or ""
        # Если ищем crypto, пропускаем xyz/alias markets явно.
        if not asset_is_non_crypto and is_non_crypto_prefix(base):
            continue
        # Точное совпадение по baseSymbol (case-insensitive).
        if base.upper() == asset_upper:
            return cast("dict[str, Any]", m)
        # Fallback: id-prefix (только для crypto, чтобы не зацепить чужой класс).
        if (
            not asset_is_non_crypto
            and (m.get("id") or "").upper().startswith(asset_upper)
            and is_crypto_perps_market(m)
        ):
            return cast("dict[str, Any]", m)
    raise RuntimeError(f"market not found: {exchange}/{asset} (crypto-perps only)")


def _extract_decimals(market: dict[str, Any]) -> tuple[int, int]:
    """Достаёт (baseDecimals, priceDecimals) с разумными fallback."""
    base_decimals_raw = market.get("baseDecimals")
    price_decimals_raw = market.get("priceDecimals")
    base_decimals = int(base_decimals_raw) if base_decimals_raw is not None else 4
    price_decimals = (
        int(price_decimals_raw) if price_decimals_raw is not None else DEFAULT_PRICE_DECIMALS
    )
    return base_decimals, price_decimals


# ---------------------------------------------------------------------------
# Q1. Формат longAsset/shortAsset в /funding-strategies/spread-chart  (OQ §14.1)
# ---------------------------------------------------------------------------


async def q1_spread_chart_format(
    client: ProbeClient,
    log: ProbeLogger,
    *,
    long_exchange: str = "hyperliquid",
    short_exchange: str = "lighter",
    window_days: int = 5,
) -> dict[str, Any]:
    """BUG-01 fix: rolling date window вместо hardcoded 2026-04-25..04-30.

    §4.5: long/short exchange — параметры. Для bot-5 (aster+lighter) probe.py CLI
    прокидывает `--long-exchange aster --short-exchange lighter`.
    """
    today = date.today()
    from_date = (today - timedelta(days=window_days)).isoformat()
    to_date = today.isoformat()

    log.log(
        "Q1_START",
        what="spread-chart longAsset/shortAsset format",
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        from_date=from_date,
        to_date=to_date,
    )

    candidates = ["BTC", "BTCUSDT", "BTCUSDC", "alias:gold"]
    findings: dict[str, Any] = {
        "_meta": {
            "long_exchange": long_exchange,
            "short_exchange": short_exchange,
            "from_date": from_date,
            "to_date": to_date,
        },
    }

    for fmt in candidates:
        params = {
            "longExchange": long_exchange,
            "longAsset": fmt,
            "shortExchange": short_exchange,
            "shortAsset": fmt,
            "fromDate": from_date,
            "toDate": to_date,
        }
        try:
            resp = await client.get("/funding-strategies/spread-chart", params=params)
            summary = summarize_response(resp)
            non_empty = (
                resp.status_code == 200
                and isinstance(resp.json(), list)
                and len(resp.json()) > 0
            )
        except (httpx.HTTPError, ValueError, KeyError) as e:
            summary = {"error": str(e)}
            non_empty = False

        findings[fmt] = {
            "status": summary.get("status"),
            "non_empty": non_empty,
            "summary": summary,
        }
        log.log("Q1_PROBE", format=fmt, status=summary.get("status"), non_empty=non_empty)

    log.write_artifact("q1_spread_chart_format.json", findings)
    log.log("Q1_DONE", findings=findings)
    return findings


# ---------------------------------------------------------------------------
# Q2. SSE latency для orderId  (OQ §14.4)
# ---------------------------------------------------------------------------


async def _watch_sse_for_coid(
    client: ProbeClient,
    *,
    exchange: str,
    client_order_id: str,
) -> tuple[float | None, str | None]:
    """§4.4 fix: явная функция вместо closure-capture в loop. Возвращает
    `(seen_event_at_monotonic, order_id_from_sse)` либо `(None, None)` если
    стрим закрылся раньше.
    """
    async with client.sse(
        "/exchange/updates",
        params={"exchanges": exchange},
    ) as evt:
        async for event in evt.aiter_sse():
            if event.event != "order":
                continue
            try:
                payload = event.json()
            except (ValueError, KeyError, TypeError):
                continue
            items = payload if isinstance(payload, list) else [payload]
            for o in items:
                if o.get("clientOrderId") == client_order_id:
                    return time.monotonic(), o.get("orderId") or o.get("id")
    return None, None


async def q2_sse_latency(
    client: ProbeClient,
    log: ProbeLogger,
    budget: SafetyBudget,
    *,
    exchanges: list[str],
    asset: str,
    notional_usd: Decimal,
    max_per_call_usd: Decimal,
) -> dict[str, Any]:
    """Для каждой exchange:

    1. Открываем SSE.
    2. POST LIMIT alo $notional далеко от рынка (BUG-04: размер квантован по baseDecimals).
    3. Засекаем время до прихода `order` event с присвоенным `orderId`.
    4. Cancel ордера для очистки.
    """
    log.log(
        "Q2_START",
        what="SSE latency для orderId",
        exchanges=exchanges,
        notional_usd=str(notional_usd),
    )
    results: dict[str, Any] = {}

    for exchange in exchanges:
        runs: list[dict[str, Any]] = []
        for run_idx in range(3):
            client_order_id = _coid(f"-q2-{exchange}-{run_idx}")
            market = await _get_market_info(client, exchange, asset)
            mid_price = Decimal(str(market.get("price", "0")))
            if mid_price <= 0:
                log.log("Q2_SKIP", exchange=exchange, reason="invalid mid price", market=market)
                continue
            base_decimals, price_decimals = _extract_decimals(market)

            limit_price_raw = safe_limit_price(mid_price, "buy")
            limit_price = quantize_price(limit_price_raw, price_decimals)
            base_size = quantize_size_by_base_decimals(notional_usd, mid_price, base_decimals)

            assert_small_notional(notional_usd, max_per_call_usd)
            assert_effective_notional(base_size, mid_price, max_per_call_usd)
            budget.reserve(notional_usd)

            order_body: dict[str, Any] = {
                "exchange": exchange,
                "asset": asset,
                "side": "buy",
                "size": str(base_size),
                "price": str(limit_price),
                "timeInForce": "alo",
                "reduceOnly": False,
                "clientOrderId": client_order_id,
            }
            assert_alo(order_body["timeInForce"])

            log.log(
                "Q2_POST",
                exchange=exchange,
                run=run_idx,
                client_order_id=client_order_id,
                limit_price=str(limit_price),
                size=str(base_size),
                base_decimals=base_decimals,
                effective_notional_usd=str(base_size * mid_price),
            )

            sse_task = asyncio.create_task(
                _watch_sse_for_coid(
                    client,
                    exchange=exchange,
                    client_order_id=client_order_id,
                ),
            )
            await asyncio.sleep(0.5)  # дать SSE время подключиться

            t_post: float | None = None
            try:
                t_post = time.monotonic()
                resp = await client.post("/exchange/orders", body=order_body)
                summary = summarize_response(resp)
                log.log("Q2_POST_RESPONSE", run=run_idx, summary=summary)
            except (httpx.HTTPError, ValueError) as e:
                log.log("Q2_POST_ERROR", run=run_idx, error=str(e))
                sse_task.cancel()
                continue

            seen_event_at: float | None = None
            order_id_from_sse: str | None = None
            try:
                seen_event_at, order_id_from_sse = await asyncio.wait_for(
                    sse_task,
                    timeout=10.0,
                )
            except TimeoutError:
                log.log("Q2_SSE_TIMEOUT", run=run_idx)
                sse_task.cancel()

            latency_ms = None
            if seen_event_at is not None and t_post is not None:
                latency_ms = (seen_event_at - t_post) * 1000

            runs.append(
                {
                    "run": run_idx,
                    "latency_ms": latency_ms,
                    "order_id_from_sse": order_id_from_sse,
                    "client_order_id": client_order_id,
                },
            )
            log.log("Q2_RUN_DONE", exchange=exchange, run=run_idx, latency_ms=latency_ms)

            if order_id_from_sse:
                try:
                    cancel_body = {
                        "exchange": exchange,
                        "asset": asset,
                        "orderId": order_id_from_sse,
                    }
                    cancel_resp = await client.delete("/exchange/orders", body=cancel_body)
                    log.log("Q2_CANCEL", run=run_idx, summary=summarize_response(cancel_resp))
                except httpx.HTTPError as e:
                    log.log("Q2_CANCEL_ERROR", run=run_idx, error=str(e))

            await asyncio.sleep(2.0)

        latencies = [r["latency_ms"] for r in runs if r["latency_ms"] is not None]
        median = sorted(latencies)[len(latencies) // 2] if latencies else None
        results[exchange] = {"runs": runs, "median_ms": median}
        log.log("Q2_EXCHANGE_DONE", exchange=exchange, median_ms=median)

    log.write_artifact("q2_sse_latency.json", results)
    log.log("Q2_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q3. alo + stopLoss (bracket)  (OQ §14.2 / §14.3)
# ---------------------------------------------------------------------------


async def q3_alo_bracket(
    client: ProbeClient,
    log: ProbeLogger,
    budget: SafetyBudget,
    *,
    exchanges: list[str],
    asset: str,
    notional_usd: Decimal,
    max_per_call_usd: Decimal,
) -> dict[str, Any]:
    log.log("Q3_START", what="alo + stopLoss conflict")
    results: dict[str, Any] = {}

    for exchange in exchanges:
        market = await _get_market_info(client, exchange, asset)
        mid_price = Decimal(str(market.get("price", "0")))
        if mid_price <= 0:
            results[exchange] = {"error": "invalid mid price"}
            continue
        base_decimals, price_decimals = _extract_decimals(market)

        limit_price = quantize_price(safe_limit_price(mid_price, "buy"), price_decimals)
        sl_trigger = quantize_price(mid_price * Decimal("0.5"), price_decimals)
        base_size = quantize_size_by_base_decimals(notional_usd, mid_price, base_decimals)

        assert_small_notional(notional_usd, max_per_call_usd)
        assert_effective_notional(base_size, mid_price, max_per_call_usd)
        budget.reserve(notional_usd)

        client_order_id = _coid(f"-q3-{exchange}")
        order_body: dict[str, Any] = {
            "exchange": exchange,
            "asset": asset,
            "side": "buy",
            "size": str(base_size),
            "price": str(limit_price),
            "timeInForce": "alo",
            "reduceOnly": False,
            "clientOrderId": client_order_id,
            "stopLoss": {"triggerPrice": str(sl_trigger)},
        }
        assert_alo(order_body["timeInForce"])

        log.log("Q3_POST", exchange=exchange, body_summary={"alo": True, "bracket": True})
        try:
            resp = await client.post("/exchange/orders", body=order_body)
            summary = summarize_response(resp)
            results[exchange] = {
                "accepted": resp.status_code == 200,
                "status": resp.status_code,
                "summary": summary,
                "client_order_id": client_order_id,
            }
            log.log(
                "Q3_RESPONSE",
                exchange=exchange,
                accepted=resp.status_code == 200,
                summary=summary,
            )
        except httpx.HTTPError as e:
            results[exchange] = {"accepted": False, "error": str(e)}
            log.log("Q3_ERROR", exchange=exchange, error=str(e))

        await asyncio.sleep(1.5)
        try:
            open_resp = await client.get("/exchange/open-orders", params={"exchanges": exchange})
            for o in open_resp.json() or []:
                if o.get("clientOrderId") == client_order_id:
                    await client.delete(
                        "/exchange/orders",
                        body={
                            "exchange": exchange,
                            "asset": asset,
                            "orderId": o.get("orderId") or o.get("id"),
                        },
                    )
        except httpx.HTTPError as e:
            log.log("Q3_CANCEL_ERROR", exchange=exchange, error=str(e))

    log.write_artifact("q3_alo_bracket.json", results)
    log.log("Q3_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q4. clientOrderId idempotency  (OQ §14.5)
# ---------------------------------------------------------------------------


async def q4_clientorderid_idempotent(
    client: ProbeClient,
    log: ProbeLogger,
    budget: SafetyBudget,
    *,
    exchanges: list[str],
    asset: str,
    notional_usd: Decimal,
    max_per_call_usd: Decimal,
) -> dict[str, Any]:
    """BUG-07 fix: budget.reserve() вызывается ПЕРЕД каждым POST, чтобы
    pessimistically учесть случай non-idempotent биржи (2 ордера на 2× notional).
    """
    log.log("Q4_START", what="clientOrderId idempotency")
    results: dict[str, Any] = {}

    for exchange in exchanges:
        market = await _get_market_info(client, exchange, asset)
        mid_price = Decimal(str(market.get("price", "0")))
        if mid_price <= 0:
            results[exchange] = {"error": "invalid mid price"}
            continue
        base_decimals, price_decimals = _extract_decimals(market)

        limit_price = quantize_price(safe_limit_price(mid_price, "buy"), price_decimals)
        base_size = quantize_size_by_base_decimals(notional_usd, mid_price, base_decimals)
        client_order_id = _coid(f"-q4-{exchange}")

        assert_small_notional(notional_usd, max_per_call_usd)
        assert_effective_notional(base_size, mid_price, max_per_call_usd)

        order_body: dict[str, Any] = {
            "exchange": exchange,
            "asset": asset,
            "side": "buy",
            "size": str(base_size),
            "price": str(limit_price),
            "timeInForce": "alo",
            "reduceOnly": False,
            "clientOrderId": client_order_id,
        }
        assert_alo(order_body["timeInForce"])

        budget.reserve(notional_usd)
        log.log("Q4_POST_1", exchange=exchange, client_order_id=client_order_id)
        resp1 = await client.post("/exchange/orders", body=order_body)
        s1 = summarize_response(resp1)

        await asyncio.sleep(0.5)
        budget.reserve(notional_usd)  # BUG-07 fix: pessimistic reserve если биржа non-idempotent
        log.log("Q4_POST_2_RETRY_SAME_COID", exchange=exchange)
        resp2 = await client.post("/exchange/orders", body=order_body)
        s2 = summarize_response(resp2)

        await asyncio.sleep(1.0)
        open_resp = await client.get("/exchange/open-orders", params={"exchanges": exchange})
        matching = [
            o
            for o in (open_resp.json() or [])
            if o.get("clientOrderId") == client_order_id
        ]

        results[exchange] = {
            "post1_status": s1.get("status"),
            "post2_status": s2.get("status"),
            "open_orders_with_same_coid": len(matching),
            "interpretation": (
                "duplicate" if len(matching) > 1
                else "idempotent" if len(matching) == 1
                else "unknown"
            ),
        }
        log.log("Q4_RESULT", exchange=exchange, **results[exchange])

        for o in matching:
            try:
                await client.delete(
                    "/exchange/orders",
                    body={
                        "exchange": exchange,
                        "asset": asset,
                        "orderId": o.get("orderId") or o.get("id"),
                    },
                )
            except httpx.HTTPError as e:
                log.log("Q4_CANCEL_ERROR", exchange=exchange, error=str(e))

    log.write_artifact("q4_clientorderid_idempotent.json", results)
    log.log("Q4_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q5. Bearer на SSE без token query  (OQ §14.7)
# ---------------------------------------------------------------------------


# Известные SSE event types сервера, которые означают "auth/validation failed"
# и должны интерпретироваться как СБОЙ Bearer-only flow, а не как успех (BUG-02).
_SSE_AUTH_REJECT_EVENTS = frozenset({"error"})


async def q5_bearer_sse(
    client: ProbeClient,
    log: ProbeLogger,
    *,
    target_exchanges: list[str],
) -> dict[str, Any]:
    """BUG-02 fix: если первый SSE event имеет `event: error` — это auth-rejection
    от сервера (тело "Validation failed"), а не keep-alive. Bearer-only flow
    при этом считается НЕ-работающим, и в production обязателен `?token=` reconnect-loop.
    """
    log.log("Q5_START", what="Bearer на SSE без ?token=")
    results: dict[str, Any] = {}
    params = {"exchanges": ",".join(target_exchanges)}

    bearer_works = False
    bearer_first_event: str | None = None
    bearer_error_body: str | None = None
    bearer_error: str | None = None
    try:
        async with client.sse("/exchange/updates", params=params, use_bearer=True) as evt:
            try:
                event = await asyncio.wait_for(evt.aiter_sse().__anext__(), timeout=15.0)
                bearer_first_event = event.event
                if event.event in _SSE_AUTH_REJECT_EVENTS:
                    bearer_works = False
                    bearer_error_body = (event.data or "")[:300]
                    bearer_error = (
                        f"server returned event:{event.event} (auth-rejection): "
                        f"{bearer_error_body!r}"
                    )
                    log.log(
                        "Q5_BEARER_REJECTED",
                        sse_event=event.event,
                        data_preview=bearer_error_body,
                    )
                else:
                    bearer_works = True
                    log.log("Q5_BEARER_FIRST_EVENT", sse_event=event.event)
            except TimeoutError:
                bearer_works = True  # connection OK, просто нет events за 15s
                bearer_first_event = "no_events_within_15s"
                log.log(
                    "Q5_BEARER_TIMEOUT",
                    interpretation="stream_opened_but_silent",
                )
    except httpx.HTTPStatusError as e:
        bearer_error = f"HTTP {e.response.status_code}"
        log.log(
            "Q5_BEARER_HTTP_ERROR",
            status=e.response.status_code,
            body_preview=(e.response.text or "")[:200],
        )
    except (httpx.HTTPError, asyncio.CancelledError, RuntimeError) as e:
        bearer_error = str(e)
        log.log("Q5_BEARER_EXCEPTION", error=str(e)[:300])

    results["bearer_only"] = {
        "stream_opened": bearer_works,
        "first_event": bearer_first_event,
        "error_body": bearer_error_body,
        "error": bearer_error,
    }
    log.log("Q5_BEARER", **results["bearer_only"])

    token_works: bool | None = None
    token_first_event: str | None = None
    try:
        token_resp = await client.post("/exchange/updates-token", body={})
        if token_resp.status_code == 200:
            body = token_resp.json()
            token = body.get("token") if isinstance(body, dict) else None
            if token:
                async with client.sse(
                    "/exchange/updates",
                    params={"exchanges": ",".join(target_exchanges)},
                    use_bearer=False,
                    token_query=token,
                ) as evt:
                    try:
                        event = await asyncio.wait_for(
                            evt.aiter_sse().__anext__(),
                            timeout=15.0,
                        )
                        token_first_event = event.event
                        token_works = event.event not in _SSE_AUTH_REJECT_EVENTS
                    except TimeoutError:
                        token_works = True
                        token_first_event = "no_events_within_15s"  # noqa: S105
        else:
            token_works = False
        results["token_query"] = {
            "endpoint_status": token_resp.status_code,
            "stream_opened": token_works,
            "first_event": token_first_event,
        }
    except (httpx.HTTPError, asyncio.CancelledError, RuntimeError) as e:
        results["token_query"] = {"error": str(e)}

    log.write_artifact("q5_bearer_sse.json", results)
    log.log("Q5_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q6. fundingInterval per market  (OQ §14.9)
# ---------------------------------------------------------------------------


async def q6_funding_interval(
    client: ProbeClient,
    log: ProbeLogger,
    *,
    exchanges: list[str],
) -> dict[str, Any]:
    """Отдельно репортим crypto vs xyz/alias counts (для C9 — hard-reject `xyz:`)."""
    log.log("Q6_START", what="fundingInterval per market")
    results: dict[str, Any] = {}

    for exchange in exchanges:
        resp = await client.get("/exchange/markets", params={"exchanges": exchange})
        markets = resp.json() if resp.status_code == 200 else []

        crypto_markets = [m for m in markets if is_crypto_perps_market(m)]
        non_crypto_markets = [
            m for m in markets if is_non_crypto_prefix(m.get("baseSymbol") or "")
        ]

        per_exchange: list[dict[str, Any]] = []
        for m in crypto_markets[:10]:
            per_exchange.append(
                {
                    "asset": m.get("baseSymbol") or m.get("id"),
                    "fundingInterval": m.get("fundingInterval"),
                    "nextFundingTime": m.get("nextFundingTime"),
                    "open": m.get("open"),
                },
            )

        non_crypto_sample = sorted(
            {m.get("baseSymbol") for m in non_crypto_markets if m.get("baseSymbol")},
        )[:10]

        results[exchange] = {
            "sampled_crypto": per_exchange,
            "total_markets": len(markets),
            "crypto_markets_count": len(crypto_markets),
            "non_crypto_markets_count": len(non_crypto_markets),
            "non_crypto_sample": non_crypto_sample,
        }
        log.log(
            "Q6_EXCHANGE",
            exchange=exchange,
            total_markets=len(markets),
            crypto_count=len(crypto_markets),
            non_crypto_count=len(non_crypto_markets),
        )

    log.write_artifact("q6_funding_interval.json", results)
    log.log("Q6_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q7. Cross-margin liquidationPrice drift  (C1, V10) — КРИТИЧНО
# ---------------------------------------------------------------------------


async def q7_cross_margin_drift(
    client: ProbeClient,
    log: ProbeLogger,
    budget: SafetyBudget,
    *,
    exchange: str,
    primary_asset: str,
    secondary_asset: str,
    leverage: int,
    notional_usd: Decimal,
    max_per_call_usd: Decimal,
) -> dict[str, Any]:
    """КРИТИЧНЫЙ тест C1.

    1. Открыть isolated $notional <primary> <leverage>×, прочитать
       position.liquidationPrice (real_iso).
    2. Сравнить с quotes(isolated) → predicted_iso.
    3. Закрыть.
    4. Открыть cross $notional <primary> <leverage>×, прочитать
       position.liquidationPrice (real_cross_only_primary).
    5. Открыть cross $notional <secondary> <leverage>× (вторая позиция на
       той же бирже).
    6. Прочитать position.liquidationPrice <primary> ещё раз
       (real_cross_after_secondary).
    7. Сравнить delta = (real_cross_after_secondary - real_cross_only_primary)
       / real_cross_only_primary.
    8. Закрыть обе.

    Этот тест требует МАРКЕТНОГО исполнения — это РЕАЛЬНЫЕ деньги
    из risk-капитала.
    """
    log.log(
        "Q7_START",
        what="cross-margin liquidationPrice drift",
        exchange=exchange,
        primary=primary_asset,
        secondary=secondary_asset,
        leverage=leverage,
        notional_usd=str(notional_usd),
        warning="THIS WILL OPEN REAL POSITIONS WITH MARKET EXECUTION",
    )

    findings: dict[str, Any] = {
        "predicted_iso": None,
        "real_iso": None,
        "predicted_cross_only_primary": None,
        "real_cross_only_primary": None,
        "real_cross_after_secondary": None,
        "delta_pct_primary_after_secondary": None,
        "iso_predicted_vs_real_pct": None,
    }

    assert_small_notional(notional_usd, max_per_call_usd)
    budget.reserve(notional_usd)

    try:
        await client.post(
            "/exchange/margin-mode",
            body={
                "exchange": exchange,
                "asset": primary_asset,
                "marginMode": "isolated",
            },
        )
        await client.post(
            "/exchange/leverage",
            body={
                "exchange": exchange,
                "asset": primary_asset,
                "leverage": leverage,
            },
        )
    except httpx.HTTPError as e:
        log.log("Q7_SETUP_ERROR", error=str(e))

    quotes_iso = await client.get(
        "/exchange/quotes",
        params={
            "asset": primary_asset,
            "exchanges": exchange,
            "leverage": leverage,
            "marginMode": "isolated",
            "side": "buy",
            "quoteSize": str(notional_usd),
        },
    )
    iso_predicted = None
    if quotes_iso.status_code == 200:
        items = quotes_iso.json()
        if items and isinstance(items, list):
            iso_predicted = items[0].get("quote", {}).get("liquidationPrice")
    findings["predicted_iso"] = iso_predicted
    log.log("Q7_QUOTES_ISO", liquidation_price=iso_predicted)

    coid_iso = _coid(f"-q7-iso-{primary_asset}")
    body: dict[str, Any] = {
        "exchange": exchange,
        "asset": primary_asset,
        "side": "buy",
        "size": "0",
        "quoteSize": str(notional_usd),
        "timeInForce": "ioc",
        "reduceOnly": False,
        "clientOrderId": coid_iso,
    }
    log.log("Q7_OPEN_ISO_MARKET", coid=coid_iso, warning="REAL MARKET ORDER")
    open_resp = await client.post("/exchange/orders", body=body)
    log.log("Q7_OPEN_ISO_RESP", summary=summarize_response(open_resp))

    await asyncio.sleep(3.0)
    pos_resp = await client.get("/exchange/positions", params={"exchanges": exchange})
    real_iso = None
    for p in pos_resp.json() or []:
        if (p.get("baseSymbol") == primary_asset.upper()) and p.get("marginMode") == "isolated":
            real_iso = p.get("liquidationPrice")
            break
    findings["real_iso"] = real_iso
    log.log("Q7_POSITION_ISO", liquidation_price=real_iso)

    if iso_predicted and real_iso:
        try:
            findings["iso_predicted_vs_real_pct"] = float(
                abs(Decimal(str(real_iso)) - Decimal(str(iso_predicted)))
                / Decimal(str(real_iso))
                * 100,
            )
        except (ValueError, ArithmeticError) as e:
            log.log("Q7_ISO_DELTA_CALC_ERROR", error=str(e))

    log.log("Q7_CLOSE_ISO_MARKET")
    close_iso_body: dict[str, Any] = {
        "exchange": exchange,
        "asset": primary_asset,
        "side": "sell",
        "size": "0",
        "quoteSize": str(notional_usd),
        "timeInForce": "ioc",
        "reduceOnly": True,
        "clientOrderId": _coid("-q7-iso-close"),
    }
    await client.post("/exchange/orders", body=close_iso_body)
    await asyncio.sleep(3.0)

    await client.post(
        "/exchange/margin-mode",
        body={
            "exchange": exchange,
            "asset": primary_asset,
            "marginMode": "cross",
        },
    )
    await client.post(
        "/exchange/margin-mode",
        body={
            "exchange": exchange,
            "asset": secondary_asset,
            "marginMode": "cross",
        },
    )

    budget.reserve(notional_usd)  # primary cross
    budget.reserve(notional_usd)  # secondary cross

    quotes_cross = await client.get(
        "/exchange/quotes",
        params={
            "asset": primary_asset,
            "exchanges": exchange,
            "leverage": leverage,
            "marginMode": "cross",
            "side": "buy",
            "quoteSize": str(notional_usd),
        },
    )
    cross_predicted = None
    if quotes_cross.status_code == 200:
        items = quotes_cross.json()
        if items and isinstance(items, list):
            cross_predicted = items[0].get("quote", {}).get("liquidationPrice")
    findings["predicted_cross_only_primary"] = cross_predicted

    coid_cross_p = _coid(f"-q7-cross-{primary_asset}")
    log.log("Q7_OPEN_CROSS_PRIMARY", coid=coid_cross_p, warning="REAL MARKET ORDER")
    await client.post(
        "/exchange/orders",
        body={
            "exchange": exchange,
            "asset": primary_asset,
            "side": "buy",
            "size": "0",
            "quoteSize": str(notional_usd),
            "timeInForce": "ioc",
            "reduceOnly": False,
            "clientOrderId": coid_cross_p,
        },
    )
    await asyncio.sleep(3.0)

    pos_resp_1 = await client.get("/exchange/positions", params={"exchanges": exchange})
    for p in pos_resp_1.json() or []:
        if p.get("baseSymbol") == primary_asset.upper() and p.get("marginMode") == "cross":
            findings["real_cross_only_primary"] = p.get("liquidationPrice")
            break
    log.log("Q7_POSITION_CROSS_PRIMARY", liq=findings["real_cross_only_primary"])

    coid_cross_s = _coid(f"-q7-cross-{secondary_asset}")
    log.log("Q7_OPEN_CROSS_SECONDARY", coid=coid_cross_s, warning="REAL MARKET ORDER")
    await client.post(
        "/exchange/orders",
        body={
            "exchange": exchange,
            "asset": secondary_asset,
            "side": "buy",
            "size": "0",
            "quoteSize": str(notional_usd),
            "timeInForce": "ioc",
            "reduceOnly": False,
            "clientOrderId": coid_cross_s,
        },
    )
    await asyncio.sleep(3.0)

    pos_resp_2 = await client.get("/exchange/positions", params={"exchanges": exchange})
    for p in pos_resp_2.json() or []:
        if p.get("baseSymbol") == primary_asset.upper() and p.get("marginMode") == "cross":
            findings["real_cross_after_secondary"] = p.get("liquidationPrice")
            break
    log.log(
        "Q7_POSITION_CROSS_AFTER_SECONDARY",
        liq=findings["real_cross_after_secondary"],
    )

    try:
        a = Decimal(str(findings["real_cross_only_primary"]))
        b = Decimal(str(findings["real_cross_after_secondary"]))
        if a > 0:
            findings["delta_pct_primary_after_secondary"] = float(
                abs(b - a) / a * 100,
            )
    except (ValueError, ArithmeticError) as e:
        log.log("Q7_DRIFT_CALC_ERROR", error=str(e))

    for asset_to_close, coid_suffix in [
        (primary_asset, "primary"),
        (secondary_asset, "secondary"),
    ]:
        log.log("Q7_CLOSE_CROSS", asset=asset_to_close)
        await client.post(
            "/exchange/orders",
            body={
                "exchange": exchange,
                "asset": asset_to_close,
                "side": "sell",
                "size": "0",
                "quoteSize": str(notional_usd),
                "timeInForce": "ioc",
                "reduceOnly": True,
                "clientOrderId": _coid(f"-q7-close-{coid_suffix}"),
            },
        )
        await asyncio.sleep(2.0)

    log.write_artifact("q7_cross_margin_drift.json", findings)
    log.log("Q7_DONE", findings=findings)
    return findings


# ---------------------------------------------------------------------------
# Q8. 5xx behavior на POST /exchange/orders  (C2)
# ---------------------------------------------------------------------------


async def q8_5xx_behavior(
    client: ProbeClient,
    log: ProbeLogger,
    budget: SafetyBudget,
    *,
    exchanges: list[str],
    asset: str,
    notional_usd: Decimal,
    max_per_call_usd: Decimal,
) -> dict[str, Any]:
    """Этот тест НЕ может быть полностью автоматизирован — нужно вручную
    отрубить сеть во время POST.

    Как наполовину-автоматизированный: ставим короткий timeout (0.1 сек),
    смотрим что произойдёт когда timeout сработает.
    """
    log.log(
        "Q8_START",
        what="5xx behavior + double-order risk",
        note="manual VPN kill required for full test",
    )
    results: dict[str, Any] = {}

    for exchange in exchanges:
        market = await _get_market_info(client, exchange, asset)
        mid_price = Decimal(str(market.get("price", "0")))
        if mid_price <= 0:
            results[exchange] = {"error": "invalid mid price"}
            continue
        base_decimals, price_decimals = _extract_decimals(market)

        limit_price = quantize_price(safe_limit_price(mid_price, "buy"), price_decimals)
        base_size = quantize_size_by_base_decimals(notional_usd, mid_price, base_decimals)
        coid = _coid(f"-q8-{exchange}")

        assert_small_notional(notional_usd, max_per_call_usd)
        assert_effective_notional(base_size, mid_price, max_per_call_usd)
        budget.reserve(notional_usd)

        order_body: dict[str, Any] = {
            "exchange": exchange,
            "asset": asset,
            "side": "buy",
            "size": str(base_size),
            "price": str(limit_price),
            "timeInForce": "alo",
            "reduceOnly": False,
            "clientOrderId": coid,
        }
        assert_alo(order_body["timeInForce"])

        log.log("Q8_POST_WITH_SHORT_TIMEOUT", exchange=exchange, coid=coid)
        try:
            short_timeout_resp = await asyncio.wait_for(
                client.post("/exchange/orders", body=order_body),
                timeout=0.1,
            )
            log.log("Q8_NO_TIMEOUT", summary=summarize_response(short_timeout_resp))
            client_saw_response = True
        except (TimeoutError, httpx.ReadTimeout, httpx.ConnectTimeout):
            client_saw_response = False
            log.log("Q8_TIMEOUT_OCCURRED", coid=coid)
        except httpx.HTTPError as e:
            client_saw_response = False
            log.log("Q8_TIMEOUT_ERROR", error=str(e))

        await asyncio.sleep(3.0)
        open_resp = await client.get("/exchange/open-orders", params={"exchanges": exchange})
        all_orders_resp = await client.get(
            "/exchange/orders",
            params={"exchanges": exchange, "limit": 50},
        )

        match_in_open = [
            o for o in (open_resp.json() or []) if o.get("clientOrderId") == coid
        ]
        all_orders_data = all_orders_resp.json()
        # BUG-05 артефакт: VOOI отдаёт `items`, не `data`. Принимаем оба ключа на случай
        # вариаций ответа между endpoints/exchanges.
        all_orders_list = (
            all_orders_data.get("items") or all_orders_data.get("data") or []
            if isinstance(all_orders_data, dict)
            else (all_orders_data or [])
        )
        match_in_all = [o for o in all_orders_list if o.get("clientOrderId") == coid]

        results[exchange] = {
            "client_saw_response": client_saw_response,
            "found_in_open_orders": len(match_in_open),
            "found_in_orders_history": len(match_in_all),
            "interpretation": (
                "order_created_despite_timeout"
                if (len(match_in_open) > 0 or len(match_in_all) > 0)
                else "no_order_on_exchange"
            ),
        }
        log.log("Q8_RESULT", exchange=exchange, **results[exchange])

        for o in match_in_open:
            try:
                await client.delete(
                    "/exchange/orders",
                    body={
                        "exchange": exchange,
                        "asset": asset,
                        "orderId": o.get("orderId") or o.get("id"),
                    },
                )
            except httpx.HTTPError as e:
                log.log("Q8_CLEANUP_DELETE_ERROR", error=str(e))

    log.write_artifact("q8_5xx_behavior.json", results)
    log.log("Q8_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q9. POST /exchange/orders без broker  (C7)
# ---------------------------------------------------------------------------


async def q9_without_broker(
    client: ProbeClient,
    log: ProbeLogger,
    budget: SafetyBudget,
    *,
    exchanges: list[str],
    asset: str,
    notional_usd: Decimal,
    max_per_call_usd: Decimal,
) -> dict[str, Any]:
    log.log("Q9_START", what="POST orders без broker")
    results: dict[str, Any] = {}

    for exchange in exchanges:
        market = await _get_market_info(client, exchange, asset)
        mid_price = Decimal(str(market.get("price", "0")))
        if mid_price <= 0:
            results[exchange] = {"error": "invalid mid price"}
            continue
        base_decimals, price_decimals = _extract_decimals(market)

        limit_price = quantize_price(safe_limit_price(mid_price, "buy"), price_decimals)
        base_size = quantize_size_by_base_decimals(notional_usd, mid_price, base_decimals)
        coid = _coid(f"-q9-{exchange}")

        assert_small_notional(notional_usd, max_per_call_usd)
        assert_effective_notional(base_size, mid_price, max_per_call_usd)
        budget.reserve(notional_usd)

        order_body: dict[str, Any] = {
            "exchange": exchange,
            "asset": asset,
            "side": "buy",
            "size": str(base_size),
            "price": str(limit_price),
            "timeInForce": "alo",
            "reduceOnly": False,
            "clientOrderId": coid,
        }
        assert_alo(order_body["timeInForce"])

        log.log("Q9_POST_NO_BROKER", exchange=exchange, coid=coid)
        try:
            resp = await client.post("/exchange/orders", body=order_body)
            results[exchange] = {
                "status": resp.status_code,
                "accepted": resp.status_code == 200,
                "summary": summarize_response(resp),
            }
            log.log("Q9_RESPONSE", exchange=exchange, status=resp.status_code)

            if resp.status_code == 200:
                await asyncio.sleep(1.5)
                open_resp = await client.get(
                    "/exchange/open-orders",
                    params={"exchanges": exchange},
                )
                for o in open_resp.json() or []:
                    if o.get("clientOrderId") == coid:
                        try:
                            await client.delete(
                                "/exchange/orders",
                                body={
                                    "exchange": exchange,
                                    "asset": asset,
                                    "orderId": o.get("orderId") or o.get("id"),
                                },
                            )
                        except httpx.HTTPError as e:
                            log.log("Q9_CLEANUP_DELETE_ERROR", error=str(e))
        except httpx.HTTPError as e:
            results[exchange] = {"accepted": False, "error": str(e)}
            log.log("Q9_ERROR", exchange=exchange, error=str(e))

    log.write_artifact("q9_without_broker.json", results)
    log.log("Q9_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q10. /exchange/orders фильтр по clientOrderId  (C2)
# ---------------------------------------------------------------------------


def _extract_orders_list(body: Any) -> list[dict[str, Any]]:
    """BUG-05 fix: VOOI `/exchange/orders` отдаёт `{cursor, items}`, тогда как
    некоторые подмножества/прошлые версии — `{data: [...]}` или просто `[...]`.
    Унифицируем.
    """
    if isinstance(body, list):
        return cast("list[dict[str, Any]]", body)
    if isinstance(body, dict):
        for key in ("items", "data"):
            v = body.get(key)
            if isinstance(v, list):
                return cast("list[dict[str, Any]]", v)
    return []


async def q10_orderid_filter(
    client: ProbeClient,
    log: ProbeLogger,
    *,
    target_exchanges: list[str],
) -> dict[str, Any]:
    """Read-only пробинг фильтрации.

    BUG-05 fix:
    1. Если history пустой — берём fake-COID (заведомо несуществующий) и
       проверяем что биржа отвечает 200 + items=[] (filter принят) либо
       400/422 (filter не поддерживается).
    2. Унифицированная обработка `items`/`data` ключей через `_extract_orders_list`.
    """
    log.log("Q10_START", what="/exchange/orders filter by clientOrderId")
    results: dict[str, Any] = {}

    for exchange in target_exchanges:
        recent = await client.get(
            "/exchange/orders",
            params={"exchanges": exchange, "limit": 5},
        )
        recent_body = recent.json() if recent.status_code == 200 else None
        orders_list = _extract_orders_list(recent_body)
        sample_coid = orders_list[0].get("clientOrderId") if orders_list else None
        log.log(
            "Q10_SAMPLE",
            exchange=exchange,
            sample_coid=sample_coid,
            history_len=len(orders_list),
            response_shape=(
                "list" if isinstance(recent_body, list)
                else "dict:" + ",".join(sorted(recent_body.keys())) if isinstance(recent_body, dict)
                else type(recent_body).__name__
            ),
        )

        # Fake COID для случая когда нет sample либо для дополнительной проверки шейпа.
        fake_coid = f"vooi-funding-arb-probe-nonexistent-{uuid.uuid4().hex}"
        coids_to_test = []
        if sample_coid:
            coids_to_test.append(("real_sample", sample_coid))
        coids_to_test.append(("fake_nonexistent", fake_coid))

        results[exchange] = {
            "history_len": len(orders_list),
            "sample_coid": sample_coid,
            "fake_coid_used": fake_coid,
            "tests": {},
        }

        for endpoint in ["/exchange/orders", "/exchange/open-orders"]:
            for param_name in ["clientOrderId", "client_order_id"]:
                for label, coid in coids_to_test:
                    resp = await client.get(
                        endpoint,
                        params={"exchanges": exchange, param_name: coid},
                    )
                    body = resp.json() if resp.status_code == 200 else None
                    items = _extract_orders_list(body)
                    key = f"{endpoint}?{param_name}=({label})"
                    results[exchange]["tests"][key] = {
                        "status": resp.status_code,
                        "len": len(items),
                        "filter_applied": (
                            resp.status_code == 200
                            and (
                                # для real sample: ожидаем 1+ matched
                                (label == "real_sample" and len(items) >= 1)
                                # для fake: ожидаем 0 matched (filter принят)
                                or (label == "fake_nonexistent" and len(items) == 0)
                            )
                        ),
                    }

        log.log("Q10_RESULT", exchange=exchange, summary=results[exchange])

    log.write_artifact("q10_orderid_filter.json", results)
    log.log("Q10_DONE", results=results)
    return results


# ---------------------------------------------------------------------------
# Q11. SSE silent timeout  (C4)
# ---------------------------------------------------------------------------


async def q11_sse_silent(
    client: ProbeClient,
    log: ProbeLogger,
    *,
    target_exchanges: list[str],
    duration_sec: int = 300,
) -> dict[str, Any]:
    """BUG-06 fix: error-events отделяются от data-events. Если стрим закрылся
    с одним только `error` за <1s — это auth-rejection, а НЕ "stream_active".
    """
    log.log("Q11_START", what="SSE silent timeout", duration_sec=duration_sec)
    events: list[dict[str, Any]] = []
    error_events: list[dict[str, Any]] = []
    t0 = time.monotonic()

    try:
        async with client.sse(
            "/exchange/updates",
            params={"exchanges": ",".join(target_exchanges)},
        ) as evt:
            async for event in evt.aiter_sse():
                rec = {
                    "elapsed_sec": time.monotonic() - t0,
                    "event_type": event.event,
                    "raw_size_bytes": len(event.data) if event.data else 0,
                    "data_preview": (event.data or "")[:200],
                }
                if event.event in _SSE_AUTH_REJECT_EVENTS:
                    error_events.append(rec)
                    log.log(
                        "Q11_ERROR_EVENT",
                        elapsed_sec=rec["elapsed_sec"],
                        data_preview=rec["data_preview"],
                    )
                else:
                    events.append(rec)
                    log.log(
                        "Q11_EVENT",
                        elapsed_sec=rec["elapsed_sec"],
                        event_type=event.event,
                    )
                if time.monotonic() - t0 > duration_sec:
                    break
    except (httpx.HTTPError, asyncio.CancelledError, RuntimeError) as e:
        log.log("Q11_STREAM_ERROR", error=str(e))

    if error_events and not events:
        interpretation = "auth_rejected_by_server"
    elif events:
        interpretation = "stream_active"
    else:
        interpretation = "silent_no_keepalive_confirmed"

    findings = {
        "duration_sec": duration_sec,
        "events_count": len(events),
        "error_events_count": len(error_events),
        "events_sample": events[:20],
        "error_events_sample": error_events[:5],
        "first_event_at_sec": events[0]["elapsed_sec"] if events else None,
        "last_event_at_sec": events[-1]["elapsed_sec"] if events else None,
        "interpretation": interpretation,
    }
    log.write_artifact("q11_sse_silent.json", findings)
    log.log("Q11_DONE", **findings)
    return findings
