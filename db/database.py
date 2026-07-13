"""
Async database layer for Supabase PostgreSQL.

Uses asyncpg directly against the Supabase connection string (DB_DSN),
which is simpler and faster for high-frequency upserts than going
through the PostgREST HTTP API.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

import asyncpg

logger = logging.getLogger("harmonic_bot.db")


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self, min_size: int = 2, max_size: int = 10):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=min_size, max_size=max_size)
        logger.info("Connected to Supabase Postgres.")

    async def close(self):
        if self.pool:
            await self.pool.close()

    # ------------------------------------------------------------------
    # coins
    # ------------------------------------------------------------------
    async def upsert_coins(self, coins: Iterable[dict]):
        """coins: [{symbol, quote_volume_24h, rank}]"""
        rows = [(c["symbol"], c["quote_volume_24h"], c["rank"]) for c in coins]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("update coins set is_active = false")
                await conn.executemany(
                    """
                    insert into coins (symbol, quote_volume_24h, rank, is_active, updated_at)
                    values ($1, $2, $3, true, now())
                    on conflict (symbol) do update
                        set quote_volume_24h = excluded.quote_volume_24h,
                            rank = excluded.rank,
                            is_active = true,
                            updated_at = now()
                    """,
                    rows,
                )

    async def get_active_symbols(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("select symbol from coins where is_active = true order by rank asc")
            return [r["symbol"] for r in rows]

    # ------------------------------------------------------------------
    # candles
    # ------------------------------------------------------------------
    async def upsert_candles(self, symbol: str, timeframe: str, candles: list[dict]):
        """candles: [{open_time, open, high, low, close, volume, is_closed}]"""
        if not candles:
            return
        rows = [
            (
                symbol,
                timeframe,
                c["open_time"],
                c["open"],
                c["high"],
                c["low"],
                c["close"],
                c["volume"],
                c["is_closed"],
            )
            for c in candles
        ]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                insert into candles (symbol, timeframe, open_time, open, high, low, close, volume, is_closed)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                on conflict (symbol, timeframe, open_time) do update
                    set open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        volume = excluded.volume,
                        is_closed = excluded.is_closed
                """,
                rows,
            )

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select open_time, open, high, low, close, volume, is_closed
                from candles
                where symbol = $1 and timeframe = $2
                order by open_time desc
                limit $3
                """,
                symbol, timeframe, limit,
            )
            return [dict(r) for r in reversed(rows)]

    async def get_last_candle_time(self, symbol: str, timeframe: str) -> datetime | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select open_time from candles
                where symbol = $1 and timeframe = $2 and is_closed = true
                order by open_time desc limit 1
                """,
                symbol, timeframe,
            )
            return row["open_time"] if row else None

    # ------------------------------------------------------------------
    # swing points
    # ------------------------------------------------------------------
    async def replace_swing_points(self, symbol: str, timeframe: str, method: str, points: list[dict]):
        """Full replace of swing points for a symbol/timeframe/method (cheap to recompute)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "delete from swing_points where symbol = $1 and timeframe = $2 and method = $3",
                    symbol, timeframe, method,
                )
                if points:
                    rows = [
                        (symbol, timeframe, p["point_time"], p["price"], p["point_type"], p.get("candle_index"), method)
                        for p in points
                    ]
                    await conn.executemany(
                        """
                        insert into swing_points (symbol, timeframe, point_time, price, point_type, candle_index, method)
                        values ($1, $2, $3, $4, $5, $6, $7)
                        on conflict do nothing
                        """,
                        rows,
                    )

    # ------------------------------------------------------------------
    # detected patterns
    # ------------------------------------------------------------------
    async def insert_pattern(self, p: dict) -> bool:
        """Returns True if a new row was inserted (i.e. genuinely new signal)."""
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(
                """
                insert into detected_patterns (
                    symbol, timeframe, pattern_name, direction,
                    x_time, a_time, b_time, c_time, d_time,
                    x_price, a_price, b_price, c_price, d_price,
                    entry_zone_low, entry_zone_high, stop_loss, tp1, tp2, tp3,
                    pattern_score
                ) values (
                    $1,$2,$3,$4, $5,$6,$7,$8,$9, $10,$11,$12,$13,$14,
                    $15,$16,$17,$18,$19,$20, $21
                )
                on conflict (symbol, timeframe, pattern_name, direction, d_time) do nothing
                returning id
                """,
                p["symbol"], p["timeframe"], p["pattern_name"], p["direction"],
                p["x_time"], p["a_time"], p["b_time"], p["c_time"], p["d_time"],
                p["x_price"], p["a_price"], p["b_price"], p["c_price"], p["d_price"],
                p["entry_zone_low"], p["entry_zone_high"], p["stop_loss"], p["tp1"], p["tp2"], p["tp3"],
                p["pattern_score"],
            )
            return result is not None

    async def get_pending_notifications(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("select * from v_pending_notifications")
            return [dict(r) for r in rows]

    async def mark_notified(self, pattern_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "update detected_patterns set notified = true, notified_at = now() where id = $1",
                pattern_id,
            )

    # ------------------------------------------------------------------
    # scan_log (rate-limit / cadence bookkeeping)
    # ------------------------------------------------------------------
    async def should_rescan(self, symbol: str, timeframe: str, interval_hours: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "select last_scanned_at from scan_log where symbol = $1 and timeframe = $2",
                symbol, timeframe,
            )
            if row is None:
                return True
            elapsed_hours = (datetime.utcnow() - row["last_scanned_at"].replace(tzinfo=None)).total_seconds() / 3600
            return elapsed_hours >= interval_hours

    async def mark_scanned(self, symbol: str, timeframe: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                insert into scan_log (symbol, timeframe, last_scanned_at)
                values ($1, $2, now())
                on conflict (symbol, timeframe) do update set last_scanned_at = now()
                """,
                symbol, timeframe,
            )
