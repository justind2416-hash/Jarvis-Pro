"""Live-session coaching (v5.0.3): coach notes, added exercises, suggestions, summary."""
import pytest

from test_data_integrity import call, today, days_ago, rows, fresh_db  # noqa: F401

import database as db
import fitness as fx
import training as tr


def _start(program="Arsenal"):
    return db.create_workout_session(program)["session_id"]


def test_notes_attach_to_live_session_and_show_in_current():
    assert tr.add_note("hi").get("error")  # no session yet
    sid = _start()
    r = call("post_coaching_note", text="Elbows to ribs on rows", kind="cue", exercise="Seated Low Row")
    assert r["status"] == "posted" and r["note"]["session_id"] == sid
    assert call("post_coaching_note", text="x", kind="bogus").get("error")
    tr.add_note("Left shoulder pinch on set 2", kind="pain", author="athlete", source="app")
    notes = call("get_current_session")["coach_notes"]
    assert [n["kind"] for n in notes] == ["pain", "cue"]
    assert tr.delete_note(notes[0]["id"])["status"] == "deleted"
    assert len(tr.get_notes()) == 1


def test_add_and_remove_session_exercise():
    assert tr.add_session_exercise("Face Pulls").get("error")  # no live session
    sid = _start()
    r = call("add_exercise_to_session", exercise="face pull (g15)", sets=3, reps="15", reason="rear delts low")
    assert r["status"] == "added" and r["exercise"]["name"] == "Face Pulls" and r["exercise"]["added_by"] == "claude"
    assert r["exercise"]["reason"] == "rear delts low" and r["exercise"]["cues"]
    assert tr.add_session_exercise("Face Pulls").get("error")  # duplicate
    lib_default = tr.add_session_exercise("Bird Dog", source="app")["exercise"]
    assert lib_default["sets"] == fx.find_exercise("Bird Dog")["default_sets"] and lib_default["added_by"] == "app"
    assert [e["name"] for e in call("get_current_session")["added_exercises"]] == ["Face Pulls", "Bird Dog"]
    assert call("remove_exercise_from_session", exercise="Face Pulls")["status"] == "removed"
    assert [e["name"] for e in tr.get_session_exercises(sid)] == ["Bird Dog"]
    assert tr.add_session_exercise("Face Pulls")["status"] == "added"  # can be re-added


def test_suggestions_prioritise_behind_rehab_and_exclude_done():
    _start()
    call("log_set", exercise="Dumbbell Bench Press", weight="50", reps="10")
    s = call("suggest_exercises", limit=10)
    names = [x["name"] for x in s["suggestions"]]
    assert "Dumbbell Bench Press" not in names
    assert set(s["rehab_behind"]) == set(fx.REHAB_LABELS.values())
    assert s["suggestions"][0]["rehab_targets"], "rehab work should rank first when every target is behind"
    assert s["focus"] == "Chest"
    assert all("barbell" not in x["equipment"] or len(x["equipment"]) > 1 for x in s["suggestions"])
    q = tr.suggest_exercises(q="pull-up", limit=5)
    assert len(q["suggestions"]) >= 3 and all("pull" in x["name"].lower() for x in q["suggestions"][:3])


def test_summary_prs_and_comparison():
    call("log_set", exercise="Dumbbell Bench Press", weight="45", reps="10", date=days_ago(3), program="Arsenal")
    sid = _start("Arsenal")
    call("log_set", exercise="db bench press", weight="50", reps="10")
    call("log_set", exercise="Dumbbell Bench Press", weight="50", reps="8")
    shoulder = fx.search_exercises(rehab_target="left_shoulder", category="rehab")[0]["name"]
    call("log_set", exercise=shoulder, weight="band", reps="15")
    tr.add_note("Strong pressing today", kind="praise")
    db.end_workout_session(sid)
    s = call("get_session_summary", session_id=sid)
    assert s["total_sets"] == 3 and s["total_volume"] == 900
    assert s["prs"] == [{"exercise": "db bench press", "weight": 50.0, "previous": 45.0}] or \
        s["prs"][0]["weight"] == 50.0
    assert s["previous"]["total_volume"] == 450 and s["previous"]["volume_change_pct"] == 100
    assert "Left shoulder" in s["rehab_targets_hit"] and s["muscle_groups"]["Chest"] == 2.0
    assert s["coach_notes"][0]["kind"] == "praise" and s["duration_min"] is not None


def test_demo_end_removes_notes_and_added():
    d = db.start_demo_session("Demo")["session_id"]
    tr.add_note("demo note")
    tr.add_session_exercise("Bird Dog")
    db.end_demo_session(d)
    assert rows("SELECT COUNT(*) AS n FROM session_coaching_notes")[0]["n"] == 0
    assert rows("SELECT COUNT(*) AS n FROM exercise_modifications")[0]["n"] == 0


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_http(client):
    assert client.post("/api/session/add-exercise", json={"exercise": "Bird Dog"}).status_code == 400
    sid = client.post("/api/session/start", json={"program": "Arsenal"}).json()["session_id"]
    assert client.post("/api/session/notes", json={"text": "tight left trap", "kind": "pain"}).json()["note"]["author"] == "athlete"
    assert client.post("/api/session/add-exercise", json={"exercise": "Bird Dog"}).json()["status"] == "added"
    cur = client.get("/api/session/current").json()
    assert cur["coach_notes"][0]["text"] == "tight left trap" and cur["added_exercises"][0]["name"] == "Bird Dog"
    sug = client.get("/api/exercises-suggest", params={"session_id": sid, "exclude": "Pallof Press|Push-Ups"}).json()
    assert sug["suggestions"] and not {"Pallof Press", "Push-Ups", "Bird Dog"} & {x["name"] for x in sug["suggestions"]}
    client.post("/api/set/log", json={"exercise": "Bird Dog", "weight": "bodyweight", "reps": "10", "session_id": sid})
    client.post("/api/session/end", json={"session_id": sid})
    s = client.get(f"/api/session/{sid}/summary").json()
    assert s["total_sets"] == 1 and s["ended_at"]
    assert client.get("/api/session/nope_123/summary").status_code == 404


def test_add_to_today_plan():
    r = call("add_exercise_to_today_plan", exercise="bird dog")
    assert r["status"] == "added_to_plan" and r["program_name"] == "Custom" and r["exercise"]["name"] == "Bird Dog"
    assert call("add_exercise_to_today_plan", exercise="Bird Dog").get("error")
    r2 = tr.add_to_today_plan("Face Pulls", sets=4, reps="15")
    plan = db.get_planned_workout(today())
    assert [e["name"] for e in plan["exercises"]] == ["Bird Dog", "Face Pulls"] and plan["exercises"][1]["sets"] == 4
    assert rows("SELECT COUNT(*) AS n FROM planned_workouts WHERE superseded_at IS NOT NULL")[0]["n"] == 1
