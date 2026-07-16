-- Full dashboard schema, for a dedicated Supabase project (separate from the
-- scheduler MVP's project/table). Matches the design in docs/sql_schema.md:
-- one row per market, upserted in place (newest last_updated wins) -- no
-- snapshot history, since none is kept yet.
--
-- Run this once in the new project's SQL Editor before the first
-- build_remote_db.py load.

CREATE TABLE IF NOT EXISTS coins (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    ticker TEXT
);

CREATE TABLE IF NOT EXISTS markets (
    id           TEXT PRIMARY KEY,
    coin_id      TEXT NOT NULL REFERENCES coins(id),
    question     TEXT,
    slug         TEXT,
    series_slug  TEXT,
    created_at   TIMESTAMP,
    start_date   TIMESTAMP,
    end_date     TIMESTAMP,
    closed_time  TIMESTAMP,
    active       BOOLEAN,
    closed       BOOLEAN,
    volume       REAL,
    liquidity    REAL,
    last_updated TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_markets_coin        ON markets(coin_id);
CREATE INDEX IF NOT EXISTS idx_markets_created_at  ON markets(created_at);
CREATE INDEX IF NOT EXISTS idx_markets_start_end   ON markets(start_date, end_date);
