"""
Unit tests for pattern_validator.py and entry_calculator.py.

Run with:  pytest tests/test_patterns.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.pattern_validator import validate_xabcd_pattern, validate_abcd_pattern, scan_for_patterns
from analysis.entry_calculator import (
    calculate_trade_levels,
    calculate_position_size,
    is_entry_still_actionable,
    meets_min_risk_reward,
)
from analysis.circuit_breaker import CircuitBreaker
from analysis.pattern_tracker import _evaluate


def _pt(price, minutes_offset):
    return {"price": price, "point_time": datetime(2026, 1, 1) + timedelta(minutes=minutes_offset)}


def test_bullish_gartley_detected():
    # Textbook bullish Gartley:
    # X=0, A=100 (XA=100), B=38.2 (0.618 retrace of XA from A),
    # C=76.4 (0.618 retrace of AB from B), D=21.4 (0.786 retrace of XA from A)
    X = _pt(0, 0)
    A = _pt(100, 1)
    B = _pt(38.2, 2)
    C = _pt(76.4, 3)
    D = _pt(21.4, 4)

    match = validate_xabcd_pattern(X, A, B, C, D, tolerance=0.05)
    assert match is not None
    assert match["pattern_name"] == "Gartley"
    assert match["direction"] == "bullish"
    assert 0 <= match["pattern_score"] <= 100


def test_invalid_leg_direction_rejected():
    # AB does not retrace (keeps rising) -> should never validate as XABCD
    X = _pt(0, 0)
    A = _pt(100, 1)
    B = _pt(120, 2)   # invalid: AB should go down for bullish
    C = _pt(90, 3)
    D = _pt(110, 4)
    match = validate_xabcd_pattern(X, A, B, C, D, tolerance=0.05)
    assert match is None


def test_abcd_pattern():
    A = _pt(0, 0)
    B = _pt(100, 1)
    C = _pt(30, 2)     # 0.7 retrace of AB, within 0.618-0.786 range
    D = _pt(143, 3)     # 1.618 extension of BC (BC = -70, D = C + 1.618*70 ~ 143.26)
    match = validate_abcd_pattern(A, B, C, D, tolerance=0.05)
    assert match is not None
    assert match["pattern_name"] == "ABCD"
    assert match["direction"] == "bullish"


def test_scan_for_patterns_finds_gartley():
    swings = [
        _pt(0, 0), _pt(100, 1), _pt(38.2, 2), _pt(76.4, 3), _pt(21.4, 4),
    ]
    for i, s in enumerate(swings):
        s["point_type"] = "low" if i % 2 == 0 else "high"
    matches = scan_for_patterns(swings, tolerance=0.05, min_score=0)
    names = [m["pattern_name"] for m in matches]
    assert "Gartley" in names


def test_entry_calculator_bullish():
    match = {
        "direction": "bullish",
        "X": _pt(0, 0),
        "A": _pt(100, 1),
        "D": _pt(21.4, 4),
    }
    levels = calculate_trade_levels(match, entry_zone_pct=0.5, sl_buffer_pct=0.3)
    assert levels["entry_zone_low"] < 21.4 < levels["entry_zone_high"]
    assert levels["stop_loss"] <= 0  # at or below X (0); buffer is proportional to X so it's 0 here
    assert levels["tp3"] == 100.0  # tp3 should equal point A


def test_entry_calculator_uses_atr_sl_when_enabled():
    # Bug fix: SL buffer should scale with ATR instead of always being a
    # flat % of price, when use_atr_sl / atr are supplied.
    match = {"direction": "bullish", "X": _pt(0, 0), "A": _pt(100, 1), "D": _pt(21.4, 4)}
    static_levels = calculate_trade_levels(match, entry_zone_pct=0.5, sl_buffer_pct=0.3)
    atr_levels = calculate_trade_levels(
        match, entry_zone_pct=0.5, sl_buffer_pct=0.3, atr=5.0, sl_atr_multiplier=1.5,
    )
    # ATR buffer here (5.0 * 1.5 = 7.5) is much larger than the static
    # buffer (0.3% of an extreme near 0), so the ATR-based stop should sit
    # further away from the entry zone.
    assert atr_levels["stop_loss"] < static_levels["stop_loss"]
    assert "risk_reward_ratio" in atr_levels


def test_risk_reward_gate():
    match = {"direction": "bullish", "X": _pt(0, 0), "A": _pt(100, 1), "D": _pt(21.4, 4)}
    levels = calculate_trade_levels(match, entry_zone_pct=0.5, sl_buffer_pct=0.3)
    assert meets_min_risk_reward(levels, levels["risk_reward_ratio"])
    assert not meets_min_risk_reward(levels, levels["risk_reward_ratio"] + 1.0)


def test_position_sizing_caps_at_max_leverage():
    pos = calculate_position_size(
        entry_price=100.0, stop_loss=99.9,  # tiny risk per unit -> huge notional needed
        account_equity=1000.0, risk_per_trade_pct=1.0, max_leverage=5.0,
    )
    assert pos.leverage_used <= 5.0
    assert pos.capped_by_max_leverage is True
    # actual $ risk taken is <= the nominal risk budget once capped
    assert pos.quantity * abs(100.0 - 99.9) <= 10.0 + 1e-6


def test_position_sizing_uncapped():
    pos = calculate_position_size(
        entry_price=100.0, stop_loss=90.0,
        account_equity=1000.0, risk_per_trade_pct=1.0, max_leverage=50.0,
    )
    assert pos.capped_by_max_leverage is False
    assert abs(pos.risk_amount - 10.0) < 1e-6
    assert abs(pos.quantity - 1.0) < 1e-6  # 10 / |100-90| = 1.0 units


def test_entry_still_actionable_rejects_sl_breached():
    ok, reason = is_entry_still_actionable(
        "bullish", current_price=9.0, entry_zone_low=10.0, entry_zone_high=10.5,
        stop_loss=9.5, max_deviation_pct=1.0,
    )
    assert ok is False
    assert reason == "sl_already_breached"


def test_entry_still_actionable_rejects_stale_far_price():
    ok, reason = is_entry_still_actionable(
        "bullish", current_price=12.0, entry_zone_low=10.0, entry_zone_high=10.5,
        stop_loss=9.5, max_deviation_pct=1.0,
    )
    assert ok is False
    assert reason == "entry_too_far"


def test_entry_still_actionable_accepts_valid_price():
    ok, reason = is_entry_still_actionable(
        "bullish", current_price=10.2, entry_zone_low=10.0, entry_zone_high=10.5,
        stop_loss=9.5, max_deviation_pct=1.0,
    )
    assert ok is True
    assert reason == ""


def test_circuit_breaker_opens_after_max_failures():
    cb = CircuitBreaker(max_failures=3, cooldown_seconds=3600)
    key = "BAD_USDT:1h"
    assert not cb.is_open(key)
    for _ in range(3):
        cb.record_failure(key)
    assert cb.is_open(key)


def test_circuit_breaker_closes_on_success():
    cb = CircuitBreaker(max_failures=2, cooldown_seconds=3600)
    key = "BAD_USDT:1h"
    cb.record_failure(key)
    cb.record_failure(key)
    assert cb.is_open(key)
    cb.record_success(key)
    assert not cb.is_open(key)


def test_pattern_tracker_invalidates_before_entry():
    # Bullish pattern: price runs down to/through SL without ever trading
    # inside the entry zone -> invalidated, not sl_hit.
    pattern = {
        "direction": "bullish", "entry_zone_low": 100.0, "entry_zone_high": 101.0,
        "stop_loss": 95.0, "tp1": 110.0, "tp2": 115.0, "tp3": 120.0,
        "entered": False, "status": "confirmed",
    }
    candle = {"high": 99.0, "low": 94.0}  # never touched [100, 101], dropped through 95
    status, extra = _evaluate(pattern, candle)
    assert status == "invalidated"


def test_pattern_tracker_sl_hit_after_entry():
    pattern = {
        "direction": "bullish", "entry_zone_low": 100.0, "entry_zone_high": 101.0,
        "stop_loss": 95.0, "tp1": 110.0, "tp2": 115.0, "tp3": 120.0,
        "entered": True, "status": "confirmed",
    }
    candle = {"high": 101.0, "low": 94.0}
    status, extra = _evaluate(pattern, candle)
    assert status == "sl_hit"


def test_pattern_tracker_tp1_hit():
    pattern = {
        "direction": "bullish", "entry_zone_low": 100.0, "entry_zone_high": 101.0,
        "stop_loss": 95.0, "tp1": 110.0, "tp2": 115.0, "tp3": 120.0,
        "entered": True, "status": "confirmed",
    }
    candle = {"high": 111.0, "low": 105.0}
    status, extra = _evaluate(pattern, candle)
    assert status == "tp1_hit"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
