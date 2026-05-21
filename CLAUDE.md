# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Authoritative companion docs — read these first when relevant

- **`AGENTS.md`** — hard rules and the contract every AI agent here must follow (don't edit `.env`, never flip `BOT_DRY_RUN=false`, never bump deps without re-locking, common gotchas with Hyperliquid coid format / Lighter bracket coid blanking / Aster `fundingFee: null`). Treat its "Hard rules" and "Things AI agents commonly get wrong" sections as binding.
- **`docs/STRATEGY.md`** — the single source of strategy truth: cycle structure, close-criteria priority list, why every default is what it is. Required reading before touching anything in `fundbot/mvp.py` that affects open/close decisions.
- **`README.md`** — local + dedicated-VPS (systemd) launch paths and the safety-model overview.

## Commands

The project uses `uv` exclusively for environment and dependency management. There is no `pip`/`venv` path.

```bash
# Install / re-lock
uv sync --extra dev                    # installs runtime + dev deps from uv.lock

# Run the bot
uv run python -m fundbot                # respects BOT_DRY_RUN (true by default)
uv run python -m probe.probe readonly   # phase-0 API validation (no writes)
uv run python -m probe.probe <q1..q11>  # individual probes; q2/q3/q4/q7/q8/q9 issue real orders

# Tests
uv run pytest -q                        # unit tests (default; integration marker is excluded)
uv run pytest -m integration            # live-API tests — places real minimum-size orders
uv run pytest tests/test_mvp_patches.py::TestFilterOpportunities::test_blacklist_filters_asset  # single test
uv run pytest -k "smart_neg"            # by keyword

# Lint / type-check (both required to pass before any PR)
uv run ruff check .
uv run mypy fundbot probe               # mypy is strict; ignore_missing_imports is on
```

Pytest config (`pyproject.toml`) auto-applies `-ra -q --tb=short`, sets `asyncio_mode=auto`, and registers the `integration` marker.

## Architecture — the big picture

The whole trading engine is **one file**: `fundbot/mvp.py` (~3900 lines). The `fundbot/api/`, `fundbot/execution/`, `fundbot/strategy/` etc. directories exist as **empty stubs reserved for a future refactor** — don't be misled into looking for logic there. `mvp.py` is intentionally the single source of strategy truth.

### Process model

One async Python process. No DB, no queue, no scheduler service — APScheduler runs in-process. SIGTERM = graceful shutdown (current cycle finishes, state flushed, exit).

### Two interleaved cycles

Both controlled by env vars (`BOT_LOOP_INTERVAL_SEC` / `BOT_TRADING_CYCLE_SEC`):

- **Monitor cycle** (default 300s) — refresh data, snapshot APR per arb, check `hard_stop_loss`. No opens/closes here other than the hard stop.
- **Trading cycle** (default 3600s, top of hour) — walks the full open/close decision tree.

Funding pays hourly, so the trading cycle is intentionally aligned to that cadence — reacting faster mostly just bleeds slippage.

### Decision tree — the part of the code that matters most

For each open arb, the closer evaluates **in this exact order and stops on the first match**:

`hard_stop_loss → max_hold → min_hold gate → smart_neg_N → smart_decl_N → low_apr_N`

Two rules **always fire** regardless of age: `hard_stop_loss` and `max_hold`. The other four are "soft exits" gated by `BOT_MIN_HOLD_HOURS` and by a `safe_floor` (computed from open APR × `BOT_SAFE_FLOOR_MULT`, floored at `BOT_MIN_NET_APR`). Once `funding_breakeven_achieved` flips true (accrued funding > estimated friction × 1.5), the safe-floor gate is dropped so winners can ride. Full rationale: `docs/STRATEGY.md`.

For new opens, the bot filters `get_funding_strategies` results by APR band, volume floors, blacklist/whitelist, cooldown, adverse-basis, slippage estimate, and per-venue margin cap. Survivors get atomic two-leg open via `batch_create_orders` with bracket SL/TP attached.

### State files — what they are and why

All state lives in plain files on a writable volume; no DB. Paths are env-configurable (`BOT_STATE_FILE`, `BOT_SNAPSHOT_FILE`, `BOT_COOLDOWN_FILE`, `BOT_PID_FILE`).

- **`state.ndjson`** — append-only event log. One JSON event per significant action. **This is the "is-alive" heartbeat** — mtime ticks every monitor cycle; a 10-min gap means the bot is wedged.
- **`state-snapshot.json`** — restartable position snapshot. Written atomically (`.tmp` + rename).
- **`state-cooldown.json`** — per-asset cooldown ledger (assets that hurt you twice in `BOT_PAIR_COOLDOWN_HOURS` are paused).
- **`instance.uuid`** — generated once; used as namespace prefix in every `clientOrderId`.
- **PID file** — single-instance lock. Two copies under the same token will double-open.

### Identity model (don't get this wrong)

- **`instance_uuid`** — this process. Persisted in `instance.uuid`.
- **`arb_id`** — one open pair. Generated at open time.
- **`clientOrderId`** is deterministic per `(instance_uuid, arb_id, leg)`. Two formats:
  - Hyperliquid: `0x` + 32 hex chars (a sha256 prefix). HL rejects other formats.
  - Lighter / Aster: human-readable `vooi-funding-arb-<instance>-<arb>-<leg>`.

  Lighter additionally **drops `clientOrderId` from responses when bracket SL/TP is attached** — the bot has a fallback that matches by `(asset, side, size)`. Don't "fix" the missing coid.

### Logging contract

All structured events go through `NDJsonLog.emit({"event": "...", ...})` (defined in `mvp.py`, near line 732). One event per significant action. **Don't add free-form `print()`** — downstream parsers depend on the NDJSON shape.

### SSE event stream

`fundbot/sse.py:VooiEventStream` maintains a long-lived SSE connection to `/exchange/updates` (default on, `BOT_SSE_ENABLED=true`). The three tight-loop pollers — `survivor_watcher_loop`, the ALO open fill watcher inside `_open_limit_then_market`, and the ALO close fill watcher inside `_close_leg_alo_then_market` — race their `asyncio.sleep` against an `asyncio.Event` from the stream via `sse.wait_first_or_timeout(...)`.

**REST stays the source of truth.** SSE just lets the poll wake up faster — every decision is still confirmed by the same REST call as before. A spurious wake costs one extra REST; a missed wake means the caller falls through to the timeout path. If the stream auth-rejects, crashes, or goes silent for more than `BOT_SSE_HEARTBEAT_TIMEOUT_SEC=60`, callers automatically drop to pure REST polling.

Don't try to "shortcut" the REST check on an SSE wake — the matcher is intentionally conservative and event shapes are not fully documented. Full feature doc: [`docs/SSE.md`](docs/SSE.md).

### Companion tools (separate from the bot)

- **`probe/`** — phase-0 API validator. `uv run python -m probe.probe readonly` is a safe first step before running the bot at all. Subcommands `q1..q11` cover individual API contract checks (idempotency, SSE auth, 5xx behavior, etc.). Several issue real orders — check the help text.
- **`scripts/`** — operational helpers (`close_one.py`, `close_all.py`, `close_orphan.py`, `recover_snapshot.py`, `show_balances.py`, etc.). Each is a small standalone script that imports from `fundbot.mvp` / `probe.client`.
- **`skills/funding-arb-cycle/`** — a Claude Code skill that re-implements the same algorithm as an interactive co-pilot over the VOOI MCP. Useful as a manual fallback or learning tool; shares state file formats with the bot.

## Server-side broker attribution

Broker / integrator attribution is handled server-side by the VOOI API. The bot does **not** read `BOT_BROKER_*` env vars and does **not** set a `broker` field on outgoing orders. If you see legacy references in the code or docs, treat them as bugs.
