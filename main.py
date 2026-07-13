"""
Harmonic Pattern Telegram Bot — main entrypoint.

Run with:
    python main.py

Flow:
  1. Startup: connect DB, refresh top-30 coin universe, backfill candles.
  2. Launch four concurrent loops:
       a) WebSocket listener -> on every real candle close, re-check that
          symbol/timeframe for harmonic patterns (rate-limited to once
          per `rescan_interval_hours` per symbol/timeframe via scan_log).
       b) Periodic housekeeping loop -> refresh coin universe (and push
          that change to the live WS subscription), gap-fill candles.
       c) Pattern monitor loop -> re-checks every still-open detected
          pattern against the latest price to update its status
          (entered / tp1_hit / tp2_hit / tp3_hit / sl_hit / invalidated).
       d) Notification flush loop -> sends pending Telegram notifications.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
from pathlib import Path

from config.settings import load_config, Config
from db.database import Database
from data.mexc_client import MexcClient
from data.candle_cache import CandleCache
from analysis.swing_detector import detect_swings, latest_atr
from analysis.pattern_validator import scan_for_patterns
from analysis.entry_calculator import (
    calculate_trade_levels,
    calculate_position_size,
    is_entry_still_actionable,
    meets_min_risk_reward,
)
from analysis.pattern_tracker import check_open_patterns
from analysis.circuit_breaker import CircuitBreaker
from notifier.telegram_bot import TelegramNotifier

logger = logging.getLogger("harmonic_bot")


def _naive_utc(dt):
    """Strip tzinfo (assumes UTC) so datetimes from different sources compare safely."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def setup_logging(cfg: Config):
    log_path = Path(cfg.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=cfg.logging.max_bytes, backupCount=cfg.logging.backup_count
    )
    stream = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(fmt)
    stream.setFormatter(fmt)

    root = logging.getLogger("harmonic_bot")
    root.setLevel(cfg.logging.level)
    root.addHandler(handler)
    root.addHandler(stream)


class HarmonicBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.supabase.db_dsn)
        self.mexc = MexcClient(
            cfg.mexc.rest_base_url, cfg.mexc.ws_url,
            request_delay=cfg.mexc.request_delay_seconds,
            max_retries=cfg.mexc.max_retries,
        )
        self.cache: CandleCache | None = None
        self.notifier = TelegramNotifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        self._stop = asyncio.Event()

        # BUG FIX: dead-letter / circuit breaker for symbols that keep
        # failing (e.g. delisted mid-run). Without this, a broken symbol
        # would be retried forever on every candle close / gap-fill pass.
        self._scan_breaker = CircuitBreaker(
            max_failures=cfg.circuit_breaker.max_consecutive_failures,
            cooldown_seconds=cfg.circuit_breaker.cooldown_minutes * 60,
        )
        self._backfill_breaker = CircuitBreaker(
            max_failures=cfg.circuit_breaker.max_consecutive_failures,
            cooldown_seconds=cfg.circuit_breaker.cooldown_minutes * 60,
        )

    async def start(self):
        await self.db.connect()

        async with self.mexc:
            self.cache = CandleCache(self.mexc, self.db, self.cfg.scan.timeframes, self.cfg.scan.candles_per_fetch)

            await self.refresh_universe(initial=True)
            symbols = await self.db.get_active_symbols()
            logger.info(f"Tracking {len(symbols)} symbols: {symbols}")

            logger.info("Backfilling historical candles (this can take a while on first run)...")
            await self.cache.backfill_all(symbols)

            await self.notifier.send_text(
                f"✅ Harmonic Pattern Bot started. Tracking {len(symbols)} symbols "
                f"across {', '.join(self.cfg.scan.timeframes)}."
            )

            tasks = [
                asyncio.create_task(self.ws_loop(symbols)),
                asyncio.create_task(self.housekeeping_loop()),
                asyncio.create_task(self.notification_flush_loop()),
                asyncio.create_task(self.pattern_monitor_loop()),
            ]
            await self._stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        await self.db.close()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    async def refresh_universe(self, initial: bool = False):
        try:
            coins = await self.mexc.get_top_volume_contracts(self.cfg.scan.top_n_coins)
            await self.db.upsert_coins(coins)
            logger.info(f"Refreshed coin universe: {len(coins)} symbols")

            # BUG FIX: the universe refresh used to only flip `is_active`
            # in the `coins` table -- it never touched the live WS
            # subscription, which was captured once at startup and never
            # revisited. A symbol dropping out of the top-N would keep
            # being streamed/scanned forever. We now push every refresh
            # straight to the live WebSocket subscription.
            if not initial:
                symbols = await self.db.get_active_symbols()
                await self.mexc.update_symbols(symbols, self.cfg.scan.timeframes)
        except Exception as e:
            logger.error(f"Failed to refresh coin universe: {e}")

    async def housekeeping_loop(self):
        """Every hour: refresh the top-30 universe (+ WS subscription) and gap-fill candles."""
        while True:
            try:
                await asyncio.sleep(3600)
                await self.refresh_universe()
                symbols = await self.db.get_active_symbols()
                await self._refill_gaps_with_breaker(symbols)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Housekeeping loop error: {e}")

    async def _refill_gaps_with_breaker(self, symbols: list[str]):
        """refill_gaps, but skipping symbols whose circuit breaker is open."""
        healthy = [s for s in symbols if not self._backfill_breaker.is_open(s)]
        skipped = len(symbols) - len(healthy)
        if skipped:
            logger.warning(f"Circuit breaker open for {skipped} symbol(s); skipping gap-fill for them this round.")

        async def on_close(sym, tf):
            await self.on_candle_closed(sym, tf)

        for symbol in healthy:
            try:
                await self.cache.refill_gaps([symbol], on_close)
                self._backfill_breaker.record_success(symbol)
            except Exception as e:
                self._backfill_breaker.record_failure(symbol)
                logger.warning(f"Gap refill failed for {symbol}: {e}")

    async def ws_loop(self, symbols: list[str]):
        async def on_ws_update(symbol, timeframe, candle):
            async def on_close(sym, tf):
                await self.on_candle_closed(sym, tf)
            await self.cache.handle_ws_update(symbol, timeframe, candle, on_close)

        try:
            await self.mexc.watch_klines(symbols, self.cfg.scan.timeframes, on_ws_update)
        except asyncio.CancelledError:
            pass

    async def notification_flush_loop(self):
        """Every 10s, send any confirmed-but-unnotified patterns to Telegram."""
        while True:
            try:
                await asyncio.sleep(10)
                pending = await self.db.get_pending_notifications()
                for row in pending:
                    try:
                        await self.notifier.send_signal(row)
                        await self.db.mark_notified(row["id"])
                    except Exception as e:
                        logger.error(f"Failed to notify pattern {row['id']}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Notification flush loop error: {e}")

    async def pattern_monitor_loop(self):
        """
        BUG FIX: previously nothing ever updated `detected_patterns.status`
        past its initial 'confirmed' value -- the sl_hit/tp*_hit/invalidated
        states existed only in the schema, never in practice. This loop
        periodically re-checks every still-open pattern's price levels
        against the latest closed candle and persists status transitions.
        """
        interval = self.cfg.scan.pattern_monitor_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
                updated = await check_open_patterns(self.db)
                if updated:
                    logger.info(f"Pattern monitor: updated {updated} pattern(s).")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pattern monitor loop error: {e}")

    # ------------------------------------------------------------------
    async def on_candle_closed(self, symbol: str, timeframe: str):
        """
        Fired only when a real candle close is confirmed (never mid-candle).
        Respects the rescan_interval_hours cadence to avoid hammering MEXC /
        re-scanning more often than the strategy calls for.
        """
        should_scan = await self.db.should_rescan(symbol, timeframe, self.cfg.scan.rescan_interval_hours)
        if not should_scan:
            return

        # BUG FIX: dead-letter / circuit breaker. A symbol that keeps
        # failing (delisting mid-run, persistent bad data, etc.) used to
        # be retried unconditionally on every single candle close forever.
        breaker_key = f"{symbol}:{timeframe}"
        if self._scan_breaker.is_open(breaker_key):
            logger.debug(f"Circuit breaker open for {breaker_key}; skipping scan.")
            return

        try:
            await self.scan_symbol(symbol, timeframe)
            await self.db.mark_scanned(symbol, timeframe)
            self._scan_breaker.record_success(breaker_key)
        except Exception as e:
            self._scan_breaker.record_failure(breaker_key)
            logger.error(f"Pattern scan failed for {symbol} {timeframe}: {e}")

    async def scan_symbol(self, symbol: str, timeframe: str):
        candles = await self.db.get_candles(symbol, timeframe, limit=self.cfg.scan.candles_per_fetch)
        closed_candles = [c for c in candles if c["is_closed"]]

        if len(closed_candles) < self.cfg.scan.min_candles_required:
            return

        swings = detect_swings(closed_candles, self.cfg.scan.swing_method, self.cfg.scan)
        if len(swings) < 4:
            return

        await self.db.replace_swing_points(symbol, timeframe, self.cfg.scan.swing_method, swings)

        matches = scan_for_patterns(
            swings, tolerance=self.cfg.pattern.fib_tolerance, min_score=self.cfg.pattern.min_pattern_score
        )
        if not matches:
            return

        # Current market price = close of the most recent closed candle.
        # Used below both to reject stale/blown-through signals (bug #1)
        # and as the ATR reference series for adaptive SL sizing (bug #3).
        current_price = closed_candles[-1]["close"]
        atr_value = latest_atr(closed_candles, atr_period=self.cfg.scan.atr_period)

        for match in matches:
            # ------------------------------------------------------------------
            # BUG FIX #1: validate that D is still realistic vs. current price.
            # A pattern whose D formed several candles ago (e.g. because the
            # rescan cadence or a slow scan cycle let it sit) could otherwise
            # still fire a signal well after price already blew past the
            # entry zone or the stop loss entirely.
            # ------------------------------------------------------------------
            d_time = match["D"]["point_time"]
            # Defense-in-depth: normalize both sides to naive-UTC before
            # comparing. The real fix for the "naive vs aware" crash lives
            # in swing_detector.py (point_time was silently losing its
            # timezone during a numpy round-trip), but this comparison
            # stays robust even if some other future swing method or data
            # source produces a naive timestamp.
            d_time_naive = _naive_utc(d_time)
            candles_since_d = sum(
                1 for c in closed_candles if _naive_utc(c["open_time"]) > d_time_naive
            )
            if candles_since_d > self.cfg.pattern.max_candles_since_d:
                logger.debug(
                    f"Skipping stale {symbol} {timeframe} {match['pattern_name']}: "
                    f"D is {candles_since_d} closed candles old (max {self.cfg.pattern.max_candles_since_d})."
                )
                continue

            # ------------------------------------------------------------------
            # BUG FIX #3: adaptive, ATR-scaled SL/entry-zone sizing + risk/reward
            # gate + position sizing, instead of pure static percentages with
            # no volatility awareness and no risk controls at all.
            # ------------------------------------------------------------------
            levels = calculate_trade_levels(
                match,
                self.cfg.pattern.entry_zone_pct,
                self.cfg.pattern.sl_buffer_pct,
                atr=atr_value,
                sl_atr_multiplier=self.cfg.pattern.sl_atr_multiplier if self.cfg.pattern.use_atr_sl else None,
                entry_zone_atr_multiplier=(
                    self.cfg.pattern.entry_zone_atr_multiplier if self.cfg.pattern.use_atr_entry_zone else None
                ),
            )

            actionable, reason = is_entry_still_actionable(
                match["direction"],
                current_price,
                levels["entry_zone_low"],
                levels["entry_zone_high"],
                levels["stop_loss"],
                self.cfg.pattern.max_entry_deviation_pct,
            )
            if not actionable:
                logger.info(
                    f"Skipping {symbol} {timeframe} {match['pattern_name']}: "
                    f"entry no longer actionable ({reason}); current_price={current_price}"
                )
                continue

            if not meets_min_risk_reward(levels, self.cfg.pattern.min_risk_reward):
                logger.debug(
                    f"Skipping {symbol} {timeframe} {match['pattern_name']}: "
                    f"risk/reward {levels['risk_reward_ratio']} < {self.cfg.pattern.min_risk_reward}"
                )
                continue

            entry_mid = (levels["entry_zone_low"] + levels["entry_zone_high"]) / 2
            position = calculate_position_size(
                entry_price=entry_mid,
                stop_loss=levels["stop_loss"],
                account_equity=self.cfg.risk.account_equity_usdt,
                risk_per_trade_pct=self.cfg.risk.risk_per_trade_pct,
                max_leverage=self.cfg.risk.max_leverage,
            )

            row = {
                "symbol": symbol,
                "timeframe": timeframe,
                "pattern_name": match["pattern_name"],
                "direction": match["direction"],
                "x_time": match["X"]["point_time"] if match.get("X") else None,
                "a_time": match["A"]["point_time"],
                "b_time": match["B"]["point_time"],
                "c_time": match["C"]["point_time"],
                "d_time": match["D"]["point_time"],
                "x_price": match["X"]["price"] if match.get("X") else None,
                "a_price": match["A"]["price"],
                "b_price": match["B"]["price"],
                "c_price": match["C"]["price"],
                "d_price": match["D"]["price"],
                "pattern_score": match["pattern_score"],
                "atr_at_signal": atr_value,
                "risk_amount": position.risk_amount,
                "position_qty": position.quantity,
                "position_leverage": position.leverage_used,
                **levels,
            }
            inserted = await self.db.insert_pattern(row)
            if inserted:
                logger.info(
                    f"New pattern: {symbol} {timeframe} {match['pattern_name']} "
                    f"score={match['pattern_score']} rr={levels['risk_reward_ratio']}"
                )


async def main():
    cfg = load_config()
    setup_logging(cfg)

    bot = HarmonicBot(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            pass  # Windows

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
