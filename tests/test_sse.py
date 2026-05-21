"""Unit tests for fundbot/sse.py — VooiEventStream + helpers.

Network is never touched. Where the stream's _connect_once would normally
run, we drive the dispatcher directly to avoid mocking httpx_sse. The
stream's lifecycle (auth-reject, disable, heartbeat, reconnect) is
covered by feeding events into _dispatch and inspecting state.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

from fundbot import sse

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _LogCapture:
    """NDJsonLog-compatible callable that records emit calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, **fields: Any) -> None:
        self.events.append((event, dict(fields)))

    def types(self) -> list[str]:
        return [e for e, _ in self.events]


def _make_stream(
    *,
    heartbeat: float = 60.0,
    reconnect_max: int = 10,
) -> tuple[sse.VooiEventStream, _LogCapture]:
    log = _LogCapture()
    stream = sse.VooiEventStream(
        base_url="https://example.test",
        bearer_token="dummy",
        exchanges=("hyperliquid", "lighter"),
        log_emit=log,
        heartbeat_timeout_sec=heartbeat,
        reconnect_max_attempts=reconnect_max,
    )
    return stream, log


# ----------------------------------------------------------------------
# _extract_items / _waiter_matches
# ----------------------------------------------------------------------


def test_extract_items_list_of_dicts() -> None:
    payload = [{"a": 1}, {"a": 2}]
    assert sse._extract_items(payload) == [{"a": 1}, {"a": 2}]


def test_extract_items_dict_passthrough() -> None:
    payload = {"a": 1}
    assert sse._extract_items(payload) == [{"a": 1}]


def test_extract_items_items_wrapper() -> None:
    payload = {"items": [{"a": 1}], "meta": "ignored"}
    assert sse._extract_items(payload) == [{"a": 1}]


def test_extract_items_none_and_scalar() -> None:
    assert sse._extract_items(None) == []
    assert sse._extract_items(42) == []
    assert sse._extract_items("hello") == []


def test_waiter_matches_empty_items_is_conservative() -> None:
    w = sse._Waiter(
        bucket="order", exchange="hyperliquid", asset_upper="BTC",
        side="buy", event=asyncio.Event(),
    )
    assert sse._waiter_matches(w, []) is True


def test_waiter_matches_exchange_filter() -> None:
    w = sse._Waiter(
        bucket="position", exchange="lighter", asset_upper="ETH",
        side=None, event=asyncio.Event(),
    )
    # exchange field present and mismatched → no
    assert sse._waiter_matches(w, [{"exchange": "hyperliquid", "asset": "ETH"}]) is False
    # exchange matches → yes
    assert sse._waiter_matches(w, [{"exchange": "lighter", "asset": "ETH"}]) is True
    # exchange field absent → conservatively true
    assert sse._waiter_matches(w, [{"asset": "ETH"}]) is True


def test_waiter_matches_asset_case_insensitive() -> None:
    w = sse._Waiter(
        bucket="position", exchange="lighter", asset_upper="ETH",
        side=None, event=asyncio.Event(),
    )
    assert sse._waiter_matches(w, [{"exchange": "lighter", "asset": "eth"}]) is True
    assert sse._waiter_matches(w, [{"exchange": "lighter", "baseSymbol": "ETH"}]) is True
    assert sse._waiter_matches(w, [{"exchange": "lighter", "symbol": "BTC"}]) is False


def test_waiter_matches_side_filter() -> None:
    w = sse._Waiter(
        bucket="order", exchange="hyperliquid", asset_upper="BTC",
        side="buy", event=asyncio.Event(),
    )
    match_buy = [{"exchange": "hyperliquid", "asset": "BTC", "side": "buy"}]
    match_sell = [{"exchange": "hyperliquid", "asset": "BTC", "side": "sell"}]
    assert sse._waiter_matches(w, match_buy) is True
    assert sse._waiter_matches(w, match_sell) is False
    # side absent → conservatively true
    assert sse._waiter_matches(w, [{"exchange": "hyperliquid", "asset": "BTC"}]) is True


