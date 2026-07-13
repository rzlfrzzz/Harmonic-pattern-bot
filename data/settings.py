"""
Configuration loader.

Loads config/config.yaml (falls back to config.example.yaml with a warning),
resolves environment variable overrides, and exposes a single Config object
used throughout the bot.

Environment variable overrides (useful for Docker / CI secrets):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    MEXC_API_KEY
    MEXC_API_SECRET
    SUPABASE_DB_DSN
"""
from __future__ import annotations

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CONFIG_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = CONFIG_DIR / "config.example.yaml"


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass
class MexcConfig:
    rest_base_url: str
    ws_url: str
    api_key: str
    api_secret: str
    request_delay_seconds: float
    max_retries: int


@dataclass
class SupabaseConfig:
    db_dsn: str


@dataclass
class ScanConfig:
    top_n_coins: int
    timeframes: list
    candles_per_fetch: int
    min_candles_required: int
    rescan_interval_hours: int
    swing_method: str
    zigzag_pct: float
    fractal_window: int
    atr_period: int
    atr_multiplier: float
    scipy_prominence_atr_mult: float
    pattern_monitor_interval_seconds: int = 60


@dataclass
class PatternConfig:
    fib_tolerance: float
    min_pattern_score: int
    entry_zone_pct: float
    sl_buffer_pct: float
    # --- adaptive (ATR-based) SL/entry sizing -------------------------
    use_atr_sl: bool = False               # when true, SL buffer = atr * sl_atr_multiplier
    sl_atr_multiplier: float = 1.5
    use_atr_entry_zone: bool = False       # when true, entry half-width = atr * entry_zone_atr_multiplier
    entry_zone_atr_multiplier: float = 0.5
    # --- risk/reward gate ---------------------------------------------
    min_risk_reward: float = 1.5           # signals below this TP1-based R:R are dropped
    # --- D staleness / "is this signal still actionable" checks -------
    max_candles_since_d: int = 2           # skip signals whose D is this many closed candles old
    max_entry_deviation_pct: float = 1.0   # skip if price has run this % beyond the entry zone edge


@dataclass
class RiskConfig:
    account_equity_usdt: float = 1000.0
    risk_per_trade_pct: float = 1.0
    max_leverage: float = 5.0


@dataclass
class CircuitBreakerConfig:
    max_consecutive_failures: int = 5
    cooldown_minutes: int = 60


@dataclass
class LoggingConfig:
    level: str
    file: str
    max_bytes: int
    backup_count: int


@dataclass
class Config:
    telegram: TelegramConfig
    mexc: MexcConfig
    supabase: SupabaseConfig
    scan: ScanConfig
    pattern: PatternConfig
    logging: LoggingConfig
    risk: RiskConfig
    circuit_breaker: CircuitBreakerConfig


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            print(
                f"[config] WARNING: {cfg_path} not found. "
                f"Falling back to {EXAMPLE_CONFIG_PATH}. "
                f"Copy it to config.yaml and fill in real credentials."
            )
            cfg_path = EXAMPLE_CONFIG_PATH
        else:
            raise FileNotFoundError(f"No config file found at {cfg_path}")

    raw = _load_yaml(cfg_path)

    # Environment overrides
    raw["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", raw["telegram"]["bot_token"])
    raw["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID", raw["telegram"]["chat_id"])
    raw["mexc"]["api_key"] = os.getenv("MEXC_API_KEY", raw["mexc"].get("api_key", ""))
    raw["mexc"]["api_secret"] = os.getenv("MEXC_API_SECRET", raw["mexc"].get("api_secret", ""))
    raw["supabase"]["db_dsn"] = os.getenv("SUPABASE_DB_DSN", raw["supabase"]["db_dsn"])

    return Config(
        telegram=TelegramConfig(**raw["telegram"]),
        mexc=MexcConfig(**raw["mexc"]),
        supabase=SupabaseConfig(**raw["supabase"]),
        scan=ScanConfig(**raw["scan"]),
        pattern=PatternConfig(**raw["pattern"]),
        logging=LoggingConfig(**raw["logging"]),
        # `risk` / `circuit_breaker` are new sections; fall back to their
        # dataclass defaults so existing config.yaml files written before
        # this fix still load without edits.
        risk=RiskConfig(**raw.get("risk", {})),
        circuit_breaker=CircuitBreakerConfig(**raw.get("circuit_breaker", {})),
    )
