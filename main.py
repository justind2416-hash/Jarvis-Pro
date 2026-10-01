from pathlib import Path
import os
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except (ImportError, OSError):
    pass
load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse, Response, JSONResponse
from openai import OpenAI

from brain import chat, chat_stream, JARVIS_MODEL
from database import (
    init_db,
    log_workout_set,
    log_bodyweight_entry,
    get_bodyweight_history,
    get_recent_sessions,
    get_carry_forward_items,
    add_carry_forward,
    resolve_carry_forward,
    get_full_history,
    create_workout_session,
    end_workout_session,
    get_current_session_data,
    save_progress_photo,
    get_planned_workout,
    mark_planned_workout_done,
    mark_planned_workout_replaced,
    get_progress_photos,
    get_progress_photo_data,
    delete_progress_photo,
    get_exercise_schema,
    get_all_exercise_schemas,
    EXERCISE_TYPES,
    start_demo_session,
    end_demo_session,
    _local_today,
)
# Also import the MCP tool definitions from mcp_server for reuse
# MCP endpoint secret — set MCP_SECRET env var on Railway
MCP_SECRET = os.environ.get("MCP_SECRET", "")
FEEDBACK_WEBHOOK = os.environ.get("FEEDBACK_WEBHOOK_URL", "")

from mcp_server import TOOLS as MCP_TOOLS, _load, _save, DEFAULT_PROFILE, DEFAULT_PROGRAM

# OpenAI client for TTS
# OpenAI client initialized lazily to ensure env vars are loaded
_openai_client = None

def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        key = os.getenv("OPENAI_API_KEY") or os.getenv("openai_API_Key") or os.getenv("openai_api_key")
        if key:
            _openai_client = OpenAI(api_key=key)
    return _openai_client

APP_VERSION = "5.0.5"

app = FastAPI(title="JARVIS Workout Assistant")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Serve static files (for bust_lines.js from converter, etc.)
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Ensure database is initialized on startup
init_db()


def _is_onboarded() -> bool:
    """Check if the athlete has completed onboarding."""
    try:
        profile = _load("profile.json", DEFAULT_PROFILE)
        name = profile.get("name", "")
        return bool(name) and name not in ("", "New Athlete")
    except Exception:
        return False


@app.get("/")
async def index(request: Request):
    if not _is_onboarded():
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/onboard", status_code=302)
    resp = templates.TemplateResponse(
        name="index.html", request=request, context={"version": APP_VERSION}
    )
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/onboard")
async def onboard_page(request: Request):
    if _is_onboarded():
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("onboard.html", {"request": request})


@app.post("/api/onboard")
async def api_onboard(request: Request):
    try:
        data = await request.json()
        from datetime import datetime
        profile = {
            "name": data.get("name", "Athlete"),
            "age": data.get("age", 0),
            "dob": data.get("dob", ""),
            "height": data.get("height", ""),
            "weight_lbs": data.get("weight_lbs", 0),
            "target_weight_lbs": data.get("target_weight_lbs", 0),
            "goals": data.get("goals", {"primary": "", "selected": []}),
            "experience": data.get("experience", ""),
            "equipment": data.get("equipment", []),
            "training_frequency": data.get("training_frequency", 4),
            "training_split": data.get("training_split", ""),
            "limitations": data.get("limitations", ""),
            "onboarded_at": datetime.now().isoformat(),
        }
        _save("profile.json", profile)
        program = {
            "name": "Awaiting Coach Setup",
            "phase": "Onboarding",
            "exercises": [],
            "notes": "Connect Claude to design your first program.",
        }
        _save("program.json", program)

        # Seed goals based on selections
        _seed_onboarding_goals(profile)

        return JSONResponse({"status": "ok", "name": profile["name"]})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


def _seed_onboarding_goals(profile: dict):
    """Create starter goals based on onboarding selections."""
    try:
        from fitness import set_goal
        selected = profile.get("goals", {}).get("selected", [])
        freq = profile.get("training_frequency", 4)

        # Always create a training frequency goal
        set_goal(
            goal_key="training_frequency",
            title=f"Train {freq}x/week",
            category="habit",
            metric="sessions_per_week",
            unit="sessions",
            direction="increase",
            start_value=0,
            target_value=freq,
            source="onboarding",
        )

        if "lose_weight" in selected and profile.get("target_weight_lbs"):
            set_goal(
                goal_key="bodyweight_cut",
                title=f"Reach {profile['target_weight_lbs']} lb",
                category="body_comp",
                metric="bodyweight_lbs",
                unit="lb",
                direction="decrease",
                start_value=profile.get("weight_lbs", 0),
                target_value=profile["target_weight_lbs"],
                source="onboarding",
            )

        if "build_muscle" in selected:
            set_goal(
                goal_key="build_muscle",
                title="Build muscle",
                category="aesthetic",
                direction="increase",
                source="onboarding",
            )
    except Exception as e:
        print(f"[onboarding] Goal seeding failed (non-fatal): {e}")


@app.get("/api/onboard/status")
async def onboard_status():
    profile = _load("profile.json", DEFAULT_PROFILE)
    return JSONResponse({
        "onboarded": _is_onboarded(),
        "name": profile.get("name", ""),
    })


@app.get("/coach")
async def coach_page(request: Request):
    base = str(request.base_url).rstrip("/")
    secret = os.environ.get("MCP_SECRET", "")
    mcp_url = f"{base}/mcp/{secret}" if secret else f"{base}/mcp"
    profile = _load("profile.json", DEFAULT_PROFILE)
    return templates.TemplateResponse("coach.html", {
        "request": request,
        "mcp_url": mcp_url,
        "athlete_name": profile.get("name", "Athlete"),
    })


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.json()
    message = body.get("message", "")
    history = body.get("history", [])
    reply = await chat(message, history)
    return {"reply": reply}


async def _stream_generator(message: str, history: list):
    async for chunk in chat_stream(message, history):
        data = json.dumps({"text": chunk})
        yield f"data: {data}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    body = await request.json()
    message = body.get("message", "")
    history = body.get("history", [])
    return StreamingResponse(
        _stream_generator(message, history),
        media_type="text/event-stream",
    )


@app.post("/api/tts")
async def api_tts(request: Request):
    """Convert text to speech using OpenAI TTS-HD API, streamed as MP3."""
    openai_client = _get_openai_client()
    if not openai_client:
        return Response(content="OPENAI_API_KEY not configured", status_code=500)
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return Response(content="No text provided", status_code=400)
    try:
        response = openai_client.audio.speech.create(
            model="tts-1-hd",
            voice="nova",
            input=text,
            response_format="mp3",
            speed=1.15,
        )

        def generate():
            for chunk in response.iter_bytes(1024):
                yield chunk

        return StreamingResponse(generate(), media_type="audio/mpeg")
    except Exception as exc:
        return Response(content=f"TTS error: {exc}", status_code=500)




