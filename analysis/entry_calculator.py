"""
Trade level calculator.

Given a confirmed harmonic pattern match, computes:
  - Entry zone around D (± entry_zone_pct)
  - Stop loss beyond the X/A extreme, with a small buffer
  - TP1/TP2/TP3 via Fibonacci retracement of the CD leg (standard harmonic
    trade management: TP1 = 0.382 retrace of AD (or CD), TP2 = 0.618,
    TP3 = full retrace back to point A)
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

    # Stop loss: beyond the extreme point (X preferred; fall back to A for ABCD)
    extreme = X if X is not None else A
    buffer = abs(extreme) * (sl_buffer_pct / 100)

    if direction == "bullish":
        stop_loss = extreme - buffer   # SL below the low extreme
    else:
        stop_loss = extreme + buffer   # SL above the high extreme

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
