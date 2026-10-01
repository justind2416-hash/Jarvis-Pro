"""
coaching.py — HOME coaching overview (v5.0.5).

One read that answers "where am I against the Dec 24 goals?": countdown, weight pace and
projection, rehab status per target, running progress, this week's training balance, and a
short list of rule-based insights. Claude (the coach) can pin a free-text brief on top via
MCP set_coach_brief; briefs are append-only so the history stays.
"""

import re
from datetime import datetime, timedelta

from database import get_db, _run_ddl, _now_ts, _local_now, _local_today, get_bodyweight_history, demo_flag
import fitness as fx

BIRTHDAY = fx.BIRTHDAY
BLOCK_START = "2026-10-01"  # start of the 12-week run-in to 40
PUSH = ("horizontal_push", "vertical_push")
PULL = ("horizontal_pull", "vertical_pull")
VTAPER = ("Shoulders", "Back", "Chest")


def migrate_coaching():
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS coach_briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            text TEXT NOT NULL,
            source TEXT,
            is_demo INTEGER DEFAULT 0
        )
    """)


def set_brief(text: str, source: str = "mcp") -> dict:
    text = str(text or "").strip()
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO coach_briefs (created_at, text, source, is_demo) VALUES (?, ?, ?, ?) RETURNING id",
            (_now_ts(), text[:3000], source, demo_flag()),
        ).fetchone()
    return {"status": "cleared" if not text else "posted", "id": row["id"], "text": text}


def get_brief():
    with get_db() as conn:
        row = conn.execute("SELECT id, created_at, text, source FROM coach_briefs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row and row["text"] else None


# ---------------------------------------------------------------------------

def _d(iso: str):
    return datetime.strptime(iso, "%Y-%m-%d").date()


def _weight_section(goal: dict, days_left: int) -> dict:
    """Trend, actual rate (least-squares slope over the last 21 days of on-protocol weigh-ins),
    rate needed to reach the top of the target range, and where the current rate lands on Dec 24."""
    entries = [e for e in get_bodyweight_history(200, include_off_protocol=False)]
    today = _local_now().date()
    recent = [e for e in entries if _d(e["date"]) >= today - timedelta(days=20)]
    trend, last_date = fx.bodyweight_trend(7)
    lo = (goal or {}).get("target_min") or 185
    hi = (goal or {}).get("target_max") or 190
    rate = None
    if len({e["date"] for e in recent}) >= 3:
        xs = [(_d(e["date"]) - today).days for e in recent]
        ys = [float(e["weight_lbs"]) for e in recent]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if den:
            rate = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 7, 2)  # lb / week
    need = round((hi - trend) / max(1, days_left) * 7, 2) if trend is not None and days_left > 0 else None
    projected = round(trend + rate * days_left / 7, 1) if trend is not None and rate is not None else None
    if trend is None:
        status = "no_data"
    elif trend <= hi:
        status = "in_range" if trend >= lo else "below_range"
    elif rate is None:
        status = "need_data"
    elif projected is not None and projected <= hi:
        status = "on_pace"
    else:
        status = "behind"
    return {"trend_7d": trend, "latest": entries[0]["weight_lbs"] if entries else None,
            "latest_date": entries[0]["date"] if entries else None, "target_min": lo, "target_max": hi,
            "to_go": round(trend - hi, 1) if trend is not None else None, "rate_lb_per_wk": rate,
            "needed_lb_per_wk": need, "projected_dec24": projected, "status": status,
            "start": (goal or {}).get("start_value"), "pct": (goal or {}).get("pct")}


def _rehab_section(goals: dict) -> list:
    """Per rehab target: days hit this week vs target and the last date any set for it was logged."""
    sets60 = fx.recent_sets(60)
    last = {}
    for s in sets60:
        for t in fx.exercise_muscles(s["exercise"])["rehab_targets"]:
            if s["date"] > last.get(t, ""):
                last[t] = s["date"]
    today = _local_now().date()
    out = []
    for t in fx.REHAB_TARGETS:
        g = goals.get("rehab_" + t) or {}
        ld = last.get(t)
        days_since = (today - _d(ld)).days if ld else None
        cur, tgt = g.get("current") or 0, g.get("target_value") or 4
        status = "done" if cur >= tgt else "due" if days_since is None or days_since >= 2 else "ok"
        picks = [x["name"] for x in fx.search_exercises(rehab_target=t, category="rehab", limit=3)]
        out.append({"target": t, "label": fx.REHAB_LABELS[t], "days_this_week": int(cur), "target_days": int(tgt),
                    "last_done": ld, "days_since": days_since, "status": status, "try": picks})
    return out


def _run_section(goals: dict) -> dict:
    g = goals.get("run_distance") or {}
    sets = fx.recent_sets(90)
    runs = [(s["date"], fx.set_distance_mi(s)) for s in sets if fx._is_run(s)]
    runs = [(d, mi) for d, mi in runs if mi]
    week_cut = (_local_now().date() - timedelta(days=6)).isoformat()
    week = [mi for d, mi in runs if d >= week_cut]
    return {"longest_mi": g.get("current"), "target_mi": g.get("target_value"), "target_max_mi": g.get("target_max"),
            "target_date": g.get("target_date"), "week_mi": round(sum(week), 2), "runs_this_week": len(week),
            "last_run": max((d for d, _ in runs), default=None), "pct": g.get("pct")}


def _training_section(goals: dict) -> dict:
    vol = fx.muscle_volume(2)
    tw, prev = vol["this_week"], vol["weeks"][0]
    sets7 = fx.recent_sets(7)
    push = pull = 0
    for s in sets7:
        pat = (fx.find_exercise(s["exercise"]) or {}).get("movement_pattern")
        push += pat in PUSH
        pull += pat in PULL
    freq = goals.get("training_frequency") or {}
    with get_db() as conn:
        last = conn.execute(
            "SELECT date, program FROM sessions WHERE deleted_at IS NULL AND substr(session_id, 1, 5) != 'demo_' "
            "AND (is_demo IS NULL OR is_demo = 0) ORDER BY date DESC, id DESC LIMIT 1").fetchone()
    return {"sessions_this_week": int(freq.get("current") or 0), "sessions_target": int(freq.get("target_value") or 4),
            "sets": tw["sets"], "status": tw["status"], "rehab_sets": tw["rehab_sets"],
            "low_groups": [g for g, st in tw["status"].items() if st == "low"],
            "vtaper_sets": round(sum(tw["sets"][g] for g in VTAPER), 1),
            "vtaper_sets_prev": round(sum(prev["sets"][g] for g in VTAPER), 1),
            "push_sets": push, "pull_sets": pull,
            "last_session": dict(last) if last else None, "target": vol["target"]}


def _insights(weight, rehab, run, train, days_left) -> list:
    out = []
    for r in rehab:
        if r["status"] == "due":
            when = "never logged" if r["days_since"] is None else f"last done {r['days_since']} days ago"
            out.append({"level": "warn", "area": "rehab",
                        "text": f"{r['label']}: {when} ({r['days_this_week']}/{r['target_days']} this week). Try {', '.join(r['try'][:2])}."})
    st = weight["status"]
    if st == "behind" and weight["rate_lb_per_wk"] is not None:
        verb = "gaining" if weight["rate_lb_per_wk"] > 0 else "losing"
        out.append({"level": "warn", "area": "weight",
                    "text": f"Weight: {verb} {abs(weight['rate_lb_per_wk'])} lb/wk → ~{weight['projected_dec24']} on Dec 24. "
                            f"Need {weight['needed_lb_per_wk']} lb/wk to reach {weight['target_max']:g}."})
    elif st == "on_pace":
        out.append({"level": "ok", "area": "weight",
                    "text": f"Weight on pace: {weight['rate_lb_per_wk']} lb/wk projects {weight['projected_dec24']} by Dec 24."})
    elif st in ("in_range", "below_range"):
        out.append({"level": "ok", "area": "weight", "text": f"Weight {weight['trend_7d']} — inside the target. Hold and recomp."})
    elif st in ("no_data", "need_data"):
        out.append({"level": "info", "area": "weight", "text": "Weigh in fasted most mornings — 3+ weigh-ins unlock the pace projection."})
    low_vt = [g for g in VTAPER if g in train["low_groups"]]
    if low_vt:
        out.append({"level": "warn", "area": "v-taper",
                    "text": "V-taper volume low: " + ", ".join(f"{g} {train['sets'][g]:g}" for g in low_vt)
                            + f" hard sets this week (aim {train['target']['min']}+)."})
    if train["push_sets"] > train["pull_sets"] and train["push_sets"] >= 4:
        out.append({"level": "warn", "area": "balance",
                    "text": f"Push {train['push_sets']} vs pull {train['pull_sets']} sets — keep pull ≥ push to protect the left shoulder."})
    elif train["pull_sets"] and train["pull_sets"] >= train["push_sets"]:
        out.append({"level": "ok", "area": "balance", "text": f"Pull ≥ push ({train['pull_sets']}:{train['push_sets']}) — good for shoulder health."})
    if run["target_mi"]:
        if not run["runs_this_week"]:
            out.append({"level": "warn", "area": "running", "text": f"No runs in the last 7 days — {run['target_mi']:g} mi needs 2–3 easy runs a week."})
        elif run["longest_mi"]:
            out.append({"level": "info", "area": "running",
                        "text": f"Longest run {run['longest_mi']:g} mi of {run['target_mi']:g}; {run['week_mi']:g} mi this week. Add ~10%/week to the long run."})
    gap = train["sessions_target"] - train["sessions_this_week"]
    if gap > 0:
        out.append({"level": "info", "area": "frequency", "text": f"{train['sessions_this_week']}/{train['sessions_target']} sessions in the last 7 days."})
    order = {"warn": 0, "info": 1, "ok": 2}
    return sorted(out, key=lambda i: order[i["level"]])


def overview() -> dict:
    today = _local_now().date()
    days_left = (_d(BIRTHDAY) - today).days
    span = max(1, (_d(BIRTHDAY) - _d(BLOCK_START)).days)
    goals = {g["goal_key"]: g for g in fx.get_goals()}
    weight = _weight_section(goals.get("bodyweight_cut"), days_left)
    rehab = _rehab_section(goals)
    run = _run_section(goals)
    train = _training_section(goals)
    return {
        "date": _local_today(),
        "countdown": {"target_date": BIRTHDAY, "days_left": days_left, "weeks_left": round(days_left / 7, 1),
                      "block_start": BLOCK_START, "block_pct": round(max(0, min(100, (today - _d(BLOCK_START)).days / span * 100)), 1),
                      "week_of_block": max(1, min(span // 7 + 1, (today - _d(BLOCK_START)).days // 7 + 1))},
        "brief": get_brief(),
        "weight": weight, "rehab": rehab, "running": run, "training": train,
        "insights": _insights(weight, rehab, run, train, days_left),
        "goals_on_track": sum(1 for g in goals.values() if g.get("on_track") or g.get("in_range")),
        "goals_total": len(goals),
    }
