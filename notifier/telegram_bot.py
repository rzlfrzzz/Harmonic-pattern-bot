"""
Telegram notifier.

Formats and sends the harmonic-pattern signal message defined in the spec,
with retry-safe delivery via python-telegram-bot.
"""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("harmonic_bot.telegram")


def _fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:,.4f}"
    else:
        return f"{p:.8f}".rstrip("0").rstrip(".")


def format_signal_message(pattern_row: dict) -> str:
    direction = pattern_row["direction"]
    emoji = "🟢" if direction == "bullish" else "🔴"
    label = "Bullish" if direction == "bullish" else "Bearish"

    x_line = f"X: {_fmt_price(pattern_row['x_price'])}\n" if pattern_row.get("x_price") is not None else ""

    return (
        f"{emoji} {label} {pattern_row['pattern_name']} Detected\n\n"
        f"Pair: {pattern_row['symbol']}\n"
        f"Timeframe: {pattern_row['timeframe']}\n\n"
        f"{x_line}"
        f"A: {_fmt_price(pattern_row['a_price'])}\n"
        f"B: {_fmt_price(pattern_row['b_price'])}\n"
        f"C: {_fmt_price(pattern_row['c_price'])}\n"
        f"D: {_fmt_price(pattern_row['d_price'])}\n\n"
        f"Entry Zone: {_fmt_price(pattern_row['entry_zone_low'])}–{_fmt_price(pattern_row['entry_zone_high'])}\n"
        f"Stop Loss: {_fmt_price(pattern_row['stop_loss'])}\n"
        f"TP1: {_fmt_price(pattern_row['tp1'])}\n"
        f"TP2: {_fmt_price(pattern_row['tp2'])}\n"
        f"TP3: {_fmt_price(pattern_row['tp3'])}\n\n"
        f"Pattern Score: {pattern_row['pattern_score']}/100\n"
        f"Status: Candle D confirmed"
    )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(TelegramError),
    )
    async def send_signal(self, pattern_row: dict):
        text = format_signal_message(pattern_row)
        await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=None)
        logger.info(f"Sent signal: {pattern_row['symbol']} {pattern_row['timeframe']} {pattern_row['pattern_name']}")

    async def send_text(self, text: str):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
