# Scheduler MVP

Proves out the "GitHub Actions cron -> Gamma API -> filter -> remote Postgres"
automation end to end, on a deliberately tiny slice of the real pipeline:

- `status=open` markets only
- filtered to the 10 coins in `TOP10_COIN_IDS` (`scheduler/fetch_and_store.py`)
- 4 columns per market: `id`, `slug`, `volume`, `updated_at` (`scheduler/schema.sql`)
- same incremental idea as `src/data_collection/update_fetch.py` (only fetch
  what changed since last time), except the cutoff is read back from the
  `markets` table itself (`MAX(updated_at)`) instead of a local state file,
  since GitHub Actions runners don't keep disk between runs.

Runs on a cron in `.github/workflows/poll_markets.yml` (every 10 minutes;
Polymarket's own data moves on roughly a 5-minute cadence, and GitHub's
schedule trigger isn't exact anyway, so 10 min is a sane test cadence).

## One-time setup

1. **Create a Supabase project.** supabase.com -> New project -> pick any
   name/region/password (save the password, you'll need it in step 2).
   Free tier is enough for this.

2. **Get the connection string.** In the project: Settings -> Database ->
   Connection string. Pick **Connection pooling**, mode **Transaction**
   (port `6543`), not the direct connection (port `5432`) -- Supabase's
   direct connection needs IPv6, which GitHub Actions runners don't have.
   Copy the URI and fill in the password you set in step 1.

3. **Create the table.** In Supabase: SQL Editor -> New query -> paste the
   contents of `scheduler/schema.sql` -> Run. (The script also runs
   `CREATE TABLE IF NOT EXISTS` itself on every run, so this step is really
   just so you can see the empty table before any data lands.)

4. **Add the connection string as a GitHub secret.** In your GitHub repo:
   Settings -> Secrets and variables -> Actions -> New repository secret.
   Name: `DATABASE_URL`. Value: the URI from step 2.

5. **Push this branch/these files to GitHub** (I'll do this once you confirm
   -- see chat). The workflow only starts firing once it exists on the
   default branch.

6. **Trigger one run by hand** to test without waiting for the cron: repo ->
   Actions tab -> "Poll Polymarket crypto markets" -> Run workflow. Check the
   run's logs, then check Supabase -> Table Editor -> `markets` for rows.

## What to expect

- First run has no cutoff yet (`MAX(updated_at)` is `NULL`), so it scans
  every currently-open market on Polymarket (tens of thousands) and keeps
  only the ~3,000 that match the top-10 coin list. Takes under a minute.
- Every run after that only scans markets updated since the last run's
  newest `updated_at` -- a handful of pages, seconds.
- GitHub's cron isn't exact -- expect runs some minutes later than
  scheduled, especially at busy times. Fine for this test.

## Known MVP limitations (not bugs, just scope)

- Only tracks markets while they're `open`. A market that closes between
  runs stops getting updated in the table (its last-known volume goes
  stale) -- the full pipeline's `closed`-status sweep isn't replicated here.
- No history: same "latest snapshot wins" trade-off as `docs/sql_schema.md`.
- Coin list is hardcoded (`TOP10_COIN_IDS`), not derived from actual
  popularity ranking.
