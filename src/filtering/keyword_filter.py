#!/usr/bin/env python3
# ---------- Description ----------

"""
keyword_filter.py -- flag which Polymarket markets are about a cryptocurrency.

A market is classified by looking at two fields, cheapest/most reliable first:
  1. `slug`     -- machine-generated, lowercase, hyphen-separated (e.g.
                   "btc-up-or-down-5m", "ethereum-above-1830-on-july-8-3am-et").
  2. `question` -- human-readable fallback, only checked if the slug didn't match.

Two kinds of keyword per coin, both matched on word boundaries:
  - full name  (e.g. "bitcoin", "the graph")  -- case-insensitive everywhere.
  - safe ticker (e.g. "BTC", "GRT")           -- case-insensitive in the slug
    (slugs have no case signal to check anyway), but must appear in ALL CAPS
    in the question text. That one rule is what stops a ticker like SOL from
    matching a person's name in ordinary prose ("... vs. Sol Larraya Guidi").

Tickers/names that collide with common English words or real names (TRUMP,
TON, LINK, ...) are simply left out of assets/crypto_dictionary.json -- see
its "_documented_exclusions" section. A market that's only identifiable
through one of those terms will be missed; that's an accepted trade-off, not
a bug (see docs/crypto_filter_design.md for the reasoning).

CLI usage:
    python keyword_filter.py
    python keyword_filter.py --limit 200000
    python keyword_filter.py --input ... --output ... --dictionary ...
"""

# ---------- Imports ----------

import argparse
import json
import os
import re
import time
from collections import Counter

# ---------- Base parameters ----------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_INPUT = os.path.join(REPO_ROOT, "data", "processed", "all_markets.jsonl")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "data", "processed", "filtered_crypto_markets.jsonl")
DEFAULT_DICTIONARY = os.path.join(REPO_ROOT, "assets", "crypto_dictionary.json")

PROGRESS_EVERY = 100_000


# ---------- Dictionary loading ----------

def _multiword_pattern(phrase):
    """Turn "the graph" into a pattern that matches "the-graph", "the_graph",
    or "the graph" -- slugs use hyphens, prose uses spaces."""
    words = [re.escape(w) for w in phrase.split()]
    return r"[-_\s]+".join(words)


def load_dictionary(path):
    """Build the compiled regexes + coin lookups used by classify_market().

    Returns a dict with:
      name_re                 - matches any coin name, case-insensitive
      ticker_re_ci             - matches any safe ticker, case-insensitive (for slugs)
      ticker_re_cs             - matches any safe ticker, case-SENSITIVE (for prose)
      name_lookup              - normalized name -> coin id
      ticker_lookup            - upper-case ticker -> coin id
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    names, tickers = [], []
    name_lookup, ticker_lookup = {}, {}

    for coin in data["coins"]:
        for name in coin["names"]:
            names.append(_multiword_pattern(name))
            name_lookup[re.sub(r"[-_\s]+", " ", name.lower()).strip()] = coin["id"]
        for ticker in coin["tickers"]:
            tickers.append(re.escape(ticker))
            ticker_lookup[ticker.upper()] = coin["id"]

    # Longest-first so a multi-word name matches fully rather than a shorter
    # alternative grabbing a prefix of it first.
    names.sort(key=len, reverse=True)
    tickers.sort(key=len, reverse=True)

    name_re = re.compile(r"\b(?:" + "|".join(names) + r")\b", re.IGNORECASE)
    ticker_re_ci = re.compile(r"\b(?:" + "|".join(tickers) + r")\b", re.IGNORECASE)
    ticker_re_cs = re.compile(r"\b(?:" + "|".join(tickers) + r")\b")

    return {
        "name_re": name_re,
        "ticker_re_ci": ticker_re_ci,
        "ticker_re_cs": ticker_re_cs,
        "name_lookup": name_lookup,
        "ticker_lookup": ticker_lookup,
    }


# ---------- Classification ----------

def _find_coin(text, name_re, ticker_re, name_lookup, ticker_lookup):
    if not text:
        return None
    m = name_re.search(text)
    if m:
        normalized = re.sub(r"[-_\s]+", " ", m.group(0).lower()).strip()
        return name_lookup.get(normalized)
    m = ticker_re.search(text)
    if m:
        return ticker_lookup.get(m.group(0).upper())
    return None


def classify_market(market, dictionary):
    """Return the matched coin id, or None if the market isn't crypto-related."""
    slug = market.get("slug") or ""
    coin = _find_coin(
        slug, dictionary["name_re"], dictionary["ticker_re_ci"],
        dictionary["name_lookup"], dictionary["ticker_lookup"],
    )
    if coin is not None:
        return coin

    question = market.get("question") or ""
    return _find_coin(
        question, dictionary["name_re"], dictionary["ticker_re_cs"],
        dictionary["name_lookup"], dictionary["ticker_lookup"],
    )


# ---------- Main filtering pass ----------

def filter_markets(input_path, output_path, dictionary, limit=None):
    coin_counts = Counter()
    seen = 0
    kept = 0
    start = time.time()

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(input_path, encoding="utf-8") as fin, open(output_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if limit is not None and seen >= limit:
                break
            seen += 1
            market = json.loads(line)

            coin = classify_market(market, dictionary)
            if coin is not None:
                kept += 1
                coin_counts[coin] += 1
                market["matched_coin"] = coin
                fout.write(json.dumps(market) + "\n")

            if seen % PROGRESS_EVERY == 0:
                elapsed = time.time() - start
                print(f"  {seen:,} markets scanned, {kept:,} kept ({elapsed:.1f}s)")

    elapsed = time.time() - start
    print(f"\nDone: {seen:,} markets scanned, {kept:,} kept as crypto ({elapsed:.1f}s).")
    print("Top coins by market count:")
    for coin, count in coin_counts.most_common(15):
        print(f"  {coin:<24} {count:,}")

    return seen, kept, coin_counts


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Flag crypto-related Polymarket markets by keyword.")
    ap.add_argument("--input", default=DEFAULT_INPUT, help="Source JSONL (one market per line).")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help="Where to write the kept markets.")
    ap.add_argument("--dictionary", default=DEFAULT_DICTIONARY, help="Coin dictionary JSON.")
    ap.add_argument("--limit", type=int, default=None, help="Only scan the first N markets (for testing).")
    args = ap.parse_args()

    dictionary = load_dictionary(args.dictionary)
    filter_markets(args.input, args.output, dictionary, limit=args.limit)


if __name__ == "__main__":
    main()
