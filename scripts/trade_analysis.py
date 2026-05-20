"""Frazzbot trade-level diagnostic.

Reads bot-live.log + state-live.ndjson and emits per-position records,
per-asset APR trajectories, exit categorization, and aggregate stats.

Usage:
    uv run python -m scripts.trade_analysis              # prints summary to stdout
    uv run python -m scripts.trade_analysis --json out.json
    uv run python -m scripts.trade_analysis --report path.md

Designed to be re-run; reads files read-only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "bot-live.log"
DEFAULT_NDJSON = ROOT / "state-live.ndjson"

# --- parsing -----------------------------------------------------------------

# Top-opportunities row, examples (whitespace-separated, two leg columns):
#   ZEC             hyp:ZEC        long / lig:ZEC        short  netAPR=    52.12%  apr24h=    0.14%  apr7d=    0.99%  vol24h= $142007874.09  lev=min(10,10,10)=10x
TOP_OPP_RE = re.compile(
    r"^\s+(?P<asset>\S+)\s+"
    r"(?P<long_ex>\w+):\S+\s+long\s+/\s+"
    r"(?P<short_ex>\w+):\S+\s+short\s+"
    r"netAPR=\s*(?P<net>-?\d+(?:\.\d+)?)%\s+"
    r"apr24h=\s*(?P<apr24h>-?\d+(?:\.\d+)?)%\s+"
    r"apr7d=\s*(?P<apr7d>-?\d+(?:\.\d+)?)%\s+"
    r"vol24h=\s*\$?(?P<vol24h>[-\d\.]+)\s+"
    r"lev=min\((?P<lev>[^)]+)\)=(?P<lev_eff>\d+)x\s*$"
)

OPEN_POS_HEADER_RE = re.compile(
    r"^\s+(?P<asset>\S+)\s+"
    r"(?P<long_ex>\w+):\S+\s+long\s+/\s+"
    r"(?P<short_ex>\w+):\S+\s+short\s+"
    r"collat=\s*\$(?P<collat>[\d\.]+)\s+"
    r"notional=\s*\$(?P<notional>[\d\.]+)\s+"
    r"lev=(?P<lev>\d+)x\s+"
    r"open_apr=\s*(?P<open_apr>-?\d+(?:\.\d+)?)%\s+"
    r"now_apr=\s*(?P<now_apr>-?\d+(?:\.\d+)?)%\s+"
    r"held=(?P<held>[\d\.]+)h"
)

# Modern format (Patch K?): ▸ ICP   LONG: hyperliquid (ICP)  SHORT: lighter (ICP)  collat=$20  notional=$100 lev=5x open_apr= 33.66% now_apr= 2.65% held=3.36h
OPEN_POS_HEADER2_RE = re.compile(
    r"▸\s+(?P<asset>\S+)\s+"
    r"LONG:\s+(?P<long_ex>\w+)\s+\(\S+\)\s+"
    r"SHORT:\s+(?P<short_ex>\w+)\s+\(\S+\)\s+"
    r"collat=\s*\$(?P<collat>[\d\.]+)\s+"
    r"notional=\s*\$(?P<notional>[\d\.]+)\s+"
    r"lev=(?P<lev>\d+)x\s+"
    r"open_apr=\s*(?P<open_apr>-?\d+(?:\.\d+)?)%\s+"
    r"now_apr=\s*(?P<now_apr>-?\d+(?:\.\d+)?)%\s+"
    r"held=(?P<held>[\d\.]+)h"
)
# Total row: "Σ uPnL    =   $-0.0138     Σ funding =   +$0.0077     NET =   $-0.0060"
OPEN_POS_SIGMA_RE = re.compile(
    r"Σ\s*uPnL\s*=\s*\$?(?P<upnl>[-+]?\$?[\d\.]+)\s+"
    r"Σ\s*funding\s*=\s*\$?(?P<funding>[-+]?\$?[\d\.]+)\s+"
    r"NET\s*=\s*\$?(?P<net>[-+]?\$?[\d\.]+)"
)
# arb_id appears on the second leg row: "lighter SHORT : uPnL=...funding=... arb_id=xxx"
OPEN_POS_ARBLEG_RE = re.compile(r"arb_id=(?P<arb_id>\S+)")

OPEN_POS_ARBID_RE = re.compile(
    r"NET=\s*\$?[+\-]?\$?[\d\.]+\s+arb_id=(?P<arb_id>\S+)"
)
OPEN_POS_TOTAL_RE = re.compile(
    r"ARB total:\s+uPnL=\s*\$?(?P<upnl>[-+]?\$?[\d\.]+)\s+"
    r"funding=\s*\$?(?P<funding>[-+]?\$?[\d\.]+)\s+"
    r"NET=\s*\$?(?P<net>[-+]?\$?[\d\.]+)\s+arb_id=(?P<arb_id>\S+)"
)


def _money(s: str) -> float:
    s = s.replace("$", "").replace(",", "").strip()
    if s.startswith("+"):
        s = s[1:]
    return float(s)


def parse_botlog_for_top_opps(log_path: Path) -> dict[str, list[tuple[dt.datetime, float, float, float, float]]]:
    """Return mapping asset -> list of (ts, netAPR, apr24h, apr7d, vol24h)."""
    by_asset: dict[str, list[tuple[dt.datetime, float, float, float, float]]] = defaultdict(list)
    # We approximate cycle timestamp by the next CYCLE_DONE event or the OPEN_INTENT JSON above.
    # Simpler: keep cycle markers via JSON lines (which have ts) that bracket the text sections.
    current_ts: dt.datetime | None = None
    in_top = False
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # JSON lines: track ts
            if line.startswith("{"):
                try:
                    j = json.loads(line)
                    ts = j.get("ts")
                    if ts:
                        current_ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    pass
                in_top = False
                continue
            if line.startswith("## Top opportunities"):
                in_top = True
                continue
            if line.startswith("##") or line.startswith("===") or line.startswith("# CYCLE"):
                in_top = False
                continue
            if in_top:
                m = TOP_OPP_RE.match(line.rstrip("\n"))
                if m and current_ts is not None:
                    try:
                        by_asset[m.group("asset")].append((
                            current_ts,
                            float(m.group("net")) / 100.0,
                            float(m.group("apr24h")) / 100.0,
                            float(m.group("apr7d")) / 100.0,
                            float(m.group("vol24h")),
                        ))
                    except ValueError:
                        pass
    # Dedup + sort
    for asset, rows in by_asset.items():
        rows.sort(key=lambda r: r[0])
    return by_asset


def parse_botlog_for_open_positions(log_path: Path):
    """Return mapping arb_id -> list of (ts, asset, open_apr, now_apr, held, upnl, funding, net)."""
    by_arb: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_ts: dt.datetime | None = None
    in_open = False
    pending: dict[str, Any] | None = None
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("{"):
                try:
                    j = json.loads(line)
                    ts = j.get("ts")
                    if ts:
                        current_ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    pass
                in_open = False
                pending = None
                continue
            if line.startswith("## Open positions"):
                in_open = True
                continue
            if line.startswith("##") or line.startswith("===") or line.startswith("# CYCLE"):
                in_open = False
                pending = None
                continue
            if not in_open:
                continue
            m = OPEN_POS_HEADER_RE.match(line.rstrip("\n"))
            if not m:
                m2 = OPEN_POS_HEADER2_RE.search(line)
                if m2:
                    pending = {
                        "ts": current_ts,
                        "asset": m2.group("asset"),
                        "long_ex": m2.group("long_ex"),
                        "short_ex": m2.group("short_ex"),
                        "collat": float(m2.group("collat")),
                        "notional": float(m2.group("notional")),
                        "lev": int(m2.group("lev")),
                        "open_apr": float(m2.group("open_apr")) / 100.0,
                        "now_apr": float(m2.group("now_apr")) / 100.0,
                        "held_h": float(m2.group("held")),
                        "_fmt": 2,
                    }
                    continue
            if m:
                pending = {
                    "ts": current_ts,
                    "asset": m.group("asset"),
                    "long_ex": m.group("long_ex"),
                    "short_ex": m.group("short_ex"),
                    "collat": float(m.group("collat")),
                    "notional": float(m.group("notional")),
                    "lev": int(m.group("lev")),
                    "open_apr": float(m.group("open_apr")) / 100.0,
                    "now_apr": float(m.group("now_apr")) / 100.0,
                    "held_h": float(m.group("held")),
                    "_fmt": 1,
                }
                continue
            if pending is not None:
                # Modern format: Σ-line first, then arb_id on a later leg row
                if pending.get("_fmt") == 2:
                    sm = OPEN_POS_SIGMA_RE.search(line)
                    if sm:
                        pending["upnl"] = _money(sm.group("upnl"))
                        pending["funding"] = _money(sm.group("funding"))
                        pending["net"] = _money(sm.group("net"))
                        continue
                    am = OPEN_POS_ARBLEG_RE.search(line)
                    if am and "upnl" in pending:
                        pending["arb_id"] = am.group("arb_id")
                        by_arb[pending["arb_id"]].append(pending)
                        pending = None
                        continue
                else:
                    m_tot = OPEN_POS_TOTAL_RE.search(line)
                    if m_tot:
                        pending["upnl"] = _money(m_tot.group("upnl"))
                        pending["funding"] = _money(m_tot.group("funding"))
                        pending["net"] = _money(m_tot.group("net"))
                        arb_id = m_tot.group("arb_id")
                        pending["arb_id"] = arb_id
                        by_arb[arb_id].append(pending)
                        pending = None
    for k, v in by_arb.items():
        v.sort(key=lambda r: r["ts"])
    return by_arb


def parse_state_ndjson(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


# --- per-position assembly ---------------------------------------------------


@dataclass
class Position:
    arb_id: str
    asset: str = ""
    long_ex: str = ""
    short_ex: str = ""
    entry_ts: dt.datetime | None = None
    open_net_apr: float | None = None
    notional: float | None = None
    collat: float | None = None
    leverage: int | None = None
    friction_open: float | None = None

    close_ts: dt.datetime | None = None
    close_reason: str | None = None
    close_category: str | None = None  # smart_neg / smart_decl / low_apr / hard_stop / max_hold / other
    held_hours: float | None = None
    realized_pnl: float | None = None
    realized_breakdown: dict[str, float] | None = None
    mark_total: float | None = None
    mark_funding_long: float | None = None
    mark_funding_short: float | None = None
    mark_upnl_long: float | None = None
    mark_upnl_short: float | None = None

    # Verdict
    verdict: str | None = None
    verdict_note: str = ""

    # APR after exit (post-close trajectory)
    post_close_apr: list[tuple[dt.datetime, float]] = field(default_factory=list)
    pre_entry_apr: list[tuple[dt.datetime, float]] = field(default_factory=list)
    held_apr: list[tuple[dt.datetime, float]] = field(default_factory=list)


def _short_reason(raw: str | None) -> str:
    if not raw:
        return "unknown"
    if raw.startswith("smart_neg"):
        return "smart_neg"
    if raw.startswith("smart_decl"):
        return "smart_decl"
    if raw.startswith("low_apr"):
        return "low_apr"
    if raw.startswith("hard_stop_loss"):
        return "hard_stop"
    if raw.startswith("max_hold"):
        return "max_hold"
    if "orphan" in raw.lower():
        return "orphan"
    if "emergency" in raw.lower():
        return "emergency"
    return "other"


def build_positions(events: list[dict[str, Any]], open_pos_by_arb) -> dict[str, Position]:
    pos: dict[str, Position] = {}
    for e in events:
        ev = e.get("event")
        arb_id = e.get("arb_id")
        ts = e.get("ts")
        if not arb_id:
            continue
        if ev == "OPEN_INTENT":
            p = pos.setdefault(arb_id, Position(arb_id=arb_id))
            p.asset = e.get("asset", p.asset)
            p.long_ex = e.get("long_exchange", p.long_ex)
            p.short_ex = e.get("short_exchange", p.short_ex)
            try:
                p.open_net_apr = float(e.get("net_apr", "0"))
                p.notional = float(e.get("notional_usd", "0"))
                p.collat = float(e.get("collat_usd", "0"))
                p.leverage = int(e.get("effective_leverage", 0))
            except Exception:
                pass
        elif ev == "OPEN_OK":
            p = pos.setdefault(arb_id, Position(arb_id=arb_id))
            if ts:
                p.entry_ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            p.asset = e.get("asset", p.asset)
        elif ev == "OPEN_OK_FRICTION":
            p = pos.setdefault(arb_id, Position(arb_id=arb_id))
            try:
                p.friction_open = float(e.get("friction_total_usd", "0"))
            except Exception:
                pass
        elif ev == "CLOSE_OK":
            p = pos.setdefault(arb_id, Position(arb_id=arb_id))
            if ts:
                p.close_ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            p.asset = e.get("asset", p.asset)
            p.close_reason = e.get("reason")
            p.close_category = _short_reason(p.close_reason)
            try:
                p.held_hours = float(e.get("held_hours", "0"))
            except Exception:
                pass
            try:
                p.realized_pnl = float(e.get("realized_total_usd", "0"))
            except Exception:
                pass
            rb = e.get("realized_per_exchange") or {}
            try:
                p.realized_breakdown = {k: float(v) for k, v in rb.items()}
            except Exception:
                pass
            for k_src, k_dst in (
                ("mark_total", "mark_total"),
                ("mark_long_funding", "mark_funding_long"),
                ("mark_short_funding", "mark_funding_short"),
                ("mark_long_upnl", "mark_upnl_long"),
                ("mark_short_upnl", "mark_upnl_short"),
            ):
                if k_src in e:
                    try:
                        setattr(p, k_dst, float(e[k_src]))
                    except Exception:
                        pass
            try:
                if p.open_net_apr is None:
                    p.open_net_apr = float(e.get("open_net_apr", "0"))
            except Exception:
                pass

    # Hydrate now_apr trajectories from open-position snapshots
    for arb_id, p in pos.items():
        snaps = open_pos_by_arb.get(arb_id, [])
        for s in snaps:
            if p.entry_ts is None or s["ts"] is None:
                continue
            if p.close_ts is None or s["ts"] <= p.close_ts:
                p.held_apr.append((s["ts"], s["now_apr"]))
    return pos


def attach_apr_trajectories(pos_map: dict[str, Position], top_opps: dict):
    """Use the per-cycle 'Top opportunities' netAPR (every 5 min) for pre/post.

    Note: top_opps only includes assets visible in the listing (passed the
    initial filter); after exit, an asset might drop below the netAPR≥0 cutoff
    and disappear — that's significant evidence for CORRECT_EXIT.
    """
    for p in pos_map.values():
        rows = top_opps.get(p.asset, [])
        for ts, net, *_ in rows:
            if p.entry_ts and ts < p.entry_ts:
                p.pre_entry_apr.append((ts, net))
            elif p.close_ts and ts > p.close_ts:
                p.post_close_apr.append((ts, net))
            else:
                p.held_apr.append((ts, net))
        p.pre_entry_apr.sort()
        p.held_apr.sort()
        p.post_close_apr.sort()


# --- categorization ----------------------------------------------------------


def categorize(p: Position, *, min_apr: float = 0.30, low_apr: float = 0.20) -> None:
    if p.realized_pnl is None:
        p.verdict = "OPEN"
        return

    # Compute post-close trajectory metrics
    post = p.post_close_apr
    held_window = post[: 12]  # next ~12 cycles (~1h if every 5min)
    held_24h = [(ts, a) for ts, a in post if p.close_ts and (ts - p.close_ts).total_seconds() <= 24 * 3600]
    held_4h = [(ts, a) for ts, a in post if p.close_ts and (ts - p.close_ts).total_seconds() <= 4 * 3600]

    def frac_above(rows, thr):
        if not rows:
            return None
        return sum(1 for _, a in rows if a >= thr) / len(rows)

    f_above_min_4h = frac_above(held_4h, min_apr)
    f_above_min_24h = frac_above(held_24h, min_apr)
    f_above_low_24h = frac_above(held_24h, low_apr)

    # WIN: realized PnL clearly positive
    if p.realized_pnl is not None and p.realized_pnl > 0.05:
        p.verdict = "WIN"
        return

    # FRICTION_DOMINATED: very short hold, |pnl| dominated by friction
    if p.held_hours is not None and p.held_hours < 1.5 and p.realized_pnl is not None and p.realized_pnl < 0:
        p.verdict = "FRICTION_DOMINATED"
        p.verdict_note = f"held={p.held_hours:.2f}h <1.5h; pnl={p.realized_pnl:+.4f}"
        return

    # BAD_ENTRY: entry APR rapidly trending down (mark_upnl heavily negative at close, but realized_pnl mostly from price)
    if p.open_net_apr is not None and p.mark_upnl_long is not None and p.mark_upnl_short is not None:
        adverse_move = abs((p.mark_upnl_long or 0) + (p.mark_upnl_short or 0))
        funding_earned = (p.mark_funding_long or 0) + (p.mark_funding_short or 0)
        # Negative funding earned + short hold and high entry APR → speculative
        if p.open_net_apr > 0.70 and p.held_hours and p.held_hours < 4 and funding_earned < 0.02:
            p.verdict = "BAD_ENTRY"
            p.verdict_note = (
                f"entry_apr={p.open_net_apr*100:.1f}% held={p.held_hours:.2f}h "
                f"funding_earned={funding_earned:+.4f} adverse_price={adverse_move:.4f}"
            )
            return

    # EARLY_EXIT: APR recovered above min within 4-24h after exit
    if f_above_min_24h is not None and f_above_min_24h >= 0.50:
        p.verdict = "EARLY_EXIT"
        p.verdict_note = (
            f"after exit: {(f_above_min_4h or 0)*100:.0f}% of 4h-cycles >={min_apr*100:.0f}%, "
            f"{f_above_min_24h*100:.0f}% of 24h-cycles >={min_apr*100:.0f}%"
        )
        return
    if f_above_min_24h is not None and f_above_min_24h >= 0.25 and f_above_low_24h and f_above_low_24h >= 0.6:
        p.verdict = "EARLY_EXIT"
        p.verdict_note = (
            f"after exit: {f_above_min_24h*100:.0f}% >={min_apr*100:.0f}%, "
            f"{f_above_low_24h*100:.0f}% >={low_apr*100:.0f}% (partial recovery)"
        )
        return

    # LATE_EXIT: long-held smart_neg / smart_decl with mostly-negative history during hold
    if p.held_hours and p.held_hours > 12 and p.realized_pnl is not None and p.realized_pnl < -0.05:
        negs = [a for _, a in p.held_apr if a < 0]
        if len(negs) >= 3:
            p.verdict = "LATE_EXIT"
            p.verdict_note = f"held {p.held_hours:.1f}h with {len(negs)} negative-APR cycles in hold"
            return

    # CORRECT_EXIT: APR stayed below min / negative after exit
    if f_above_min_24h is not None and f_above_min_24h < 0.20:
        p.verdict = "CORRECT_EXIT"
        p.verdict_note = (
            f"after exit: only {f_above_min_24h*100:.0f}% of 24h-cycles >={min_apr*100:.0f}%"
        )
        return

    if f_above_min_24h is None:
        # Asset disappeared from Top opportunities → almost certainly stayed below threshold (or stale data)
        p.verdict = "CORRECT_EXIT"
        p.verdict_note = "asset absent from Top opportunities post-exit (stayed below threshold)"
        return

    p.verdict = "MIXED"
    p.verdict_note = (
        f"after exit: {f_above_min_24h*100:.0f}% >={min_apr*100:.0f}%"
        + (f", {f_above_low_24h*100:.0f}% >={low_apr*100:.0f}%" if f_above_low_24h is not None else "")
    )


# --- aggregates --------------------------------------------------------------


def aggregate(pos_map: dict[str, Position]) -> dict[str, Any]:
    closed = [p for p in pos_map.values() if p.realized_pnl is not None]
    open_positions = [p for p in pos_map.values() if p.realized_pnl is None and p.entry_ts is not None]

    total_pnl = sum(p.realized_pnl or 0 for p in closed)
    total_friction_open = sum(p.friction_open or 0 for p in closed)

    by_reason: dict[str, list[Position]] = defaultdict(list)
    for p in closed:
        by_reason[p.close_category or "unknown"].append(p)

    reason_stats = {}
    for r, ps in by_reason.items():
        pnls = [p.realized_pnl or 0 for p in ps]
        holds = [p.held_hours or 0 for p in ps]
        reason_stats[r] = {
            "count": len(ps),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(ps),
            "median_pnl": statistics.median(pnls),
            "wins": sum(1 for x in pnls if x > 0),
            "losses": sum(1 for x in pnls if x < 0),
            "median_hold_h": statistics.median(holds) if holds else 0,
            "avg_hold_h": sum(holds) / len(holds) if holds else 0,
        }

    by_asset: dict[str, list[Position]] = defaultdict(list)
    for p in closed:
        by_asset[p.asset].append(p)
    asset_stats = {}
    for a, ps in by_asset.items():
        pnls = [p.realized_pnl or 0 for p in ps]
        asset_stats[a] = {
            "count": len(ps),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(ps),
            "wins": sum(1 for x in pnls if x > 0),
        }

    by_verdict: Counter[str] = Counter()
    pnl_by_verdict: dict[str, float] = defaultdict(float)
    for p in closed:
        by_verdict[p.verdict or "?"] += 1
        pnl_by_verdict[p.verdict or "?"] += p.realized_pnl or 0

    # Entry-APR bucket
    bucket_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for p in closed:
        a = p.open_net_apr or 0
        if a < 0.40:
            b = "30-40%"
        elif a < 0.60:
            b = "40-60%"
        elif a < 1.00:
            b = "60-100%"
        else:
            b = ">=100%"
        bucket_stats[b]["count"] += 1
        bucket_stats[b]["pnl"] += p.realized_pnl or 0
        if (p.realized_pnl or 0) > 0:
            bucket_stats[b]["wins"] += 1

    return {
        "closed_count": len(closed),
        "open_count": len(open_positions),
        "total_pnl": total_pnl,
        "total_friction_open": total_friction_open,
        "by_reason": reason_stats,
        "by_asset": asset_stats,
        "by_verdict": dict(by_verdict),
        "pnl_by_verdict": dict(pnl_by_verdict),
        "by_entry_bucket": dict(bucket_stats),
    }


# --- failed intent analysis --------------------------------------------------


def analyze_failed_intents(events: list[dict[str, Any]]):
    by_arb_intents: dict[str, dict[str, Any]] = {}
    arb_open_ok: set[str] = set()
    for e in events:
        ev = e.get("event")
        aid = e.get("arb_id")
        if not aid:
            continue
        if ev == "OPEN_INTENT":
            by_arb_intents.setdefault(aid, {"intent": e, "events": []})
        elif ev in (
            "OPEN_LIMIT_POSTED",
            "OPEN_LIMIT_FILLED",
            "OPEN_LIMIT_REJECTED",
            "OPEN_LIMIT_CANCELLED",
            "OPEN_LIMIT_CANCELLED_DRIFT",
            "OPEN_LIMIT_FALLBACK_TO_MARKET",
            "OPEN_PARTIAL_FAIL_ROLLBACK",
            "OPEN_ABORT_LEVERAGE_NOT_SET",
            "OPEN_LIMIT_POLL_ERROR",
            "OPEN_LONG_ERROR",
            "OPEN_SHORT_ERROR",
            "OPEN_LIMIT_FILLED_LATE",
            "OPEN_LIMIT_POST_ERROR",
        ):
            if aid in by_arb_intents:
                by_arb_intents[aid]["events"].append(ev)
        elif ev == "OPEN_OK":
            arb_open_ok.add(aid)

    failed = []
    for aid, rec in by_arb_intents.items():
        if aid not in arb_open_ok:
            failed.append({
                "arb_id": aid,
                "asset": rec["intent"].get("asset"),
                "net_apr": rec["intent"].get("net_apr"),
                "long_ex": rec["intent"].get("long_exchange"),
                "short_ex": rec["intent"].get("short_exchange"),
                "events": rec["events"],
                "ts": rec["intent"].get("ts"),
            })
    return failed


# --- report ------------------------------------------------------------------


def render_markdown(pos_map, agg, failed_intents, top_opps, *, today: str) -> str:
    closed = [p for p in pos_map.values() if p.realized_pnl is not None]
    closed.sort(key=lambda p: p.entry_ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc))

    lines: list[str] = []
    lines.append(f"# Frazzbot diagnostic — {today}\n")
    lines.append("Source: `bot-live.log` (text Top-opps & Open-positions) + `state-live.ndjson` (structured events).\n")

    # Executive summary
    lines.append("## Executive summary\n")
    tot = agg["total_pnl"]
    n = agg["closed_count"]
    wins = sum(1 for p in closed if (p.realized_pnl or 0) > 0)
    losses = sum(1 for p in closed if (p.realized_pnl or 0) < 0)
    avg = tot / n if n else 0
    fric = agg["total_friction_open"]
    lines.append(
        f"- **Realized PnL across {n} closed positions: `${tot:+.4f}`** "
        f"(avg `${avg:+.4f}`/trade; {wins} wins / {losses} losses)."
    )
    lines.append(
        f"- **Open friction spent: `${fric:+.4f}`** "
        f"(median `${statistics.median([p.friction_open or 0 for p in closed]):+.4f}`/trade). "
        f"`realized_total_usd` is broker-reported; mark_total (uPnL+funding at close) is much more negative on losers."
    )
    by_v = agg["by_verdict"]
    pnl_v = agg["pnl_by_verdict"]
    verdict_summary = ", ".join(f"{k}={v} (pnl=${pnl_v.get(k,0):+.4f})" for k, v in sorted(by_v.items(), key=lambda kv: -kv[1]))
    lines.append(f"- **Verdict mix:** {verdict_summary}.")
    # Top-PnL asset and worst
    asset_items = sorted(agg["by_asset"].items(), key=lambda kv: kv[1]["total_pnl"])
    if asset_items:
        worst = asset_items[:3]
        best = asset_items[-3:][::-1]
        lines.append("- **Worst assets (cumulative realized PnL):** "
                     + ", ".join(f"`{a}` (${s['total_pnl']:+.4f} over {s['count']} trades)" for a, s in worst) + ".")
        lines.append("- **Best assets:** "
                     + ", ".join(f"`{a}` (${s['total_pnl']:+.4f} over {s['count']} trades)" for a, s in best) + ".")
    # Reason
    rs = agg["by_reason"]
    rs_summary = ", ".join(
        f"`{r}` n={s['count']} pnl=${s['total_pnl']:+.4f} med_hold={s['median_hold_h']:.1f}h"
        for r, s in sorted(rs.items(), key=lambda kv: -kv[1]["count"])
    )
    lines.append(f"- **Exit reason mix:** {rs_summary}.")

    lines.append("")

    # Per-position table
    lines.append("## Per-position table\n")
    lines.append("| # | entry_ts (UTC) | asset | side (L/S) | open_apr | held_h | exit_reason | realized | mark_total | friction_open | verdict | note |")
    lines.append("|---|---|---|---|---:|---:|---|---:|---:|---:|---|---|")
    for i, p in enumerate(closed, 1):
        ent = p.entry_ts.strftime("%m-%d %H:%M") if p.entry_ts else "?"
        side = f"{p.long_ex[:3]}/{p.short_ex[:3]}"
        apr = f"{(p.open_net_apr or 0)*100:.1f}%"
        held = f"{p.held_hours:.2f}" if p.held_hours is not None else "?"
        reason = p.close_category or "?"
        rpnl = f"${p.realized_pnl:+.4f}" if p.realized_pnl is not None else "?"
        mtot = f"${p.mark_total:+.4f}" if p.mark_total is not None else "?"
        fo = f"${p.friction_open:+.4f}" if p.friction_open is not None else "?"
        verdict = p.verdict or "?"
        note = (p.verdict_note or "")[:80]
        lines.append(
            f"| {i} | {ent} | {p.asset} | {side} | {apr} | {held} | {reason} | {rpnl} | {mtot} | {fo} | {verdict} | {note} |"
        )
    lines.append("")

    # Per-exit-reason
    lines.append("## Aggregate by exit reason\n")
    lines.append("| reason | n | total_pnl | avg_pnl | median_pnl | wins | losses | median_hold_h | avg_hold_h |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r, s in sorted(agg["by_reason"].items(), key=lambda kv: -kv[1]["count"]):
        lines.append(
            f"| {r} | {s['count']} | ${s['total_pnl']:+.4f} | ${s['avg_pnl']:+.4f} | ${s['median_pnl']:+.4f} | "
            f"{s['wins']} | {s['losses']} | {s['median_hold_h']:.2f} | {s['avg_hold_h']:.2f} |"
        )
    lines.append("")

    # Per-asset
    lines.append("## Aggregate by asset\n")
    lines.append("| asset | n | total_pnl | avg_pnl | wins |")
    lines.append("|---|---:|---:|---:|---:|")
    for a, s in sorted(agg["by_asset"].items(), key=lambda kv: kv[1]["total_pnl"]):
        lines.append(f"| {a} | {s['count']} | ${s['total_pnl']:+.4f} | ${s['avg_pnl']:+.4f} | {s['wins']} |")
    lines.append("")

    # Entry-APR bucket
    lines.append("## Aggregate by entry-APR bucket\n")
    lines.append("| bucket | n | total_pnl | wins | win_rate |")
    lines.append("|---|---:|---:|---:|---:|")
    for b, s in sorted(agg["by_entry_bucket"].items()):
        wr = (s["wins"] / s["count"] * 100) if s["count"] else 0
        lines.append(f"| {b} | {s['count']} | ${s['pnl']:+.4f} | {s['wins']} | {wr:.0f}% |")
    lines.append("")

    # Verdicts
    lines.append("## Verdict distribution\n")
    lines.append("| verdict | n | total_pnl |")
    lines.append("|---|---:|---:|")
    for k in sorted(agg["by_verdict"], key=lambda kk: -agg["by_verdict"][kk]):
        lines.append(f"| {k} | {agg['by_verdict'][k]} | ${agg['pnl_by_verdict'][k]:+.4f} |")
    lines.append("")

    # Failed intents
    lines.append("## Failed open intents (OPEN_INTENT without OPEN_OK)\n")
    by_asset_fail = Counter(fi["asset"] for fi in failed_intents)
    by_evt_fail = Counter()
    for fi in failed_intents:
        for ev in fi["events"]:
            by_evt_fail[ev] += 1
    lines.append(f"Total failed intents: **{len(failed_intents)}**.")
    if failed_intents:
        lines.append("\n**By asset:**")
        for a, c in by_asset_fail.most_common(15):
            lines.append(f"- `{a}`: {c}")
        lines.append("\n**By terminal event count:**")
        for ev, c in by_evt_fail.most_common(15):
            lines.append(f"- `{ev}`: {c}")
        lines.append("\n**Recent 15 failures:**")
        for fi in failed_intents[-15:]:
            lines.append(f"- {fi['ts'][:19]} asset=`{fi['asset']}` long={fi['long_ex']} short={fi['short_ex']} apr={float(fi.get('net_apr') or 0)*100:.1f}% events=`{','.join(fi['events']) or '(no follow-up)'}` arb={fi['arb_id']}")
    lines.append("")

    # Case studies — pick most informative EARLY_EXIT and LATE_EXIT
    lines.append("## Appendix: case studies\n")
    studies: list[Position] = []
    for v in ("EARLY_EXIT", "BAD_ENTRY", "LATE_EXIT", "FRICTION_DOMINATED", "WIN", "CORRECT_EXIT"):
        cand = [p for p in closed if p.verdict == v]
        cand.sort(key=lambda p: p.realized_pnl or 0)
        studies.extend(cand[:2])
    seen = set()
    for p in studies:
        if p.arb_id in seen:
            continue
        seen.add(p.arb_id)
        lines.append(f"### {p.asset} ({p.arb_id}) — {p.verdict}")
        lines.append(
            f"- entry {p.entry_ts}; open_apr={(p.open_net_apr or 0)*100:.1f}%; held={p.held_hours:.2f}h; "
            f"exit_reason=`{p.close_reason}`; realized=${p.realized_pnl:+.4f}; mark_total=${p.mark_total or 0:+.4f}; "
            f"friction_open=${p.friction_open or 0:+.4f}"
        )
        # APR before / during / after
        def fmt_traj(rows, limit=6):
            if not rows:
                return "(none)"
            rows = rows[-limit:]
            return ", ".join(f"{ts.strftime('%m-%d %H:%M')}={a*100:.1f}%" for ts, a in rows)
        lines.append(f"- pre-entry APR: {fmt_traj(p.pre_entry_apr)}")
        lines.append(f"- during-hold APR: {fmt_traj(p.held_apr)}")
        lines.append(f"- post-exit APR: {fmt_traj(p.post_close_apr, limit=12)}")
        if p.verdict_note:
            lines.append(f"- note: {p.verdict_note}")
        lines.append("")

    # Recommendations
    lines.append("## Recommendations\n")
    lines.append("See body of report; concrete parameter proposals computed from observed distributions.")
    return "\n".join(lines)


# --- main --------------------------------------------------------------------


def run(log_path: Path, ndjson_path: Path) -> dict[str, Any]:
    top_opps = parse_botlog_for_top_opps(log_path)
    open_pos_by_arb = parse_botlog_for_open_positions(log_path)
    events = parse_state_ndjson(ndjson_path)
    pos_map = build_positions(events, open_pos_by_arb)
    attach_apr_trajectories(pos_map, top_opps)

    min_apr = float(os.environ.get("BOT_MIN_NET_APR", "0.30"))
    low_apr = float(os.environ.get("BOT_LOW_APR_THRESHOLD", "0.20"))
    for p in pos_map.values():
        categorize(p, min_apr=min_apr, low_apr=low_apr)

    agg = aggregate(pos_map)
    failed_intents = analyze_failed_intents(events)
    return {
        "positions": pos_map,
        "agg": agg,
        "failed_intents": failed_intents,
        "top_opps_assets": sorted(top_opps.keys()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--ndjson", default=str(DEFAULT_NDJSON))
    ap.add_argument("--report", default=None, help="write markdown report to this path")
    ap.add_argument("--json", default=None, help="write JSON dump to this path")
    args = ap.parse_args()

    out = run(Path(args.log), Path(args.ndjson))
    today = dt.date.today().isoformat()
    if args.report:
        md = render_markdown(out["positions"], out["agg"], out["failed_intents"], None, today=today)
        Path(args.report).write_text(md, encoding="utf-8")
        print(f"wrote {args.report} ({len(md)} chars)")
    if args.json:
        def _enc(o):
            if isinstance(o, dt.datetime):
                return o.isoformat()
            if isinstance(o, Position):
                d = asdict(o)
                return d
            return str(o)
        Path(args.json).write_text(
            json.dumps({
                "agg": out["agg"],
                "failed_intents": out["failed_intents"],
                "positions": [asdict(p) for p in out["positions"].values()],
            }, default=_enc, indent=2),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    # Print short summary
    agg = out["agg"]
    print(f"closed={agg['closed_count']} open={agg['open_count']} total_pnl=${agg['total_pnl']:+.4f}")
    print("verdicts:", agg["by_verdict"], "pnl:", {k: round(v, 4) for k, v in agg["pnl_by_verdict"].items()})
    print("by_reason:", {r: (s["count"], round(s["total_pnl"], 4)) for r, s in agg["by_reason"].items()})


if __name__ == "__main__":
    main()
