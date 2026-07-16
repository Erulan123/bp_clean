# ---------- Description ----------

"""
Shared constants and cached data-loading helpers used by both pages
(views/home.py, views/top_markets.py). Kept in one place so the coin
ranking/color-map logic -- and the DB connection point -- aren't
duplicated across page files.

Connects to the dashboard's own Supabase Postgres project (see
schema.sql/build_remote_db.py) -- a separate project from the one the
scheduler MVP (scheduler/) writes to. The connection string is read from
Streamlit secrets, never committed to git: locally via a gitignored
.streamlit/secrets.toml, on Streamlit Community Cloud via the app's
Secrets panel. Both populate st.secrets identically.
"""

# ---------- Imports ----------

import psycopg2
import pandas as pd
import streamlit as st

# ---------- Base parameters ----------

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


# ---------- Connection ----------

def get_connection():
    return psycopg2.connect(st.secrets["DATABASE_URL"])


# ---------- Data loading ----------

@st.cache_data
def load_coin_options():
    """Every coin that has at least one market, ranked by all-time total
    volume (stable regardless of the period picked) -- this ranking drives
    both the checklist order/numbering and the default top-N checked."""
    conn = get_connection()
    df = pd.read_sql(
        "SELECT c.id, c.name, SUM(m.volume) AS total_volume "
        "FROM coins c "
        "JOIN markets m ON m.coin_id = c.id "
        "GROUP BY c.id ORDER BY total_volume DESC",
        conn,
    )
    conn.close()
    ranked_coins = df[["id", "name"]].to_dict("records")
    default_ids = set(df["id"].head(DEFAULT_TOP_N))
    color_map = dict(zip(df["id"].head(len(CATEGORICAL_PALETTE)), CATEGORICAL_PALETTE))
    return ranked_coins, default_ids, color_map


def coin_picker(ranked_coins, default_coin_ids, key_prefix=""):
    """Renders the same per-coin checkbox list used on both pages, numbered
    by all-time volume rank. Returns the tuple of selected coin ids."""
    selected_ids = []
    for i, coin in enumerate(ranked_coins, start=1):
        checked = st.checkbox(
            f"{i}. {coin['name']}", value=coin["id"] in default_coin_ids,
            key=f"{key_prefix}coin_{coin['id']}",
        )
        if checked:
            selected_ids.append(coin["id"])
    return tuple(selected_ids)
