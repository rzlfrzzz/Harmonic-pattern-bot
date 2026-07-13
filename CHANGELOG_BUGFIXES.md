# Bugfix changelog

This documents the 7 bugs reported and exactly what changed to fix each one.
File/line references are to the fixed codebase.

## 1. No validation that D is still realistic vs. current price (stale signals)

**Fix:** `analysis/entry_calculator.py::is_entry_still_actionable()` + wiring in
`main.py::scan_symbol()`.

- Every match is now checked against the latest closed candle's close price
  before it's ever inserted:
  - `candles_since_d` (closed candles after D) is compared to the new
    `pattern.max_candles_since_d` config — patterns whose D is too old are
    dropped (`main.py` ~"Skipping stale...").
  - `is_entry_still_actionable()` rejects the signal if price has already
    blown through the stop loss (`sl_already_breached`) or has run more than
    `pattern.max_entry_deviation_pct` beyond the entry zone edge
    (`entry_too_far`).

## 2. No invalidation / outcome-tracking mechanism (status column never updated)

**Fix:** new `analysis/pattern_tracker.py`, new `db.get_open_patterns` /
`db.update_pattern_status` / `db.mark_pattern_entered` /
`db.get_latest_closed_candle`, new `main.py::pattern_monitor_loop()`.

- A new periodic loop (`scan.pattern_monitor_interval_seconds`, default 60s)
  re-checks every non-terminal pattern against the latest closed candle and
  transitions `status`:
  `confirmed -> entered -> tp1_hit -> tp2_hit -> tp3_hit` or
  `confirmed/entered -> sl_hit`, or `confirmed -> invalidated` if price runs to
  the stop level *before* ever trading inside the entry zone (distinguishing
  "never filled" from "filled then stopped out").
- New `entered` boolean and `closed_at` timestamp columns support this
  (see `db/schema.sql` and `db/migrations/001_risk_and_tracking.sql` for
  upgrading an existing database).

## 3. Minimal risk management (no sizing, no R:R check, static SL/entry %)

**Fix:** `analysis/entry_calculator.py` additions + new `risk` config section.

- `calculate_trade_levels()` now accepts an optional ATR value and can size
  the SL buffer (`sl_atr_multiplier`) and/or entry zone half-width
  (`entry_zone_atr_multiplier`) as ATR multiples instead of a flat % of
  price — toggle via `pattern.use_atr_sl` / `pattern.use_atr_entry_zone`.
  ATR itself comes from the existing `atr_pivot_swings` machinery, now
  exposed as `analysis/swing_detector.py::latest_atr()`.
- `risk_reward_ratio` (TP1-based) is always computed; `meets_min_risk_reward()`
  gates signals below `pattern.min_risk_reward`.
- `calculate_position_size()` implements fixed-fractional sizing from a new
  `risk` config section (`account_equity_usdt`, `risk_per_trade_pct`,
  `max_leverage`); the resulting qty/risk/leverage are stored on the pattern
  row and shown in the Telegram message.

## 4. Race condition between WS loop and gap-fill housekeeping

**Fix:** `data/candle_cache.py` rewritten around a shared, locked
`_ingest_candles()` routine.

- One `asyncio.Lock` per `(symbol, timeframe)` now serializes every write —
  both `handle_ws_update` and `refill_gaps` acquire it before touching that
  key's candles/rollover state.
- Rollover ("bucket just closed") detection lives in a single shared method
  used by both call sites, so there's exactly one place that decides to fire
  `on_close`, eliminating the double-trigger scenario. `refill_gaps` also now
  *can* fire `on_close` itself (previously it never did), which additionally
  closes the original gap the function exists for — a rollover the WS
  genuinely missed will now still be detected.

## 5. `_last_open_time` is memory-only; lost on restart

**Fix:** `data/candle_cache.py::_get_baseline_open_time()`.

- Before assuming "no previous candle" (which suppresses the very first
  close detection), the cache now falls back to
  `db.get_last_candle_time()` to seed a real baseline from persisted data.
  This also covers symbols where `backfill_all` failed to seed the
  in-memory cache for any reason.

## 6. Universe refresh never touches the live WS subscription

**Fix:** `data/mexc_client.py::update_symbols()` + `main.py::refresh_universe()`.

- `MexcClient` now tracks a *live, mutable* desired-subscription state
  (`_desired_symbols` / `_desired_timeframes`) instead of a frozen list
  captured once at `watch_klines()` call time. Both the initial connect and
  every reconnect read from this live state.
- `update_symbols()` diffs the new desired set against what's currently
  subscribed and sends incremental `sub.kline` / `unsub.kline` messages on
  the live connection immediately (falls back silently to "next connect"
  if not currently connected).
- `main.py`'s hourly `refresh_universe()` now calls `update_symbols()` right
  after refreshing `coins.is_active`, so a symbol dropping out of the top-N
  actually stops being streamed/scanned instead of just being flagged
  inactive in the DB while still being watched forever.

## 7. No dead-letter / circuit breaker for persistently failing symbols

**Fix:** new `analysis/circuit_breaker.py`, wired into
`main.py::on_candle_closed()` (scan path) and
`main.py::_refill_gaps_with_breaker()` (gap-fill path).

- A simple in-memory `CircuitBreaker` tracks consecutive failures per
  `symbol:timeframe` key. After `circuit_breaker.max_consecutive_failures`
  in a row, the key is skipped for `circuit_breaker.cooldown_minutes` before
  being retried (half-open), rather than being retried unconditionally on
  every candle close / gap-fill pass forever. A single success resets the
  counter immediately.

---

## Config changes

New sections/keys in `config.example.yaml` (all have safe defaults, so an
existing `config.yaml` written before this fix still loads — see
`config/settings.py::load_config()`):

- `scan.pattern_monitor_interval_seconds`
- `pattern.use_atr_sl`, `pattern.sl_atr_multiplier`,
  `pattern.use_atr_entry_zone`, `pattern.entry_zone_atr_multiplier`,
  `pattern.min_risk_reward`, `pattern.max_candles_since_d`,
  `pattern.max_entry_deviation_pct`
- `risk.account_equity_usdt`, `risk.risk_per_trade_pct`, `risk.max_leverage`
- `circuit_breaker.max_consecutive_failures`, `circuit_breaker.cooldown_minutes`

## Database changes

Run `db/migrations/001_risk_and_tracking.sql` against an existing database
(or just re-run the updated `db/schema.sql` on a fresh one). New columns on
`detected_patterns`: `atr_at_signal`, `risk_reward_ratio`, `risk_amount`,
`position_qty`, `position_leverage`, `entered`, `closed_at`.

## Tests

`tests/test_patterns.py` gained 12 new tests covering ATR-adaptive SL,
the risk/reward gate, position sizing (capped and uncapped), the
entry-actionability check, the circuit breaker, and pattern-outcome
transitions (invalidated vs. sl_hit vs. tp1_hit). All 17 tests (5 original +
12 new) pass.