# ═══════════════════════════════════════════════════════════════════════════════
# APP CONFIG — tunables without redeploying
# ═══════════════════════════════════════════════════════════════════════════════
APP_CONFIG = {
    "rest_timer_default": int(os.environ.get("REST_TIMER_DEFAULT", "90")),
    "rest_timer_compound": int(os.environ.get("REST_TIMER_COMPOUND", "120")),
    "rest_timer_isolation": int(os.environ.get("REST_TIMER_ISOLATION", "60")),
    "poll_interval_active": int(os.environ.get("POLL_INTERVAL_ACTIVE", "5")),
    "poll_interval_idle": int(os.environ.get("POLL_INTERVAL_IDLE", "20")),
    "min_operating_cash": int(os.environ.get("MIN_OPERATING_CASH", "100000")),
}

@app.get("/api/config")
async def api_config():
    """Return app configuration. Tunables set via env vars, no redeploy needed."""
    return JSONResponse(APP_CONFIG)

@app.get("/api/debug-date")
async def api_debug_date():
    from database import _local_today, _local_now
    from datetime import datetime, date
    return JSONResponse({
        "utc_now": datetime.utcnow().isoformat(),
        "local_now": _local_now().isoformat(),
        "local_today": _local_today(),
        "utc_today": date.today().isoformat(),
    })


@app.get("/api/status")
async def api_status():
    return JSONResponse({
        "status": "online",
        "model": JARVIS_MODEL if 'JARVIS_MODEL' in dir() else "claude-sonnet-5",
        "version": APP_VERSION,
        "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local")[:8],
        "deploy_time": os.environ.get("RAILWAY_DEPLOY_TIMESTAMP", "unknown"),
    })

@app.get("/api/status-old")
async def api_status_old():
    return {"status": "online", "model": JARVIS_MODEL}


# ═══════════════════════════════════════════════════════════════════════════════
# WORKOUT SESSION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/session/start")
async def api_session_start(request: Request):
    """Start a new workout session. Auto-marks plan as replaced if program differs."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    program = body.get("program", "Arsenal")
    result = create_workout_session(program, notes=body.get("notes", ""), source="app")
    
    # Check if this program differs from today's plan — if so, mark plan as replaced
    try:
        planned = get_planned_workout()
        if planned and planned.get("program_name") and program:
            if planned["program_name"].lower() != program.lower() and planned.get("status") == "pending":
                mark_planned_workout_replaced(replacement_program=program)
    except Exception:
        pass
    
    return JSONResponse(result)


@app.post("/api/session/end")
async def api_session_end(request: Request):
    """End the current workout session and return summary."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    session_id = body.get("session_id")
    # Demo sessions are never "completed" — delete them instead of marking the plan done.
    if not session_id:
        current = get_current_session_data()
        if current and current.get("active") and str(current.get("session_id", "")).startswith("demo_"):
            session_id = current["session_id"]
    if str(session_id or "").startswith("demo_"):
        return JSONResponse(end_demo_session(session_id))
    result = end_workout_session(session_id, notes=body.get("notes", ""), end_time=body.get("end_time"), source="app")
    # Mark today's planned workout as completed
    try:
        mark_planned_workout_done(_local_today(), result.get("session_id") or session_id)
    except Exception:
        pass
    return JSONResponse(result)


@app.post("/api/demo/start")
async def api_demo_start(request: Request):
    """Start a demo session: a real, 'demo_'-tagged DB session that is hard-deleted on end."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    result = start_demo_session(body.get("program") or "Demo")
    return JSONResponse(result, status_code=409 if result.get("error") else 200)


@app.post("/api/demo/end")
async def api_demo_end(request: Request):
    """End a demo session and delete it with all of its sets. Rejects non-demo ids."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    result = end_demo_session(body.get("session_id", ""))
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/set/log")
async def api_set_log(request: Request):
    """Log a structured workout set."""
    body = await request.json()
    exercise = body.get("exercise")
    weight = body.get("weight")
    reps = body.get("reps")
    if not exercise or weight is None or reps is None:
        return JSONResponse({"error": "exercise, weight, and reps are required"}, status_code=400)
    result = log_workout_set(
        exercise=exercise,
        weight=str(weight),
        reps=str(reps),
        rpe=str(body.get("rpe", "")),
        notes=body.get("notes", ""),
        session_id=body.get("session_id"),
        source=body.get("source") or "app",
        performed_at=body.get("performed_at"),
        supersedes_set_id=body.get("supersedes_set_id"),
    )
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.put("/api/set/{set_id}")
async def api_update_set(set_id: int, request: Request):
    """Correct a set in place; prior values go to set_history."""
    from database import update_set
    body = await request.json()
    result = update_set(
        set_id, reason=body.get("reason", ""), source=body.get("source") or "correction",
        weight=body.get("weight"), reps=body.get("reps"), rpe=body.get("rpe"),
        notes=body.get("notes"), exercise=body.get("exercise"), performed_at=body.get("performed_at"),
    )
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.delete("/api/set/{set_id}")
async def api_delete_set(set_id: int, reason: str = ""):
    """Soft-delete a set (restorable)."""
    from database import confirm_delete_set
    result = confirm_delete_set(set_id, reason=reason, source="app")
    return JSONResponse(result, status_code=404 if result.get("error") else 200)


@app.post("/api/set/{set_id}/restore")
async def api_restore_set(set_id: int):
    from database import restore_set
    result = restore_set(set_id, source="app")
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.get("/api/set/{set_id}/history")
async def api_set_history(set_id: int):
    from database import get_set_history
    result = get_set_history(set_id)
    return JSONResponse(result, status_code=404 if result.get("error") else 200)


@app.get("/api/session/current")
async def api_session_current():
    """Return current session data with all logged sets."""
    data = get_current_session_data()
    if data is None:
        return JSONResponse({"active": False, "session_id": None})
    import training
    data["coach_notes"] = training.get_notes(data["session_id"])
    data["added_exercises"] = training.get_session_exercises(data["session_id"])
    return JSONResponse(data)


@app.post("/api/carry-forward/resolve")
async def api_carry_forward_resolve(request: Request):
    """Resolve a carry-forward item by id."""
    body = await request.json()
    item_id = body.get("id")
    if item_id is None:
        return JSONResponse({"error": "id is required"}, status_code=400)
    resolve_carry_forward(int(item_id))
    return JSONResponse({"status": "resolved", "id": item_id})


@app.get("/api/carry-forward")
async def api_carry_forward_list():
    """Return all unresolved carry-forward items."""
    items = get_carry_forward_items()
    return JSONResponse({"items": items})



# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# BODYWEIGHT API
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/bodyweight")
async def api_log_bodyweight(request: Request):
    body = await request.json()
    weight = body.get("weight")
    dt = body.get("date")
    notes = body.get("notes", "")
    if weight is None:
        return JSONResponse({"error": "weight is required"}, status_code=400)
    result = log_bodyweight_entry(float(weight), dt, notes, fasted=body.get("fasted"),
                                  time_of_day=body.get("time_of_day"),
                                  off_protocol=body.get("off_protocol", False))
    return JSONResponse(result, status_code=400 if result.get("error") else 200)

