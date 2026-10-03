"""
JARVIS Workout MCP Server
Exposes workout data as MCP tools for Claude integration.

Can run in two modes:
  1. stdio (standalone): python mcp_server.py
  2. HTTP (integrated): imported by main.py, tools served via /mcp endpoint

All data now persists in SQLite via database.py.
JSON files in data/ are kept as defaults/fallbacks for profile and program config.
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime, date

from database import (
    log_workout_set,
    log_bodyweight_entry,
    get_bodyweight_history,
    get_recent_sessions,
    get_carry_forward_items,
    get_full_history,
    add_carry_forward,
    resolve_carry_forward,
    start_session_v2,
    end_session_v2,
    delete_set,
    confirm_delete_set,
    delete_session,
    confirm_delete_session,
    get_session_log,
    save_planned_schedule,
    get_planned_workout,
    get_planned_schedule,
    mark_planned_workout_done,
    mark_planned_workout_skipped,
    get_workout_compliance,
    set_exercise_schema,
    compute_and_log_variance,
    get_session_variance_report,
    log_deviation,
    get_session_deviations,
    get_recent_deviations,
    upsert_context,
    get_context,
    get_full_coaching_context,
    get_exercise_schema,
    get_all_exercise_schemas,
    DEVIATION_REASONS,
    find_or_create_session_for_date,
    restore_set,
    restore_session,
    update_set,
    get_set_history,
    update_session,
    get_session_history,
    get_planned_vs_actual,
    get_or_create_today_session,
    audit,
    _local_today,
    get_photos_for_mcp,
    update_photo,
    soft_delete_photo,
    backfill_planned_program,
    skip_exercise,
    save_dispatch_report)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Default data (for profile and program — these stay in JSON for easy editing)
# ---------------------------------------------------------------------------

DEFAULT_PROFILE = {
    "name": "New Athlete",
    "age": 0,
    "dob": "",
    "height": "",
    "weight_lbs": 0,
    "goals": {
        "primary": "",
        "selected": []
    },
    "experience": "",
    "equipment": [],
    "training_frequency": 0,
    "training_split": "",
    "limitations": "",
    "onboarded_at": ""
}

DEFAULT_PROGRAM = {
    "name": "Awaiting Coach Setup",
    "phase": "Onboarding",
    "exercises": [],
    "notes": "Connect Claude to design your first program."
}

DEFAULT_HISTORY = {"sessions": []}
DEFAULT_BODYWEIGHT = {"entries": []}

# ---------------------------------------------------------------------------
# JSON data helpers (for profile and program config files only)
# ---------------------------------------------------------------------------

def _path(name: str) -> Path:
    return DATA_DIR / name


def _load(name: str, default: dict) -> dict:
    p = _path(name)
    if not p.exists():
        _save(name, default)
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, data: dict) -> None:
    p = _path(name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Tool implementations — now backed by SQLite via database.py
# ---------------------------------------------------------------------------

def get_profile(_args: dict) -> dict:
    """Return the athlete's athlete profile."""
    return _load("profile.json", DEFAULT_PROFILE)


def get_program(_args: dict) -> dict:
    """Return the current workout program."""
    return _load("program.json", DEFAULT_PROGRAM)


