#!/usr/bin/env python3
# ---------- Description ----------

"""
build_remote_db.py -- one-time (or occasionally re-run) bulk load of the full
crypto markets dataset into the dashboard's own Supabase Postgres project.

Counterpart to build_db.py (which builds the local SQLite file used before
this project had a remote database) -- same source files, same one-row-
per-market shape, but upserted into Postgres instead of a fresh local file,
so re-running this is safe and just refreshes existing rows rather than
duplicating them. See dashboard_demo/schema.sql for the table definitions
(run that once in the Supabase SQL Editor before the first load here).

This is a separate Supabase project from the one src/data_collection's
scheduler MVP (see scheduler/) writes to -- different schema, different
purpose (this one is the full dataset behind the deployed dashboard).

Env vars:
    DATABASE_URL -- Postgres connection string for the DASHBOARD's Supabase
                    project (Connection pooling / Transaction mode URI).

Usage:
    python dashboard_demo/build_remote_db.py
    python dashboard_demo/build_remote_db.py --jsonl ... --dictionary ...
"""

# ---------- Imports ----------

import argparse
import json
import os
import time

import psycopg2
import psycopg2.extras

# ---------- Base parameters ----------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_JSONL = os.path.join(REPO_ROOT, "data", "processed", "filtered_crypto_markets.jsonl")
DEFAULT_DICTIONARY = os.path.join(REPO_ROOT, "assets", "crypto_dictionary.json")

BATCH_SIZE = 5_000

COINS_UPSERT_SQL = """
INSERT INTO coins (id, name, ticker) VALUES %s
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, ticker = EXCLUDED.ticker;
"""

MARKETS_UPSERT_SQL = """
INSERT INTO markets (
    id, coin_id, question, slug, series_slug, created_at, start_date,
    end_date, closed_time, active, closed, volume, liquidity, last_updated
) VALUES %s
ON CONFLICT (id) DO UPDATE SET
    coin_id = EXCLUDED.coin_id,
    question = EXCLUDED.question,
    slug = EXCLUDED.slug,
    series_slug = EXCLUDED.series_slug,
    created_at = EXCLUDED.created_at,
    start_date = EXCLUDED.start_date,
    end_date = EXCLUDED.end_date,
    closed_time = EXCLUDED.closed_time,
    active = EXCLUDED.active,
    closed = EXCLUDED.closed,
    volume = EXCLUDED.volume,
    liquidity = EXCLUDED.liquidity,
    last_updated = EXCLUDED.last_updated
WHERE EXCLUDED.last_updated > markets.last_updated;
"""


# ---------- Extraction ----------

def load_coins(dictionary_path):
    with open(dictionary_path, encoding="utf-8") as f:
        data = json.load(f)
    rows = []
    for coin in data["coins"]:
        ticker = coin["tickers"][0] if coin["tickers"] else None
        rows.append((coin["id"], coin["name"], ticker))
    return rows


def series_slug(market):
    events = market.get("events") or []
    if not events:
        return None
    series = events[0].get("series") or []
    return series[0].get("slug") if series else None


def extract_market_row(market):
    return (
        str(market["id"]),
        market["matched_coin"],
        market.get("question"),
        market.get("slug"),
        series_slug(market),
        market.get("createdAt"),
        market.get("startDate"),
        market.get("endDate"),
        market.get("closedTime"),
        bool(market.get("active")),
        bool(market.get("closed")),
        market.get("volumeNum"),
        market.get("liquidityNum"),
        market.get("updatedAt"),
    )


# ---------- Build ----------

def build_remote_db(jsonl_path, dictionary_path, database_url):
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, COINS_UPSERT_SQL, load_coins(dictionary_path))
        conn.commit()

        t0 = time.time()
        n = 0
        batch = []

        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                market = json.loads(line)
                batch.append(extract_market_row(market))
                n += 1

                if len(batch) >= BATCH_SIZE:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_values(cur, MARKETS_UPSERT_SQL, batch)
                    conn.commit()
                    batch.clear()

                if n % 100_000 == 0:
                    print(f"  {n:,} markets loaded ({time.time() - t0:.1f}s)")

        if batch:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, MARKETS_UPSERT_SQL, batch)
            conn.commit()

        print(f"Done in {time.time() - t0:.1f}s ({n:,} markets upserted).")
    finally:
        conn.close()


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Bulk-load the crypto markets dataset into the dashboard's Supabase project.")
    ap.add_argument("--jsonl", default=DEFAULT_JSONL)
    ap.add_argument("--dictionary", default=DEFAULT_DICTIONARY)
    args = ap.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL env var is not set.")

    build_remote_db(args.jsonl, args.dictionary, database_url)


if __name__ == "__main__":
    main()
