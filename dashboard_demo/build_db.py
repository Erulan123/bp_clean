#!/usr/bin/env python3
# ---------- Description ----------

"""
build_db.py -- build the SQLite database for the crypto markets dashboard.

Reads assets/crypto_dictionary.json for the coin dimension table, and
data/processed/filtered_crypto_markets.jsonl (one market per line, already
tagged with `matched_coin` by src/filtering/keyword_filter.py) for everything
else. One pass over the JSONL file: each record fills one row in `markets`
(static identity fields) and one row in `market_snapshots` (the metrics that
change over a market's life -- volume, liquidity, active/closed status).

Dates are stored as-is (ISO 8601 text) rather than parsed at load time --
SQLite has no native DATE type anyway, and ISO 8601 strings already sort and
range-filter correctly as plain text. See docs/to_do.md for why.

Usage:
    python build_db.py
    python build_db.py --jsonl ... --dictionary ... --db ...
"""

# ---------- Imports ----------

import argparse
import json
import os
import sqlite3
import time

# ---------- Base parameters ----------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_JSONL = os.path.join(REPO_ROOT, "data", "processed", "filtered_crypto_markets.jsonl")
DEFAULT_DICTIONARY = os.path.join(REPO_ROOT, "assets", "crypto_dictionary.json")
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "processed", "markets.db")

BATCH_SIZE = 5_000

SCHEMA = """
CREATE TABLE coins (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    ticker TEXT
);

CREATE TABLE markets (
    id          TEXT PRIMARY KEY,
    coin_id     TEXT NOT NULL REFERENCES coins(id),
    question    TEXT,
    slug        TEXT,
    series_slug TEXT,
    created_at  TEXT,
    start_date  TEXT,
    end_date    TEXT,
    closed_time TEXT
);

CREATE TABLE market_snapshots (
    market_id     TEXT NOT NULL REFERENCES markets(id),
    snapshot_time TEXT NOT NULL,
    volume        REAL,
    liquidity     REAL,
    active        INTEGER,
    closed        INTEGER,
    PRIMARY KEY (market_id, snapshot_time)
);
"""

INDEXES = [
    "CREATE INDEX idx_markets_coin        ON markets(coin_id);",
    "CREATE INDEX idx_markets_created_at  ON markets(created_at);",
    "CREATE INDEX idx_markets_coin_period ON markets(coin_id, created_at);",
    "CREATE INDEX idx_markets_start_end   ON markets(start_date, end_date);",
    "CREATE INDEX idx_snapshots_market    ON market_snapshots(market_id);",
    "CREATE INDEX idx_snapshots_time      ON market_snapshots(snapshot_time);",
]


# ---------- Extraction ----------

def load_coins(dictionary_path):
    """One row per coin: id, display name, first safe ticker (or None)."""
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
    )


def extract_snapshot_row(market):
    return (
        str(market["id"]),
        market.get("updatedAt"),
        market.get("volumeNum"),
        market.get("liquidityNum"),
        int(bool(market.get("active"))),
        int(bool(market.get("closed"))),
    )


# ---------- Build ----------

def build_db(jsonl_path, dictionary_path, db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=OFF;")
    conn.executescript(SCHEMA)

    conn.executemany("INSERT INTO coins VALUES (?, ?, ?);", load_coins(dictionary_path))
    conn.commit()

    t0 = time.time()
    n = 0
    market_batch, snapshot_batch = [], []

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            market = json.loads(line)
            market_batch.append(extract_market_row(market))
            snapshot_batch.append(extract_snapshot_row(market))
            n += 1

            if len(market_batch) >= BATCH_SIZE:
                conn.executemany("INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);", market_batch)
                conn.executemany("INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?);", snapshot_batch)
                conn.commit()
                market_batch.clear()
                snapshot_batch.clear()

            if n % 100_000 == 0:
                print(f"  {n:,} markets loaded ({time.time() - t0:.1f}s)")

    if market_batch:
        conn.executemany("INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);", market_batch)
        conn.executemany("INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?);", snapshot_batch)
        conn.commit()

    print("Building indexes...")
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()

    print(f"Done in {time.time() - t0:.1f}s -> {db_path} ({n:,} markets)")
    conn.close()


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Build the crypto markets dashboard SQLite database.")
    ap.add_argument("--jsonl", default=DEFAULT_JSONL)
    ap.add_argument("--dictionary", default=DEFAULT_DICTIONARY)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    build_db(args.jsonl, args.dictionary, args.db)


if __name__ == "__main__":
    main()
