"""NDJSON logger для probe-script.

- В stdout — для оперативного наблюдения.
- В файл `probe/runs/<run-id>/events.ndjson` — полный сырой лог для последующего анализа.
- JWT и любые секреты НИКОГДА не попадают в лог:
  - **по KEY**: `Authorization`, `token`, `jwt`, etc. → `***REDACTED***`
  - **по VALUE (§4.1 fix)**: regex post-pass убивает `Bearer <jwt>` и raw JWT
    в любом строковом значении, даже если ключ нейтральный.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_KEYS = frozenset(
    {
        "Authorization",
        "authorization",
        "VOOI_BEARER_TOKEN",
        "bearer_token",
        "token",
        "jwt",
        "X-Token",
    },
)

# §4.1 fix: regex post-pass на строковые значения.
# - "Bearer <token>" → "Bearer ***REDACTED***"
# - голый JWT (3 base64url-сегмента, разделённых точкой) → "***REDACTED_JWT***"
# - vooi_<hex> токены вида `vooi_e0a0fe18` → "***REDACTED***"
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
_VOOI_TOKEN_RE = re.compile(r"\bvooi_[A-Za-z0-9]{6,}\b")


def _redact_string(s: str) -> str:
    return _VOOI_TOKEN_RE.sub(
        "***REDACTED***",
        _JWT_RE.sub(
            "***REDACTED_JWT***",
            _BEARER_RE.sub("Bearer ***REDACTED***", s),
        ),
    )


def _redact(data: Any) -> Any:
    """Recursive replacement of sensitive values with `***REDACTED***`.

    Two-pass:
    1. По ключу — если ключ из `_SECRET_KEYS`, value полностью затирается.
    2. По значению — все строковые значения проходят regex для Bearer/JWT/vooi_.
    """
    if isinstance(data, dict):
        return {
            k: ("***REDACTED***" if k in _SECRET_KEYS else _redact(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact(item) for item in data]
    if isinstance(data, str):
        return _redact_string(data)
    return data


class ProbeLogger:
    def __init__(self, run_id: str | None = None, runs_dir: Path | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.runs_dir = runs_dir or Path("probe/runs")
        self.run_dir = self.runs_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_file = self.run_dir / "events.ndjson"

    def log(self, event: str, **data: Any) -> None:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **_redact(data),
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        with self.events_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def write_artifact(self, name: str, content: Any) -> Path:
        """Save raw artifact (JSON response, etc.) — for post-analysis."""
        path = self.run_dir / name
        with path.open("w", encoding="utf-8") as f:
            if isinstance(content, (dict, list)):
                json.dump(_redact(content), f, indent=2, default=str)
            else:
                f.write(_redact_string(str(content)))
        return path