def test_waiter_matches_any_item_matches() -> None:
    w = sse._Waiter(
        bucket="order", exchange="lighter", asset_upper="BTC",
        side="sell", event=asyncio.Event(),
    )
    # Mixed batch — should match if ANY item matches.
    items: list[dict[str, Any]] = [
        {"exchange": "hyperliquid", "asset": "ETH", "side": "buy"},
        {"exchange": "lighter", "asset": "BTC", "side": "sell"},
    ]
    assert sse._waiter_matches(w, items) is True


# ----------------------------------------------------------------------
# decimal_or_none
# ----------------------------------------------------------------------


def test_decimal_or_none_happy_paths() -> None:
    assert sse.decimal_or_none("1.5") == Decimal("1.5")
    assert sse.decimal_or_none(2) == Decimal("2")


def test_decimal_or_none_bad_input() -> None:
    assert sse.decimal_or_none(None) is None
    assert sse.decimal_or_none("not a number") is None
    assert sse.decimal_or_none([]) is None


# ----------------------------------------------------------------------
# VooiEventStream — health
# ----------------------------------------------------------------------


async def test_is_healthy_default_true_before_any_event() -> None:
    stream, _ = _make_stream()
    assert stream.is_healthy() is True


async def test_is_healthy_drops_after_heartbeat() -> None:
    stream, _ = _make_stream(heartbeat=0.05)
    # Simulate one event so heartbeat logic activates.
    await stream._dispatch("position", json.dumps([{"exchange": "lighter", "asset": "ETH"}]))
    assert stream.is_healthy() is True
    await asyncio.sleep(0.1)
    assert stream.is_healthy() is False


# ----------------------------------------------------------------------
# VooiEventStream — register + dispatch
# ----------------------------------------------------------------------


async def test_register_order_waiter_fires_on_matching_event() -> None:
    stream, log = _make_stream()
    ev = await stream.register_order_waiter(
        exchange="hyperliquid", asset="BTC", side="buy",
    )
    assert ev.is_set() is False
    fill_evt = [{"exchange": "hyperliquid", "asset": "BTC", "side": "buy", "status": "filled"}]
    await stream._dispatch("order", json.dumps(fill_evt))
    assert ev.is_set() is True
    # Auto-removed from registry after firing.
    assert len(stream._waiters) == 0
    # Did we log the wake?
    assert "SSE_WAITER_FIRED" in log.types()


async def test_register_order_waiter_ignores_wrong_event_type() -> None:
    stream, _ = _make_stream()
    ev = await stream.register_order_waiter(exchange="lighter", asset="ETH", side="sell")
    await stream._dispatch("marketPrice", json.dumps([{"exchange": "lighter", "price": "100"}]))
    assert ev.is_set() is False


async def test_register_order_waiter_ignores_mismatched_payload() -> None:
    stream, _ = _make_stream()
    ev = await stream.register_order_waiter(exchange="lighter", asset="ETH", side="sell")
    await stream._dispatch(
        "order",
        json.dumps([{"exchange": "hyperliquid", "asset": "ETH", "side": "sell"}]),
    )
    assert ev.is_set() is False


async def test_register_position_waiter_fires_on_position_event() -> None:
    stream, _ = _make_stream()
    ev = await stream.register_position_waiter(exchange="lighter", asset="WLD")
    await stream._dispatch(
        "positionUpdate",
        json.dumps([{"exchange": "lighter", "asset": "WLD", "size": "0"}]),
    )
    assert ev.is_set() is True


