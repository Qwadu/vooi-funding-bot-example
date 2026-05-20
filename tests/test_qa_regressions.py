"""Regression-тесты на конкретные баги, найденные QA в Phase 0 probe-script.

Каждый тест ссылается на номер бага из `docs/qa-report.md`. После ФИКСА
теста — он verify-fix (positive assertion). Если регрессия вернётся
(кто-то откатит фикс), тест упадёт.

Контракт именования: `test_bugNN_<short>` — стабилен через git history,
чтобы трекать жизненный цикл бага.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from httpx_sse import aconnect_sse
from probe.log import ProbeLogger, _redact
from probe.questions import (
    _SSE_AUTH_REJECT_EVENTS,
    _extract_orders_list,
    q1_spread_chart_format,
)
from probe.safety import (
    ProbeSafetyError,
    quantize_size_by_base_decimals,
)

# =============================================================================
# BUG-01 — Q1 даты: rolling window, не hardcoded
# =============================================================================


def test_bug01_q1_uses_rolling_date_window() -> None:
    """Q1 не должен содержать hardcoded дат вида 2026-04-25/2026-04-30.
    Должен использовать `date.today() - timedelta(days=N)`.
    """
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    assert '"fromDate": "2026-' not in src, "hardcoded fromDate detected"
    assert '"toDate": "2026-' not in src, "hardcoded toDate detected"
    assert "date.today()" in src, "expected date.today() rolling window"
    assert "timedelta" in src, "expected timedelta для rolling window"


# =============================================================================
# BUG-02 — Q5 SSE Bearer: error event = auth-rejection, НЕ success
# =============================================================================


def test_bug02_q5_classifies_error_event_as_rejection() -> None:
    """Q5 при `event.event in _SSE_AUTH_REJECT_EVENTS` → bearer_works=False.

    `error` в `_SSE_AUTH_REJECT_EVENTS` — общий контракт для Q5/Q11.
    """
    assert "error" in _SSE_AUTH_REJECT_EVENTS

    src = Path("probe/questions.py").read_text(encoding="utf-8")
    q5_start = src.find("async def q5_bearer_sse")
    q5_end = src.find("async def q6_funding_interval", q5_start)
    assert q5_start > 0 and q5_end > q5_start
    q5 = src[q5_start:q5_end]

    assert "_SSE_AUTH_REJECT_EVENTS" in q5, "Q5 must consult auth-reject set"
    # bearer_works = False должен присутствовать в ветке rejection.
    assert "bearer_works = False" in q5
    # И на ?token= тоже та же фильтрация.
    assert q5.count("_SSE_AUTH_REJECT_EVENTS") >= 2


# =============================================================================
# BUG-03 — PROBE_RUNS_DIR env читается probe.probe
# =============================================================================


def test_bug03_probe_runs_dir_env_is_read(tmp_path: Path) -> None:
    """`probe.probe._amain` должен читать `PROBE_RUNS_DIR` и передавать в ProbeLogger.

    Сам ProbeLogger принимает `runs_dir` параметром — это уже правильно.
    Здесь проверяем что в исходнике probe/probe.py есть чтение env.
    """
    # ProbeLogger без runs_dir → дефолт.
    log = ProbeLogger(run_id="r")
    assert log.runs_dir == Path("probe/runs")

    # ProbeLogger с runs_dir → используется он.
    custom = tmp_path / "custom_runs"
    log2 = ProbeLogger(run_id="r2", runs_dir=custom)
    assert log2.runs_dir == custom

    # И главное — probe.probe.py читает env-переменную.
    src = Path("probe/probe.py").read_text(encoding="utf-8")
    assert 'PROBE_RUNS_DIR' in src
    assert 'ProbeLogger(runs_dir=runs_dir)' in src or 'runs_dir=runs_dir' in src


def test_bug03_env_var_not_silently_ignored(tmp_path: Path) -> None:
    """При установленной `PROBE_RUNS_DIR` env, `_amain` создаст артефакты в этом
    каталоге (smoke через ProbeLogger напрямую — _amain тестируется в integration).
    """
    custom = tmp_path / "envdir"
    with patch.dict(os.environ, {"PROBE_RUNS_DIR": str(custom)}):
        # Воспроизводим логику probe.probe._amain.
        env_dir = Path(os.environ["PROBE_RUNS_DIR"])
        log = ProbeLogger(runs_dir=env_dir)
        assert log.runs_dir == custom
        assert custom.exists()


# =============================================================================
# BUG-04 — size quantize по baseDecimals + ROUND_DOWN
# =============================================================================


def test_bug04_quantize_uses_base_decimals_round_down() -> None:
    """`quantize_size_by_base_decimals` корректно квантует с ROUND_DOWN."""
    # baseDecimals=4, $5 / $76421 = 0.0000654 → должно стать 0.0000 → ОТКАЗ
    # (ниже step), потому что ROUND_DOWN не возвращает >= step.
    with pytest.raises(ProbeSafetyError, match="too small"):
        quantize_size_by_base_decimals(Decimal("5"), Decimal("76421"), base_decimals=4)


def test_bug04_quantize_rounds_down_not_up() -> None:
    """ROUND_DOWN: при достаточном notional возвращает значение, которое
    при умножении на mid НЕ превышает заявленный notional.
    """
    notional = Decimal("100")
    mid = Decimal("76421")
    qty = quantize_size_by_base_decimals(notional, mid, base_decimals=4)
    effective = qty * mid
    assert effective <= notional, (
        f"effective notional {effective} превышает заявленный cap {notional}"
    )


def test_bug04_quantize_aster_basedecimals_3() -> None:
    """Aster `baseDecimals=3` (step=0.001). $100 / $76421 = 0.00131 → 0.001."""
    qty = quantize_size_by_base_decimals(Decimal("100"), Decimal("76421"), base_decimals=3)
    assert qty == Decimal("0.001")


def test_bug04_no_hardcoded_quantize_in_questions() -> None:
    """В probe/questions.py не должно остаться hardcoded `quantize(Decimal("0.0001"))`.

    Все Q-функции теперь должны использовать `quantize_size_by_base_decimals`.
    """
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    assert 'quantize(Decimal("0.0001"))' not in src
    assert "quantize_size_by_base_decimals" in src


# =============================================================================
# BUG-05 — Q10 fake-COID + items/data unification
# =============================================================================


def test_bug05_q10_uses_fake_coid_when_no_history() -> None:
    """Q10 должна тестировать filter даже на пустой истории (fake-COID)."""
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    # Старый pattern удалён.
    assert '"no_sample_to_test"' not in src
    # Новый pattern: fake-COID generation.
    q10_start = src.find("async def q10_orderid_filter")
    q10_end = src.find("# ----", q10_start + 100)
    q10 = src[q10_start:q10_end] if q10_end > q10_start else src[q10_start:]
    assert "fake_coid" in q10 or "nonexistent" in q10


def test_bug05_extract_orders_list_handles_items_key() -> None:
    """`_extract_orders_list` принимает `{items: [...]}` (новый формат VOOI)."""
    body = {"cursor": "abc", "items": [{"id": "1"}, {"id": "2"}]}
    out = _extract_orders_list(body)
    assert len(out) == 2
    assert out[0]["id"] == "1"


def test_bug05_extract_orders_list_handles_data_key_legacy() -> None:
    """Backwards-compat: `{data: [...]}` тоже поддерживается."""
    body = {"data": [{"id": "1"}]}
    out = _extract_orders_list(body)
    assert len(out) == 1


def test_bug05_extract_orders_list_handles_raw_list() -> None:
    """Если ответ — голый list, тоже работает."""
    out = _extract_orders_list([{"id": "1"}])
    assert len(out) == 1


def test_bug05_extract_orders_list_unknown_shape_returns_empty() -> None:
    """Незнакомый shape → пустой список (не падать)."""
    assert _extract_orders_list({"unknown_key": [1]}) == []
    assert _extract_orders_list(None) == []
    assert _extract_orders_list("text") == []


# =============================================================================
# BUG-06 — Q11 различает error events от data events
# =============================================================================


def test_bug06_q11_distinguishes_error_from_data_events() -> None:
    """Q11 должен иметь раздельные счётчики events vs error_events,
    и interpretation `auth_rejected_by_server` для случая когда есть только error.
    """
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    q11_start = src.find("async def q11_sse_silent")
    assert q11_start > 0
    q11 = src[q11_start:]
    assert "error_events" in q11
    assert "auth_rejected_by_server" in q11
    assert "_SSE_AUTH_REJECT_EVENTS" in q11


# =============================================================================
# BUG-07 — Q4 budget reserve вызывается ДВАЖДЫ
# =============================================================================


def test_bug07_q4_reserves_budget_per_post() -> None:
    """Q4 делает 2 POST с одним и тем же clientOrderId, и `budget.reserve`
    вызывается ПЕРЕД КАЖДЫМ POST (pessimistic, на случай non-idempotent биржи).
    """
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    q4_start = src.find("async def q4_clientorderid_idempotent")
    q4_end = src.find("# ---", q4_start + 100)
    q4 = src[q4_start:q4_end]
    reserves = q4.count("budget.reserve(")
    posts = q4.count('await client.post("/exchange/orders"')
    assert posts == 2, f"expected 2 POSTs, got {posts}"
    assert reserves >= 2, (
        f"BUG-07: expected ≥2 budget.reserve() (one per POST), got {reserves}"
    )


# =============================================================================
# BUG-08 — mypy --strict в _get_market_info чист (cast)
# =============================================================================


def test_bug08_get_market_info_uses_cast() -> None:
    """`_get_market_info` использует `cast()` для возвратов из `markets[i]` (Any-source)."""
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    fn_start = src.find("async def _get_market_info")
    fn_end = src.find("def _extract_decimals", fn_start)
    fn = src[fn_start:fn_end] if fn_end > fn_start else src[fn_start : fn_start + 2000]
    assert 'cast("dict[str, Any]"' in fn or "cast('dict[str, Any]'" in fn


# =============================================================================
# §4.1 — _redact() маскирует Bearer/JWT в строковых значениях
# =============================================================================


def test_redact_value_masks_bearer_in_string_value() -> None:
    """§4.1: regex post-pass убирает `Bearer <token>` даже если ключ нейтральный."""
    out = _redact({"raw_response": "Authorization: Bearer eyJ.SECRET"})
    assert "SECRET" not in out["raw_response"]
    assert "***REDACTED***" in out["raw_response"]


def test_redact_value_masks_jwt_pattern() -> None:
    jwt = "eyJabc.def123.ghi456"
    out = _redact({"note": f"observed {jwt} in stream"})
    assert jwt not in out["note"]
    assert "REDACTED" in out["note"]


def test_redact_value_masks_vooi_token() -> None:
    out = _redact({"context": "using vooi_e0a0fe18 for sub-account"})
    assert "vooi_e0a0fe18" not in out["context"]
    assert "REDACTED" in out["context"]


# =============================================================================
# §4.3 — assert_alo принимает реальный timeInForce, а не литерал
# =============================================================================


def test_4_3_assert_alo_called_with_real_tif() -> None:
    """В каждой Q-write функции вызов `assert_alo(order_body["timeInForce"])`
    (а не `assert_alo("alo")` с литералом).
    """
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    # Старый pattern удалён.
    assert 'assert_alo("alo")' not in src
    # Новый pattern: aргумент = order_body["timeInForce"].
    pattern = re.compile(r'assert_alo\(order_body\[["\']timeInForce["\']\]\)')
    matches = pattern.findall(src)
    # Q2/Q3/Q4/Q8/Q9 = 5 write-функций
    assert len(matches) >= 5, (
        f"expected ≥5 assert_alo(order_body['timeInForce']) calls, got {len(matches)}"
    )


# =============================================================================
# §4.4 — Q2 watch_sse без closure capture (выделенная функция)
# =============================================================================


def test_4_4_q2_watch_sse_is_standalone_function() -> None:
    """Q2 не использует closure-capture в loop — отдельная функция
    `_watch_sse_for_coid` принимает client_order_id/exchange как аргументы.
    """
    src = Path("probe/questions.py").read_text(encoding="utf-8")
    assert "async def _watch_sse_for_coid" in src
    # И внутри Q2 нет nested `async def watch_sse():` (closure).
    q2_start = src.find("async def q2_sse_latency")
    q2_end = src.find("async def q3_alo_bracket", q2_start)
    q2 = src[q2_start:q2_end]
    assert "async def watch_sse(" not in q2


# =============================================================================
# §4.5 — Q1 принимает long/short exchange как параметры + CLI флаги
# =============================================================================


def test_4_5_q1_accepts_exchange_kwargs() -> None:
    """`q1_spread_chart_format` имеет `long_exchange` и `short_exchange` параметры."""
    params = inspect.signature(q1_spread_chart_format).parameters
    assert "long_exchange" in params
    assert "short_exchange" in params


def test_4_5_probe_cli_has_exchange_flags() -> None:
    src = Path("probe/probe.py").read_text(encoding="utf-8")
    assert "--long-exchange" in src
    assert "--short-exchange" in src


# =============================================================================
# Live integration smoke — Bearer на SSE на bot-5 token возвращает error
# =============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("VOOI_BEARER_TOKEN"), reason="needs live bearer token",
)
@pytest.mark.asyncio
async def test_integration_bearer_sse_returns_error_event() -> None:
    """LIVE: подтвердить что Bearer-only на /exchange/updates возвращает event:error.

    На дату QA-прогона поведение: SSE открывается, первый event = `error` с
    телом `Validation failed`. Это НЕ keep-alive, это auth-rejection.

    Если этот тест поломается (стало возвращаться `data` event первым) —
    отлично, можно удалить ?token= reconnect-loop в Phase 1. Но пока
    держим как safety net.
    """
    base_url = os.environ.get("VOOI_API_BASE_URL", "https://perps-api.vooi.io")
    token = os.environ["VOOI_BEARER_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
    ) as cl, aconnect_sse(
        cl, "GET", "/exchange/updates", params={"exchanges": "lighter,aster"},
    ) as evt:
        event = await asyncio.wait_for(evt.aiter_sse().__anext__(), timeout=15.0)
        assert event.event == "error", f"expected error event, got {event.event}"
