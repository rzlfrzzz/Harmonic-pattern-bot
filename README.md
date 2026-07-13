# Harmonic Pattern Telegram Bot (MEXC Futures)

Detects harmonic chart patterns — Gartley, Bat, Butterfly, Crab, Deep Crab,
Cypher, Shark, and ABCD — on the top-30 MEXC futures pairs by volume,
across 5m / 15m / 1h / 4h timeframes, and pushes entry/SL/TP signals to
Telegram the moment point D is confirmed on a **closed** candle.

---

## 1. Architecture

```
harmonic_bot/
├── main.py                      # orchestrator / entrypoint
├── Dockerfile                    # container image definition
├── docker-compose.yml             # bot service (+ optional local Postgres)
├── .env.example                   # copy to .env for Docker/CLI secrets
├── config/
│   ├── config.example.yaml      # copy to config.yaml and fill in secrets
│   └── settings.py              # config loader (+ env var overrides)
├── db/
│   ├── schema.sql                # Supabase Postgres schema
│   └── database.py               # async DB layer (asyncpg)
├── data/
│   ├── mexc_client.py             # MEXC REST + WebSocket client
│   └── candle_cache.py            # backfill + live candle-close detection
├── analysis/
│   ├── swing_detector.py          # ZigZag / Fractal / ATR-pivot / scipy peaks
│   ├── pattern_validator.py       # Fibonacci ratio validation for all 8 patterns
│   └── entry_calculator.py        # entry zone / SL / TP1-3
├── notifier/
│   └── telegram_bot.py            # message formatting + delivery
├── tests/
│   └── test_patterns.py           # unit tests for pattern math
└── requirements.txt
```

**Data flow:**
`MEXC REST (backfill)` → `Supabase candles table` → `WebSocket (live)` →
candle-close detected → `swing_detector` → `pattern_validator` →
`entry_calculator` → `detected_patterns table` → `telegram_bot` (poll loop
sends anything unnotified).

Pattern re-validation only fires on a genuine candle close (never on a
still-forming candle), and is further throttled to once every
`rescan_interval_hours` (default 4h) per symbol/timeframe via the
`scan_log` table, to stay well inside MEXC's rate limits.

---

## 2. Prerequisites

- Python 3.9+
- A [Supabase](https://supabase.com) project (free tier is enough to start)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat/channel ID (send a message to the bot, then check
  `https://api.telegram.org/bot<TOKEN>/getUpdates` for `chat.id`)

---

## 3. Setup

You can run the bot either directly with Python (section 3.1-3.5) or with
Docker (section 3.6) — Docker is the recommended path for a long-running
production deployment.

### 3.1 Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3.2 Set up Supabase

1. Create a new Supabase project.
2. Open **SQL Editor** and run the contents of `db/schema.sql` once.
3. Go to **Project Settings → Database → Connection string** and copy the
   URI (use the "Connection pooling" URI if you're deploying somewhere
   with many short-lived connections; the direct URI is fine for a single
   long-running bot process).

### 3.3 Configure the bot

```bash
cp config/config.example.yaml config/config.yaml
```

Edit `config/config.yaml`:

```yaml
telegram:
  bot_token: "123456:ABC-your-real-token"
  chat_id: "-1001234567890"

supabase:
  db_dsn: "postgresql://postgres:[email protected]:5432/postgres"
```

Adjust `scan:` and `pattern:` sections to taste (swing method, Fibonacci
tolerance, rescan cadence, minimum pattern score, etc). Defaults match the
spec: `FIB_TOLERANCE = 0.05`, rescan every 4 hours, top 30 coins, all four
timeframes.

