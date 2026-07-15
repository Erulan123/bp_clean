#!/usr/bin/env python3
# ---------- Description ----------

"""
fetch_and_store.py -- minimal viable scheduler fetch.

Meant to run unattended on a GitHub Actions cron (see
.github/workflows/poll_markets.yml), so it can't rely on local state between
runs the way src/data_collection/update_fetch.py does (no local disk
persists between Action runs). Instead the cutoff is read back FROM the
destination table itself: MAX(updated_at) across everything already stored.
That's the same "cutoff = last time we know we saw" idea, just backed by
Postgres instead of a state file.

Scope, deliberately small (this is the MVP to prove the automation works,
not the full pipeline):
  - status=open markets only
  - filtered down to TOP10_COIN_IDS (via the same keyword filter the rest of
    the project uses -- see src/filtering/keyword_filter.py)
  - 4 columns: id, slug, volume, updated_at (see scheduler/schema.sql)

Same "page newest-updatedAt-first, stop at the cutoff" strategy as
update_fetch.py, and the same string-compare-don't-parse handling of
updatedAt (see that file's module docstring for why that's safe).

Env vars:
    DATABASE_URL -- Postgres connection string (Supabase "Connection
                    pooling" URI -- see scheduler/README.md for why).

CLI usage:
    python scheduler/fetch_and_store.py
"""

# ---------- Imports ----------

import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src", "filtering"))
from keyword_filter import load_dictionary, classify_market  # noqa: E402

# ---------- Base parameters ----------

BASE_URL = "https://gamma-api.polymarket.com/markets/keyset"
DICTIONARY_PATH = os.path.join(REPO_ROOT, "assets", "crypto_dictionary.json")
LIMIT = 100
MAX_RETRIES = 3
MAX_PAGES = 500  # safety cap (50,000 open markets) -- open markets shouldn't get near this

# The 10 coins this MVP tracks. Ids match assets/crypto_dictionary.json.
TOP10_COIN_IDS = {
    "bitcoin", "ethereum", "ripple", "binancecoin", "solana",
    "dogecoin", "cardano", "chainlink", "litecoin", "polkadot",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS markets (
    id         TEXT PRIMARY KEY,
    slug       TEXT NOT NULL,
    coin_id    TEXT NOT NULL,
    volume     DOUBLE PRECISION,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_updated_at ON markets (updated_at);
"""

UPSERT_SQL = """
INSERT INTO markets (id, slug, coin_id, volume, updated_at)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    slug = EXCLUDED.slug,
    coin_id = EXCLUDED.coin_id,
    volume = EXCLUDED.volume,
    updated_at = EXCLUDED.updated_at
WHERE EXCLUDED.updated_at > markets.updated_at;
"""


# ---------- Gamma API ----------

def fetch_page(session, after_cursor):
    params = {
        "limit": LIMIT,
        "order": "updatedAt",
        "ascending": "false",  # newest -> oldest, so the cutoff check can stop early
        "closed": "false",     # open markets only
    }
    if after_cursor:
        params["after_cursor"] = after_cursor

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("markets", []), data.get("next_cursor")
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)


def fetch_updated_crypto_markets(cutoff, dictionary):
    """Pages open markets newest-updatedAt-first, stops at cutoff (or
    exhausts all open markets if cutoff is None -- first-ever run), and
    keeps only the ones that classify as one of TOP10_COIN_IDS."""
    session = requests.Session()
    after_cursor = None
    kept = []
    scanned = 0

    for page_num in range(MAX_PAGES):
        markets, next_cursor = fetch_page(session, after_cursor)
        if not markets:
            break

        hit_cutoff = False
        for m in markets:
            updated_at = m.get("updatedAt")
            if cutoff is not None and updated_at <= cutoff:
                hit_cutoff = True
                break
            scanned += 1

            coin_id = classify_market(m, dictionary)
            if coin_id in TOP10_COIN_IDS:
                kept.append((m.get("id"), m.get("slug"), coin_id, m.get("volumeNum"), updated_at))

        if hit_cutoff:
            break
        if not next_cursor:
            break
        after_cursor = next_cursor
    else:
        print(f"  !! hit MAX_PAGES={MAX_PAGES} safety cap without reaching the cutoff -- "
              f"some older open markets were not scanned this run.")

    return kept, scanned


# ---------- Postgres ----------

def get_cutoff(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(updated_at) FROM markets;")
        row = cur.fetchone()
        return row[0] if row else None


def upsert_markets(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, UPSERT_SQL, rows)
    conn.commit()


# ---------- Entry point ----------

def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL env var is not set.")

    dictionary = load_dictionary(DICTIONARY_PATH)

    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

        cutoff = get_cutoff(conn)
        print(f"Cutoff: {cutoff!r} ({'bootstrap -- first run' if cutoff is None else 'incremental'})")

        rows, scanned = fetch_updated_crypto_markets(cutoff, dictionary)
        upsert_markets(conn, rows)

        print(f"Scanned {scanned} open markets updated since cutoff, "
              f"kept {len(rows)} in the top-10 coin list, upserted into 'markets'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
