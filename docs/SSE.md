# SSE event stream

`fundbot` maintains an optional long-lived Server-Sent Events connection to `/exchange/updates`. It is a **latency optimisation** over the existing REST polling — every decision the bot makes is still confirmed by a REST read. SSE just lets the tight-loop pollers (the 1-second survivor watcher and the 5-second ALO fill watchers) wake up earlier than their next poll tick when the server has anything to say.

## TL;DR

- Default: **on** (`BOT_SSE_ENABLED=true`).
- Turn off any time with `BOT_SSE_ENABLED=false` — the bot drops to pure REST polling and behaviour is byte-identical to a no-SSE deployment.
- REST is canonical. SSE is never the source of truth — a spurious wake costs one extra REST call; a missed wake means the caller waits its full poll interval like before. Both are bounded.

## What it does

Three places in the engine used to sleep:

| Site | Old sleep | What it waits for |
|---|---|---|
| `survivor_watcher_loop` | `await asyncio.sleep(BOT_SURVIVOR_WATCH_SEC=1)` | A leg disappearing (the other-side SL/TP fired and we now carry a naked leg). |
| `_open_limit_then_market` poll | `await asyncio.sleep(BOT_LIMIT_ALO_POLL_SEC=5)` | The maker (ALO) entry order to fill. |
| `_close_leg_alo_then_market` poll | `await asyncio.sleep(BOT_LIMIT_ALO_POLL_SEC=5)` | The reduce-only ALO close to land. |

When `BOT_SSE_ENABLED=true`, each of these now does:

```python
sse_wake = await stream.register_*_waiter(exchange=..., asset=..., side=...)
woke_early = await sse.wait_first_or_timeout([sse_wake, ...], timeout=poll_sec)
# REST check runs whether we woke via SSE or via timeout — same code as before
```

If the SSE stream is healthy and the matching event arrives, `wait_first_or_timeout` returns in <1 second instead of waiting the full 1 or 5 seconds. The caller then runs its existing REST check, draws the same conclusion, and continues.

## The contract with the API

Endpoint: `GET /exchange/updates?exchanges=<ex1>&exchanges=<ex2>` (repeat-key style — the server rejects CSV `?exchanges=hl,lighter` with HTTP 400 `Validation failed`).

Auth: `Authorization: Bearer <jwt>` header. A short-lived query token (`POST /exchange/updates-token` → `?token=`) is also supported but the bot uses the bearer header.

Event types observed: `marketPrice` (high-volume funding-rate + price updates), and order/position-related types (the matcher accepts `order`, `orderUpdate`, `orderExecuted`, `fill`, `orderFilled`, `position`, `positionUpdate`, `positionClosed`). Unknown event types are logged as `SSE_EVENT_RECEIVED` and otherwise ignored.

The first SSE event being `event: error` is treated as a permanent auth/contract rejection — the bot logs `SSE_AUTH_REJECTED`, disables the stream, and drops to REST-only for the rest of the process lifetime.

There is no documented heartbeat. The bot enforces its own: if no event arrives for `BOT_SSE_HEARTBEAT_TIMEOUT_SEC` (default 60s), the stream is reported unhealthy and `is_healthy() == False` makes callers fall through to plain REST polling until the next reconnect succeeds.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BOT_SSE_ENABLED` | `true` | Master switch. Set `false` to skip stream startup entirely. |
| `BOT_SSE_HEARTBEAT_TIMEOUT_SEC` | `60` | Silence threshold after which the stream is considered unhealthy. |
| `BOT_SSE_RECONNECT_MAX_ATTEMPTS` | `10` | After this many connect failures, the stream is permanently disabled for the process. The bot continues in pure REST mode. |

## What you'll see in `state.ndjson`

| Event | When |
|---|---|
| `SSE_STARTED` | Stream task spawned at boot. |
| `SSE_CONNECTED` | `aconnect_sse` opened the connection (per attempt). |
| `SSE_EVENT_RECEIVED` | Non-`marketPrice` event arrived (preview included). |
| `SSE_WAITER_FIRED` | An incoming event matched a registered waiter — caller is being woken. |
| `OPEN_LIMIT_SSE_WAKE` | ALO open fill watcher woke via SSE (rather than its 5s poll tick). |
| `CLOSE_ALO_SSE_WAKE` | ALO close fill watcher woke via SSE. |
| `SURVIVOR_WATCH_SSE_WAKE` | Survivor watcher woke via SSE. |
| `SSE_HEALTH` | Every 5 min — counters per event type, `healthy` flag, reconnect total. |
| `SSE_RECONNECT_SCHEDULED` | After a stream-side error; logs the backoff. |
| `SSE_AUTH_REJECTED` | First event was `event: error` — permanent disable. |
| `SSE_DISABLED` | `BOT_SSE_ENABLED=false` at boot, or reconnect-max exceeded. |
| `SSE_STOPPED` | Graceful shutdown — total events received, reconnect count. |

## Safety properties

The wake is **additive**. The code path is:

```
[SSE wake OR poll timeout] → REST check → decision
```

The REST check is the same one as before. Therefore:

- **Spurious wake** (SSE fires when nothing relevant happened): caller does one extra REST call, sees the state hasn't changed, sleeps again. Zero behaviour change.
- **Missed wake** (SSE silently drops the event): caller falls through to the timeout path and behaves exactly like the no-SSE bot.
- **Stream crash / auth rejection**: `is_healthy()` flips to false; waiters return pre-set events; callers immediately fall to their REST-only path. The bot continues trading.
- **Wrong-shaped payload** (we can't parse `exchange`/`asset` fields): the matcher fires the waiter conservatively (better-safe-than-sorry); one extra REST call, no harm.

There is no code path where an SSE event causes the bot to decide differently from how it would have decided after the next poll tick — only sooner.

## Verifying

After bot start, with SSE enabled:

```bash
# is the stream up?
grep -E 'SSE_STARTED|SSE_CONNECTED' state.ndjson | tail -2

# is it producing events? (idle bot sees mostly marketPrice)
grep '"event": "SSE_HEALTH"' state.ndjson | tail -1 | python3 -m json.tool

# did any waiter fire recently? (only happens during opens/closes)
grep '"event": "SSE_WAITER_FIRED"' state.ndjson | tail -5
```

If `SSE_HEALTH` shows `healthy: false` for an extended period, the stream is reconnecting silently. Inspect `SSE_RECONNECT_SCHEDULED` events to see the backoff. If the bot logged `SSE_AUTH_REJECTED`, restarts won't help until the underlying credential / endpoint issue is resolved — the bot will continue in REST-only mode regardless.

## Forcing pure-REST

```bash
sed -i 's/^BOT_SSE_ENABLED=.*/BOT_SSE_ENABLED=false/' .env
# then restart the bot
```

Behaviour reverts to the pre-SSE version exactly. No state migration needed.

## Implementation references

- `fundbot/sse.py` — `VooiEventStream`, the singleton accessor, `wait_first_or_timeout` helper.
- `fundbot/mvp.py` — call sites: `survivor_watcher_loop`, `_open_limit_then_market` polling loop, `_close_leg_alo_then_market` polling loop.
- `tests/test_sse.py` — unit tests for the matcher, waiter registry, auth-rejection, heartbeat.
