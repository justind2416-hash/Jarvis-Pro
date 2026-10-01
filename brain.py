"""
brain.py — Claude AI integration for JARVIS Pro workout assistant.

Dynamically injects the athlete's profile and recent workout history into
the system prompt so JARVIS remembers what happened across sessions.

All athlete-specific data comes from profile.json (written during onboarding)
— nothing is hardcoded.
"""

import os
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except (ImportError, OSError):
    pass
import anthropic
from anthropic.types import TextBlock

import re

from database import (
    log_chat_message,
    get_chat_history_for_prompt,
    get_recent_history_for_prompt,
    log_chat,
    get_or_create_today_session,
    log_workout_set,
)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).resolve().parent / ".env")

JARVIS_MODEL = os.getenv("JARVIS_MODEL", "claude-sonnet-5")
_api_key = os.getenv("ANTHROPIC_API_KEY")

if _api_key:
    client = anthropic.Anthropic(api_key=_api_key)
else:
    client = None


def _load_profile() -> dict:
    """Load the athlete's profile from disk."""
    try:
        from mcp_server import _load, DEFAULT_PROFILE
        return _load("profile.json", DEFAULT_PROFILE)
    except Exception:
        return {"name": "Athlete"}


def _load_program() -> dict:
    """Load the current workout program from disk."""
    try:
        from mcp_server import _load, DEFAULT_PROGRAM
        return _load("program.json", DEFAULT_PROGRAM)
    except Exception:
        return {"name": "No program", "exercises": []}


# ---------------------------------------------------------------------------
# System prompt — built dynamically from athlete profile
# ---------------------------------------------------------------------------

def _build_profile_section(profile: dict) -> str:
    """Build the athlete profile section of the system prompt."""
    name = profile.get("name", "Athlete")
    age = profile.get("age", "")
    dob = profile.get("dob", "")
    height = profile.get("height", "")
    weight = profile.get("weight_lbs", "")
    goals = profile.get("goals", {})
    experience = profile.get("experience", "")
    equipment = profile.get("equipment", [])
    limitations = profile.get("limitations", "")
    frequency = profile.get("training_frequency", "")
    split = profile.get("training_split", "")

    lines = []
    lines.append(f"Name: {name}")
    if age:
        lines.append(f"Age: {age}" + (f" (DOB {dob})" if dob else ""))
    if height:
        lines.append(f"Height: {height}")
    if weight:
        lines.append(f"Weight: ~{weight} lb")

    if goals:
        primary = goals.get("primary", "")
        if primary:
            lines.append(f"Primary goal: {primary}")
        selected = goals.get("selected", [])
        if selected:
            lines.append(f"Goals: {', '.join(g.replace('_', ' ').title() for g in selected)}")

    if experience:
        lines.append(f"Experience level: {experience}")
    if equipment:
        lines.append(f"Equipment: {', '.join(equipment)}")
    if frequency:
        lines.append(f"Training frequency: {frequency} days/week")
    if split:
        lines.append(f"Preferred split: {split.replace('_', ' ').title()}")
    if limitations:
        lines.append(f"Injuries/limitations: {limitations}")

    return "\n".join(lines)


def _build_program_section(program: dict) -> str:
    """Build the program section of the system prompt."""
    name = program.get("name", "No program")
    phase = program.get("phase", "")
    exercises = program.get("exercises", [])
    notes = program.get("notes", "")

    lines = [f"Program: {name}"]
    if phase:
        lines.append(f"Phase: {phase}")
    if exercises:
        for ex in exercises:
            line = f"  • {ex.get('name', '?')}: {ex.get('sets', '?')}×{ex.get('reps', '?')}"
            if ex.get('working_weight'):
                line += f" @ {ex['working_weight']}"
            if ex.get('notes'):
                line += f" — {ex['notes']}"
            lines.append(line)
    if notes:
        lines.append(f"Notes: {notes}")

    return "\n".join(lines)


