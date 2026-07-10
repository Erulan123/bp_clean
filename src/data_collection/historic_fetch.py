# ---------- Description ----------

"""
One-time (but resumable) historic crawl of Polymarket markets via the gamma
keyset API, ordered by id from highest to lowest (newest to oldest).

Each run is tagged with a run_id (1 by default). Resuming the same run_id
continues exactly where it left off; passing a different --run-id starts an
independent crawl with its own data and state files, without touching an
earlier run's progress. --fresh wipes a run_id's data and state files so it
starts over from scratch instead of resuming.

CLI usage:
    python historic_fetch.py --status open
    python historic_fetch.py --status closed
    python historic_fetch.py --status open --run-id 2
    python historic_fetch.py --status open --fresh

Programmatic usage:
    from historic_fetch import fetch_historic_markets
    fetch_historic_markets(status="open")

Data is written in CHUNK_SIZE-sized files (open_run1_001.jsonl, _002.jsonl,
...) instead of one giant growing file, so only the chunk currently being
written can ever need trimming on restart -- every earlier chunk already has
exactly CHUNK_SIZE confirmed lines and is never touched again.

The state file holds exactly one checkpoint: the current cursor, chunk
position, and totals. Every page fetched (LIMIT markets) is written to the
chunk file, fsynced, and the state file is atomically rewritten (temp file +
rename) to match -- so the two are always in sync, and a crash can only ever
cost you the one page in flight. Safe to interrupt and rerun any time.
"""

# ---------- Imports ----------

import argparse
import json
import os
import time
import requests
from datetime import datetime, timezone

# ---------- Base parameters ----------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAMMA_URL = "https://gamma-api.polymarket.com/markets/keyset"
LIMIT = 100
CHUNK_SIZE = 100_000   # markets per data file
PROGRESS_EVERY = 1000  # print a progress line roughly every N markets
MAX_RETRIES = 3        # retries for a transient network error before giving up on this run

DATA_DIR = os.path.join(REPO_ROOT, "data", "raw", "historic_fetch")
STATE_DIR = os.path.join(REPO_ROOT, "states", "historic_fetch")


# ---------- Helper functions ----------

def chunk_path(status, run_id, chunk_index):
    return os.path.join(DATA_DIR, status, f"{status}_run{run_id}_{chunk_index:03d}.jsonl")


def state_path(status, run_id):
    return os.path.join(STATE_DIR, f"{status}_run{run_id}_state.jsonl")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- Safety functions ----------

