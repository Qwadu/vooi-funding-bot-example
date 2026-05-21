"""SSE event-stream client for /exchange/updates.

Phase 1 contract: this module exposes a long-lived background task that
maintains one SSE connection to the VOOI API and lets call-sites register
"wake-me-up" waiters keyed on (exchange, asset, [side]). It is a *latency
optimisation* over the existing REST polling — REST stays the source of
truth, every decision is still confirmed by a REST read.

Design choices
--------------
- One persistent connection per process. Bearer auth. Query format is
  ``?exchanges=hyperliquid&exchanges=lighter`` (repeat-key style, which
  httpx encodes from ``params={"exchanges": [...]}``).
- Reconnect with exponential backoff (1s → 60s cap). Capped retries before
  permanent disable.
- Heartbeat-by-data: if no event arrives for ``heartbeat_timeout_sec`` the
  stream is reported unhealthy and callers fall back to plain REST polling.
  VOOI does not currently send transport-level keep-alives.
- Auth-rejection detector: if the first received event is ``event: error``
  with body ``Validation failed`` (or any "error" event before any data
  event), the stream is permanently disabled for the process lifetime and
  all subsequent ``register_*`` calls return an already-set Event so the
  caller's REST-only path runs immediately.
- ``register_*`` returns a plain ``asyncio.Event``; the caller is expected
  to race it against its existing poll-sleep with
  ``asyncio.wait_for(ev.wait(), timeout=poll_sec)``. On wake (whether
  early via SSE or by timeout) the caller does its normal REST confirm.
  This makes the SSE path strictly additive — if it ever misclassifies
  or misses an event, behaviour is identical to today's polling.
- The matcher is intentionally generous: any event whose type is in the
  configured ORDER/POSITION sets, *and* whose payload (best-effort field
  extraction) does not contradict the waiter's keys, fires the waiter.
  When the payload shape is unknown we wake conservatively. The cost of
  a spurious wake is one extra REST call; the cost of a missed wake is
  going back to the next poll tick — both are bounded.

The actual VOOI event-type catalogue (e.g. ``order``, ``orderUpdate``,
``position``, ``positionUpdate``) is not formally documented in our
copy of the spec. The constants below match what production logs surface;
unrecognised event types are still logged so we can expand later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from httpx_sse import aconnect_sse

# Event-type buckets. Conservative — better to fire too often than to
# miss. ``marketPrice`` is intentionally NOT in either set: it's a high-
# frequency stream we don't want to wake every waiter on.
ORDER_EVENT_TYPES: frozenset[str] = frozenset({
    "order",
    "orderUpdate",
    "orderExecuted",
    "fill",
    "orderFilled",
})
POSITION_EVENT_TYPES: frozenset[str] = frozenset({
    "position",
    "positionUpdate",
    "positionClosed",
})
# An SSE "event: error" before any data event = auth/contract rejection.
AUTH_REJECT_EVENT_TYPES: frozenset[str] = frozenset({"error"})


@dataclass
class _Waiter:
    """Internal registered waiter; fires its asyncio.Event on a match.

    A waiter is auto-removed from the stream's registry after it fires.
    The caller is also expected to call ``stream.unregister(event)`` in
    its finally-clause for the timeout path, but late unregister is
    a no-op so this isn't strictly required for correctness.
    """

    bucket: str  # "order" or "position"
    exchange: str
    asset_upper: str
    side: str | None  # None = any side
    event: asyncio.Event
    fired: bool = False


@dataclass
class _StreamStats:
    """For NDJSON-logged health snapshots."""

    events_total: int = 0
    events_by_type: dict[str, int] = field(default_factory=dict)
    last_event_ts: float = 0.0
    reconnects: int = 0
    auth_rejected: bool = False
    disabled: bool = False


class VooiEventStream:
    """Long-lived SSE multiplexer for /exchange/updates."""

    SSE_PATH = "/exchange/updates"

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        exchanges: tuple[str, ...],
        log_emit: Callable[..., None],
        heartbeat_timeout_sec: float = 60.0,
        reconnect_max_attempts: int = 10,
        idle_log_interval_sec: float = 300.0,
    ) -> None:
        if not exchanges:
            raise ValueError("exchanges must be non-empty")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._exchanges = tuple(exchanges)
        self._log_emit = log_emit
        self._heartbeat_timeout = heartbeat_timeout_sec
        self._reconnect_max = reconnect_max_attempts
        self._idle_log_interval = idle_log_interval_sec

        self._waiters: list[_Waiter] = []
        self._waiters_lock = asyncio.Lock()
        self._stats = _StreamStats()
        self._last_event_id: str | None = None
        # Started=True once we've seen the first event (data or error).
        self._started = False
        # is_healthy gates whether waiters can short-circuit; default True
        # and flipped on heartbeat timeout / disable.
        self._healthy = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """True if the stream is connected and producing events recently.

        Callers can use this to decide whether the SSE wake-event is
        worth waiting on, or to drop straight to pure REST polling.
        """
        if self._stats.disabled or self._stats.auth_rejected:
            return False
        if not self._healthy:
            return False
        # If we've received at least one event, enforce heartbeat.
        return not (
            self._stats.events_total > 0
            and self._stats.last_event_ts > 0
            and (time.monotonic() - self._stats.last_event_ts) > self._heartbeat_timeout
        )

    def is_disabled(self) -> bool:
        return self._stats.disabled or self._stats.auth_rejected

    async def register_order_waiter(
        self,
        *,
        exchange: str,
        asset: str,
        side: str | None = None,
    ) -> asyncio.Event:
        """Returns an Event that fires on the next matching order event.

        If the stream is permanently disabled, returns an already-set
        Event so the caller's poll-sleep wakes immediately and falls
        through to its REST check — exactly the current behaviour.
        """
        ev = asyncio.Event()
        if self.is_disabled():
            ev.set()
            return ev
        async with self._waiters_lock:
            self._waiters.append(
                _Waiter(
                    bucket="order",
                    exchange=exchange,
                    asset_upper=asset.upper(),
                    side=side,
                    event=ev,
                )
            )
        return ev

    async def register_position_waiter(
        self,
        *,
        exchange: str,
        asset: str,
    ) -> asyncio.Event:
        """Returns an Event that fires on the next matching position event."""
        ev = asyncio.Event()
        if self.is_disabled():
            ev.set()
            return ev
        async with self._waiters_lock:
            self._waiters.append(
                _Waiter(
                    bucket="position",
                    exchange=exchange,
                    asset_upper=asset.upper(),
                    side=None,
                    event=ev,
                )
            )
        return ev

    async def unregister(self, ev: asyncio.Event) -> None:
        """Remove a waiter that timed out without firing. Safe to call twice."""
        async with self._waiters_lock:
            self._waiters = [w for w in self._waiters if w.event is not ev]

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Connect-loop coroutine. Spawn this once with create_task."""
        self._log_emit(
            "SSE_STARTED",
            url=self._base + self.SSE_PATH,
            exchanges=list(self._exchanges),
            heartbeat_timeout_sec=self._heartbeat_timeout,
            reconnect_max=self._reconnect_max,
        )
        attempt = 0
        while not stop.is_set():
            try:
                await self._connect_once(stop)
                # Clean exit (stop signalled or server closed normally) → reset attempt.
                attempt = 0
            except _AuthRejectedError:
                # Permanent. Bot continues, REST-only.
                self._stats.auth_rejected = True
                self._stats.disabled = True
                self._healthy = False
                self._log_emit("SSE_AUTH_REJECTED", hint="stream permanently disabled, REST-only")
                # Wake any still-pending waiters so callers don't hang.
                await self._wake_all_waiters("auth_rejected")
                return
            except (TimeoutError, httpx.HTTPError) as e:
                attempt += 1
                self._stats.reconnects += 1
                if attempt > self._reconnect_max:
                    self._stats.disabled = True
                    self._healthy = False
                    self._log_emit(
                        "SSE_DISABLED",
                        reason="reconnect_max_attempts_exceeded",
                        attempts=attempt,
                        last_error=str(e)[:200],
                    )
                    await self._wake_all_waiters("disabled")
                    return
                backoff = min(60.0, 2 ** min(attempt, 6))
                self._log_emit(
                    "SSE_RECONNECT_SCHEDULED",
                    attempt=attempt,
                    backoff_sec=backoff,
                    error=str(e)[:200],
                )
                self._healthy = False
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
        self._log_emit(
            "SSE_STOPPED",
            events_total=self._stats.events_total,
            reconnects=self._stats.reconnects,
        )

    async def _connect_once(self, stop: asyncio.Event) -> None:
        params: dict[str, Any] = {"exchanges": list(self._exchanges)}
        # Send Last-Event-ID so the server can resume mid-stream if it supports it.
        headers = dict(self._headers)
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id

        timeout = httpx.Timeout(self._heartbeat_timeout, connect=10.0)
        async with httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=timeout,
        ) as client, aconnect_sse(client, "GET", self.SSE_PATH, params=params) as evtsource:
            self._log_emit(
                "SSE_CONNECTED",
                last_event_id=self._last_event_id,
                exchanges=list(self._exchanges),
            )
            self._healthy = True
            last_idle_log = time.monotonic()
            async for sse in evtsource.aiter_sse():
                if stop.is_set():
                    return
                # First event handling: detect auth-rejection.
                if not self._started:
                    self._started = True
                    if sse.event in AUTH_REJECT_EVENT_TYPES:
                        self._log_emit(
                            "SSE_FIRST_EVENT_ERROR",
                            event_type=sse.event,
                            data_preview=(sse.data or "")[:200],
                        )
                        raise _AuthRejectedError(sse.data or "")
                if sse.id is not None and sse.id != "":
                    self._last_event_id = sse.id

                await self._dispatch(sse.event or "", sse.data or "")

                # Periodic idle health log.
                now = time.monotonic()
                if now - last_idle_log > self._idle_log_interval:
                    self._log_emit(
                        "SSE_HEALTH",
                        events_total=self._stats.events_total,
                        by_type=dict(self._stats.events_by_type),
                        healthy=self.is_healthy(),
                        reconnects=self._stats.reconnects,
                    )
                    last_idle_log = now

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, event_type: str, raw_data: str) -> None:
        self._stats.events_total += 1
        self._stats.events_by_type[event_type] = self._stats.events_by_type.get(event_type, 0) + 1
        self._stats.last_event_ts = time.monotonic()

        # Cheap event log. marketPrice is high-volume; suppress to a sampled rate.
        if event_type != "marketPrice":
            self._log_emit(
                "SSE_EVENT_RECEIVED",
                event_type=event_type,
                data_preview=raw_data[:300] if raw_data else "",
            )

        # Parse JSON best-effort.
        payload: Any = None
        try:
            payload = json.loads(raw_data) if raw_data else None
        except (json.JSONDecodeError, ValueError):
            payload = None

        # Decide which bucket this belongs to.
        bucket: str | None
        if event_type in ORDER_EVENT_TYPES:
            bucket = "order"
        elif event_type in POSITION_EVENT_TYPES:
            bucket = "position"
        else:
            return

        await self._fire_matching_waiters(bucket, payload, event_type)

    async def _fire_matching_waiters(
        self,
        bucket: str,
        payload: Any,
        event_type: str,
    ) -> None:
        # Extract candidate fields from the payload. Tolerant — many shapes.
        items = _extract_items(payload)
        async with self._waiters_lock:
            to_remove: list[_Waiter] = []
            for w in self._waiters:
                if w.bucket != bucket or w.fired:
                    continue
                if _waiter_matches(w, items):
                    w.fired = True
                    w.event.set()
                    to_remove.append(w)
                    self._log_emit(
                        "SSE_WAITER_FIRED",
                        bucket=bucket,
                        event_type=event_type,
                        exchange=w.exchange,
                        asset=w.asset_upper,
                        side=w.side,
                    )
            if to_remove:
                self._waiters = [w for w in self._waiters if w not in to_remove]

    async def _wake_all_waiters(self, reason: str) -> None:
        async with self._waiters_lock:
            for w in self._waiters:
                if not w.fired:
                    w.fired = True
                    w.event.set()
            n = len(self._waiters)
            self._waiters = []
        if n:
            self._log_emit("SSE_WAITERS_WAKE_ALL", reason=reason, count=n)


