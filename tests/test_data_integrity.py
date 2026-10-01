"""
Data-integrity tests. Run with:  python -m pytest tests/ -q

Default: a throwaway SQLite database per test. DATABASE_URL is always cleared so these
tests can never touch production.

To run against a local, disposable Postgres instead:
    JARVIS_TEST_PG_URL=postgresql://postgres@127.0.0.1:54329/jarvis_test python -m pytest tests/ -q
The schema of that database is dropped and recreated before every test, so the URL must
point at localhost/127.0.0.1 (enforced below).
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

os.environ.pop("DATABASE_URL", None)
TEST_PG_URL = os.environ.get("JARVIS_TEST_PG_URL")
if TEST_PG_URL:
    if urlparse(TEST_PG_URL).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("JARVIS_TEST_PG_URL must point at a local throwaway database")
    os.environ["DATABASE_URL"] = TEST_PG_URL
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = tempfile.mkdtemp(prefix="jarvis-test-import-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db  # noqa: E402
import mcp_server as mcp  # noqa: E402

sqlite_only = pytest.mark.skipif(bool(TEST_PG_URL), reason="SQLite-file test")


def _reset_pg_schema():
    import psycopg2
    conn = psycopg2.connect(TEST_PG_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    if db.USE_PG:
        _reset_pg_schema()
    else:
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_backfill_is_bool", None)
    db.init_db()
    yield


def call(tool, **args):
    """Invoke an MCP tool the way the /mcp endpoint does, including JSON serialization."""
    result = mcp.TOOLS[tool]["fn"](args)
    json.dumps(result)  # every tool result must be JSON-serializable
    return result


def today():
    return db._local_today()


def days_ago(n):
    return (db._local_now().date() - timedelta(days=n)).isoformat()


def days_ahead(n):
    return (db._local_now().date() + timedelta(days=n)).isoformat()


def rows(sql, params=()):
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


PLAN = {
    "program_name": "Ironforge",
    "exercises": [
        {"name": "Goblet Squat to Box", "weight": "60 lb", "reps": "8-10", "sets": 3},
        {"name": "Push-Ups", "weight": "bodyweight", "reps": "max", "sets": 3},
    ],
    "warmup": [{"name": "Arm Circles", "target": "20 each direction", "sets": 1}],
}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, date TEXT NOT NULL,
    session_date TEXT, program TEXT DEFAULT 'Arsenal', started_at TEXT, ended_at TEXT, notes TEXT DEFAULT '',
    is_backfill INTEGER DEFAULT 0, created_at TEXT DEFAULT '');
CREATE TABLE workout_sets (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, exercise TEXT NOT NULL,
    weight TEXT NOT NULL DEFAULT 'bodyweight', reps TEXT DEFAULT '', rpe TEXT DEFAULT '', notes TEXT DEFAULT '',
    timestamp TEXT DEFAULT '', set_index INTEGER, logged_at TEXT DEFAULT '');
CREATE TABLE bodyweight (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, weight_lbs REAL NOT NULL,
    notes TEXT DEFAULT '', timestamp TEXT NOT NULL);
CREATE TABLE planned_workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, planned_date TEXT NOT NULL,
    program_name TEXT NOT NULL, workout_data TEXT NOT NULL, status TEXT DEFAULT 'pending',
    actual_session_id TEXT, notes TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
INSERT INTO sessions (session_id, date, session_date, program, started_at, ended_at, notes, is_backfill)
    VALUES ('backfill_2026-09-14', '2026-09-14', '2026-09-14', 'Strength A', NULL, NULL, 'legacy', 1),
           ('session_2026-09-26_Ironforge', '2026-09-26', '2026-09-26', 'Ironforge',
            '2026-09-26T05:00:00-04:00', '2026-09-26T06:00:00-04:00', 'real', 0);
INSERT INTO workout_sets (session_id, exercise, weight, reps, timestamp)
    VALUES ('backfill_2026-09-14', 'Goblet squat to box', '30', '8', '2026-09-14T19:00:00'),
           ('session_2026-09-26_Ironforge', 'Goblet Squat to Box', '60', '8', '2026-09-26T05:10:00-04:00'),
           ('session_2026-09-26_Ironforge', 'Goblet Squat to Box', '60', '9', '2026-09-26T05:15:00-04:00');
INSERT INTO bodyweight (date, weight_lbs, notes, timestamp) VALUES ('2026-09-19', 199.4, '', '2026-09-19T07:00:00');
INSERT INTO planned_workouts (planned_date, program_name, workout_data) VALUES ('2026-09-26', 'Ironforge', '{}');
"""