Alternatively, skip the YAML edits and set environment variables instead
(useful for Docker/CI secrets — these override the YAML values):

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export SUPABASE_DB_DSN="postgresql://..."
```

### 3.4 Verify MEXC endpoints

MEXC's public contract API occasionally changes response shapes. Before
running for real, sanity-check the two endpoints the bot depends on still
match what `data/mexc_client.py` expects:

- `GET /api/v1/contract/ticker` — should return `amount24` (24h turnover) per symbol
- `GET /api/v1/contract/kline/{symbol}` — should return `time/open/high/low/close/vol` arrays

If MEXC has changed field names, only `data/mexc_client.py` needs updating
— every other module talks to `MexcClient`, never to the raw API.

### 3.5 Run

```bash
python main.py
```

On first run the bot will:
1. Pull the top 30 USDT-margined futures contracts by 24h volume
2. Backfill up to `candles_per_fetch` (default 500) historical candles per
   symbol/timeframe into Supabase
3. Send a Telegram "bot started" message
4. Open the MEXC WebSocket and begin live monitoring

Logs are written to `logs/bot.log` (rotating) and stdout.

### 3.6 Run with Docker (recommended for production)

The project ships with a `Dockerfile` and `docker-compose.yml`.

**1. Set up Supabase and Telegram as in 3.2 first** — Docker doesn't
change that part.

**2. Create your secrets file:**

```bash
cp .env.example .env
```

Edit `.env` and fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and
`SUPABASE_DB_DSN`. This file is read automatically by `docker compose`
and is git-ignored, so real secrets never end up in the image or a repo.

**3. Create `config/config.yaml`** (still needed for the non-secret
settings like `scan:` / `pattern:` tuning — Docker only overrides the
secret fields via env vars):

```bash
cp config/config.example.yaml config/config.yaml
```

You can leave the `telegram:`/`mexc:`/`supabase:` sections in
`config.yaml` as placeholders since `.env` overrides them at runtime;
just make sure `scan:` and `pattern:` reflect what you want.

**4. Build and run:**

```bash
docker compose up -d --build
```

**5. Check logs:**

```bash
docker compose logs -f harmonic-bot
```

**6. Stop:**

```bash
docker compose down
```

**Updating config without rebuilding:** `config/config.yaml` is mounted
into the container read-only, so editing it on the host and restarting
(`docker compose restart harmonic-bot`) is enough — no rebuild needed.
Only `requirements.txt` or code changes require `--build`.

**Testing locally without Supabase:** a `local-postgres` service is
included behind the `local-db` Docker Compose profile, pre-seeded with
`db/schema.sql` on first boot. To use it instead of a real Supabase
project:

```bash
# in .env, point SUPABASE_DB_DSN at the local container:
# SUPABASE_DB_DSN=postgresql://harmonic:harmonic@local-postgres:5432/harmonic

docker compose --profile local-db up -d --build
```

**Health checks:** the container defines a `HEALTHCHECK` that verifies
`logs/bot.log` has been written to in the last 10 minutes (the bot has no
HTTP server, so this is a simple liveness proxy). `docker ps` will show
`healthy`/`unhealthy` status accordingly.

---

## 4. How pattern detection works

1. **Swing points** — pick one method in config (`zigzag` is the default
   and generally most robust for harmonic patterns; `fractal` is simpler
   but noisier; `atr_pivot` filters fractals by a minimum ATR-multiple
   move; `scipy_peaks` uses `scipy.signal.find_peaks` with ATR-scaled
   prominence).
2. **XABCD / ABCD candidates** — the last 5 (or 4, for ABCD) alternating
   swing points are checked against each pattern's Fibonacci ratio rules
   from the spec, each with ±5% tolerance (`FIB_TOLERANCE`).
3. **Pattern score (0-100)** — average of how close each ratio (B/XA,
   C/AB, D/BC, D/XA-or-XC) sits to the *ideal* Fibonacci value within its
   valid range; scores below `pattern.min_pattern_score` are discarded.
4. **Only closed candles are used** — the D point (and every swing before
   it) must come from a closed candle. A pattern is written to
   `detected_patterns` the moment D closes; it is never re-validated on a
   still-forming candle.
5. **Entry/SL/TP:**
   - Entry zone: `D ± entry_zone_pct%`
   - Stop loss: beyond the X (or A, for ABCD) extreme, with a small
     `sl_buffer_pct%` buffer
   - TP1/TP2/TP3: 38.2% / 61.8% / 100% retracement of the D→A move (TP3 = A)

---

## 5. Testing

Unit tests cover the Fibonacci ratio math (all pattern types) and the
entry/SL/TP calculator, using synthetic X/A/B/C/D price fixtures — no live
API or database needed.

```bash
pip install pytest
pytest tests/ -v
```

To sanity-check the full pipeline against real data without waiting for a
live candle close, you can call `HarmonicBot.scan_symbol(symbol, timeframe)`
directly from a Python shell once candles have been backfilled.

---

## 6. Rate-limit & operational notes

- REST calls are spaced by `mexc.request_delay_seconds` (default 0.3s)
  and retried with exponential backoff on transient errors.
- The WebSocket reconnects automatically with backoff (1s → 60s cap) on
  disconnect.
- Full pattern re-scans are capped at once per `rescan_interval_hours`
  per symbol/timeframe — a candle closing doesn't trigger a re-scan if
  that pair was already scanned recently.
- The coin universe (top 30 by volume) refreshes hourly; symbols that
  drop out are marked inactive but their historical data isn't deleted.
- `detected_patterns` has a uniqueness constraint on
  `(symbol, timeframe, pattern_name, direction, d_time)`, so the same
  D-point pattern is never notified twice even across restarts.

---

## 7. Disclaimer

This bot is a technical-analysis signal tool, not financial advice.
Harmonic pattern detection is inherently probabilistic — always validate
signals yourself and manage risk accordingly before trading on them.
