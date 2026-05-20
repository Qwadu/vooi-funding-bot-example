"""Recover state-snapshot.json from append-only state-live.ndjson event log.

Берём все OPEN_INTENT + OPEN_OK без последующего CLOSE_OK.
Использовать однократно после рестарта чтобы не потерять открытые позы.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    log_path = Path(sys.argv[1] if len(sys.argv) > 1 else "state-live.ndjson")
    snap_path = Path(sys.argv[2] if len(sys.argv) > 2 else "state-snapshot.json")

    if not log_path.exists():
        print(f"ERROR: {log_path} not found", file=sys.stderr)
        return 1

    intents: dict[str, dict] = {}
    quotes: dict[str, dict] = {}
    opens_long: set[str] = set()
    opens_short: set[str] = set()
    closes: set[str] = set()
    opened_at: dict[str, str] = {}

    for raw in log_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        evt = ev.get("event")
        arb_id = ev.get("arb_id")
        if not arb_id:
            continue
        if evt == "OPEN_INTENT":
            intents[arb_id] = ev
            opened_at[arb_id] = ev.get("ts", "")
        elif evt == "OPEN_QUOTES_OK":
            quotes[arb_id] = ev
        elif evt == "OPEN_LONG_RESP" and ev.get("status") == 200:
            opens_long.add(arb_id)
        elif evt == "OPEN_SHORT_RESP" and ev.get("status") == 200:
            opens_short.add(arb_id)
        elif evt == "OPEN_OK":
            opens_long.add(arb_id)
            opens_short.add(arb_id)
        elif evt in ("CLOSE_OK", "OPEN_ROLLBACK_RESP"):
            closes.add(arb_id)

    fully_open = (opens_long & opens_short) - closes
    print(f"Found {len(fully_open)} fully open arbs (not closed/rolled-back)")

    snapshot: dict[str, dict] = {}
    for arb_id in fully_open:
        intent = intents.get(arb_id)
        if intent is None:
            print(f"  skip {arb_id}: no INTENT event", file=sys.stderr)
            continue
        quote = quotes.get(arb_id, {})
        # base_symbol должен совпадать с asset для крипто-перпов; на HL для multi-listed
        # бывает HYPE/HYPE-USD варианты. Берём из QUOTES_OK если есть, иначе asset.
        long_base = quote.get("long_base_symbol") or intent.get("asset", "")
        short_base = quote.get("short_base_symbol") or intent.get("asset", "")
        coid_long = quote.get("coid_long") or f"recovered-{arb_id}-long"
        coid_short = quote.get("coid_short") or f"recovered-{arb_id}-short"

        snapshot[arb_id] = {
            "arb_id": arb_id,
            "asset": intent["asset"],
            "long_exchange": intent["long_exchange"],
            "short_exchange": intent["short_exchange"],
            "long_base_symbol": long_base,
            "short_base_symbol": short_base,
            "opened_at": opened_at.get(arb_id) or intent.get("ts", ""),
            "open_net_apr": intent.get("net_apr", "0"),
            "leg_collat_usd": intent.get("collat_usd", "5"),
            "leg_notional_usd": intent.get("notional_usd", "0"),
            "effective_leverage": int(intent.get("effective_leverage", 1)),
            "long_client_order_id": coid_long,
            "short_client_order_id": coid_short,
        }
        print(f"  recovered {arb_id}: asset={intent['asset']} "
              f"long={intent['long_exchange']} short={intent['short_exchange']}")

    snap_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"\nWrote {snap_path} with {len(snapshot)} positions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
