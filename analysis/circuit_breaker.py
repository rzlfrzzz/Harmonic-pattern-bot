"""
Circuit breaker for per-symbol (or per symbol/timeframe) failure handling.

BUG FIX: previously, if a symbol kept erroring out (e.g. delisted mid-run,
persistent REST 4xx from MEXC, malformed kline payload, etc.) `scan_symbol`
and the backfill/gap-fill loops would retry it forever on every single
candle close, spamming the logs and wasting REST budget/rate-limit headroom
on a symbol that will never succeed again in this run.

This is a simple in-memory circuit breaker: after `max_failures` consecutive
failures for a key, it "opens" (skips further attempts) for `cooldown_seconds`.
A single success resets the failure count and closes the breaker immediately.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(self, max_failures: int = 5, cooldown_seconds: float = 3600):
        self.max_failures = max_failures
        self.cooldown_seconds = cooldown_seconds
        self._state: dict[str, _State] = {}

    def is_open(self, key: str) -> bool:
        """True if this key should currently be skipped."""
        state = self._state.get(key)
        if state is None or state.opened_at is None:
            return False
        if time.monotonic() - state.opened_at >= self.cooldown_seconds:
            # cooldown elapsed -> allow a fresh attempt (half-open); don't
            # reset the failure counter yet, only a real success does that.
            state.opened_at = None
            return False
        return True

    def record_success(self, key: str) -> None:
        self._state[key] = _State()

    def record_failure(self, key: str) -> None:
        state = self._state.setdefault(key, _State())
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.max_failures and state.opened_at is None:
            state.opened_at = time.monotonic()

    def status(self, key: str) -> dict:
        state = self._state.get(key, _State())
        return {
            "consecutive_failures": state.consecutive_failures,
            "open": self.is_open(key),
        }
