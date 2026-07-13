"""
Swing point detection.

Given a closed-candle OHLC series, extracts alternating swing highs and
swing lows that become candidate X/A/B/C/D points for harmonic pattern
validation. Four interchangeable methods are provided; pick one via
config `scan.swing_method`.

All methods return a list of dicts, oldest-first:
    [{"point_time": datetime, "price": float, "point_type": "high"|"low",
      "candle_index": int, "confirmed": bool}, ...]
guaranteed to strictly alternate high/low/high/low...

"confirmed" is True for a swing point that cannot change as new candles
arrive, and False for a trailing pivot that is still provisional (price
could keep extending it before it locks in). Only `zigzag_swings` produces
unconfirmed points today (see the trailing pivot appended at the end of
that function) — the other methods only ever emit a point once its
surrounding window has fully closed, so they are always confirmed.
Consumers (see pattern_validator.scan_for_patterns) must not use an
unconfirmed point as D, since the pattern could silently invalidate itself
on the next candle after a signal has already been sent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import find_peaks


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["open_time"] = pd.to_datetime(df["open_time"])
    return df.reset_index(drop=True)


def _enforce_alternation(points: list[dict]) -> list[dict]:
    """If two consecutive points share the same type, keep only the more extreme one."""
    if not points:
        return points
    cleaned = [points[0]]
    for p in points[1:]:
        last = cleaned[-1]
        if p["point_type"] == last["point_type"]:
            if p["point_type"] == "high" and p["price"] > last["price"]:
                cleaned[-1] = p
            elif p["point_type"] == "low" and p["price"] < last["price"]:
                cleaned[-1] = p
            # else: drop p, keep last
        else:
            cleaned.append(p)
    return cleaned


# ----------------------------------------------------------------------
# Method 1: ZigZag (percentage reversal)
# ----------------------------------------------------------------------
def zigzag_swings(candles: list[dict], pct_threshold: float = 3.0) -> list[dict]:
    df = _candles_to_df(candles)
    if len(df) < 3:
        return []

    highs, lows = df["high"].values, df["low"].values
    times = df["open_time"].values

    points = []
    trend = None  # 'up' or 'down'
    last_pivot_idx = 0
    last_pivot_price = highs[0]

    for i in range(1, len(df)):
        if trend is None:
            change_up = (highs[i] - last_pivot_price) / last_pivot_price * 100
            change_down = (last_pivot_price - lows[i]) / last_pivot_price * 100
            if change_up >= pct_threshold:
                trend = "up"
                last_pivot_idx, last_pivot_price = i, highs[i]
            elif change_down >= pct_threshold:
                trend = "down"
                last_pivot_idx, last_pivot_price = i, lows[i]
            continue

        if trend == "up":
            if highs[i] > last_pivot_price:
                last_pivot_idx, last_pivot_price = i, highs[i]
            else:
                retrace = (last_pivot_price - lows[i]) / last_pivot_price * 100
                if retrace >= pct_threshold:
                    points.append({
                        "point_time": pd.Timestamp(times[last_pivot_idx]).to_pydatetime(),
                        "price": float(last_pivot_price),
                        "point_type": "high",
                        "candle_index": int(last_pivot_idx),
                        "confirmed": True,
                    })
                    trend = "down"
                    last_pivot_idx, last_pivot_price = i, lows[i]
        else:  # trend == 'down'
            if lows[i] < last_pivot_price:
                last_pivot_idx, last_pivot_price = i, lows[i]
            else:
                rally = (highs[i] - last_pivot_price) / last_pivot_price * 100
                if rally >= pct_threshold:
                    points.append({
                        "point_time": pd.Timestamp(times[last_pivot_idx]).to_pydatetime(),
                        "price": float(last_pivot_price),
                        "point_type": "low",
                        "candle_index": int(last_pivot_idx),
                        "confirmed": True,
                    })
                    trend = "up"
                    last_pivot_idx, last_pivot_price = i, highs[i]

    # append the trailing, still-forming pivot so the most recent swing is
    # available to callers -- but it is NOT confirmed: price could keep
    # extending in the same direction on the next candle, which would move
    # this point rather than lock it in. Callers must not treat this as a
    # final D.
    points.append({
        "point_time": pd.Timestamp(times[last_pivot_idx]).to_pydatetime(),
        "price": float(last_pivot_price),
        "point_type": "high" if trend == "up" else "low",
        "candle_index": int(last_pivot_idx),
        "confirmed": False,
    })

    return _enforce_alternation(points)


# ----------------------------------------------------------------------
# Method 2: Fractal pivot (N bars either side)
# ----------------------------------------------------------------------
def fractal_swings(candles: list[dict], window: int = 2) -> list[dict]:
    df = _candles_to_df(candles)
    n = len(df)
    points = []
    for i in range(window, n - window):
        window_highs = df["high"].values[i - window:i + window + 1]
        window_lows = df["low"].values[i - window:i + window + 1]
        if df["high"].values[i] == window_highs.max() and (window_highs == window_highs.max()).sum() == 1:
            points.append({
                "point_time": df["open_time"].values[i],
                "price": float(df["high"].values[i]),
                "point_type": "high",
                "candle_index": i,
                "confirmed": True,
            })
        if df["low"].values[i] == window_lows.min() and (window_lows == window_lows.min()).sum() == 1:
            points.append({
                "point_time": df["open_time"].values[i],
                "price": float(df["low"].values[i]),
                "point_type": "low",
                "candle_index": i,
                "confirmed": True,
            })
    points.sort(key=lambda p: p["candle_index"])
    for p in points:
        p["point_time"] = pd.Timestamp(p["point_time"]).to_pydatetime()
    return _enforce_alternation(points)


# ----------------------------------------------------------------------
# Method 3: ATR-based pivot (fractal filtered by minimum ATR-multiple move)
# ----------------------------------------------------------------------
def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def latest_atr(candles: list[dict], atr_period: int = 14) -> float | None:
    """
    Returns the most recent ATR value (absolute price units) for a closed
    candle series, or None if there isn't enough history yet.

    Exposed so callers outside this module (entry_calculator / main scan
    loop) can size stop-loss buffers and entry zones to each symbol's
    actual recent volatility instead of a flat, one-size-fits-all
    percentage of price — see analysis/entry_calculator.py.
    """
    df = _candles_to_df(candles)
    if len(df) < 2:
        return None
    df["atr"] = _atr(df, atr_period)
    value = df["atr"].iloc[-1]
    return float(value) if pd.notna(value) else None


def atr_pivot_swings(candles: list[dict], atr_period: int = 14, atr_multiplier: float = 1.5) -> list[dict]:
    df = _candles_to_df(candles)
    if len(df) < atr_period + 2:
        return fractal_swings(candles, window=2)

    df["atr"] = _atr(df, atr_period)
    raw = fractal_swings(candles, window=2)

    filtered = []
    for p in raw:
        idx = p["candle_index"]
        min_move = df["atr"].iloc[idx] * atr_multiplier
        if not filtered:
            filtered.append(p)
            continue
        last = filtered[-1]
        move = abs(p["price"] - last["price"])
        if move >= min_move:
            filtered.append(p)
        elif p["point_type"] == last["point_type"]:
            if (p["point_type"] == "high" and p["price"] > last["price"]) or \
               (p["point_type"] == "low" and p["price"] < last["price"]):
                filtered[-1] = p
    return _enforce_alternation(filtered)


# ----------------------------------------------------------------------
# Method 4: scipy.signal.find_peaks
# ----------------------------------------------------------------------
def scipy_peaks_swings(candles: list[dict], prominence_atr_mult: float = 1.0, atr_period: int = 14) -> list[dict]:
    df = _candles_to_df(candles)
    if len(df) < atr_period + 2:
        return []

    atr_series = _atr(df, atr_period)
    prominence = float(atr_series.iloc[-1] * prominence_atr_mult) or None

    high_peaks, _ = find_peaks(df["high"].values, prominence=prominence)
    low_peaks, _ = find_peaks(-df["low"].values, prominence=prominence)

    points = []
    for i in high_peaks:
        points.append({
            "point_time": pd.Timestamp(df["open_time"].values[i]).to_pydatetime(),
            "price": float(df["high"].values[i]),
            "point_type": "high",
            "candle_index": int(i),
            "confirmed": True,
        })
    for i in low_peaks:
        points.append({
            "point_time": pd.Timestamp(df["open_time"].values[i]).to_pydatetime(),
            "price": float(df["low"].values[i]),
            "point_type": "low",
            "candle_index": int(i),
            "confirmed": True,
        })
    points.sort(key=lambda p: p["candle_index"])
    return _enforce_alternation(points)


# ----------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------
def detect_swings(candles: list[dict], method: str, cfg) -> list[dict]:
    closed_only = [c for c in candles if c.get("is_closed", True)]
    if method == "zigzag":
        return zigzag_swings(closed_only, pct_threshold=cfg.zigzag_pct)
    elif method == "fractal":
        return fractal_swings(closed_only, window=cfg.fractal_window)
    elif method == "atr_pivot":
        return atr_pivot_swings(closed_only, atr_period=cfg.atr_period, atr_multiplier=cfg.atr_multiplier)
    elif method == "scipy_peaks":
        return scipy_peaks_swings(closed_only, prominence_atr_mult=cfg.scipy_prominence_atr_mult, atr_period=cfg.atr_period)
    else:
        raise ValueError(f"Unknown swing method: {method}")
