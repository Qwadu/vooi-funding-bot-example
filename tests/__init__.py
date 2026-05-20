"""Test suite for Frazzbot by Alex.

Структура (Phase 1):
- tests/api/         — VooiClient, retry policies, SSE consumer
- tests/strategy/    — filter, selector, history windows
- tests/execution/   — state machine, stoploss math, orderhelpers
- tests/position/    — snapshot replay, intent/effect recovery
- tests/risk/        — limits, circuit breaker
- tests/reporting/   — event payload schemas
- tests/integration/ — live API (skipped by default, marker `integration`)

Acceptance scenarios — см. docs/plan.md §6 (criteria 1-21).
"""
