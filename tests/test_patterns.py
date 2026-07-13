"""
Unit tests for pattern_validator.py and entry_calculator.py.

Run with:  pytest tests/test_patterns.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.pattern_validator import validate_xabcd_pattern, validate_abcd_pattern, scan_for_patterns
from analysis.entry_calculator import calculate_trade_levels


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