def _build_system_prompt_base() -> str:
    """Build the static portion of the system prompt from the athlete's profile."""
    profile = _load_profile()
    program = _load_program()
    name = profile.get("name", "Athlete")

    return f"""\
You are JARVIS — {name}'s AI strength-and-conditioning coach.
Speak in a calm, direct, British-inflected tone (like the MCU JARVIS).
Keep replies concise for voice readback — 2-3 sentences per block unless detail is requested.

═══ ATHLETE PROFILE ═══
{_build_profile_section(profile)}

═══ CURRENT PROGRAM ═══
{_build_program_section(program)}

═══ GREETING RULE ═══
When {name} opens JARVIS, greet them naturally — like a personal AI assistant, \
not a gym machine. Do NOT immediately launch into workout mode.
If {name} asks about a workout or says they're ready to train, THEN load the program \
and shift into coaching mode. Otherwise, be conversational and helpful on any topic.

═══ COACHING RULES ═══
1. Always greet {name} by name at session start
2. State the day's workout focus and estimated duration
3. Call out warm-up sets vs. working sets explicitly
4. Track RPE (Rate of Perceived Exertion) — ask after heavy sets
5. If {name} reports discomfort, stop the movement and offer a substitute
6. Log every set: exercise, weight, reps, RPE, notes
7. At session end, summarize volume and flag any carry-forward items
8. On weigh-in days, compare to previous and note trend
9. Never fabricate data — if you don't know a weight or rep count, ask
10. Keep the energy focused and professional — motivate through competence, not hype

═══ GENERAL CONVERSATION ═══
You are not ONLY a workout coach. {name} may ask you ANYTHING — nutrition, recovery, \
sleep, stress, general knowledge, business questions, or just chat. Answer naturally \
and helpfully, drawing on your full knowledge. You are their personal AI assistant \
who happens to specialize in fitness.

═══ SET LOGGING — CRITICAL ═══
When {name} reports completing a set (e.g. "238 for 10", "did 100 x 12", "got 8 reps at 25 lb each"), \
you MUST include a hidden structured tag in your response so the frontend can update the workout panel.

Format: <!--SET:{{"exercise":"Exercise Name","weight":"238","reps":"10","set_num":2}}-->

Rules for the SET tag:
- Use the EXACT exercise name from the program
- Include the tag ONCE per set logged, at the END of your response
- set_num is which set number this is for that exercise (1, 2, 3, etc.)
- The tag is invisible to {name} — your natural text reply should still acknowledge the set

CRITICAL — EXERCISE MODIFICATIONS:
When {name} skips or replaces an exercise, you MUST include a MOD tag.

SKIP example:
<!--MOD:{{"action":"skip","exercise":"Chin-Up Test","reason":"skipped by request"}}-->

REPLACE example:
<!--MOD:{{"action":"replace","exercise":"Step-Ups","replacement":"Bulgarian Split Squats","weight":"20 lb each","reps":"10/side","sets":3}}-->

RULES:
- Use the EXACT original exercise name from the program
- Always include the MOD tag at the END of your response
- ALWAYS include weight, reps, and sets in replace tags
- The tag is invisible to {name} — your text reply should confirm the change naturally
- If {name} reports multiple sets at once, include one tag per set
- Weight should be just the number (e.g. "238" not "238 lb")
- Reps should be just the number (e.g. "10" not "10 reps")
"""

MAX_TOKENS = 1024

# ---------------------------------------------------------------------------
# SET tag parser — extracts and logs structured sets from Claude's response
# ---------------------------------------------------------------------------
SET_TAG_PATTERN = re.compile(r'<!--SET:(.*?)-->')

# MOD tag parser — handles skip/replace exercise modifications
MOD_TAG_PATTERN = re.compile(r"<!--MOD:(.*?)-->")
DEV_TAG_PATTERN = re.compile(r"<!--DEV:(.*?)-->")

def _process_mod_tags(reply):
    import json as _json
    from database import log_exercise_modification
    for match in MOD_TAG_PATTERN.finditer(reply):
        try:
            data = _json.loads(match.group(1))
            action = data.get("action", "skip")
            exercise = data.get("exercise", "")
            if exercise:
                log_exercise_modification(
                    session_id=None,
                    original_exercise=exercise,
                    action=action,
                    replacement_exercise=data.get("replacement"),
                    replacement_weight=data.get("weight"),
                    replacement_reps=data.get("reps"),
                    replacement_sets=data.get("sets"),
                    reason=data.get("reason", ""),
                )
        except Exception:
            pass


