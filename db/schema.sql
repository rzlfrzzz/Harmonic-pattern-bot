-- ==========================================================
-- Harmonic Pattern Bot — Supabase PostgreSQL Schema
-- Run this once in the Supabase SQL editor (or via psql) on a
-- fresh project. Safe to re-run: uses IF NOT EXISTS guards.
-- ==========================================================

create extension if not exists "uuid-ossp";

-- ----------------------------------------------------------
-- coins: universe of symbols currently tracked (top-N by volume)
-- ----------------------------------------------------------
create table if not exists coins (
    symbol          text primary key,          -- e.g. BTC_USDT
    quote_volume_24h numeric,
    rank            integer,
    is_active       boolean not null default true,
    updated_at      timestamptz not null default now()
);

-- ----------------------------------------------------------
-- candles: OHLCV cache per symbol/timeframe
-- ----------------------------------------------------------
create table if not exists candles (
    id          bigserial primary key,
    symbol      text not null references coins(symbol) on delete cascade,
    timeframe   text not null,                 -- 5m | 15m | 1h | 4h
    open_time   timestamptz not null,           -- candle open timestamp (UTC)
    open        numeric not null,
    high        numeric not null,
    low         numeric not null,
    close       numeric not null,
    volume      numeric not null,
    is_closed   boolean not null default true,
    created_at  timestamptz not null default now(),
    constraint uq_candle unique (symbol, timeframe, open_time)
);

create index if not exists idx_candles_symbol_tf_time
    on candles (symbol, timeframe, open_time desc);

create index if not exists idx_candles_tf_closed
    on candles (timeframe, is_closed);

-- ----------------------------------------------------------
-- swing_points: detected swing highs/lows per symbol/timeframe
-- ----------------------------------------------------------
create table if not exists swing_points (
    id          bigserial primary key,
    symbol      text not null references coins(symbol) on delete cascade,
    timeframe   text not null,
    point_time  timestamptz not null,
    price       numeric not null,
    point_type  text not null check (point_type in ('high', 'low')),
    candle_index integer,                       -- index into the candle series at detection time
    method      text not null default 'zigzag', -- zigzag | fractal | atr_pivot | scipy_peaks
    created_at  timestamptz not null default now(),
    constraint uq_swing unique (symbol, timeframe, point_time, point_type, method)
);

create index if not exists idx_swing_symbol_tf_time
    on swing_points (symbol, timeframe, point_time desc);

-- ----------------------------------------------------------
-- detected_patterns: confirmed harmonic patterns + trade levels
-- ----------------------------------------------------------
create table if not exists detected_patterns (
    id              uuid primary key default uuid_generate_v4(),
    symbol          text not null references coins(symbol) on delete cascade,
    timeframe       text not null,
    pattern_name    text not null,               -- Gartley | Bat | Butterfly | Crab | Deep Crab | Cypher | Shark | ABCD
    direction       text not null check (direction in ('bullish', 'bearish')),

    x_time  timestamptz,             -- null for ABCD (no X point)
    a_time  timestamptz not null,
    b_time  timestamptz not null,
    c_time  timestamptz not null,
    d_time  timestamptz not null,

    x_price numeric,                 -- null for ABCD (no X point)
    a_price numeric not null,
    b_price numeric not null,
    c_price numeric not null,
    d_price numeric not null,

    entry_zone_low  numeric not null,
    entry_zone_high numeric not null,
    stop_loss       numeric not null,
    tp1             numeric not null,
    tp2             numeric not null,
    tp3             numeric not null,

    pattern_score   numeric not null check (pattern_score >= 0 and pattern_score <= 100),
    status          text not null default 'confirmed'
                        check (status in ('confirmed', 'invalidated', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'sl_hit')),

    notified        boolean not null default false,
    notified_at     timestamptz,
    detected_at     timestamptz not null default now(),

    -- prevents the exact same D-point pattern from being re-notified
    constraint uq_pattern unique (symbol, timeframe, pattern_name, direction, d_time)
);

create index if not exists idx_patterns_symbol_tf
    on detected_patterns (symbol, timeframe, detected_at desc);

create index if not exists idx_patterns_notified
    on detected_patterns (notified) where notified = false;

create index if not exists idx_patterns_status
    on detected_patterns (status);

-- ----------------------------------------------------------
-- scan_log: bookkeeping so we respect the 4h rescan cadence
-- ----------------------------------------------------------
create table if not exists scan_log (
    symbol          text not null,
    timeframe       text not null,
    last_scanned_at timestamptz not null default now(),
    primary key (symbol, timeframe)
);

-- ----------------------------------------------------------
-- Helper view: latest un-notified confirmed patterns
-- ----------------------------------------------------------
create or replace view v_pending_notifications as
select *
from detected_patterns
where notified = false
  and status = 'confirmed'
order by detected_at asc;
