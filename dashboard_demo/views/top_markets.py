# ---------- Description ----------

"""
Top Markets page -- the single highest-volume individual markets in a
selected period, filtered by coin. Complements the Home page (which
aggregates across all markets) by surfacing specific standout markets.

Same period definition as Home: a market counts as "in period" only if its
entire lifetime (start_date to end_date) falls inside it.
"""

# ---------- Imports ----------

import pandas as pd
import plotly.express as px
import streamlit as st

from common import DEFAULT_START, DEFAULT_END, FALLBACK_COLOR, get_connection, load_coin_options, coin_picker

MIN_N, MAX_N, DEFAULT_N, STEP_N = 20, 50, 30, 5
LABEL_MAX_LEN = 60


# ---------- Data loading ----------

@st.cache_data
def load_top_markets(coin_ids: tuple, start: str, end_exclusive: str, top_n: int) -> pd.DataFrame:
    if not coin_ids:
        return pd.DataFrame(columns=["id", "coin_name", "question", "slug", "volume", "liquidity"])
    conn = get_connection()
    query = (
        "SELECT m.id, c.name AS coin_name, m.question, m.slug, m.start_date, m.end_date, "
        "m.volume, m.liquidity "
        "FROM markets m "
        "JOIN coins c ON c.id = m.coin_id "
        "WHERE m.coin_id = ANY(%s) AND m.start_date >= %s AND m.end_date < %s "
        "ORDER BY m.volume DESC "
        f"LIMIT {int(top_n)}"
    )
    df = pd.read_sql(query, conn, params=[list(coin_ids), start, end_exclusive])
    conn.close()
    df["volume"] = df["volume"].fillna(0)
    df["liquidity"] = df["liquidity"].fillna(0)
    return df


def display_label(row) -> str:
    text = row["question"] or row["slug"] or row["id"]
    return text if len(text) <= LABEL_MAX_LEN else text[:LABEL_MAX_LEN - 1] + "…"


# ---------- Page ----------

st.title("Top Markets")
st.caption(
    "The single highest-volume markets in the selected period and coins -- not an "
    "aggregate, each row is one market. Same period rule as Home: a market must start "
    "and end entirely within the selected range."
)

ranked_coins, default_coin_ids, color_map = load_coin_options()

with st.sidebar:
    st.header("Filters")
    start_date = st.date_input("Start date", value=DEFAULT_START, key="top_start")
    end_date = st.date_input("End date", value=DEFAULT_END, key="top_end")
    top_n = st.slider("Number of markets", MIN_N, MAX_N, DEFAULT_N, STEP_N, key="top_n")

    st.header("Cryptocurrencies")
    selected_ids = coin_picker(ranked_coins, default_coin_ids, key_prefix="top_")

if start_date > end_date:
    st.warning("Start date is after end date.")
    st.stop()
if not selected_ids:
    st.warning("Select at least one cryptocurrency.")
    st.stop()

end_exclusive = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

df = load_top_markets(selected_ids, str(start_date), end_exclusive, top_n)

if df.empty:
    st.warning("No markets found for this selection.")
    st.stop()

df = df.sort_values("volume", ascending=False).reset_index(drop=True)
df["label"] = df.apply(display_label, axis=1)
name_color_map = {name: color_map.get(cid, FALLBACK_COLOR) for cid, name in
                   {c["id"]: c["name"] for c in ranked_coins}.items()}

st.subheader(f"Top {len(df)} markets by volume")
fig = px.bar(
    df.sort_values("volume"), x="volume", y="label", color="coin_name",
    color_discrete_map=name_color_map, orientation="h",
)
fig.update_layout(
    xaxis_title="Volume", yaxis_title=None, showlegend=True,
    height=max(400, 24 * len(df)),
)
st.plotly_chart(fig, width="stretch")

st.subheader("Details")
st.dataframe(
    df[["coin_name", "question", "slug", "volume", "liquidity", "start_date", "end_date"]].rename(
        columns={"coin_name": "Coin", "question": "Question", "slug": "Slug",
                 "volume": "Volume", "liquidity": "Liquidity",
                 "start_date": "Start", "end_date": "End"}
    ),
    width="stretch",
    hide_index=True,
)
