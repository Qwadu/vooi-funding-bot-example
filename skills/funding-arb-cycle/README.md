# Funding Arb Cycle — a Claude Code skill for VOOI Perps

This is a Claude Code **skill** that runs funding-rate arbitrage on the VOOI Perps API through its MCP server, with a human in the loop on every order.

If you're new to all this:

- **VOOI Perps** is an aggregator. It lets one account trade perpetual futures across several exchanges (Hyperliquid, Lighter, Aster) through a single API. Each exchange runs its own funding-rate mechanism.
- **Funding rate** is a periodic payment between longs and shorts on a perpetual future. When the rate is positive, longs pay shorts; when negative, shorts pay longs. Each exchange has its own rate for the same asset.
- **Funding arbitrage** opens a long position on one exchange and a short position on another, for the same asset and the same size. The two legs cancel out price exposure (delta-neutral). What's left is the difference between the two funding rates — the *spread*. If the spread stays positive across both legs after fees, the position earns money over time without taking directional risk.
- **MCP** stands for Model Context Protocol — a way for AI assistants like Claude to call external tools. The VOOI Perps MCP exposes 43 tools for trading, account management, and discovery.
- A **skill** is a markdown file with instructions that Claude follows when you invoke it. The skill itself doesn't run code; it tells Claude how to think about a task and which tools to call.

This skill turns the strategy that used to live in a long-running Python bot into something Claude can run on demand, with you approving each trade.

---

## What you get

When you run the skill, Claude does one full cycle:

1. Pulls your current positions and the current list of funding-arb opportunities from VOOI MCP.
2. Reads its own memory files to see history per position.
3. Decides which open positions should close and which new opportunities are worth opening.
4. Shows you a plan with numbers and reasons.
5. Waits for your "yes".
6. Executes only what you approved.
7. Writes its memory back to disk so the next cycle continues where this one left off.

Each cycle takes about 30–60 seconds of Claude time plus your approval time. To run it on a schedule, pair it with `/loop` or `/schedule`.

---

## Who this is for

- Traders who want to run funding-arb on VOOI but don't want to maintain a Python bot.
- Anyone learning how delta-neutral funding strategies work — the skill is a readable spec of one working strategy.
- Builders extending the strategy: every threshold and rule is in one markdown file, so tuning is one edit away.

You do **not** need to be a programmer. You need:

- A Claude Code installation
- A VOOI Perps account with at least one venue connected and balance on it
- Your VOOI bearer token

---

## How it works — the algorithm in plain English

### Once per cycle

**Step 1. Read memory.** Claude opens two small files in `~/.funding-arb/`:

- `positions.json` — what's currently open and the history of each position.
- `cooldown.json` — which assets are paused after recent losses.

If these files don't exist (first run), they start empty.

**Step 2. Fetch live data.** Claude calls three MCP tools in parallel — they don't depend on each other so doing them at the same time is faster:

- `get_accounts` — how much money is on each exchange right now.
- `get_positions` — every open position on every exchange, including how much funding has been paid into each one so far.
- `get_funding_strategies` — the current sorted list of arb opportunities, ranked by annualized yield (netAPR).

**Step 3. Reconcile.** Claude compares what its memory says is open against what the exchanges actually show. Three things can be wrong:

- A position the memory thinks is open isn't on either exchange — it was closed outside the skill (maybe by you, via the UI). Memory is wrong. Claude removes it quietly, no penalty to the asset's cooldown.
- One leg is on the exchange, the other isn't — *half-legged*. This is a problem; the price exposure is no longer neutral. Claude proposes closing the surviving leg immediately.
- A position is on the exchange but not in memory — *orphan*. Could be old residue from a previous tool, could be a manual trade. Claude doesn't auto-adopt it; it asks you what to do.

**Step 4. Update history.** For every position the memory tracks, Claude:

- Adds the current opportunity's APR reading to a 24-entry rolling history (`apr_history`).
- Adds up the funding paid to each leg to get total accrued funding for the arb.
- Marks the position as "funding-breakeven achieved" once that total exceeds the estimated round-trip fees by 1.5×. This flag is sticky — once it flips to true, it stays true.

This history is what the close criteria look at. Without it, the strategy can't distinguish "APR briefly dipped" from "APR has been dead for an hour".

