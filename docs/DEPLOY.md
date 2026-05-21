# Deploying `vooi-funding-arb-bot`

This guide covers two paths: a local long-running process, and a containerized deploy. Pick whichever fits.

## Prerequisites

- Python 3.11 or 3.12 (3.13 not yet supported by all deps)
- `uv` package manager (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A VOOI Perps account with at least one venue connected and balance on it
- Your VOOI bearer token

Broker / integrator attribution is handled server-side by the VOOI API — you do not need to obtain or configure any builder/integrator IDs.

## Path A — local long-running process

```bash
git clone https://github.com/your-org/vooi-funding-arb-bot
cd vooi-funding-arb-bot
cp .env.example .env
$EDITOR .env                    # fill in VOOI_BEARER_TOKEN
uv sync --extra dev

# First-time safety check
BOT_DRY_RUN=true uv run python -m fundbot

# After you've watched a few cycles and trust the config:
BOT_DRY_RUN=false uv run python -m fundbot
```

To daemonize on macOS or Linux without systemd:

```bash
( nohup uv run python -m fundbot >> bot.log 2>&1 < /dev/null & echo $! > /tmp/vooi-funding-arb-bot.pid )
disown
```

To stop:

```bash
kill -TERM $(cat /tmp/vooi-funding-arb-bot.pid)
```

`SIGTERM` triggers a graceful shutdown: the current cycle finishes, state is persisted, then the process exits.

## Path B — Docker

```bash
docker build -t vooi-funding-arb-bot:latest .

docker run -d --name vooi-arb \
  --env-file .env \
  -v vooi-arb-data:/data \
  --restart unless-stopped \
  vooi-funding-arb-bot:latest

# Logs
docker logs -f vooi-arb

# Stop gracefully
docker stop vooi-arb         # default stop is SIGTERM + 10s grace
```

State files live under `/data` (a named volume in the example above). Mount a host directory if you want to inspect them externally.

## Path C — Fly.io

A `fly.toml` is not included (your app name and org are yours). Minimal setup:

```bash
fly apps create your-bot-name --org your-org
fly volumes create your_bot_data --app your-bot-name --region nrt --size 1
fly secrets set --app your-bot-name VOOI_BEARER_TOKEN=...
fly deploy --remote-only --app your-bot-name
```

A minimal `fly.toml`:

```toml
app = "your-bot-name"
primary_region = "nrt"

[build]
  dockerfile = "Dockerfile"

[env]
  BOT_DRY_RUN = "true"        # set to "false" via secrets when ready

[mounts]
  source = "your_bot_data"
  destination = "/data"

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory = "512mb"

[deploy]
  strategy = "immediate"      # single-instance, no rolling
```

```bash
fly logs --app your-bot-name        # tail
fly status --app your-bot-name
fly ssh console --app your-bot-name  # inspect /data
```

To roll back:

```bash
fly releases --app your-bot-name
fly deploy --image <previous-image-id> --app your-bot-name
```

## Health checks and monitoring

The bot's "is-it-alive" signal is `state.ndjson` mtime — it grows every monitor cycle. A 10-minute gap means the process is wedged. Suggested external check:

```bash
[[ $(($(date +%s) - $(stat -c %Y /data/state.ndjson))) -lt 600 ]]
```

For richer monitoring, parse `state.ndjson` for the most recent `CYCLE_DONE` and alert if cycle errors exceed a threshold.

## Upgrading

1. `git pull` (or pull a new Docker image).
2. Diff `.env.example` against your `.env`. New keys with sensible defaults are safe to omit; new mandatory keys will be flagged at startup.
3. Stop the bot (SIGTERM).
4. Start the new version.
5. Watch `state.ndjson` for one or two cycles before walking away.

## What to do if you suspect a problem

- **All cycles emit `RECONCILE_SKIP_FETCH_FAILED`**: token may have expired. Rotate `VOOI_BEARER_TOKEN` and restart.
- **Half-leg position visible on a venue**: don't panic. The bot's reconcile will detect it within one cycle and close the orphan. If you want to act faster, use `scripts/close_orphan.py`.
- **Bot opened something that looks wrong**: stop the process first, inspect `state.ndjson` for the `OPEN_OK` event, then close manually via `scripts/close_one.py <arb_id>` or via the VOOI UI.

Stop the process before editing `state-snapshot.json` by hand. The bot writes the snapshot atomically on shutdown; a concurrent edit will be overwritten.
