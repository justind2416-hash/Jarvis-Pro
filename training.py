"""
training.py — live-session coaching (v5.0.3): coach notes, exercises added mid-session,
smart exercise suggestions and the end-of-session summary.

Added exercises reuse exercise_modifications (action='add') so they sit next to the existing
skip/replace records. Coach notes get their own table; both are keyed by the text session_id,
so demo sessions clean them up with everything else.
"""

import re

from database import get_db, _run_ddl, _now_ts, _local_today, ANALYTIC_SET, _find_session, _duration_min
import fitness as fx

NOTE_KINDS = ("note", "cue", "adjust", "warning", "praise", "pain")


def migrate_training():
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS session_coaching_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            author TEXT DEFAULT 'claude',
            kind TEXT DEFAULT 'note',
            exercise TEXT,
            text TEXT NOT NULL,
            source TEXT,
            deleted_at TEXT,
            is_demo INTEGER DEFAULT 0
        )
    """)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_coach_notes_session ON session_coaching_notes(session_id)")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def live_session():
    """The open (live) non-backfill session, demo included, or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL "
            "AND (is_backfill IS NULL OR CAST(is_backfill AS INTEGER) = 0) ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _resolve_session(session_id=None):
    """Explicit session (int id or text id), else the live one, else today's latest."""
    if session_id not in (None, ""):
        with get_db() as conn:
            return _find_session(conn, session_id)
    s = live_session()
    if s:
        return s
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE date = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                           (_local_today(),)).fetchone()
    return dict(row) if row else None


def _is_demo(session_id) -> bool:
    return str(session_id or "").startswith("demo_")


# ---------------------------------------------------------------------------
# Coach notes
# ---------------------------------------------------------------------------

def add_note(text: str, kind: str = "note", exercise: str = None, session_id=None,
             author: str = "claude", source: str = "mcp") -> dict:
    text = str(text or "").strip()
    if not text:
        return {"error": "text is required"}
    if kind not in NOTE_KINDS:
        return {"error": f"kind must be one of {list(NOTE_KINDS)}"}
    sess = _resolve_session(session_id)
    if not sess:
        return {"error": "No session to attach the note to — start one first or pass session_id"}
    with get_db() as conn:
        row = conn.execute(
            """INSERT INTO session_coaching_notes (session_id, created_at, author, kind, exercise, text, source, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (sess["session_id"], _now_ts(), author, kind, exercise or None, text[:2000], source,
             1 if _is_demo(sess["session_id"]) else 0),
        ).fetchone()
    return {"status": "posted", "note": {"id": row["id"], "session_id": sess["session_id"], "kind": kind,
                                         "author": author, "exercise": exercise, "text": text}}


def get_notes(session_id=None, include_deleted: bool = False) -> list:
    sess = _resolve_session(session_id)
    if not sess:
        return []
    where = "" if include_deleted else " AND deleted_at IS NULL"
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, session_id, created_at, author, kind, exercise, text, deleted_at FROM session_coaching_notes "
            f"WHERE session_id = ?{where} ORDER BY id DESC",
            (sess["session_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_note(note_id: int) -> dict:
    with get_db() as conn:
        n = conn.execute("UPDATE session_coaching_notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                         (_now_ts(), int(note_id))).rowcount
    return {"status": "deleted", "id": note_id} if n else {"error": f"No live note {note_id}"}


# ---------------------------------------------------------------------------
# Exercises added to a live session
# ---------------------------------------------------------------------------

def add_session_exercise(exercise: str, sets=None, reps: str = None, weight: str = None,
                         session_id=None, reason: str = "", source: str = "app") -> dict:
    name = str(exercise or "").strip()
    if not name:
        return {"error": "exercise is required"}
    sess = _resolve_session(session_id)
    if not sess or sess.get("ended_at"):
        return {"error": "No live session — start a workout first"}
    lib = fx.find_exercise(name)
    if lib:
        name = lib["name"]
    sets = int(sets) if sets not in (None, "") else int((lib or {}).get("default_sets") or 3)
    reps = str(reps) if reps not in (None, "") else str((lib or {}).get("default_reps") or "10")
    if weight in (None, ""):
        weight = "bodyweight" if lib and "bodyweight" in lib["equipment"] and len(lib["equipment"]) == 1 else ""
    for ex in get_session_exercises(sess["session_id"]):
        if fx._key(ex["name"]) == fx._key(name):
            return {"error": f"{name} is already added to this session", "exercise": ex}
    with get_db() as conn:
        conn.execute(
            """INSERT INTO exercise_modifications (session_id, original_exercise, action, replacement_exercise,
                   replacement_weight, replacement_reps, replacement_sets, reason, timestamp)
               VALUES (?, ?, 'add', ?, ?, ?, ?, ?, ?)""",
            (sess["session_id"], name, name, str(weight or ""), reps, sets, f"[{source}] {reason or ''}".strip()[:500], _now_ts()),
        )
    added = next(e for e in get_session_exercises(sess["session_id"]) if e["name"] == name)
    return {"status": "added", "session_id": sess["session_id"], "exercise": added}


def remove_session_exercise(exercise: str, session_id=None) -> dict:
    """Mark an added exercise as removed (a 'remove_added' row; nothing is deleted)."""
    sess = _resolve_session(session_id)
    if not sess:
        return {"error": "No session"}
    if not any(fx._key(e["name"]) == fx._key(exercise) for e in get_session_exercises(sess["session_id"])):
        return {"error": f"{exercise} isn't an added exercise in this session"}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO exercise_modifications (session_id, original_exercise, action, reason, timestamp) VALUES (?, ?, 'remove_added', '', ?)",
            (sess["session_id"], exercise, _now_ts()),
        )
    return {"status": "removed", "exercise": exercise}


def get_session_exercises(session_id) -> list:
    """Exercises added to a session (in order), enriched from the library for the TRAIN card."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, original_exercise, action, replacement_weight, replacement_reps, replacement_sets, reason "
            "FROM exercise_modifications WHERE session_id = ? AND action IN ('add', 'remove_added') ORDER BY id",
            (session_id,),
        ).fetchall()
    out = {}
    for r in rows:
        k = fx._key(r["original_exercise"])
        if r["action"] == "remove_added":
            out.pop(k, None)
            continue
        lib = fx.find_exercise(r["original_exercise"]) or {}
        out[k] = {
            "name": r["original_exercise"], "sets": r["replacement_sets"] or 3, "reps": r["replacement_reps"] or "",
            "weight": r["replacement_weight"] or "", "added_by": "claude" if (r["reason"] or "").startswith("[mcp]") else "app",
            "reason": re.sub(r"^\[\w+\]\s*", "", r["reason"] or ""), "schema_type": lib.get("schema_type"), "cues": lib.get("cues", ""),
            "caution": lib.get("caution", ""), "muscles": lib.get("primary_muscles", []), "video": lib.get("video_url", ""),
        }
    return list(out.values())


