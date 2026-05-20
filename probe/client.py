"""Минимальный VOOI HTTP-клиент для probe.

Намеренно НЕ использует tenacity-retry — мы не хотим скрывать поведение API.
Каждый вызов = один HTTP request, raw response доступен для анализа.

Этот клиент НЕ для production — production-клиент будет в fundbot/api/client.py
с two retry policies (C2) и domain-моделями (V6).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from httpx_sse import EventSource, aconnect_sse


class ProbeClient:
    """Async HTTP client для VOOI Perps API. Только probe нужды."""

    def __init__(self, base_url: str, bearer_token: str, timeout_sec: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }
        self._timeout = httpx.Timeout(timeout_sec, connect=10.0)
        self._sse_timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ProbeClient:
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=self._timeout,
            http2=True,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ProbeClient not entered (use async context manager)")
        return self._client

    # -- Generic GET / POST / DELETE with raw access -------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        t0 = time.monotonic()
        resp = await self.client.get(path, params=params)
        elapsed = (time.monotonic() - t0) * 1000
        resp.extensions["latency_ms"] = elapsed
        return resp

    async def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        t0 = time.monotonic()
        resp = await self.client.post(path, json=body)
        elapsed = (time.monotonic() - t0) * 1000
        resp.extensions["latency_ms"] = elapsed
        return resp

    async def delete(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        t0 = time.monotonic()
        if body is None:
            resp = await self.client.delete(path)
        else:
            resp = await self.client.request("DELETE", path, json=body)
        elapsed = (time.monotonic() - t0) * 1000
        resp.extensions["latency_ms"] = elapsed
        return resp

    # -- SSE -----------------------------------------------------------------

    @asynccontextmanager
    async def sse(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        use_bearer: bool = True,
        token_query: str | None = None,
    ) -> AsyncIterator[EventSource]:
        """Open SSE connection.

        :param use_bearer: если True — Bearer header (Q5 проверка).
        :param token_query: если задан — добавляет ?token=<...>
            (для случая когда Bearer не работает).
        """
        headers = dict(self._headers) if use_bearer else {}
        # Дёрнем заведомо корректный header — если use_bearer=False, не передаём Authorization вовсе
        if not use_bearer and "Authorization" in headers:
            del headers["Authorization"]

        full_params = dict(params or {})
        if token_query:
            full_params["token"] = token_query

        async with httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=self._sse_timeout,
        ) as client, aconnect_sse(
            client,
            "GET",
            path,
            params=full_params,
        ) as evtsource:
            yield evtsource


def summarize_response(resp: httpx.Response) -> dict[str, Any]:
    """Compact summary for NDJSON log (без полного body, без secrets)."""
    body: Any
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:500]

    if isinstance(body, list):
        body_summary = {
            "type": "list",
            "len": len(body),
            "sample": body[0] if body else None,
        }
    elif isinstance(body, dict):
        # Только верхнеуровневые ключи + sample значений
        body_summary = {"type": "object", "keys": list(body.keys()), "preview": body}
    else:
        body_summary = {"type": "text", "value": str(body)[:300]}

    return {
        "status": resp.status_code,
        "latency_ms": resp.extensions.get("latency_ms"),
        "headers": {k: v for k, v in resp.headers.items() if k.lower() != "set-cookie"},
        "body": body_summary,
    }
