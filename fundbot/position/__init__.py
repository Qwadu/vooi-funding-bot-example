"""Position management.

Phase 1 components:
- manager.py — реестр активных арб-позиций + hourly update (ТЗ §8.1)
- valuator.py — суммирование PnL+fundingFee по парам
- snapshot.py — INTENT/EFFECT events append-only с rotation (C8)
"""
