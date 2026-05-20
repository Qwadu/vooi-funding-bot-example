"""Phase 0 — API probe-script CLI entry-point.

Usage:
    python -m probe.probe --help

Каждый subcommand закрывает один пункт из docs/api-probe-results.md (Q1..Q11).
Безопасные дефолты: $5 max per write (`PROBE_MAX_NOTIONAL_USD`), $200 budget per run,
prices DALEKO от mid (50% offset), размер квантуется по `baseDecimals` биржи с
`ROUND_DOWN` (никогда не превышаем заявленный notional).

Перед запуском:
- скопировать `.env.example` → `.env`
- заполнить `VOOI_BEARER_TOKEN` отдельного sub-account
- иметь $20-100 на каждой бирже из `BOT_TARGET_EXCHANGES`
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

from probe.client import ProbeClient
from probe.log import ProbeLogger
from probe.questions import (
    q1_spread_chart_format,
    q2_sse_latency,
    q3_alo_bracket,
    q4_clientorderid_idempotent,
    q5_bearer_sse,
    q6_funding_interval,
    q7_cross_margin_drift,
    q8_5xx_behavior,
    q9_without_broker,
    q10_orderid_filter,
    q11_sse_silent,
)
from probe.safety import SafetyBudget


def _load_dotenv(path: Path) -> None:
    """Минимальная замена python-dotenv, чтобы не тащить зависимость в probe."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.stderr.write(f"ERROR: {name} is required (set in .env)\n")
        sys.exit(2)
    return val or ""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="probe", description="VOOI Phase 0 probe-script")
    p.add_argument(
        "--budget-usd",
        type=Decimal,
        default=Decimal("200"),
        help="max total notional across all writes in this run (default 200)",
    )
    # §4.5 fix: позволяем перекрыть exchange-пары для Q1 без правки кода.
    p.add_argument(
        "--long-exchange",
        type=str,
        default=None,
        help="Q1 long-side exchange (default: hyperliquid; can use aster/lighter/...)",
    )
    p.add_argument(
        "--short-exchange",
        type=str,
        default=None,
        help="Q1 short-side exchange (default: lighter)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("readonly", help="run all read-only probes (Q1, Q5, Q6, Q10, Q11)")
    sub.add_parser("all", help="run all 11 probes (READ + WRITE; requires real $)")

    sub.add_parser("q1", help="Q1: /spread-chart longAsset/shortAsset format")
    sub.add_parser("q2", help="Q2: SSE latency для orderId (writes!)")
    sub.add_parser("q3", help="Q3: alo + stopLoss conflict (writes!)")
    sub.add_parser("q4", help="Q4: clientOrderId idempotency (writes!)")
    sub.add_parser("q5", help="Q5: Bearer на SSE")
    sub.add_parser("q6", help="Q6: fundingInterval per market")
    sub.add_parser("q7", help="Q7: cross-margin liq drift (REAL POSITIONS!)")
    sub.add_parser("q8", help="Q8: 5xx behavior (writes! manual VPN kill для full test)")
    sub.add_parser("q9", help="Q9: POST orders без broker (writes!)")
    sub.add_parser("q10", help="Q10: /orders filter by clientOrderId")
    sub.add_parser("q11", help="Q11: SSE silent timeout (5 min watch)")

    return p


def _resolve_q1_exchanges(args: argparse.Namespace) -> tuple[str, str]:
    """§4.5 fix: (long, short) exchange — из CLI флагов, иначе из .env, иначе дефолт.

    Возвращаем tuple (а не dict), чтобы mypy --strict видел точные типы и не
    путал ключи `long_exchange/short_exchange` с другими kwarg `window_days: int`
    при `**unpack`.
    """
    long_ex = (
        args.long_exchange
        or _env("PROBE_Q1_LONG_EXCHANGE", "")
        or "hyperliquid"
    )
    short_ex = (
        args.short_exchange
        or _env("PROBE_Q1_SHORT_EXCHANGE", "")
        or "lighter"
    )
    return long_ex, short_ex


async def _amain(args: argparse.Namespace) -> int:
    _load_dotenv(Path(".env"))

    base_url = _env("VOOI_API_BASE_URL", "https://perps-api.vooi.io")
    token = _env("VOOI_BEARER_TOKEN", required=True)
    target_exchanges = [
        e.strip()
        for e in _env("BOT_TARGET_EXCHANGES", "hyperliquid,lighter").split(",")
        if e.strip()
    ]
    primary_asset = _env("PROBE_ASSET_PRIMARY", "BTC")
    secondary_asset = _env("PROBE_ASSET_SECONDARY", "ETH")
    max_per_call = Decimal(_env("PROBE_MAX_NOTIONAL_USD", "5"))
    notional_for_write = max_per_call

    # BUG-03 fix: уважать PROBE_RUNS_DIR из .env.
    runs_dir = Path(_env("PROBE_RUNS_DIR", "probe/runs"))

    budget = SafetyBudget(args.budget_usd)
    log = ProbeLogger(runs_dir=runs_dir)

    q1_long_ex, q1_short_ex = _resolve_q1_exchanges(args)

    log.log(
        "PROBE_RUN_START",
        run_id=log.run_id,
        base_url=base_url,
        target_exchanges=target_exchanges,
        primary_asset=primary_asset,
        secondary_asset=secondary_asset,
        max_per_call_usd=str(max_per_call),
        budget_usd=str(args.budget_usd),
        runs_dir=str(runs_dir),
        q1_long_exchange=q1_long_ex,
        q1_short_exchange=q1_short_ex,
    )

    async with ProbeClient(base_url, token) as client:
        cmd = args.cmd

        if cmd == "readonly":
            await q1_spread_chart_format(
                client, log,
                long_exchange=q1_long_ex,
                short_exchange=q1_short_ex,
            )
            await q5_bearer_sse(client, log, target_exchanges=target_exchanges)
            await q6_funding_interval(client, log, exchanges=target_exchanges)
            await q10_orderid_filter(client, log, target_exchanges=target_exchanges)
            await q11_sse_silent(client, log, target_exchanges=target_exchanges)
        elif cmd == "all":
            await q1_spread_chart_format(
                client, log,
                long_exchange=q1_long_ex,
                short_exchange=q1_short_ex,
            )
            await q5_bearer_sse(client, log, target_exchanges=target_exchanges)
            await q6_funding_interval(client, log, exchanges=target_exchanges)
            await q10_orderid_filter(client, log, target_exchanges=target_exchanges)
            await q2_sse_latency(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
            await q3_alo_bracket(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
            await q4_clientorderid_idempotent(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
            await q9_without_broker(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
            await q8_5xx_behavior(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
            for exchange in target_exchanges:
                await q7_cross_margin_drift(
                    client, log, budget,
                    exchange=exchange,
                    primary_asset=primary_asset,
                    secondary_asset=secondary_asset,
                    leverage=5,
                    notional_usd=notional_for_write,
                    max_per_call_usd=max_per_call,
                )
            await q11_sse_silent(client, log, target_exchanges=target_exchanges)
        elif cmd == "q1":
            await q1_spread_chart_format(
                client, log,
                long_exchange=q1_long_ex,
                short_exchange=q1_short_ex,
            )
        elif cmd == "q2":
            await q2_sse_latency(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
        elif cmd == "q3":
            await q3_alo_bracket(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
        elif cmd == "q4":
            await q4_clientorderid_idempotent(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
        elif cmd == "q5":
            await q5_bearer_sse(client, log, target_exchanges=target_exchanges)
        elif cmd == "q6":
            await q6_funding_interval(client, log, exchanges=target_exchanges)
        elif cmd == "q7":
            for exchange in target_exchanges:
                await q7_cross_margin_drift(
                    client, log, budget,
                    exchange=exchange,
                    primary_asset=primary_asset,
                    secondary_asset=secondary_asset,
                    leverage=5,
                    notional_usd=notional_for_write,
                    max_per_call_usd=max_per_call,
                )
        elif cmd == "q8":
            await q8_5xx_behavior(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
        elif cmd == "q9":
            await q9_without_broker(
                client, log, budget,
                exchanges=target_exchanges, asset=primary_asset,
                notional_usd=notional_for_write, max_per_call_usd=max_per_call,
            )
        elif cmd == "q10":
            await q10_orderid_filter(client, log, target_exchanges=target_exchanges)
        elif cmd == "q11":
            await q11_sse_silent(client, log, target_exchanges=target_exchanges)
        else:
            sys.stderr.write(f"unknown command: {cmd}\n")
            return 2

    log.log("PROBE_RUN_DONE", run_id=log.run_id, budget_spent_usd=str(budget.spent))
    sys.stdout.write(f"\nRun complete. Artifacts: {log.run_dir}/\n")
    sys.stdout.write("Now fill in docs/api-probe-results.md based on findings.\n")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