@sqlite_only
def test_migration_on_legacy_db_is_additive_and_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
              for t in ("sessions", "workout_sets", "bodyweight", "planned_workouts")}
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()  # second run must be a no-op

    conn = sqlite3.connect(path)
    for table, old_rows in before.items():
        new_rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        assert len(new_rows) == len(old_rows), table
        for old, new in zip(old_rows, new_rows):
            assert tuple(new[:len(old)]) == tuple(old), f"{table} row changed"
    cols = lambda t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
    assert {"deleted_at", "deleted_reason", "supersedes_set_id", "source", "performed_at"} <= cols("workout_sets")
    assert {"deleted_at", "deleted_reason", "source", "planned_workout_snapshot"} <= cols("sessions")
    assert {"fasted", "time_of_day", "off_protocol"} <= cols("bodyweight")
    assert "superseded_at" in cols("planned_workouts")
    assert {"set_id", "source", "superseded_at"} <= cols("set_deviations")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"set_history", "session_history"} <= tables
    # Only the NULL source of the legacy backfill rows was filled in
    assert conn.execute("SELECT source FROM sessions WHERE session_id='backfill_2026-09-14'").fetchone()[0] == "backfill"
    assert conn.execute("SELECT source FROM sessions WHERE session_id='session_2026-09-26_Ironforge'").fetchone()[0] is None
    conn.close()


def test_v2_migration_no_longer_wipes_data():
    import inspect
    src = inspect.getsource(db._migrate_v2)
    assert "DELETE FROM workout_sets" not in src.replace("used to run DELETE FROM workout_sets", "")
    assert 'conn.execute("DELETE FROM sessions")' not in src


# ---------------------------------------------------------------------------
# 1. Soft deletes
# ---------------------------------------------------------------------------

def test_delete_set_is_soft_and_restorable():
    s = call("start_session", program="Ironforge")
    a = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=8, session_id=s["session_id"])
    b = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=8, session_id=s["session_id"])

    preview = call("delete_set", set_id=b["set_id"])
    assert preview["preview"]["id"] == b["set_id"]
    assert rows("SELECT deleted_at FROM workout_sets WHERE id=?", (b["set_id"],))[0]["deleted_at"] is None

    res = call("delete_set", set_id=b["set_id"], confirm=True, reason="duplicate")
    assert res["status"] == "deleted"
    row = rows("SELECT * FROM workout_sets WHERE id=?", (b["set_id"],))[0]
    assert row["deleted_at"] and row["deleted_reason"] == "duplicate"   # still in the table

    hist = call("get_history", limit=1)["sessions"][0]
    assert [x["id"] for x in hist["sets"]] == [a["set_id"]]
    assert hist["total_sets"] == 1
    with_deleted = call("get_history", limit=1, include_deleted=True)["sessions"][0]
    assert [x["id"] for x in with_deleted["deleted_sets"]] == [b["set_id"]]

    assert call("restore_set", set_id=b["set_id"])["status"] == "restored"
    assert call("get_history", limit=1)["sessions"][0]["total_sets"] == 2
    changes = [h["change_type"] for h in call("get_set_history", set_id=b["set_id"])["history"]]
    assert changes == ["restore", "delete"]


