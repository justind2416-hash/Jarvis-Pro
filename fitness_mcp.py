"""
fitness_mcp.py — MCP tools for the exercise library, body measurements and goals.
Merged into mcp_server.TOOLS, so they're served by both /mcp (HTTP) and stdio mode.
"""

import fitness as fx
import training
import coaching


def _b(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _slim(ex: dict) -> dict:
    keys = ("id", "name", "muscle_group", "primary_muscles", "secondary_muscles", "category", "equipment",
            "tags", "rehab_targets", "default_sets", "default_reps", "schema_type", "caution")
    return {k: ex.get(k) for k in keys}


# ── exercise library ─────────────────────────────────────────────────────────

def search_exercise_library_tool(args: dict) -> dict:
    rows = fx.search_exercises(
        q=args.get("query", ""), muscle_group=args.get("muscle_group"), tag=args.get("tag"),
        category=args.get("category"), equipment=args.get("equipment"), rehab_target=args.get("rehab_target"),
        muscle=args.get("muscle"), difficulty=args.get("difficulty"), limit=int(args.get("limit", 25)),
    )
    full = _b(args.get("full"))
    return {"count": len(rows), "exercises": rows if full else [_slim(r) for r in rows]}


def get_exercise_details_tool(args: dict) -> dict:
    ex = fx.get_exercise(args.get("exercise") or args.get("id"))
    if not ex:
        guess = fx.exercise_muscles(args.get("exercise", ""))
        return {"error": f"'{args.get('exercise')}' is not in the library", "muscle_guess": guess}
    return {"exercise": ex}


def get_library_facets_tool(_args: dict) -> dict:
    return fx.library_facets()


_EX_FIELDS = ("aliases", "muscle_group", "primary_muscles", "secondary_muscles", "category", "equipment",
              "movement_pattern", "difficulty", "tags", "rehab_targets", "schema_type", "default_sets",
              "default_reps", "cues", "caution", "video_url", "is_active")


def add_library_exercise_tool(args: dict) -> dict:
    return fx.add_exercise(args.get("name", ""), source="mcp", **{k: args.get(k) for k in _EX_FIELDS})


def update_library_exercise_tool(args: dict) -> dict:
    return fx.update_exercise(args.get("exercise") or args.get("id"), source="mcp",
                              **{k: args.get(k) for k in _EX_FIELDS})


# ── measurements ─────────────────────────────────────────────────────────────

def log_measurements_tool(args: dict) -> dict:
    fields = {k: v for k, v in args.items() if k not in ("date", "notes")}
    return fx.log_measurements(args.get("date"), notes=args.get("notes", ""), source="mcp", **fields)


def get_measurements_tool(args: dict) -> dict:
    rows = fx.get_measurements(int(args.get("limit", 20)), include_deleted=_b(args.get("include_deleted")))
    return {"entries": rows, "total": len(rows)}


def update_measurement_tool(args: dict) -> dict:
    if args.get("id") is None:
        return {"error": "id is required"}
    fields = {k: v for k, v in args.items() if k != "id"}
    return fx.update_measurement(int(args["id"]), source="mcp", **fields)


def delete_measurement_tool(args: dict) -> dict:
    if args.get("id") is None:
        return {"error": "id is required"}
    return fx.delete_measurement(int(args["id"]), reason=args.get("reason", ""))


# ── goals ────────────────────────────────────────────────────────────────────

def get_goals_tool(args: dict) -> dict:
    goals = fx.get_goals(include_inactive=_b(args.get("include_inactive")), category=args.get("category"))
    return {"goals": goals, "total": len(goals), "metrics": fx.GOAL_METRICS}


_GOAL_FIELDS = ("category", "metric", "unit", "direction", "start_value", "current_value", "target_value",
                "target_min", "target_max", "start_date", "target_date", "status", "priority", "auto_track", "notes")


def set_goal_tool(args: dict) -> dict:
    return fx.set_goal(args.get("goal_key"), title=args.get("title"), reason=args.get("reason", ""),
                       source="mcp", **{k: args.get(k) for k in _GOAL_FIELDS})


def delete_goal_tool(args: dict) -> dict:
    return fx.delete_goal(args.get("goal_key", ""), reason=args.get("reason", ""))


def get_goal_history_tool(args: dict) -> dict:
    return fx.get_goal_history(args.get("goal_key", ""))


def get_muscle_volume_tool(args: dict) -> dict:
    return fx.muscle_volume(int(args.get("weeks", 4)))


# ── live session (v5.0.3) ────────────────────────────────────────────────────

def get_current_session_tool(_args: dict) -> dict:
    from database import get_current_session_data
    data = get_current_session_data()
    if not data:
        return {"active": False, "session_id": None}
    data["demo"] = str(data["session_id"]).startswith("demo_")
    data["coach_notes"] = training.get_notes(data["session_id"])
    data["added_exercises"] = training.get_session_exercises(data["session_id"])
    return data


def post_coaching_note_tool(args: dict) -> dict:
    return training.add_note(args.get("text", ""), kind=args.get("kind") or "note", exercise=args.get("exercise"),
                             session_id=args.get("session_id"), author="claude", source="mcp")


def get_coaching_notes_tool(args: dict) -> dict:
    return {"notes": training.get_notes(args.get("session_id"))}


def delete_coaching_note_tool(args: dict) -> dict:
    return training.delete_note(int(args.get("id")))


def add_exercise_to_session_tool(args: dict) -> dict:
    return training.add_session_exercise(args.get("exercise", ""), sets=args.get("sets"), reps=args.get("reps"),
                                         weight=args.get("weight"), session_id=args.get("session_id"),
                                         reason=args.get("reason", ""), source="mcp")


def remove_exercise_from_session_tool(args: dict) -> dict:
    return training.remove_session_exercise(args.get("exercise", ""), session_id=args.get("session_id"))


def suggest_exercises_tool(args: dict) -> dict:
    return training.suggest_exercises(args.get("session_id"), exclude=fx._as_list(args.get("exclude")),
                                      limit=int(args.get("limit", 8)), q=args.get("query", ""))


def add_exercise_to_today_plan_tool(args: dict) -> dict:
    return training.add_to_today_plan(args.get("exercise", ""), sets=args.get("sets"), reps=args.get("reps"),
                                      weight=args.get("weight"), source="mcp")


def get_coaching_overview_tool(_args: dict) -> dict:
    return coaching.overview()


def set_coach_brief_tool(args: dict) -> dict:
    return coaching.set_brief(args.get("text", ""), source="mcp")


def start_demo_mode_tool(args: dict) -> dict:
    from database import start_demo_session
    return start_demo_session(args.get("program") or "Demo")


def end_demo_mode_tool(_args: dict) -> dict:
    from database import end_demo_session
    return end_demo_session()


def get_session_summary_tool(args: dict) -> dict:
    return training.session_summary(args.get("session_id"))


_LIST = {"type": ["array", "string"], "items": {"type": "string"}}
_NUM = {"type": ["number", "string"]}

FITNESS_TOOLS = {
    "search_exercise_library": {
        "fn": search_exercise_library_tool,
        "description": (
            "Search the exercise library (200+ exercises incl. rehab/PT). Filters combine; each accepts one value "
            "or a comma list. muscle_group: chest, back, shoulders, arms, legs, glutes, core, rehab, mobility, cardio, "
            "full_body. rehab_target: left_shoulder, thoracic_spine, lumbar_l4_l5, deep_core. tag e.g. v-taper, "
            "lumbar-safe, shoulder-rehab, mcgill-big-3, running-prehab, calisthenics-progression. Rehab work ranks first."),
        "schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Free text: name, alias, muscle, tag, cue words"},
            "muscle_group": {"type": "string"}, "tag": {"type": "string"}, "category": {"type": "string"},
            "equipment": {"type": "string", "description": "dumbbell, cable, bodyweight, band, bench, pullup_bar, ..."},
            "rehab_target": {"type": "string"}, "muscle": {"type": "string", "description": "e.g. lats, rotator_cuff, deep_core"},
            "difficulty": {"type": "string"}, "limit": {"type": "integer", "default": 25},
            "full": {"type": "boolean", "description": "Return every field (cues, caution, aliases…)", "default": False},
        }},
    },
    "get_exercise_details": {
        "fn": get_exercise_details_tool,
        "description": "Full library entry for one exercise by name/alias or id: muscles, tags, rehab targets, cues, caution, default prescription, input schema.",
        "schema": {"type": "object", "properties": {"exercise": {"type": "string"}, "id": {"type": "integer"}}},
    },
    "get_exercise_library_facets": {
        "fn": get_library_facets_tool,
        "description": "Counts of exercises per muscle group, tag, category, equipment and rehab target.",
        "schema": {"type": "object", "properties": {}},
    },
    "add_library_exercise": {
        "fn": add_library_exercise_tool,
        "description": "Add a new exercise to the library. Muscles are guessed from the name when omitted.",
        "schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "aliases": _LIST, "muscle_group": {"type": "string"},
            "primary_muscles": _LIST, "secondary_muscles": _LIST, "category": {"type": "string"}, "equipment": _LIST,
            "movement_pattern": {"type": "string"}, "difficulty": {"type": "string"}, "tags": _LIST,
            "rehab_targets": _LIST, "schema_type": {"type": "string"}, "default_sets": {"type": "integer"},
            "default_reps": {"type": "string"}, "cues": {"type": "string"}, "caution": {"type": "string"},
            "video_url": {"type": "string"},
        }, "required": ["name"]},
    },
    "update_library_exercise": {
        "fn": update_library_exercise_tool,
        "description": "Edit a library exercise (by name/alias or id). Set is_active=false to hide it from browse/suggestions.",
        "schema": {"type": "object", "properties": {
            "exercise": {"type": "string"}, "id": {"type": "integer"}, "aliases": _LIST, "muscle_group": {"type": "string"},
            "primary_muscles": _LIST, "secondary_muscles": _LIST, "category": {"type": "string"}, "equipment": _LIST,
            "movement_pattern": {"type": "string"}, "difficulty": {"type": "string"}, "tags": _LIST,
            "rehab_targets": _LIST, "schema_type": {"type": "string"}, "default_sets": {"type": "integer"},
            "default_reps": {"type": "string"}, "cues": {"type": "string"}, "caution": {"type": "string"},
            "video_url": {"type": "string"}, "is_active": {"type": "boolean"},
        }},
    },
    "log_measurements": {
        "fn": log_measurements_tool,
        "description": "Log body measurements in inches (any subset) and/or body_fat_pct. Shoulders ÷ waist drives the V-taper goal.",
        "schema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, default today"},
            "waist": _NUM, "chest": _NUM, "shoulders": _NUM, "hips": _NUM, "neck": _NUM,
            "left_arm": _NUM, "right_arm": _NUM, "left_thigh": _NUM, "right_thigh": _NUM,
            "left_calf": _NUM, "right_calf": _NUM, "body_fat_pct": _NUM, "notes": {"type": "string"},
        }},
    },
    "get_measurements": {
        "fn": get_measurements_tool,
        "description": "Body measurement history, newest first, with shoulder_waist_ratio per entry.",
        "schema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20},
                                                    "include_deleted": {"type": "boolean", "default": False}}},
    },
    "update_measurement": {
        "fn": update_measurement_tool,
        "description": "Correct a measurement entry by id. Prior values are appended to its notes.",
        "schema": {"type": "object", "properties": {
            "id": {"type": "integer"}, "date": {"type": "string"}, "waist": _NUM, "chest": _NUM, "shoulders": _NUM,
            "hips": _NUM, "neck": _NUM, "left_arm": _NUM, "right_arm": _NUM, "left_thigh": _NUM, "right_thigh": _NUM,
            "left_calf": _NUM, "right_calf": _NUM, "body_fat_pct": _NUM, "notes": {"type": "string"},
        }, "required": ["id"]},
    },
    "delete_measurement": {
        "fn": delete_measurement_tool,
        "description": "Soft-delete a measurement entry by id.",
        "schema": {"type": "object", "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
                   "required": ["id"]},
    },
    "get_goals": {
        "fn": get_goals_tool,
        "description": (
            "Goals with live progress: current (auto-computed from bodyweight / measurements / logged sets when "
            "auto_track), pct toward target, expected_pct on a linear pace, on_track, in_range, days_left. "
            "Rehab goals count days/week with sets tagged for that rehab target."),
        "schema": {"type": "object", "properties": {"include_inactive": {"type": "boolean", "default": False},
                                                    "category": {"type": "string", "description": "body_comp, rehab, running, aesthetic, habit, strength"}}},
    },
    "set_goal": {
        "fn": set_goal_tool,
        "description": (
            "Create a goal or update one by goal_key (prior version kept in goal_history). Auto-tracked metrics: "
            "bodyweight_lbs, waist_in, shoulder_waist_ratio, sessions_per_week, rehab_days_per_week (goal_key must be "
            "rehab_<target>), longest_run_mi, weekly_run_mi. Any other metric is manual — set current_value. "
            "Existing keys: bodyweight_cut, rehab_left_shoulder, rehab_thoracic_spine, rehab_lumbar_l4_l5, "
            "rehab_deep_core, run_distance, v_taper_ratio, waist_down, training_frequency."),
        "schema": {"type": "object", "properties": {
            "goal_key": {"type": "string"}, "title": {"type": "string"}, "category": {"type": "string"},
            "metric": {"type": "string"}, "unit": {"type": "string"},
            "direction": {"type": "string", "description": "increase | decrease | maintain"},
            "start_value": _NUM, "current_value": _NUM, "target_value": _NUM, "target_min": _NUM, "target_max": _NUM,
            "start_date": {"type": "string"}, "target_date": {"type": "string"},
            "status": {"type": "string", "description": "active | achieved | paused | abandoned"},
            "priority": {"type": "integer", "description": "1 = highest"}, "auto_track": {"type": "boolean"},
            "notes": {"type": "string"}, "reason": {"type": "string", "description": "Why it changed (goes to history)"},
        }},
    },
    "delete_goal": {
        "fn": delete_goal_tool,
        "description": "Soft-delete a goal by goal_key (restore by calling set_goal with the same key).",
        "schema": {"type": "object", "properties": {"goal_key": {"type": "string"}, "reason": {"type": "string"}},
                   "required": ["goal_key"]},
    },
    "get_muscle_volume": {
        "fn": get_muscle_volume_tool,
        "description": (
            "Weekly hard sets and load (lb) per muscle group — Chest, Back, Shoulders, Arms, Legs, Glutes, Core — "
            "plus rehab sets, for rolling 7-day windows ending today (newest last). Primary muscles count 1 set, "
            "secondary 0.5; cardio/mobility don't count. this_week.status flags groups under 10 / over 20 sets."),
        "schema": {"type": "object", "properties": {"weeks": {"type": "integer", "default": 4}}},
    },
    "get_current_session": {
        "fn": get_current_session_tool,
        "description": "The live (or today's latest) session as the JARVIS panel shows it: sets grouped by exercise, totals, demo flag, coach notes and exercises added mid-session.",
        "schema": {"type": "object", "properties": {}},
    },
    "post_coaching_note": {
        "fn": post_coaching_note_tool,
        "description": (
            "Post a coaching note to the live session — it appears on the TRAIN screen's COACH panel within ~5s with a "
            "toast. kind: note | cue (form cue) | adjust (load/rep change) | warning | praise | pain (symptom report). "
            "Optional exercise ties it to one movement. Defaults to the live session."),
        "schema": {"type": "object", "properties": {
            "text": {"type": "string"}, "kind": {"type": "string", "default": "note"}, "exercise": {"type": "string"},
            "session_id": {"type": "string"}}, "required": ["text"]},
    },
    "get_coaching_notes": {
        "fn": get_coaching_notes_tool,
        "description": "Coach notes for a session (default: live), newest first. Includes notes the athlete typed in the app (author=athlete).",
        "schema": {"type": "object", "properties": {"session_id": {"type": "string"}}},
    },
    "delete_coaching_note": {
        "fn": delete_coaching_note_tool,
        "description": "Soft-delete a coach note by id.",
        "schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    "add_exercise_to_session": {
        "fn": add_exercise_to_session_tool,
        "description": "Add an exercise to the live session; it appears in TRAIN's queue (ADDED group) with library cues. sets/reps default to the library prescription.",
        "schema": {"type": "object", "properties": {
            "exercise": {"type": "string"}, "sets": {"type": "integer"}, "reps": {"type": "string"},
            "weight": {"type": "string"}, "reason": {"type": "string"}, "session_id": {"type": "string"}},
            "required": ["exercise"]},
    },
    "remove_exercise_from_session": {
        "fn": remove_exercise_from_session_tool,
        "description": "Remove an exercise that was added mid-session (logged sets stay).",
        "schema": {"type": "object", "properties": {"exercise": {"type": "string"}, "session_id": {"type": "string"}},
                   "required": ["exercise"]},
    },
    "suggest_exercises": {
        "fn": suggest_exercises_tool,
        "description": (
            "Ranked exercise ideas for right now with reasons: rehab targets behind their weekly goal, muscle groups "
            "under 10 hard sets this week (V-taper groups weighted up), today's session focus. Excludes what's already "
            "logged/added and gear the home gym lacks."),
        "schema": {"type": "object", "properties": {
            "session_id": {"type": "string"}, "exclude": _LIST, "limit": {"type": "integer", "default": 8},
            "query": {"type": "string", "description": "Optional text filter"}}},
    },
    "add_exercise_to_today_plan": {
        "fn": add_exercise_to_today_plan_tool,
        "description": "Append a library exercise to TODAY's planned workout (before a session starts). Supersedes the plan row (history kept); creates a 'Custom' plan if none. Use add_exercise_to_session once a session is live.",
        "schema": {"type": "object", "properties": {
            "exercise": {"type": "string"}, "sets": {"type": "integer"}, "reps": {"type": "string"}, "weight": {"type": "string"}},
            "required": ["exercise"]},
    },
    "get_coaching_overview": {
        "fn": get_coaching_overview_tool,
        "description": (
            "The HOME coaching panel's data: countdown to Dec 24, weight trend / actual lb-per-week / needed rate / "
            "projection, rehab status per target (days this week, last done, suggested drills), running (longest, "
            "weekly miles), training balance (hard sets per group, push vs pull, V-taper volume) and rule-based insights. "
            "Read this before writing a coach brief."),
        "schema": {"type": "object", "properties": {}},
    },
    "set_coach_brief": {
        "fn": set_coach_brief_tool,
        "description": "Pin a short coaching brief (2-5 sentences: today's priority, what to watch) to the top of the JARVIS HOME panel. Empty text clears it. History is kept.",
        "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    "start_demo_mode": {
        "fn": start_demo_mode_tool,
        "description": (
            "Start DEMO mode (refused while a real session is live). While it's on, everything written — sets, "
            "weigh-ins, measurements, photos, coach notes/briefs, new goals — is tagged is_demo and shows on JARVIS "
            "normally; edits to real goals are recorded so they can be reverted. get_current_session shows demo=true."),
        "schema": {"type": "object", "properties": {"program": {"type": "string"}}},
    },
    "end_demo_mode": {
        "fn": end_demo_mode_tool,
        "description": "End DEMO mode: hard-deletes every demo-tagged row in every table and reverts real goals edited during the demo. Real data is never touched.",
        "schema": {"type": "object", "properties": {}},
    },
    "get_session_summary": {
        "fn": get_session_summary_tool,
        "description": "End-of-session summary: duration, sets, volume vs last session of the same program, per-exercise sets, PRs, muscle-group sets, rehab targets hit, coach notes.",
        "schema": {"type": "object", "properties": {"session_id": {"type": "string"}}},
    },
    "get_goal_history": {
        "fn": get_goal_history_tool,
        "description": "Every prior version of a goal (edits, deletes), newest first.",
        "schema": {"type": "object", "properties": {"goal_key": {"type": "string"}}, "required": ["goal_key"]},
    },
}
