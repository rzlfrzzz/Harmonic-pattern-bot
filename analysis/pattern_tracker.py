"""
Pattern outcome tracking.

BUG FIX: the DB schema has always had a `status` column with
('confirmed', 'invalidated', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'sl_hit')
but nothing ever updated it after the initial insert — every row sat at
'confirmed' forever, so the "outcome tracking" half of the feature never
actually existed. This module closes that gap: `check_open_patterns` is
meant to be called on a timer (see main.py's `pattern_monitor_loop`) and
walks every still-open pattern, comparing it against the latest closed
candle for that symbol/timeframe to decide whether it should transition:

  confirmed --(price touches entry zone)--> entered
  entered/confirmed --(price hits stop_loss)--> sl_hit
  entered --(price reaches tp1/tp2/tp3, in order)--> tp1_hit / tp2_hit / tp3_hit
  confirmed --(price runs past stop_loss *before ever entering*)--> invalidated

A pattern that gets to tp1_hit/tp2_hit can still continue on to a higher
TP or eventually sl_hit (trailing runs); tp3_hit and sl_hit are terminal
(the trade thesis is fully resolved either way) and invalidated is also
terminal.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("harmonic_bot.pattern_tracker")

# terminal states: once reached, we stop tracking rather than continuing
# to re-check the pattern's price levels forever.
TERMINAL_STATUSES = {"invalidated", "sl_hit", "tp3_hit"}


def _evaluate(pattern: dict, candle: dict) -> tuple[str | None, dict]:
    """
    Given an open pattern row and the latest *closed* candle for its
    symbol/timeframe, decide the new status (or None if unchanged) plus
    any extra fields to persist alongside it (e.g. entered flag).

    Uses the candle's high/low (not just close) so an intra-candle wick
    through a level is not missed just because price closed back inside.
    """
    direction = pattern["direction"]
    entry_low = float(pattern["entry_zone_low"])
    entry_high = float(pattern["entry_zone_high"])
    stop_loss = float(pattern["stop_loss"])
    tp1, tp2, tp3 = float(pattern["tp1"]), float(pattern["tp2"]), float(pattern["tp3"])
    entered = bool(pattern.get("entered", False))
    status = pattern.get("status", "confirmed")

    high, low = float(candle["high"]), float(candle["low"])

    extra: dict = {}

    if not entered:
        touched_entry = low <= entry_high and high >= entry_low
        if touched_entry:
            entered = True
            extra["entered"] = True

    if direction == "bullish":
        sl_hit = low <= stop_loss
        tp1_hit = high >= tp1
        tp2_hit = high >= tp2
        tp3_hit = high >= tp3
    else:
        sl_hit = high >= stop_loss
        tp1_hit = low <= tp1
        tp2_hit = low <= tp2
        tp3_hit = low <= tp3

    if not entered:
        # Price ran to the stop-loss side without ever giving a valid
        # entry fill -> the pattern's premise is dead, not "stopped out".
        if sl_hit:
            return "invalidated", extra
        return (None, extra) if not extra else ("confirmed", extra)

    # From here on we know entered == True.
    if sl_hit:
        return "sl_hit", extra
    if tp3_hit:
        return "tp3_hit", extra
    if tp2_hit and status not in ("tp2_hit",):
        return "tp2_hit", extra
    if tp1_hit and status not in ("tp1_hit", "tp2_hit"):
        return "tp1_hit", extra

    return (None, extra) if not extra else ("entered_only", extra)


async def check_open_patterns(db, symbol: str | None = None) -> int:
    """
    Fetches all non-terminal patterns (optionally filtered to one symbol)
    and updates their status/entered flag based on the latest closed
    candle for each symbol/timeframe. Returns the number of rows updated.
    """
    open_patterns = await db.get_open_patterns(symbol=symbol)
    updated = 0

    for pattern in open_patterns:
        candle = await db.get_latest_closed_candle(pattern["symbol"], pattern["timeframe"])
        if candle is None:
            continue

        new_status, extra = _evaluate(pattern, candle)

        if new_status == "entered_only":
            # only the `entered` flag changed, status itself stays 'confirmed'
            if extra.get("entered"):
                await db.mark_pattern_entered(pattern["id"])
                updated += 1
            continue

        if new_status is None:
            continue

        if new_status == "confirmed":
            if extra.get("entered"):
                await db.mark_pattern_entered(pattern["id"])
                updated += 1
            continue

        await db.update_pattern_status(pattern["id"], new_status, entered=extra.get("entered", pattern.get("entered", False)))
        logger.info(
            f"Pattern {pattern['symbol']} {pattern['timeframe']} {pattern['pattern_name']} "
            f"({pattern['id']}) -> status={new_status}"
        )
        updated += 1

    return updated
