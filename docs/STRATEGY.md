# Strategy — `vooi-funding-arb-bot`

This document explains *what* the bot is trying to do, *how* it decides what to trade, and *why* the defaults are what they are. It is the single source of strategy truth. Tune by editing `.env.example` (defaults) or your `.env` (overrides) — the algorithm itself is described here.

## What problem the bot solves

Perpetual futures don't have an expiry. To keep their price tied to spot, exchanges charge a periodic *funding rate*: longs pay shorts when the contract trades above spot, shorts pay longs when below. Each exchange computes its own funding rate.

When two exchanges disagree about the funding rate for the same asset, you can:
- **Long** the asset on the exchange paying out, and
- **Short** the asset on the exchange charging in.

The two legs cancel each other's price exposure. What remains is the *spread* between the two funding rates — paid to you, per hour, for as long as the spread holds.

That's the trade. Everything else in this bot is risk management around it.

## The annualized yield (netAPR)

The VOOI API returns a list of strategies ranked by `netApr` — the annualized funding-rate spread between two exchanges for the same asset, net of broker fees. The bot consumes this list every cycle. Strategies with high `netApr` are candidates.

A few realities of funding-rate APR:
- It moves. A pair quoted at 80% APR can be at 5% in an hour.
- It can flip sign. A spread that pays you can start charging you.
- The number is annualized but funding is settled hourly — the actual cash per hour is much smaller than the APR suggests.
- Round-trip friction (open + close fees + slippage on two legs) is real money. At 5–7¢ per arb on $50 notional, a position needs ~40–70 hours of funding accrual to break even.

The bot's job is to capture the lucrative spreads while avoiding the ones that decay before paying for friction.

## The cycle

The bot runs two interleaved loops:

- **Monitor cycle** (`BOT_LOOP_INTERVAL_SEC`, default 300s) — refresh state, no trading. Logs portfolio, snapshots APR for each open arb, checks `hard_stop_loss`.
- **Trading cycle** (`BOT_TRADING_CYCLE_SEC`, default 3600s) — every hour on a fixed minute. This is where opens and closes happen.

Why hourly trading and not 5-minutely? Funding pays hourly. Reacting to noise within an hour is mostly chasing slippage.

## Decision tree

### Per open position (every trading cycle)

For each currently open arb, evaluate in this exact order and stop on the first match:

1. **`hard_stop_loss`** — fires when `net_usd < -(BOT_STOP_LOSS_PCT × LEG_COLLAT × 2)`. With $10 collat per leg and 5% stop, the floor is −$1 per arb. **Always fires**, even before `min_hold`.
2. **`max_hold`** — fires when `held_h > BOT_MAX_HOLD_HOURS`. Default 96h. **Always fires**.
3. **`min_hold` gate** — if `held_h < BOT_MIN_HOLD_HOURS`, the soft exits below are skipped and the arb is marked HOLD.
4. **`smart_neg_N`** — if the last `BOT_NEGATIVE_WINDOW` APR readings are *all* below `BOT_SMART_NEG_VALUE_FLOOR` (default −10%), the spread reversed; close.
5. **`smart_decl_N`** — if the last `BOT_DECLINE_WINDOW + 1` readings are strictly declining, the spread is trending the wrong way; close even before it flips.
6. **`low_apr_N`** — if the last `BOT_LOW_APR_WINDOW` readings are *all* below `BOT_LOW_APR_THRESHOLD` (default 10%), funding decayed before paying back open fees; close. **Gated by `funding_breakeven_achieved`**: once an arb has earned more funding than estimated friction (×1.5), this rule is muted — the arb is already in the black.

Soft exits (4–6) also respect a *safe floor*:

```
safe_floor    = max(open_net_apr × BOT_SAFE_FLOOR_MULT, BOT_MIN_NET_APR)
check_floor   = NOT (funding_breakeven_achieved AND BOT_FUNDING_BREAKEVEN_SKIP_SAFE_FLOOR)
```

