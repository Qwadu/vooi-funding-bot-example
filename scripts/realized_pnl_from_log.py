#!/usr/bin/env python3
"""
Суммарный «реализованный» PnL по закрытым арбам за окно времени, из bot.log.

Источник: для каждого CLOSE_OK берётся последний NET (Σ uPnL + Σ funding по паре ног)
из текстового блока ## Open positions для данного arb_id — тот же mark, что видит бот
перед почасовым циклом. Между этим снимком и фактическим исполнением close проходит
до ~1 часа + время ордеров; точный cash PnL по биржам в лог не пишется.

Usage:
  uv run python scripts/realized_pnl_from_log.py --log bot.log --since 2026-05-04T00:00:00Z --until 2026-05-06T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ARB_ID = re.compile(r"arb_id=([a-zA-Z0-9-]+)")
SIGMA = re.compile(
    r"Σ uPnL\s*=\s*\+?\$([-+]?[0-9.]+)\s+Σ funding\s*=\s*\+?\$([-+]?[0-9.]+)\s+NET\s*=\s*\+?\$([-+]?[0-9.]+)"
)
ASSET_HDR = re.compile(r"^\s+▸\s+(\S+(?::\S+)?)\s")


@dataclass
class LegSnap:
    asset: str
    upnl: float
    funding: float
    net: float


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=Path("bot.log"))
    ap.add_argument("--since", type=str, required=True, help="ISO8601 inclusive lower bound (UTC)")
    ap.add_argument("--until", type=str, required=True, help="ISO8601 exclusive upper bound (UTC)")
    args = ap.parse_args()

    t0 = _parse_ts(args.since)
    t1 = _parse_ts(args.until)
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=timezone.utc)

    in_open = False
    cur_asset: str | None = None
    sigma: tuple[float, float, float] | None = None
    last_snap: dict[str, LegSnap] = {}

    closes: list[tuple[datetime, str, str | None, LegSnap | None]] = []

    with args.log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "## Open positions" in line:
                in_open = True
                cur_asset = None
                sigma = None
                continue
            if in_open and (
                line.startswith("## ")
                and "## Open positions" not in line
                or "PORTFOLIO TOTAL" in line
            ):
                in_open = False
                continue

            if in_open:
                am = ASSET_HDR.match(line)
                if am:
                    cur_asset = am.group(1)
                    sigma = None
                    continue
                sm = SIGMA.search(line)
                if sm:
                    sigma = (float(sm.group(1)), float(sm.group(2)), float(sm.group(3)))
                    continue
                aid = ARB_ID.search(line)
                if aid and cur_asset and sigma is not None:
                    u, fn, nt = sigma
                    last_snap[aid.group(1)] = LegSnap(
                        asset=cur_asset, upnl=u, funding=fn, net=nt
                    )
                continue

            line_st = line.strip()
            if not line_st.startswith("{"):
                continue
            try:
                o = json.loads(line_st)
            except json.JSONDecodeError:
                continue
            if o.get("event") != "CLOSE_OK":
                continue
            arb = o.get("arb_id")
            if not arb:
                continue
            ts_s = o.get("ts")
            if not ts_s:
                continue
            ts = _parse_ts(ts_s)
            inst = o.get("instance")
            snap = last_snap.get(str(arb))
            closes.append((ts, str(arb), str(inst) if inst else None, snap))

    in_window = [c for c in closes if t0 <= c[0] < t1]
    total_net = sum(c[3].net for c in in_window if c[3] is not None)
    total_up = sum(c[3].upnl for c in in_window if c[3] is not None)
    total_fund = sum(c[3].funding for c in in_window if c[3] is not None)
    missing = [c for c in in_window if c[3] is None]

    print(f"Window: [{t0.isoformat()} .. {t1.isoformat()})  log={args.log.resolve()}")
    print(f"CLOSE_OK in window: {len(in_window)}")
    if missing:
        print(f"WARNING: no prior position snapshot for {len(missing)} close(s):")
        for ts, arb, inst, _ in missing:
            print(f"  {ts.isoformat()}  {arb}  instance={inst}")
    print()
    print(f"{'close_ts_utc':<28} {'asset':<18} {'arb_id':<36} {'uPnL':>10} {'funding':>10} {'NET':>10}")
    for ts, arb, _inst, snap in sorted(in_window, key=lambda x: x[0]):
        if snap is None:
            print(f"{ts.isoformat()}  {'?':<18} {arb:<36} {'n/a':>10} {'n/a':>10} {'n/a':>10}")
        else:
            print(
                f"{ts.isoformat()}  {snap.asset:<18} {arb:<36} "
                f"{snap.upnl:>+10.4f} {snap.funding:>+10.4f} {snap.net:>+10.4f}"
            )
    print()
    print(
        f"TOTAL (sum of last-reported leg NET before close): "
        f"uPnL {total_up:+.4f}  funding {total_fund:+.4f}  NET {total_net:+.4f}"
    )


if __name__ == "__main__":
    main()
