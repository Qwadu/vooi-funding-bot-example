---
name: funding-arb-cycle
description: Run one cycle of a delta-neutral funding-rate arbitrage strategy via the VOOI Perps MCP. Scans long/short opportunities across Hyperliquid and Lighter, evaluates currently open arbs against close criteria, proposes open/close actions with rationale, executes only on explicit user confirmation, and persists state to local files. Use when the user asks "run a funding-arb cycle", "check the arb book", "what should I open/close right now", or when invoked on a schedule via /loop.
---

# Funding Arb Cycle — MCP Co-Pilot

This skill executes a single decision cycle for delta-neutral funding-rate arbitrage on the VOOI Perps MCP. It does **not** auto-trade. Every write (order placement, cancel, leverage change) is shown to the user as a plan and only executed after explicit "yes" / "approve".

## What "one cycle" means

A cycle is a single end-to-end pass: read state → fetch market data → fetch positions → score every open arb → score every new opportunity → produce a proposed plan → wait for user approval → execute the approved subset → write state back. After one cycle the skill exits. To run the strategy continuously, the user pairs it with `/loop 60m /funding-arb-cycle` or a scheduled routine.

## Required MCP

The VOOI Perps MCP server must be registered in the user's Claude configuration as `vooi-perps`. Tools will appear under the namespace `mcp__vooi-perps__<tool>`. If the namespace is different, the skill must use whatever prefix the user's environment exposes — discover via available-tools list.

The skill uses exactly these MCP tools:

| Purpose | Tool |
|---|---|
| List ranked arb opportunities | `get_funding_strategies` |
| Pricing / fees / liq price | `get_quotes` |
| Slippage estimate | `estimate_slippage` |
| Market metadata (decimals, max leverage) | `get_markets` |
| Account balances | `get_accounts` |
| Open positions (with `fundingFee`) | `get_positions` |
| Outstanding orders (for SL/TP audit) | `get_open_orders` |
| Place orders (legs + bracket) | `batch_create_orders` (preferred), `create_order` (fallback) |
| Cancel orders | `cancel_order`, `batch_cancel_orders` |
| Historical spread (optional, for smarter floors) | `get_funding_spread_chart` |

Never use deposit / withdraw / transfer / leverage / margin-mode / broker-approval tools in a routine cycle — those are explicit user-initiated operations, surfaced separately.

## State files

Stored in `~/.funding-arb/` (override path via env `FUNDING_ARB_STATE_DIR`):

- `positions.json` — map of `arb_id` → open-arb record. Keys per record:
  - `arb_id`: short hex id created at open time (8 hex chars, e.g. `a3f2b7c1`)
  - `asset`: e.g. `WLD`, `alias:silver`
  - `long_exchange`, `short_exchange`: one of `hyperliquid`, `lighter`, `aster`
  - `long_base_symbol`, `short_base_symbol`: per-venue base symbols (may differ for cross-dex pairs, e.g. `xyz:GOOGL` vs `GOOGL`)
  - `opened_at`: ISO8601 UTC
  - `open_net_apr`: decimal (0.70 = 70%)
  - `leg_collat_usd`, `leg_notional_usd`, `effective_leverage`
  - `long_client_order_id`, `short_client_order_id`: format `mcp-<arb_id>-long` / `-short`
  - `apr_history`: list of decimals, last `MAX_APR_HISTORY=24` entries
  - `peak_funding_cum`: highest observed funding accrual (USD), for fb_achieved
  - `funding_breakeven_achieved`: bool, sticky once true
- `cooldown.json` — map of `asset` → record:
  - `consecutive_losses`: int
  - `last_loss_ts`: ISO8601 UTC
  - When `consecutive_losses >= COOLDOWN_AFTER_LOSSES` AND `now - last_loss_ts < COOLDOWN_HOURS`, the asset is blocked from new opens.
  - On a winning close, the entry is deleted.
- `history.ndjson` — append-only event log: one JSON per line. Events: `CYCLE_STARTED`, `OPEN_PROPOSED`, `OPEN_APPROVED`, `OPEN_OK`, `OPEN_FAIL`, `CLOSE_PROPOSED`, `CLOSE_APPROVED`, `CLOSE_OK`, `CLOSE_FAIL`, `RECONCILE_DRIFT`, `ORPHAN_DETECTED`, `CYCLE_DONE`. Used for post-hoc audit.

If files do not exist on the first run, create them empty (`{}` or empty file).

## Defaults

Read overrides from `~/.funding-arb/config.json` if it exists; otherwise use these defaults. The user can edit either the config file or this SKILL.md to retune.

