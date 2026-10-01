# Data integrity model

**Principle:** no operation destroys the record. Corrections keep what was there before,
errors are reversible and excluded from analytics, and the plan that was in force when a
session started is preserved next to what was actually done.

## Schema (all additive — `ALTER TABLE ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`)

Applied by `_migrate_integrity()` in `database.py` on every boot. Idempotent: existing
columns are detected and skipped, and nothing is dropped, recreated, or rewritten. The one
backfill it performs only fills a NULL `source` on the legacy `backfill_YYYY-MM-DD` rows.

| Table | New columns |
|---|---|
| `workout_sets` | `deleted_at TIMESTAMP`, `deleted_reason`, `supersedes_set_id`, `source`, `performed_at` |
| `sessions` | `deleted_at TIMESTAMP`, `deleted_reason`, `source`, `planned_workout_snapshot JSONB` |
| `bodyweight` | `fasted BOOLEAN`, `time_of_day`, `off_protocol BOOLEAN` |
| `planned_workouts` | `superseded_at` |
| `set_deviations` | `set_id`, `source` (`auto`/`manual`), `superseded_at` |
| `set_history` (new) | prior values of a set before each update/delete/restore |
| `session_history` (new) | full prior session row before each update/end/delete/restore/reopen |

## Rules

- **Soft deletes.** `delete_set` / `delete_session` stamp `deleted_at` + `deleted_reason`.
  Deleting a session cascades to its live sets. `restore_set` / `restore_session` undo it
  (a session restore brings back only the sets the session delete removed). Every read
  filters `deleted_at IS NULL` unless `include_deleted=true`.
- **Analytic sets** = live and not superseded. Only these count toward totals, volume,
  set numbering and deviations. Superseded sets are returned under `superseded_sets`.
- **Session ids** are `session_{date}_{program}_{6 hex}`, so a session re-created on the
  same date + program can never adopt rows left behind by an earlier one.
- **Set ids** (integer) appear in every set payload: `get_history`, `get_session_log`
  (`set_ids`), `get_session_summaries`, `/api/session/current`, `log_set` (`set_id`).
- **Edits are versioned.** `update_set` and `update_session` write the prior values to
  `set_history` / `session_history` first. Past actuals stay editable.
- **Plans.** `set_planned_program` rejects any date before today (the whole request, nothing
  saved). Re-planning a date supersedes the old plan row instead of deleting it. Each
  session freezes the day's plan into `planned_workout_snapshot` when it starts.
- **Source** is one of `app`, `mcp`, `backfill`, `correction`.
- **performed_at** is when the set was done; defaults to now for live logging and stays
  NULL for backfill unless given. `timestamp`/`logged_at` remain the time it was recorded.
- **Session end** defaults to the last set's time, not "now". Backfill sessions only use
  real `performed_at` times; with none the end stays unknown.
- **Auto deviations.** `log_set` compares each set with the session's plan snapshot
  (weight, rep range, set count, unplanned exercise) and logs a `source='auto'` deviation.
  Editing the set re-evaluates it; the stale deviation is marked superseded.
- **Bodyweight.** `off_protocol` weigh-ins are kept but excluded from trend analytics
  (`include_off_protocol=false`, the prompt trend, `/api/activity-stats`).
- **Backfill.** `log_set(date=<past>)` / `start_session(date=<past>)` create a backfill
  session (`is_backfill`, source `backfill`) without closing today's live session. With no
  plan for that date it's actual-only (`get_planned_vs_actual` → `mode: actual_only`).
  Future dates are rejected.

Demo data is hard-deleted on end by design — it is never part of the athlete's record. Since
v5.0.5 every row written while a demo session is open (≤12 h old) carries `is_demo = 1`: sets,
sessions, bodyweight, measurements, progress photos, coach notes/briefs, added exercises, new goals.
Edits to real goals during a demo are snapshotted as `demo_update` in `goal_history`.
`wipe_demo_data()` (demo end, or any real session start) deletes every `is_demo = 1` row and every
`demo_`-keyed row, then restores the pre-demo goal rows. Plans can't be changed during a demo.

## MCP tools added

`update_set`, `get_set_history`, `restore_set`, `update_session`, `get_session_history`,
`restore_session`, `get_planned_vs_actual`.

## HTTP endpoints added

`PUT /api/set/{id}`, `DELETE /api/set/{id}` (soft), `POST /api/set/{id}/restore`,
`GET /api/set/{id}/history`, `POST /api/session/{id}/restore`. `PUT`/`DELETE
/api/session/{id}` now go through the versioned/soft paths.

## Tests

```
pip install pytest httpx
python -m pytest tests/ -q
```

Tests run against a throwaway SQLite file; `DATABASE_URL` is cleared so they can never
touch production.

To run the same suite against a local, disposable Postgres (its `public` schema is
dropped before every test, so only localhost URLs are accepted):

```
JARVIS_TEST_PG_URL=postgresql://postgres@127.0.0.1:54329/jarvis_test python -m pytest tests/ -q
```

## v5.0.1 — exercise library, measurements, goals (`fitness.py`)

Created by `migrate_fitness()` (called at the end of `init_db`), same additive rules.

| Table | Notes |
|---|---|
| `exercise_library` (new) | 240+ seeded exercises (`exercise_library_data.py`). Seeding inserts only names that are missing — edits made via API/MCP are never overwritten. List fields are JSON text. `is_active=0` hides an entry. |
| `body_measurements` (new) | Inches + body-fat %. Soft delete; corrections append the prior values to `notes`. |
| `goals` (new) + `goal_history` | Seeded once by `goal_key`. Updates snapshot the prior row into `goal_history`; deletes are soft. Progress is computed on read from bodyweight, measurements and analytic sets (demo sessions excluded). |

Also fixed: `mcp_server.log_set` / `end_session` called `audit` without importing it (every MCP
set log raised NameError), and the progress-photo functions used `get_db()` without `with`.

## v5.0.3–5.0.5 tables

| Table | Notes |
|---|---|
| `session_coaching_notes` (new) | Notes Claude posts to a live session (MCP `post_coaching_note`) or the athlete types in TRAIN. Soft delete. |
| `exercise_modifications` | `action='add'` = exercise added mid-session; `remove_added` hides it again (nothing deleted). |
| `coach_briefs` (new) | Append-only; the latest non-empty row is the HOME coach brief. |
| `is_demo` column | Added to sessions, workout_sets, bodyweight, exercise_modifications, set_deviations, progress_photos (and the new tables). |
