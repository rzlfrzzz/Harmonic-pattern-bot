"""
Candle cache orchestration.

Responsibilities:
  - Backfill historical candles from REST into Supabase (initial load + gap fill)
  - Consume the live WebSocket stream and upsert the *running* candle
  - Detect the moment a candle transitions from running -> closed
    (bucket rollover) and fire a callback so the pattern engine can
    re-check that symbol/timeframe ONLY on real closes, never mid-candle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from data.mexc_client import MexcClient, TF_SECONDS
from db.database import Database

logger = logging.getLogger("harmonic_bot.candle_cache")

OnCloseCallback = Callable[[str, str], Awaitable[None]]


class CandleCache:
    def __init__(self, mexc: MexcClient, db: Database, timeframes: list[str], candles_per_fetch: int):
        self.mexc = mexc
        self.db = db
        self.timeframes = timeframes
        self.candles_per_fetch = candles_per_fetch
        # tracks the last known open_time (epoch seconds) per (symbol, tf)
        self._last_open_time: dict[tuple, int] = {}

    async def backfill_all(self, symbols: list[str]):
        """Initial historical load / gap-fill for every symbol x timeframe."""
        for symbol in symbols:
            for tf in self.timeframes:
                try:
                    candles = await self.mexc.get_klines(symbol, tf, limit=self.candles_per_fetch)
                    await self.db.upsert_candles(symbol, tf, candles)
                    if candles:
                        self._last_open_time[(symbol, tf)] = int(candles[-1]["open_time"].timestamp())
                    logger.info(f"Backfilled {len(candles)} candles for {symbol} {tf}")
                except Exception as e:
                    logger.error(f"Backfill failed for {symbol} {tf}: {e}")

    async def refill_gaps(self, symbols: list[str]):
        """Lightweight periodic top-up in case the WebSocket missed anything."""
        for symbol in symbols:
            for tf in self.timeframes:
                try:
                    candles = await self.mexc.get_klines(symbol, tf, limit=5)
                    await self.db.upsert_candles(symbol, tf, candles)
                except Exception as e:
                    logger.warning(f"Gap refill failed for {symbol} {tf}: {e}")

    async def handle_ws_update(self, symbol: str, timeframe: str, candle: dict, on_close: OnCloseCallback):
        """
        Called for every WebSocket kline push. Upserts the running candle.
        When we see the bucket's open_time advance past the last known one,
        the previous bucket is closed -> mark it closed in DB and fire on_close.
        """
        new_open_time = int(candle["open_time"].timestamp())
        key = (symbol, timeframe)
        prev_open_time = self._last_open_time.get(key)

        if prev_open_time is not None and new_open_time > prev_open_time:
            # previous candle just closed. Fetch its final values from REST
            # to guarantee accuracy (WS last-seen values can lag slightly),
            # then mark closed and trigger pattern re-check.
            try:
                recent = await self.mexc.get_klines(symbol, timeframe, limit=3)
                closed_candles = [c for c in recent if c["is_closed"]]
                if closed_candles:
                    await self.db.upsert_candles(symbol, timeframe, closed_candles)
            except Exception as e:
                logger.warning(f"Could not fetch closing candle for {symbol} {timeframe}: {e}")

            await on_close(symbol, timeframe)

        # always store the current (possibly still-open) candle too,
        # so the cache stays fresh for display / debugging purposes
        await self.db.upsert_candles(symbol, timeframe, [candle])
        self._last_open_time[key] = new_open_time