```
TARGET_EXCHANGES        = ["hyperliquid", "lighter"]
INCLUDE_HL_NON_CRYPTO   = true                          # allow xyz:/km: HL dexes
LEG_COLLAT_USD          = 10                            # margin per leg
MAX_NOTIONAL_PER_POSITION_USD = 100
LEVERAGE_TARGET         = 10
LEVERAGE_CAP            = 5                             # effective cap
MAX_MARGIN_PER_EXCHANGE_USD = 120

# Opening filters
MIN_NET_APR             = 0.70                          # 70%
MAX_NET_APR             = 1.20                          # reject spikes above 120%
APR_UPPER_CAP           = 2.00                          # hard ceiling (likely data error)
MIN_VOLUME_24H_USD      = 100000
PER_LEG_VOLUME_MIN_USD  = 250000
APR_RATIO_1H_TO_24H_MAX = 5                             # 1h spike vs 24h avg
APR_RATIO_24H_TO_7D_MAX = 3
MAX_ADVERSE_BASIS_BPS   = 30                            # entry-spread cap
MAX_SLIPPAGE_BPS        = 200                           # per-leg slippage cap

ASSET_BLACKLIST         = ["YZY","STBL","2Z","CC","CHIP","VVV","MEGA","AZTEC","JTO","STABLE",
                           "alias:mu","alias:bmnr","EIGEN","WLFI","XMR","PYTH","ZRO","CRV"]
ASSET_WHITELIST         = []                            # empty = no whitelist

# Closing criteria
MAX_HOLD_HOURS          = 96
STOP_LOSS_PCT           = 0.05                          # of (leg_collat × 2)
SAFE_FLOOR_MULT         = 0.70                          # safe_floor = max(open_apr*0.7, MIN_NET_APR)
NEGATIVE_WINDOW         = 3
SMART_NEG_VALUE_FLOOR   = -0.10                         # smart_neg fires only if all N readings < -10%
DECLINE_WINDOW          = 6                             # smart_decl: 6 consecutive declines
LOW_APR_THRESHOLD       = 0.10                          # low_apr fires if all N readings < 10%
LOW_APR_WINDOW          = 6
MIN_HOLD_HOURS          = 12                            # soft exits blocked before 12h
FUNDING_BREAKEVEN_SKIP_SAFE_FLOOR = true                # if fb_achieved, smart_neg/decl bypass floor
ESTIMATED_FRICTION_BPS  = 15                            # for fb_achieved calc

# Cooldown
COOLDOWN_AFTER_LOSSES   = 2
COOLDOWN_HOURS          = 48

# Bracket SL/TP
EXCHANGE_SL_ENABLED     = true
EXCHANGE_SL_BUFFER_PCT  = 0.05                          # SL trigger = liq + buffer × (entry - liq)

# Broker / integrator attribution is handled server-side by the VOOI API.
# No client-side broker config is needed.

# History retention
MAX_APR_HISTORY         = 24
```

## Cycle algorithm

### 1. Load state

Read `~/.funding-arb/positions.json` and `~/.funding-arb/cooldown.json`. If a file is missing or malformed, start with `{}` and emit a `STATE_RESET` warning to the user.

### 2. Fetch live data (in parallel)

Run these three MCP calls concurrently — they have no dependency:

- `get_accounts()` → balances per exchange, used for available-margin checks
- `get_positions({ exchanges: TARGET_EXCHANGES })` → current positions, includes `fundingFee` (null on Aster only)
- `get_funding_strategies({ exchanges: TARGET_EXCHANGES, limit: 50 })` → ranked opportunity list

### 3. Reconcile

For each entry in `positions.json`, check that both legs still exist on the exchanges with the expected size and side. Detect three drift types:

- **Closed externally**: position was in state but is missing from `get_positions` on both legs → emit `RECONCILE_DRIFT close_external`, remove from state, do NOT count as win/loss for cooldown.
- **Half-legged**: one leg exists, other does not → emit `RECONCILE_DRIFT half_legged`, propose emergency unwind of the surviving leg as the first close action.
- **Orphan on exchange**: position exists on exchanges (matched by `clientOrderId` prefix `mcp-`) but is NOT in `positions.json` → emit `ORPHAN_DETECTED`, surface to user, do not auto-adopt without confirmation.

### 4. Update per-arb metrics

For each open arb in `positions.json`:

