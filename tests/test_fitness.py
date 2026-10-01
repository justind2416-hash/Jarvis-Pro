"""
Exercise library, body measurements, goals, progress photos (v5.0.1+).
Uses the same throwaway-database fixture as test_data_integrity.py.
"""
import json

import pytest

from test_data_integrity import call, today, days_ago, rows, fresh_db  # noqa: F401  (autouse fixture)

import database as db
import fitness as fx


# ── schema / seeding ────────────────────────────────────────────────────────

def test_library_seeded_and_idempotent():
    n = rows("SELECT COUNT(*) AS n FROM exercise_library")[0]["n"]
    assert n >= 200
    db.init_db()  # second boot must not duplicate anything
    assert rows("SELECT COUNT(*) AS n FROM exercise_library")[0]["n"] == n
    assert rows("SELECT COUNT(*) AS n FROM goals")[0]["n"] == len(fx.SEED_GOALS)


def test_seed_never_overwrites_edits():
    fx.update_exercise("Push-Ups", cues="custom cue")
    fx.seed_exercise_library()
    assert fx.get_exercise("Push-Ups")["cues"] == "custom cue"


def test_every_rehab_target_has_exercises():
    for t in fx.REHAB_TARGETS:
        assert len(fx.search_exercises(rehab_target=t)) >= 4, t


# ── lookup ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("logged,expected", [
    ("Push-ups", "Push-Ups"),
    ("pushups", "Push-Ups"),
    ("Goblet squat to box", "Goblet Squat to Box"),
    ("Cable pull-through", "Cable Pull-Through"),
    ("Face Pull (G15)", "Face Pulls"),
    ("Neutral-grip pulldown", "Neutral-Grip Pulldown"),
    ("Step-ups (20in box)", "Step-Ups (20in box)"),
    ("Half-kneeling 1-arm press", "Half-Kneeling 1-Arm Press"),
])
def test_find_exercise_matches_logged_names(logged, expected):
    ex = fx.find_exercise(logged)
    assert ex and ex["name"] == expected


def test_unknown_name_falls_back_to_rules():
    m = fx.exercise_muscles("Zercher Something Row")
    assert m["matched"] is None and "lats" in m["primary"]


def test_search_filters_and_text():
    back = fx.search_exercises(muscle_group="back")
    assert back and all(r["muscle_group"] == "back" for r in back)
    vt = fx.search_exercises(tag="v-taper")
    assert vt and all("v-taper" in r["tags"] for r in vt)
    hits = fx.search_exercises("pallof")
    assert hits[0]["name"] == "Pallof Press"
    multi = fx.search_exercises(muscle_group="chest,shoulders")
    assert {r["muscle_group"] for r in multi} == {"chest", "shoulders"}
    facets = fx.library_facets()
    assert facets["total"] >= 200 and facets["rehab_targets"]["left_shoulder"] >= 4


def test_add_and_update_exercise():
    r = fx.add_exercise("Sled Push Test", muscle_group="legs", tags=["fat-loss"], category="conditioning")
    assert r["status"] == "added" and r["exercise"]["tags"] == ["fat-loss"]
    assert fx.add_exercise("sled push test").get("error")  # duplicate (normalized)
    assert fx.add_exercise("Bad", muscle_group="nope").get("error")
    u = fx.update_exercise("Sled Push Test", is_active=False)
    assert u["exercise"]["is_active"] is False
    assert not fx.search_exercises("sled push")


# ── measurements ────────────────────────────────────────────────────────────

def test_measurements_roundtrip():
    r = fx.log_measurements(today(), waist=36, shoulders="50.5", left_arm=15)
    e = r["entry"]
    assert e["waist_in"] == 36 and e["shoulders_in"] == 50.5 and e["shoulder_waist_ratio"] == round(50.5 / 36, 3)
    assert fx.log_measurements(today()).get("error")
    assert fx.log_measurements(today(), waist="abc").get("error")
    assert fx.log_measurements("2026-13-01", waist=30).get("error")
    u = fx.update_measurement(e["id"], waist=35.5)
    assert u["entry"]["waist_in"] == 35.5 and '"waist_in": 36' in u["entry"]["notes"]
    assert fx.delete_measurement(e["id"])["status"] == "deleted"
    assert fx.get_measurements() == []
    assert len(fx.get_measurements(include_deleted=True)) == 1


