#!/usr/bin/env python3
# ---------- Description ----------

"""
Crypto Markets Dashboard.

Run:
    streamlit run app.py

Only markets whose entire lifetime (start_date to end_date) falls inside the
selected period are shown -- so there's a single, unambiguous notion of "in
this period," and no separate choice needed between created/end/closed dates.
"""

# ---------- Imports ----------

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# ---------- Base parameters ----------

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "markets.db"

DEFAULT_START = pd.Timestamp("2026-01-01")
DEFAULT_END = pd.Timestamp("2026-07-01")

DEFAULT_TOP_N = 10

# Validated 8-slot categorical palette (fixed order, colorblind-checked) plus
# a further unvalidated-but-distinct extension for coins beyond the top 8.
# Safe here because every chart also labels coins directly, so color is
# never the sole way to tell two coins apart.
CATEGORICAL_PALETTE = [
    "#2a78d6", "#1baf7a", "#eda100", "#008300",
    "#4a3aa7", "#e34948", "#e87ba4", "#eb6834",
    "#8a5a3c", "#767671", "#0f766e", "#7a7a1f",
    "#5b6b7a", "#b23a5e", "#7a9c1f",
]
FALLBACK_COLOR = "#c3c2b7"

METRICS = ["Volume", "Liquidity", "Number of markets", "Volume per market"]

st.set_page_config(page_title="Crypto Markets Dashboard", layout="wide")


# ---------- Data loading ----------