# ----------------------------------------------------------------------
# Helpers (module-level, easier to unit-test)
# ----------------------------------------------------------------------


class _AuthRejectedError(Exception):
    """Internal marker: server rejected our auth on first event."""


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Best-effort: pull list of records out of an event payload.

    VOOI's marketPrice events are an array of records. Single-record
    events (likely for order/position) may be either a dict or a 1-item
    array. We normalise.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        # Some shapes wrap items: {"items": [...]}.
        if isinstance(payload.get("items"), list):
            return [x for x in payload["items"] if isinstance(x, dict)]
        return [payload]
    return []


def _waiter_matches(w: _Waiter, items: list[dict[str, Any]]) -> bool:
    """True if any item plausibly matches the waiter's keys.

    Empty items list (couldn't parse) → True conservatively. The caller
    will then do a REST confirm; a spurious wake costs one extra REST
    call but never produces wrong state.
    """
    if not items:
        return True
    for item in items:
        ex = _str_lower_or_none(
            item.get("exchange") or item.get("venue") or item.get("ex")
        )
        if ex is not None and ex != w.exchange.lower():
            continue
        asset = _str_upper_or_none(
            item.get("asset")
            or item.get("baseSymbol")
            or item.get("symbol")
            or item.get("base")
        )
        if asset is not None and asset != w.asset_upper:
            continue
        if w.side is not None:
            side = _str_lower_or_none(item.get("side"))
            if side is not None and side != w.side.lower():
                continue
        return True
    return False