def _bool_arg(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def get_history(args: dict) -> dict:
    """Return recent workout session logs from the database."""
    limit = int(args.get("limit", 5))
    return get_full_history(limit, include_deleted=_bool_arg(args.get("include_deleted", False)))


def get_carry_forward(_args: dict) -> dict:
    """Return items carried forward from previous sessions."""
    items = get_carry_forward_items()
    return {"carry_forward": [item["item"] for item in items]}


def log_set(args: dict) -> dict:
    """Log a single set. Required: exercise. Optional: weight, reps, rpe, notes, date, session_id."""
    exercise = args.get("exercise")
    if not exercise:
        return {"error": "exercise is required"}

    weight = args.get("weight")
    reps = args.get("reps")
    session_date = args.get("date")
    session_id = args.get("session_id")
    program = args.get("program", "")
    supersedes = args.get("supersedes_set_id")

    # If a session_id is given, use it directly
    # If a date is given (but no session_id), find or create a session for that date
    # (a past date creates a backfill session and never touches today's live session)
    if not session_id and session_date:
        sess = find_or_create_session_for_date(session_date, program)
        if sess.get("error"):
            return sess
        session_id = str(sess["session_id"])
    elif not session_id:
        # No session_id and no date — use today's session
        session_id = get_or_create_today_session()

    source = args.get("source")
    if not source:
        if supersedes not in (None, ""):
            source = "correction"
        elif session_date and session_date < _local_today():
            source = "backfill"
        else:
            source = "mcp"

    audit("set_logged", f"{exercise} {weight} x {reps}", source="mcp", session_id=session_id or "")
    return log_workout_set(
        exercise=exercise,
        weight=str(weight) if weight is not None else "bodyweight",
        reps=str(reps) if reps is not None else "",
        rpe=str(args.get("rpe", "")),
        notes=args.get("notes", ""),
        session_id=session_id,
        source=source,
        performed_at=args.get("performed_at"),
        supersedes_set_id=int(supersedes) if supersedes not in (None, "") else None,
    )


def update_program(args: dict) -> dict:
    """Update working weights or exercises. Accepts 'exercise' name and fields to update."""
    exercise_name = args.get("exercise")
    if not exercise_name:
        return {"error": "exercise name is required"}

    program = _load("program.json", DEFAULT_PROGRAM)
    found = False
    for ex in program.get("exercises", []):
        if ex["name"].lower() == exercise_name.lower():
            for key in ("working_weight", "sets", "reps", "notes"):
                if key in args:
                    ex[key] = args[key]
            found = True
            break

    if not found:
        return {"error": f"Exercise '{exercise_name}' not found in program"}

    _save("program.json", program)
    return {"status": "updated", "program": program}


def log_bodyweight(args: dict) -> dict:
    """Log a bodyweight measurement. Required: weight. Optional: date (YYYY-MM-DD), notes."""
    weight = args.get("weight")
    if weight is None:
        return {"error": "weight is required"}

    return log_bodyweight_entry(
        weight_lbs=float(weight),
        dt=args.get("date"),
        notes=args.get("notes", ""),
        fasted=args.get("fasted"),
        time_of_day=args.get("time_of_day"),
        off_protocol=args.get("off_protocol", False),
    )


def get_session_summaries(args: dict) -> dict:
    """Return summaries of recent sessions."""
    limit = int(args.get("limit", 5))
    sessions = get_recent_sessions(limit)
    summaries = []
    for s in sessions:
        summaries.append({
            "id": s.get("id"),
            "session_id": s.get("session_id"),
            "date": s.get("date"),
            "program": s.get("program", ""),
            "exercises": s.get("exercises", []),
            "total_sets": s.get("total_sets", 0),
            "sets": [{k: x.get(k) for k in ("id", "exercise", "weight", "reps", "rpe")} for x in s.get("sets", [])],
        })
    return {"summaries": summaries, "total_sessions": len(summaries)}


def get_bodyweight_history_tool(args: dict) -> dict:
    """Return bodyweight history for trend tracking."""
    limit = int(args.get("limit", 30))
    entries = get_bodyweight_history(limit, include_off_protocol=_bool_arg(args.get("include_off_protocol", True)))
    return {"entries": entries, "total": len(entries)}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# V2 MCP Tool Functions
# ---------------------------------------------------------------------------

def start_session_tool(args: dict) -> dict:
    """Start a new workout session."""
    program = args.get("program", "Strength B")
    session_date = args.get("date")
    start_time = args.get("start_time")
    return start_session_v2(program, session_date, start_time, notes=args.get("notes", ""), source="mcp")


def end_session_tool(args: dict) -> dict:
    """End a workout session (integer id or text session_id)."""
    session_id = args.get("session_id")
    audit("session_ended", f"session_id={session_id}", source="mcp")
    return end_session_v2(session_id, args.get("end_time"),
                          notes=args.get("notes", ""), source="mcp")


def update_session_tool(args: dict) -> dict:
    session_id = args.get("session_id")
    if not session_id:
        return {"error": "session_id is required"}
    return update_session(
        session_id, reason=args.get("reason", ""), source="mcp",
        notes_mode=args.get("notes_mode", "replace"),
        notes=args.get("notes"), program=args.get("program"),
        started_at=args.get("started_at"), ended_at=args.get("ended_at"),
    )


def get_session_history_tool(args: dict) -> dict:
    if not args.get("session_id"):
        return {"error": "session_id is required"}
    return get_session_history(args["session_id"])


def delete_set_tool(args: dict) -> dict:
    """Soft-delete a set. Requires confirm=true."""
    set_id = int(args.get("set_id", 0))
    if _bool_arg(args.get("confirm", False)):
        return confirm_delete_set(set_id, reason=args.get("reason", ""), source="mcp")
    return delete_set(set_id)


def restore_set_tool(args: dict) -> dict:
    return restore_set(int(args.get("set_id", 0)), reason=args.get("reason", ""), source="mcp")


def delete_session_tool(args: dict) -> dict:
    """Soft-delete a session and its sets. Requires confirm=true."""
    session_id = args.get("session_id")
    if _bool_arg(args.get("confirm", False)):
        return confirm_delete_session(session_id, reason=args.get("reason", ""), source="mcp")
    return delete_session(session_id)


def restore_session_tool(args: dict) -> dict:
    return restore_session(args.get("session_id"), reason=args.get("reason", ""), source="mcp")


def update_set_tool(args: dict) -> dict:
    set_id = args.get("set_id")
    if set_id in (None, ""):
        return {"error": "set_id is required"}
    return update_set(
        int(set_id), reason=args.get("reason", ""), source="correction",
        weight=args.get("weight"), reps=args.get("reps"), rpe=args.get("rpe"),
        notes=args.get("notes"), exercise=args.get("exercise"), performed_at=args.get("performed_at"),
    )


def get_set_history_tool(args: dict) -> dict:
    set_id = args.get("set_id")
    if set_id in (None, ""):
        return {"error": "set_id is required"}
    return get_set_history(int(set_id))


def get_planned_vs_actual_tool(args: dict) -> dict:
    return get_planned_vs_actual(args.get("session_id"), args.get("date"))


def get_session_log_tool(args: dict) -> dict:
    """Get the session log view showing training days and gaps."""
    return {"log": get_session_log(include_deleted=_bool_arg(args.get("include_deleted", False)))}



def search_chat_history_tool(args: dict) -> dict:
    """Search conversation history for pain reports, coaching notes, form cues, etc."""
    from database import search_chat_history
    return {
        "results": search_chat_history(
            query=args.get("query"),
            tags=args.get("tags"),
            message_type=args.get("message_type"),
            exercise=args.get("exercise"),
            sentiment=args.get("sentiment"),
            days=int(args.get("days", 30)),
            limit=int(args.get("limit", 20)),
        )
    }


def get_chat_context_tool(args: dict) -> dict:
    """Get recent conversation context for continuity."""
    from database import get_recent_chat_history
    return {
        "messages": get_recent_chat_history(
            limit=int(args.get("limit", 50)),
            days=int(args.get("days", 7)),
        )
    }

def set_planned_program_tool(args: dict) -> dict:
    """Save a multi-day workout schedule from the Claude Desktop project."""
    schedule = args.get("schedule", [])
    if not schedule:
        return {"error": "No schedule provided"}
    result = save_planned_schedule(schedule)
    if result.get("error"):
        return {"ok": False, **result}
    return {"ok": True, **result}


def get_today_workout_tool(args: dict) -> dict:
    """Get today's planned workout."""
    target_date = args.get("date")
    workout = get_planned_workout(target_date)
    if not workout:
        return {"message": "No planned workout for this date", "fallback": "Use default program"}
    return workout


def get_schedule_tool(args: dict) -> dict:
    """Get the full planned schedule for the next N days."""
    days = args.get("days", 14)
    try:
        schedule = get_planned_schedule(days)
    except Exception as e:
        schedule = []
        print(f"[MCP] get_planned_schedule error: {e}")
    try:
        compliance = get_workout_compliance(days)
    except Exception as e:
        compliance = {"error": str(e)}
        print(f"[MCP] get_workout_compliance error: {e}")
    return {"schedule": schedule, "compliance": compliance}


def mark_workout_done_tool(args: dict) -> dict:
    """Mark a planned workout as completed."""
    planned_date = args.get("date")
    session_id = args.get("session_id")
    if not planned_date:
        return {"error": "date is required"}
    ok = mark_planned_workout_done(planned_date, session_id)
    return {"ok": ok}


def mark_workout_skipped_tool(args: dict) -> dict:
    """Mark a planned workout as skipped."""
    planned_date = args.get("date")
    reason = args.get("reason", "")
    if not planned_date:
        return {"error": "date is required"}
    ok = mark_planned_workout_skipped(planned_date, reason)
    return {"ok": ok}


def get_compliance_tool(args: dict) -> dict:
    """Get workout compliance stats."""
    days = args.get("days", 14)
    return get_workout_compliance(days)



def upsert_context_tool(args):
    category = args.get("category", "")
    key = args.get("key", "")
    ctx = args.get("content", "")
    if not category or not key or not ctx:
        return {"error": "category, key, and content are required"}
    return upsert_context(category, key, ctx, source=args.get("source", "project"))

def get_context_tool(args):
    return {"documents": get_context(args.get("category"), args.get("key")), "count": len(get_context(args.get("category"), args.get("key")))}

def get_full_context_tool(args):
    return {"context": get_full_coaching_context()}


def log_deviation_tool(args):
    return log_deviation(
        exercise=args.get("exercise", ""),
        planned_weight=args.get("planned_weight", ""),
        planned_reps=args.get("planned_reps", ""),
        actual_weight=args.get("actual_weight", ""),
        actual_reps=args.get("actual_reps", ""),
        deviation_type=args.get("deviation_type", "other"),
        reason=args.get("reason", ""),
        planned_notes=args.get("planned_notes", ""),
        actual_notes=args.get("actual_notes", ""),
        set_number=args.get("set_number", 1),
        session_id=args.get("session_id"),
        set_id=args.get("set_id"),
    )

def get_deviations_tool(args):
    session_id = args.get("session_id")
    date = args.get("date")
    if session_id or date:
        return {"deviations": get_session_deviations(session_id, date)}
    return {"deviations": get_recent_deviations(args.get("days", 14))}


def get_schema_tool(args):
    name = args.get("exercise", "")
    if name:
        return get_exercise_schema(name)
    return get_all_exercise_schemas()

def set_schema_tool(args):
    return set_exercise_schema(args.get("exercise", ""), args.get("type", ""))


def log_variance_tool(args):
    return compute_and_log_variance(
        session_id=args.get('session_id', ''),
        exercise=args.get('exercise', ''),
        planned_sets=args.get('planned_sets', 0),
        planned_reps=args.get('planned_reps', ''),
        planned_weight=args.get('planned_weight', ''),
        actual_sets=args.get('actual_sets', 0),
        actual_reps=args.get('actual_reps', ''),
        actual_weight=args.get('actual_weight', ''),
        reason_code=args.get('reason_code', 'other'),
        attribution=args.get('attribution', 'athlete_initiated'),
        detail=args.get('detail', ''),
        planned_weight_left=args.get('planned_weight_left', ''),
        planned_weight_right=args.get('planned_weight_right', ''),
        actual_weight_left=args.get('actual_weight_left', ''),
        actual_weight_right=args.get('actual_weight_right', ''),
    )

def get_variance_report_tool(args):
    return get_session_variance_report(
        session_id=args.get('session_id'),
        date=args.get('date'),
    )


def get_photos_tool(args):
    return {"photos": get_photos_for_mcp(
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        photo_type=args.get("type", "progress"),
        angle=args.get("angle"),
        limit=args.get("limit", 50),
        include_demo=args.get("include_demo", False),
        include_image=args.get("include_image", False)
    )}

def update_photo_tool(args):
    pid = args.get("photo_id")
    if not pid:
        return {"error": "photo_id required"}
    return update_photo(int(pid), date=args.get("date"), photo_type=args.get("type"),
                        angle=args.get("angle"), notes=args.get("notes"),
                        bodyweight=args.get("bodyweight"))

def delete_photo_tool(args):
    pid = args.get("photo_id")
    if not pid:
        return {"error": "photo_id required"}
    return soft_delete_photo(int(pid), reason=args.get("reason", ""))

def backfill_plan_tool(args):
    schedule = args.get("schedule", [])
    if not schedule:
        return {"error": "schedule required"}
    return backfill_planned_program(schedule)

def skip_exercise_tool(args):
    session_id = args.get("session_id")
    exercise = args.get("exercise")
    if not exercise:
        return {"error": "exercise name required"}
    if not session_id:
        from database import get_or_create_today_session
        session_id = get_or_create_today_session()
    return skip_exercise(session_id, exercise, 
                         reason=args.get("reason", "skipped_by_user"),
                         detail=args.get("detail", ""))

def submit_dispatch_tool(args):
    report = args.get("report")
    if not report:
        return {"error": "report text required"}
    return save_dispatch_report(report, 
                                category=args.get("category", "session_report"),
                                priority=args.get("priority", "normal"))


TOOLS = {
    "get_profile": {
        "fn": get_profile,
        "description": "Returns the athlete's profile including age, weight, goals, equipment, limitations, and training split.",
        "schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_program": {
        "fn": get_program,
        "description": "Returns the current Strength B workout program with all exercises, sets, reps, working weights, and notes.",
        "schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "get_history": {
        "fn": get_history,
        "description": "Returns recent workout session logs. Every set includes its integer 'id' (use it with update_set / delete_set / get_set_history). 'sets' holds the sets that count; corrected-away sets are listed under 'superseded_sets'. Deleted sessions/sets are hidden unless include_deleted=true.",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of recent sessions to return",
                    "default": 5
                },
                "include_deleted": {
                    "type": "boolean",
                    "description": "Also return soft-deleted sessions and a 'deleted_sets' list per session",
                    "default": False
                }
            },
            "required": []
        }
    },
    "get_carry_forward": {
        "fn": get_carry_forward,
        "description": "Returns items carried forward from previous sessions that still need to be addressed.",
        "schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "log_set": {
        "fn": log_set,
        "description": "Log a single set; returns its integer set_id. Pass session_id to attach to a specific session, or date to auto-find/create a session for that date (a past date creates a backfill session — actual-only if no plan existed). Without either, groups into today's session. If today's session has a plan, a deviation is auto-logged when weight/reps/set count differ from it. To correct a set, prefer update_set; or log a replacement with supersedes_set_id (the old set stays in the record but leaves analytics).",
        "schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "Exercise name (e.g. 'Dumbbell Bench Press')"
                },
                "weight": {
                    "type": "string",
                    "description": "Weight used (e.g. '25 lb each', '100 lb', 'bodyweight')"
                },
                "reps": {
                    "type": ["integer", "string"],
                    "description": "Reps completed (number or string like '30s hold')"
                },
                "rpe": {
                    "type": ["number", "string"],
                    "description": "Rate of perceived exertion (1-10 scale)",
                    "default": ""
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes about the set",
                    "default": ""
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID to attach this set to. Use the session_id returned by start_session."
                },
                "date": {
                    "type": "string",
                    "description": "Date (YYYY-MM-DD) to log this set under. Finds or creates a session for that date."
                },
                "program": {
                    "type": "string",
                    "description": "Program name if creating a new session via date (e.g. Ironforge, Arsenal)"
                },
                "performed_at": {
                    "type": "string",
                    "description": "ISO timestamp of when the set was actually performed (for after-the-fact logging). Defaults to now; left empty for backfill."
                },
                "supersedes_set_id": {
                    "type": "integer",
                    "description": "Integer id of an earlier set this one corrects. The earlier set is kept but excluded from analytics."
                },
                "source": {
                    "type": "string",
                    "enum": ["app", "mcp", "backfill", "correction"],
                    "description": "Where the set came from. Defaults: mcp; backfill for past dates; correction when supersedes_set_id is set."
                }
            },
            "required": ["exercise", "weight", "reps"]
        }
    },
    "update_program": {
        "fn": update_program,
        "description": "Updates working weights, sets, reps, or notes for an exercise in the program.",
        "schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": "Exercise name to update (case-insensitive match)"
                },
                "working_weight": {
                    "type": "string",
                    "description": "New working weight"
                },
                "sets": {
                    "type": "integer",
                    "description": "New number of sets"
                },
                "reps": {
                    "type": "string",
                    "description": "New rep range"
                },
                "notes": {
                    "type": "string",
                    "description": "New notes"
                }
            },
            "required": ["exercise"]
        }
    },
    "log_bodyweight": {
        "fn": log_bodyweight,
        "description": "Logs a bodyweight measurement with date for trend tracking.",
        "schema": {
            "type": "object",
            "properties": {
                "weight": {
                    "type": "number",
                    "description": "Bodyweight in pounds"
                },
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format (defaults to today)"
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes",
                    "default": ""
                },
                "fasted": {
                    "type": "boolean",
                    "description": "Whether the weigh-in was fasted. Omit if unknown."
                },
                "time_of_day": {
                    "type": "string",
                    "description": "When the weigh-in happened, e.g. '06:10' or 'morning'. Defaults to the current time for today's entries."
                },
                "off_protocol": {
                    "type": "boolean",
                    "description": "True if this weigh-in breaks the fasted-morning protocol (after meals, travel scale...). Kept in the record, excluded from trend analytics.",
                    "default": False
                }
            },
            "required": ["weight"]
        }
    },
    "get_session_summaries": {
        "fn": get_session_summaries,
        "description": "Returns high-level summaries of recent sessions (session ids, date, exercises done, total sets, and each counted set with its integer id).",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of recent sessions to summarize",
                    "default": 5
                }
            },
            "required": []
        }
    },
    "get_bodyweight_history": {
        "fn": get_bodyweight_history_tool,
        "description": "Returns bodyweight history entries (with fasted, time_of_day, off_protocol) for trend tracking and analysis.",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of recent entries to return",
                    "default": 30
                },
                "include_off_protocol": {
                    "type": "boolean",
                    "description": "Set false to return only protocol weigh-ins for trend analysis",
                    "default": True
                }
            },
            "required": []
        }
    },
    "start_session": {
        "description": "Start a workout session. Returns session_id (text, e.g. session_2026-09-26_Ironforge_a1b2c3) and db_id (integer). The day's plan is frozen into the session at start for planned-vs-actual. A past date creates a backfill session without touching today's live session.",
        "fn": start_session_tool,
        "schema": {
            "type": "object",
            "properties": {
                "program": {"type": "string", "description": "Program name (Strength A, Strength B, Easy run)"},
                "date": {"type": "string", "description": "Session date YYYY-MM-DD. Defaults to today. Past dates = backfill."},
                "start_time": {"type": "string", "description": "Start time ISO format. Optional."},
                "notes": {"type": "string", "description": "Session notes (how you feel, context)."}
            },
            "required": ["program"]
        }
    },
    "end_session": {
        "description": "End a workout session. Returns duration, summary and set ids. The end time defaults to the last set's timestamp (not now).",
        "fn": end_session_tool,
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "string"], "description": "Integer id or text session_id. If omitted, ends most recent open session."},
                "end_time": {"type": "string", "description": "End time ISO format. Defaults to the last logged set's time."},
                "notes": {"type": "string", "description": "Session notes to append at the end."}
            },
            "required": []
        }
    },
    "delete_set": {
        "description": "Soft-delete a set (for mistakes). The set is kept, hidden from reads and excluded from analytics, and can be restored with restore_set. Shows the record first; set confirm=true to delete. To fix wrong numbers, use update_set instead.",
        "fn": delete_set_tool,
        "schema": {
            "type": "object",
            "properties": {
                "set_id": {"type": "integer", "description": "Integer set id to delete"},
                "confirm": {"type": "boolean", "description": "Must be true to actually delete"},
                "reason": {"type": "string", "description": "Why it's being deleted (duplicate, logged by mistake...)"}
            },
            "required": ["set_id"]
        }
    },
    "restore_set": {
        "description": "Undo a soft delete of a set.",
        "fn": restore_set_tool,
        "schema": {
            "type": "object",
            "properties": {
                "set_id": {"type": "integer", "description": "Integer set id to restore"},
                "reason": {"type": "string"}
            },
            "required": ["set_id"]
        }
    },
    "update_set": {
        "description": "Correct a logged set in place (any date — past actuals stay writable). Prior values are saved to set history first; only fields you pass change. Re-checks the set against the session's plan.",
        "fn": update_set_tool,
        "schema": {
            "type": "object",
            "properties": {
                "set_id": {"type": "integer", "description": "Integer set id"},
                "weight": {"type": "string"},
                "reps": {"type": ["integer", "string"]},
                "rpe": {"type": ["number", "string"]},
                "notes": {"type": "string"},
                "exercise": {"type": "string", "description": "Correct the exercise name"},
                "performed_at": {"type": "string", "description": "ISO timestamp the set was performed"},
                "reason": {"type": "string", "description": "Why the correction was made"}
            },
            "required": ["set_id"]
        }
    },
    "get_set_history": {
        "description": "Show a set's current values and every prior version (edits, deletes, restores), newest first.",
        "fn": get_set_history_tool,
        "schema": {
            "type": "object",
            "properties": {"set_id": {"type": "integer"}},
            "required": ["set_id"]
        }
    },
    "delete_session": {
        "description": "Soft-delete a session and cascade to its sets. Everything is kept and can be restored with restore_session. Shows record first; set confirm=true to delete.",
        "fn": delete_session_tool,
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "string"], "description": "Integer id or text session_id"},
                "confirm": {"type": "boolean", "description": "Must be true to actually delete"},
                "reason": {"type": "string", "description": "Why it's being deleted"}
            },
            "required": ["session_id"]
        }
    },
    "restore_session": {
        "description": "Undo a soft delete of a session, restoring the sets that the session delete removed.",
        "fn": restore_session_tool,
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "string"], "description": "Integer id or text session_id"},
                "reason": {"type": "string"}
            },
            "required": ["session_id"]
        }
    },
    "update_session": {
        "description": "Edit a session's notes, program, started_at or ended_at. The prior values are saved to session history first.",
        "fn": update_session_tool,
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "string"], "description": "Integer id or text session_id"},
                "notes": {"type": "string"},
                "notes_mode": {"type": "string", "enum": ["replace", "append"], "description": "append adds to existing notes (default replace)"},
                "program": {"type": "string"},
                "started_at": {"type": "string", "description": "ISO timestamp"},
                "ended_at": {"type": "string", "description": "ISO timestamp"},
                "reason": {"type": "string", "description": "Why it changed"}
            },
            "required": ["session_id"]
        }
    },
    "get_session_history": {
        "description": "Show a session's current row and every prior version (edits, deletes, restores, reopen), newest first.",
        "fn": get_session_history_tool,
        "schema": {
            "type": "object",
            "properties": {"session_id": {"type": ["integer", "string"]}},
            "required": ["session_id"]
        }
    },
    "get_planned_vs_actual": {
        "description": "Compare the plan frozen at session start with what was actually done (counted sets with ids). Returns mode 'actual_only' when no plan existed. Defaults to today's session.",
        "fn": get_planned_vs_actual_tool,
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "string"]},
                "date": {"type": "string", "description": "YYYY-MM-DD (used when session_id is omitted)"}
            }
        }
    },
    "get_session_log": {
        "description": "Get the session log showing training days, gaps, duration, set counts and the integer ids of each session's sets.",
        "fn": get_session_log_tool,
        "schema": {
            "type": "object",
            "properties": {
                "include_deleted": {"type": "boolean", "description": "Include soft-deleted sessions and deleted set ids"}
            },
            "required": []
        }
    },
    "search_chat_history": {
        "description": "Search conversation history for pain reports, coaching notes, form cues, exercise discussions, and any past conversation.",
        "fn": search_chat_history_tool,
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for in messages"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags: pain, shoulder, back, form, PR, easy, hard, etc."},
                "message_type": {"type": "string", "description": "Filter: general, workout_log, coaching, pain_report, form_cue, question"},
                "exercise": {"type": "string", "description": "Filter by exercise name"},
                "sentiment": {"type": "string", "description": "Filter: positive, neutral, concern, pain"},
                "days": {"type": "integer", "description": "How many days back to search (default 30)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"}
            },
            "required": []
        }
    },
    "set_planned_program": {
        "description": "Save a multi-day workout schedule. Dates before today are rejected (past plans are immutable) and nothing is saved in that case; re-planning a date keeps the old plan as superseded. The Claude Desktop project uses this to push a 2-week program. Each day needs: date (YYYY-MM-DD), program_name, exercises (list with name/weight/reps/sets), warmup (list), carry_forward (list), description, notes.",
        "schema": {
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "array",
                    "description": "Array of daily workout plans",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "program_name": {"type": "string"},
                            "exercises": {"type": "array"},
                            "warmup": {"type": "array"},
                            "carry_forward": {"type": "array"},
                            "description": {"type": "string"},
                            "notes": {"type": "string"}
                        },
                        "required": ["date", "program_name", "exercises"]
                    }
                }
            },
            "required": ["schedule"]
        },
        "fn": set_planned_program_tool,
    },
    "get_today_workout": {
        "description": "Get the planned workout for today (or a specific date). Returns the full exercise list, warmup, and carry-forward items as designed by the project.",
        "schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Optional YYYY-MM-DD, defaults to today"}
            }
        },
        "fn": get_today_workout_tool,
    },
    "get_planned_schedule": {
        "description": "Get the full planned workout schedule for the next N days, plus compliance stats (planned/completed/skipped/pending).",
        "schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days to look ahead, default 14"}
            }
        },
        "fn": get_schedule_tool,
    },
    "mark_workout_done": {
        "description": "Mark a planned workout as completed. Call this when a session ends.",
        "schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD of the planned workout"},
                "session_id": {"type": "string", "description": "The actual session ID"}
            },
            "required": ["date"]
        },
        "fn": mark_workout_done_tool,
    },
    "mark_workout_skipped": {
        "description": "Mark a planned workout as skipped. Call this during nightly sync if user didn't work out.",
        "schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "reason": {"type": "string", "description": "Why it was skipped"}
            },
            "required": ["date"]
        },
        "fn": mark_workout_skipped_tool,
    },
    "get_compliance": {
        "description": "Get workout compliance stats: planned vs completed vs skipped over the past N days.",
        "schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback period in days, default 14"}
            }
        },
        "fn": get_compliance_tool,
    },
        "upsert_coaching_context": {
        "description": "Write or update a coaching context document. Categories: injury, form, programming, profile, preferences, equipment, history, coaching_notes.",
        "schema": {"type": "object", "properties": {"category": {"type": "string"}, "key": {"type": "string"}, "content": {"type": "string"}, "source": {"type": "string"}}, "required": ["category", "key", "content"]},
        "fn": upsert_context_tool,
    },
    "get_coaching_context": {
        "description": "Read coaching context documents. Filter by category and/or key.",
        "schema": {"type": "object", "properties": {"category": {"type": "string"}, "key": {"type": "string"}}},
        "fn": get_context_tool,
    },
    "get_full_coaching_context": {
        "description": "Get complete coaching context as formatted text for JARVIS system prompt.",
        "schema": {"type": "object", "properties": {}},
        "fn": get_full_context_tool,
    },
        "log_deviation": {
        "description": "Log a deviation between planned and actual exercise performance. Use when the athlete modifies weight, reps, incline, or swaps/skips an exercise. deviation_type: intensity_reduction, volume_reduction, exercise_swap, early_stop, weight_increase, weight_decrease, form_modification, skipped, other.",
        "schema": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string", "description": "Exercise name"},
                "planned_weight": {"type": "string"},
                "planned_reps": {"type": "string"},
                "actual_weight": {"type": "string"},
                "actual_reps": {"type": "string"},
                "deviation_type": {"type": "string", "description": "intensity_reduction, volume_reduction, exercise_swap, early_stop, weight_increase, weight_decrease, form_modification, skipped, other"},
                "reason": {"type": "string", "description": "Why the deviation happened"},
                "planned_notes": {"type": "string"},
                "actual_notes": {"type": "string"},
                "set_number": {"type": "integer"},
                "session_id": {"type": "string"},
                "set_id": {"type": "integer", "description": "Integer id of the set this deviation is about"}
            },
            "required": ["exercise", "deviation_type"]
        },
        "fn": log_deviation_tool,
    },
    "get_deviations": {
        "description": "Get exercise deviations (planned vs actual). Filter by session_id, date, or get recent N days. Used by nightly sync to report what changed.",
        "schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "days": {"type": "integer", "description": "Lookback days, default 14"}
            }
        },
        "fn": get_deviations_tool,
    },
        "get_exercise_schema": {
        "description": "Get the input schema for an exercise (fields, units, type) or all schemas if no exercise specified.",
        "schema": {"type": "object", "properties": {"exercise": {"type": "string"}}},
        "fn": get_schema_tool,
    },
    "set_exercise_schema": {
        "description": "Set the schema type for an exercise. Types: strength_standard, strength_unilateral, treadmill, duration_hold, mobility, mobility_bilateral, carry, cardio_zone, test.",
        "schema": {"type": "object", "properties": {"exercise": {"type": "string"}, "type": {"type": "string"}}, "required": ["exercise", "type"]},
        "fn": set_schema_tool,
    },
        "log_variance": {
        "description": "Log budget vs actual variance for an exercise. Computes set/weight deviation, records reason code and attribution. Reason codes: pain_or_symptom, coach_directed_stop, form_breakdown, equipment, time, felt_light, exceeded_prescription, other. Attribution: athlete_initiated or coach_directed. Supports laterality (left/right weights).",
        "schema": {"type": "object", "properties": {
            "session_id": {"type": "string"}, "exercise": {"type": "string"},
            "planned_sets": {"type": "integer"}, "planned_reps": {"type": "string"}, "planned_weight": {"type": "string"},
            "actual_sets": {"type": "integer"}, "actual_reps": {"type": "string"}, "actual_weight": {"type": "string"},
            "reason_code": {"type": "string", "description": "pain_or_symptom|coach_directed_stop|form_breakdown|equipment|time|felt_light|exceeded_prescription|other"},
            "attribution": {"type": "string", "description": "athlete_initiated or coach_directed"},
            "detail": {"type": "string"},
            "planned_weight_left": {"type": "string"}, "planned_weight_right": {"type": "string"},
            "actual_weight_left": {"type": "string"}, "actual_weight_right": {"type": "string"}
        }, "required": ["exercise"]},
        "fn": log_variance_tool,
    },
    "get_variance_report": {
        "description": "Get budget vs actual variance report for a session. Shows per-exercise deviations, reason code summary, over/under budget counts.",
        "schema": {"type": "object", "properties": {
            "session_id": {"type": "string"}, "date": {"type": "string"}
        }},
        "fn": get_variance_report_tool,
    },
    "get_photos": {
        "description": "Get progress photos with metadata. Returns id, date, type (progress/segment/other), angle, url, notes, bodyweight. Excludes segment dividers by default.",
        "schema": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "type": {"type": "string", "description": "progress (default), segment, other, or all"},
            "angle": {"type": "string", "description": "front, side, back, other"},
            "limit": {"type": "integer", "description": "Max photos, default 50"},
            "include_demo": {"type": "boolean"},
            "include_image": {"type": "boolean", "description": "Return base64 image data inline. Use sparingly for large batches."}
        }},
        "fn": get_photos_tool,
    },
    "update_photo": {
        "description": "Update photo metadata: date, type (progress/segment/other), angle, notes. Use to correct backfilled photos.",
        "schema": {"type": "object", "properties": {
            "photo_id": {"type": "integer"},
            "date": {"type": "string"},
            "type": {"type": "string", "description": "progress, segment, other"},
            "angle": {"type": "string"},
            "notes": {"type": "string"},
            "bodyweight": {"type": "number", "description": "Bodyweight in lbs to associate with this photo"}
        }, "required": ["photo_id"]},
        "fn": update_photo_tool,
    },
    "delete_photo": {
        "description": "Soft-delete a progress photo.",
        "schema": {"type": "object", "properties": {
            "photo_id": {"type": "integer"},
            "reason": {"type": "string"}
        }, "required": ["photo_id"]},
        "fn": delete_photo_tool,
    },
    "backfill_planned_program": {
        "description": "Write plans for PAST dates with backfilled=true marker. Unlike set_planned_program, this allows past dates for honest reconstruction. Backfilled plans are clearly distinguished from prescriptive plans.",
        "schema": {"type": "object", "properties": {
            "schedule": {"type": "array", "description": "Array of daily plans with date, program_name, exercises, warmup, carry_forward, description, notes",
                "items": {"type": "object", "properties": {
                    "date": {"type": "string"}, "program_name": {"type": "string"},
                    "exercises": {"type": "array"}, "warmup": {"type": "array"},
                    "carry_forward": {"type": "array"}, "description": {"type": "string"},
                    "notes": {"type": "string"}
                }, "required": ["date", "program_name", "exercises"]}}
        }, "required": ["schedule"]},
        "fn": backfill_plan_tool,
    },
    "skip_exercise": {
        "description": "Skip an exercise, set, or block in the current session. Records the skip with reason and logs a deviation. reason: skipped_by_coach, skipped_by_user, not_attempted.",
        "schema": {"type": "object", "properties": {
            "exercise": {"type": "string", "description": "Exercise name to skip"},
            "session_id": {"type": "string", "description": "Session ID (defaults to current)"},
            "reason": {"type": "string", "description": "skipped_by_coach, skipped_by_user, not_attempted"},
            "detail": {"type": "string", "description": "Why it was skipped"}
        }, "required": ["exercise"]},
        "fn": skip_exercise_tool,
    },
    "submit_dispatch_report": {
        "description": "Submit a report to dispatch (the orchestration layer). The coach should show the report to the athlete and get approval BEFORE calling this. Categories: session_report, bug_report, feature_request, data_issue, coaching_note.",
        "schema": {"type": "object", "properties": {
            "report": {"type": "string", "description": "The full report text to deliver"},
            "category": {"type": "string", "description": "session_report, bug_report, feature_request, data_issue, coaching_note"},
            "priority": {"type": "string", "description": "normal, urgent"}
        }, "required": ["report"]},
        "fn": submit_dispatch_tool,
    },
        "get_chat_context": {
        "description": "Get recent conversation messages for context continuity.",
        "fn": get_chat_context_tool,
        "schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max messages (default 50)"},
                "days": {"type": "integer", "description": "Days back (default 7)"}
            },
            "required": []
        }
    }
}

