"""Frazzbot Dashboard — комплексный монитор бота.

Показывает по одной команде:
  1. Текущий портфель (uPnL / Funding / NET / ROI)
  2. Каждую открытую стратегию с комиссиями
  3. Последние 24 цикла: что открылось / закрылось / отклонилось и почему
  4. Статистику rejection'ов с причинами
  5. Закрытые позиции с реализованным P&L

Запуск:
  uv run python scripts/monitor.py
  uv run python scripts/monitor.py --hours 48
  uv run python scripts/monitor.py --watch      # обновляется каждые 60s
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "bot.log"
SNAP = REPO / "state-snapshot.json"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _d(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _fmt(d: Decimal | None, places: int = 4, prefix: str = "$") -> str:
    if d is None:
        return "  n/a "
    sign = "+" if d >= 0 else "-"
    return f"{sign}{prefix}{abs(d):.{places}f}"


def _pct(d: Decimal | None) -> str:
    if d is None:
        return "  n/a "
    return f"{d * 100:+.2f}%"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _held(opened_at: str) -> float:
    return (datetime.now(UTC) - _ts(opened_at)).total_seconds() / 3600


# ─────────────────────────────────────────────────────────────────────────────
# Log parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_log(log_path: Path, since: datetime) -> dict:
    """Парсит NDJSON лог и возвращает словарь с событиями за период."""
    events: list[dict] = []
    if not log_path.exists():
        return {"events": events}

    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
                ts_s = o.get("ts", "")
                if not ts_s:
                    continue
                ts = _ts(ts_s)
                if ts >= since:
                    events.append(o)
            except (json.JSONDecodeError, ValueError):
                continue

    return {"events": events}


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def build_summary(events: list[dict]) -> dict:
    opens: list[dict] = []
    closes: list[dict] = []
    intents: list[dict] = []
    rollbacks: list[dict] = []
    aborts: defaultdict[str, list[dict]] = defaultdict(list)
    cycle_done: list[dict] = []
    leverage_mismatches: list[dict] = []
    funding_breakevenss: list[dict] = []
    markets_warns: list[dict] = []

    open_by_arb: dict[str, dict] = {}   # arb_id -> OPEN_OK event
    intent_by_arb: dict[str, dict] = {} # arb_id -> OPEN_INTENT event

    for o in events:
        ev = o.get("event", "")
        arb = o.get("arb_id", "")

        if ev == "OPEN_INTENT":
            intents.append(o)
            if arb:
                intent_by_arb[arb] = o
        elif ev == "OPEN_OK":
            opens.append(o)
            if arb:
                open_by_arb[arb] = o
        elif ev == "CLOSE_OK":
            closes.append(o)
        elif ev == "OPEN_PARTIAL_FAIL_ROLLBACK":
            rollbacks.append(o)
        elif ev == "CYCLE_DONE":
            cycle_done.append(o)
        elif ev == "LEVERAGE_MISMATCH":
            leverage_mismatches.append(o)
        elif ev == "FUNDING_BREAKEVEN":
            funding_breakevenss.append(o)
        elif ev == "MARKETS_CACHE_EMPTY_WARN":
            markets_warns.append(o)
        elif ev.startswith("OPEN_ABORT"):
            aborts[ev].append(o)

    # Считаем intent без OPEN_OK = failed intents
    failed_intents = [
        o for o in intents
        if o.get("arb_id") and o["arb_id"] not in open_by_arb
    ]

    return {
        "opens": opens,
        "closes": closes,
        "intents": intents,
        "failed_intents": failed_intents,
        "rollbacks": rollbacks,
        "aborts": aborts,
        "cycle_done": cycle_done,
        "leverage_mismatches": leverage_mismatches,
        "funding_breakevens": funding_breakevenss,
        "markets_warns": markets_warns,
    }


def parse_snapshot(snap_path: Path) -> list[dict]:
    if not snap_path.exists():
        return []
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        return list(data.values())
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

W = 94  # report width


def hr(char: str = "─") -> str:
    return char * W


def render(hours: int = 24, log_path: Path = LOG, snap_path: Path = SNAP) -> str:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)

    parsed = parse_log(log_path, since)
    events = parsed["events"]
    summary = build_summary(events)
    positions = parse_snapshot(snap_path)

    lines: list[str] = []
    lines.append("")
    lines.append("╔" + "═" * (W - 2) + "╗")
    lines.append(f"║  FRAZZBOT MONITOR  ·  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC  ·  window={hours}h{' ' * 30}║")
    lines.append("╚" + "═" * (W - 2) + "╝")

    # ── 1. Текущий портфель из cycle_report ──────────────────────────────────
    cr = _get_cycle_report_data(log_path)
    lines.append("")
    lines.append("┌─ ПОРТФЕЛЬ (последний цикл) " + "─" * (W - 29) + "┐")

    if cr:
        lines.append(
            f"│  Цикл #{cr['cycle_no']:>3}  [{cr['kind']:>7}]  "
            f"started={cr['started']}  elapsed={cr['elapsed']}s"
            + " " * max(0, W - 65 - len(cr['cycle_no']) - len(cr['elapsed'])) + "│"
        )
        lines.append(f"│  Стратегий открыто: {len(cr['rows'])}  Капитал: ${cr.get('capital', '?')}" + " " * 30 + "│")
        lines.append(f"│{'─' * (W - 2)}│")

        col = "│  {:<16} {:>5} {:>6} {:>7} {:>11} {:>11} {:>11} {:>7} │"
        lines.append(col.format("ASSET", "dir", "now_apr", "held", "uPnL", "Funding", "NET", "open_apr"))
        lines.append(f"│  {'─' * 16} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 11} {'─' * 11} {'─' * 11} {'─' * 7} │")

        p_upnl = Decimal(0)
        p_fund = Decimal(0)
        p_net  = Decimal(0)

        for r in cr["rows"]:
            apr_s = f"{r['now_apr']:.1f}%" if r["now_apr"] is not None else "  n/a"
            held_s = f"{r['held']:.1f}h" if r["held"] is not None else " n/a"
            open_apr_s = f"{r['open_apr']:.1f}%" if r["open_apr"] is not None else "  n/a"

            upnl_s = _fmt(r["upnl"], 4)
            fund_s = _fmt(r["funding"], 4)
            net_s  = _fmt(r["net"], 4)

            # флаг тревоги
            flag = ""
            if r["now_apr"] is not None and r["now_apr"] < 0:
                flag = "⚠"
            if r["net"] is not None and r["net"] < Decimal("-0.05"):
                flag = "🔴"

            lines.append(col.format(
                r["asset"][:16] + flag,
                r["dir"],
                apr_s, held_s,
                upnl_s, fund_s, net_s,
                open_apr_s,
            ))

            if r["upnl"] is not None:   p_upnl += r["upnl"]
            if r["funding"] is not None: p_fund += r["funding"]
            if r["net"] is not None:     p_net  += r["net"]

        lines.append(f"│  {'─' * 16} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 11} {'─' * 11} {'─' * 11} {'─' * 7} │")
        roi_pct = (p_net / Decimal(cr.get("capital", "1")) * 100) if cr.get("capital") else Decimal(0)
        lines.append(
            f"│  {'ИТОГО':<16} {'':>5} {'':>6} {'':>7} "
            f"{_fmt(p_upnl, 4):>11} {_fmt(p_fund, 4):>11} {_fmt(p_net, 4):>11} "
            f"{roi_pct:>+6.2f}% │"
        )
        if cr.get("balances"):
            lines.append(f"│  Балансы: {cr['balances']:<{W-13}}│")
    else:
        lines.append(f"│  (нет данных cycle report){' ' * (W - 28)}│")

    lines.append("└" + "─" * (W - 2) + "┘")

    # ── 2. Последние закрытия ────────────────────────────────────────────────
    closes = summary["closes"]
    lines.append("")
    lines.append(f"┌─ ЗАКРЫТЫЕ ПОЗИЦИИ за {hours}h ({len(closes)} шт.) " + "─" * max(0, W - 28 - len(str(hours)) - len(str(len(closes)))) + "┐")

    if closes:
        cl_col = "│  {:<22} {:<18} {:>10} {:>10} {:>10} {:<20} │"
        lines.append(cl_col.format("ts (UTC)", "asset/arb_id", "realized", "mark_net", "held_h", "reason"))
        lines.append(f"│  {'─' * 22} {'─' * 18} {'─' * 10} {'─' * 10} {'─' * 10} {'─' * 20} │")
        for c in sorted(closes, key=lambda x: x.get("ts", ""))[-15:]:
            ts_s = c.get("ts", "")[:19].replace("T", " ")
            asset = c.get("asset", c.get("arb_id", "?"))[:18]
            realized = _fmt(_d(c.get("realized_total_usd")), 4)
            mark_net = _fmt(_d(c.get("mark_total")), 4)
            held_h = c.get("held_hours", "?")
            if held_h != "?":
                try: held_h = f"{float(held_h):.1f}h"
                except: pass
            reason = str(c.get("reason", c.get("close_source", "?")))[:20]
            lines.append(cl_col.format(ts_s, asset, realized, mark_net, str(held_h), reason))
    else:
        lines.append(f"│  (нет закрытий за период){' ' * (W - 27)}│")

    lines.append("└" + "─" * (W - 2) + "┘")

    # ── 3. Открытия + Ошибки открытия ────────────────────────────────────────
    opens   = summary["opens"]
    intents = summary["intents"]
    failed  = summary["failed_intents"]
    rollbks = summary["rollbacks"]

    lines.append("")
    lines.append(
        f"┌─ ОТКРЫТИЯ за {hours}h: "
        f"intents={len(intents)}  opened={len(opens)}  failed={len(failed)}  rollbacks={len(rollbks)} "
        + "─" * max(0, W - 60 - len(str(hours))) + "┐"
    )

    # Строим индекс: arb_id -> OPEN_INTENT (для net_apr/collat/lev на каждое открытие)
    intent_by_arb = {o["arb_id"]: o for o in intents if "arb_id" in o}

    if opens:
        op_col = "│  {:<20} {:<14} {:>10} {:>8} {:>8} {:>6} {:<12} │"
        lines.append(op_col.format("ts (UTC)", "asset", "net_apr", "collat", "notl", "lev", "long/short"))
        lines.append(f"│  {'─' * 20} {'─' * 14} {'─' * 10} {'─' * 8} {'─' * 8} {'─' * 6} {'─' * 12} │")
        for o in sorted(opens, key=lambda x: x.get("ts", ""))[-12:]:
            ts_s = o.get("ts", "")[:19].replace("T", " ")
            asset = o.get("asset", "?")[:14]
            intent = intent_by_arb.get(o.get("arb_id", ""), {})
            apr_s = f"{float(intent.get('net_apr', 0)):.1%}" if intent.get("net_apr") else "?"
            collat_s = f"${intent.get('collat_usd', '?')}"
            notl_s = f"${intent.get('notional_usd', '?')}"
            lev_s = f"{intent.get('effective_leverage', '?')}x"
            dir_s = f"{intent.get('long_exchange','?')[:4]}/{intent.get('short_exchange','?')[:4]}"
            lines.append(op_col.format(ts_s, asset, apr_s, collat_s, notl_s, lev_s, dir_s))
    else:
        lines.append(f"│  (нет открытий){' ' * (W - 17)}│")

    # Провальные OPEN_INTENT (не получили OPEN_OK)
    if failed:
        lines.append(f"│{'─' * (W - 2)}│")
        lines.append(f"│  FAILED intents (не дошли до OPEN_OK):{' ' * (W - 40)}│")
        fail_col = "│    {:<12} {:>10} {:<25} {:<25} │"
        lines.append(fail_col.format("asset", "net_apr", "long_ex", "short_ex"))
        for o in failed[-8:]:
            asset = o.get("asset", "?")[:12]
            apr_s = f"{float(o.get('net_apr', 0)):.1%}"
            long_ex = o.get("long_exchange", "?")[:25]
            short_ex = o.get("short_exchange", "?")[:25]
            lines.append(fail_col.format(asset, apr_s, long_ex, short_ex))

    lines.append("└" + "─" * (W - 2) + "┘")

    # ── 4. Почему отклонялись (OPEN_ABORT_*) ─────────────────────────────────
    aborts = summary["aborts"]
    total_aborts = sum(len(v) for v in aborts.values())
    lines.append("")
    lines.append(f"┌─ ПРИЧИНЫ ОТКЛОНЕНИЙ (OPEN_ABORT_*) за {hours}h — {total_aborts} шт. " + "─" * max(0, W - 50) + "┐")

    if aborts:
        for ev_name, evs in sorted(aborts.items(), key=lambda x: -len(x[1])):
            reason_short = ev_name.replace("OPEN_ABORT_", "")
            assets = [e.get("asset", "?") for e in evs[:8]]
            asset_list = ", ".join(assets) + ("…" if len(evs) > 8 else "")
            lines.append(f"│  {reason_short:<28} {len(evs):>4}×   {asset_list:<{W - 40}}│")
    else:
        lines.append(f"│  (нет OPEN_ABORT событий за период){' ' * (W - 37)}│")

    lines.append("└" + "─" * (W - 2) + "┘")

    # ── 5. Последние N циклов (история по часам) ─────────────────────────────
    cycle_events = summary["cycle_done"]
    lines.append("")
    lines.append(f"┌─ ИСТОРИЯ ЦИКЛОВ за {hours}h ({len(cycle_events)} циклов) " + "─" * max(0, W - 35 - len(str(hours)) - len(str(len(cycle_events)))) + "┐")

    if cycle_events:
        ch_col = "│  {:<6} {:<10} {:>9} {:>7} {:>8} {:>9} {:>6} {:>6} {:>8} │"
        lines.append(ch_col.format("cycle", "ts", "elapsed", "trading", "open_pos", "actions", "opens", "closes", "errors"))
        lines.append(f"│  {'─' * 6} {'─' * 10} {'─' * 9} {'─' * 7} {'─' * 8} {'─' * 9} {'─' * 6} {'─' * 6} {'─' * 8} │")
        for c in sorted(cycle_events, key=lambda x: x.get("ts", ""))[-24:]:
            ts_s = c.get("ts", "")[:16].replace("T", " ")
            cycle_n = str(c.get("cycle", "?"))
            elapsed = f"{c.get('elapsed_sec', 0):.1f}s"
            trading = "✓" if c.get("is_trading_cycle") else "─"
            open_pos = str(c.get("open_positions", "?"))
            actions = c.get("actions", [])
            n_opens  = sum(1 for a in actions if a.startswith("OPEN "))
            n_closes = sum(1 for a in actions if a.startswith("CLOSE "))
            n_actions = len([a for a in actions if not a.startswith("SKIP") and not a.startswith("MONITOR")])
            n_errors = len(c.get("errors", []))
            err_s = str(n_errors) if n_errors == 0 else f"⚠{n_errors}"
            lines.append(ch_col.format(
                cycle_n, ts_s, elapsed, trading, open_pos,
                str(n_actions), str(n_opens), str(n_closes), err_s,
            ))
    else:
        lines.append(f"│  (нет CYCLE_DONE событий за период — лог слишком старый?){' ' * (W - 58)}│")

    lines.append("└" + "─" * (W - 2) + "┘")

    # ── 6. Предупреждения ────────────────────────────────────────────────────
    warns = []
    if summary["leverage_mismatches"]:
        warns.append(f"LEVERAGE_MISMATCH: {len(summary['leverage_mismatches'])} раз(а)")
    if summary["markets_warns"]:
        warns.append(f"MARKETS_CACHE_EMPTY: {len(summary['markets_warns'])} раз(а)")
    if summary["funding_breakevens"]:
        warns.append(f"FUNDING_BREAKEVEN достигнут: {len(summary['funding_breakevens'])} позиций")
    if len(rollbks := summary["rollbacks"]) > 0:
        warns.append(f"ROLLBACKS (half-legged open): {len(rollbks)}")

    if warns:
        lines.append("")
        lines.append(f"┌─ ПРЕДУПРЕЖДЕНИЯ " + "─" * (W - 18) + "┐")
        for w in warns:
            lines.append(f"│  ⚠  {w:<{W - 7}}│")
        lines.append("└" + "─" * (W - 2) + "┘")

    # ── 7. Snapshot: открытые позиции (для справки) ──────────────────────────
    if positions:
        lines.append("")
        lines.append(f"┌─ ОТКРЫТЫЕ ПОЗИЦИИ (snapshot, {len(positions)} шт.) " + "─" * max(0, W - 32 - len(str(len(positions)))) + "┐")
        sn_col = "│  {:<16} {:<10} {:<10} {:>8} {:>7} {:>7} {:>9} {:>8} │"
        lines.append(sn_col.format("asset", "long_ex", "short_ex", "open_apr", "collat", "notl", "held", "lev"))
        lines.append(f"│  {'─' * 16} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 7} {'─' * 7} {'─' * 9} {'─' * 8} │")
        for p in sorted(positions, key=lambda x: x.get("opened_at", "")):
            held = _held(p.get("opened_at", "2000-01-01"))
            apr_s = f"{float(p.get('open_net_apr', 0)):.1%}"
            lines.append(sn_col.format(
                p.get("asset", "?")[:16],
                p.get("long_exchange", "?")[:10],
                p.get("short_exchange", "?")[:10],
                apr_s,
                f"${p.get('leg_collat_usd', '?')}",
                f"${p.get('leg_notional_usd', '?')}",
                f"{held:.1f}h",
                f"{p.get('effective_leverage', '?')}x",
            ))
        lines.append("└" + "─" * (W - 2) + "┘")

    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Cycle report parser (из bot.log текстового формата)
# ─────────────────────────────────────────────────────────────────────────────

def _get_cycle_report_data(log_path: Path) -> dict | None:
    """Парсит последний ## CYCLE блок из bot.log."""
    import re

    if not log_path.exists():
        return None

    text = log_path.read_text(encoding="utf-8", errors="replace")
    headers = list(re.finditer(
        r"^# CYCLE #(\d+)\s+\[(\w+)\s*\]\s+started=(\S+).*?elapsed=([\d.]+)s",
        text, re.MULTILINE,
    ))
    if not headers:
        return None

    last = headers[-1]
    block = text[last.start():]

    cycle_no = last.group(1)
    kind = last.group(2)
    started = last.group(3)
    elapsed = last.group(4)

    # Балансы
    bal_parts = []
    total_bal = Decimal(0)
    for m in re.finditer(
        r"^\s+(\S+)\s+total=\s*\$([\d.,]+)\s+available=\s*\$([\d.,]+)\s+in_use=\s*\$([\d.,]+)",
        block, re.MULTILINE,
    ):
        key = m.group(1)
        if re.match(r"^[a-z]", key):
            v = Decimal(m.group(2).replace(",", ""))
            bal_parts.append(f"{key}=${v:.2f}")
            total_bal += v
    balances = "  ".join(bal_parts) + f"  Σ=${total_bal:.2f}" if bal_parts else ""

    def _parse_money(s: str) -> Decimal | None:
        s = s.strip().replace("$", "").replace("+", "").replace(",", "")
        try:
            return Decimal(s)
        except Exception:
            return None

    # Позиции — паттерн идентичен cycle_report.py
    pos_starts = list(re.finditer(
        r"▸\s+(\S+)\s+LONG:\s+(\S+)\s+\((\S+)\)\s+SHORT:\s+(\S+)\s+\((\S+)\)\s+"
        r"collat=\s*\$([\-\d.,]+)\s+notional=\s*\$([\-\d.,]+)\s+lev=(\d+)x\s+"
        r"open_apr=\s*([\-\d.,]+)%\s+now_apr=\s*([\-\d.,]+)%\s+held=([\d.]+)h",
        block,
    ))

    rows = []
    for i, m in enumerate(pos_starts):
        end = pos_starts[i + 1].start() if i + 1 < len(pos_starts) else len(block)
        sub = block[m.end():end]

        sm = re.search(
            r"Σ uPnL\s*=\s*([+\-]?\$[+\-\d.,]+)\s+Σ funding\s*=\s*([+\-]?\$[+\-\d.,]+)\s+NET\s*=\s*([+\-]?\$[+\-\d.,]+)",
            sub,
        )
        upnl    = _parse_money(sm.group(1)) if sm else None
        funding = _parse_money(sm.group(2)) if sm else None
        net     = _parse_money(sm.group(3)) if sm else None

        long_ex  = m.group(2)[:4]
        short_ex = m.group(4)[:4]

        rows.append({
            "asset":    m.group(1),
            "dir":      f"{long_ex}/{short_ex}",
            "collat":   Decimal(m.group(6).replace(",", "")),
            "notional": Decimal(m.group(7).replace(",", "")),
            "lev":      m.group(8),
            "open_apr": Decimal(m.group(9).replace(",", "")) / 100,
            "now_apr":  Decimal(m.group(10).replace(",", "")) / 100,
            "held":     Decimal(m.group(11)),
            "upnl":     upnl,
            "funding":  funding,
            "net":      net,
        })

    # Портфель total
    pm = re.search(
        r"PORTFOLIO TOTAL\s*\((\d+) strategies,\s+capital=\$([\d.,]+)\)",
        block,
    )
    capital = pm.group(2).replace(",", "") if pm else None

    return {
        "cycle_no": cycle_no,
        "kind": kind,
        "started": started,
        "elapsed": elapsed,
        "rows": rows,
        "capital": capital,
        "balances": balances,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Frazzbot dashboard monitor")
    parser.add_argument("--hours", type=int, default=24, help="Окно анализа в часах (default 24)")
    parser.add_argument("--log", type=Path, default=LOG, help="Путь к bot.log")
    parser.add_argument("--snap", type=Path, default=SNAP, help="Путь к state-snapshot.json")
    parser.add_argument("--watch", action="store_true", help="Обновлять каждые 60 секунд")
    parser.add_argument("--interval", type=int, default=60, help="Интервал обновления в watch-режиме (сек)")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                # Очистка терминала
                os.system("cls" if os.name == "nt" else "clear")
                sys.stdout.write(render(args.hours, args.log, args.snap))
                sys.stdout.write(f"\n  [watch mode — обновление каждые {args.interval}s, Ctrl+C для выхода]\n")
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nWatch mode stopped.")
            return 0
    else:
        sys.stdout.write(render(args.hours, args.log, args.snap))
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
