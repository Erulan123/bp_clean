-- Minimal schema for the scheduler MVP. Run this once in the Supabase SQL
-- editor before the first workflow run.
--
-- One row per market, upserted in place (newest updatedAt wins) -- same
-- "keep only the latest snapshot" idea as docs/sql_schema.md, just with the
-- smaller field set this MVP actually needs.

CREATE TABLE IF NOT EXISTS markets (
    id         TEXT PRIMARY KEY,      -- Polymarket market id
    slug       TEXT NOT NULL,
    coin_id    TEXT NOT NULL,         -- matches assets/crypto_dictionary.json coin ids
    volume     DOUBLE PRECISION,
    updated_at TEXT NOT NULL          -- raw API updatedAt string, compared lexicographically
);                                    -- (same approach as src/data_collection/update_fetch.py)

CREATE INDEX IF NOT EXISTS idx_markets_updated_at ON markets (updated_at);
