# Plan: Dynamic Heatmap of Cryptocoin Bids on Polymarket

## Context

This is a 12 ECTS BSc Data Science bachelor project (~300 hours) at the University of Neuchâtel. The system must continuously ingest cryptocurrency prediction market data from Polymarket's public APIs, persist it in a time-series database, and render a live web-based heatmap showing which cryptocurrencies attract the most market activity over a configurable time window. The project requires a 15-30 page academic report with justified methodology, experimental results, and proper scientific literature citations.

---

## API Reality Check (Critical Ground Truth)

Before any code is written, understand what the Polymarket public APIs actually expose without authentication:

| Endpoint | Auth Required | What It Returns |
|---|---|---|
| `gamma-api.polymarket.com/events?tag_slug=crypto&active=true` | No | Event metadata + aggregated `volume24hr`, `liquidity`, `lastTradePrice` |
| `clob.polymarket.com/book?token_id=TOKEN_ID` | No | Live orderbook `{bids: [{price, size}], asks: [{price, size}]}` |
| `clob.polymarket.com/trades?maker_asset_id=TOKEN_ID` | **Yes** | Returns `{"error":"Unauthorized"}` — unavailable without an API key |

The CLOB `/trades` endpoint requires an API key. The project must not depend on it. The two-source architecture (Gamma API for aggregated volume + CLOB `/book` for live orderbook depth) is richer than trades alone and becomes a methodological contribution, not a limitation. Frame it in the report as:

> "The combination of orderbook depth (current state of bid supply) and rolling volume (history of executed demand) provides complementary signals that trade-level data alone would not capture."

---

## Tech Stack with Justifications

| Component | Choice | Why not the alternative |
|---|---|---|
| **Time-series DB** | TimescaleDB 2.x (Docker) | SQLite: no concurrent writes, no time partitioning. InfluxDB: proprietary Flux query language. TimescaleDB gives `time_bucket()`, continuous aggregates, full SQL. |
| **HTTP client** | `httpx` (async) | `requests` is synchronous — polling 80-150 CLOB orderbooks at ~100ms each takes 8-15s per cycle, longer than the 60s interval. `httpx.AsyncClient` + `asyncio.gather` finishes in <2s. |
| **Scheduler** | APScheduler 3.10 | Cron minimum resolution is 1 minute; requires system-level access; cannot share the DB session pool. APScheduler runs inside the Python process at second-level intervals with `max_instances=1, coalesce=True`. |
| **Dashboard** | Plotly Dash 2.17 | Streamlit reruns the entire script on every interaction. React+D3 requires JavaScript/TypeScript. Dash's callback model fires only on changed inputs; `go.Heatmap` is the exact primitive needed. |
| **Caching** | flask-caching (15s TTL) | The aggregate heatmap query takes 200-800ms as data accumulates. Memoizing means the 30s Dash refresh interval does not hammer the DB on every tick. |
| **Retry logic** | `tenacity` | A single network hiccup during a poll corrupts temporal continuity. `@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))` handles this in two lines. |
| **Python version** | **3.12** (not 3.14) | `psycopg2-binary` has no prebuilt wheel for Python 3.14 on Windows. Create venv with `py -3.12 -m venv .venv`. |

---

## Project File Structure

```
polymarket-heatmap/
├── .env                        # DB password, config overrides
├── .env.example
├── requirements.txt
├── docker-compose.yml          # TimescaleDB container
├── config/
│   └── settings.py             # pydantic-settings config class
├── src/
│   ├── ingestion/
│   │   ├── gamma_client.py     # Gamma API: events + volume (paginated)
│   │   ├── clob_client.py      # CLOB API: /book?token_id= (async batch)
│   │   ├── normalizer.py       # JSON → Pydantic models + coin_tag extraction
│   │   └── scheduler.py        # APScheduler job definitions
│   ├── models/
│   │   ├── db.py               # SQLAlchemy engine + session factory
│   │   └── tables.py           # table defs + hypertable init SQL
│   ├── storage/
│   │   └── repository.py       # bulk upsert helpers (SQLAlchemy Core)
│   ├── metrics/
│   │   └── popularity.py       # compute_popularity_matrix() → pd.DataFrame
│   └── dashboard/
│       ├── app.py              # Dash app + flask-caching init
│       ├── layout.py           # component tree
│       └── callbacks.py        # @app.callback definitions
├── scripts/
│   ├── init_db.py              # create tables, hypertables, indexes
│   ├── run_pipeline.py         # entry: start APScheduler
│   └── run_dashboard.py        # entry: start Dash on port 8050
├── notebooks/
│   ├── 01_api_exploration.ipynb
│   ├── 02_eda_volume.ipynb
│   ├── 03_eda_orderbook_depth.ipynb
│   ├── 04_metric_validation.ipynb
│   └── 05_correlation_analysis.ipynb
└── tests/
    ├── test_gamma_client.py
    ├── test_clob_client.py
    ├── test_normalizer.py
    └── test_popularity.py
```

