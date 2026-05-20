#!/usr/bin/env python3
"""Close all open positions from state-snapshot.json via bot's close_position().

Использует тот же reduceOnly + retry mechanism что и сам бот при streak/stop close.
Запуск: uv run python scripts/close_all.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow `from fundbot.mvp import ...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fundbot.mvp import (  # noqa: E402
    NDJsonLog,
    Settings,
    VooiClient,
    close_position,
    load_snapshot,
    save_snapshot,
)


def _load_env_file(env_path: Path = Path(".env")) -> None:
    """Same simple .env loader as fundbot.mvp.run() — without subclassing it."""
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


async def main() -> int:
    _load_env_file()

    dry_run_override = "--dry-run" in sys.argv
    if dry_run_override:
        os.environ["BOT_DRY_RUN"] = "true"

    settings = Settings.from_env()
    log = NDJsonLog(settings.state_file, f"close-all-{settings.instance_uuid}")

    state = load_snapshot(settings.snapshot_file)
    if not state:
        print("No open positions in snapshot.")
        return 0

    print(f"=== close_all: {len(state)} positions to close ===")
    for arb_id, pos in state.items():
        print(f"  {pos.asset:8s}  {pos.long_exchange[:3]}/{pos.short_exchange[:3]}  "
              f"notional=${pos.leg_notional_usd}  arb_id={arb_id}")
    print()

    success: list[str] = []
    failed: list[str] = []

    async with VooiClient(settings.base_url, settings.bearer_token) as client:
        for arb_id, pos in list(state.items()):
            print(f"--- closing {pos.asset} ({arb_id}) ---")
            try:
                ok = await close_position(
                    client, log, settings, pos,
                    reason="manual_close_all",
                )
                if ok:
                    success.append(pos.asset)
                    print(f"  ✓ {pos.asset} CLOSE_OK\n")
                else:
                    failed.append(pos.asset)
                    print(f"  ✗ {pos.asset} CLOSE returned False\n")
            except (RuntimeError, ValueError, OSError) as e:
                failed.append(f"{pos.asset}({type(e).__name__})")
                print(f"  ✗ {pos.asset} EXCEPTION: {e}\n")

    print("\n=== summary ===")
    print(f"  closed OK: {len(success)} → {success}")
    print(f"  failed:    {len(failed)} → {failed}")
    if not failed and state:
        save_snapshot({}, settings.snapshot_file)
        print(f"  snapshot cleared → {settings.snapshot_file}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
