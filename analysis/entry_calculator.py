"""
Trade level calculator.

Given a confirmed harmonic pattern match, computes:
  - Entry zone around D (± entry_zone_pct)
  - Stop loss beyond the true structural extreme, with a small buffer
  - TP1/TP2/TP3 via Fibonacci retracement of the D->A move (standard
    harmonic trade management: TP1 = 0.382, TP2 = 0.618, TP3 = 1.0 = A)
"""
from __future__ import annotations


def calculate_trade_levels(match: dict, entry_zone_pct: float, sl_buffer_pct: float) -> dict:
    direction = match["direction"]
    D = match["D"]["price"]
    A = match["A"]["price"]
    X = match["X"]["price"] if match.get("X") else None

    # Entry zone: D +/- entry_zone_pct%
    half = D * (entry_zone_pct / 100)
    entry_low, entry_high = sorted([D - half, D + half])

    # Stop loss: beyond the true structural extreme.
    # IMPORTANT: extension-type patterns (Crab, Deep Crab, and sometimes
    # Butterfly) have D projecting *beyond* X (D/XA ratio > 1.0), so D
    # itself can be more extreme than X. Using X alone as the SL reference
    # in that case places the stop loss on the wrong side of the entry
    # zone entirely (SL above entry on a long, or below entry on a short).
    # We therefore always take whichever of {X (or A for ABCD), D} is more
    # extreme in the trade's direction, AND make sure the stop loss never
    # lands inside the entry zone even if sl_buffer_pct < entry_zone_pct.
    reference = X if X is not None else A

    if direction == "bullish":
        extreme = min(reference, D, entry_low)
        buffer = abs(extreme) * (sl_buffer_pct / 100)
        stop_loss = extreme - buffer
    else:
        extreme = max(reference, D, entry_high)
        buffer = abs(extreme) * (sl_buffer_pct / 100)
        stop_loss = extreme + buffer

    # Take profits: Fibonacci retracements of the D->A move back toward A,
    # standard harmonic profit-taking convention.
    move = A - D
    tp1 = D + move * 0.382
    tp2 = D + move * 0.618
    tp3 = D + move * 1.0   # = A

    return {
        "entry_zone_low": round(entry_low, 8),
        "entry_zone_high": round(entry_high, 8),
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "tp3": round(tp3, 8),
    }
