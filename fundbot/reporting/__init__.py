"""Reporting: NDJSON events + healthz/metrics HTTP server.

Phase 1 components:
- events.py — NDJSON в stdout, события из ТЗ §9.1 + V5 HOURLY_SCAN_SUMMARY + V9 expected_net_apr
- healthz.py — /healthz endpoint (учитывает SSE freshness, N3)
- metrics.py — /metrics Prometheus-style (P50/P99 histograms, N4)
"""
