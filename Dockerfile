# syntax=docker/dockerfile:1.7
# vooi-funding-arb-bot — production image
# Principles: non-root user, tini PID 1, PYTHONUNBUFFERED=1, uv for installs.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.4.30

# ---------------------------------------------------------------------------
# Build stage — install deps into /app/.venv
# ---------------------------------------------------------------------------
FROM base AS builder

WORKDIR /app

COPY pyproject.toml ./
COPY uv.lock* ./

RUN uv venv /app/.venv
RUN uv sync --frozen --no-dev || uv sync --no-dev

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM base AS runtime

# Non-root user
RUN groupadd --system --gid 1001 fundbot \
    && useradd --system --uid 1001 --gid fundbot --create-home --shell /usr/sbin/nologin fundbot

WORKDIR /app

COPY --from=builder --chown=fundbot:fundbot /app/.venv /app/.venv

COPY --chown=fundbot:fundbot fundbot/ /app/fundbot/
COPY --chown=fundbot:fundbot probe/ /app/probe/

# Volume mount-point (see fly.toml or your orchestrator)
RUN mkdir -p /data && chown fundbot:fundbot /data
VOLUME /data

ENV PATH="/app/.venv/bin:${PATH}"
ENV BOT_STATE_FILE=/data/state.ndjson
ENV BOT_INSTANCE_UUID_FILE=/data/instance.uuid
ENV BOT_COOLDOWN_FILE=/data/state-cooldown.json
ENV BOT_SNAPSHOT_FILE=/data/state-snapshot.json
ENV BOT_PID_FILE=/data/vooi-funding-arb-bot.pid

USER fundbot

EXPOSE 8080

ENTRYPOINT ["tini", "--"]
CMD ["python", "-u", "-m", "fundbot"]