def test_delete_session_cascades_softly_and_restores():
    s = call("start_session", program="Arsenal")
    keep = call("log_set", exercise="DB Hip Thrust", weight="50", reps=10, session_id=s["session_id"])
    gone_first = call("log_set", exercise="DB Hip Thrust", weight="50", reps=10, session_id=s["session_id"])
    call("delete_set", set_id=gone_first["set_id"], confirm=True, reason="typo")

    res = call("delete_session", session_id=s["db_id"], confirm=True, reason="test session")
    assert res["status"] == "deleted" and res["sets_deleted"] == 1
    assert rows("SELECT COUNT(*) AS n FROM workout_sets")[0]["n"] >= 2  # nothing removed
    assert all(x["session_id"] != s["session_id"] for x in call("get_history", limit=10)["sessions"])
    assert all(x["session_id"] != s["session_id"] for x in call("get_session_log")["log"])
    assert any(x["session_id"] == s["session_id"] for x in call("get_history", limit=10, include_deleted=True)["sessions"])

    # text session_id works as well as the integer id
    r = call("restore_session", session_id=s["session_id"])
    assert r["status"] == "restored" and r["sets_restored"] == 1
    sess = [x for x in call("get_history", limit=10)["sessions"] if x["session_id"] == s["session_id"]][0]
    assert [x["id"] for x in sess["sets"]] == [keep["set_id"]]  # individually deleted set stays deleted


def test_cannot_log_into_deleted_session():
    s = call("start_session", program="Arsenal")
    call("delete_session", session_id=s["session_id"], confirm=True)
    r = call("log_set", exercise="DB Hip Thrust", weight="50", reps=10, session_id=s["session_id"])
    assert "error" in r


# ---------------------------------------------------------------------------
# 2. Unique session ids
# ---------------------------------------------------------------------------

def test_recreated_session_gets_new_id_and_no_orphans():
    s1 = call("start_session", program="Ironforge")
    assert s1["session_id"].startswith(f"session_{today()}_Ironforge_")
    assert len(s1["session_id"].rsplit("_", 1)[1]) == 6
    call("log_set", exercise="Push-Ups", weight="bodyweight", reps=20, session_id=s1["session_id"])
    call("delete_session", session_id=s1["db_id"], confirm=True)

    s2 = call("start_session", program="Ironforge")
    assert s2["status"] == "created"
    assert s2["session_id"] != s1["session_id"]
    assert call("get_history", limit=1)["sessions"][0]["total_sets"] == 0


def test_app_session_ids_are_unique():
    a = db.create_workout_session("Arsenal")
    b = db.create_workout_session("Arsenal")
    assert a["session_id"] != b["session_id"]
    assert rows("SELECT ended_at FROM sessions WHERE session_id=?", (a["session_id"],))[0]["ended_at"]


# ---------------------------------------------------------------------------
# 3. Set ids in responses
# ---------------------------------------------------------------------------

def test_set_ids_everywhere():
    s = call("start_session", program="Ironforge")
    ids = [call("log_set", exercise="Push-Ups", weight="bodyweight", reps=r, session_id=s["session_id"])["set_id"]
           for r in (20, 15)]
    assert all(isinstance(i, int) for i in ids)
    assert [x["id"] for x in call("get_history", limit=1)["sessions"][0]["sets"]] == ids
    log_entry = [e for e in call("get_session_log")["log"] if e["session_id"] == s["session_id"]][0]
    assert log_entry["set_ids"] == ids and log_entry["total_sets"] == 2
    summary = call("get_session_summaries", limit=1)["summaries"][0]
    assert [x["id"] for x in summary["sets"]] == ids
    current = db.get_current_session_data()
    assert [x["id"] for x in current["exercise_sets"]["Push-Ups"]] == ids


# ---------------------------------------------------------------------------
# 4. update_set with versioning
# ---------------------------------------------------------------------------