---

## Database Schema

### `docker-compose.yml`
```yaml
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_USER: polymarket
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: polymarket_heatmap
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
```

### Table 1: `crypto_events` — event metadata
```sql
CREATE TABLE crypto_events (
    event_id   TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    coin_tag   TEXT NOT NULL,   -- 'BTC', 'ETH', 'SOL', 'OTHER', ...
    slug       TEXT,
    end_date   TIMESTAMPTZ,
    is_active  BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_crypto_events_coin ON crypto_events(coin_tag);
```

### Table 2: `event_volume_snapshots` — **primary hypertable** (5-min Gamma polls)
```sql
CREATE TABLE event_volume_snapshots (
    snapshot_time TIMESTAMPTZ NOT NULL,
    event_id      TEXT NOT NULL,
    coin_tag      TEXT NOT NULL,
    volume_24hr   NUMERIC(20,4),
    liquidity     NUMERIC(20,4),
    open_interest NUMERIC(20,4),
    total_markets INTEGER
);
SELECT create_hypertable('event_volume_snapshots', 'snapshot_time',
       chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ON event_volume_snapshots (coin_tag, snapshot_time DESC);
CREATE INDEX ON event_volume_snapshots (event_id, snapshot_time DESC);
```

### Table 3: `orderbook_snapshots` — **secondary hypertable** (60-sec CLOB polls, top 30 events only)
```sql
CREATE TABLE orderbook_snapshots (
    snapshot_time  TIMESTAMPTZ NOT NULL,
    market_id      TEXT NOT NULL,
    coin_tag       TEXT NOT NULL,
    token_id       TEXT NOT NULL,
    total_bid_size NUMERIC(20,4),  -- sum of all bid sizes (USDC)
    bid_depth_top5 NUMERIC(20,4),  -- sum(price × size) for top 5 bid levels
    best_bid_price NUMERIC(6,4),
    best_ask_price NUMERIC(6,4),
    spread         NUMERIC(6,4)
);
SELECT create_hypertable('orderbook_snapshots', 'snapshot_time',
       chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ON orderbook_snapshots (coin_tag, snapshot_time DESC);
```

### Continuous Aggregate: `coin_popularity_5min` — pre-computed for dashboard speed
```sql
CREATE MATERIALIZED VIEW coin_popularity_5min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', snapshot_time) AS bucket,
    coin_tag,
    SUM(volume_24hr)         AS total_volume,
    SUM(liquidity)           AS total_liquidity,
    COUNT(DISTINCT event_id) AS active_events
FROM event_volume_snapshots
GROUP BY bucket, coin_tag;

SELECT add_continuous_aggregate_policy('coin_popularity_5min',
    start_offset    => INTERVAL '1 hour',
    end_offset      => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes');
```

---

## Data Pipeline Architecture