def _process_dev_tags(reply):
    """Process <!--DEV:{...}--> tags for planned vs actual deviations."""
    import json as _json
    from database import log_deviation
    for match in DEV_TAG_PATTERN.finditer(reply):
        try:
            data = _json.loads(match.group(1))
            log_deviation(
                exercise=data.get('exercise', ''),
                planned_weight=data.get('planned_weight', ''),
                planned_reps=data.get('planned_reps', ''),
                actual_weight=data.get('actual_weight', ''),
                actual_reps=data.get('actual_reps', ''),
                deviation_type=data.get('type', 'other'),
                reason=data.get('reason', ''),
                planned_notes=data.get('planned_notes', ''),
                actual_notes=data.get('actual_notes', ''),
                set_number=data.get('set', 1),
            )
        except Exception as e:
            print(f'[DEV tag error] {e}')


def _process_set_tags(reply: str) -> str:
    """Find <!--SET:{...}--> tags in the reply, log each set to the DB, and return reply unchanged."""
    import json as _json
    for match in SET_TAG_PATTERN.finditer(reply):
        try:
            data = _json.loads(match.group(1))
            exercise = data.get("exercise", "")
            weight = str(data.get("weight", ""))
            reps = str(data.get("reps", ""))
            if exercise and weight and reps:
                log_workout_set(
                    exercise=exercise,
                    weight=weight,
                    reps=reps,
                    rpe=str(data.get("rpe", "")),
                    notes=data.get("notes", ""),
                )
        except (_json.JSONDecodeError, Exception):
            pass
    return reply


# ---------------------------------------------------------------------------
# Dynamic system prompt builder
# ---------------------------------------------------------------------------


def _auto_classify_message(text: str) -> tuple:
    """Auto-detect message type, tags, and sentiment from content."""
    text_lower = text.lower()
    tags = []
    message_type = "general"
    sentiment = "neutral"
    exercise_context = None

    pain_words = ["pain", "hurt", "sore", "tight", "tweak", "ache", "stiff", "strain", "sharp", "numb", "tingling"]
    body_parts = ["shoulder", "back", "knee", "hip", "neck", "elbow", "wrist", "ankle"]

    for w in pain_words:
        if w in text_lower:
            tags.append("pain")
            sentiment = "pain"
            message_type = "pain_report"
            break

    for bp in body_parts:
        if bp in text_lower:
            tags.append(bp)
            if "pain" not in tags:
                tags.append("body")

    form_words = ["form", "cue", "technique", "position", "grip", "stance", "brace", "squeeze", "anchor"]
    for w in form_words:
        if w in text_lower:
            tags.append("form")
            message_type = "form_cue"
            break

    if any(w in text_lower for w in ["pr", "personal record", "new max", "went up", "progress"]):
        tags.append("PR")
        sentiment = "positive"

    exercises = [
        "squat", "press", "row", "pulldown", "pull-through", "push-up", "plank",
        "hip thrust", "step-up", "pallof", "curl", "fly", "deadlift", "bench",
        "hollow body", "bird dog", "carry", "hang", "chin-up", "pull-up"
    ]
    for ex in exercises:
        if ex in text_lower:
            exercise_context = ex
            break

    if any(c.isdigit() for c in text) and any(w in text_lower for w in ["for", "x", "reps", "set", "lb"]):
        message_type = "workout_log"

    if any(w in text_lower for w in ["easy", "light", "too light", "plenty left"]):
        tags.append("easy")
        sentiment = "positive"
    elif any(w in text_lower for w in ["hard", "heavy", "struggled", "barely", "failed", "grind"]):
        tags.append("hard")
        sentiment = "concern"

    return message_type, list(set(tags)), sentiment, exercise_context