def test_update_set_versions_prior_values():
    s = call("start_session", program="Ironforge")
    x = call("log_set", exercise="Goblet Squat to Box", weight="30", reps=8, rpe=7, session_id=s["session_id"])
    r = call("update_set", set_id=x["set_id"], weight="60", reason="mis-dialed")
    assert r["status"] == "updated" and r["before"] == {"weight": "30"} and r["after"] == {"weight": "60"}
    r = call("update_set", set_id=x["set_id"], reps=9, notes="felt good", reason="recount")
    h = call("get_set_history", set_id=x["set_id"])
    assert h["current"]["weight"] == "60" and h["current"]["reps"] == "9"
    assert [(e["weight"], e["reps"], e["reason"]) for e in h["history"]] == [("60", "8", "recount"), ("30", "8", "mis-dialed")]
    assert call("update_set", set_id=x["set_id"], weight="60")["status"] == "unchanged"
    assert "error" in call("update_set", set_id=x["set_id"])

    call("delete_set", set_id=x["set_id"], confirm=True)
    assert "error" in call("update_set", set_id=x["set_id"], weight="70")


def test_past_actuals_remain_writable():
    x = call("log_set", exercise="Push-Ups", weight="bodyweight", reps=12, date=days_ago(10))
    assert call("update_set", set_id=x["set_id"], reps=14, reason="found paper log")["status"] == "updated"


# ---------------------------------------------------------------------------
# 5. Session notes + session history
# ---------------------------------------------------------------------------

def test_session_notes_and_history():
    s = call("start_session", program="Arsenal", notes="slept 5h")
    call("log_set", exercise="DB Hip Thrust", weight="50", reps=10, session_id=s["session_id"])
    end = call("end_session", session_id=s["session_id"], notes="back fine")
    assert end["notes"] == "slept 5h\nback fine"

    u = call("update_session", session_id=s["db_id"], notes="left knee twinge", notes_mode="append", reason="added later")
    assert u["status"] == "updated"
    h = call("get_session_history", session_id=s["session_id"])
    assert h["current"]["notes"] == "slept 5h\nback fine\nleft knee twinge"
    assert h["history"][0]["change_type"] == "update" and h["history"][0]["before"]["notes"] == "slept 5h\nback fine"
    assert any(e["change_type"] == "end" and e["before"]["notes"] == "slept 5h" for e in h["history"])


# ---------------------------------------------------------------------------
# 6. Immutable past plans + snapshot at session start
# ---------------------------------------------------------------------------

def test_set_planned_program_rejects_past_dates_atomically():
    r = call("set_planned_program", schedule=[
        {"date": days_ahead(1), **PLAN},
        {"date": days_ago(1), **PLAN},
    ])
    assert r["ok"] is False and r["rejected_dates"] == [days_ago(1)]
    assert rows("SELECT COUNT(*) AS n FROM planned_workouts")[0]["n"] == 0
    assert call("set_planned_program", schedule=[{"date": today(), **PLAN}])["ok"] is True


def test_replanning_supersedes_instead_of_deleting():
    call("set_planned_program", schedule=[{"date": days_ahead(2), **PLAN}])
    call("set_planned_program", schedule=[{"date": days_ahead(2), **PLAN, "program_name": "Arsenal"}])
    all_rows = rows("SELECT program_name, superseded_at FROM planned_workouts WHERE planned_date=? ORDER BY id", (days_ahead(2),))
    assert [r["program_name"] for r in all_rows] == ["Ironforge", "Arsenal"]
    assert all_rows[0]["superseded_at"] and all_rows[1]["superseded_at"] is None
    assert db.get_planned_workout(days_ahead(2))["program_name"] == "Arsenal"
    assert len([d for d in db.get_planned_schedule(14) if d["date"] == days_ahead(2)]) == 1