@app.get("/api/bodyweight/history")
async def api_bodyweight_history(days: int = None):
    """Latest 30 entries, or every entry from the last `days` days (newest first)."""
    if days:
        from datetime import timedelta
        from database import _local_now
        cutoff = (_local_now().date() - timedelta(days=max(1, days) - 1)).isoformat()
        entries = [e for e in get_bodyweight_history(2000) if e["date"] >= cutoff]
    else:
        entries = get_bodyweight_history(30)
    return JSONResponse({"entries": entries})

# PROGRAM API — dynamic workout panel data
# ═══════════════════════════════════════════════════════════════════════════════


# Exercise demo data (images + video links)
EXERCISE_DEMOS = {
    "Goblet Squat to Box": {
        "cues": "Chest up, sit back to box, drive through heels. Feel: quads, glutes.",
        "video": "https://www.youtube.com/results?search_query=goblet+squat+to+box+form"
    },
    "Cable Pull-Through": {
        "cues": "Hinge at hips, arms straight, squeeze glutes at top. Feel: hamstrings, glutes.",
        "video": "https://www.youtube.com/results?search_query=cable+pull+through+form"
    },
    "Seated Low Row": {
        "cues": "Elbows to ribs, blades down first, no neck tension. Feel: lats, mid-back.",
        "video": "https://www.youtube.com/results?search_query=seated+cable+row+form"
    },
    "Push-Ups": {
        "cues": "Squeeze glutes first, elbows at 45 degrees, full range. Feel: chest, triceps.",
        "video": "https://www.youtube.com/results?search_query=push+up+proper+form"
    },
    "Pallof Press": {
        "cues": "Half-kneeling, press straight out, resist rotation. Feel: core, obliques.",
        "video": "https://www.youtube.com/results?search_query=pallof+press+half+kneeling+form"
    },
    "Side Plank": {
        "cues": "Stack feet or stagger, straight line head to feet, breathe. Feel: obliques.",
        "video": "https://www.youtube.com/results?search_query=side+plank+form"
    },
    "Hollow Body Hold": {
        "cues": "Lower back pressed to floor, arms overhead, legs straight. Feel: deep core.",
        "video": "https://www.youtube.com/results?search_query=hollow+body+hold+form"
    },
    "Step-Ups (20in box)": {
        "cues": "Full foot on box, drive up through heel, control down. Feel: quads, glutes.",
        "video": "https://www.youtube.com/results?search_query=dumbbell+step+ups+form"
    },
    "Half-Kneeling 1-Arm Press": {
        "cues": "Tall kneeling, core braced, press straight up. Feel: shoulders, core stability.",
        "video": "https://www.youtube.com/results?search_query=half+kneeling+single+arm+press"
    },
    "DB Hip Thrust": {
        "cues": "Shoulder blades on bench, drive hips up, squeeze at top. Feel: glutes.",
        "video": "https://www.youtube.com/results?search_query=dumbbell+hip+thrust+form"
    },
    "Neutral-Grip Pulldown": {
        "cues": "Anchor with legs, pull to chest, squeeze lats. Feel: lats, biceps.",
        "video": "https://www.youtube.com/results?search_query=neutral+grip+lat+pulldown+form"
    },
    "Chin-Up Test": {
        "cues": "Full hang, chin over bar, control the negative. Feel: lats, biceps.",
        "video": "https://www.youtube.com/results?search_query=chin+up+proper+form"
    },
    "Face Pulls": {
        "cues": "High pull, external rotate at top, squeeze rear delts. Feel: rear delts, upper back.",
        "video": "https://www.youtube.com/results?search_query=face+pulls+proper+form"
    },
    "Bird Dog": {
        "cues": "Opposite arm and leg, keep hips square, slow and controlled. Feel: core, stability.",
        "video": "https://www.youtube.com/results?search_query=bird+dog+exercise+form"
    },
    "Suitcase Carry": {
        "cues": "One side loaded, stay tall, dont lean. Feel: obliques, grip.",
        "video": "https://www.youtube.com/results?search_query=suitcase+carry+exercise+form"
    },
    "Dead Hang": {
        "cues": "Full grip, shoulders packed, breathe and relax. Feel: grip, shoulder stretch.",
        "video": "https://www.youtube.com/results?search_query=dead+hang+proper+form"
    },
    "Light Cardio": {
        "cues": "Easy pace, get the blood flowing, 5 minutes.",
        "video": ""
    },
    "Dynamic Stretches": {
        "cues": "Arm circles, leg swings, hip openers. Full range of motion.",
        "video": ""
    },
    "Treadmill Walk/Jog": {
        "cues": "3.0-3.5 mph, 2-3% incline, 5 minutes. Build from walk to light jog. Get blood flowing.",
        "video": "https://www.youtube.com/results?search_query=treadmill+warm+up+before+lifting"
    },
    "Arm Circles": {
        "cues": "Start small, gradually widen. 20 forward, 20 backward.",
        "video": "https://www.youtube.com/results?search_query=arm+circles+warm+up"
    },
    "Leg Swings": {
        "cues": "Hold wall for balance. Forward and back, then side to side.",
        "video": "https://www.youtube.com/results?search_query=leg+swings+warm+up+exercise"
    },
    "Hip Openers": {
        "cues": "Wide stance, shift side to side. Open groin and hip flexors.",
        "video": "https://www.youtube.com/results?search_query=hip+opener+warm+up+exercise"
    },
    "Band Pull-Aparts": {
        "cues": "Arms straight, pull band to chest. Squeeze shoulder blades.",
        "video": "https://www.youtube.com/results?search_query=band+pull+aparts+warm+up"
    },
    "Warm-Up Set 1": {
        "cues": "50% working weight. Full range, focus on form.",
        "video": ""
    },
    "Warm-Up Set 2": {
        "cues": "70% working weight. Last prep before working sets.",
        "video": ""
    },
    "Bulgarian Split Squats": {
        "cues": "Rear foot on bench, front knee tracks over toes, torso upright. Feel: quads, glutes, hip flexor stretch.",
        "video": "https://www.youtube.com/results?search_query=bulgarian+split+squat+dumbbell+form"
    },
    "Warm-Up Sets": {
        "cues": "50% of working weight, focus on form and range of motion.",
        "video": ""
    },
}