```
Gamma API (every 5 min)            CLOB API (every 60 sec)
     │                                    │
     ▼                                    ▼
gamma_client.py                    clob_client.py
(paginates /events,                (async batch /book?token_id=,
 ~150 crypto events)                semaphore=20 concurrent calls)
     │                                    │
     └──────────────┬─────────────────────┘
                    ▼
             normalizer.py
        (Pydantic models, COIN_MAP dict
         extracts coin_tag from event title)
                    │
                    ▼
             repository.py
        (SQLAlchemy Core bulk inserts
         ON CONFLICT DO UPDATE)
                    │
                    ▼
            TimescaleDB
         ┌──────────────────────────────┐
         │ event_volume_snapshots       │ ←── continuous aggregate
         │ orderbook_snapshots          │         ↓
         └──────────────────────────────┘  coin_popularity_5min
                    │
                    ▼
          metrics/popularity.py
      compute_popularity_matrix()
      → pd.DataFrame [coins × time_buckets]
                    │
                    ▼
        dashboard/callbacks.py
   (@app.callback + flask-caching 15s TTL)
                    │
                    ▼
        Browser: go.Heatmap
       (auto-refresh every 30s via dcc.Interval)
```

**Key implementation details:**

- **`gamma_client.py`**: Paginate `/events` with `offset` until response < 50 items. Params: `tag_slug=crypto, active=true, closed=false, order=volume, ascending=false`. Wrap every HTTP call in `@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))`.

- **`clob_client.py`**: Endpoint is `/book?token_id=TOKEN_ID` (not `/orderbook/TOKEN_ID`). The `token_id` comes from the `clobTokenIds` array in the Gamma event response. Handle HTTP 404 silently — markets close mid-poll. Use `asyncio.Semaphore(20)` to cap concurrent requests.

- **`normalizer.py`**: Coin extraction via `COIN_MAP` dict that maps keywords in event titles to standard tickers. Example entries: `{"Bitcoin": "BTC", "BTC": "BTC", "Ethereum": "ETH", "Solana": "SOL", "Hyperliquid": "HYPE", "MicroStrategy": "MSTR"}`. Unmatched events → `"OTHER"`. Document coverage stats in the report.

- **`scheduler.py`**: Both jobs configured with `max_instances=1, coalesce=True` so if a poll takes longer than the interval, the next run is skipped rather than stacked.

- Pipeline and dashboard run as **two separate processes** sharing the same PostgreSQL database.

---

## Popularity Metrics (Experimental Core)

Three metrics, each observable from unauthenticated data:

### V_score (Volume Score) — primary metric
```
V_score(coin, t) = SUM of volume_24hr across all events tagged with coin at time t
```
Source: Gamma API. Measures USDC traded in prediction markets about this coin in the past 24h.

### L_score (Liquidity Score)
```
L_score(coin, t) = SUM of liquidity across all events tagged with coin at time t
```
Source: Gamma API. Measures USDC posted as open limit orders — bid commitment, not just activity.

### D_score (Orderbook Bid Depth)
```
D_score(coin, t) = AVG over sampled markets of SUM(bid_price_i × bid_size_i) for top 5 bid levels
```
Source: CLOB `/book` snapshots. Measures dollar-value of live buy-side conviction.

### Composite Score (heatmap default)
```
P(coin, t) = 0.5 × norm(V) + 0.3 × norm(L) + 0.2 × norm(D)

where norm(x) = (x − min_coins(x)) / (max_coins(x) − min_coins(x) + ε)
```

The weights `(0.5, 0.3, 0.2)` are the **experimental variable**. Vary them across configurations and measure Spearman rank correlation between resulting coin rankings. This sensitivity analysis is the core empirical experiment reported in the thesis.

**Function signature** in `src/metrics/popularity.py`:
```python
def compute_popularity_matrix(
    engine,
    window_start: datetime,
    window_end: datetime,
    bucket_size: str = '5 minutes',
    metric: str = 'composite'   # 'volume' | 'liquidity' | 'depth' | 'composite'
) -> pd.DataFrame:
    """Returns DataFrame: rows=coin_tag, columns=time_bucket timestamps, values in [0,1]"""
```

---

## Dashboard Design