1. Look up the current matching strategy in `get_funding_strategies` output by `(asset, long_exchange, short_exchange)`. If present, take `netApr` as the current reading. If missing (asset no longer in top 50), set current reading to `0` (signals decay).
2. Append the reading to `apr_history`; truncate to last `MAX_APR_HISTORY` entries.
3. Compute funding accrued: sum `fundingFee` across both legs from `get_positions`. Update `peak_funding_cum = max(peak_funding_cum, current_funding)`.
4. Compute friction estimate: `friction_usd = (leg_notional_usd * 2) * (ESTIMATED_FRICTION_BPS / 10000) * 2` (round-trip, both legs). Set `funding_breakeven_achieved = true` if `peak_funding_cum > friction_usd * 1.5`. Once true, this flag is sticky.
5. Compute mark-to-market: `uPnL = sum(unrealizedPnl)` across both legs; `net_usd = uPnL + current_funding`.
6. Compute `held_h = (now - opened_at) in hours`.

### 5. Decide closes — priority order, first match wins

For each arb, evaluate in this exact order:

1. **`hard_stop_loss`** — fires if `net_usd < -STOP_LOSS_PCT × leg_collat × 2`. Example: with $10 collat per leg and `STOP_LOSS_PCT=0.05`, threshold is −$1.00. **Fires regardless of min_hold**.
2. **`max_hold`** — fires if `held_h > MAX_HOLD_HOURS`. Fires regardless of min_hold.
3. **`min_hold` gate** — if `held_h < MIN_HOLD_HOURS`, skip remaining soft-exits and mark `HOLD (min_hold)`.
4. Compute `safe_floor = max(open_net_apr × SAFE_FLOOR_MULT, MIN_NET_APR)` and `check_floor = NOT (funding_breakeven_achieved AND FUNDING_BREAKEVEN_SKIP_SAFE_FLOOR)`. Floor gate: most soft-exits only fire when `current_apr < safe_floor` OR when the gate is skipped because fb is already achieved.
5. **`smart_neg_N`** — if (no `check_floor` gate OR `current_apr < safe_floor`) AND last `NEGATIVE_WINDOW` readings are ALL below `SMART_NEG_VALUE_FLOOR`, fire.
6. **`smart_decl_N`** — if (no `check_floor` gate OR `current_apr < safe_floor`) AND last `DECLINE_WINDOW + 1` readings are strictly declining, fire.
7. **`low_apr_N`** — if `NOT funding_breakeven_achieved` AND last `LOW_APR_WINDOW` readings are ALL below `LOW_APR_THRESHOLD`, fire. (Note: `low_apr` is gated by `fb_achieved` — once funding has paid back friction, low APR alone does not justify closing.)

Tag every close with a single `close_reason` string (e.g. `low_apr_6 hist=[0.05,...] threshold=0.10 fb=false`).

### 6. Decide opens

Start from `get_funding_strategies` results (already sorted by `netApr` descending). Apply filters in this order, rejecting on first failure:

1. `asset` not in `ASSET_BLACKLIST` and (if `ASSET_WHITELIST` non-empty) in `ASSET_WHITELIST`
2. `asset` not currently in `cooldown.json` with active block
3. `asset` not currently in any open arb in `positions.json` (one arb per asset at a time)
4. `MIN_NET_APR ≤ netApr ≤ min(MAX_NET_APR, APR_UPPER_CAP)`
5. `apr_24h ≥ 0` AND `apr_7d ≥ 0` (no headline-only spikes)
6. `apr_1h / max(apr_24h, 0.0001) ≤ APR_RATIO_1H_TO_24H_MAX`
7. `apr_24h / max(apr_7d, 0.0001) ≤ APR_RATIO_24H_TO_7D_MAX`
8. Both legs' `vol24hUsd ≥ MIN_VOLUME_24H_USD`
9. Both legs' `vol24hUsd ≥ PER_LEG_VOLUME_MIN_USD` (stricter per-leg gate)
10. Both legs in `TARGET_EXCHANGES`
11. If `long_exchange == 'hyperliquid' AND long_base_symbol startswith 'xyz:'/'km:'` (or similar HIP-3 prefix) and `INCLUDE_HL_NON_CRYPTO == false`, reject

For each surviving candidate, in `netApr`-descending order:

