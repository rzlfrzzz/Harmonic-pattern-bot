"""
Trade level calculator + risk management.

Given a confirmed harmonic pattern match, computes:
  - Entry zone around D (± entry_zone_pct, OR an ATR-based zone when an
    ATR value is supplied — see `calculate_trade_levels`)
  - Stop loss beyond the true structural extreme, with a buffer that is
    either a static % of price or an ATR-multiple (adaptive per symbol)
  - TP1/TP2/TP3 via Fibonacci retracement of the D->A move (standard
    harmonic trade management: TP1 = 0.382, TP2 = 0.618, TP3 = 1.0 = A)
  - Risk/reward ratio of the trade (distance to TP1 vs distance to SL)
  - Position sizing given account equity and a fixed risk-per-trade %

BUG FIX NOTES (see README/CHANGELOG for full bug list):
  - SL buffer used to always be a flat % of price (`sl_buffer_pct`)
    regardless of the symbol's actual volatility. A 0.3% buffer might be
    huge for a low-volatility major pair and tiny (guaranteed-stopout)
    for a volatile alt. We now support an ATR-based buffer
    (`atr * sl_atr_multiplier`) that scales with real recent volatility,
    selected automatically whenever an ATR value is passed in.
  - There was no risk/reward gate at all: any pattern match, however bad
    its reward-to-risk profile, generated a signal. `risk_reward_ratio`
    is now always computed and returned so callers can filter on it
    (see `meets_min_risk_reward`).
  - There was no position sizing: signals only had price levels, with no
    guidance on how much size a fixed risk-per-trade budget implies. See
    `calculate_position_size`.
  - There was no check that D (the entry trigger) was still realistic
    relative to the *current* market price when the signal was about to
    be sent — patterns detected several candles after D actually formed
    would still fire a stale signal. See `is_entry_still_actionable`.
"""
from __future__ import annotations

from dataclasses import dataclass


def calculate_trade_levels(
    match: dict,
    entry_zone_pct: float,
    sl_buffer_pct: float,
    atr: float | None = None,
    sl_atr_multiplier: float | None = None,
    entry_zone_atr_multiplier: float | None = None,
) -> dict:
    """
    Compute entry zone / stop loss / take profits for a matched pattern.

    `atr` (optional): the ATR value (absolute price units) for this
    symbol/timeframe at the time of detection. When provided together
    with `sl_atr_multiplier`, the stop-loss buffer becomes
    `atr * sl_atr_multiplier` instead of a flat % of price — this makes
    the stop adapt to each symbol's actual recent volatility rather than
    using the same static percentage for a low-volatility major and a
    high-volatility altcoin alike. Same idea applies to the entry zone
    half-width via `entry_zone_atr_multiplier`, when supplied.
    """
    direction = match["direction"]
    D = match["D"]["price"]
    A = match["A"]["price"]
    X = match["X"]["price"] if match.get("X") else None

    # Entry zone: prefer an ATR-scaled half-width when available, else
    # fall back to the static % of D.
    if atr is not None and entry_zone_atr_multiplier is not None:
        half = atr * entry_zone_atr_multiplier
    else:
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

    use_atr_sl = atr is not None and sl_atr_multiplier is not None

    if direction == "bullish":
        extreme = min(reference, D, entry_low)
        buffer = (atr * sl_atr_multiplier) if use_atr_sl else abs(extreme) * (sl_buffer_pct / 100)
        stop_loss = extreme - buffer
    else:
        extreme = max(reference, D, entry_high)
        buffer = (atr * sl_atr_multiplier) if use_atr_sl else abs(extreme) * (sl_buffer_pct / 100)
        stop_loss = extreme + buffer

    # Take profits: Fibonacci retracements of the D->A move back toward A,
    # standard harmonic profit-taking convention.
    move = A - D
    tp1 = D + move * 0.382
    tp2 = D + move * 0.618
    tp3 = D + move * 1.0   # = A

    # Risk/reward: use the entry-zone midpoint as the assumed fill price,
    # risk = distance to SL, reward = distance to TP1 (the conservative,
    # first-target reward). Both distances are always positive.
    entry_mid = (entry_low + entry_high) / 2
    risk = abs(entry_mid - stop_loss)
    reward_tp1 = abs(tp1 - entry_mid)
    risk_reward_ratio = round(reward_tp1 / risk, 3) if risk > 0 else 0.0

    return {
        "entry_zone_low": round(entry_low, 8),
        "entry_zone_high": round(entry_high, 8),
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "tp3": round(tp3, 8),
        "risk_reward_ratio": risk_reward_ratio,
    }