# ---------------------------------------------------------------------------
# Smart suggestions
# ---------------------------------------------------------------------------

_UNAVAILABLE = {"barbell", "machine"}  # home gym: G15 cables, dumbbells, bands, bench, bar, box, ball


def suggest_exercises(session_id=None, exclude=None, limit: int = 8, q: str = "") -> dict:
    """Rank library exercises for adding to the session right now. Signals, in order of weight:
    rehab targets behind their weekly goal, muscle groups under 10 hard sets this week
    (V-taper groups weighted up), and what today's session is already training."""
    sess = _resolve_session(session_id)
    excl = {fx._key(x) for x in (exclude or [])}
    today_groups, today_rehab = {}, set()
    if sess:
        with get_db() as conn:
            done = [r["exercise"] for r in conn.execute(
                f"SELECT ws.exercise FROM workout_sets ws WHERE ws.session_id = ? AND {ANALYTIC_SET}",
                (sess["session_id"],)).fetchall()]
        for name in done + [e["name"] for e in get_session_exercises(sess["session_id"])]:
            excl.add(fx._key(name))
            m = fx.exercise_muscles(name)
            today_rehab.update(m["rehab_targets"])
            for g, w in fx.classify_set(name)["groups"].items():
                today_groups[g] = today_groups.get(g, 0) + w
    vol = fx.muscle_volume(1)["this_week"]
    goals = {g["goal_key"]: g for g in fx.get_goals()}
    rehab_need = {}
    for t in fx.REHAB_TARGETS:
        g = goals.get("rehab_" + t)
        if g and g.get("current") is not None and g.get("target_value") and g["current"] < g["target_value"] and t not in today_rehab:
            rehab_need[t] = g
    focus = max(today_groups, key=today_groups.get) if today_groups else None
    cands = fx.search_exercises(q) if q else fx.search_exercises()
    scored = []
    for ex in cands:
        if fx._key(ex["name"]) in excl or ex.get("category") == "cardio" or ex.get("muscle_group") == "cardio":
            continue
        if set(ex["equipment"]) and set(ex["equipment"]) <= _UNAVAILABLE:
            continue
        score, reasons = 0.0, []
        for t in ex["rehab_targets"]:
            if t in rehab_need:
                g = rehab_need[t]
                score += 5 + (g["target_value"] - g["current"])
                reasons.append(f"{fx.REHAB_LABELS[t]} rehab: {int(g['current'])}/{int(g['target_value'])} days this week")
        groups = fx.classify_set(ex["name"])["groups"]
        for grp, w in groups.items():
            if w < 1:
                continue
            have = vol["sets"].get(grp, 0)
            if have < fx.WEEKLY_SET_TARGET["min"]:
                gap = (fx.WEEKLY_SET_TARGET["min"] - have) / fx.WEEKLY_SET_TARGET["min"]
                boost = 1.5 if grp in ("Shoulders", "Back", "Chest") and "v-taper" in ex["tags"] else 1.0
                score += 3 * gap * boost
                reasons.append(f"{grp}: {have:g} hard sets this week (target {fx.WEEKLY_SET_TARGET['min']}+)")
        if focus and focus in groups:
            score += 1.5
            reasons.append(f"Fits today's {focus.lower()} focus")
        if "v-taper" in ex["tags"]:
            score += 0.5
        if ex.get("difficulty") == "advanced":
            score -= 1.5
        if ex.get("caution"):
            score -= 0.3
        if q:
            # Text matches are already filtered; name hits outrank cue/tag hits.
            score += 2 + (6 if all(w in ex["name"].lower() or fx._key(w) in fx._key(ex["name"])
                                   for w in q.lower().split()) else 0)
        if score <= 0 and not q:
            continue
        scored.append((score, ex, reasons))
    scored.sort(key=lambda t: (-t[0], t[1]["name"]))
    out = []
    for score, ex, reasons in scored[: max(1, int(limit))]:
        out.append({"name": ex["name"], "score": round(score, 2), "reasons": reasons[:3], "muscle_group": ex["muscle_group"],
                    "primary_muscles": ex["primary_muscles"], "rehab_targets": ex["rehab_targets"], "tags": ex["tags"],
                    "default_sets": ex["default_sets"], "default_reps": ex["default_reps"], "cues": ex["cues"],
                    "caution": ex["caution"], "schema_type": ex["schema_type"], "equipment": ex["equipment"]})
    return {"session_id": sess["session_id"] if sess else None, "focus": focus,
            "rehab_behind": [fx.REHAB_LABELS[t] for t in rehab_need], "suggestions": out}


