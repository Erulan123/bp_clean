# Polymarket Data Pipeline — Bachelor Project

## Overview

This project builds a data pipeline to fetch, store, and analyse all prediction markets from [Polymarket](https://polymarket.com) using the **Gamma API** (`gamma-api.polymarket.com`). The end goal is to identify and track crypto-related markets and display them through an interactive website.

---

## What Has Been Done

### 1. Full Market Fetch (`fetching_markets.ipynb`)

Two pipelines were written — one for **closed** markets and one for **open** markets — using keyset pagination (`after_cursor`) on the Gamma API.

Key engineering decisions:

- **Resumable by design.** A `state.json` file tracks the cursor position, total fetched, and timestamps. Rerunning the notebook picks up exactly where it left off.
- **Crash-safe writes.** Each page of 100 markets is flushed and `fsync`'d to disk *before* the cursor in `state.json` is advanced. If the process is killed mid-run, no data is lost and no phantom records are created.
- **Atomic state saves.** `state.json` is written to a temp file and then renamed, so it is never left in a half-written state (especially important on Windows).
- **On-resume reconciliation.** If the data file has more lines than the last confirmed checkpoint (from an interrupted write), the extra lines are trimmed before continuing.
- **Periodic integrity checks.** Every 5,000 markets, the number of lines on disk is compared against the counter in state — a mismatch stops the pipeline immediately.

Output files: `closed_markets.jsonl` and `open_markets.jsonl` (one JSON object per line).

> **Note:** No complete open-source pipeline for fetching all Polymarket markets was found online. The cursor logic, valid ordering fields, and JSON schema were largely undocumented and required deep research and empirical testing.

### 2. Data Exploration (`explore_markets.ipynb`)

The two JSONL files were combined into `all_markets.jsonl` and analysed in a memory-efficient way (streaming line-by-line, never loading the full dataset into RAM).

Key findings:

| Metric | Value |
|---|---|
| Total markets fetched | **1,466,625** |
| Duplicate records | **0** |
| Date range | Oct 2020 → Jun 2026 |
| Markets with no event attached | **13 (0.00%)** |
| Unique event IDs referenced | **603,794** |
| Events with both open and closed markets | **1,066** |

A notable observation: market creation has grown sharply — from tens of markets per month in 2020–2021 to over **250,000/month** in early 2026.

---

### 3. Crypto Filter and Market Analysis (new this week)

#### Event-based pagination — limitations

The idea was to paginate through events rather than markets to reduce redundancy (event metadata is duplicated inside every market record). In practice, event-based pagination has a few problems: the event endpoint is less stable, pagination ordering is harder to reason about, and the saved-as-markets approach — one line per market — turned out to be simpler and more reliable for keyword search. Each line is one self-contained record, which makes grep-style filtering straightforward.

#### Crypto dictionary (`crypto_dictionary.json`, `crypto_dictionary_full.json`)

A crypto dictionary was built using the top ~300 cryptocurrencies retrieved from CoinGecko. For each coin, the dictionary stores both the full name and the ticker. The dictionary distinguishes between **safe tickers** (unambiguous, like `BTC`, `ETH`, `SOL`) and **ambiguous tickers** (like `TRUMP`, `OP`, `TOL`) that collide with common English words or political names. Ambiguous tickers are flagged and handled separately during filtering.

#### Crypto filter (`crypto_filter.py`)

The filter runs over the full JSONL and writes two outputs:

- **`crypto_matches.csv`** — one row per *(market, field, keyword, coin)* hit. Each row records *where* the coin was found and whether the hit qualifies or is review-only.
- **`crypto_markets.jsonl`** — the full records for all kept markets (568,159 markets after filtering ~1.47M).

The hardest part of this step was handling **false positives**. Short tickers especially are prone to collisions — `OP` matches "operation", `GALA` matches event names, `TON` is extremely common as a regular English word. This is why markets matched *only* by ambiguous tickers or description text are flagged as `review_only` in the CSV rather than included in the main set. The `crypto_matches.csv` file exists specifically to make it possible to audit and correct these cases without re-running the whole filter.

#### Key findings (`crypto_markets_analysis.ipynb`)

The notebook explores the 568,159 kept markets and the 10,865,379 match log rows. Main findings:

**Coin distribution:**
- Bitcoin (129k markets), Ethereum (124k), Solana (93k), and XRP (91k) dominate by market count. The top 5 coins account for the large majority of all crypto markets.
- A small number of coins inflate heavily when description hits are included. Chainlink goes from 97 qualifying markets to 411,519 total mentions — almost all of those come from resolution text like "ETH/LINK" rather than markets actually about Chainlink. Same pattern for Tether (17 → 147k). This confirms that description fields are unreliable for classification.

**Field coverage:**
- Market-level fields (`m.slug`, `m.question`) catch 566,946 out of 568,159 markets (99.8% coverage on their own). Event and series fields add only ~1,213 more markets combined (0.2%). The `feeType` field catches 304k markets but is almost entirely redundant with the market-level fields.
- The clearest takeaway: **slug and question are by far the most reliable fields for crypto filtering**. Event/series fields are useful as a safety net but contribute very little marginal coverage.

**Multi-coin markets:**
- Most markets (>95%) mention only one coin. The most common co-mentioned pair is Bitcoin + Ethereum (51 markets).

**Recurring series domination:**
- A large fraction of the 568k markets comes from auto-generated recurring series (e.g. *BTC Multi Strikes Hourly*, *ETH Multi Strikes Weekly*). A small number of series likely account for a very large share of the total count — this is important context when interpreting the dataset, since most of these markets have near-zero volume.

**Volume:**
- The majority of markets have volume close to 0. The volume distribution is heavily right-skewed. Most traded volume is concentrated in a handful of Bitcoin and Ethereum markets, which makes sense given they are the most liquid assets on the platform.

**Open vs. closed:**
- 563,660 of the 568,159 crypto markets are closed (99.2%). Only 4,499 are open. Nearly all major coins show a 99% closed fraction.

**Ambiguous hits (review bucket):**
- 14,214 markets were held back as review-only. The biggest source by far is `TRUMP` (12,650 markets) — these are almost entirely political prediction markets, not crypto. TON (585 markets) and POL (426 markets) are the next largest ambiguous categories.

#### False positive handling

The `crypto_matches.csv` was used to flag ambiguous hits. Some entries in the coin dictionary need more careful treatment: `TRUMP` is a clear false positive source (political markets), `TON` (The Open Network) collides badly with common English, and description-only hits are almost never about the coin itself. The analysis confirms that filtering on name + unambiguous ticker in slug/question/title is both high-recall and low-noise.

#### What the analysis clarified about field selection

After going through the notebook, the best fields for reliable crypto classification are:
1. `m.slug` and `m.question` — highest coverage, machine-readable, low noise
2. `e.slug`, `e.title`, `s.slug`, `s.title` — marginal but worth keeping
3. `m.description` and `e.description` — should only be used as supplementary signal, never as the sole qualifying field

---

## Limitations

- The `_full` variants of the outputs (`crypto_markets_full.jsonl`, `crypto_matches_full.csv`) use the top ~300 coins but may still have false positives, especially for short tickers. A manual spot-check on ambiguous hits is still needed to confirm precision.
- Recurring auto-generated series inflate the market count significantly. Without separating one-off markets from recurring batches, the 568k number overstates the diversity of the dataset.
- Volume data has many zeros and NaN values. Some markets never traded or volume was not recorded in the API response.

---

## Next Steps

### Step 1 — Data storage decision
Now that the structure and scale of the data is understood, the next decision is how to store it cleanly. The two main options are:
- A **SQL database** (e.g. SQLite or PostgreSQL) for faster querying, proper deduplication, and easier web dashboard integration.
- Continue with **JSONL** files for simplicity but add indexed lookup structures.

A SQL database is the likely direction given the goal of building a dynamic web dashboard.

### Step 2 — MVP dashboard
Get a minimal interactive dashboard running before optimising. The repository is not clean yet and there are overlapping scripts and intermediate files — the priority is to understand what works, get an MVP live, and then refactor. Cleaning up too early would slow things down without adding value.

### Step 3 — Optimise existing processes
Once the pipeline and storage are settled, revisit the fetching scripts and the filter for speed and reliability. Event-based pagination may become relevant again if incremental updates are needed.

---

## Main Challenges

1. **No existing reference pipeline.** There is no public, documented pipeline for bulk-fetching all Polymarket markets. The cursor logic, valid `order` fields, and the structure of the JSON responses had to be reverse-engineered through documentation gaps, forum searches, and live API testing.

2. **Sparse API documentation.** Many fields in the market JSON have no official explanation. Understanding what a field means (e.g., which fields support ordering, how the `events` array is structured, what `closed` means at the event vs. market level) required extensive trial and error.

3. **False positive handling in crypto filtering.** Short tickers collide with common words and political names. Managing this required building a two-tier dictionary (safe vs. ambiguous tickers), logging every hit in a separate CSV, and introducing a review-only category for uncertain cases rather than including or excluding them outright.
