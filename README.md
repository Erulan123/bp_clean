# Measuring Crypto Attention Through Prediction Markets: A Time-Series Database and Interactive Dashboard Built on Polymarket Data

## Overview

This project builds a data pipeline to fetch, store, and analyse every prediction market
that has ever existed on [Polymarket](https://polymarket.com) via its **Gamma API**
(`gamma-api.polymarket.com`), identify the ones that are actually about a cryptocurrency,
and track how much attention each coin gets over time through a SQL database and an
interactive dashboard.

The repository was a loose collection of exploration notebooks and scripts at the last
checkpoint. It's now a structured pipeline: fetch → merge → filter → store → visualize,
with each stage as its own script or notebook.

---

## The pipeline, step by step

1. **Historic fetch** (`src/data_collection/historic_fetch.py`) -- a one-time, resumable
   crawl of every market via keyset pagination, ordered newest-to-oldest by id. Crash-safe
   by design: every page is written and fsynced before its checkpoint is saved, so an
   interrupted run loses at most the one page in flight and can always resume exactly
   where it left off.

2. **Incremental updates** (`src/data_collection/update_fetch.py`) -- captures everything
   that's changed *since the last run* by paging through markets ordered by `updatedAt`
   descending and stopping once it crosses a cutoff timestamp. The cutoff is always the
   *previous* run's own start time (not its finish time), so each run's scan window
   overlaps the entire duration the previous one was executing -- nothing that changed
   mid-run can slip through the gap. The whole incremental-update strategy depends on the
   API's `updatedAt`-descending ordering guarantee actually holding, which was verified
   separately before relying on it.

3. **Merge** (`src/data_collection/merge_data.py`) -- combines the historic crawl and
   every update run since into `data/processed/all_markets.jsonl`: exactly one row per
   market id, keeping whichever version has the newest `updatedAt`. Incremental on
   its own end too -- it remembers which files it's already folded in and never rereads
   them.

4. **Crypto keyword filter** (`src/filtering/keyword_filter.py`) -- flags which markets
   are about a cryptocurrency by matching coin names/tickers (`assets/crypto_dictionary.json`)
   against each market's slug (checked first) and question text (fallback), producing
   `data/processed/filtered_crypto_markets.jsonl`. Matches on the slug first because it's
   machine-generated and far less prone to false positives than free-text matching, with
   the question text as a fallback for markets with no useful slug.

5. **SQL database** (`dashboard_demo/build_db.py`) -- builds `data/processed/markets.db`
   (SQLite): a small `coins` table, a `markets` table (one row per market, static fields),
   and a `market_snapshots` table (volume/liquidity/status -- meant to grow one row per
   market per periodic refresh, not be overwritten in place).

6. **Dashboard** (`dashboard_demo/app.py`) -- a Streamlit app over `markets.db`: pick a
   date range and a set of coins (checklist, defaults to the top 10 by volume), see
   volume/liquidity/market-count/volume-per-market broken down by coin as a bar chart,
   treemap, coin×time heatmap, and time series.

---

## Repository structure

```
src/data_collection/     historic_fetch.py, update_fetch.py, merge_data.py, fetch_pipeline.py
                         (fetch_pipeline.py is the single entry point -- figures out
                         whether to resume the historic crawl or run the next update)
src/filtering/           keyword_filter.py -- the crypto classifier

assets/
  crypto_dictionary.json   curated coin name/ticker dictionary the filter matches against

data/
  raw/                     historic_fetch.py + update_fetch.py output (not shipped --
                           large and superseded by processed/all_markets.jsonl)
  processed/
    all_markets.jsonl       every market, deduplicated (merge_data.py output)
    filtered_crypto_markets.jsonl   crypto markets only (keyword_filter.py output)
    markets.db              the SQLite database (build_db.py output)

dashboard_demo/
  build_db.py              builds markets.db from filtered_crypto_markets.jsonl
  app.py                   the Streamlit dashboard
  .streamlit/config.toml   disables the first-run usage-stats prompt
  requirements.txt         just what app.py needs (streamlit, plotly, pandas)

notebooks/
  markets_exploration.ipynb     explores the full, unfiltered dataset (all_markets.jsonl)
  crypto_markets_analysis.ipynb explores the crypto-filtered subset (filtered_crypto_markets.jsonl)
  popularity_metrics.ipynb      the treemap/heatmap views behind the dashboard's design
```

---

## Running it

**Setup:**
```
pip install -r requirements_s.txt
```

**Data.** `data/raw/`, `data/processed/all_markets.jsonl`, `filtered_crypto_markets.jsonl`,
and `markets.db` are not shipped in the repository -- they're large, and a persistent
storage setup for the pipeline hasn't been settled on yet. Get in touch and the files
you need will be shared directly.

**To see the dashboard** You only need `markets.db` (386MB) -- neither
`app.py` nor `popularity_metrics.ipynb` touch the raw JSONL files.
```
cd dashboard_demo
streamlit run app.py
```

---

## Findings

### The full dataset (`markets_exploration.ipynb`)

- **1,698,867 markets total**, spanning 70 active months. Market creation has grown
  enormously: the busiest month on record (2026-06) alone had **331,277 markets created**
  -- more than the entire platform had in its first few years combined.
- **75.6%** of markets (1,284,877) have any trading volume at all; of those, the median
  is **$1,560** but the mean is **$71,124** -- a classic long-tail: a small number of
  markets carry most of the money.
- **97.4%** of markets resolve with a clear winner. The outcome split is close to
  balanced (No 25.2%, Down 15.9%, Up 15.8%, Under 9.9%, Yes 9.4%, Over 7.5%, ...) -- no
  obvious systematic bias in how these markets resolve.
- Recurrence cadence is dominated by **daily** markets (1,083,365 -- most of the
  platform), then 5-minute (210,617), genuinely one-off (127,356), 15-minute (106,455),
  and hourly (105,667). Most of Polymarket, by count, is templated and short-lived, not
  one-off event markets.
- **Why a real keyword dictionary was necessary, not just a ticker heuristic:** naively
  splitting the series ticker on `-` (`"bitcoin-multi-strikes-hourly"` → `"bitcoin"`) and
  ranking by volume puts `nba`, `soccer`, `fomc`, `mlb`, `league`, `nfl`, `counter`,
  `atp`, `nhl`, and `us` in the same top-15 list as `btc`, `bitcoin`, `eth`, and
  `ethereum` -- sports/politics tickers that happen to split the same way. This is the
  concrete evidence behind building `keyword_filter.py` with a curated dictionary instead
  of a shortcut.

### The crypto-filtered subset (`crypto_markets_analysis.ipynb`)

- **634,680 markets** kept, across **43 distinct coins** -- but **7 coins (Bitcoin,
  Ethereum, Solana, XRP, Hyperliquid, Dogecoin, BNB) account for 99.93%** of them. The
  other 36 coins combined are a rounding error by market count.
- Same concentration in volume, more extreme: **Bitcoin alone is $10.83B of the $15.02B
  total crypto volume (72%)**; Bitcoin + Ethereum together are ~90%.
- **133 distinct series**, and the **top 20 account for 88.1%** of all crypto markets --
  confirms the dataset is mostly a handful of auto-generated recurring templates
  (`btc-up-or-down-5m`, `bitcoin-multi-strikes-hourly`, ...) repeated at huge scale, not
  a wide diversity of one-off questions.
- **23.2%** of crypto markets have zero volume; median volume across the rest is modest
  ($859) -- most of these templated markets barely trade, even though collectively they
  dominate the market *count*.
- **99.6%** of crypto markets are closed (632,382 / 634,680) -- consistent with most of
  them being short-lived recurring templates that resolve quickly.

### The popularity dashboard (`popularity_metrics.ipynb`)

Four charts, each pairing a magnitude with a second metric so both show up in one
picture: volume (size) × liquidity (color); market count (size) × volume-per-market
(color); and the same volume / volume-per-market split again as a coin×month heatmap.
The market-count-vs-volume-per-market pairing is the one that matters most: it's what
makes recurring-template inflation visible directly in the chart, rather than something
you have to already know to correct for -- a coin can be huge on size (thousands of
auto-generated 5-minute markets) and still pale on color if each one barely trades.

---

## Limitations / next steps

- The dashboard and `market_snapshots` table are currently built from a single point-in-time
  load -- one snapshot per market, not real periodic history yet. Wiring `update_fetch.py`
  into a periodic upsert job (append new snapshot rows on each refresh) is the next step
  to make period-over-period comparisons reflect real measurements instead of an
  approximation from `created_at`.
- 36 of the 43 matched coins contribute a handful of markets each -- the filter's recall
  on long-tail coins hasn't been stress-tested the way the top 7 have.
- A few known, accepted false positives remain by design, where a coin's name collides
  with a real person/team/title (an NHL team named Avalanche, a Valorant team named
  Bonk, the movie *Tron: Ares*) -- judged not worth special-cased logic for the small
  number of markets involved.
