"""Strategy layer: scanner, history, filter, selector.

Phase 1 components:
- scanner.py — hourly cycle через AsyncIOScheduler (V3)
- history.py — /funding-strategies/spread-chart с in-memory cache (N5)
- filter.py — 12 фильтров ТЗ §5.2 + extended hard-reject (C5) + V4 caps + V13 cooldown
- selector.py — budget gate + slippage pre-check + dedup
"""
