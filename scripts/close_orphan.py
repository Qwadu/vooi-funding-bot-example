"""One-off helper: закрыть orphan-позицию reduce-only ровно тем размером, что на бирже.

Используем когда бот «забыл» позицию из-за бага в close_position и residue остался висеть
(например, STABLE short на Lighter после fix-cycle 2026-05-01 11:49Z).

Запуск:
    uv run python scripts/close_orphan.py <exchange> <baseSymbol> <side>

где side: 'buy' для long, 'sell' для short (направление _существующей_ позиции).

После запроса позиции скрипт спросит подтверждение y/N перед отправкой ордера.
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe.client import ProbeClient
from probe.probe import _load_dotenv


async def fetch_position(
    client: ProbeClient,
    exchange: str,
    base_symbol: str,
    side: str,
) -> dict[str, Any] | None:
    resp = await client.get("/exchange/positions", params={"exchanges": exchange})
    if resp.status_code != 200:
        print(f"ERROR: GET /exchange/positions {exchange} → status={resp.status_code}", file=sys.stderr)
        return None
    body = resp.json() or []
    target = base_symbol.upper()
    for p in body:
        if (p.get("baseSymbol") or "").upper() == target and p.get("side") == side:
            return p
    return None


async def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: close_orphan.py <exchange> <baseSymbol> <side>", file=sys.stderr)
        print("  side='buy' для long-residue, 'sell' для short-residue", file=sys.stderr)
        return 2
    exchange, asset, side = sys.argv[1].lower(), sys.argv[2].upper(), sys.argv[3].lower()
    if side not in ("buy", "sell"):
        print(f"ERROR: invalid side={side!r}, expected 'buy' or 'sell'", file=sys.stderr)
        return 2

    _load_dotenv(Path(".env"))
    base_url = os.environ.get("VOOI_API_BASE_URL", "https://perps-api.vooi.io")
    token = os.environ.get("VOOI_BEARER_TOKEN")
    if not token:
        print("ERROR: VOOI_BEARER_TOKEN not set in .env", file=sys.stderr)
        return 2

    async with ProbeClient(base_url, token) as client:
        pos = await fetch_position(client, exchange, asset, side)
        if pos is None:
            print(f"No {side} {asset} position found on {exchange}.")
            return 0

        size_raw = pos.get("size", "0")
        try:
            size_dec = abs(Decimal(str(size_raw)))
        except (ValueError, ArithmeticError):
            print(f"ERROR: cannot parse size={size_raw!r}", file=sys.stderr)
            return 1

        if size_dec <= 0:
            print(f"Position size is zero ({size_raw!r}); nothing to close.")
            return 0

        close_side = "sell" if side == "buy" else "buy"
        print(f"Found {side} {asset} on {exchange}: size={size_raw}  unrealizedPnl={pos.get('unrealizedPnl')}  fundingPnl={pos.get('fundingPnl') or pos.get('fundingFee')}")
        print(f"  → will send reduce-only {close_side} order, size={size_dec}")
        if os.environ.get("FORCE") != "1":
            ans = input("Confirm? [y/N] ").strip().lower()
            if ans != "y":
                print("Aborted.")
                return 0
        else:
            print("FORCE=1 → skipping interactive confirm.")

        body: dict[str, Any] = {
            "exchange": exchange,
            "asset": asset,
            "side": close_side,
            "size": str(size_dec),
            "reduceOnly": True,
            "clientOrderId": f"vooi-funding-arb-orphan-cleanup-{asset}-{exchange}",
        }

        print(f"POST /exchange/orders body={body}")
        r = await client.post("/exchange/orders", body=body)
        print(f"  status={r.status_code}  body={r.text[:300]}")

        await asyncio.sleep(2.0)
        pos2 = await fetch_position(client, exchange, asset, side)
        if pos2 is None:
            print(f"VERIFIED: position {side} {asset} on {exchange} is gone.")
            return 0
        size2 = pos2.get("size", "0")
        try:
            size2_dec = abs(Decimal(str(size2)))
        except (ValueError, ArithmeticError):
            size2_dec = Decimal("0")
        if size2_dec <= 0:
            print(f"VERIFIED: size={size2} (effectively closed).")
            return 0
        print(f"WARN: residue still present, size={size2}.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