`check_floor=true` means smart_neg and smart_decl only fire when `current_apr < safe_floor`. Once the arb has paid back friction, the floor is dropped so the arb can ride longer.

### Per new opportunity (every trading cycle)

Walk the ranked opportunity list. Reject on first filter that fails:

1. Asset is not in `BOT_ASSET_BLACKLIST`.
2. If `BOT_ASSET_WHITELIST` is non-empty: asset is in it.
3. Asset is not on cooldown (`BOT_PAIR_COOLDOWN_*`).
4. Asset is not already open in another arb (one arb per asset).
5. `BOT_MIN_NET_APR ≤ netApr ≤ min(BOT_MAX_NET_APR, BOT_APR_UPPER_CAP)`.
6. `apr_24h ≥ 0` AND `apr_7d ≥ 0` (no headline-only spikes).
7. `apr_1h / apr_24h ≤ BOT_APR_RATIO_1H_TO_24H_MAX`.
8. `apr_24h / apr_7d ≤ BOT_APR_RATIO_24H_TO_7D_MAX`.
9. Both legs' `vol24hUsd ≥ BOT_MIN_VOLUME_24H_USD` AND `≥ BOT_PER_LEG_VOLUME_MIN_USD`.
10. Both legs are in `BOT_TARGET_EXCHANGES`.
11. HL HIP-3 dexes (`xyz:`, `alias:`, `km:`) only if `BOT_INCLUDE_HL_NON_CRYPTO=true`. These markets share the standard `hyperliquid:perps:<quote>` margin pool (verified live 2026-05-22 — they appear under the same `type="spot"` account record as crypto-perps, no separate `xyz` bucket).

For survivors, in netAPR-descending order:

12. `get_quotes` on both legs — entry prices, liquidation prices.
13. `estimate_slippage` on both legs — reject if either > `BOT_MAX_SLIPPAGE_BPS`.
14. Adverse-basis check: `basis_bps = abs(long_price - short_price) / min × 10000`. Reject if `> BOT_MAX_ADVERSE_BASIS_BPS`.
15. Effective leverage = `min(BOT_LEVERAGE_TARGET, BOT_LEVERAGE_CAP, market_max_long, market_max_short)`.
16. Available-margin check: per-venue margin used + this leg's collat ≤ `BOT_MAX_MARGIN_PER_EXCHANGE_USD`.
17. Available-balance check: settled balance per venue ≥ `LEG_COLLAT × 1.1`.

Stop once you've proposed enough opens for the cycle (the bot caps concurrent opens to keep venue rate limits happy).

## Bracket SL/TP at open

For each opening:

- **Stop loss** per leg: a price `EXCHANGE_SL_BUFFER_PCT` of the way back toward entry from the liquidation price. Goal: fire *before* the liquidation engine.
- **Take profit (cross-leg)**: each leg's TP is set to the *other* leg's SL price (the same dollar level). If one leg rips toward its liquidation, the other leg's TP fires at the equivalent favorable level — paying for the bad leg.
- Hyperliquid prices are rounded to 5 significant figures (HL rejects over-precision).
- If a leg has no `liquidationPrice` (HL cross-margin returns null sometimes), SL/TP are skipped on that leg. The cycle's `hard_stop_loss` still protects it.

## Entry: limit-then-market

When `BOT_LIMIT_ALO_ENABLED=true`, the bot picks the cheaper-to-make leg and posts a post-only limit (ALO) `BOT_LIMIT_ALO_OFFSET_BPS` away from mid. It polls for fill, and on `BOT_LIMIT_ALO_TIMEOUT_SEC` falls back to market on both legs.

Trade-off: limit captures the maker rebate and avoids taker slippage, at the cost of fill risk. Known sharp edge: if the limit fills *during the cancel*, the bot can end up with a partial position on the limit leg and a full position on the market leg — a *half-leg* state. The reconcile path catches this and emits `RECONCILE_ORPHAN_DETECTED`, then closes the orphan automatically.

## Capital sizing