```
┌──────────────────────────────────────────────────────────────┐
│  POLYMARKET CRYPTO HEATMAP                Status: LIVE  ●    │
├──────────────────────────────────────────────────────────────┤
│  Window: [1H] [6H] [24H] [7D]    Bucket: [5min] [1h]       │
│  Metric: [Volume] [Liquidity] [Bid Depth] [Composite]       │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  HEATMAP  (go.Heatmap, Viridis colorscale)                   │
│  Y-axis: coin_tag sorted by total score (hottest at top)    │
│  X-axis: time buckets (oldest left → now right)             │
│  Color:  0.0 = dark (cold) → 1.0 = bright (hot)            │
│                                                              │
│  BTC  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░               │
│  ETH  ▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░               │
│  SOL  ░▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░               │
│  XRP  ░░░░░▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░               │
│  t-24h ──────────────────────────────────── now            │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  [Bar: Current Rankings]   │  [Line: Selected Coin History]  │
│  #1 BTC  ████████  0.92   │  Click any cell to select coin  │
│  #2 ETH  ██████    0.71   │                                 │
│  #3 SOL  ████      0.45   │                                 │
├──────────────────────────────────────────────────────────────┤
│  Last update: 2026-06-25 18:32 UTC | Markets: 238 | Pts: 47K│
└──────────────────────────────────────────────────────────────┘
```

- `dcc.Interval(interval=30_000)` drives auto-refresh
- `flask-caching` with 15s TTL on the main heatmap callback
- Dark theme via `dbc.themes.DARKLY`
- Hover tooltip: coin, timestamp, exact score
- Click any heatmap cell → line chart updates to show that coin's full history

---

## 8-Week Timeline

| Week | Focus | Concrete Deliverable |
|---|---|---|
| **1** | Setup + API exploration | Python 3.12 venv, Docker + TimescaleDB running, both API clients returning correct data, `01_api_exploration.ipynb` documenting raw JSON shapes and pagination behavior |
| **2** | Normalizer + Storage | Pydantic models, full `COIN_MAP` dict tested against all 150+ event titles, `repository.py` bulk inserts tested, `init_db.py` creates all tables + hypertables + indexes |
| **3** | Scheduler + 48h data collection | APScheduler running both jobs continuously, structlog showing healthy cycles, 48 hours of data in the database |
| **4** | Metrics + EDA | All three metrics implemented, `02_eda_volume.ipynb` + `03_eda_orderbook.ipynb` complete, Spearman correlation between metrics computed |
| **5** | Dashboard core | Heatmap rendering live data at `localhost:8050`, 30s auto-refresh working, flask-caching applied |
| **6** | Dashboard polish + performance | Time-window and metric selectors working, `EXPLAIN ANALYZE` confirms queries < 200ms, dark CSS theme, status bar |
| **7** | Experiments | CoinGecko price data fetched for BTC/ETH/SOL/XRP, cross-correlation + Granger causality analysis in `05_correlation_analysis.ipynb`, figures exported as SVG |
| **8** | Report writing | Full 20-25 page report written and submitted |

---

## Report Structure (20-25 pages)

| Section | Pages | Key Content |
|---|---|---|
| Abstract | 0.5 | System, data collected, key empirical finding |
| 1. Introduction | 1.5 | Motivation, 3 research questions (RQ1: which coins lead activity? RQ2: does volume correlate with price? RQ3: do metrics agree?) |
| 2. Background | 3 | Prediction markets (Wolfers & Zitzewitz 2004), CLOB microstructure (Glosten-Milgrom 1985), TimescaleDB (Kornacker 2016), crypto markets |
| 3. Related Work | 1.5 | Prior work on prediction market aggregation, crypto visualization, social signal → price (Ante 2023); state the gap |
| 4. System Architecture | 4 | Architecture diagram, API discovery + limitations, DB schema design rationale, dashboard design, tech justification table |
| 5. Methodology | 3 | Coin classification procedure + coverage stats, formal metric definitions with normalization formula, experimental design for sensitivity analysis and price correlation |
| 6. Data Collection & Quality | 2 | Collection period, snapshot counts, data quality issues (disappearing markets, CLOB errors, pagination gaps, 14% OTHER events) |
| 7. Results | 4 | Coin popularity rankings over time, Spearman correlation matrix between metrics, sensitivity analysis heatmap, cross-correlation plots, Granger causality results |
| 8. Dashboard | 1.5 | Screenshot, interactive features walkthrough |
| 9. Discussion | 1.5 | Interpretation, limitations, future work (authenticated API, WebSocket real-time feed) |
| 10. Conclusion | 0.5 | Summary of system and key finding |
| References | 1.5 | See list below |
| Appendices | 3 | Key code excerpts, extra figures |