**Step 5. Decide closes.** For each open position, Claude evaluates a strict priority list, top to bottom, first match wins:

1. **`hard_stop_loss`** — net dollar P&L is below the kill-switch threshold (default −$1 per arb on $10/leg). Always fires, even before the min-hold age.
2. **`max_hold`** — the position is older than the max hold (default 96 hours). Always fires.
3. **min-hold check** — if the position is younger than the min-hold (default 12 hours), every soft exit below is skipped and the position is marked HOLD.
4. **`smart_neg_3`** — last 3 APR readings are all below −10%. The spread reversed; the strategy is losing money on funding.
5. **`smart_decl_6`** — 6 readings in a row trending down. Even if APR is still positive, the trend says it'll be unprofitable soon.
6. **`low_apr_6`** — last 6 readings all below 10% APR, and the position has NOT yet earned back its open fees. Funding decayed before paying for itself; cut losses.

If a position has already paid back its fees (`funding_breakeven_achieved=true`), `low_apr` is ignored — the position is already in the black.

For positions older than min-hold but with no exit triggered, the rules `smart_neg` and `smart_decl` only fire if the current APR is below a *safe floor* (default = 70% of the APR at open, with a hard minimum of `MIN_NET_APR`). Once the position has paid back fees, this floor is dropped — letting profitable positions ride longer.

**Step 6. Decide opens.** Claude walks the opportunity list (already sorted by netAPR) and filters out:

