# Phase 0 — API probe-script

> Цель: эмпирически закрыть 11 вопросов к VOOI API ([`../docs/api-probe-results.md`](../docs/api-probe-results.md)) до начала Phase 1 кодинга. Самый критичный — Q7 (cross-margin liquidationPrice drift).

## Что делает

Каждый subcommand отвечает на один вопрос. Список — `python -m probe.probe --help`.

| Subcommand | Что | Writes на бирже? | Ожидаемая стоимость |
|---|---|---|---|
| `q1` | формат `longAsset/shortAsset` в spread-chart | нет | $0 |
| `q2` | SSE latency для `orderId` | да: 6 LIMIT alo далеко от рынка (3 на биржу × 2 биржи) | $0 (alo не fill, gas минимальный) |
| `q3` | `alo + stopLoss` conflict | да: 2 LIMIT alo с bracket | $0 (alo не fill) |
| `q4` | `clientOrderId` idempotency | да: 4 LIMIT alo (2 пары retry) | $0 |
| `q5` | Bearer на SSE | нет (read-only stream) | $0 |
| `q6` | `fundingInterval` per market | нет | $0 |
| **`q7`** | **cross-margin liq drift (КРИТИЧНО)** | **да: МАРКЕТНЫЕ позиции $20 BTC + $20 ETH** | **~$0.5 fees + gas, риск $40 на короткое время** |
| `q8` | 5xx behavior + double-order | да: 2 LIMIT alo | $0 |
| `q9` | POST без broker | да: 2 LIMIT alo (попытка) | $0 |
| `q10` | `/orders` filter by `clientOrderId` | нет | $0 |
| `q11` | SSE silent timeout (5 мин) | нет | $0 |

Composite:

| Subcommand | Что |
|---|---|
| `readonly` | пробежать только Q1, Q5, Q6, Q10, Q11 — без писем на биржу |
| `all` | пробежать все 11 (требует реального капитала на sub-account) |

## Подготовка

1. **Отдельный VOOI sub-account** для Frazzbot Alex. Не используем тот же что у Frazzbot Max (см. [`../AGENTS.md`](../AGENTS.md) — изоляция).

2. **Минимальные балансы для полного `all` прогона:**
   - HL: $50 perp balance (Q7 берёт $40 одновременно).
   - Lighter: $50 perp balance.
   - Aster: пока не используется (см. `BOT_ASTER_ACTIVATION_DATE` в `.env`).

3. **JWT** генерируется через VOOI UI (`POST /user/tokens` с Ed25519 подписью или через UI кнопку «Issue API token»).

4. **Brokers** — пока для probe **не нужны**, broker config мы **проверяем** в Q9. Но для production (Phase 1+) `BOT_BROKER_*_ID` будут обязательны.

## Запуск

```bash
cd <path-to-repo>
cp .env.example .env
# отредактировать .env: VOOI_BEARER_TOKEN, опционально BOT_TARGET_EXCHANGES

# Установить deps (один раз)
uv sync

# Read-only batch (можно пускать в любом момент, $0)
uv run python -m probe.probe readonly

# Q7 critical — отдельно (готов потерять до $1 на fees)
uv run python -m probe.probe q7

# Полный батч (требует реального капитала)
uv run python -m probe.probe all
```

## Где искать результаты

```
probe/runs/<run-id>/
├── events.ndjson           # полный stream событий (без JWT)
├── q1_spread_chart_format.json
├── q2_sse_latency.json
├── q3_alo_bracket.json
├── q4_clientorderid_idempotent.json
├── q5_bearer_sse.json
├── q6_funding_interval.json
├── q7_cross_margin_drift.json   # КРИТИЧНО — анализ для C1
├── q8_5xx_behavior.json
├── q9_without_broker.json
├── q10_orderid_filter.json
└── q11_sse_silent.json
```

Эти файлы — **не commit**'им (они в `.gitignore`). Вместо этого:

1. Анализируем содержимое.
2. Заполняем агрегированные ответы в [`../docs/api-probe-results.md`](../docs/api-probe-results.md) — этот файл commit'им как артефакт фазы.

## Безопасность

- **Все LIMIT-ордера** ставятся ДАЛЕКО от рынка (+/− 50% от mid) → maker-only, гарантированно не fill.
- **Per-call notional cap:** `PROBE_MAX_NOTIONAL_USD` (default $20).
- **Per-run total budget:** `--budget-usd` (default $200).
- **JWT** не попадает в логи (фильтр в `probe/log.py`).
- **Cleanup:** каждый `q*` пытается cancel свои тестовые ордера в конце.

## Q7 — что делать если что-то пошло не так

1. Если probe упал между «open BTC» и «open ETH» → у тебя одна открытая cross-позиция. Закрой её через VOOI UI (`Reduce-only Market`).
2. Если упал после обоих open, до cleanup → две cross-позиции. Закрой обе через UI.
3. Если cleanup не успел — `position.size > 0` будет видно в `/exchange/positions`. Бот при следующем запуске Phase 1 distinguishes orphan позиции и cancel'ит их при reconcile, но в Phase 0 руками легче.

## После завершения probe

1. Заполнить TBD в [`../docs/api-probe-results.md`](../docs/api-probe-results.md).
2. Принять решения «Решение для кода» в каждом разделе.
3. Закомментировать/закрыть [`../docs/spec-cto2-review.md`](../docs/spec-cto2-review.md) → C1, C2, C7, OQ §14.1, §14.4, §14.5, §14.7, §14.9.
4. Перейти в Phase 1 кодинг (см. [`../docs/plan.md §3 Phase 1`](../docs/plan.md) — 10 шагов в строгом порядке).