def test_session_snapshots_plan_at_start():
    call("set_planned_program", schedule=[{"date": today(), **PLAN}])
    s = call("start_session", program="Ironforge")
    assert s["has_plan_snapshot"] is True
    # Re-plan today after starting: the session keeps what was planned when it began
    call("set_planned_program", schedule=[{"date": today(), "program_name": "Arsenal",
                                           "exercises": [{"name": "DB Hip Thrust", "weight": "50", "reps": "10", "sets": 3}]}])
    pva = call("get_planned_vs_actual", session_id=s["session_id"])
    assert pva["mode"] == "planned_vs_actual"
    assert pva["plan"]["program_name"] == "Ironforge"


# ---------------------------------------------------------------------------
# 7. Supersedes link
# ---------------------------------------------------------------------------

def test_superseded_sets_leave_analytics_but_stay_in_record():
    s = call("start_session", program="Ironforge")
    wrong = call("log_set", exercise="Goblet Squat to Box", weight="30", reps=8, session_id=s["session_id"])
    fixed = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=8, session_id=s["session_id"],
                 supersedes_set_id=wrong["set_id"])
    assert fixed["entry"]["source"] == "correction"
    hist = call("get_history", limit=1)["sessions"][0]
    assert [x["id"] for x in hist["sets"]] == [fixed["set_id"]]
    assert hist["superseded_sets"][0]["id"] == wrong["set_id"]
    assert hist["superseded_sets"][0]["superseded_by_set_id"] == fixed["set_id"]
    end = call("end_session", session_id=s["session_id"])
    assert end["total_sets"] == 1 and end["total_volume"] == 480
    assert call("get_set_history", set_id=wrong["set_id"])["superseded_by_set_ids"] == [fixed["set_id"]]
    # Deleting the correction brings the original back into analytics
    call("delete_set", set_id=fixed["set_id"], confirm=True)
    assert [x["id"] for x in call("get_history", limit=1)["sessions"][0]["sets"]] == [wrong["set_id"]]


# ---------------------------------------------------------------------------
# 8/9/10. Source, performed_at, end-time default
# ---------------------------------------------------------------------------

def test_source_and_performed_at():
    s = call("start_session", program="Ironforge")
    m = call("log_set", exercise="Push-Ups", weight="bodyweight", reps=20, session_id=s["session_id"])
    a = db.log_workout_set("Push-Ups", "bodyweight", "18", session_id=s["session_id"])
    b = call("log_set", exercise="Push-Ups", weight="bodyweight", reps=15, date=days_ago(3))
    t = call("log_set", exercise="Push-Ups", weight="bodyweight", reps=15, session_id=s["session_id"],
             performed_at=f"{today()}T05:20:00")
    got = {r["id"]: r for r in rows("SELECT id, source, performed_at FROM workout_sets")}
    assert got[m["set_id"]]["source"] == "mcp" and got[m["set_id"]]["performed_at"]
    assert got[a["set_id"]]["source"] == "app"
    assert got[b["set_id"]]["source"] == "backfill" and got[b["set_id"]]["performed_at"] is None
    assert got[t["set_id"]]["performed_at"] == f"{today()}T05:20:00"
    assert rows("SELECT source FROM sessions WHERE session_id=?", (s["session_id"],))[0]["source"] == "mcp"
    assert "error" in db.log_workout_set("Push-Ups", "bodyweight", "1", session_id=s["session_id"], source="bogus")


def test_end_session_defaults_to_last_set_time():
    s = call("start_session", program="Ironforge", date=days_ago(1), start_time=f"{days_ago(1)}T05:00:00")
    call("log_set", exercise="Push-Ups", weight="bodyweight", reps=20, session_id=s["session_id"],
         performed_at=f"{days_ago(1)}T05:10:00")
    call("log_set", exercise="Push-Ups", weight="bodyweight", reps=18, session_id=s["session_id"],
         performed_at=f"{days_ago(1)}T05:47:00")
    end = call("end_session", session_id=s["session_id"])
    assert end["ended_at"] == f"{days_ago(1)}T05:47:00"
    assert end["duration_min"] == 47.0
    # explicit end_time still wins
    s2 = call("start_session", program="Arsenal")
    e2 = call("end_session", session_id=s2["db_id"], end_time=f"{today()}T23:59:00")
    assert e2["ended_at"] == f"{today()}T23:59:00"