@st.cache_data
def load_coin_options():
    """Every coin that has at least one market, ranked by all-time total
    volume (stable regardless of the period picked) -- this ranking drives
    both the checklist order/numbering and the default top-N checked."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT c.id, c.name, SUM(s.volume) AS total_volume "
        "FROM coins c "
        "JOIN markets m ON m.coin_id = c.id "
        "JOIN market_snapshots s ON s.market_id = m.id "
        "GROUP BY c.id ORDER BY total_volume DESC",
        conn,
    )
    conn.close()
    ranked_coins = df[["id", "name"]].to_dict("records")
    default_ids = set(df["id"].head(DEFAULT_TOP_N))
    color_map = dict(zip(df["id"].head(len(CATEGORICAL_PALETTE)), CATEGORICAL_PALETTE))
    return ranked_coins, default_ids, color_map


@st.cache_data
def load_filtered(coin_ids: tuple, start: str, end_exclusive: str) -> pd.DataFrame:
    if not coin_ids:
        return pd.DataFrame(columns=["id", "coin_id", "coin_name", "start_date", "volume", "liquidity"])
    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(coin_ids))
    query = (
        "SELECT m.id, m.coin_id, c.name AS coin_name, m.start_date, s.volume, s.liquidity "
        "FROM markets m "
        "JOIN coins c ON c.id = m.coin_id "
        "JOIN market_snapshots s ON s.market_id = m.id "
        f"WHERE m.coin_id IN ({placeholders}) AND m.start_date >= ? AND m.end_date < ?"
    )
    df = pd.read_sql(query, conn, params=[*coin_ids, start, end_exclusive])
    conn.close()
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce", utc=True, format="ISO8601").dt.tz_localize(None)
    df["volume"] = df["volume"].fillna(0)
    df["liquidity"] = df["liquidity"].fillna(0)
    return df


def bucket_series(dates: pd.Series, granularity: str) -> pd.Series:
    if granularity == "Day":
        return dates.dt.floor("D")
    if granularity == "Week":
        return dates.dt.to_period("W").dt.start_time
    return dates.dt.to_period("M").dt.start_time


def aggregate(df: pd.DataFrame, group_cols: list, metric: str) -> pd.DataFrame:
    if metric == "Number of markets":
        out = df.groupby(group_cols).size().reset_index(name="value")
    elif metric == "Volume per market":
        total = df.groupby(group_cols)["volume"].sum()
        count = df.groupby(group_cols).size()
        out = (total / count).reset_index(name="value")
    else:
        col = "volume" if metric == "Volume" else "liquidity"
        out = df.groupby(group_cols)[col].sum().reset_index(name="value")
    return out


# ---------- Page ----------

st.title("Crypto Markets Dashboard")
st.caption(
    "Only markets whose entire lifetime (start date to end date) falls within the "
    "selected period are included. Volume/liquidity are each market's lifetime totals "
    "as of the last update, not trading flow within the period specifically."
)

if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. Run build_db.py first.")
    st.stop()

ranked_coins, default_coin_ids, color_map = load_coin_options()
name_by_id = {c["id"]: c["name"] for c in ranked_coins}
name_color_map = {name: color_map.get(cid, FALLBACK_COLOR) for cid, name in name_by_id.items()}

with st.sidebar:
    st.header("Filters")
    start_date = st.date_input("Start date", value=DEFAULT_START)
    end_date = st.date_input("End date", value=DEFAULT_END)
    metric = st.selectbox("Metric", METRICS, index=0)
    bucket_gran = st.selectbox("Time bucket", ["Day", "Week", "Month"], index=2)

    st.header("Cryptocurrencies")
    selected_ids = []
    for i, coin in enumerate(ranked_coins, start=1):
        checked = st.checkbox(
            f"{i}. {coin['name']}", value=coin["id"] in default_coin_ids, key=f"coin_{coin['id']}"
        )
        if checked:
            selected_ids.append(coin["id"])

if start_date > end_date:
    st.warning("Start date is after end date.")
    st.stop()
if not selected_ids:
    st.warning("Select at least one cryptocurrency.")
    st.stop()

selected_ids = tuple(selected_ids)
end_exclusive = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

df = load_filtered(selected_ids, str(start_date), end_exclusive)

if df.empty:
    st.warning("No markets found for this selection.")
    st.stop()

# ---- KPI row ----
col1, col2, col3 = st.columns(3)
col1.metric("Total volume", f"${df['volume'].sum():,.0f}")
col2.metric("Total liquidity", f"${df['liquidity'].sum():,.0f}")
col3.metric("Number of markets", f"{df['id'].nunique():,}")

# ---- by-coin aggregate ----
by_coin = aggregate(df, ["coin_name"], metric).sort_values("value", ascending=False)

st.subheader(f"{metric} by coin")
c1, c2 = st.columns(2)

with c1:
    fig_bar = px.bar(
        by_coin, x="coin_name", y="value", color="coin_name",
        color_discrete_map=name_color_map,
        text_auto=".2s",
    )
    fig_bar.update_layout(showlegend=False, xaxis_title="Coin", yaxis_title=metric)
    st.plotly_chart(fig_bar, width="stretch")

with c2:
    fig_tree = px.treemap(
        by_coin, path=["coin_name"], values="value", color="value",
        color_continuous_scale="Blues",
    )
    st.plotly_chart(fig_tree, width="stretch")

# ---- heatmap: coin x time bucket ----
st.subheader(f"{metric} heatmap (coin x {bucket_gran.lower()})")
df["bucket"] = bucket_series(df["start_date"], bucket_gran)
heat_agg = aggregate(df, ["coin_name", "bucket"], metric)
pivot = heat_agg.pivot(index="coin_name", columns="bucket", values="value").fillna(0)
pivot = pivot.reindex(by_coin["coin_name"])
fig_heat = px.imshow(
    pivot, aspect="auto", color_continuous_scale=["#2a78d6", "#e34948"],
    labels=dict(x="Time bucket", y="Coin", color=metric),
)
st.plotly_chart(fig_heat, width="stretch")

# ---- time series: aggregate metric over time ----
st.subheader(f"{metric} over time")
ts = aggregate(df, ["bucket"], metric).sort_values("bucket")
fig_ts = px.line(ts, x="bucket", y="value", markers=True)
fig_ts.update_traces(line=dict(width=2, color=CATEGORICAL_PALETTE[0]))
fig_ts.update_layout(hovermode="x unified", xaxis_title="Date", yaxis_title=metric)
st.plotly_chart(fig_ts, width="stretch")