PROGRAMS = {
    "Ironforge": {
        "name": "Ironforge",
        "description": "Lower power, hinge, core strength",
        "warmup": [
            {"name": "Treadmill Walk/Jog", "target": "5 min, 3.0 mph, 2% incline", "sets": 1, "input_type": "duration"},
            {"name": "Arm Circles", "target": "20 each direction", "sets": 1, "input_type": "reps_only"},
            {"name": "Leg Swings", "target": "15 each leg", "sets": 1, "input_type": "reps_only"},
            {"name": "Hip Openers", "target": "10 each side", "sets": 1, "input_type": "reps_only"},
            {"name": "Band Pull-Aparts", "target": "15 reps", "sets": 1, "input_type": "reps_only"},
            {"name": "Warm-Up Set 1", "target": "50% weight x 8 reps", "sets": 1},
            {"name": "Warm-Up Set 2", "target": "70% weight x 5 reps", "sets": 1},
        ],
        "exercises": [
            {"name": "Goblet Squat to Box", "weight": "60 lb", "reps": "8-10", "sets": 3},
            {"name": "Cable Pull-Through", "weight": "200 lb", "reps": "10", "sets": 3},
            {"name": "Seated Low Row", "weight": "200 lb", "reps": "10", "sets": 3},
            {"name": "Push-Ups", "weight": "bodyweight", "reps": "max", "sets": 3},
            {"name": "Pallof Press", "weight": "80 lb", "reps": "10/side", "sets": 3},
            {"name": "Side Plank", "weight": "bodyweight", "reps": "20s hold", "sets": 3, "input_type": "duration"},
            {"name": "Hollow Body Hold", "weight": "bodyweight", "reps": "20s hold", "sets": 3, "input_type": "duration"},
        ],
    },
    "Arsenal": {
        "name": "Arsenal",
        "description": "Upper pull/push, arms, command",
        "warmup": [
            {"name": "Treadmill Walk/Jog", "target": "5 min, 3.0 mph, 2% incline", "sets": 1, "input_type": "duration"},
            {"name": "Arm Circles", "target": "20 each direction", "sets": 1, "input_type": "reps_only"},
            {"name": "Leg Swings", "target": "15 each leg", "sets": 1, "input_type": "reps_only"},
            {"name": "Hip Openers", "target": "10 each side", "sets": 1, "input_type": "reps_only"},
            {"name": "Band Pull-Aparts", "target": "15 reps", "sets": 1, "input_type": "reps_only"},
            {"name": "Warm-Up Set 1", "target": "50% weight x 8 reps", "sets": 1},
            {"name": "Warm-Up Set 2", "target": "70% weight x 5 reps", "sets": 1},
        ],
        "exercises": [
            {"name": "Step-Ups (20in box)", "weight": "20 lb each", "reps": "10/side", "sets": 3},
            {"name": "Half-Kneeling 1-Arm Press", "weight": "25 lb", "reps": "8-10", "sets": 3},
            {"name": "DB Hip Thrust", "weight": "50 lb", "reps": "10", "sets": 3},
            {"name": "Neutral-Grip Pulldown", "weight": "238 lb", "reps": "10", "sets": 3},
        ],
        "carry_forward": [
            {"name": "Chin-Up Test", "target": "assess max reps", "sets": 1},
            {"name": "Face Pulls", "target": "establish working weight", "sets": 3},
            {"name": "Bird Dog", "target": "form check", "sets": 3},
            {"name": "Suitcase Carry", "target": "establish working weight", "sets": 1},
            {"name": "Dead Hang", "target": "baseline time", "sets": 1},
        ],
    },
}


@app.get("/api/program/{program_name}")
async def get_program_data(program_name: str):
    program = PROGRAMS.get(program_name, PROGRAMS["Arsenal"])
    # Merge demo data into exercises
    result = dict(program)
    for section in ['warmup', 'exercises', 'carry_forward']:
        if section in result:
            for ex in result[section]:
                demo = EXERCISE_DEMOS.get(ex['name'], {})
                ex['cues'] = demo.get('cues', '')
                ex['video'] = demo.get('video', '')
                # Add schema type for dynamic input forms
                schema = get_exercise_schema(ex.get('name', ''))
                ex['schema_type'] = schema.get('type', 'strength_standard')
    return result


@app.get("/api/programs")
async def list_programs():
    return {"programs": list(PROGRAMS.keys())}


# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# VOICE WEBSOCKET — Streaming STT via Deepgram
# ═══════════════════════════════════════════════════════════════════════════════
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import base64

DEEPGRAM_KEY = os.getenv('DEEPGRAM_API_KEY') or os.getenv('deepgram_api_key', '')

@app.websocket('/ws/voice')
async def voice_websocket(websocket: WebSocket):
    await websocket.accept()
    
    if not DEEPGRAM_KEY:
        await websocket.send_json({'type': 'error', 'message': 'DEEPGRAM_API_KEY not configured'})
        await websocket.close()
        return
    
    try:
        from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
        
        dg = DeepgramClient(DEEPGRAM_KEY)
        dg_connection = dg.listen.live.v('1')
        
        transcript_buffer = []
        
        def on_transcript(self, result, **kwargs):
            try:
                alt = result.channel.alternatives[0]
                if alt.transcript.strip() and alt.confidence > 0.5:
                    is_final = result.is_final
                    transcript_buffer.append({
                        'text': alt.transcript,
                        'is_final': is_final,
                        'confidence': alt.confidence,
                    })
                    # Send interim results to browser
                    asyncio.get_event_loop().call_soon_threadsafe(
                        asyncio.ensure_future,
                        websocket.send_json({
                            'type': 'transcript',
                            'text': alt.transcript,
                            'is_final': is_final,
                        })
                    )
            except Exception as e:
                print(f'[voice] Transcript error: {e}')
        
        def on_utterance_end(self, result, **kwargs):
            # Utterance complete — combine all final transcripts and send to Claude
            final_text = ' '.join(t['text'] for t in transcript_buffer if t.get('is_final'))
            if final_text.strip():
                asyncio.get_event_loop().call_soon_threadsafe(
                    asyncio.ensure_future,
                    _process_voice_input(websocket, final_text.strip())
                )
            transcript_buffer.clear()
        
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
        dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        
        options = LiveOptions(
            model='nova-3',
            language='en-US',
            smart_format=True,
            interim_results=True,
            utterance_end_ms=2000,
            vad_events=True,
            filler_words=False,
            punctuate=True,
            endpointing=500,
        )
        
        if not dg_connection.start(options):
            await websocket.send_json({'type': 'error', 'message': 'Deepgram connection failed'})
            await websocket.close()
            return
        
        await websocket.send_json({'type': 'ready', 'message': 'Voice stream connected'})
        
        # Receive audio from browser and forward to Deepgram
        while True:
            data = await websocket.receive()
            if 'bytes' in data:
                dg_connection.send(data['bytes'])
            elif 'text' in data:
                msg = json.loads(data['text'])
                if msg.get('type') == 'stop':
                    break
        
        dg_connection.finish()
        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f'[voice] WebSocket error: {e}')
        try:
            await websocket.send_json({'type': 'error', 'message': str(e)})
        except:
            pass


