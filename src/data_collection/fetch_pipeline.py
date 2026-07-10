# ---------- Description ----------

"""
Single entry point for the market collection pipeline built on historic_fetch.py
and update_fetch.py: figures out the one next step for a given status/run_id
and runs it, so you don't have to remember whether the historic crawl has
finished before running an update.

No state logic is duplicated here -- each decision just delegates to the
existing functions, which are already idempotent/resumable on their own:
    - fetch_historic_markets(status, run_id) starts fresh, resumes, or is a
      no-op if already done -- whichever applies.
    - fetch_updates(status, run_id) bootstraps off historic_fetch.py's
      first_started_at on the first call, and off its own last run_started_at
      on every call after -- but only once the historic fetch is done (see
      update_fetch.py's determine_cutoff).

So running this repeatedly (cron, task scheduler, or by hand) always makes
exactly one unit of forward progress: start/resume the historic crawl if it
isn't done yet, otherwise run the next incremental update.

CLI usage:
    python fetch_pipeline.py --status both             # advance both open and closed
    python fetch_pipeline.py --status open --run-id 2
    python fetch_pipeline.py --status both --check      # print stats + next action only
"""

# ---------- Imports ----------

import argparse
import json
import os

from historic_fetch import fetch_historic_markets, state_path as historic_state_path
from update_fetch import fetch_updates, own_state_path as update_state_path


# ---------- Helper functions ----------

def load_last_jsonl_line(path):
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
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def next_action(hist, updates):
    if not hist or not hist.get("done"):
        return "start historic fetch" if not hist else "resume historic fetch"
    if not updates:
        return "run bootstrap update"
    return "run next incremental update"


# ---------- Check mode ----------

def print_status(status, run_id):
    hist = load_last_jsonl_line(historic_state_path(status, run_id))
    updates = load_all_jsonl_lines(update_state_path(status, run_id))

    print(f"=== {status} (run {run_id}) ===")
    if not hist:
        print("  historic fetch : not started")
    else:
        print(f"  historic fetch : done={hist['done']} | total_fetched={hist['total_fetched']} | "
              f"chunks={hist['chunk_index']} | first_started_at={hist['first_started_at']}")

    if not updates:
        print("  updates        : none yet")
    else:
        print(f"  updates        : {len(updates)} run(s)")
        for u in updates:
            overshoot = f" (+{u['overshoot_fetched']} overshoot)" if u.get("overshoot_fetched") else ""
            print(f"    run_started_at={u['run_started_at']} | bootstrap={u['is_bootstrap']} | "
                  f"fetched={u['total_fetched']}{overshoot} | stop={u['stop_reason']}")

    print(f"  next action    : {next_action(hist, updates)}")
    print()


# ---------- Run mode ----------

def run_pipeline_step(status, run_id):
    hist_result = fetch_historic_markets(status, run_id=run_id)
    if hist_result["done"]:
        fetch_updates(status, run_id=run_id)
    else:
        print(f"[{status}] historic fetch not finished this run -- skipping update, rerun later.")


# ---------- Entry point ----------

def main():
    parser = argparse.ArgumentParser(description="Run (or check) the next step of the market collection pipeline.")
    parser.add_argument("--status", choices=["open", "closed", "both"], default="both")
    parser.add_argument("--run-id", type=int, default=1)
    parser.add_argument("--check", action="store_true",
                         help="print stats and the next action for each status, without running anything")
    args = parser.parse_args()

    statuses = ("open", "closed") if args.status == "both" else (args.status,)

    if args.check:
        for status in statuses:
            print_status(status, args.run_id)
        return

    for status in statuses:
        run_pipeline_step(status, args.run_id)


if __name__ == "__main__":
    main()