---

## Key Scientific Literature

**Prediction Markets**
- Wolfers, J., & Zitzewitz, E. (2004). Prediction markets. *Journal of Economic Perspectives*, 18(2), 107–126.
- Hanson, R. (2003). Combinatorial information market design. *Information Systems Frontiers*, 5(1), 107–119.
- Manski, C. F. (2006). Interpreting the predictions of prediction markets. *Economics Letters*, 91(3), 425–429.

**Market Microstructure**
- Glosten, L. R., & Milgrom, P. R. (1985). Bid, ask and transaction prices in a specialist market with heterogeneously informed traders. *Journal of Financial Economics*, 14(1), 71–100.
- Kyle, A. S. (1985). Continuous auctions and insider trading. *Econometrica*, 53(6), 1315–1335.

**Time-Series Databases**
- Kornacker, M., et al. (2016). TimescaleDB: A time-series database for PostgreSQL. *Technical whitepaper*, Timescale Inc.
- Jensen, S. K., Pedersen, T. B., & Thomsen, C. (2017). Time series management systems: A survey. *IEEE TKDE*, 29(11), 2581–2600.

**Cryptocurrency Markets**
- Ante, L. (2023). How Elon Musk's Twitter activity moves cryptocurrency markets. *Technological Forecasting and Social Change*, 186, 122112.
- Dyhrberg, A. H. (2016). Bitcoin, gold and the dollar – A GARCH volatility analysis. *Finance Research Letters*, 16, 85–92.

**Information Visualization**
- Ware, C. (2012). *Information Visualization: Perception for Design* (3rd ed.). Morgan Kaufmann. [Justifies heatmap + Viridis colorscale choice]
- Harrower, M., & Brewer, C. A. (2003). ColorBrewer.org. *The Cartographic Journal*, 40(1), 27–37. [Justifies perceptually uniform colorscale]

**Data Systems**
- Kleppmann, M. (2017). *Designing Data-Intensive Applications*. O'Reilly. Ch. 3 & 10. [Justifies polling over stream processing for this use case]

---

## `requirements.txt`

```
# Core
pydantic==2.7.1
pydantic-settings==2.3.1
python-dotenv==1.0.1

# HTTP + Reliability
httpx==0.27.2
tenacity==8.3.0

# Database
SQLAlchemy==2.0.30
psycopg2-binary==2.9.9
alembic==1.13.1

# Scheduling
APScheduler==3.10.4

# Data Science
pandas==2.2.2
numpy==1.26.4
scipy==1.13.1
statsmodels==0.14.2

# Dashboard
dash==2.17.1
dash-bootstrap-components==1.6.0
plotly==5.22.0
flask-caching==2.3.0

# EDA + Analytics
duckdb==0.10.3
matplotlib==3.9.0
seaborn==0.13.2

# Logging
structlog==24.1.0

# Testing
pytest==8.2.2
pytest-asyncio==0.23.7
respx==0.21.1
```

> **Note:** Create venv with `py -3.12 -m venv .venv` before installing. `psycopg2-binary` has no prebuilt wheel for Python 3.14 on Windows.

---

## Verification Checklist

1. **Database growing**: `SELECT count(*) FROM event_volume_snapshots;` increases by ~150 rows every 5 minutes.
2. **Hypertable chunks**: `SELECT * FROM timescaledb_information.chunks WHERE hypertable_name='event_volume_snapshots';` shows daily chunks.
3. **Scheduler logs**: `structlog` output shows both `gamma_poll` and `clob_poll` completing without exceptions.
4. **Dashboard live**: Navigate to `http://localhost:8050`, confirm heatmap updates every 30 seconds, metric selector changes displayed values, time window selector changes x-axis range.
5. **Metrics validation**: In `04_metric_validation.ipynb`, Spearman correlation between V_score and L_score coin rankings should be ρ > 0.7 (they measure related phenomena). ρ < 0.5 signals a coin-tag extraction inconsistency to investigate.
6. **Query performance**: `EXPLAIN ANALYZE` on the heatmap query confirms < 200ms with 2+ weeks of data (continuous aggregate is being hit).