def meets_min_risk_reward(levels: dict, min_risk_reward: float) -> bool:
    """True if the trade's risk/reward (TP1-based) clears the configured floor."""
    return levels.get("risk_reward_ratio", 0.0) >= min_risk_reward


@dataclass
class PositionSize:
    risk_amount: float          # $ amount risked (equity * risk_per_trade_pct)
    quantity: float              # position size in base-asset units
    notional: float              # quantity * entry price
    leverage_required: float     # notional / equity, informational
    leverage_used: float         # min(leverage_required, max_leverage) actually usable
    capped_by_max_leverage: bool


def calculate_position_size(
    entry_price: float,
    stop_loss: float,
    account_equity: float,
    risk_per_trade_pct: float,
    max_leverage: float,
) -> PositionSize:
    """
    Simple fixed-fractional position sizing:
        risk_amount = equity * risk_per_trade_pct%
        quantity    = risk_amount / |entry - stop_loss|
        notional    = quantity * entry_price
        leverage_required = notional / equity

    If `leverage_required` exceeds `max_leverage`, the position is capped
    at `max_leverage` (quantity reduced), so the actual dollar risk taken
    ends up *less* than `risk_amount` in that case — safer than silently
    over-leveraging to hit the full risk budget.
    """
    risk_amount = account_equity * (risk_per_trade_pct / 100)
    per_unit_risk = abs(entry_price - stop_loss)
    if per_unit_risk <= 0 or account_equity <= 0:
        return PositionSize(0.0, 0.0, 0.0, 0.0, 0.0, False)

    quantity = risk_amount / per_unit_risk
    notional = quantity * entry_price
    leverage_required = notional / account_equity if account_equity > 0 else 0.0

    capped = leverage_required > max_leverage
    if capped:
        max_notional = account_equity * max_leverage
        quantity = max_notional / entry_price if entry_price > 0 else 0.0
        notional = quantity * entry_price
        leverage_used = max_leverage
    else:
        leverage_used = leverage_required

    return PositionSize(
        risk_amount=round(risk_amount, 8),
        quantity=round(quantity, 8),
        notional=round(notional, 8),
        leverage_required=round(leverage_required, 4),
        leverage_used=round(leverage_used, 4),
        capped_by_max_leverage=capped,
    )


def is_entry_still_actionable(
    direction: str,
    current_price: float,
    entry_zone_low: float,
    entry_zone_high: float,
    stop_loss: float,
    max_deviation_pct: float,
) -> tuple[bool, str]:
    """
    Validates that D is *still* realistic relative to the current market
    price before a signal is persisted/sent. Without this check, a
    pattern could be detected several candles after D actually formed
    (e.g. rescan-interval gap, slow scan loop, backfill catch-up) and the
    bot would still fire a stale signal for an entry price has already
    blown past.

    Returns (is_actionable, reason). reason is '' when actionable, and a
    short machine-readable code otherwise:
      - 'sl_already_breached': price has already moved beyond the stop
        loss without ever giving a valid entry — the pattern is dead.
      - 'entry_too_far': price is still on the correct side of the stop
        loss, but has moved more than `max_deviation_pct` beyond the
        entry zone edge — chasing it now would have a materially worse
        risk/reward than what was calculated.
    """
    if direction == "bullish":
        if current_price <= stop_loss:
            return False, "sl_already_breached"
        max_allowed = entry_zone_high * (1 + max_deviation_pct / 100)
        if current_price > max_allowed:
            return False, "entry_too_far"
    else:
        if current_price >= stop_loss:
            return False, "sl_already_breached"
        max_allowed = entry_zone_low * (1 - max_deviation_pct / 100)
        if current_price < max_allowed:
            return False, "entry_too_far"

    return True, ""