# ---------------------------------------------------------------------------
# End-of-session summary
# ---------------------------------------------------------------------------

def _top_weight(sets) -> float:
    best = 0.0
    for s in sets:
        w = str(s.get("weight") or "")
        if re.search(r"mph|body|test|band", w, re.I):
            continue
        m = re.search(r"\d+(\.\d+)?", w)
        if m:
            best = max(best, float(m.group(0)))
    return best


def session_summary(session_id=None) -> dict:
    sess = _resolve_session(session_id)
    if not sess:
        return {"error": "No session found"}
    sid = sess["session_id"]
    with get_db() as conn:
        sets = [dict(r) for r in conn.execute(
            f"SELECT ws.id, ws.exercise, ws.weight, ws.reps, ws.rpe, ws.notes FROM workout_sets ws "
            f"WHERE ws.session_id = ? AND {ANALYTIC_SET} ORDER BY ws.id", (sid,)).fetchall()]
        # Best previous load per exercise (real sessions dated before this one).
        prior = [dict(r) for r in conn.execute(
            f"""SELECT ws.exercise, ws.weight FROM workout_sets ws JOIN sessions s ON s.session_id = ws.session_id
                WHERE s.session_id != ? AND s.date <= ? AND s.deleted_at IS NULL
                AND substr(s.session_id, 1, 5) != 'demo_' AND {ANALYTIC_SET}""",
            (sid, sess["date"])).fetchall()]
        prev = conn.execute(
            "SELECT session_id, date FROM sessions WHERE program = ? AND session_id != ? AND date <= ? AND deleted_at IS NULL "
            "AND substr(session_id, 1, 5) != 'demo_' ORDER BY date DESC, id DESC LIMIT 1",
            (sess.get("program"), sid, sess["date"])).fetchone()
        prev_sets = [dict(r) for r in conn.execute(
            f"SELECT ws.weight, ws.reps FROM workout_sets ws WHERE ws.session_id = ? AND {ANALYTIC_SET}",
            (prev["session_id"],)).fetchall()] if prev else []
    prior_best = {}
    for p in prior:
        k = fx._key((fx.find_exercise(p["exercise"]) or {}).get("name") or p["exercise"])
        prior_best[k] = max(prior_best.get(k, 0.0), _top_weight([p]))
    by_ex, groups, rehab = {}, {}, set()
    for s in sets:
        e = by_ex.setdefault(s["exercise"], {"exercise": s["exercise"], "sets": [], "volume": 0})
        e["sets"].append(f"{s['weight']} × {s['reps']}")
        e["volume"] += round(fx._set_load(s))
        c = fx.classify_set(s["exercise"])
        for g, w in c["groups"].items():
            groups[g] = groups.get(g, 0) + w
        rehab.update(fx.exercise_muscles(s["exercise"])["rehab_targets"])
    prs = []
    for name, e in by_ex.items():
        top = _top_weight([s for s in sets if s["exercise"] == name])
        k = fx._key((fx.find_exercise(name) or {}).get("name") or name)
        if top > 0 and k in prior_best and top > prior_best[k]:
            prs.append({"exercise": name, "weight": top, "previous": prior_best[k]})
        e["top_weight"] = top or None
    volume = round(sum(fx._set_load(s) for s in sets))
    prev_volume = round(sum(fx._set_load(s) for s in prev_sets)) if prev else None
    planned = None
    try:
        from database import get_planned_vs_actual
        pva = get_planned_vs_actual(sid)
        planned = {k: pva.get(k) for k in ("mode", "summary") if k in pva} or None
    except Exception:
        pass
    notes = get_notes(sid)
    goals = [g for g in fx.get_goals() if g["category"] == "rehab"]
    return {
        "session_id": sid, "program": sess.get("program"), "date": sess.get("date"),
        "started_at": sess.get("started_at"), "ended_at": sess.get("ended_at"),
        "duration_min": _duration_min(sess.get("started_at"), sess.get("ended_at")),
        "demo": _is_demo(sid), "total_sets": len(sets), "total_volume": volume,
        "previous": {"date": prev["date"], "total_volume": prev_volume,
                     "volume_change_pct": round((volume - prev_volume) / prev_volume * 100) if prev_volume else None} if prev else None,
        "exercises": list(by_ex.values()), "muscle_groups": {g: round(v, 1) for g, v in sorted(groups.items(), key=lambda t: -t[1])},
        "rehab_targets_hit": [fx.REHAB_LABELS[t] for t in fx.REHAB_TARGETS if t in rehab],
        "rehab_week": [{"title": g["title"], "current": g["current"], "target": g["target_value"]} for g in goals],
        "prs": prs, "coach_notes": notes, "planned_vs_actual": planned,
    }


