"""v5.0.5: demo mode tags every write with is_demo and wipes it on end; HOME coaching overview."""
import pytest

from test_data_integrity import call, today, days_ago, rows, fresh_db  # noqa: F401

import database as db
import fitness as fx
import training as tr
import coaching as co


def _count(table, where="1 = 1"):
    return rows(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")[0]["n"]


def test_demo_tags_everything_and_end_wipes_only_demo():
    # Real data first
    db.log_bodyweight_entry(198.0, days_ago(1))
    fx.log_measurements(days_ago(1), waist=37)
    real = db.create_workout_session("Arsenal")["session_id"]
    db.log_workout_set("Push-Ups", "bodyweight", "20", session_id=real)
    db.end_workout_session(real)
    pid = db.save_progress_photo(b"real")
    goals_before = {g["goal_key"]: (g["target_value"], g["notes"]) for g in fx.get_goals(include_inactive=True)}

    d = call("start_demo_mode", program="Arsenal")
    assert d["demo"] and db.demo_active()
    # Claude over MCP + app writes during the demo
    call("log_set", exercise="Lat Pulldown", weight="120", reps="10")  # lands in the demo session
    call("log_bodyweight", weight=150)
    call("log_measurements", waist=30)
    call("post_coaching_note", text="demo cue")
    call("add_exercise_to_session", exercise="Face Pulls")
    call("set_coach_brief", text="demo brief")
    call("set_goal", goal_key="bodyweight_cut", target_value=170, notes="demo edit")
    call("set_goal", title="Demo only goal", metric="x", current_value=1, target_value=2)
    call("delete_goal", goal_key="waist_down")
    db.save_progress_photo(b"demo")
    assert tr.add_to_today_plan("Bird Dog").get("error")  # plans untouched in demo
    cur = call("get_current_session")
    assert cur["demo"] and cur["exercise_sets"]["Lat Pulldown"]
    assert _count("bodyweight", "is_demo = 1") == 1 and _count("body_measurements", "is_demo = 1") == 1
    assert _count("workout_sets", "is_demo = 1") == 1 and _count("progress_photos", "is_demo = 1") == 1
    assert {g["goal_key"] for g in fx.get_goals()} >= {"demo_only_goal"}

    r = call("end_demo_mode")
    assert r["status"] == "deleted" and r["deleted"]["goals_reverted"] == 2
    assert not db.demo_active()
    for t in ("sessions", "workout_sets", "bodyweight", "body_measurements", "progress_photos", "coach_briefs",
              "goals", "session_coaching_notes"):
        assert _count(t, "is_demo = 1") == 0, t
    assert _count("sessions", "substr(session_id, 1, 5) = 'demo_'") == 0
    assert _count("exercise_modifications") == 0
    # Real data intact, goals exactly as before
    assert [e["weight_lbs"] for e in db.get_bodyweight_history()] == [198.0]
    assert [m["waist_in"] for m in fx.get_measurements()] == [37]
    assert _count("workout_sets") == 1 and [p["id"] for p in db.get_progress_photos()] == [pid]
    after = {g["goal_key"]: (g["target_value"], g["notes"]) for g in fx.get_goals(include_inactive=True)}
    assert after == goals_before
    assert co.get_brief() is None


def test_real_start_purges_forgotten_demo():
    db.start_demo_session("Demo")
    db.log_bodyweight_entry(140.0)
    assert _count("bodyweight", "is_demo = 1") == 1
    db.create_workout_session("Arsenal")
    assert _count("bodyweight", "is_demo = 1") == 0 and _count("sessions", "is_demo = 1") == 0


def test_stale_demo_stops_tagging(monkeypatch):
    db.start_demo_session("Demo")
    with db.get_db() as conn:
        conn.execute("UPDATE sessions SET started_at = ? WHERE is_demo = 1", ("2026-01-01T06:00:00",))
    assert not db.demo_active()
    db.log_bodyweight_entry(199.0)
    assert _count("bodyweight", "is_demo = 1") == 0


def test_end_demo_rejects_real_ids():
    sid = db.create_workout_session("Arsenal")["session_id"]
    assert db.end_demo_session(sid).get("error")


def test_overview_sections_and_insights():
    for i, w in enumerate([199.6, 199.2, 198.9, 198.5, 198.2]):
        db.log_bodyweight_entry(w, days_ago(8 - i * 2))
    call("log_set", exercise="Dumbbell Bench Press", weight="50", reps="10")
    call("log_set", exercise="Dumbbell Bench Press", weight="50", reps="10")
    call("log_set", exercise="Push-Ups", weight="bodyweight", reps="20")
    call("log_set", exercise="Dumbbell Shoulder Press", weight="30", reps="10")
    o = call("get_coaching_overview")
    assert o["countdown"]["target_date"] == "2026-12-24" and o["countdown"]["days_left"] > 0
    w = o["weight"]
    assert w["rate_lb_per_wk"] < 0 and w["needed_lb_per_wk"] < 0 and w["projected_dec24"] < w["trend_7d"]
    assert {r["target"] for r in o["rehab"]} == set(fx.REHAB_TARGETS)
    assert all(r["status"] == "due" and r["try"] for r in o["rehab"])
    t = o["training"]
    assert t["push_sets"] == 4 and t["pull_sets"] == 0 and "Back" in t["low_groups"]
    areas = [i["area"] for i in o["insights"]]
    assert "rehab" in areas and "balance" in areas and "v-taper" in areas
    assert o["insights"][0]["level"] == "warn"


def test_brief_roundtrip_and_sync_sig():
    s1 = fx.sync_signature()["brief"]
    assert call("set_coach_brief", text="Thoracic work first today; easy 3 mi.")["status"] == "posted"
    assert co.overview()["brief"]["text"].startswith("Thoracic")
    assert fx.sync_signature()["brief"] != s1
    co.set_brief("")
    assert co.get_brief() is None


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_http(client):
    assert client.get("/api/coaching/overview").json()["countdown"]["days_left"] > 0
    assert client.post("/api/coaching/brief", json={"text": "hi"}).json()["status"] == "posted"
    d = client.post("/api/demo/start", json={}).json()
    client.post("/api/bodyweight", json={"weight": 151})
    assert client.get("/api/bodyweight/history").json()["entries"][0]["is_demo"] is True
    r = client.post("/api/session/end", json={"session_id": d["session_id"]}).json()
    assert r["demo"] and r["deleted"]["bodyweight"] == 1
    assert client.get("/api/bodyweight/history").json()["entries"] == []
