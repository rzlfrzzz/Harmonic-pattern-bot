"""
Harmonic pattern recognition.

Takes the most recent swing points (X, A, B, C, D candidates) and checks
them against the Fibonacci ratio rules for each of the eight supported
patterns. Produces a 0-100 pattern score based on how close each ratio
is to its ideal value, within FIB_TOLERANCE.

IMPORTANT: patterns are only validated once point D corresponds to a
*closed* candle AND a *confirmed* swing point (see swing_detector.py's
"confirmed" flag). A trailing, still-forming pivot from zigzag_swings can
still move on the next candle, which would silently invalidate an
already-detected pattern after a signal has been sent -- so it must never
be used as D.
"""
from __future__ import annotations

from dataclasses import dataclass


def _ratio(a: float, b: float) -> float:
    """abs(a) / abs(b), guarding div-by-zero."""
    return abs(a) / abs(b) if b != 0 else float("inf")


def _in_range(value: float, low: float, high: float, tol: float) -> bool:
    lo = low * (1 - tol)
    hi = high * (1 + tol)
    return lo <= value <= hi


def _score_component(value: float, low: float, high: float, tol: float) -> float:
    """
    1.0 if value falls inside [low, high] (the ideal zone), decaying
    linearly to 0.0 at the tolerance-expanded edges (lo, hi) used by
    `_in_range`.

    This is only ever called on values that already passed `_in_range`,
    i.e. lo <= value <= hi is guaranteed by the caller. It therefore never
    needs to handle a genuinely out-of-tolerance value -- there is no dead
    branch here, unlike the old decay-past-the-edges version which could
    return a positive score for ratios `validate_xabcd_pattern` had
    already rejected.
    """
    if low <= value <= high:
        return 1.0
    lo = low * (1 - tol)
    hi = high * (1 + tol)
    if value < low:
        span = low - lo
        return max(0.0, (value - lo) / span) if span > 0 else 1.0
    else:
        span = hi - high
        return max(0.0, (hi - value) / span) if span > 0 else 1.0


@dataclass
class PatternRule:
    name: str
    # each is (low, high) ideal ratio range
    b_xa: tuple
    c_ab: tuple
    d_bc: tuple
    d_xa: tuple | None = None   # None for Cypher/Shark, which use XC instead
    d_xc: tuple | None = None


PATTERN_RULES = [
    PatternRule("Gartley",   b_xa=(0.618 * 0.95, 0.618 * 1.05), c_ab=(0.382, 0.886), d_bc=(1.272, 1.618), d_xa=(0.786 * 0.95, 0.786 * 1.05)),
    PatternRule("Bat",       b_xa=(0.5 * 0.95, 0.5 * 1.05),     c_ab=(0.382, 0.886), d_bc=(1.618, 2.618), d_xa=(0.886 * 0.95, 0.886 * 1.05)),
    PatternRule("Butterfly", b_xa=(0.786 * 0.95, 0.786 * 1.05), c_ab=(0.382, 0.886), d_bc=(1.618, 2.236), d_xa=(1.27 * 0.95, 1.27 * 1.05)),
    PatternRule("Crab",      b_xa=(0.618 * 0.95, 0.618 * 1.05), c_ab=(0.382, 0.886), d_bc=(2.618, 3.618), d_xa=(1.618 * 0.95, 1.618 * 1.05)),
    PatternRule("Deep Crab", b_xa=(0.886 * 0.95, 0.886 * 1.05), c_ab=(0.382, 0.886), d_bc=(2.0, 3.236),   d_xa=(1.618 * 0.95, 1.618 * 1.05)),
    # NOTE on Cypher/Shark ratios below: fixed to match the ratio definitions
    # most commonly cited for Scott Carney's harmonic patterns.
    #   Cypher:  C is an XA-extension beyond A, with BC (c_ab here) extending
    #            1.13-1.414 of AB; D retraces 0.786 of XC (d_xc).
    #   Shark:   BC (d_bc here) extends 1.13-1.618 of... of BC itself relative
    #            to the CD leg, while D (d_xa here) sits 0.886-1.13 of XA.
    #            The previous config had d_bc and the CD-vs-XA ratio swapped.
    PatternRule("Cypher",    b_xa=(0.618 * 0.95, 0.618 * 1.05), c_ab=(1.13, 1.414),  d_bc=(1.27, 1.414),  d_xc=(0.786, 0.886)),
    PatternRule("Shark",     b_xa=(1.618 * 0.95, 1.618 * 1.05), c_ab=(1.618, 2.24),  d_bc=(1.13, 1.618),  d_xa=(0.886 * 0.95, 0.886 * 1.05)),
]


