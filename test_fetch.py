"""Quick connection/throughput test: fetches closed markets and prints
speed every 1000 markets. Doesn't save anything to disk."""

import time
import requests

from src.data_collection.historic_fetch import fetch_page

N_MARKETS = 5000
PROGRESS_EVERY = 1000

session = requests.Session()
cursor = None
fetched = 0
start = time.time()
last_checkpoint = start

while fetched < N_MARKETS:
    markets, cursor = fetch_page(session, "true", cursor)
    if not markets:
        print("No more markets available.")
        break
    fetched += len(markets)

    if fetched % PROGRESS_EVERY < len(markets):
        now = time.time()
        interval_rate = PROGRESS_EVERY / (now - last_checkpoint)
        overall_rate = fetched / (now - start)
        print(f"{fetched} markets | interval {interval_rate:.1f}/s | overall {overall_rate:.1f}/s")
        last_checkpoint = now

    if not cursor:
        break

elapsed = time.time() - start
print(f"\nDone: {fetched} markets in {elapsed:.1f}s ({fetched / elapsed:.1f} markets/sec)")
