"""Order execution: state machine, stop-loss, helpers.

Phase 1 components:
- opener.py — state machine ТЗ §6 + intent-events (C8) + per-asset mutex (V4)
- closer.py — batch close + exit-defer near settlement (V15)
- stoploss.py — SL расчёт через quotes(isolated) (C1) + min absolute distance (4.D.3)
- orderhelpers.py — limit-pricing, тиксайз, нормализация
"""