async def _process_voice_input(websocket, text):
    """Process transcribed voice input through Claude and respond."""
    try:
        # Send to Claude
        reply = await chat(text, [])
        
        # Send text reply to browser
        await websocket.send_json({
            'type': 'response',
            'user_text': text,
            'jarvis_text': reply,
        })
        
        # Generate TTS audio
        openai_client = _get_openai_client()
        if openai_client:
            try:
                tts_response = openai_client.audio.speech.create(
                    model='tts-1-hd',
                    voice='nova',
                    input=reply.replace('<!--SET:', '').replace('-->', ''),
                    response_format='mp3',
                    speed=1.15,
                )
                audio_bytes = tts_response.content
                audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
                await websocket.send_json({
                    'type': 'audio',
                    'data': audio_b64,
                    'format': 'mp3',
                })
            except Exception as e:
                print(f'[voice] TTS error: {e}')
    except Exception as e:
        print(f'[voice] Process error: {e}')
        await websocket.send_json({'type': 'error', 'message': str(e)})



# ═══════════════════════════════════════════════════════════════════════════════
# TODAY'S PLANNED WORKOUT
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/today-workout")
async def api_today_workout(date: str = None):
    """
    Get today's planned workout. Falls back to hardcoded program if no plan exists.
    This is the primary endpoint the frontend should use.
    """
    try:
        planned = get_planned_workout(date)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e), "source": "fallback", "name": "Error", "exercises": [], "warmup": [], "carry_forward": []}, status_code=200)
    
    if planned:
        # Merge demo data (cues/videos) and schema types into the planned exercises
        for section in ['warmup', 'exercises', 'carry_forward']:
            for ex in (planned.get(section) or []):
                try:
                    demo = EXERCISE_DEMOS.get(ex.get('name', ''), {})
                    if not ex.get('cues'):
                        ex['cues'] = demo.get('cues', '')
                    if not ex.get('video'):
                        ex['video'] = demo.get('video', '')
                    if not ex.get('schema_type'):
                        schema = get_exercise_schema(ex.get('name', ''))
                        ex['schema_type'] = schema.get('type', 'strength_standard')
                except Exception:
                    pass
        
        return JSONResponse({
            "source": "planned",
            "name": planned["program_name"],
            "description": planned.get("description", ""),
            "warmup": planned.get("warmup", []),
            "exercises": planned.get("exercises", []),
            "carry_forward": planned.get("carry_forward", []),
            "date": planned["date"],
            "status": planned["status"],
            "notes": planned.get("notes", ""),
        })
    else:
        # Fall back to hardcoded rotation
        # Check last session to determine which program to load
        from database import get_recent_sessions
        recent = get_recent_sessions(limit=1)
        last_program = recent[0]["program"] if recent else ""
        
        # Alternate between Arsenal and Ironforge
        if "Arsenal" in last_program or "Strength B" in last_program:
            next_program = "Ironforge"
        else:
            next_program = "Arsenal"
        
        program = PROGRAMS.get(next_program, PROGRAMS["Arsenal"])
        result = dict(program)
        for section in ['warmup', 'exercises', 'carry_forward']:
            if section in result:
                for ex in result[section]:
                    demo = EXERCISE_DEMOS.get(ex['name'], {})
                    ex['cues'] = demo.get('cues', '')
                    ex['video'] = demo.get('video', '')
        
        result["source"] = "fallback"
        return JSONResponse(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 30-DAY ACTIVITY STATS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/activity-stats")
async def api_activity_stats(days: int = 30):
    """Get daily activity data for the past N days, including rest days."""
    from datetime import date, timedelta, datetime as dt

    sessions = get_recent_sessions(limit=100)
    bodyweight_entries = get_bodyweight_history(limit=60, include_off_protocol=False)

    # Build a map of date -> session data
    session_map = {}
    for s in sessions:
        d = s.get("date", "")
        if d and d not in session_map:
            # Determine muscle groups from exercises
            muscles = set()
            for ex_name in s.get("exercises", []):
                name_lower = ex_name.lower()
                if any(w in name_lower for w in ["pulldown", "pull-up", "chin-up", "row", "pull"]):
                    muscles.add("Back")
                if any(w in name_lower for w in ["press", "bench", "push"]):
                    muscles.add("Chest")
                if any(w in name_lower for w in ["shoulder", "delt", "lateral", "face pull"]):
                    muscles.add("Shoulders")
                if any(w in name_lower for w in ["curl", "tricep", "arm", "bicep"]):
                    muscles.add("Arms")
                if any(w in name_lower for w in ["squat", "leg", "step-up", "lunge"]):
                    muscles.add("Legs")
                if any(w in name_lower for w in ["hip thrust", "glute", "deadlift", "hinge", "rdl"]):
                    muscles.add("Glutes")
                if any(w in name_lower for w in ["core", "plank", "bird dog", "ab"]):
                    muscles.add("Core")
                if any(w in name_lower for w in ["carry", "hang", "grip"]):
                    muscles.add("Grip")
            if not muscles and s.get("exercises"):
                muscles.add("Full Body")

            # Calculate total volume
            total_volume = 0
            for st in s.get("sets", []):
                try:
                    w = float(str(st.get("weight", "0")).replace("lb", "").replace("each", "").strip().split()[0])
                    r = int(str(st.get("reps", "0")).split("/")[0].strip())
                    total_volume += w * r
                except (ValueError, IndexError):
                    pass

            session_map[d] = {
                "program": s.get("program", ""),
                "total_sets": s.get("total_sets", 0),
                "duration_min": s.get("duration_min"),
                "exercises": s.get("exercises", []),
                "muscles": sorted(muscles),
                "total_volume": round(total_volume),
            }

    # Build bodyweight map
    bw_map = {}
    for bw in bodyweight_entries:
        bw_map[bw["date"]] = bw["weight_lbs"]

    # Generate daily data for past N days
    today = date.today()
    daily = []
    for i in range(days):
        d = today - timedelta(days=i)
        d_str = d.isoformat()
        sess = session_map.get(d_str)
        daily.append({
            "date": d_str,
            "day_name": d.strftime("%a"),
            "workout": sess,
            "bodyweight": bw_map.get(d_str),
        })

    # Weekly summaries
    weeks = []
    for week_start in range(0, days, 7):
        week_days = daily[week_start:week_start + 7]
        if not week_days:
            continue
        workout_days = [d for d in week_days if d["workout"]]
        all_muscles = set()
        total_sets = 0
        total_volume = 0
        for wd in workout_days:
            all_muscles.update(wd["workout"].get("muscles", []))
            total_sets += wd["workout"].get("total_sets", 0)
            total_volume += wd["workout"].get("total_volume", 0)

        # Get bodyweight for the week (first available)
        week_bw = None
        for d in reversed(week_days):
            if d.get("bodyweight"):
                week_bw = d["bodyweight"]
                break

        weeks.append({
            "start_date": week_days[-1]["date"],
            "end_date": week_days[0]["date"],
            "workouts": len(workout_days),
            "total_sets": total_sets,
            "total_volume": round(total_volume),
            "muscles": sorted(all_muscles),
            "bodyweight": week_bw,
        })

    return JSONResponse({
        "days": daily,
        "weeks": weeks,
        "total_workouts": len([d for d in daily if d["workout"]]),
        "total_rest_days": len([d for d in daily if not d["workout"]]),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# PROGRESS PHOTOS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/progress-photo")
async def api_upload_photo(request: Request):
    """Upload a progress photo. Accepts base64 JPEG data."""
    body = await request.json()
    data_url = body.get("photo", "")
    angle = body.get("angle", "front")
    bodyweight = body.get("bodyweight")
    notes = body.get("notes", "")

    if not data_url:
        return JSONResponse({"error": "No photo data"}, status_code=400)

    # Parse data URL: "data:image/jpeg;base64,/9j/4AAQ..."
    if "base64," in data_url:
        header, b64data = data_url.split("base64,", 1)
        mime_type = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
    else:
        b64data = data_url
        mime_type = "image/jpeg"

    try:
        photo_bytes = base64.b64decode(b64data)
    except Exception:
        return JSONResponse({"error": "photo is not valid base64"}, status_code=400)
    if not photo_bytes:
        return JSONResponse({"error": "No photo data"}, status_code=400)
    photo_id = save_progress_photo(photo_bytes, angle=angle, bodyweight=bodyweight, notes=notes,
                                   mime_type=mime_type, photo_date=body.get("date"))

    return JSONResponse({
        "ok": True,
        "photo_id": photo_id,
        "size_kb": round(len(photo_bytes) / 1024, 1)
    })


@app.get("/api/progress-photos")
async def api_list_photos(limit: int = 20, date_from: str = None, date_to: str = None):
    """List progress photos (metadata only, no binary data)."""
    photos = get_progress_photos(limit=limit, date_from=date_from, date_to=date_to)
    return JSONResponse({"photos": photos})


@app.get("/api/progress-photo/{photo_id}")
async def api_get_photo(photo_id: int):
    """Get a progress photo as a JPEG image."""
    photo = get_progress_photo_data(photo_id)
    if not photo:
        return JSONResponse({"error": "Photo not found"}, status_code=404)
    return Response(
        content=bytes(photo["photo_data"]),
        media_type=photo.get("mime_type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"}
    )


@app.delete("/api/progress-photo/{photo_id}")
async def api_delete_photo(photo_id: int):
    """Delete a progress photo."""
    deleted = delete_progress_photo(photo_id)
    return JSONResponse({"ok": deleted})


# ═══════════════════════════════════════════════════════════════════════════════
# EXERCISE SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/exercise-schema/{exercise_name}")
async def api_exercise_schema(exercise_name: str):
    """Get the input schema for a specific exercise."""
    from urllib.parse import unquote
    schema = get_exercise_schema(unquote(exercise_name))
    return JSONResponse(schema)


@app.get("/api/exercise-schemas")
async def api_all_schemas():
    """Get all exercise type definitions and mappings."""
    return JSONResponse(get_all_exercise_schemas())


# ═══════════════════════════════════════════════════════════════════════════════
# SESSION DETAIL + MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/session-detail/{session_date}")
async def api_session_detail(session_date: str):
    """Get full session detail for a specific date including every set."""
    sessions = get_recent_sessions(limit=100)
    day_sessions = [s for s in sessions if s.get("date") == session_date]
    if not day_sessions:
        return JSONResponse({"error": "No sessions found for this date"}, status_code=404)
    return JSONResponse({"date": session_date, "sessions": day_sessions})


@app.put("/api/session/{session_id}")
async def api_update_session(session_id: str, request: Request):
    """Update session timestamps/notes/program. Prior values go to session_history."""
    body = await request.json()
    from database import update_session
    result = update_session(
        session_id, reason=body.get("reason", ""), source="app",
        notes_mode=body.get("notes_mode", "replace"),
        started_at=body.get("started_at") or None, ended_at=body.get("ended_at") or None,
        notes=body.get("notes"), program=body.get("program"),
    )
    if result.get("error"):
        return JSONResponse(result, status_code=400)
    return JSONResponse({'ok': True, 'session_id': session_id, **result})


@app.delete("/api/session/{session_id}")
async def api_delete_session(session_id: str, reason: str = ""):
    """Soft-delete a session and its sets (restorable via /api/session/{id}/restore)."""
    from database import confirm_delete_session
    try:
        result = confirm_delete_session(session_id, reason=reason, source="app")
        return JSONResponse(result, status_code=404 if result.get("error") else 200)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/session/{session_id}/restore")
async def api_restore_session(session_id: str):
    from database import restore_session
    result = restore_session(session_id, source="app")
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@app.post("/api/plan-supersede/{plan_id}")
async def api_plan_supersede(plan_id: int):
    """Force-supersede a specific plan row by ID."""
    from database import _raw_conn, _now_ts
    conn = _raw_conn()
    try:
        stamp = _now_ts()
        conn.execute("UPDATE planned_workouts SET superseded_at = ?, status = 'replaced', updated_at = ? WHERE id = ?", (stamp, stamp, plan_id))
        conn.close()
        return JSONResponse({"ok": True, "superseded": plan_id})
    except Exception as e:
        conn.close()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/plan-status")
async def api_plan_status(request: Request):
    """Update a planned workout status (replaced, skipped, completed)."""
    body = await request.json()
    date = body.get("date")
    status = body.get("status", "replaced")
    notes = body.get("notes", "")
    if not date:
        return JSONResponse({"error": "date required"}, status_code=400)
    from database import _raw_conn, _local_now
    conn = _raw_conn()
    cursor = conn.execute(
        "UPDATE planned_workouts SET status = ?, notes = ?, updated_at = ? WHERE planned_date = ?",
        (status, notes, _local_now().isoformat(), date)
    )
    conn.close()
    return JSONResponse({"ok": True, "date": date, "status": status})


@app.post("/api/swap-workout")
async def api_swap_workout(request: Request):
    """Swap a different day's workout into today's slot."""
    body = await request.json()
    source_date = body.get("source_date")
    if not source_date:
        return JSONResponse({"error": "source_date required"}, status_code=400)
    
    from database import get_planned_workout, save_planned_schedule, _local_today
    
    # Get the source workout
    source = get_planned_workout(source_date)
    if not source:
        return JSONResponse({"error": "No workout found for " + source_date}, status_code=404)
    
    today = body.get("target_date") or _local_today()
    
    # Save the source workout as today's plan (past target dates are rejected)
    saved = save_planned_schedule([{
        "date": today,
        "program_name": source["program_name"],
        "exercises": source.get("exercises", []),
        "warmup": source.get("warmup", []),
        "carry_forward": source.get("carry_forward", []),
        "description": source.get("description", "") + " (swapped from " + source_date + ")",
        "notes": "Swapped from " + source_date,
    }])
    if saved.get("error"):
        return JSONResponse(saved, status_code=400)
    
    return JSONResponse({"ok": True, "swapped_from": source_date, "today": today, "program": source["program_name"]})


@app.get("/api/schedule-preview")
async def api_schedule_preview(days: int = 14):
    """Get the planned workout schedule for the next N days for the schedule browser."""
    from database import get_planned_schedule as gps
    schedule = gps(days)
    return JSONResponse({"schedule": schedule, "days": days})


# ═══════════════════════════════════════════════════════════════════════════════
# NIGHTLY SYNC ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/nightly-sync")
async def api_nightly_sync():
    """
    Trigger the nightly sync manually or via scheduled task.
    Checks today's activity, marks skipped workouts, returns a report
    for the Claude Desktop project to evaluate and adjust the schedule.
    """
    from datetime import date, timedelta
    from database import (
        get_planned_workout as _gpw,
        get_workout_compliance as _gwc,
        mark_planned_workout_skipped as _mps,
        get_planned_schedule as _gps,
        get_session_deviations as _gsd,
    )

    today = _local_today()
    sessions = get_recent_sessions(limit=10)
    today_sessions = [s for s in sessions if s.get("date") == today]
    
    planned = _gpw(today)
    compliance = _gwc(14)
    bw_entries = get_bodyweight_history(7)
    upcoming = _gps(14)
    
    # Mark skipped if planned but not done
    skipped = False
    if planned and planned["status"] == "pending" and not today_sessions:
        _mps(today, "No session logged — nightly sync")
        skipped = True
    
    report = {
        "date": today,
        "worked_out": len(today_sessions) > 0,
        "sessions": [{
            "program": s.get("program"),
            "total_sets": s.get("total_sets"),
            "duration_min": s.get("duration_min"),
            "exercises": s.get("exercises", []),
        } for s in today_sessions],
        "planned": {
            "program": planned["program_name"] if planned else None,
            "status": "skipped" if skipped else (planned["status"] if planned else "no_plan"),
        },
        "compliance_14d": compliance,
        "bodyweight": bw_entries[:3] if bw_entries else [],
        "days_planned_ahead": len(upcoming),
        "needs_replan": len(upcoming) < 7,
        "deviations": _gsd(date=today),
    }
    
    return JSONResponse(report)


@app.post("/api/exercise/modify")
async def api_modify_exercise(request: Request):
    body = await request.json()
    from database import log_exercise_modification
    result = log_exercise_modification(
        session_id=body.get('session_id'),
        original_exercise=body.get('original_exercise', ''),
        action=body.get('action', 'skip'),
        replacement_exercise=body.get('replacement_exercise'),
        replacement_weight=body.get('replacement_weight'),
        replacement_reps=body.get('replacement_reps'),
        replacement_sets=body.get('replacement_sets'),
        reason=body.get('reason', ''),
    )
    return JSONResponse(result)

@app.get("/api/session/modifications")
async def api_session_modifications():
    from database import get_session_modifications
    mods = get_session_modifications()
    return JSONResponse({"modifications": mods})

# ═══════════════════════════════════════════════════════════════════════════════
# EXERCISE LIBRARY · BODY MEASUREMENTS · GOALS (v5.0.1)
# ═══════════════════════════════════════════════════════════════════════════════
import fitness as fx


def _res(result: dict, error_status: int = 400):
    return JSONResponse(result, status_code=error_status if isinstance(result, dict) and result.get("error") else 200)


@app.get("/api/exercises")
async def api_exercises(q: str = "", muscle_group: str = None, tag: str = None, category: str = None,
                        equipment: str = None, rehab_target: str = None, muscle: str = None,
                        difficulty: str = None, limit: int = 300):
    rows = fx.search_exercises(q, muscle_group=muscle_group, tag=tag, category=category, equipment=equipment,
                               rehab_target=rehab_target, muscle=muscle, difficulty=difficulty, limit=limit)
    return JSONResponse({"count": len(rows), "exercises": rows})


@app.get("/api/exercises/facets")
async def api_exercise_facets():
    return JSONResponse(fx.library_facets())


@app.get("/api/exercises/lookup")
async def api_exercise_lookup(name: str):
    """Library entry for a logged name, or the muscle guess used for analytics."""
    ex = fx.find_exercise(name)
    return JSONResponse({"exercise": ex, "muscles": fx.exercise_muscles(name)})


@app.get("/api/exercises/{ident}")
async def api_exercise_get(ident: str):
    from urllib.parse import unquote
    ex = fx.get_exercise(unquote(ident))
    if not ex:
        return JSONResponse({"error": f"No exercise '{ident}'"}, status_code=404)
    return JSONResponse({"exercise": ex})


@app.post("/api/exercises")
async def api_exercise_add(request: Request):
    body = await request.json()
    name = body.pop("name", "")
    return _res(fx.add_exercise(name, source="app", **{k: v for k, v in body.items() if k in fx._EDITABLE_EX}))


@app.put("/api/exercises/{ident}")
async def api_exercise_update(ident: str, request: Request):
    body = await request.json()
    return _res(fx.update_exercise(ident, source="app", **{k: v for k, v in body.items() if k in fx._EDITABLE_EX}))


@app.get("/api/measurements")
async def api_measurements(limit: int = 60):
    return JSONResponse({"entries": fx.get_measurements(limit)})


@app.post("/api/measurements")
async def api_measurements_log(request: Request):
    body = await request.json()
    dt, notes = body.pop("date", None), body.pop("notes", "")
    return _res(fx.log_measurements(dt, notes=notes, source="app", **body))


@app.put("/api/measurements/{mid}")
async def api_measurements_update(mid: int, request: Request):
    body = await request.json()
    return _res(fx.update_measurement(mid, source="app", **body))


@app.delete("/api/measurements/{mid}")
async def api_measurements_delete(mid: int, reason: str = ""):
    return _res(fx.delete_measurement(mid, reason=reason), 404)


@app.get("/api/stats/muscle-volume")
async def api_muscle_volume(weeks: int = 8):
    """Weekly hard sets + load per muscle group (rolling 7-day windows, newest last)."""
    return JSONResponse(fx.muscle_volume(weeks))


# ── live-session coaching (v5.0.3) ──
import training


@app.get("/api/session/notes")
async def api_session_notes(session_id: str = None):
    return JSONResponse({"notes": training.get_notes(session_id)})


@app.post("/api/session/notes")
async def api_session_note_add(request: Request):
    body = await request.json()
    return _res(training.add_note(body.get("text", ""), kind=body.get("kind") or "note", exercise=body.get("exercise"),
                                  session_id=body.get("session_id"), author="athlete", source="app"))


@app.delete("/api/session/notes/{note_id}")
async def api_session_note_delete(note_id: int):
    return _res(training.delete_note(note_id), 404)


@app.post("/api/session/add-exercise")
async def api_session_add_exercise(request: Request):
    body = await request.json()
    return _res(training.add_session_exercise(body.get("exercise", ""), sets=body.get("sets"), reps=body.get("reps"),
                                              weight=body.get("weight"), session_id=body.get("session_id"),
                                              reason=body.get("reason", ""), source="app"))


@app.post("/api/session/remove-exercise")
async def api_session_remove_exercise(request: Request):
    body = await request.json()
    return _res(training.remove_session_exercise(body.get("exercise", ""), session_id=body.get("session_id")))


@app.post("/api/plan/today/add-exercise")
async def api_plan_add_exercise(request: Request):
    body = await request.json()
    return _res(training.add_to_today_plan(body.get("exercise", ""), sets=body.get("sets"), reps=body.get("reps"),
                                           weight=body.get("weight"), source="app"))


@app.get("/api/exercises-suggest")
async def api_exercise_suggest(session_id: str = None, exclude: str = "", limit: int = 8, q: str = ""):
    """Smart suggestions for the Add Exercise sheet. exclude = '|'-separated names already in the plan."""
    names = [x for x in exclude.split("|") if x.strip()]
    return JSONResponse(training.suggest_exercises(session_id, exclude=names, limit=limit, q=q))


@app.get("/api/session/{session_id}/summary")
async def api_session_summary(session_id: str):
    return _res(training.session_summary(session_id), 404)


# ── HOME coaching (v5.0.5) ──
import coaching


@app.get("/api/coaching/overview")
async def api_coaching_overview():
    return JSONResponse(coaching.overview())


@app.post("/api/coaching/brief")
async def api_coaching_brief(request: Request):
    body = await request.json()
    return _res(coaching.set_brief(body.get("text", ""), source="app"))


@app.get("/api/sync-sig")
async def api_sync_sig():
    """Change signature for data Claude writes over MCP outside the live session."""
    return JSONResponse(fx.sync_signature())


@app.get("/api/goals")
async def api_goals(include_inactive: bool = False, category: str = None):
    return JSONResponse({"goals": fx.get_goals(include_inactive=include_inactive, category=category)})


@app.post("/api/goals")
async def api_goals_set(request: Request):
    body = await request.json()
    key, title, reason = body.pop("goal_key", None), body.pop("title", None), body.pop("reason", "")
    fields = {k: v for k, v in body.items() if k in fx._GOAL_FIELDS}
    return _res(fx.set_goal(key, title=title, reason=reason, source="app", **fields))


@app.delete("/api/goals/{goal_key}")
async def api_goals_delete(goal_key: str, reason: str = ""):
    return _res(fx.delete_goal(goal_key, reason=reason, source="app"), 404)


@app.get("/api/debug/env")
async def debug_env():
    """Debug: show which API key env vars are set (names only, no values)."""
    return {
        "OPENAI_API_KEY_set": bool(os.getenv("OPENAI_API_KEY")),
        "openai_API_Key_set": bool(os.getenv("openai_API_Key")),
        "ANTHROPIC_API_KEY_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "JARVIS_MODEL_set": bool(os.getenv("JARVIS_MODEL")),
        "all_env_keys_with_openai": [k for k in os.environ if 'openai' in k.lower() or 'OPENAI' in k],
    }

# MCP HTTP ENDPOINT — JSON-RPC over HTTP
#
# This allows Claude.ai (via Tailscale/ngrok) to call JARVIS MCP tools.
# Supports the MCP Streamable HTTP transport spec.
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FEEDBACK SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Submit user feedback (bug, feature request, design, other)."""
    try:
        data = await request.json()
        category = data.get("category", "other")
        message = data.get("message", "").strip()
        page_context = data.get("page_context", "")

        if not message:
            return JSONResponse({"error": "Message is required"}, status_code=400)

        from database import get_db, _now_ts
        profile = _load("profile.json", DEFAULT_PROFILE)
        athlete_name = profile.get("name", "")

        with get_db() as conn:
            conn.execute(
                """INSERT INTO feedback (category, message, athlete_name, page_context, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (category, message, athlete_name, page_context, _now_ts()),
            )

        # Optionally forward to webhook
        feedback_payload = {
            "category": category,
            "message": message,
            "athlete_name": athlete_name,
            "page_context": page_context,
        }
        if FEEDBACK_WEBHOOK:
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    await client.post(FEEDBACK_WEBHOOK, json=feedback_payload, timeout=5)
            except Exception as e:
                print(f"[feedback] Webhook failed: {e}")

        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/feedback")
async def get_feedback():
    """List all feedback (for admin review)."""
    from database import get_db
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100").fetchall()
    return JSONResponse([dict(r) for r in rows])

MCP_SERVER_INFO = {
    "name": "jarvis-workout",
    "version": "1.0.0",
}

MCP_CAPABILITIES = {
    "tools": {},
}


def _mcp_tool_list() -> list[dict]:
    """Build the MCP tools/list response from MCP_TOOLS registry."""
    tool_list = []
    for name, info in MCP_TOOLS.items():
        tool_list.append({
            "name": name,
            "description": info["description"],
            "inputSchema": info["schema"],
        })
    return tool_list


def _mcp_call_tool(tool_name: str, arguments: dict) -> dict:
    """Execute an MCP tool and return the result content."""
    if tool_name not in MCP_TOOLS:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {tool_name}"})}],
            "isError": True,
        }
    try:
        result = MCP_TOOLS[tool_name]["fn"](arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}],
            "isError": False,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
            "isError": True,
        }