def _build_system_prompt() -> str:
    """Build the full system prompt with live context injected on every message."""
    profile = _load_profile()
    name = profile.get("name", "Athlete")

    parts = [_build_system_prompt_base()]

    # Inject live context
    from datetime import datetime as _dt
    now = _dt.now()
    day_name = now.strftime("%A")
    date_str = now.strftime("%B %d, %Y")
    time_str = now.strftime("%I:%M %p").lstrip("0")
    hour = now.hour
    time_of_day = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"

    is_weekday = now.weekday() < 5

    context_lines = [
        f"Current date: {day_name}, {date_str}",
        f"Current time: {time_str} ({time_of_day})",
        f"Athlete: {name}",
        f"Training day: {'Yes (weekday)' if is_weekday else 'Rest day (weekend)'}",
    ]
    if hour < 7:
        context_lines.append(f"Note: It's very early. {name} may be doing an early session.")
    elif hour >= 22:
        context_lines.append("Note: It's late. Keep responses brief unless asked.")

    parts.append("\n═══ LIVE CONTEXT ═══\n" + "\n".join(context_lines))

    # Inject workout history
    history_block = get_recent_history_for_prompt(session_limit=5)
    if history_block.strip():
        parts.append("\n═══ SESSION MEMORY (from database) ═══\n" + history_block)

    # Inject coaching context
    try:
        from database import get_full_coaching_context
        coaching_ctx = get_full_coaching_context()
        if coaching_ctx:
            parts.append("\n" + coaching_ctx)
    except Exception:
        pass

    # Inject recent chat context
    try:
        chat_context = get_chat_history_for_prompt(limit=30, days=7)
        if chat_context and "No recent conversations" not in chat_context:
            parts.append("\n═══ RECENT CONVERSATIONS ═══\n" + chat_context)
    except Exception:
        pass

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_messages(user_message: str, history: list[dict]) -> list[dict]:
    """Combine conversation history with the latest user message."""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": user_message})
    return messages


def _ensure_client() -> anthropic.Anthropic:
    """Return the initialised client or raise a clear error."""
    if client is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file or export it as an environment variable."
        )
    return client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def chat(user_message: str, history: list[dict]) -> str:
    """Send a message to Claude and return the full response text."""
    log_chat("user", user_message)

    try:
        c = _ensure_client()
        system_prompt = _build_system_prompt()
        response = c.messages.create(
            model=JARVIS_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=_build_messages(user_message, history),
            thinking={"type": "disabled"},
        )
        text_blocks = [b for b in response.content if isinstance(b, TextBlock)]
        reply = text_blocks[0].text if text_blocks else ""

        if reply:
            log_chat("assistant", reply)
            _process_set_tags(reply)

        return reply
    except RuntimeError as exc:
        return str(exc)
    except anthropic.APIConnectionError:
        return "I'm unable to reach the Anthropic API at the moment. Check your network connection and try again."
    except anthropic.AuthenticationError:
        return "Authentication failed. Please verify your ANTHROPIC_API_KEY is correct."
    except anthropic.RateLimitError:
        return "Rate limit hit. Give it a moment, then try again."
    except anthropic.APIStatusError as exc:
        return f"API error ({exc.status_code}): {exc.message}"
    except Exception as exc:
        return f"Unexpected error: {exc}"


async def chat_stream(user_message: str, history: list[dict]):
    """Stream a response from Claude, yielding text chunks as they arrive."""
    log_chat("user", user_message)

    full_reply = ""
    try:
        c = _ensure_client()
        system_prompt = _build_system_prompt()
        with c.messages.stream(
            model=JARVIS_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=_build_messages(user_message, history),
            thinking={"type": "disabled"},
        ) as stream:
            for text in stream.text_stream:
                full_reply += text
                yield text

        if full_reply:
            log_chat("assistant", full_reply)
            _process_set_tags(full_reply)

    except RuntimeError as exc:
        yield str(exc)
    except anthropic.APIConnectionError:
        yield "I'm unable to reach the Anthropic API at the moment. Check your network connection and try again."
    except anthropic.AuthenticationError:
        yield "Authentication failed. Please verify your ANTHROPIC_API_KEY is correct."
    except anthropic.RateLimitError:
        yield "Rate limit hit. Give it a moment, then try again."
    except anthropic.APIStatusError as exc:
        yield f"API error ({exc.status_code}): {exc.message}"
    except Exception as exc:
        yield f"Unexpected error: {exc}"