def _str_lower_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s.lower() if s else None


def _str_upper_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s.upper() if s else None


# ----------------------------------------------------------------------
# Module-level singleton accessor
# ----------------------------------------------------------------------
#
# Single bot process → one stream. Storing as a module variable keeps
# call-site changes in mvp.py minimal (no need to thread a new arg
# through 6 functions). Tests reset via ``set_active_stream(None)``.

_active_stream: VooiEventStream | None = None


def set_active_stream(stream: VooiEventStream | None) -> None:
    global _active_stream
    _active_stream = stream


def get_active_stream() -> VooiEventStream | None:
    return _active_stream


# ----------------------------------------------------------------------
# Small Decimal helper exported for callers that need to compare event
# sizes against position state. Kept here to avoid mvp ↔ sse circular
# imports.
# ----------------------------------------------------------------------


def decimal_or_none(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


async def wait_first_or_timeout(
    events: list[asyncio.Event],
    timeout: float,
) -> bool:
    """Race a list of asyncio.Events against a timeout.

    Returns True if at least one event was set within ``timeout``;
    False if the timeout elapsed first. Cancels and cleans up any
    pending waiter-tasks. If ``events`` is empty, behaves like
    ``asyncio.sleep(timeout)`` and returns False.

    Used by tight-loop pollers (ALO fill watcher, survivor watcher)
    to race their poll-interval sleep against any SSE wake. Even when
    the SSE wake fires spuriously, the caller's subsequent REST check
    confirms the actual state — so a wrong wake just costs one extra
    REST call, never wrong behaviour.
    """
    if not events:
        try:
            await asyncio.sleep(timeout)
        finally:
            pass
        return False

    waiter_tasks = [asyncio.ensure_future(ev.wait()) for ev in events]
    try:
        done, _pending = await asyncio.wait(
            waiter_tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        return bool(done)
    finally:
        for t in waiter_tasks:
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
