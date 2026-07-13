"""
MEXC Futures (contract) data client.

Wraps the two things this bot needs from MEXC:
  1. REST: top-volume contracts + historical K-lines (candles)
  2. WebSocket: live K-line stream, used to detect "candle closed" events

NOTE ON ENDPOINTS:
MEXC's public contract API has changed shape a few times. The endpoints
below match the documented v1 contract API as of this bot's last update
(https://mexcdevelop.github.io/apidocs/contract_v1_en/). If MEXC changes
paths/params, only this file needs to change — every other module talks
to `MexcClient`, never to the raw API.

Symbol format: MEXC futures symbols look like "BTC_USDT".
Interval mapping: MEXC uses Min1, Min5, Min15, Min30, Min60, Hour4, Hour8, Day1, ...
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

import aiohttp
import websockets
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("harmonic_bot.mexc")

# our timeframe label -> MEXC interval enum
TF_MAP = {
    "5m": "Min5",
    "15m": "Min15",
    "1h": "Min60",
    "4h": "Hour4",
}

# our timeframe label -> seconds (used for gap-filling / next-close prediction)
TF_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}


class MexcClient:
    def __init__(self, rest_base_url: str, ws_url: str, request_delay: float = 0.3, max_retries: int = 3):
        self.rest_base_url = rest_base_url.rstrip("/")
        self.ws_url = ws_url
        self.request_delay = request_delay
        self.max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def _get(self, path: str, params: dict | None = None) -> dict:
        assert self._session is not None, "use `async with MexcClient(...) as client:`"
        url = f"{self.rest_base_url}{path}"
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        await asyncio.sleep(self.request_delay)  # rate-limit spacing
        return data

    async def get_top_volume_contracts(self, top_n: int = 30) -> list[dict]:
        """
        Returns [{symbol, quote_volume_24h, rank}] sorted by 24h turnover desc.
        Endpoint: GET /api/v1/contract/ticker (all symbols)
        """
        data = await self._get("/api/v1/contract/ticker")
        tickers = data.get("data", [])
        if not isinstance(tickers, list):
            tickers = [tickers]

        # amount24 = 24h turnover (quote volume) on MEXC contract tickers
        def vol(t):
            return float(t.get("amount24") or t.get("volume24") or 0)

        usdt_perp = [t for t in tickers if str(t.get("symbol", "")).endswith("_USDT")]
        usdt_perp.sort(key=vol, reverse=True)

        top = usdt_perp[:top_n]
        return [
            {"symbol": t["symbol"], "quote_volume_24h": vol(t), "rank": i + 1}
            for i, t in enumerate(top)
        ]

    async def get_klines(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict]:
        """
        Returns closed+current candles, oldest first:
        [{open_time (datetime, UTC), open, high, low, close, volume, is_closed}]
        Endpoint: GET /api/v1/contract/kline/{symbol}?interval=...&start=...&end=...
        MEXC returns arrays keyed by field name: time, open, close, high, low, vol
        """
        interval = TF_MAP[timeframe]
        step = TF_SECONDS[timeframe]
        end_ts = int(time.time())
        start_ts = end_ts - step * limit

        data = await self._get(
            f"/api/v1/contract/kline/{symbol}",
            params={"interval": interval, "start": start_ts, "end": end_ts},
        )
        payload = data.get("data", {})
        times = payload.get("time", [])
        opens = payload.get("open", [])
        closes = payload.get("close", [])
        highs = payload.get("high", [])
        lows = payload.get("low", [])
        vols = payload.get("vol", [])

        candles = []
        now_bucket_start = end_ts - (end_ts % step)
        for i in range(len(times)):
            t = int(times[i])
            is_closed = t < now_bucket_start
            candles.append({
                "open_time": datetime.fromtimestamp(t, tz=timezone.utc),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": float(vols[i]),
                "is_closed": is_closed,
            })
        return candles

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    async def watch_klines(
        self,
        symbols: list[str],
        timeframes: list[str],
        on_candle_close: Callable[[str, str, dict], "asyncio.Future"],
        app_ping_interval: float = 15.0,
    ):
        """
        Subscribes to kline channels for every (symbol, timeframe) pair and
        invokes `on_candle_close(symbol, timeframe, candle)` on every update.
        The caller (candle_cache) is responsible for deciding when a bucket
        rollover means the previous candle is now closed.

        IMPORTANT: MEXC's contract WebSocket does not answer standard
        WebSocket protocol-level ping frames. It requires an
        application-level `{"method": "ping"}` JSON message sent at least
        every ~20s, and replies with `{"channel": "pong", ...}` (falling
        back to `{"method": "pong"}` on some gateway versions). If you
        rely on the `websockets` library's built-in ping_interval instead,
        the server never responds to those protocol pings and the client
        will disconnect with a "keepalive ping timeout" every ~45s. So we
        disable the library's protocol-level ping entirely (ping_interval=
        None) and run our own application-level ping loop instead.

        Reconnects automatically with exponential backoff on drop.
        """
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    logger.info("MEXC WebSocket connected.")
                    backoff = 1
                    for symbol in symbols:
                        for tf in timeframes:
                            sub = {
                                "method": "sub.kline",
                                "param": {"symbol": symbol, "interval": TF_MAP[tf]},
                            }
                            await ws.send(json.dumps(sub))
                            await asyncio.sleep(0.05)  # gentle subscribe pacing

                    ping_task = asyncio.create_task(self._app_ping_loop(ws, app_ping_interval))
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            channel = msg.get("channel")

                            if channel in ("pong", "rs.error") or msg.get("method") == "pong":
                                # heartbeat ack or subscription error notice; nothing to process
                                if channel == "rs.error":
                                    logger.warning(f"MEXC WS error message: {msg}")
                                continue

                            if channel != "push.kline":
                                continue

                            data = msg.get("data", {})
                            symbol = msg.get("symbol") or data.get("symbol")
                            interval = data.get("interval")
                            tf = next((k for k, v in TF_MAP.items() if v == interval), None)
                            if tf is None or symbol is None:
                                continue

                            bucket_t = int(data.get("t") or data.get("time") or 0)
                            candle = {
                                "open_time": datetime.fromtimestamp(bucket_t, tz=timezone.utc),
                                "open": float(data["o"]),
                                "high": float(data["h"]),
                                "low": float(data["l"]),
                                "close": float(data["c"]),
                                "volume": float(data.get("v", 0)),
                                "is_closed": False,
                            }
                            await on_candle_close(symbol, tf, candle)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                logger.warning(f"MEXC WebSocket disconnected ({e}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    @staticmethod
    async def _app_ping_loop(ws, interval: float):
        """Sends MEXC's required application-level ping to keep the connection alive."""
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"method": "ping"}))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
