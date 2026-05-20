"""Frazzbot by Alex — delta-neutral funding-rate arbitrage bot via VOOI Perps API.

Phase 1 modules будут реализованы в порядке из docs/plan.md §3 Phase 1:
1. domain models (V6) — fundbot.api.models
2. two retry policies (C2) — fundbot.api.retry
3. intent/effect events caraсass (C8) — fundbot.position.snapshot
4. instance UUID (C6)
5. REST-first bootstrap + SSE watchdog (C3, C4)
6. single-instance startup gate (C6)
7. extended hard-reject (C5)
8. broker config refuse-start (C7)
9. clock-drift check (V8)
10. strategy/, execution/, position/, risk/, reporting/ модули
"""

__version__ = "0.1.0"
