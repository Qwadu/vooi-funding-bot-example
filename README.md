> **Disclaimer**
>
> These examples are provided for educational purposes only. They are not financial advice and do not guarantee profit.
>
> The current bot examples were generated with AI-assisted development and tested before publication. They demonstrate how VOOI Ultra users can quickly prototype trading bots and agentic trading workflows using VOOI Perps API access available through VOOI Ultra.
>
> Testing does not make the bots risk-free. Trading bots can place real orders and interact with real funds. Review the code, configuration, strategy, and risk controls before running any bot with live capital.

---

# vooi-funding-arb-bot

A production-grade Python bot for **delta-neutral funding-rate arbitrage** on perpetual futures, running through the [VOOI Perps API](https://perps-api.vooi.io/docs). Trades Hyperliquid and Lighter today; Aster supported in code, off by default.

> **Real money software.** This bot places real orders against real funds. It can lose money. Read [`docs/STRATEGY.md`](docs/STRATEGY.md) and the disclaimer in [`LICENSE`](LICENSE) before running it with live capital.

---

## What it does in one paragraph

The bot scans funding-rate spreads across two exchanges every hour. When it finds a pair where the spread is large enough to pay back trading friction within a reasonable hold time, it opens a long position on the exchange paying out funding and a short position on the exchange charging in — same asset, same size, opposite sides. The two legs cancel each other's price exposure; the bot collects the funding-rate difference for as long as the spread holds. It closes positions when the spread decays, reverses, or the position hits stop-loss thresholds.

That is the entire strategy. Everything else in this codebase is risk management around it.

## When this bot is useful

- You have a VOOI Perps account with balance on at least two venues.
- You want to earn yield from cross-venue funding-rate spreads without taking directional risk.
- You're willing to lose 10–20% of position notional in worst-case scenarios (venue outages, half-leg fills, sudden funding flips).
- You'll monitor the bot — daily, not 24/7. It's autonomous but it's not foolproof.

## When this bot is *not* useful

- You don't have a VOOI account or can't connect at least two venues.
- You want directional exposure to crypto. (This bot is delta-neutral by design.)
- You expect double-digit monthly returns. Realistic ballpark: low single-digit percentages monthly, highly variable.
- You won't read logs. The bot emits structured NDJSON for a reason — it's the only way to understand what it did and why.

---

## Running the bot

Two supported paths are documented inline below: **local** (macOS/Linux dev machine, foreground or `nohup`) and **dedicated Linux VPS** (systemd unit, autostart, log rotation). For Docker and Fly.io, see [`docs/DEPLOY.md`](docs/DEPLOY.md).

Before either path, prepare:

- A VOOI Perps account with balance on at least two venues.
- Your VOOI bearer token — get it at [ultra.vooi.io/api-tokens](https://ultra.vooi.io/api-tokens).

### Run locally (macOS / Linux)

Good for first-time setup, dry-runs, monitoring the bot from your dev machine.

```bash
# 1. Install uv (Python package manager)
#    macOS:
brew install uv
#    Linux (or macOS without Homebrew):
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and install
git clone https://github.com/your-org/vooi-funding-arb-bot
cd vooi-funding-arb-bot
uv sync --extra dev

# 3. Configure
cp .env.example .env
$EDITOR .env                                  # fill VOOI_BEARER_TOKEN

# 4. Validate the API contract (optional, recommended on first run)
uv run python -m probe.probe readonly

# 5. Dry-run — no orders, just decisions written to the log
BOT_DRY_RUN=true uv run python -m fundbot

# 6. Live — only after you've watched a few dry-run cycles
BOT_DRY_RUN=false uv run python -m fundbot
```

To background the bot in the same shell session without `systemd`:

```bash
nohup uv run python -m fundbot >> bot.log 2>&1 &
echo $! > /tmp/vooi-funding-arb-bot.pid
disown
```

Stop gracefully (current cycle finishes, state is persisted):

```bash
kill -TERM "$(cat /tmp/vooi-funding-arb-bot.pid)"
```

Tail logs while running:

```bash
tail -f bot.log              # human-readable stdout
tail -f state.ndjson         # one JSON event per line (machine-readable)
```

### Run on a dedicated VPS (Linux, systemd)

Recommended for live trading: the bot is a long-running process and benefits from a supervised, restart-on-failure environment. Instructions are written for Ubuntu 22.04 / 24.04 or Debian 12 — adapt the package manager for other distros.

**1. Provision the box.** A 1 vCPU / 1 GB RAM VPS is enough. Pick a region close to the VOOI API for latency (e.g. Tokyo, Frankfurt). Make sure outbound HTTPS to `perps-api.vooi.io` is allowed.

**2. Create a dedicated system user.** The bot does not need root.

```bash
sudo adduser --system --group --shell /bin/bash --home /opt/vooi-arb vooi-arb
sudo install -d -o vooi-arb -g vooi-arb /opt/vooi-arb /var/lib/vooi-arb /var/log/vooi-arb
```

`/var/lib/vooi-arb` will hold state files; `/var/log/vooi-arb` will hold rotated logs.

**3. Install Python and `uv`.**

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git curl ca-certificates
sudo -u vooi-arb -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

`uv` lands at `/opt/vooi-arb/.local/bin/uv`. Add it to the bot user's PATH (done via the systemd unit below).

**4. Clone the repo and install dependencies.**

```bash
sudo -u vooi-arb -H git clone https://github.com/your-org/vooi-funding-arb-bot /opt/vooi-arb/app
cd /opt/vooi-arb/app
sudo -u vooi-arb -H /opt/vooi-arb/.local/bin/uv sync
```

**5. Configure `.env`.** Permissions matter — the file holds your bearer token.

```bash
sudo -u vooi-arb -H cp /opt/vooi-arb/app/.env.example /opt/vooi-arb/app/.env
sudo -u vooi-arb -H $EDITOR /opt/vooi-arb/app/.env
sudo chmod 600 /opt/vooi-arb/app/.env
```

Set in `.env` (the values shown move state out of the repo and into the var dir):

```bash
BOT_DRY_RUN=true
BOT_STATE_FILE=/var/lib/vooi-arb/state.ndjson
BOT_SNAPSHOT_FILE=/var/lib/vooi-arb/state-snapshot.json
BOT_COOLDOWN_FILE=/var/lib/vooi-arb/state-cooldown.json
BOT_PID_FILE=/var/lib/vooi-arb/bot.pid
```

**6. (Optional) Run probe + dry-run once manually before installing the service.**

```bash
sudo -u vooi-arb -H bash -c 'cd /opt/vooi-arb/app && /opt/vooi-arb/.local/bin/uv run python -m probe.probe readonly'
sudo -u vooi-arb -H bash -c 'cd /opt/vooi-arb/app && BOT_DRY_RUN=true /opt/vooi-arb/.local/bin/uv run python -m fundbot' &
# Watch /var/lib/vooi-arb/state.ndjson for a few cycles, then Ctrl-C.
```

**7. Install the systemd unit.** Write `/etc/systemd/system/vooi-arb.service`:

```ini
[Unit]
Description=vooi-funding-arb-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vooi-arb
Group=vooi-arb
WorkingDirectory=/opt/vooi-arb/app
EnvironmentFile=/opt/vooi-arb/app/.env
Environment=PATH=/opt/vooi-arb/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/vooi-arb/.local/bin/uv run python -m fundbot
Restart=on-failure
RestartSec=15
KillSignal=SIGTERM
TimeoutStopSec=120
StandardOutput=append:/var/log/vooi-arb/bot.log
StandardError=append:/var/log/vooi-arb/bot.log

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/vooi-arb /var/log/vooi-arb /opt/vooi-arb/app

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vooi-arb.service
sudo systemctl status vooi-arb.service
```

**8. Operate the service.**

```bash
# Tail human-readable bot output:
sudo tail -f /var/log/vooi-arb/bot.log

# Tail structured events (one JSON per line):
sudo tail -f /var/lib/vooi-arb/state.ndjson

# Restart (graceful SIGTERM; current cycle finishes first):
sudo systemctl restart vooi-arb.service

# Stop:
sudo systemctl stop vooi-arb.service
```

**9. Rotate logs.** Add `/etc/logrotate.d/vooi-arb`:

```
/var/log/vooi-arb/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su vooi-arb vooi-arb
}
```

**10. Flip to live trading.**

```bash
sudo -u vooi-arb -H sed -i 's/^BOT_DRY_RUN=.*/BOT_DRY_RUN=false/' /opt/vooi-arb/app/.env
sudo systemctl restart vooi-arb.service
```

**Upgrades.**

```bash
sudo systemctl stop vooi-arb.service
sudo -u vooi-arb -H bash -c 'cd /opt/vooi-arb/app && git pull && /opt/vooi-arb/.local/bin/uv sync'
# Diff .env.example for new keys, update .env if needed.
sudo systemctl start vooi-arb.service
sudo journalctl -u vooi-arb.service -n 50 --no-pager
```

**Health check.** The bot's "is-alive" signal is the mtime of `state.ndjson` — it ticks every monitor cycle (5 min by default). A 10-minute gap means the process is wedged. A minimal external check:

```bash
[[ $(($(date +%s) - $(stat -c %Y /var/lib/vooi-arb/state.ndjson))) -lt 600 ]]
```

For container-based deployment (Docker / Fly.io) and a deeper troubleshooting checklist, see [`docs/DEPLOY.md`](docs/DEPLOY.md).

---

## How it works (the algorithm at a glance)

Each cycle, the bot:

1. **Reads state** from `state-snapshot.json` (positions) and `state-cooldown.json` (cooldown ledger).
2. **Fetches live data** from VOOI in parallel: `get_funding_strategies` (ranked opportunities), `get_positions` (with funding accrual per leg), `get_accounts` (margin balances).
3. **Reconciles** state against reality. Detects positions closed externally, half-leg states, and orphan positions on the exchange that aren't in state.
4. **Updates per-arb history**: appends the current netAPR reading to a rolling 24-entry list, updates funding accrued, updates the sticky `funding_breakeven_achieved` flag.
5. **Decides closes** by walking a priority list — `hard_stop_loss` → `max_hold` → `min_hold gate` → `smart_neg_N` → `smart_decl_N` → `low_apr_N`. First match wins.
6. **Decides opens** by filtering the opportunity list — APR band, volume floors, blacklist, cooldown, slippage estimate, basis check, per-venue margin cap. Survivors get bracket SL/TP placed alongside the entry order.
7. **Executes** with `batch_create_orders` (atomic two-leg open) or per-leg `create_order` for closes. Broker / integrator attribution is set server-side by the VOOI API; the bot does not need to pass any builder/integrator IDs.
8. **Writes state back** atomically and emits an `NDJSON` event line summarizing the cycle.

Full algorithm with thresholds and rationale: [`docs/STRATEGY.md`](docs/STRATEGY.md).

---

## Architecture

```
                  ┌──────────────────────────┐
                  │   VOOI Perps REST API    │  ←─ single integration point
                  └────────────┬─────────────┘
                               │
              ┌────────────────┴─────────────────┐
              │            fundbot/              │
              │  ─────────────────────────────   │
              │   mvp.py     — engine            │
              │     • cycle loop                 │
              │     • opportunity ranking        │
              │     • close decision tree        │
              │     • limit-then-market entry    │
              │     • reconcile + orphan close   │
              │     • bracket SL/TP placement    │
              │     • NDJSON event log           │
              └────────────────┬─────────────────┘
                               │
              ┌────────────────┴─────────────────┐
              │           state files            │
              │  ─────────────────────────────   │
              │   state.ndjson         (events)  │
              │   state-snapshot.json  (resume)  │
              │   state-cooldown.json  (block)   │
              │   instance.uuid        (coid ns) │
              └──────────────────────────────────┘
```

The bot is a single async Python process. No external services, no database, no message queue. State lives in JSON / NDJSON files on a writable volume. SIGTERM = graceful shutdown (current cycle finishes, then state writes).

A long-lived SSE connection to `/exchange/updates` (default on) lets the tight-loop pollers — the 1-second survivor watcher and the 5-second ALO fill watchers — wake on push events instead of strictly polling. REST stays the source of truth: every decision is still REST-confirmed, the stream just lets the bot react faster. See [`docs/SSE.md`](docs/SSE.md) for details and disable instructions.

### Companion tools

- **`probe/`** — a phased API validation tool you run *before* the bot. Confirms your token works, your account is funded, and the venues respond as expected. See [`probe/README.md`](probe/README.md).
- **`scripts/`** — operational helpers: close one arb, close all, recover snapshot from logs, show balances. Each is a small standalone Python script.
- **`skills/funding-arb-cycle/`** — a [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) skill that re-implements the bot's strategy as an interactive co-pilot over the VOOI MCP. Same algorithm; you confirm each open/close manually. Useful as a learning tool or as a manual fallback when the bot is offline.

---

## Configuration

All configuration is environment variables. The full list with defaults and inline comments is in [`.env.example`](.env.example). The keys you almost always need to touch:

| Variable | What it controls | Default |
|---|---|---|
| `VOOI_BEARER_TOKEN` | Auth to the VOOI API — get it at [ultra.vooi.io/api-tokens](https://ultra.vooi.io/api-tokens) | *(required)* |
| `BOT_TARGET_EXCHANGES` | Which venues to trade | `hyperliquid,lighter` |
| `BOT_LEG_COLLAT_USD` | Margin per leg | `10` |
| `BOT_LEVERAGE_TARGET` / `_CAP` | Target / hard-cap leverage | `10` / `5` |
| `BOT_MIN_NET_APR` | Don't open below this APR | `0.70` (= 70%) |
| `BOT_MIN_HOLD_HOURS` | Don't soft-close before this age | `12` |
| `BOT_MAX_HOLD_HOURS` | Force-close at this age | `96` |
| `BOT_STOP_LOSS_PCT` | Hard-stop threshold | `0.05` |
| `BOT_LOW_APR_THRESHOLD` / `_WINDOW` | Soft-close on APR decay | `0.10` / `6` |
| `BOT_ASSET_BLACKLIST` | Skip these symbols | *(empty)* |
| `BOT_DRY_RUN` | If `true`, no orders are placed | `true` |
| `BOT_SSE_ENABLED` | SSE event stream — latency optimisation over REST polling. REST stays canonical. See [`docs/SSE.md`](docs/SSE.md). | `true` |

The defaults are tuned to a small-capital environment ($100–500 deployed). At larger size, friction-per-arb drops and you can profitably widen the opportunity pool — see [`docs/STRATEGY.md` § Why these defaults](docs/STRATEGY.md#why-these-defaults).

---

## Safety model

The bot has several layers between you and a runaway loss:

1. **`BOT_DRY_RUN=true` by default.** No order goes out unless you explicitly set it to `false`.
2. **`hard_stop_loss`** — per-arb dollar threshold checked every monitor cycle (5 min by default). Hits even before `min_hold`.
3. **`max_hold`** — every arb is force-closed after `BOT_MAX_HOLD_HOURS` regardless of P&L.
4. **`BOT_PAIR_COOLDOWN_*`** — assets that hurt you twice in 48h are paused.
5. **Per-venue margin cap** — bot refuses to open if it would exceed `BOT_MAX_MARGIN_PER_EXCHANGE_USD`.
6. **Bracket SL/TP** — every open carries on-venue stop-loss and take-profit orders. They survive the bot going down.
7. **Single-instance lock** — PID file at `BOT_PID_FILE` prevents accidentally running two copies on the same token.
8. **Atomic state writes** — `.tmp` + rename, so a crash mid-write doesn't corrupt your snapshot.
9. **Graceful shutdown** — SIGTERM finishes the current cycle, persists state, and exits cleanly.
10. **SSE is strictly additive.** REST is canonical. If the event stream crashes, silences, or is rejected, callers fall through to their original poll-based behaviour with no state change. See [`docs/SSE.md` § Safety properties](docs/SSE.md#safety-properties).

None of these are a substitute for monitoring. Watch `state.ndjson` daily.

---

## What's in this repo

```
.
├── README.md                # this file
├── LICENSE                  # MIT + disclaimer
├── CONTRIBUTING.md          # how to send a PR
├── AGENTS.md                # contract for AI agents working in this repo
├── .env.example             # config template — copy to .env
├── pyproject.toml           # Python project manifest
├── uv.lock                  # locked dependencies (re-lock with `uv lock`)
├── Dockerfile               # production container
├── .dockerignore
├── .gitignore
│
├── fundbot/                 # the bot
│   ├── __main__.py          # `python -m fundbot` entry point
│   └── mvp.py               # the engine — one file, ~3700 lines
│
├── probe/                   # phase-0 API validator (run before first trade)
│   └── README.md
│
├── scripts/                 # operational helpers
│   ├── close_one.py         # close a specific arb_id
│   ├── close_all.py         # emergency unwind all positions
│   ├── close_orphan.py      # close one orphan leg by asset+exchange
│   ├── recover_snapshot.py  # rebuild state-snapshot.json from state.ndjson
│   ├── show_balances.py     # one-shot balance dump
│   ├── trade_analysis.py    # diagnostic stats from logs
│   ├── realized_pnl_from_log.py
│   ├── cycle_report.py
│   ├── monitor.py
│   └── plot_portfolio_from_log.py
│
├── tests/                   # pytest suite
│
├── skills/                  # Claude Code skills
│   └── funding-arb-cycle/   # MCP co-pilot: same strategy, human-in-the-loop
│       ├── SKILL.md
│       └── README.md
│
└── docs/
    ├── STRATEGY.md          # full algorithm and tuning rationale
    └── DEPLOY.md            # local / Docker / Fly.io deployment
```

---

## For AI agents

If you are an AI assistant (Claude, Cursor, Aider, etc.) about to make changes here, read [`AGENTS.md`](AGENTS.md). It encodes the safety rules that humans expect you to follow.

The companion **Claude Code skill** at [`skills/funding-arb-cycle/`](skills/funding-arb-cycle/) lets you (or an end user) drive the same strategy through the VOOI MCP server, with explicit per-action confirmation. It is a separate product — useful when:

- You don't want a long-running process.
- You want to run on a `/loop 60m` cadence with a human approving each cycle.
- You're learning the strategy and want to step through decisions interactively.

The skill and the bot share state file formats; you can switch between them.

---

## Roadmap (suggestions; PRs welcome)

- **Aster integration polish** — funding-fee field is null on Aster positions; need a fallback that estimates funding from `get_funding_spread_chart` × held_h.
- **Per-asset config** — currently filters are global. Some symbols deserve tighter / looser thresholds.
- **Historical spread for safe-floor** — replace the rolling `apr_history` with on-demand `get_funding_spread_chart` for a smarter floor calculation.
- **Web UI / dashboard** — none today. The bot is fine without one, but daily monitoring is friction.
- **Telegram / Discord notifications** — emit events on open / close / hard-stop. Not in core; should be a plug-in.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) before sending PRs.

---

## Links

- VOOI Perps API docs: https://perps-api.vooi.io/docs
- VOOI Perps OpenAPI JSON: https://perps-api.vooi.io/swagger/json
- VOOI Perps MCP server: https://perps-api.vooi.io/mcp
- Strategy reference: [`docs/STRATEGY.md`](docs/STRATEGY.md)
- Deployment: [`docs/DEPLOY.md`](docs/DEPLOY.md)
- Issues / discussion: see project Homepage in `pyproject.toml`

---

## License

MIT. See [`LICENSE`](LICENSE).

**This is real-money software. Read the disclaimer.**