def test_measurement_mcp_tools():
    r = call("log_measurements", waist=35, shoulders=52)
    assert r["status"] == "logged"
    got = call("get_measurements")
    assert got["total"] == 1 and got["entries"][0]["shoulder_waist_ratio"] == round(52 / 35, 3)


# ── goals ───────────────────────────────────────────────────────────────────

def _goal(key):
    return next(g for g in fx.get_goals(include_inactive=True) if g["goal_key"] == key)


def test_seeded_goals_cover_the_brief():
    keys = {g["goal_key"] for g in fx.get_goals()}
    assert {"bodyweight_cut", "rehab_left_shoulder", "rehab_thoracic_spine", "rehab_lumbar_l4_l5",
            "rehab_deep_core", "run_distance", "v_taper_ratio"} <= keys
    bw = _goal("bodyweight_cut")
    assert bw["target_min"] == 185 and bw["target_max"] == 190 and bw["target_date"] == "2026-12-24"


def test_bodyweight_goal_tracks_weigh_ins():
    db.log_bodyweight_entry(196.0, today())
    db.log_bodyweight_entry(210.0, today(), off_protocol=True)  # excluded from the trend
    g = _goal("bodyweight_cut")
    assert g["current"] == 196.0
    assert g["pct"] == round((196.0 - 198.5) / (187.5 - 198.5) * 100, 1)
    assert g["in_range"] is False


def test_rehab_goal_counts_days_with_rehab_sets():
    shoulder = fx.search_exercises(rehab_target="left_shoulder")[0]["name"]
    call("log_set", exercise=shoulder, weight="band", reps="15", date=days_ago(1))
    call("log_set", exercise=shoulder, weight="band", reps="15", date=days_ago(2))
    call("log_set", exercise="Push-Ups", weight="bodyweight", reps="20")
    g = _goal("rehab_left_shoulder")
    assert g["current"] == 2.0 and g["pct"] == 50.0 and g["on_track"] is False
    assert _goal("training_frequency")["current"] == 3.0


def test_run_goal_from_treadmill_sets():
    call("log_set", exercise="Treadmill Walk/Jog", weight="6.0 mph @ 1%", reps="30 min")
    assert _goal("run_distance")["current"] == 3.0


def test_demo_sets_never_count():
    start = db.start_demo_session("Demo")
    db.log_workout_set("Push-Ups", "bodyweight", "10", session_id=start["session_id"])
    assert _goal("training_frequency")["current"] == 0.0


def test_ratio_goal_uses_first_measurement_as_baseline():
    fx.log_measurements(days_ago(10), waist=38, shoulders=50)
    fx.log_measurements(today(), waist=37, shoulders=51)
    g = _goal("v_taper_ratio")
    assert g["current"] == round(51 / 37, 3)
    w = _goal("waist_down")
    assert w["current"] == 37 and w["suggested_target"] == 35.0


def test_set_goal_versions_and_creates():
    r = call("set_goal", goal_key="run_distance", target_value=8, reason="5 mi done")
    assert r["status"] == "updated" and r["goal"]["target_value"] == 8
    hist = call("get_goal_history", goal_key="run_distance")["history"]
    assert hist[0]["snapshot"]["target_value"] == 5 and hist[0]["reason"] == "5 mi done"
    new = call("set_goal", title="Strict pull-ups x10", metric="max_pullups", current_value=4, target_value=10,
               start_value=2, category="strength")
    assert new["status"] == "created" and new["goal"]["goal_key"] == "strict_pull_ups_x10"
    assert new["goal"]["pct"] == 25.0 and new["goal"]["auto_track"] is False
    assert call("set_goal", goal_key="x", direction="sideways", title="x").get("error")
    assert call("delete_goal", goal_key="strict_pull_ups_x10")["status"] == "deleted"
    assert "strict_pull_ups_x10" not in {g["goal_key"] for g in call("get_goals")["goals"]}


def test_library_mcp_tools_serialize():
    r = call("search_exercise_library", rehab_target="lumbar_l4_l5", limit=5)
    assert 0 < r["count"] <= 5
    d = call("get_exercise_details", exercise="bird dog")
    assert d["exercise"]["name"] == "Bird Dog"
    assert call("get_exercise_details", exercise="zzz").get("error")
    assert call("get_exercise_library_facets")["total"] >= 200


# ── progress photos (were 500ing: get_db() used without `with`) ─────────────