def validate_xabcd_pattern(X: dict, A: dict, B: dict, C: dict, D: dict, tolerance: float) -> dict | None:
    """
    X, A, B, C, D: {"price": float, "point_time": datetime}
    Determines direction from the X->A leg and checks all supported XABCD
    patterns. Returns the best-scoring match or None if nothing qualifies.
    """
    xa = A["price"] - X["price"]
    ab = B["price"] - A["price"]
    bc = C["price"] - B["price"]
    cd = D["price"] - C["price"]
    xc = C["price"] - X["price"]

    direction = "bullish" if xa > 0 else "bearish"
    # Legs must alternate direction: XA up, AB down, BC up, CD down (bullish),
    # or the mirror image for bearish.
    if direction == "bullish":
        if not (xa > 0 and ab < 0 and bc > 0 and cd < 0):
            return None
    else:
        if not (xa < 0 and ab > 0 and bc < 0 and cd > 0):
            return None

    b_xa_ratio = _ratio(A["price"] - B["price"], xa)     # retracement of AB against XA
    c_ab_ratio = _ratio(C["price"] - B["price"], ab)     # retracement of BC against AB
    d_bc_ratio = _ratio(D["price"] - C["price"], bc)     # extension of CD against BC
    d_xa_ratio = _ratio(A["price"] - D["price"], xa)     # retracement of D against XA (same convention as B)
    d_xc_ratio = _ratio(D["price"] - C["price"], xc) if xc != 0 else float("inf")

    best = None
    for rule in PATTERN_RULES:
        if not _in_range(b_xa_ratio, *rule.b_xa, tol=tolerance):
            continue
        if not _in_range(c_ab_ratio, *rule.c_ab, tol=tolerance):
            continue
        if not _in_range(d_bc_ratio, *rule.d_bc, tol=tolerance):
            continue

        if rule.d_xa is not None:
            if not _in_range(d_xa_ratio, *rule.d_xa, tol=tolerance):
                continue
            d_score = _score_component(d_xa_ratio, *rule.d_xa, tol=tolerance)
        else:
            if not _in_range(d_xc_ratio, *rule.d_xc, tol=tolerance):
                continue
            d_score = _score_component(d_xc_ratio, *rule.d_xc, tol=tolerance)

        b_score = _score_component(b_xa_ratio, *rule.b_xa, tol=tolerance)
        c_score = _score_component(c_ab_ratio, *rule.c_ab, tol=tolerance)
        bc_score = _score_component(d_bc_ratio, *rule.d_bc, tol=tolerance)

        score = round((b_score + c_score + bc_score + d_score) / 4 * 100, 1)

        candidate = {
            "pattern_name": rule.name,
            "direction": direction,
            "pattern_score": score,
            "X": X, "A": A, "B": B, "C": C, "D": D,
        }
        if best is None or score > best["pattern_score"]:
            best = candidate

    return best


def validate_abcd_pattern(A: dict, B: dict, C: dict, D: dict, tolerance: float) -> dict | None:
    """
    Simple ABCD per spec: C ≈ 0.618-0.786 of AB (retracement),
    D ≈ 1.618 extension of BC.
    """
    ab = B["price"] - A["price"]
    bc = C["price"] - B["price"]
    cd = D["price"] - C["price"]

    direction = "bullish" if ab > 0 else "bearish"
    if direction == "bullish":
        if not (ab > 0 and bc < 0 and cd > 0):
            return None
    else:
        if not (ab < 0 and bc > 0 and cd < 0):
            return None

    c_ab_ratio = _ratio(C["price"] - B["price"], ab)
    d_bc_ratio = _ratio(D["price"] - C["price"], bc)

    c_range = (0.618 * 0.95, 0.786 * 1.05)
    d_range = (1.618 * 0.95, 1.618 * 1.05)

    if not _in_range(c_ab_ratio, *c_range, tol=tolerance):
        return None
    if not _in_range(d_bc_ratio, *d_range, tol=tolerance):
        return None

    c_score = _score_component(c_ab_ratio, *c_range, tol=tolerance)
    d_score = _score_component(d_bc_ratio, *d_range, tol=tolerance)
    score = round((c_score + d_score) / 2 * 100, 1)

    return {
        "pattern_name": "ABCD",
        "direction": direction,
        "pattern_score": score,
        "X": None, "A": A, "B": B, "C": C, "D": D,
    }


def scan_for_patterns(
    swing_points: list[dict],
    tolerance: float,
    min_score: float = 0,
    max_trailing_windows: int = 3,
) -> list[dict]:
    """
    Checks the last `max_trailing_windows` completed XABCD (5-point) and
    ABCD (4-point) combinations for a valid harmonic pattern -- not just
    the single most recent one -- in case a pattern completed a couple of
    swings ago and hasn't been notified yet.

    Windows are built by counting back from the end of `swing_points`
    (end = n, n-1, n-2, ...) rather than by pre-computing a fixed index
    slice, which was the source of the previous off-by-one bug: for small
    or unlucky values of n, the old `range(max(0, n-6), max(0, n-4))`
    could produce an empty range and silently skip a fully-formed pattern.
    This version always includes the single most recent window (offset=0)
    whenever there are at least 5 (or 4) points, and stops early once it
    runs out of history.

    A candidate is only kept if D is a *confirmed* swing point (see
    swing_detector.py). An unconfirmed trailing pivot can still move on
    the next candle, which would invalidate the pattern after a signal has
    already gone out -- so it's excluded here rather than left for callers
    to remember to filter.
    """
    results = []
    n = len(swing_points)

    # XABCD: need 5 alternating points (X, A, B, C, D)
    for offset in range(max_trailing_windows):
        end = n - offset
        start = end - 5
        if start < 0:
            break
        X, A, B, C, D = swing_points[start:end]
        if not D.get("confirmed", True):
            continue
        match = validate_xabcd_pattern(X, A, B, C, D, tolerance)
        if match and match["pattern_score"] >= min_score:
            results.append(match)

    # ABCD: need 4 alternating points (A, B, C, D)
    for offset in range(max_trailing_windows):
        end = n - offset
        start = end - 4
        if start < 0:
            break
        A, B, C, D = swing_points[start:end]
        if not D.get("confirmed", True):
            continue
        match = validate_abcd_pattern(A, B, C, D, tolerance)
        if match and match["pattern_score"] >= min_score:
            results.append(match)

    return results