- Assets on the blacklist
- Assets in active cooldown
- Assets that are already open in another arb
- Opportunities outside the APR band (default 70%–120%, plus a hard ceiling of 200% to reject data errors)
- Opportunities whose 1-hour APR is wildly above the 24-hour average (a spike that won't last)
- Opportunities below volume floors (illiquid pairs lose on slippage)
- Opportunities not in the configured target exchanges

For everything left, Claude calls `estimate_slippage` on both legs to make sure each leg can be filled cheaply, then calls `get_quotes` to check the entry spread isn't already too wide. Failing either check → skip.

Then capital checks: per-exchange margin cap, per-position notional cap, and a hard limit of 3 new opens per cycle.

**Step 7. Compute SL/TP.** For each opening, Claude computes bracket orders:

- **Stop loss**: a price near each leg's liquidation level, with a 5% buffer back toward entry (so the stop fires *before* the liquidation engine does).
- **Take profit (cross-leg)**: each leg's TP is set to the other leg's SL price. This is what makes the bracket symmetric — if one side rips toward liquidation, the other side hits a fat profit that pays for the loss.

For Hyperliquid, prices are rounded to 5 significant figures (Hyperliquid rejects over-precise inputs). If the exchange can't return a liquidation price (e.g. HL cross-margin), SL/TP are skipped on that leg; the cycle's hard_stop_loss check still protects it.

**Step 8. Build the plan and ask.** Claude prints a Markdown report:

- Current portfolio state
- Proposed closes with reasons
- Proposed opens with entry prices, SL/TP, expected slippage, expected fees
- Capital used before and after the plan

Then it asks: `yes` / `yes close only` / `yes open only` / `skip`.

**Step 9. Execute.** Only after you say yes does Claude call write tools:

- For closes: cancel the bracket orders, then `batch_create_orders` with `reduceOnly=true` on both legs.
- For opens: `batch_create_orders` with both legs in one call, each leg carrying its bracket SL/TP.

If a leg fails, Claude rolls back: a partial open is unwound immediately, a half-leg close is surfaced as a problem and the cycle stops.

**Step 10. Write memory.** Updated `positions.json` and `cooldown.json` are written atomically. An event line is appended to `history.ndjson` for audit.

---

## MCP tools used

| Tool | When | Why |
|---|---|---|
| `get_accounts` | Every cycle | Margin available per exchange |
| `get_positions` | Every cycle | Actual position state, funding accrued per leg |
| `get_funding_strategies` | Every cycle | Ranked opportunities |
| `get_open_orders` | Before closes | Find the bracket orders to cancel |
| `get_markets` | When opening | Per-market decimals, max leverage |
| `get_quotes` | When opening | Entry prices, liq price for SL |
| `estimate_slippage` | When opening | Reject illiquid legs |
| `batch_create_orders` | On approval | Place both legs atomically |
| `batch_cancel_orders` | On approval | Cancel bracket orders before closes |
| `create_order` | Fallback only | Retry one leg without bracket if venue rejects |
| `cancel_order` | Edge cases | Single cancel when batch is overkill |
| `get_funding_spread_chart` | Optional | Smarter safe-floor based on historical spread |

The skill never calls deposits, withdrawals, transfers, leverage changes, margin-mode changes, or broker approvals from inside a cycle. Those are explicit human-initiated operations.

---

## Configuration

All settings live in two places:

1. **Defaults inside `SKILL.md`** — read by Claude on every invocation. Edit this file to retune the strategy globally.
2. **Optional `~/.funding-arb/config.json`** — overrides specific keys without touching the skill. Use this for environment-specific overrides (e.g. higher capital on prod, lower on test).

The most useful settings to tune:

| Setting | Default | What it does |
|---|---|---|
| `MIN_NET_APR` | 0.70 | Don't open below 70% APR. Higher = pickier pool, fewer trades. |
| `MAX_NET_APR` | 1.20 | Reject crazy-high spreads (often noise). |
| `LEG_COLLAT_USD` | 10 | Margin per leg. Bigger = bigger PnL per trade, more risk. |
| `LEVERAGE_TARGET` | 10 | Target effective leverage on the position. |
| `LEVERAGE_CAP` | 5 | Hard cap regardless of target. |
| `MIN_HOLD_HOURS` | 12 | Don't soft-close before this age. Funding needs time to accrue. |
| `MAX_HOLD_HOURS` | 96 | Force-close after this age regardless of P&L. |
| `LOW_APR_THRESHOLD` | 0.10 | Soft-close after APR sustained below 10%. |
| `LOW_APR_WINDOW` | 6 | Number of readings (≈ 30 min) before low_apr fires. |
| `SMART_NEG_VALUE_FLOOR` | −0.10 | smart_neg only fires when APR sustained below −10%. |
| `ASSET_BLACKLIST` | (list) | Skip these symbols entirely. |
| `COOLDOWN_AFTER_LOSSES` | 2 | After 2 losses on the same asset within 48 hours, pause it. |

---

## State files

In `~/.funding-arb/` (override with the env var `FUNDING_ARB_STATE_DIR`):

### `positions.json`

One object per currently open arb:

```json
{
  "a3f2b7c1": {
    "arb_id": "a3f2b7c1",
    "asset": "WLD",
    "long_exchange": "hyperliquid",
    "short_exchange": "lighter",
    "long_base_symbol": "WLD",
    "short_base_symbol": "WLD",
    "opened_at": "2026-05-18T11:43:42Z",
    "open_net_apr": "0.5361",
    "leg_collat_usd": "10",
    "leg_notional_usd": "50",
    "effective_leverage": 5,
    "long_client_order_id": "mcp-a3f2b7c1-long",
    "short_client_order_id": "mcp-a3f2b7c1-short",
    "apr_history": ["0.5361", "0.4823", "..."],
    "peak_funding_cum": "0.1918",
    "funding_breakeven_achieved": false
  }
}
```

### `cooldown.json`

```json
{
  "ALIAS:MU": {
    "consecutive_losses": 2,
    "last_loss_ts": "2026-05-17T06:06:18Z"
  }
}
```

### `history.ndjson`

Append-only, one JSON object per line:

```json
{"ts":"2026-05-18T12:00:01Z","event":"CYCLE_STARTED"}
{"ts":"2026-05-18T12:00:08Z","event":"CLOSE_PROPOSED","arb_id":"a3f2b7c1","reason":"low_apr_6 ..."}
{"ts":"2026-05-18T12:00:24Z","event":"CLOSE_APPROVED","arb_id":"a3f2b7c1"}
{"ts":"2026-05-18T12:00:31Z","event":"CLOSE_OK","arb_id":"a3f2b7c1","realized_usd":"+0.0312"}
{"ts":"2026-05-18T12:00:32Z","event":"CYCLE_DONE","duration_sec":31,"closes":1,"opens":0}
```

---

## Setup

### 1. Install the MCP server

In your Claude Code config (`~/.claude/config.json` or `.mcp.json` in your project), add:

```json
{
  "mcpServers": {
    "vooi-perps": {
      "type": "http",
      "url": "https://perps-api.vooi.io/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_VOOI_BEARER_TOKEN"
      }
    }
  }
}
```

Replace `YOUR_VOOI_BEARER_TOKEN` with the token from your VOOI account. After saving, restart Claude Code so it picks up the new server.

Verify with `/mcp` — you should see `vooi-perps` listed and `get_funding_strategies` callable.

### 2. Install the skill

Copy `funding-arb-cycle/` into your Claude skills directory:

```bash
mkdir -p ~/.claude/skills
cp -r funding-arb-cycle ~/.claude/skills/
```

In a fresh Claude session, `/funding-arb-cycle` should now appear as an available skill.

### 3. Set the state directory

```bash
mkdir -p ~/.funding-arb
```

(Or set `FUNDING_ARB_STATE_DIR` to a different path.)

### 4. First run

```
/funding-arb-cycle dry
```

`dry` mode runs the full cycle but skips writes regardless of your reply — a safe way to see the plan format on your actual portfolio.

---

## Example outputs

### A typical cycle plan (truncated)

```
## Funding Arb Cycle — 2026-05-18T12:00:00Z

### State
- Open arbs: 5/12 | Total collat: $50 / cap $120 per exchange (HL $20, Lighter $30)
- Aggregate uPnL: -$0.37 | Funding: +$0.22 | NET: -$0.10
- Cooldown active: ALIAS:MU (2/2, 28h remaining)

### Currently Open
| asset | dir | hrs | uPnL | funding | NET | last APR | fb | streak | decision |
|---|---|---:|---:|---:|---:|---:|:-:|:-:|---|
| WLD | hyp→lig | 73.5 | -0.02 | +0.19 | +0.18 | 0.45 | ❌ | 0 | HOLD |
| BIO | hyp→lig | 7.3 | -0.05 | 0.00 | -0.04 | -0.04 | ❌ | 3 | HOLD (min_hold) |
| LIT | hyp→lig | 5.3 | +0.03 | +0.00 | +0.03 | 1.08 | ❌ | 0 | HOLD (min_hold) |
| S   | hyp→lig | 3.3 | -0.10 | 0.00 | -0.08 | 0.61 | ❌ | 0 | HOLD (min_hold) |
| alias:bmnr | lig→hyp | 8.2 | -0.20 | +0.03 | -0.18 | 0.27 | ❌ | 0 | HOLD (min_hold) |

### Reconcile
- DASH found on both venues but not in state — ORPHAN. Suggest manual close or re-adopt.

### Proposed Closes
(none this cycle)

### Proposed Opens
1. GMX — long=hyperliquid:GMX, short=lighter:GMX, netAPR=83.4%, vol24=$2.1M, lev=5x
   Entry: long@27.21 short@27.23; basis 7.4bps, per-leg slip 8/12 bps
   Bracket: long SL=24.49 TP=29.40, short SL=29.40 TP=24.49
   Collat: $10×2; notional: $50×2
2. AVNT — long=lighter:AVNT, short=hyperliquid:AVNT, netAPR=78.9%, vol24=$1.4M, lev=5x
   Entry: long@0.4233 short@0.4235; basis 4.7bps, per-leg slip 11/9 bps
   Bracket: long SL=0.3811 TP=0.4663, short SL=0.4663 TP=0.3811
   Collat: $10×2; notional: $50×2

### Capital after plan
- HL margin used: $20 → $40 (cap $120)
- Lighter margin used: $30 → $50 (cap $120)

### Approval
Reply "yes" to execute everything, "yes open only" to skip the orphan note,
or "skip" to monitor and exit.
```

### After approval

```
Executing 2 opens...

✅ GMX opened — arb_id=4b7e2c9a, both legs filled, brackets attached
✅ AVNT opened — arb_id=8d1f0a3e, both legs filled, brackets attached

State updated. 2 new arbs, 7 total open.
DASH orphan flagged for manual handling (not auto-adopted).
```

### After a skip

```
No writes performed. State unchanged.

If you want to revisit, run `/funding-arb-cycle` again.
For continuous monitoring without prompts, use `/funding-arb-cycle monitor` under /loop.
```

---

## Limitations and safety notes

**Not autonomous.** The VOOI MCP server explicitly requires human confirmation for every write. This skill respects that. If you want full autonomy, you need a real bot — the strategy in `SKILL.md` can be a starting reference.

**Latency.** From the moment a position should close to the moment you tap "yes", any amount of time can pass. The bot version closed in seconds; this skill closes when you respond. For funding strategies this is usually fine — funding rates move on hour-scale, not second-scale. But during venue outages or flash dislocations, the gap can hurt.

**State is local.** If you delete `~/.funding-arb/`, the skill loses memory of which arb_id maps to which position. The reconcile step will surface every position as an orphan. Treat the state directory like a wallet — back it up if it matters.

**Concurrency.** Running two Claude sessions with the same VOOI token at the same time will give each a different view of state and may double-open. Don't.

**Token rotation.** When the VOOI bearer token expires, every MCP call returns 401. The skill stops cleanly and surfaces the error. Update the token in your Claude config, then re-run.

**No cross-session learning.** The skill doesn't remember previous tunings or what you said last time. Conventions live in `SKILL.md`; tuning lives in `config.json` or the SKILL.md defaults.

**Aster support.** Aster is listed in the supported exchanges but currently:
- Aster doesn't return funding accrued in `get_positions` (the `fundingFee` field is null).
- Aster doesn't support bracket SL/TP.
For now, leave Aster out of `TARGET_EXCHANGES` and use Hyperliquid + Lighter only.

**Hyperliquid HIP-3 dexes.** Markets on HL with `xyz:` or `km:` prefixes are independent dexes built on top of Hyperliquid. Some pairs (US equities, FX, commodities) are only available on these dexes. The skill includes them by default (`INCLUDE_HL_NON_CRYPTO=true`); turn off if you only want crypto.

**This is not financial advice.** The strategy can lose money. The defaults are a starting point. Test with small position sizes first.

---

## How to extend

The strategy lives in one file (`SKILL.md`). Common extensions:

- **Add a new close criterion.** Add a new bullet to Step 5 in the algorithm section. Pick a unique reason name (`my_criterion_N`) that will appear in `history.ndjson` for audit.
- **Add a new filter.** Add a step to the Step 6 list. Reject candidates that fail.
- **Tune per-asset.** Add a `PER_ASSET_OVERRIDES` map in the defaults. When evaluating a candidate, check the map for asset-specific min_net_apr / leg_collat / etc.
- **Add a new exchange.** When a new venue ships on VOOI MCP, add it to `TARGET_EXCHANGES`. The rest of the algorithm is venue-agnostic.

---

## Where this came from

This skill is the strategy of the companion Python bot in this repository, distilled to its decision logic. The bot ran for a few weeks and produced detailed analytics on what worked and what didn't. The defaults in `SKILL.md` reflect tuning from May 2026:

- Round-trip friction is ~5–7¢ per arb on $50 notional → positions need 40–70 hours of funding to break even.
- Median hold was 8 hours under the old config — too short. `MIN_HOLD_HOURS=12` and `LOW_APR_THRESHOLD=0.10` were the biggest improvements.
- About 60% of closes were "low APR" before tuning — assets that decayed too fast. Blacklisting the 8 worst offenders and raising the min APR floor cut that fraction.

Future work, if anyone wants to take it on:

- Replace the rolling `apr_history` with `get_funding_spread_chart` queries. The MCP gives hourly history; the bot only saw what its cycles caught.
- Add a `funding-arb-report` skill that summarizes `history.ndjson` into weekly/monthly P&L.
- Add a `funding-arb-close-one` skill for ad-hoc single-arb closes.

---

## Quick reference

| Command | What it does |
|---|---|
| `/funding-arb-cycle` | One cycle, ask before each write |
| `/funding-arb-cycle dry` | One cycle, never write |
| `/funding-arb-cycle monitor` | One cycle, report only — for `/loop` |
| `/funding-arb-cycle audit` | One cycle with extra detail per filter |
| `/funding-arb-cycle force-open WLD` | Force-open one named asset (still checks slippage and capital) |
| `/funding-arb-cycle force-close <arb_id>` | Close a specific arb |
| `/loop 60m /funding-arb-cycle` | Run hourly while Claude is open |

Made with intent. Use with care.
