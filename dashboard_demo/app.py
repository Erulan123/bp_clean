#!/usr/bin/env python3
# ---------- Description ----------

"""
Crypto Markets Dashboard -- entry point.

Just routes between the two pages (views/home.py, views/top_markets.py);
all actual page content lives in views/, shared helpers in common.py.

Run:
    streamlit run app.py
"""

# ---------- Imports ----------

import streamlit as st

st.set_page_config(page_title="Crypto Markets Dashboard", layout="wide")

home = st.Page("views/home.py", title="Home", icon=":material/home:", default=True)
top_markets = st.Page("views/top_markets.py", title="Top Markets", icon=":material/leaderboard:")

pg = st.navigation([home, top_markets])
pg.run()