def load_state(status, run_id):
    """Reads the checkpoint. Tolerates both the current single-line state
    file and an older append-only log (reads the last valid line) -- either
    way only the most recent line matters, and a crash can only ever
    truncate that one, so a JSON error there just falls back to the line
    before it."""
    path = state_path(status, run_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        lines = [line for line in f if line.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def save_state(state):
    """Atomically overwrites the state file with this one checkpoint entry
    (temp file + rename) instead of appending -- the file never grows, so
    checkpointing every single page for a multi-hundred-thousand-market
    crawl still costs one small write each, not an ever-growing log. Windows
    can momentarily lock the destination during the rename, so retry
    briefly."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_path(state["status"], state["run_id"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(state) + "\n")
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2)
    os.replace(tmp, path)  # last attempt: let it raise if still locked


def count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f)


def trim_chunk_file(path, expected_lines):
    """Trim the current chunk file back to exactly the number of lines the
    last checkpoint confirmed, discarding any unconfirmed tail left by a
    previous interrupted run. Done atomically so a crash mid-trim can't
    corrupt the file."""
    lines_present = count_lines(path)
    if lines_present == expected_lines:
        return
    if lines_present < expected_lines:
        print(f"!! {path} has {lines_present} lines but state expects {expected_lines}. Stopping -- investigate.")
        raise SystemExit(1)

    with open(path) as f:
        lines = f.readlines()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(lines[:expected_lines])
    os.replace(tmp, path)
    print(f"Trimmed {path}: discarded {lines_present - expected_lines} unconfirmed line(s) from a previous interrupted run.")


def clean_run(status, run_id):
    """Deletes every data chunk and the state file belonging to this
    status/run_id combo, so the next call starts from scratch instead of
    resuming. Only touches files tagged with this exact run_id -- other
    runs (and the other status) are untouched."""
    removed = []
    data_dir = os.path.join(DATA_DIR, status)
    prefix = f"{status}_run{run_id}_"
    if os.path.isdir(data_dir):
        for name in os.listdir(data_dir):
            if name.startswith(prefix) and name.endswith(".jsonl"):
                path = os.path.join(data_dir, name)
                os.remove(path)
                removed.append(path)

    sp = state_path(status, run_id)
    if os.path.exists(sp):
        os.remove(sp)
        removed.append(sp)

    if removed:
        print(f"--fresh: removed {len(removed)} file(s) for {status} run {run_id}:")
        for p in removed:
            print(f"  {p}")
    else:
        print(f"--fresh: nothing to remove for {status} run {run_id} (already clean).")


def checkpoint(out, done, status, run_id, first_started_at, checkpoint_started_at,
               chunk_index, lines_in_chunk, next_cursor, fetched_this_run, total_fetched):
    """fsyncs the chunk file currently being written (without closing it, so
    the crawl can keep appending to it) and atomically rewrites the state
    file. Called after every page, so state and data are always in sync."""
    out.flush()
    os.fsync(out.fileno())
    save_state({
        "status": status,
        "run_id": run_id,
        "first_started_at": first_started_at,
        "checkpoint_started_at": checkpoint_started_at,
        "checkpoint_written_at": now_iso(),
        "chunk_index": chunk_index,
        "lines_in_chunk": lines_in_chunk,
        "next_cursor": next_cursor,
        "fetched_this_run": fetched_this_run,
        "total_fetched": total_fetched,
        "done": done,
    })


def checkpoint_and_close(out, done, status, run_id, first_started_at, checkpoint_started_at,
                          chunk_index, lines_in_chunk, next_cursor, fetched_this_run, total_fetched):
    """Same as checkpoint(), but also closes the chunk file -- used once
    this chunk is finished (chunk boundary, run completion, or an
    interruption)."""
    checkpoint(out, done, status, run_id, first_started_at, checkpoint_started_at,
               chunk_index, lines_in_chunk, next_cursor, fetched_this_run, total_fetched)
    out.close()


# ---------- Pipeline functions ----------

def fetch_page(session, closed_str, after_cursor=None):
    """Fetches one page of markets, newest-by-id first."""
    params = {
        "limit": LIMIT,
        "order": "id",
        "ascending": "false",  # newest -> oldest
        "closed": closed_str,
    }
    if after_cursor:
        params["after_cursor"] = after_cursor

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(GAMMA_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("markets", []), data.get("next_cursor")
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)


def fetch_historic_markets(status, chunk_size=CHUNK_SIZE, run_id=1, progress_every=PROGRESS_EVERY, fresh=False):
    """Crawls every market of the given status, newest-by-id first, picking
    up wherever run_id last left off (or starting over if fresh=True).
    Returns a small summary dict so callers don't have to parse stdout."""
    closed_str = "true" if status == "closed" else "false"
    os.makedirs(os.path.join(DATA_DIR, status), exist_ok=True)

    if fresh:
        clean_run(status, run_id)

    state = load_state(status, run_id)
    if state and state["done"]:
        print(f"{status} (run {run_id}): already fully fetched ({state['total_fetched']} markets). Nothing to do.")
        return {"status": status, "run_id": run_id, "total_fetched": state["total_fetched"], "done": True}

    # first_started_at is only recorded once this run_id's first fetch_page()
    # call has actually succeeded (not before) -- a run that never manages to
    # complete a single request leaves it unset, so it stays exactly what it
    # says: the time historic data collection actually began, not the time we
    # merely decided to start. Once set, it's carried forward unchanged.
    first_started_at = state["first_started_at"] if state else None
    after_cursor = state["next_cursor"] if state else None
    total_fetched = state["total_fetched"] if state else 0
    chunk_index = state["chunk_index"] if state else 1
    # older state files predate the lines_in_chunk field -- fall back to
    # deriving it from total_fetched for those
    lines_in_chunk = state["lines_in_chunk"] if state and "lines_in_chunk" in state else total_fetched % chunk_size
    fetched_this_run = 0
    checkpoint_started_at = now_iso()

    # Trim the chunk we're resuming into back to exactly what the last
    # checkpoint confirmed (discarding any unconfirmed tail from a crash),
    # then roll over to a fresh chunk file if that one was already full.
    trim_chunk_file(chunk_path(status, run_id, chunk_index), lines_in_chunk)
    if lines_in_chunk >= chunk_size:
        chunk_index += 1
        lines_in_chunk = 0

    print(f"Starting {status} historic fetch (run {run_id}) | {total_fetched} markets already saved "
          f"(chunk {chunk_index:03d}) | resuming from cursor={after_cursor}")

    session = requests.Session()
    start_time = time.time()
    out = open(chunk_path(status, run_id, chunk_index), "a")

    try:
        while True:
            markets, next_cursor = fetch_page(session, closed_str, after_cursor)
            if first_started_at is None:
                first_started_at = now_iso()
            if not markets:
                break

            for m in markets:
                out.write(json.dumps(m) + "\n")

            total_fetched += len(markets)
            fetched_this_run += len(markets)
            lines_in_chunk += len(markets)
            after_cursor = next_cursor
            last_market = markets[-1]

            # Fsync the chunk file and rewrite the state file together, every
            # page -- this is the "atomic save" that keeps next_cursor and
            # total_fetched on disk in lockstep with the data.
            checkpoint(out, False, status, run_id, first_started_at, checkpoint_started_at,
                       chunk_index, lines_in_chunk, after_cursor, fetched_this_run, total_fetched)

            if total_fetched % progress_every < LIMIT:
                elapsed = time.time() - start_time
                rate = fetched_this_run / elapsed if elapsed > 0 else 0
                print(f"  {total_fetched} markets total ({fetched_this_run} this run) | "
                      f"{elapsed:.1f}s elapsed | {rate:.1f} markets/sec | "
                      f"last id={last_market.get('id')} createdAt={last_market.get('createdAt')}")

            if not next_cursor:
                break

            # crossed a chunk boundary -> seal this chunk, start a fresh one
            if lines_in_chunk >= chunk_size:
                out.close()
                print(f"  chunk {chunk_index:03d} sealed at {lines_in_chunk} markets")
                chunk_index += 1
                lines_in_chunk = 0
                out = open(chunk_path(status, run_id, chunk_index), "a")

    except KeyboardInterrupt:
        checkpoint_and_close(out, False, status, run_id, first_started_at, checkpoint_started_at,
                              chunk_index, lines_in_chunk, after_cursor, fetched_this_run, total_fetched)
        print(f"\nInterrupted at {total_fetched} markets ({fetched_this_run} fetched this run). "
              f"Progress saved -- rerun the same command to resume.")
        return {"status": status, "run_id": run_id, "total_fetched": total_fetched, "done": False}
    except requests.RequestException as e:
        checkpoint_and_close(out, False, status, run_id, first_started_at, checkpoint_started_at,
                              chunk_index, lines_in_chunk, after_cursor, fetched_this_run, total_fetched)
        print(f"\nNetwork error after {total_fetched} markets: {e}. Progress saved -- rerun to resume.")
        return {"status": status, "run_id": run_id, "total_fetched": total_fetched, "done": False}

    checkpoint_and_close(out, True, status, run_id, first_started_at, checkpoint_started_at,
                          chunk_index, lines_in_chunk, after_cursor, fetched_this_run, total_fetched)
    print(f"Done. {total_fetched} total {status} markets saved across {chunk_index} file(s) (run {run_id}).")
    return {"status": status, "run_id": run_id, "total_fetched": total_fetched, "done": True}


def main():
    parser = argparse.ArgumentParser(description="Historic crawl of Polymarket markets, newest-by-id first.")
    parser.add_argument("--status", choices=["open", "closed"], required=True)
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--run-id", type=int, default=1)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    fetch_historic_markets(status=args.status, chunk_size=args.chunk_size, run_id=args.run_id, fresh=args.fresh)


if __name__ == "__main__":
    main()
