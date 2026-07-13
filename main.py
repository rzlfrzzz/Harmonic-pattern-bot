"""
Harmonic Pattern Telegram Bot — main entrypoint.

Run with:
    python main.py

Flow:
  1. Startup: connect DB, refresh top-30 coin universe, backfill candles.
  2. Launch two concurrent loops:
       a) WebSocket listener -> on every real candle close, re-check that
          symbol/timeframe for harmonic patterns (rate-limited to once
          per `rescan_interval_hours` per symbol/timeframe via scan_log).
       b) Periodic housekeeping loop -> refresh coin universe, gap-fill
          candles, and flush any pending Telegram notifications.
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
from analysis.swing_detector import detect_swings
from analysis.pattern_validator import scan_for_patterns
from analysis.entry_calculator import calculate_trade_levels
from notifier.telegram_bot import TelegramNotifier

logger = logging.getLogger("harmonic_bot")


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

    async def start(self):
        await self.db.connect()

        async with self.mexc:
            self.cache = CandleCache(self.mexc, self.db, self.cfg.scan.timeframes, self.cfg.scan.candles_per_fetch)

            await self.refresh_universe()
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
            ]
            await self._stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        await self.db.close()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    async def refresh_universe(self):
        try:
            coins = await self.mexc.get_top_volume_contracts(self.cfg.scan.top_n_coins)
            await self.db.upsert_coins(coins)
            logger.info(f"Refreshed coin universe: {len(coins)} symbols")
        except Exception as e:
            logger.error(f"Failed to refresh coin universe: {e}")

    async def housekeeping_loop(self):
        """Every hour: refresh the top-30 universe and gap-fill candles."""
        while True:
            try:
                await asyncio.sleep(3600)
                await self.refresh_universe()
                symbols = await self.db.get_active_symbols()
                await self.cache.refill_gaps(symbols)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Housekeeping loop error: {e}")

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

        try:
            await self.scan_symbol(symbol, timeframe)
            await self.db.mark_scanned(symbol, timeframe)
        except Exception as e:
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

        for match in matches:
            levels = calculate_trade_levels(
                match, self.cfg.pattern.entry_zone_pct, self.cfg.pattern.sl_buffer_pct
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
                **levels,
            }
            inserted = await self.db.insert_pattern(row)
            if inserted:
                logger.info(f"New pattern: {symbol} {timeframe} {match['pattern_name']} score={match['pattern_score']}")


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