def test_backfill_end_without_times_is_unknown_not_now():
    s = call("start_session", program="Arsenal", date=days_ago(5))
    call("log_set", exercise="DB Hip Thrust", weight="50", reps=10, session_id=s["session_id"])
    assert call("end_session", session_id=s["session_id"])["ended_at"] is None


# ---------------------------------------------------------------------------
# 11. Auto deviations
# ---------------------------------------------------------------------------

def test_auto_deviations_against_plan():
    call("set_planned_program", schedule=[{"date": today(), **PLAN}])
    s = call("start_session", program="Ironforge")
    sid = s["session_id"]
    light = call("log_set", exercise="Goblet Squat to Box", weight="50 lb", reps=8, session_id=sid)
    assert light["deviation"]["deviation_type"] == "weight_decrease"
    ok = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=9, session_id=sid)
    assert ok["deviation"] is None
    short = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=6, session_id=sid)
    assert short["deviation"]["deviation_type"] == "volume_reduction"
    extra = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=8, session_id=sid)
    assert extra["deviation"]["deviation_type"] == "exceeded_prescription"
    assert call("log_set", exercise="Push-Ups", weight="bodyweight", reps=25, session_id=sid)["deviation"] is None
    assert call("log_set", exercise="Arm Circles", weight="bodyweight", reps=20, session_id=sid)["deviation"] is None
    swap = call("log_set", exercise="Cable Fly", weight="40", reps=12, session_id=sid)
    assert swap["deviation"]["deviation_type"] == "unplanned_exercise"

    # Correcting the light set re-evaluates it; the stale deviation is superseded, not deleted
    call("update_set", set_id=light["set_id"], weight="60", reason="mis-typed")
    current = call("get_deviations", session_id=sid)["deviations"]
    assert light["set_id"] not in [d["set_id"] for d in current]
    assert rows("SELECT COUNT(*) AS n FROM set_deviations WHERE set_id=?", (light["set_id"],))[0]["n"] == 1

    # Deleting a set hides its deviation from reports
    call("delete_set", set_id=swap["set_id"], confirm=True)
    assert swap["set_id"] not in [d["set_id"] for d in call("get_deviations", session_id=sid)["deviations"]]
    assert call("get_variance_report", session_id=sid)["total_deviations"] == len(call("get_deviations", session_id=sid)["deviations"])


def test_no_plan_means_no_auto_deviation():
    s = call("start_session", program="Ironforge")
    assert call("log_set", exercise="Goblet Squat to Box", weight="10", reps=1, session_id=s["session_id"])["deviation"] is None


def test_manual_log_deviation_returns_id():
    r = call("log_deviation", exercise="Pallof Press", deviation_type="form_modification", reason="half kneeling")
    assert isinstance(r["id"], int)


# ---------------------------------------------------------------------------
# 12. Bodyweight
# ---------------------------------------------------------------------------

def test_bodyweight_protocol_fields():
    call("log_bodyweight", weight=199.2, date=days_ago(3), fasted=True, time_of_day="06:05")
    call("log_bodyweight", weight=203.8, date=days_ago(2), fasted=False, time_of_day="19:30", off_protocol=True,
         notes="after dinner")
    today_entry = call("log_bodyweight", weight=198.9)["entry"]
    assert today_entry["time_of_day"] and today_entry["fasted"] is None
    everything = call("get_bodyweight_history", limit=10)["entries"]
    assert any(e["off_protocol"] and e["weight_lbs"] == 203.8 for e in everything)
    trend = call("get_bodyweight_history", limit=10, include_off_protocol=False)["entries"]
    assert all(not e["off_protocol"] for e in trend) and 203.8 not in [e["weight_lbs"] for e in trend]
    assert "203.8" not in db.get_recent_history_for_prompt()
    assert "error" in call("log_bodyweight", weight=200, date="09/29/2026")