12. Call `estimate_slippage` on BOTH legs with `notionalUsd = leg_collat × LEVERAGE_TARGET`. Reject if either leg's `slippageBps > MAX_SLIPPAGE_BPS`.
13. Call `get_quotes` on BOTH legs to read entry prices. Compute basis: `basis_bps = abs(long_avgPrice - short_avgPrice) / min(...) × 10000`. Reject if `basis_bps > MAX_ADVERSE_BASIS_BPS`.
14. Read `maxLeverage` from `get_markets` for each leg. `effective_lev = min(LEVERAGE_TARGET, LEVERAGE_CAP, leg1_maxLev, leg2_maxLev)`.
15. Available-margin check: for each exchange, sum the leg_collat of currently-open arbs on that exchange (from `positions.json`). Add this candidate's `leg_collat_usd`. Reject if total > `MAX_MARGIN_PER_EXCHANGE_USD` for either side.
16. Available-balance check: `get_accounts` balance on each exchange ≥ `leg_collat_usd × 1.1` buffer.
17. Stop opening once a portfolio cap is reached (no more than 12 open at once by default), OR once 3 new opens are proposed in one cycle (concurrency limit; revisit next cycle).

### 7. Compute bracket SL/TP for opens

For each open candidate:

- Read `liqPrice` from `get_quotes` per leg.
- Compute SL trigger per leg using buffer: for long, `sl_long = liq_long + (entry_long - liq_long) × EXCHANGE_SL_BUFFER_PCT`; for short, mirror.
- Cross-leg TP: `tp_long = round(sl_short, price_decimals_long, "sell")`; `tp_short = round(sl_long, price_decimals_short, "buy")`. Verify `tp_long > entry_long` AND `tp_short < entry_short` — if not, omit BOTH TPs (keep SLs).
- For Hyperliquid: clamp to max 5 significant figures.
- For exchanges or markets where `liqPrice` is null (e.g. HL cross-margin), omit SL+TP on that leg; the position will rely on the cycle's hard_stop_loss check.

### 8. Build the plan

Format and show to the user as Markdown:

```
## Funding Arb Cycle — <ISO timestamp UTC>

### State
- Open arbs: <n>/12 | Total collat: $X / cap $Y per exchange
- Aggregate uPnL: $X | Funding: +$Y | NET: $Z
- Cooldown active: <list of assets, count, hours_remaining>

### Currently Open
| asset | dir | hrs | uPnL | funding | NET | last APR | fb | streak | decision |
|...|

### Reconcile
- <any drift/orphans>

### Proposed Closes
- <arb_id> <asset> — reason=<close_reason>; will cancel brackets + market-close both legs reduceOnly
  Expected slip: <leg1_slip_bps> / <leg2_slip_bps>; expected realized: <net_usd> + close_friction est. <X>
  ...

### Proposed Opens
- <asset> long=<ex>:<sym> short=<ex>:<sym> netAPR=<x>% vol24=<$Y> lev=<n>x
  Entry: long@<p1> short@<p2>; bracket SL long=<x> short=<y>, TP long=<x> short=<y>
  Collat: $X×2; notional: $Y×2; basis: <bps>; per-leg slip: <bps>
  ...

### Capital after plan
- HL margin used: $A → $B (cap $120)
- Lighter margin used: $A → $B (cap $120)

### Approval
Reply "yes" to execute everything, "yes close only" / "yes open only" for partial,
or "skip" to monitor and exit.
```

### 9. Wait for user approval

Use `AskUserQuestion` if the harness supports it; otherwise emit the plan as text and explicitly wait for the user's reply. **Do not execute any write tool before approval.** Acceptable approvals: `yes`, `approve`, `да`, `ок`, `confirm`, `execute`. Anything else: skip writes, write `CYCLE_DONE skip` to `history.ndjson`, exit.

### 10. Execute approved closes (first)

For each approved close, in order:

1. Cancel outstanding TP/SL bracket orders for both legs:
   - `get_open_orders({ exchanges: [<arb.long_exchange>, <arb.short_exchange>] })`
   - Filter to entries with `baseSymbol == arb.long_base_symbol/short_base_symbol` and `type ∈ {"stopLoss","takeProfit"}` matching the arb's coid where available.
   - Build a `batch_cancel_orders` payload covering all matching orders (one batch per arb).
2. Place reduce-only market close on both legs via `batch_create_orders` with:
   - `exchange`, `asset`, `side` (inverse of entry), `size` (= entry size), `reduceOnly: true`, `clientOrderId: "mcp-<arb_id>-close-<leg>"`. (Broker attribution is set server-side by the API.)
3. Surface per-leg response (status, fill, fees) to user.
4. If both legs closed successfully (status 200/2xx): emit `CLOSE_OK` to history, remove from `positions.json`. Determine win/loss: `realized = uPnL_at_close + funding_cum - close_friction_estimate`. If `realized < 0`, increment `cooldown[asset].consecutive_losses` and set `last_loss_ts = now`. If `realized > 0`, delete `cooldown[asset]`.
5. If only one leg closed: emit `CLOSE_FAIL half_legged` and STOP further cycle actions — surface to user, do not attempt new opens this cycle.

