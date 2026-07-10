# ---------- Description ----------

"""
Incremental sync of Polymarket markets, paired with historic_fetch.py: fetches
everything whose updatedAt has changed since the last time this ran, newest-
updated first, stopping as soon as it reaches a market it has already seen.

Each run is tagged with the same --run-id used for historic_fetch.py, so an
update run always knows exactly which historic crawl it's following up on
(state files for the two live side by side: states/historic_fetch/{status}_
run{run_id}_state.jsonl and states/updates/{status}_run{run_id}_updates_state
.jsonl).

Bootstrap rule (first update run ever for this run_id, no state of its own
yet): the cutoff is historic_fetch.py's own first_started_at, not its finish
time. historic_fetch.py's crawl can, in theory, miss a market that gets
updated while the crawl is still running (its updatedAt jumps to "now", which
can land in a position the crawl has already scanned past). Using the
historic crawl's START time as this script's first cutoff means this run's
scan window fully overlaps the entire duration that crawl was running, so
anything that changed mid-crawl -- caught by the crawl or not -- is
guaranteed to be re-swept here. (Using the crawl's finish time instead would
leave exactly that gap open.)

Every run after the first uses the previous run's own start time as its
cutoff, for the same reason: it guarantees each run's scan window overlaps
the entire duration the previous run was executing.

Every run starts fresh from cursor=None (never carries a cursor across runs)
and pages backward until it crosses the cutoff. A run's progress is only
made durable (state appended, output file kept) if it completes in full --
see the note above append_run_state for why a partial run must not be
checkpointed. Output is a single (unchunked) file per run.

--overshoot N is a verification knob, not a production setting: instead of
stopping at the first market at/below the cutoff, it keeps collecting up to
N more such markets, and checks that none of them is followed by a market
that's unexpectedly back above the cutoff. That would mean the API's
"updatedAt descending" ordering isn't as strictly monotonic as this whole
cutoff-stop strategy assumes -- see tests/updatedAt_test.ipynb, which probes
the same assumption. Leave it at 0 for real runs.

CLI usage:
    python update_fetch.py --status open
    python update_fetch.py --status open --run-id 2
    python update_fetch.py --status open --overshoot 1000   # verification only

Programmatic usage:
    from update_fetch import fetch_updates
    fetch_updates(status="open")
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

BASE_URL = "https://gamma-api.polymarket.com/markets/keyset"
LIMIT = 100
PROGRESS_EVERY = 1000
MAX_RETRIES = 3

DATA_DIR = os.path.join(REPO_ROOT, "data", "raw", "updates")
STATE_DIR = os.path.join(REPO_ROOT, "states", "updates")
HISTORIC_STATE_DIR = os.path.join(REPO_ROOT, "states", "historic_fetch")


# ---------- Helper functions ----------

def output_path(status, run_id, timestamp):
    return os.path.join(DATA_DIR, status, f"{status}_run{run_id}_update_{timestamp}.jsonl")


def own_state_path(status, run_id):
    return os.path.join(STATE_DIR, f"{status}_run{run_id}_updates_state.jsonl")


def historic_state_path(status, run_id):
    return os.path.join(HISTORIC_STATE_DIR, f"{status}_run{run_id}_state.jsonl")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def filename_timestamp():
    # ':' isn't legal in Windows filenames, so this keeps the same
    # YYYY-MM-DDTHH-MM-SS.ffffff shape as isoformat() with colons swapped for hyphens.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%f")


def load_last_jsonl_line(path):
    """Same "last valid line wins" idiom as historic_fetch.py's checkpoint
    log -- if a line is truncated mid-write by a crash, the one before it is
    still a fully-committed, trustworthy checkpoint."""
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


def determine_cutoff(status, run_id):
    """Returns (cutoff_time, is_bootstrap). See module docstring for why the
    bootstrap cutoff is historic_fetch.py's first_started_at rather than its
    finish time."""
    own_last = load_last_jsonl_line(own_state_path(status, run_id))
    if own_last:
        return own_last["run_started_at"], False

    historic_last = load_last_jsonl_line(historic_state_path(status, run_id))
    if not historic_last or not historic_last.get("done"):
        raise SystemExit(
            f"historic fetch for '{status}' run {run_id} isn't complete yet -- "
            f"run historic_fetch.py --status {status} --run-id {run_id} first."
        )
    return historic_last["first_started_at"], True


def append_run_state(status, run_id, run_started_at, run_finished_at, is_bootstrap,
                      cutoff_time_used, total_fetched, overshoot_fetched,
                      ordering_violations, stop_reason, output_file):
    """Appended ONCE, only when a run completes in full. A run's
    run_started_at becomes the NEXT run's cutoff, so checkpointing a run that
    was interrupted partway through would advance the cutoff past ground that
    was never actually scanned -- permanently reopening the exact gap this
    script exists to close. Since every run starts fresh from cursor=None
    anyway, the only safe recovery from an interrupted run is to simply rerun
    it against the same old cutoff, which falls out for free as long as
    nothing gets appended here on failure."""
    entry = {
        "status": status,
        "run_id": run_id,
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "is_bootstrap": is_bootstrap,
        "cutoff_time_used": cutoff_time_used,
        "total_fetched": total_fetched,
        "overshoot_fetched": overshoot_fetched,
        "ordering_violations": ordering_violations,
        "stop_reason": stop_reason,
        "output_file": output_file,
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(own_state_path(status, run_id), "a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------- Pipeline functions ----------

def fetch_page(session, closed_str, after_cursor):
    params = {
        "limit": LIMIT,
        "order": "updatedAt",
        "ascending": "false",  # newest -> oldest, so the cutoff check can stop early
        "closed": closed_str,
    }
    if after_cursor:
        params["after_cursor"] = after_cursor

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("markets", []), data.get("next_cursor")
        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)


def fetch_updates(status, run_id=1, progress_every=PROGRESS_EVERY, overshoot=0):
    """Runs one full incremental sync for the given status/run_id. Returns a
    small summary dict so callers don't have to parse stdout.

    overshoot=0 is the production path: stop at the first market at/below
    cutoff_time. overshoot=N keeps going for N more such markets purely to
    verify the ordering assumption the cutoff-stop relies on (see module
    docstring) -- those extra markets are written to the same output file.
    """
    closed_str = "true" if status == "closed" else "false"
    cutoff_time, is_bootstrap = determine_cutoff(status, run_id)

    run_started_at = now_iso()  # recorded BEFORE fetching -> becomes next run's cutoff
    timestamp = filename_timestamp()
    os.makedirs(os.path.join(DATA_DIR, status), exist_ok=True)
    final_path = output_path(status, run_id, timestamp)
    tmp_path = final_path + ".tmp"

    print(f"Starting {status} update sync (run {run_id}) | bootstrap={is_bootstrap} | "
          f"cutoff={cutoff_time}" + (f" | overshoot={overshoot}" if overshoot else ""))

    session = requests.Session()
    start_time = time.time()
    after_cursor = None
    total_fetched = 0       # markets strictly newer than cutoff_time -- the real update set
    overshoot_fetched = 0   # extra at/below-cutoff markets kept for verification only
    ordering_violations = 0
    crossed_at = None       # index (within total_fetched) where we first saw updatedAt <= cutoff
    last_updated_at = None
    stop_reason = None

    try:
        with open(tmp_path, "w") as f:
            while True:
                markets, next_cursor = fetch_page(session, closed_str, after_cursor)
                if not markets:
                    stop_reason = "no more markets"
                    break

                hit_cutoff = False
                for m in markets:
                    updated_at = m.get("updatedAt")

                    # Global monotonicity check: order=updatedAt&ascending=false should
                    # never show an increase. A violation here would mean the cutoff-stop
                    # strategy this whole script relies on isn't actually safe.
                    if last_updated_at is not None and updated_at > last_updated_at:
                        ordering_violations += 1
                        print(f"  !! ordering violation: market {m.get('id')} updatedAt={updated_at} "
                              f"came after updatedAt={last_updated_at} in the same page/run")
                    last_updated_at = updated_at

                    if updated_at > cutoff_time:
                        if crossed_at is not None:
                            # we'd already crossed at/below cutoff and now we're back above it --
                            # exactly the reappearance case that would make stopping early unsafe.
                            ordering_violations += 1
                            print(f"  !! ordering violation: market {m.get('id')} updatedAt={updated_at} "
                                  f"reappeared ABOVE cutoff after we'd already crossed it")
                        f.write(json.dumps(m) + "\n")
                        total_fetched += 1
                        continue

                    if crossed_at is None:
                        crossed_at = total_fetched
                    if overshoot_fetched >= overshoot:
                        hit_cutoff = True
                        break
                    f.write(json.dumps(m) + "\n")
                    overshoot_fetched += 1

                if total_fetched and total_fetched % progress_every < LIMIT:
                    print(f"  {total_fetched} markets fetched | {time.time() - start_time:.1f}s elapsed")

                if hit_cutoff:
                    stop_reason = "reached cutoff" if overshoot == 0 else \
                        f"reached cutoff + {overshoot_fetched} overshoot (crossed at index {crossed_at})"
                    break
                if not next_cursor:
                    stop_reason = "no next_cursor"
                    break
                after_cursor = next_cursor

    except (KeyboardInterrupt, requests.RequestException) as e:
        reason = "Interrupted" if isinstance(e, KeyboardInterrupt) else f"Network error: {e}"
        print(f"\n{reason} after {total_fetched} markets this run. "
              f"No checkpoint written -- rerun will retry the same cutoff window from scratch. "
              f"Partial output left at {tmp_path}.")
        return {"status": status, "run_id": run_id, "total_fetched": total_fetched, "done": False,
                "stop_reason": "interrupted"}

    os.replace(tmp_path, final_path)
    run_finished_at = now_iso()
    append_run_state(status, run_id, run_started_at, run_finished_at, is_bootstrap,
                      cutoff_time, total_fetched, overshoot_fetched, ordering_violations,
                      stop_reason, final_path)

    print(f"Done. {total_fetched} markets synced ({stop_reason}) -> {final_path}")
    if overshoot:
        print(f"  ordering violations found: {ordering_violations} "
              f"(0 means it's safe to stop exactly at the cutoff)")

    return {
        "status": status, "run_id": run_id, "total_fetched": total_fetched,
        "overshoot_fetched": overshoot_fetched, "ordering_violations": ordering_violations,
        "done": True, "stop_reason": stop_reason, "output_file": final_path,
    }


# ---------- Entry point ----------

def main():
    parser = argparse.ArgumentParser(description="Incremental sync of Polymarket markets, newest-updated first.")
    parser.add_argument("--status", choices=["open", "closed"], required=True)
    parser.add_argument("--run-id", type=int, default=1)
    parser.add_argument("--overshoot", type=int, default=0,
                         help="verification only: keep collecting N markets past the cutoff instead of "
                              "stopping immediately (default 0 = normal production behavior)")
    args = parser.parse_args()
    fetch_updates(status=args.status, run_id=args.run_id, overshoot=args.overshoot)


if __name__ == "__main__":
    main()