# ---------------------------------------------------------------------------
# Add a library exercise to today's plan (v5.0.4) — used when no session is live
# ---------------------------------------------------------------------------

def add_to_today_plan(exercise: str, sets=None, reps: str = None, weight: str = None, source: str = "app") -> dict:
    """Append an exercise to today's planned workout. The plan row is superseded, not edited
    (save_planned_schedule keeps the prior version). With no plan for today, a 'Custom' plan is
    created. Refused once today's plan is completed/replaced."""
    from database import get_planned_workout, save_planned_schedule, demo_active
    if demo_active():
        return {"error": "Demo mode is on — plans aren't changed during a demo. Add it to the demo session instead."}
    name = str(exercise or "").strip()
    if not name:
        return {"error": "exercise is required"}
    lib = fx.find_exercise(name) or {}
    name = lib.get("name", name)
    plan = get_planned_workout(_local_today())
    if plan and plan.get("status") not in (None, "pending"):
        return {"error": f"Today's plan is already {plan['status']} — start a session to add exercises"}
    exercises = list((plan or {}).get("exercises") or [])
    if any(fx._key(e.get("name")) == fx._key(name) for e in exercises):
        return {"error": f"{name} is already in today's plan"}
    item = {"name": name, "sets": int(sets) if sets not in (None, "") else int(lib.get("default_sets") or 3),
            "reps": str(reps) if reps not in (None, "") else str(lib.get("default_reps") or "10"),
            "schema_type": lib.get("schema_type") or "strength_standard", "cues": lib.get("cues", "")}
    if weight not in (None, ""):
        item["weight"] = str(weight)
    elif lib.get("equipment") == ["bodyweight"]:
        item["weight"] = "bodyweight"
    exercises.append(item)
    day = {
        "date": _local_today(), "program_name": (plan or {}).get("program_name") or "Custom",
        "exercises": exercises, "warmup": (plan or {}).get("warmup", []),
        "carry_forward": (plan or {}).get("carry_forward", []),
        "description": (plan or {}).get("description", "") or "Built from the exercise library",
        "notes": (plan or {}).get("notes", ""),
    }
    res = save_planned_schedule([day])
    if res.get("error"):
        return res
    return {"status": "added_to_plan", "date": day["date"], "program_name": day["program_name"],
            "exercise": item, "source": source}