### 11. Execute approved opens

For each approved open:

1. Build `batch_create_orders` payload with TWO orders (long leg + short leg), each with bracket `stopLoss` and `takeProfit` (where computed), and `clientOrderId: "mcp-<arb_id>-<leg>"`. (Broker attribution is set server-side by the API.)
2. Submit as a single batch call. If the venue rejects bracket (HL price out of range, common error: status 400/503 + "out of range"), retry the rejected leg individually with `create_order` and without `stopLoss` / `takeProfit`. Emit `OPEN_RETRY_NO_BRACKET`.
3. If both legs successful: emit `OPEN_OK`, insert into `positions.json` with `apr_history=[netApr]`, `peak_funding_cum=0`, `funding_breakeven_achieved=false`.
4. If long succeeded but short failed: this is a **partial open**. Immediately fire a reduce-only market close on the long leg via `create_order` and emit `OPEN_ROLLBACK`. If rollback fails, emit `EMERGENCY_UNWIND_NEEDED` and STOP the cycle, surface to user with explicit instructions.

### 12. Write state back

- Persist `positions.json` and `cooldown.json` atomically (write to `.tmp`, rename).
- Append `CYCLE_DONE` to `history.ndjson` with summary stats: cycle duration, opens executed, closes executed, errors, total NET delta.

### 13. Output final summary

One-paragraph summary for the user: how many opens / closes happened, what the new portfolio looks like, anything pending follow-up (orphans, half-leg failures, cooldown expansions).

## Important rules

- **Never execute any write without explicit user "yes"** for that exact plan. The MCP instructions enforce this on the server side — violating it may produce 401 / 403 responses. More importantly, the user must always be in the loop.
- **If a write fails with HTTP 401**: the bearer token is stale. Stop the cycle, surface to user, do not retry.
- **If a write fails with 5xx**: do NOT retry automatically inside the cycle. Surface the error verbatim. Retrying can cause double-open (the original may have committed on-venue).
- **Lighter `clientOrderId` is ignored when bracket present**. Match Lighter orders by `(baseSymbol, side, size)` in reconcile. This is a known venue limitation.
- **Hyperliquid bracket prices** must satisfy max 5 sig figs AND max `6 - baseDecimals` decimal places. Round before sending.
- **Aster does not support bracket** `stopLoss`/`takeProfit`. If a leg is on Aster, use a separate trigger order instead, or omit SL/TP. (Currently default `TARGET_EXCHANGES` excludes Aster.)
- **Funding fields on Aster positions are `null`** in `get_positions`. If Aster is later enabled, fall back to estimating funding from `get_funding_spread_chart` × held_h.
- **Concurrency**: never open more than 3 new arbs in one cycle. The user gets a clearer plan and venue rate limits are kinder.
- **State files are the source of truth for `apr_history` and `fb_achieved`** — do not rebuild these from external sources mid-cycle.
- **Time zone**: all timestamps in state are UTC ISO8601.
- **Idempotency**: if the user re-runs `/funding-arb-cycle` within a few seconds, the plan should be substantively identical. Do not re-fetch and re-decide on no-data-change.

## Invocation

The user can invoke this skill in any of these ways:

- `/funding-arb-cycle` — run a single cycle, propose plan, wait for approval
- `/loop 60m /funding-arb-cycle` — repeat every hour while the Claude session is alive
- via `/schedule` cron — for routines that fire even when the user is offline (note: still respects approval flow on next user check-in)

Optional args (passed in the user message):

- `dry` — skip writes regardless of user reply (sandbox mode)
- `monitor` — same as `dry`, used by `/loop` for "just report, don't ask"
- `force-open <asset>` — bypass open filters for one named asset (still subject to slippage and capital checks)
- `force-close <arb_id>` — close a specific arb regardless of close criteria
- `audit` — extra output: per-leg fee breakdown, every filter rejection reason

## What this skill does NOT do

- Open or close positions without user approval
- Deposit, withdraw, transfer funds between exchanges
- Approve brokers / change leverage / change margin mode
- Run continuously without an external trigger
- Track or report PnL across closed positions over time (use the companion `funding-arb-report` skill if available, or query `history.ndjson` directly)
- Replace the venue UIs for emergency interventions — if the user needs to bypass the bot's logic in a crisis, point them to VOOI / HL / Lighter UI directly