async def test_unregister_removes_unfired_waiter() -> None:
    stream, _ = _make_stream()
    ev = await stream.register_order_waiter(exchange="hyperliquid", asset="BTC")
    assert len(stream._waiters) == 1
    await stream.unregister(ev)
    assert len(stream._waiters) == 0
    # Subsequent matching event should NOT fire (waiter is gone).
    await stream._dispatch("order", json.dumps([{"exchange": "hyperliquid", "asset": "BTC"}]))
    assert ev.is_set() is False


async def test_unregister_is_idempotent() -> None:
    stream, _ = _make_stream()
    ev = await stream.register_order_waiter(exchange="lighter", asset="ETH")
    await stream.unregister(ev)
    # Second unregister is a no-op (no exception).
    await stream.unregister(ev)


# ----------------------------------------------------------------------
# Auth-reject path: register_* returns pre-set Event
# ----------------------------------------------------------------------


async def test_register_when_disabled_returns_preset_event() -> None:
    stream, _ = _make_stream()
    # Manually flip disabled (simulating an _AuthRejected raise).
    stream._stats.disabled = True
    ev = await stream.register_order_waiter(exchange="hyperliquid", asset="BTC")
    assert ev.is_set() is True
    pos_ev = await stream.register_position_waiter(exchange="lighter", asset="ETH")
    assert pos_ev.is_set() is True


async def test_wake_all_waiters_unblocks_pending() -> None:
    stream, log = _make_stream()
    ev1 = await stream.register_order_waiter(exchange="hyperliquid", asset="BTC")
    ev2 = await stream.register_position_waiter(exchange="lighter", asset="ETH")
    assert ev1.is_set() is False
    assert ev2.is_set() is False
    await stream._wake_all_waiters("test_reason")
    assert ev1.is_set() is True
    assert ev2.is_set() is True
    assert "SSE_WAITERS_WAKE_ALL" in log.types()


# ----------------------------------------------------------------------
# wait_first_or_timeout
# ----------------------------------------------------------------------


async def test_wait_first_or_timeout_returns_false_on_timeout() -> None:
    ev = asyncio.Event()
    t0 = time.monotonic()
    woke = await sse.wait_first_or_timeout([ev], timeout=0.05)
    elapsed = time.monotonic() - t0
    assert woke is False
    assert 0.04 < elapsed < 0.5  # generous upper bound for CI jitter


async def test_wait_first_or_timeout_returns_true_when_event_fires() -> None:
    ev = asyncio.Event()

    async def _fire() -> None:
        await asyncio.sleep(0.02)
        ev.set()

    fire_task = asyncio.create_task(_fire())
    try:
        woke = await sse.wait_first_or_timeout([ev], timeout=1.0)
    finally:
        if not fire_task.done():
            fire_task.cancel()
    assert woke is True


async def test_wait_first_or_timeout_no_events_acts_as_sleep() -> None:
    t0 = time.monotonic()
    woke = await sse.wait_first_or_timeout([], timeout=0.05)
    elapsed = time.monotonic() - t0
    assert woke is False
    assert elapsed >= 0.04


async def test_wait_first_or_timeout_multiple_events_first_wins() -> None:
    ev_late = asyncio.Event()
    ev_early = asyncio.Event()

    async def _fire_early() -> None:
        await asyncio.sleep(0.02)
        ev_early.set()

    async def _fire_late() -> None:
        await asyncio.sleep(0.5)
        ev_late.set()

    tasks = [asyncio.create_task(_fire_early()), asyncio.create_task(_fire_late())]
    try:
        woke = await sse.wait_first_or_timeout([ev_late, ev_early], timeout=1.0)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    assert woke is True
    assert ev_early.is_set() is True


# ----------------------------------------------------------------------
# Module-level singleton accessor
# ----------------------------------------------------------------------


async def test_set_and_get_active_stream() -> None:
    sse.set_active_stream(None)
    assert sse.get_active_stream() is None
    s, _ = _make_stream()
    sse.set_active_stream(s)
    assert sse.get_active_stream() is s
    sse.set_active_stream(None)
    assert sse.get_active_stream() is None
