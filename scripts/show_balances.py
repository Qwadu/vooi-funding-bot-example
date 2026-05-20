"""One-off helper: показать актуальные балансы и market counts на каждой бирже.

Запуск:
    uv run python scripts/show_balances.py

Read-only — никаких ордеров/изменений.

Помечает раздельно:
- `account.type=perps` — для crypto-arb (USED by funding-arb bot)
- `account.type=xyz` — для stocks/commodities (`xyz:*` пары; OUT of scope)
- `account.type` иное (`spot`/`other`) — также показано, но out of scope.

Для каждой биржи также показывает разбивку markets: crypto-perps vs xyz/alias.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe.client import ProbeClient
from probe.markets import (
    CRYPTO_PERPS_ACCOUNT_TYPE,
    is_crypto_perps_market,
    is_non_crypto_prefix,
)
from probe.probe import _load_dotenv


def _redact(token: str) -> str:
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


def _fmt_money(s: Any) -> str:
    try:
        return f"${Decimal(str(s)):,.4f}"
    except (ValueError, ArithmeticError):
        return f"${s}"


async def _fetch_accounts(
    client: ProbeClient,
    exchange: str,
) -> list[dict[str, Any]]:
    resp = await client.get("/exchange/accounts", params={"exchanges": exchange})
    if resp.status_code != 200:
        return []
    body = resp.json()
    return body if isinstance(body, list) else []


async def _fetch_markets_breakdown(
    client: ProbeClient,
    exchange: str,
) -> dict[str, Any]:
    resp = await client.get("/exchange/markets", params={"exchanges": exchange})
    if resp.status_code != 200:
        return {"total": 0, "crypto_perps": 0, "non_crypto": 0, "non_crypto_sample": []}
    markets = resp.json() if isinstance(resp.json(), list) else []
    crypto = [m for m in markets if is_crypto_perps_market(m)]
    non_crypto_syms = sorted(
        {
            m.get("baseSymbol")
            for m in markets
            if is_non_crypto_prefix(m.get("baseSymbol") or "")
        },
    )
    return {
        "total": len(markets),
        "crypto_perps": len(crypto),
        "non_crypto": len(non_crypto_syms),
        "non_crypto_sample": non_crypto_syms[:8],
    }


async def main() -> int:
    _load_dotenv(Path(".env"))
    base_url = os.environ.get("VOOI_API_BASE_URL", "https://perps-api.vooi.io")
    token = os.environ.get("VOOI_BEARER_TOKEN")
    if not token:
        print("ERROR: VOOI_BEARER_TOKEN not set in .env", file=sys.stderr)
        return 2

    target_exchanges = [
        e.strip()
        for e in os.environ.get(
            "BOT_TARGET_EXCHANGES", "hyperliquid,lighter,aster",
        ).split(",")
        if e.strip()
    ]

    print(f"# base_url    : {base_url}")
    print(f"# token       : {_redact(token)}")
    print(f"# exchanges   : {target_exchanges}")
    print()

    grand_total = Decimal("0")
    crypto_total = Decimal("0")
    crypto_available = Decimal("0")

    async with ProbeClient(base_url, token) as client:
        for exchange in target_exchanges:
            accounts = await _fetch_accounts(client, exchange)
            markets = await _fetch_markets_breakdown(client, exchange)

            print(
                f"## {exchange}  "
                f"(markets total={markets['total']}, "
                f"crypto-perps={markets['crypto_perps']}, "
                f"non-crypto={markets['non_crypto']})",
            )
            if markets["non_crypto_sample"]:
                print(
                    f"   non-crypto sample: {markets['non_crypto_sample']}",
                )

            if not accounts:
                print("   (нет accounts response)")
                print()
                continue

            by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for a in accounts:
                by_type[a.get("type") or "<unknown>"].append(a)

            for acct_type in sorted(by_type.keys()):
                role = (
                    "crypto-arb (USED)"
                    if acct_type == CRYPTO_PERPS_ACCOUNT_TYPE
                    else "stocks/commodities (OUT of scope)"
                    if acct_type == "xyz"
                    else "out of scope"
                )
                print(f"   account.type={acct_type!r}  [{role}]")
                for a in by_type[acct_type]:
                    total = a.get("totalBalance", "0")
                    avail = a.get("availableMargin", "0")
                    in_use = a.get("marginInUse", "0")
                    wd = a.get("withdrawable", "0")
                    print(
                        f"      total={_fmt_money(total):>14}  "
                        f"available={_fmt_money(avail):>14}  "
                        f"in_use={_fmt_money(in_use):>10}  "
                        f"withdrawable={_fmt_money(wd):>14}",
                    )
                    try:
                        d = Decimal(str(total))
                        grand_total += d
                        if acct_type == CRYPTO_PERPS_ACCOUNT_TYPE:
                            crypto_total += d
                            crypto_available += Decimal(str(avail))
                    except (ValueError, ArithmeticError):
                        pass
            print()

    print("=" * 70)
    print(f"# Σ all accounts (incl. xyz)        : {_fmt_money(grand_total)}")
    print(f"# Σ crypto-perps total              : {_fmt_money(crypto_total)}")
    print(f"# Σ crypto-perps available для arb  : {_fmt_money(crypto_available)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
