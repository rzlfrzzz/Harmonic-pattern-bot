-- ==========================================================
-- Migration 001: risk-management columns + outcome tracking
-- Safe to run against an existing (pre-fix) database — every
-- statement is additive and idempotent.
-- ==========================================================

alter table detected_patterns
    add column if not exists atr_at_signal     numeric,
    add column if not exists risk_reward_ratio numeric,
    add column if not exists risk_amount       numeric,
    add column if not exists position_qty      numeric,
    add column if not exists position_leverage numeric,
    add column if not exists entered           boolean not null default false,
    add column if not exists closed_at         timestamptz;

create index if not exists idx_patterns_open
    on detected_patterns (symbol, timeframe)
    where status not in ('invalidated', 'sl_hit', 'tp3_hit');