def test_progress_photo_roundtrip():
    pid = db.save_progress_photo(b"\xff\xd8jpegbytes", angle="side", bodyweight=197.5)
    lst = db.get_progress_photos()
    assert lst[0]["id"] == pid and lst[0]["date"] == today() and lst[0]["size_bytes"] == 11
    assert db.get_progress_photo_data(pid)["photo_data"] == b"\xff\xd8jpegbytes"
    assert db.delete_progress_photo(pid) is True
    assert db.get_progress_photos() == []


# ── HTTP ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_http_endpoints(client):
    assert client.get("/api/status").json()["version"].startswith("5.0.")
    r = client.get("/api/exercises", params={"muscle_group": "core", "tag": "mcgill-big-3"}).json()
    assert r["count"] >= 1
    assert client.get("/api/exercises/Bird%20Dog").json()["exercise"]["name"] == "Bird Dog"
    assert client.get("/api/exercises/nope-nope").status_code == 404
    assert client.get("/api/exercises/facets").json()["total"] >= 200
    m = client.post("/api/measurements", json={"waist": 36, "shoulders": 51}).json()
    assert m["status"] == "logged"
    assert client.post("/api/measurements", json={}).status_code == 400
    goals = client.get("/api/goals").json()["goals"]
    assert any(g["goal_key"] == "v_taper_ratio" and g["current"] == round(51 / 36, 3) for g in goals)
    import base64
    up = client.post("/api/progress-photo", json={"photo": "data:image/jpeg;base64," + base64.b64encode(b"abc").decode()})
    assert up.status_code == 200
    pid = up.json()["photo_id"]
    assert client.get("/api/progress-photos").json()["photos"][0]["id"] == pid
    img = client.get(f"/api/progress-photo/{pid}")
    assert img.status_code == 200 and img.content == b"abc"
    page = client.get("/")
    assert page.status_code == 200 and "JARVIS V5.0." in page.text


# ── v5.0.2: volume by muscle group, bodyweight window, sync signature ──────

def test_muscle_volume_counts_primary_and_secondary():
    call("log_set", exercise="Dumbbell Bench Press", weight="50", reps="10")
    call("log_set", exercise="Dumbbell Bench Press", weight="50", reps="8")
    call("log_set", exercise="Treadmill Walk/Jog", weight="3.0 mph @ 2%", reps="5 min")  # cardio: not counted
    shoulder = fx.search_exercises(rehab_target="left_shoulder", category="rehab")[0]["name"]
    call("log_set", exercise=shoulder, weight="band", reps="15")
    v = fx.muscle_volume(4)
    assert len(v["weeks"]) == 4 and v["weeks"][-1]["end"] == today()
    tw = v["this_week"]
    assert tw["sets"]["Chest"] == 2.0 and tw["load"]["Chest"] == 900
    assert tw["sets"]["Shoulders"] == 1.0 and tw["sets"]["Arms"] == 1.0  # secondary delts + triceps at ½
    assert tw["sets"]["Legs"] == 0 and tw["rehab_sets"] == 1
    assert tw["status"]["Chest"] == "low"
    assert v["weeks"][-1]["total_sets"] == 4


def test_muscle_volume_windows_and_exclusions():
    call("log_set", exercise="Lat Pulldown", weight="120", reps="10", date=days_ago(8))
    demo = db.start_demo_session("Demo")
    db.log_workout_set("Lat Pulldown", "120", "10", session_id=demo["session_id"])
    v = fx.muscle_volume(2)
    assert v["weeks"][0]["sets"]["Back"] == 1.0
    assert v["weeks"][1]["sets"]["Back"] == 0


def test_bodyweight_days_window_and_sync_sig(client):
    db.log_bodyweight_entry(199.0, days_ago(40))
    db.log_bodyweight_entry(197.0, today())
    assert len(client.get("/api/bodyweight/history", params={"days": 30}).json()["entries"]) == 1
    assert len(client.get("/api/bodyweight/history", params={"days": 90}).json()["entries"]) >= 2
    s1 = client.get("/api/sync-sig").json()
    fx.log_measurements(today(), waist=36)
    s2 = client.get("/api/sync-sig").json()
    assert s1["measurements"] != s2["measurements"] and s1["goals"] == s2["goals"]
    mv = client.get("/api/stats/muscle-volume").json()
    assert len(mv["weeks"]) == 8 and "Rehab" not in mv["groups"]
    assert call("get_muscle_volume", weeks=2)["weeks"]


def test_wrong_direction_is_behind():
    db.log_bodyweight_entry(201.0, today())
    assert _goal("bodyweight_cut")["on_track"] is False
