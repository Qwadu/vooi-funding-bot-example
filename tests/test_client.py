"""Unit-тесты на probe.client.summarize_response с моками httpx через respx.

Не открываем реальные сокеты — все ответы имитируются.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from probe.client import ProbeClient, summarize_response


class TestSummarizeResponse:
    def test_dict_body_summary_keeps_keys(self) -> None:
        resp = httpx.Response(200, json={"a": 1, "b": "two"})
        resp.extensions["latency_ms"] = 12.5
        s = summarize_response(resp)
        assert s["status"] == 200
        assert s["latency_ms"] == 12.5
        assert s["body"]["type"] == "object"
        assert set(s["body"]["keys"]) == {"a", "b"}

    def test_list_body_summary_keeps_first_sample(self) -> None:
        resp = httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        resp.extensions["latency_ms"] = 0.0
        s = summarize_response(resp)
        assert s["body"]["type"] == "list"
        assert s["body"]["len"] == 2
        assert s["body"]["sample"] == {"id": 1}

    def test_empty_list_body(self) -> None:
        resp = httpx.Response(200, json=[])
        resp.extensions["latency_ms"] = 0.0
        s = summarize_response(resp)
        assert s["body"]["len"] == 0
        assert s["body"]["sample"] is None

    def test_text_body_truncated(self) -> None:
        resp = httpx.Response(500, text="x" * 1000)
        resp.extensions["latency_ms"] = 0.0
        s = summarize_response(resp)
        # text-fallback path uses [:500] in body parse, then [:300] in summary
        assert s["body"]["type"] == "text"
        assert len(s["body"]["value"]) <= 300

    def test_summary_strips_set_cookie_header(self) -> None:
        resp = httpx.Response(
            200, json={}, headers={"Set-Cookie": "secret=abc", "X-Other": "ok"}
        )
        resp.extensions["latency_ms"] = 0.0
        s = summarize_response(resp)
        assert "set-cookie" not in {k.lower() for k in s["headers"]}
        assert s["headers"]["x-other"] == "ok"


@pytest.mark.asyncio
class TestProbeClient:
    @respx.mock
    async def test_get_records_latency_ms_in_extensions(self) -> None:
        route = respx.get("https://example.test/exchange/markets").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with ProbeClient("https://example.test", "fake-token") as c:
            resp = await c.get("/exchange/markets")
        assert route.called
        assert resp.status_code == 200
        # Latency должен быть >= 0
        assert resp.extensions["latency_ms"] >= 0

    @respx.mock
    async def test_post_sends_authorization_header(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True})

        respx.post("https://example.test/exchange/orders").mock(side_effect=handler)
        async with ProbeClient("https://example.test", "TOKEN_XYZ") as c:
            r = await c.post("/exchange/orders", body={"a": 1})
        assert r.status_code == 200
        assert captured["auth"] == "Bearer TOKEN_XYZ"
        assert json.loads(captured["body"]) == {"a": 1}

    @respx.mock
    async def test_delete_with_body_uses_request_method(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True})

        respx.route(method="DELETE", url="https://example.test/exchange/orders").mock(
            side_effect=handler
        )
        async with ProbeClient("https://example.test", "T") as c:
            await c.delete("/exchange/orders", body={"orderId": "x"})
        assert captured["method"] == "DELETE"
        assert json.loads(captured["body"]) == {"orderId": "x"}

    async def test_using_client_outside_context_raises(self) -> None:
        c = ProbeClient("https://example.test", "T")
        with pytest.raises(RuntimeError, match="not entered"):
            _ = c.client
