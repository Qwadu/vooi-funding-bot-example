"""Read bot.log, find latest # CYCLE block, emit a compact monitor-style report.

Run: python3 scripts/cycle_report.py
"""

from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "bot.log"


def _num(s: str | None) -> Decimal | None:
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("$", "").replace("+", "")
    if s in ("", "n/a", "None"):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _fmt_signed(d: Decimal | None, places: int = 4) -> str:
    if d is None:
        return "n/a"
    sign = "+" if d >= 0 else "-"
    return f"{sign}${abs(d):.{places}f}"


def main() -> int:
    text = LOG.read_text(encoding="utf-8", errors="replace")

    headers = list(re.finditer(
        r"^# CYCLE #(\d+)\s+\[(\w+)\s*\]\s+started=(\S+).*?elapsed=([\d.]+)s.*$",
        text,
        re.MULTILINE,
    ))
    if not headers:
        print("No cycle header found.", file=sys.stderr)
        return 1
    last = headers[-1]
    cycle_no, kind, started, elapsed = last.group(1), last.group(2), last.group(3), last.group(4)
    block = text[last.start():]

    # Accounts: ``hyperliquid:perps``, ``hyperliquid:xyz``, ``lighter``, … (see mvp.render_report).
    acct: dict[str, tuple[Decimal | None, Decimal | None, Decimal | None]] = {}
    for m in re.finditer(
        r"^\s+(\S+)\s+total=\s*\$([\-\d.,]+)\s+available=\s*\$([\-\d.,]+)\s+in_use=\s*\$([\-\d.,]+)",
        block,
        re.MULTILINE,
    ):
        key = m.group(1)
        if re.match(r"^[a-z]", key):
            acct[key] = (_num(m.group(2)), _num(m.group(3)), _num(m.group(4)))

    pos_starts = list(re.finditer(
        r"▸\s+(\S+)\s+LONG:\s+(\S+)\s+\((\S+)\)\s+SHORT:\s+(\S+)\s+\((\S+)\)\s+collat=\s*\$([\-\d.,]+)\s+notional=\s*\$([\-\d.,]+)\s+lev=(\d+)x\s+open_apr=\s*([\-\d.,]+)%\s+now_apr=\s*([\-\d.,]+)%\s+held=([\d.]+)h",
        block,
    ))

    rows = []
    for i, m in enumerate(pos_starts):
        asset = m.group(1)
        long_ex = m.group(2)
        short_ex = m.group(4)
        notional = _num(m.group(7))
        lev = m.group(8)
        now_apr = _num(m.group(10))
        held = _num(m.group(11))
        end = pos_starts[i + 1].start() if i + 1 < len(pos_starts) else len(block)
        sub = block[m.end():end]
        sm = re.search(
            r"Σ uPnL\s*=\s*([+\-]?\$[+\-\d.,]+)\s+Σ funding\s*=\s*([+\-]?\$[+\-\d.,]+)\s+NET\s*=\s*([+\-]?\$[+\-\d.,]+)",
            sub,
        )
        if sm:
            upnl = _num(sm.group(1))
            funding = _num(sm.group(2))
            net = _num(sm.group(3))
        else:
            upnl = funding = net = None
        rows.append({
            "asset": asset,
            "dir": f"{long_ex[:3]}/{short_ex[:3]}",
            "now_apr": now_apr,
            "held": held,
            "notional": notional,
            "lev": lev,
            "upnl": upnl,
            "funding": funding,
            "net": net,
        })

    tm = re.search(
        r"PORTFOLIO TOTAL\s*\((\d+) strategies,\s+capital=\$([\d.,]+)\)\s+┃\s+Σ uPnL\s*=\s*([+\-]?\$[+\-\d.,]+)\s+Σ funding\s*=\s*([+\-]?\$[+\-\d.,]+)\s+NET\s*=\s*([+\-]?\$[+\-\d.,]+)\s*\(\s*([\-\d.]+)%\s+ROI\)",
        block,
    )
    if tm:
        n_strats, capital, p_upnl, p_funding, p_net, p_roi = tm.groups()
        p_upnl_d = _num(p_upnl)
        p_funding_d = _num(p_funding)
        p_net_d = _num(p_net)
    else:
        n_strats = capital = p_roi = "?"
        p_upnl_d = p_funding_d = p_net_d = None

    am = re.search(r"## Actions this cycle(.*?)(?:={3,}|\Z)", block, re.DOTALL)
    actions = []
    if am:
        for line in am.group(1).strip().splitlines():
            line = line.strip().lstrip("-").strip()
            if line and "MONITOR_ONLY" not in line:
                actions.append(line)

    print(f"## Cycle #{cycle_no} [{kind}] — {started}  elapsed={elapsed}s")
    print()
    print("| Asset | dir | now_apr | held | uPnL | funding | NET |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for r in rows:
        apr_str = f"{r['now_apr']:.2f}%" if r["now_apr"] is not None else "n/a"
        held_str = f"{r['held']:.2f}h" if r["held"] is not None else "n/a"
        print(
            f"| {r['asset']} | {r['dir']} | {apr_str} | {held_str} | "
            f"{_fmt_signed(r['upnl'])} | {_fmt_signed(r['funding'])} | {_fmt_signed(r['net'])} |",
        )
    roi_str = f"{p_roi}%" if p_roi != "?" else "?"
    print(
        f"| **TOTAL** | {n_strats} strats / cap=${capital} | | | "
        f"**{_fmt_signed(p_upnl_d)}** | **{_fmt_signed(p_funding_d)}** | "
        f"**{_fmt_signed(p_net_d)}** ({roi_str} ROI) |",
    )

    print()
    if acct:
        bal_parts = []
        total = Decimal("0")
        for ex, (tot, _av, _iu) in sorted(acct.items()):
            if tot is None:
                continue
            bal_parts.append(f"{ex}=${tot:.2f}")
            total += tot
        print(f"Balances: {'  '.join(bal_parts)}  Σ=${total:.2f}")

    if actions:
        print()
        print("Actions:")
        for a in actions:
            print(f"  - {a}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