# ---------------------------------------------------------------------------
# 13. Backfill
# ---------------------------------------------------------------------------

def test_backfill_is_actual_only_and_leaves_live_session_alone():
    live = call("start_session", program="Arsenal")
    call("log_set", exercise="DB Hip Thrust", weight="50", reps=10, session_id=live["session_id"])

    b1 = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=8, date=days_ago(4), program="Ironforge")
    b2 = call("log_set", exercise="Goblet Squat to Box", weight="60", reps=8, date=days_ago(4), program="Ironforge")
    assert b1["entry"]["session_id"] == b2["entry"]["session_id"]
    assert b1["deviation"] is None

    sess = rows("SELECT * FROM sessions WHERE session_id=?", (b1["entry"]["session_id"],))[0]
    assert sess["is_backfill"] == 1 and sess["source"] == "backfill" and sess["planned_workout_snapshot"] is None
    assert sess["date"] == days_ago(4)
    assert rows("SELECT ended_at FROM sessions WHERE session_id=?", (live["session_id"],))[0]["ended_at"] is None
    assert db.get_current_session_data()["session_id"] == live["session_id"]

    pva = call("get_planned_vs_actual", session_id=b1["entry"]["session_id"])
    assert pva["mode"] == "actual_only" and [x["id"] for x in pva["actual"]["Goblet Squat to Box"]] == [b1["set_id"], b2["set_id"]]

    assert "error" in call("log_set", exercise="Push-Ups", weight="bodyweight", reps=1, date=days_ahead(1))
    assert "error" in call("start_session", program="Arsenal", date=days_ahead(1))
    assert "error" in call("log_set", exercise="Push-Ups", weight="bodyweight", reps=1, date="2026-13-45")


def test_backfill_with_existing_future_plan_snapshot():
    # A plan pushed for today, then logged after the fact tomorrow, still snapshots the plan
    call("set_planned_program", schedule=[{"date": today(), **PLAN}])
    s = call("start_session", program="Ironforge", date=today())
    assert s["has_plan_snapshot"] is True


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_http_endpoints_soft_delete_and_update():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)

    start = client.post("/api/session/start", json={"program": "Arsenal", "notes": "from app"}).json()
    logged = client.post("/api/set/log", json={"exercise": "DB Hip Thrust", "weight": "50", "reps": "10",
                                               "session_id": start["session_id"]}).json()
    assert logged["status"] == "logged" and isinstance(logged["set_id"], int)
    assert rows("SELECT source FROM workout_sets WHERE id=?", (logged["set_id"],))[0]["source"] == "app"

    cur = client.get("/api/session/current").json()
    assert cur["exercise_sets"]["DB Hip Thrust"][0]["id"] == logged["set_id"]

    assert client.put(f"/api/set/{logged['set_id']}", json={"reps": "12", "reason": "recount"}).json()["status"] == "updated"
    assert client.get(f"/api/set/{logged['set_id']}/history").json()["history"][0]["reps"] == "10"

    r = client.put(f"/api/session/{start['id']}", json={"ended_at": f"{today()}T06:00:00", "reason": "fix"})
    assert r.json()["ok"] is True

    assert client.delete(f"/api/session/{start['id']}?reason=test").json()["status"] == "deleted"
    assert rows("SELECT COUNT(*) AS n FROM workout_sets WHERE id=?", (logged["set_id"],))[0]["n"] == 1
    assert client.post(f"/api/session/{start['id']}/restore").json()["status"] == "restored"

    bw = client.post("/api/bodyweight", json={"weight": 199, "fasted": True, "off_protocol": False}).json()
    assert bw["entry"]["fasted"] is True

    past = client.post("/api/swap-workout", json={"source_date": today(), "target_date": days_ago(1)})
    assert past.status_code in (400, 404)