def _handle_mcp_message(msg: dict) -> dict | None:
    """Process a single MCP JSON-RPC message and return the response."""
    method = msg.get("method", "")
    msg_id = msg.get("id")

    # Notifications (no id) — acknowledge silently
    if msg_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": MCP_SERVER_INFO,
                "capabilities": MCP_CAPABILITIES,
            },
        }

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": _mcp_tool_list()},
        }

    elif method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = _mcp_call_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }


@app.post("/mcp")
@app.post("/mcp/{secret}")
async def mcp_endpoint(request: Request, secret: str = ""):
    # Require secret key if MCP_SECRET is configured
    if MCP_SECRET and secret != MCP_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    """MCP JSON-RPC over HTTP endpoint.

    Accepts single JSON-RPC messages or batches.
    Claude.ai MCP connectors POST here.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    # Handle batch requests
    if isinstance(body, list):
        responses = []
        for msg in body:
            resp = _handle_mcp_message(msg)
            if resp is not None:
                responses.append(resp)
        return JSONResponse(responses if responses else {"jsonrpc": "2.0", "id": None, "result": {}})

    # Single request
    resp = _handle_mcp_message(body)
    if resp is None:
        # Notification — return 204 No Content
        return Response(status_code=204)
    return JSONResponse(resp)


@app.get("/mcp")
async def mcp_sse_endpoint(request: Request):
    """SSE endpoint for MCP Streamable HTTP transport.

    Some MCP clients use GET for the SSE channel.
    Returns a simple server-info event so the client knows the server is alive.
    """
    async def event_stream():
        # Send server info as the initial event
        data = json.dumps({
            "jsonrpc": "2.0",
            "method": "server/info",
            "params": MCP_SERVER_INFO,
        })
        yield f"data: {data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CORS — needed for Claude.ai MCP connector to reach this server
# ═══════════════════════════════════════════════════════════════════════════════
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
