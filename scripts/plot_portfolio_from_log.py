#!/usr/bin/env python3
"""
Parse fundbot human-readable cycle reports from bot.log and build a portfolio chart.

Metrics per cycle (UTC time from # CYCLE started=… combined with date from the last JSON ts):
  - margin_total: sum of margin bucket totals in ## Accounts
  - book_equity: capital + NET from PORTFOLIO TOTAL (strategy book mark)

Default: aggregate last sample per UTC hour, write standalone HTML (Chart.js CDN).

  uv run python scripts/plot_portfolio_from_log.py --log bot.log -o artifacts/portfolio_hourly.html
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


CYCLE_RE = re.compile(
    r"# CYCLE #\d+ \[(?P<mode>TRADING|MONITOR)\s*\]\s+started=(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})Z"
)
ACCOUNTS_HDR = "## Accounts"
OPEN_HDR = "## Open positions"
PORTFOLIO_HDR = re.compile(r"PORTFOLIO TOTAL\s+\(\d+ strategies,\s+capital=\$([0-9.]+)\)")
TOTAL_IN_LINE = re.compile(r"total=\s*\$([0-9.]+)")
NET_IN_LINE = re.compile(r"NET\s*=\s*\+?\$([-+]?[0-9.]+)")


@dataclass
class Sample:
    ts: datetime
    mode: str
    margin_total: float
    capital: float
    net: float

    @property
    def book_equity(self) -> float:
        return self.capital + self.net


def _parse_json_ts(line: str) -> datetime | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts = obj.get("ts")
    if not ts or not isinstance(ts, str):
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _combine_cycle_time(last_ts: datetime | None, h: str, m: str, s: str) -> datetime | None:
    if last_ts is None:
        return None
    return last_ts.astimezone(timezone.utc).replace(
        hour=int(h), minute=int(m), second=int(s), microsecond=0
    )


def parse_log(path: Path) -> list[Sample]:
    last_ts: datetime | None = None
    cycle_dt: datetime | None = None
    cycle_mode: str = ""
    in_accounts = False
    margin_sum = 0.0
    pending_capital: float | None = None
    out: list[Sample] = []

    def flush_portfolio(capital: float, net_line: str) -> None:
        nonlocal cycle_dt, cycle_mode, margin_sum
        if cycle_dt is None:
            return
        net_m = NET_IN_LINE.search(net_line)
        if net_m is None:
            cycle_dt = None
            return
        net = float(net_m.group(1))
        out.append(
            Sample(
                ts=cycle_dt,
                mode=cycle_mode,
                margin_total=margin_sum,
                capital=capital,
                net=net,
            )
        )
        cycle_dt = None

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            jts = _parse_json_ts(line)
            if jts is not None:
                last_ts = jts

            if pending_capital is not None:
                flush_portfolio(pending_capital, line)
                pending_capital = None
                in_accounts = False
                continue

            cm = CYCLE_RE.search(line)
            if cm:
                cycle_dt = _combine_cycle_time(last_ts, cm["h"], cm["m"], cm["s"])
                cycle_mode = cm["mode"].strip()
                in_accounts = False
                margin_sum = 0.0
                continue

            if cycle_dt is None:
                continue

            if ACCOUNTS_HDR in line:
                in_accounts = True
                continue

            if in_accounts and OPEN_HDR in line:
                in_accounts = False
                continue

            if in_accounts:
                tm = TOTAL_IN_LINE.search(line)
                if tm:
                    margin_sum += float(tm.group(1))
                continue

            pm = PORTFOLIO_HDR.search(line)
            if pm:
                capital = float(pm.group(1))
                if NET_IN_LINE.search(line):
                    flush_portfolio(capital, line)
                    in_accounts = False
                else:
                    pending_capital = capital

    out.sort(key=lambda x: x.ts)
    return out


def hourly_last(samples: list[Sample]) -> list[Sample]:
    """Keep the last sample in each UTC hour."""
    by_hour: dict[datetime, Sample] = {}
    for s in samples:
        h = s.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        by_hour[h] = s
    return [by_hour[k] for k in sorted(by_hour)]


def write_html(path: Path, samples: list[Sample], title: str) -> None:
    labels = [s.ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:00") for s in samples]
    margin = [round(s.margin_total, 2) for s in samples]
    book = [round(s.book_equity, 2) for s in samples]

    data_json = json.dumps(
        {"labels": labels, "margin_total": margin, "book_equity": book},
        ensure_ascii=False,
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; background: #111; color: #e8e8e8; }}
    h1 {{ font-size: 1.1rem; font-weight: 600; }}
    .note {{ color: #888; font-size: 0.85rem; margin-bottom: 16px; }}
    #wrap {{ max-width: 1100px; }}
  </style>
</head>
<body>
  <div id="wrap">
    <h1>{title}</h1>
    <p class="note">margin_total = сумма total= по строкам Accounts;
      book_equity = capital + NET из PORTFOLIO TOTAL (последняя точка в каждом UTC-часе).</p>
    <canvas id="c" height="90"></canvas>
  </div>
  <script>
    const D = {data_json};
    const ctx = document.getElementById('c');
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: D.labels,
        datasets: [
          {{
            label: 'Margin total ($)',
            data: D.margin_total,
            borderColor: '#4fc3f7',
            backgroundColor: 'rgba(79,195,247,0.15)',
            tension: 0.15,
            fill: false,
          }},
          {{
            label: 'Book equity capital+NET ($)',
            data: D.book_equity,
            borderColor: '#81c784',
            backgroundColor: 'rgba(129,199,132,0.1)',
            tension: 0.15,
            fill: false,
          }},
        ],
      }},
      options: {{
        responsive: true,
        interaction: {{ mode: 'index', intersect: false }},
        scales: {{
          x: {{
            ticks: {{ maxRotation: 45, minRotation: 45, color: '#aaa' }},
            grid: {{ color: '#333' }},
          }},
          y: {{
            ticks: {{ color: '#aaa' }},
            grid: {{ color: '#333' }},
          }},
        }},
        plugins: {{
          legend: {{ labels: {{ color: '#ccc' }} }},
        }},
      }},
    }});
  </script>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Portfolio time series from fundbot bot.log")
    ap.add_argument("--log", type=Path, default=Path("bot.log"), help="Path to bot.log")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("artifacts/portfolio_hourly.html"),
        help="Output HTML path",
    )
    ap.add_argument(
        "--all-cycles",
        action="store_true",
        help="Plot every cycle (not only last per UTC hour)",
    )
    args = ap.parse_args()

    if not args.log.is_file():
        raise SystemExit(f"Log not found: {args.log.resolve()}")

    raw = parse_log(args.log)
    if not raw:
        raise SystemExit("No PORTFOLIO TOTAL samples parsed (check log format).")

    series = raw if args.all_cycles else hourly_last(raw)
    title = "Portfolio from log (hourly)" if not args.all_cycles else "Portfolio from log (all cycles)"
    write_html(args.output, series, title)
    print(f"Wrote {args.output.resolve()} ({len(series)} points, {len(raw)} raw cycles)")


if __name__ == "__main__":
    main()