from fitness_mcp import FITNESS_TOOLS  # noqa: E402
TOOLS.update(FITNESS_TOOLS)

# ---------------------------------------------------------------------------
# MCP stdio protocol handling (for standalone mode)
# ---------------------------------------------------------------------------

SERVER_INFO = {
    "name": "jarvis-workout",
    "version": "1.0.0"
}

CAPABILITIES = {
    "tools": {}
}


def read_message():
    """Read a JSON-RPC message from stdin using Content-Length framing."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None  # EOF
        line = line.decode("utf-8")
        if line in ("\r\n", "\n"):
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()

    content_length = int(headers.get("Content-Length", 0))
    if content_length == 0:
        return None

    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def send_message(msg: dict):
    """Send a JSON-RPC message to stdout using Content-Length framing."""
    body = json.dumps(msg, ensure_ascii=False)
    encoded = body.encode("utf-8")
    header = f"Content-Length: {len(encoded)}\r\n\r\n"
    sys.stdout.buffer.write(header.encode("utf-8"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def handle_initialize(msg: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES
        }
    }


def handle_tools_list(msg: dict) -> dict:
    tool_list = []
    for name, info in TOOLS.items():
        tool_list.append({
            "name": name,
            "description": info["description"],
            "inputSchema": info.get("schema", info.get("inputSchema", {}))
        })
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "result": {"tools": tool_list}
    }


def handle_tools_call(msg: dict) -> dict:
    params = msg.get("params", {})
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    if tool_name not in TOOLS:
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"error": f"Unknown tool: {tool_name}"})
                }],
                "isError": True
            }
        }

    try:
        result = TOOLS[tool_name]["fn"](arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, indent=2, ensure_ascii=False)
                }],
                "isError": False
            }
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"error": str(e)})
                }],
                "isError": True
            }
        }


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}

# ---------------------------------------------------------------------------
# Main loop (stdio mode)
# ---------------------------------------------------------------------------

def main():
    # Ensure default JSON config files exist
    _load("profile.json", DEFAULT_PROFILE)
    _load("program.json", DEFAULT_PROGRAM)

    sys.stderr.write(f"JARVIS workout MCP server started. Data dir: {DATA_DIR}\n")
    sys.stderr.flush()

    while True:
        try:
            msg = read_message()
            if msg is None:
                break

            method = msg.get("method", "")

            if "id" not in msg:
                continue

            handler = HANDLERS.get(method)
            if handler:
                response = handler(msg)
                send_message(response)
            else:
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                })

        except json.JSONDecodeError as e:
            sys.stderr.write(f"JSON decode error: {e}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Unexpected error: {e}\n")
            sys.stderr.flush()
            try:
                if msg and "id" in msg:
                    send_message({
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {
                            "code": -32603,
                            "message": str(e)
                        }
                    })
            except Exception:
                pass

    sys.stderr.write("JARVIS workout MCP server stopped.\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
