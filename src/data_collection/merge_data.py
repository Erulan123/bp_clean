# ---------- Description ----------

"""
Combines the historic crawl and every update run into one file:
data/processed/all_markets.jsonl -- exactly one row per market id, holding
whichever version of it has the newest updatedAt.

Merging is incremental. The first run merges every finished historic crawl
it finds, then every update run, oldest first. Every run after that starts
from the all_markets.jsonl already on disk and only reads the update files
it hasn't merged yet -- it never rereads the (large, static) historic chunks
or old update files again. states/merge/merge_state.jsonl remembers exactly
which historic state files and which update files have already been folded
in, plus the time of each merge. If nothing new has finished fetching since
the last merge, running this again does nothing.

CLI usage:
    python merge_data.py
"""

# ---------- Imports ----------

import json
import os
from datetime import datetime, timezone

from historic_fetch import chunk_path

# ---------- Base parameters ----------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HISTORIC_STATE_DIR = os.path.join(REPO_ROOT, "states", "historic_fetch")
UPDATES_STATE_DIR = os.path.join(REPO_ROOT, "states", "updates")
MERGE_STATE_PATH = os.path.join(REPO_ROOT, "states", "merge", "merge_state.jsonl")
OUTPUT_PATH = os.path.join(REPO_ROOT, "data", "processed", "all_markets.jsonl")


# ---------- Helper functions ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_last_jsonl_line(path):
    """Same "last valid line wins" idiom used across the pipeline scripts."""
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


def load_all_jsonl_lines(path):
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))
    return entries


def append_jsonl(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def already_merged(merge_log):
    """Every historic state file and every update file already folded into
    all_markets.jsonl, gathered from every past merge run."""
    historic_done, updates_done = set(), set()
    for entry in merge_log:
        historic_done.update(entry["historic_merged"])
        updates_done.update(entry["update_merged"])
    return historic_done, updates_done


def find_finished_historic_runs(state_dir):
    """One entry per status/run_id whose historic crawl is fully done."""
    runs = []
    if not os.path.isdir(state_dir):
        return runs
    for name in sorted(os.listdir(state_dir)):
        if not name.endswith(".jsonl"):
            continue
        state = load_last_jsonl_line(os.path.join(state_dir, name))
        if state and state["done"]:
            state["state_path"] = os.path.join(state_dir, name)
            runs.append(state)
    return runs


def find_update_runs(state_dir):
    """One entry per completed update run, oldest first, across every
    status/run_id -- each line names its own output_file."""
    entries = []
    if not os.path.isdir(state_dir):
        return entries
    for name in sorted(os.listdir(state_dir)):
        if name.endswith(".jsonl"):
            entries.extend(load_all_jsonl_lines(os.path.join(state_dir, name)))
    entries.sort(key=lambda e: e["run_started_at"])
    return entries


def read_markets(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def apply_markets(markets, path):
    """Folds every market in `path` into `markets`, keeping whichever
    version of a given id has the newer updatedAt."""
    n = 0
    for m in read_markets(path):
        existing = markets.get(m["id"])
        if existing is None or m.get("updatedAt", "") >= existing.get("updatedAt", ""):
            markets[m["id"]] = m
        n += 1
    return n


def write_markets(path, markets):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for m in markets.values():
            f.write(json.dumps(m) + "\n")
    os.replace(tmp, path)


# ---------- Pipeline function ----------

def merge_all_markets(historic_state_dir=HISTORIC_STATE_DIR, updates_state_dir=UPDATES_STATE_DIR,
                       merge_state_path=MERGE_STATE_PATH, output_path=OUTPUT_PATH):
    """Runs one merge pass. Returns a small summary dict so callers don't
    have to parse stdout."""
    merge_log = load_all_jsonl_lines(merge_state_path)
    historic_done, updates_done = already_merged(merge_log)

    new_historic = [r for r in find_finished_historic_runs(historic_state_dir)
                     if r["state_path"] not in historic_done]
    new_updates = [e for e in find_update_runs(updates_state_dir)
                    if e["output_file"] not in updates_done]

    if not new_historic and not new_updates:
        print("Nothing new to merge -- all_markets.jsonl is already up to date.")
        return {"merged": False, "new_historic": 0, "new_updates": 0}

    markets = {m["id"]: m for m in read_markets(output_path)}
    print(f"Starting from {len(markets)} markets already in {output_path}")

    for run in new_historic:
        n = 0
        for chunk in range(1, run["chunk_index"] + 1):
            n += apply_markets(markets, chunk_path(run["status"], run["run_id"], chunk))
        print(f"  merged historic {run['status']} run {run['run_id']}: {n} markets")

    for entry in new_updates:
        n = apply_markets(markets, entry["output_file"])
        print(f"  merged update {entry['status']} run {entry['run_id']} "
              f"({entry['run_started_at']}): {n} markets")

    write_markets(output_path, markets)
    append_jsonl(merge_state_path, {
        "merged_at": now_iso(),
        "historic_merged": [r["state_path"] for r in new_historic],
        "update_merged": [e["output_file"] for e in new_updates],
        "total_markets": len(markets),
    })

    print(f"Done. {len(markets)} total unique markets written to {output_path}")
    return {"merged": True, "new_historic": len(new_historic), "new_updates": len(new_updates),
            "total_markets": len(markets)}


# ---------- Entry point ----------

if __name__ == "__main__":
    merge_all_markets()