- `BOT_LEG_COLLAT_USD` × 2 legs per arb. With default $10 per leg, a fully-opened portfolio at 12 arbs uses $240 margin.
- `BOT_LEVERAGE_TARGET` is the wishlist; `BOT_LEVERAGE_CAP` is the hard cap; the actual `effective_lev` is whichever venue minimum binds.
- `BOT_MAX_MARGIN_PER_EXCHANGE_USD` caps each venue separately. The bot will refuse to open if either side would exceed the cap.

## Cooldown

After `BOT_PAIR_COOLDOWN_AFTER_LOSSES` consecutive losing closes on the same asset within `BOT_PAIR_COOLDOWN_HOURS`, the asset is paused. State is persisted in `state-cooldown.json`. A winning close resets the counter.

## State files

All paths configurable via env. Defaults are repo-local for local dev; in Docker they go to `/data/`.

- `state.ndjson` — append-only event log; one JSON object per line. Includes `OPEN_OK`, `CLOSE_OK`, `CYCLE_DONE`, errors, every reconcile event. This is your forensic record.
- `state-snapshot.json` — restartable snapshot. Map of `arb_id` → position record (`apr_history`, `peak_funding_cum`, `funding_breakeven_achieved`, etc.). Re-read on every start.
- `state-cooldown.json` — cooldown ledger. Map of asset → `{ consecutive_losses, last_loss_ts }`.
- `instance.uuid` — bot writes its own 12-char hex UUID on first start. Used in `clientOrderId` for venue-side attribution.

## Why these defaults

These numbers reflect tuning from production runs:

- **Round-trip friction ≈ 5–7¢** per $50-leg arb. Opening a pair costs maker fee + slippage; closing costs taker fee + slippage. Round-trip on both legs is what funding has to pay back.
- **Funding income ≈ $0.001–0.0016 per hour** on a typical $50-leg arb with 50–100% APR. That means **40–70 hours** to break even on friction alone — and the position must hold APR > 0 the whole time.
- **`BOT_MIN_HOLD_HOURS=12`** stops the bot from closing too early on noise. Earlier values (4–6h) closed positions before funding paid for itself.
- **`BOT_LOW_APR_THRESHOLD=0.10`** + **`BOT_LOW_APR_WINDOW=6`** require 6 readings (~30 min) below 10% APR before low-APR closes a position. Tighter thresholds cut profitable positions; looser thresholds keep loser positions around.
- **`BOT_MIN_NET_APR=0.70`** = pickier pool, higher per-trade expectancy. With friction dominating at this scale, 50–70% APR opportunities don't pay back often enough.
- **`BOT_PAIR_COOLDOWN_AFTER_LOSSES=2` / `BOT_PAIR_COOLDOWN_HOURS=48`** = if an asset hurt you twice in 48h, sit out and let conditions change.

When the bot is running on more capital, friction-per-arb drops (fee is mostly bps × notional), so the breakeven hours drop and you can afford to widen the opportunity pool. Re-tune accordingly.

## Things that intentionally aren't here

- **No directional trading.** This bot is delta-neutral by construction. Removing one leg makes it a different (more dangerous) strategy.
- **No martingale / averaging-down.** If a position loses, it closes. It doesn't double up.
- **No oracle / external data.** Every input is from the VOOI API.
- **No discretionary overrides via Slack/Telegram.** Operate it via `BOT_DRY_RUN`, config, or by stopping the process and editing state files.

## When to expect the bot to lose money

- **Decay before breakeven.** The spread you opened on collapses before funding accrued enough to cover friction. This is the dominant loss mode.
- **Adverse spread on entry.** A wide basis on entry that doesn't mean-revert; legs converge slowly and uPnL stays negative.
- **Half-leg events.** Partial fills on the limit leg paired with a market leg create exposure. The bot self-heals via reconcile, but you eat the friction.
- **Venue / API outages.** Funding accrues regardless, but you can't act on it.

Operate small. Watch the first 7 days of `state.ndjson` carefully. Tune from real data, not from the defaults' opinion of what should work.
