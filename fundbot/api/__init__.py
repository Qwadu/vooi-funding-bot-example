"""VOOI Perps API client.

Phase 1 components:
- models.py — domain слой с Decimal-coercion (V6)
- models_gen.py — generated DTO от datamodel-code-generator (input only)
- retry.py — две retry policies retry_read / retry_write (C2)
- client.py — VooiClient REST методы
- sse.py — SSE consumer с REST-first bootstrap (C3) и watchdog 60s (C4)
- errors.py — typed exceptions (WriteFailedError, etc.)
"""
