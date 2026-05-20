#!/usr/bin/env python3
"""Close a single strategy from state-snapshot.json via close_position() + save snapshot.

Usage:
    uv run python scripts/close_one.py <asset-or-arb-id> [--dry-run]

Examples:
    uv run python scripts/close_one.py alias:samsung
    uv run python scripts/close_one.py 4641b1d7d1ac-2b0b6ea9

If the bot process is still running, restart it after this script so in-memory state
reloads from the updated snapshot (otherwise it may still track the old position).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

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


def _pick_position(state: dict, needle: str) -> tuple[str, object] | None:
    needle_l = needle.strip().lower()
    if needle in state:
        return needle, state[needle]
    matches: list[tuple[str, object]] = []
    for arb_id, pos in state.items():
        if pos.asset.lower() == needle_l:
            matches.append((arb_id, pos))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"ERROR: ambiguous asset {needle!r} → {len(matches)} rows", file=sys.stderr)
        for a, p in matches:
            print(f"  {a}  {p.asset}", file=sys.stderr)
        return None
    return None


async def main() -> int:
    _load_env_file()
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if dry:
        os.environ["BOT_DRY_RUN"] = "true"

    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2

    settings = Settings.from_env()
    log = NDJsonLog(settings.state_file, f"close-one-{settings.instance_uuid}")
    state = load_snapshot(settings.snapshot_file)
    if not state:
        print("No open positions in snapshot.")
        return 0

    picked = _pick_position(state, args[0])
    if picked is None:
        print(f"No position matching {args[0]!r} in {settings.snapshot_file}", file=sys.stderr)
        return 1

    arb_id, pos = picked
    print(f"=== close_one: {pos.asset}  arb_id={arb_id}  "
          f"{pos.long_exchange}/{pos.short_exchange}  "
          f"{pos.long_base_symbol} / {pos.short_base_symbol} ===")

    async with VooiClient(settings.base_url, settings.bearer_token) as client:
        ok = await close_position(
            client,
            log,
            settings,
            pos,
            reason="manual_close_one",
        )

    if ok:
        save_snapshot(state, settings.snapshot_file)
        print(f"CLOSE_OK + snapshot saved ({settings.snapshot_file})")
        return 0
    print("CLOSE failed — snapshot not modified", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
