"""
fitness.py — exercise library, body measurements and goals (v5.0.1+).

Same rules as database.py (see docs/DATA_INTEGRITY.md): schema changes are additive only
(CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN), deletes are soft, and edits to goals
keep the prior row in goal_history. List-valued columns (muscles, tags, equipment…) are
stored as JSON text so the same SQL runs on Postgres and SQLite.
"""

import json
import re
from datetime import date, datetime, timedelta

from database import (
    get_db,
    _add_column,
    _run_ddl,
    _local_now,
    _local_today,
    _now_ts,
    _valid_date,
    ANALYTIC_SET,
)

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

MUSCLE_GROUPS = ("chest", "back", "shoulders", "arms", "legs", "glutes", "core",
                 "rehab", "mobility", "cardio", "full_body")
CATEGORIES = ("strength", "calisthenics", "rehab", "mobility", "cardio", "plyometric", "conditioning")
REHAB_TARGETS = ("left_shoulder", "thoracic_spine", "lumbar_l4_l5", "deep_core")
REHAB_LABELS = {
    "left_shoulder": "Left shoulder",
    "thoracic_spine": "Thoracic spine",
    "lumbar_l4_l5": "L4-5 lumbar",
    "deep_core": "Deep core",
}

# Individual muscles → the display groups used by the weekly-volume chart.
MUSCLE_TO_GROUP = {
    "chest": "Chest", "serratus": "Chest",
    "lats": "Back", "mid": "Back", "traps": "Back", "thoracic": "Back", "neck": "Back",
    "delts": "Shoulders", "reardelt": "Shoulders", "rotator_cuff": "Shoulders",
    "biceps": "Arms", "triceps": "Arms", "forearms": "Arms",
    "quads": "Legs", "hamstrings": "Legs", "calves": "Legs", "adductors": "Legs", "hip_flexors": "Legs",
    "glutes": "Glutes",
    "abs": "Core", "obliques": "Core", "deep_core": "Core", "lowback": "Core",
}
VOLUME_GROUPS = ("Chest", "Back", "Shoulders", "Arms", "Legs", "Glutes", "Core")

# Fallback for names that aren't in the library: [regex, primary, secondary, muscle_group].
# Mirrors the frontend's MM_RULES (first match wins, specific before generic).
_FALLBACK_RULES = [
    (r"hamstring curl|leg curl|nordic", ["hamstrings"], ["calves"], "legs"),
    (r"calf|calves|tibialis", ["calves"], [], "legs"),
    (r"tricep|pushdown|skull|kickback|\bdips?\b", ["triceps"], ["chest", "delts"], "arms"),
    (r"curl", ["biceps"], ["forearms"], "arms"),
    (r"pull-?down|pull-?up|\bchin|\blats?\b", ["lats"], ["mid", "biceps", "reardelt"], "back"),
    (r"face pull|pull-?apart|rear delt|reverse fly|y-raise", ["reardelt", "mid"], ["traps", "rotator_cuff"], "shoulders"),
    (r"external rotation|internal rotation|rotator", ["rotator_cuff"], ["reardelt"], "rehab"),
    (r"\brow\b|rows", ["lats", "mid"], ["reardelt", "biceps"], "back"),
    (r"shrug", ["traps"], ["forearms"], "back"),
    (r"open book|thread the needle|t-?spine|thoracic", ["thoracic"], ["mid"], "rehab"),
    (r"breath|drawing-?in|tva", ["deep_core"], ["abs"], "rehab"),
    (r"leg swing|hip opener|hip flexor|90/90|couch stretch", ["hip_flexors", "glutes"], ["hamstrings"], "mobility"),
    (r"stretch|strap|cat ?cow|cat-?camel", ["hamstrings"], ["lowback"], "mobility"),
    (r"deadlift|rdl|pull-?through|good morning|hinge|swing|back extension", ["hamstrings", "glutes"], ["lowback", "forearms"], "glutes"),
    (r"hip thrust|glute|bridge|abduct|clamshell|monster walk|band walk", ["glutes"], ["hamstrings"], "glutes"),
    (r"split squat|lunge|step-?up|pistol|y-balance", ["quads", "glutes"], ["hamstrings", "calves"], "legs"),
    (r"squat|leg press|leg extension|wall sit", ["quads", "glutes"], ["hamstrings", "abs"], "legs"),
    (r"overhead|shoulder press|military|half-?kneeling.*press|1-arm press|arnold|landmine press", ["delts"], ["triceps", "abs"], "shoulders"),
    (r"lateral raise|front raise|upright row|arm circle", ["delts"], ["traps"], "shoulders"),
    (r"bench|push-?up|chest press|\bfly|flye|pec", ["chest"], ["delts", "triceps"], "chest"),
    (r"pallof|woodchop|side plank|suitcase|oblique|anti-rot", ["obliques", "abs"], ["deep_core", "glutes"], "core"),
    (r"bird dog|superman", ["lowback", "glutes"], ["deep_core", "delts"], "core"),
    (r"plank|hollow|dead bug|crunch|sit-?up|leg raise|\babs?\b|core|curl-?up", ["abs"], ["obliques", "deep_core"], "core"),
    (r"carry|farmer", ["forearms", "traps"], ["obliques", "abs"], "core"),
    (r"hang|grip", ["forearms"], ["lats"], "mobility"),
    (r"treadmill|walk|jog|\brun|bike|cycl|elliptical|rower|stair|cardio|zone|stride|tempo|interval", ["quads", "calves"], ["hamstrings", "glutes"], "cardio"),
    (r"press", ["chest", "delts"], ["triceps"], "chest"),
]

_JSON_FIELDS = ("aliases", "primary_muscles", "secondary_muscles", "equipment", "tags", "rehab_targets")


def _key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower().replace("&", "and"))


def _name_variants(name: str) -> list:
    """Lookup keys for a name: exact, without parentheticals / '(G15)', and singularized."""
    s = str(name or "").lower()
    out = [_key(s)]
    no_paren = re.sub(r"\([^)]*\)", " ", s)
    out.append(_key(no_paren))
    words = re.findall(r"[a-z0-9]+", no_paren.replace("db ", "dumbbell "))
    out.append("".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words))
    seen, uniq = set(), []
    for k in out:
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _jl(value) -> list:
    """Parse a JSON-list column (or accept a list / comma string)."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        v = json.loads(value)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return [x.strip() for x in str(value).split(",") if x.strip()]


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


# ---------------------------------------------------------------------------
# Schema (additive only)
# ---------------------------------------------------------------------------

def migrate_fitness():
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            name_key TEXT,
            aliases TEXT DEFAULT '[]',
            muscle_group TEXT,
            primary_muscles TEXT DEFAULT '[]',
            secondary_muscles TEXT DEFAULT '[]',
            category TEXT,
            equipment TEXT DEFAULT '[]',
            movement_pattern TEXT,
            difficulty TEXT,
            tags TEXT DEFAULT '[]',
            rehab_targets TEXT DEFAULT '[]',
            schema_type TEXT DEFAULT 'strength_standard',
            default_sets INTEGER DEFAULT 3,
            default_reps TEXT DEFAULT '',
            cues TEXT DEFAULT '',
            caution TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            source TEXT DEFAULT 'seed',
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_exlib_group ON exercise_library(muscle_group)")
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_exlib_key ON exercise_library(name_key)")

    _run_ddl("""
        CREATE TABLE IF NOT EXISTS body_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            waist_in REAL,
            chest_in REAL,
            shoulders_in REAL,
            hips_in REAL,
            neck_in REAL,
            left_arm_in REAL,
            right_arm_in REAL,
            left_thigh_in REAL,
            right_thigh_in REAL,
            left_calf_in REAL,
            right_calf_in REAL,
            body_fat_pct REAL,
            notes TEXT DEFAULT '',
            source TEXT,
            created_at TEXT,
            deleted_at TEXT,
            deleted_reason TEXT
        )
    """)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_measure_date ON body_measurements(date)")

    _run_ddl("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            category TEXT DEFAULT 'other',
            metric TEXT,
            unit TEXT DEFAULT '',
            direction TEXT DEFAULT 'increase',
            start_value REAL,
            current_value REAL,
            target_value REAL,
            target_min REAL,
            target_max REAL,
            start_date TEXT,
            target_date TEXT,
            status TEXT DEFAULT 'active',
            priority INTEGER DEFAULT 5,
            auto_track INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            source TEXT,
            created_at TEXT,
            updated_at TEXT,
            deleted_at TEXT
        )
    """)
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS goal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            change_type TEXT NOT NULL,
            reason TEXT DEFAULT '',
            source TEXT,
            changed_at TEXT NOT NULL,
            snapshot TEXT NOT NULL
        )
    """)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_goal_history_goal ON goal_history(goal_id)")

    # Demo flag on the new tables (Phase 5 wipes demo rows across every table).
    for table in ("body_measurements", "goals"):
        _add_column(table, "is_demo", "INTEGER DEFAULT 0")

    seed_exercise_library()
    seed_goals()

    from training import migrate_training
    migrate_training()
    from coaching import migrate_coaching
    migrate_coaching()


# ---------------------------------------------------------------------------
# Exercise library
# ---------------------------------------------------------------------------

def _library_row(r) -> dict:
    d = dict(r)
    for f in _JSON_FIELDS:
        d[f] = _jl(d.get(f))
    d["is_active"] = bool(d.get("is_active", 1))
    return d


def seed_exercise_library() -> int:
    """Insert seed exercises that aren't in the table yet. Never overwrites an existing row,
    so edits made through the API/MCP survive every redeploy."""
    _invalidate_index()
    try:
        from exercise_library_data import EXERCISES
    except ImportError:
        return 0
    try:
        with get_db() as conn:
            have = {r["name_key"] for r in conn.execute("SELECT name_key FROM exercise_library").fetchall()}
            now = _now_ts()
            added = 0
            for ex in EXERCISES:
                k = _key(ex["name"])
                if k in have:
                    continue
                conn.execute(
                    """INSERT INTO exercise_library
                       (name, name_key, aliases, muscle_group, primary_muscles, secondary_muscles, category,
                        equipment, movement_pattern, difficulty, tags, rehab_targets, schema_type,
                        default_sets, default_reps, cues, caution, video_url, source, is_active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ex["name"], k, json.dumps(ex.get("aliases", [])), ex.get("muscle_group"),
                     json.dumps(ex.get("primary_muscles", [])), json.dumps(ex.get("secondary_muscles", [])),
                     ex.get("category"), json.dumps(ex.get("equipment", [])), ex.get("movement_pattern"),
                     ex.get("difficulty"), json.dumps(ex.get("tags", [])), json.dumps(ex.get("rehab_targets", [])),
                     ex.get("schema_type") or "strength_standard", int(ex.get("default_sets") or 3),
                     str(ex.get("default_reps") or ""), ex.get("cues", ""), ex.get("caution", ""),
                     ex.get("video_url", ""), "seed", 1, now, now),
                )
                have.add(k)
                added += 1
        if added:
            print(f"[fitness] exercise library: seeded {added} exercises")
        _invalidate_index()
        return added
    except Exception as e:
        print(f"[fitness] exercise library seed failed: {e}")
        return 0


_INDEX = None  # (rows_by_id, key -> id)


def _invalidate_index():
    global _INDEX
    _INDEX = None


def _library_index():
    global _INDEX
    if _INDEX is None:
        with get_db() as conn:
            rows = [_library_row(r) for r in conn.execute(
                "SELECT * FROM exercise_library WHERE is_active = 1 ORDER BY id").fetchall()]
        by_id, keys = {}, {}
        # Names claim keys before aliases so an alias can never shadow a real name.
        for r in rows:
            by_id[r["id"]] = r
            for k in _name_variants(r["name"]):
                keys.setdefault(k, r["id"])
        for r in rows:
            for a in r["aliases"]:
                for k in _name_variants(a):
                    keys.setdefault(k, r["id"])
        _INDEX = (by_id, keys)
    return _INDEX


def find_exercise(name: str):
    """Library row for an exercise name or alias (tolerates case, punctuation, plurals, '(G15)')."""
    if not name:
        return None
    by_id, keys = _library_index()
    for k in _name_variants(name):
        if k in keys:
            return by_id[keys[k]]
    return None


def exercise_muscles(name: str) -> dict:
    """{primary, secondary, muscle_group, rehab_targets, matched} for any logged exercise name."""
    row = find_exercise(name)
    if row:
        return {"primary": row["primary_muscles"], "secondary": row["secondary_muscles"],
                "muscle_group": row["muscle_group"], "rehab_targets": row["rehab_targets"],
                "matched": row["name"]}
    low = str(name or "").lower() + " "
    for rx, p, s, g in _FALLBACK_RULES:
        if re.search(rx, low):
            return {"primary": p, "secondary": s, "muscle_group": g, "rehab_targets": [], "matched": None}
    return {"primary": [], "secondary": [], "muscle_group": None, "rehab_targets": [], "matched": None}


def search_exercises(q: str = "", muscle_group: str = None, tag: str = None, category: str = None,
                     equipment: str = None, rehab_target: str = None, muscle: str = None,
                     difficulty: str = None, limit: int = 300, include_inactive: bool = False) -> list:
    """Filter the library. Text search matches name, aliases, muscles, tags and cues; every
    word in q must match somewhere. Filters accept a single value or a comma-separated list."""
    by_id, _ = _library_index()
    rows = list(by_id.values())
    if include_inactive:
        with get_db() as conn:
            rows = [_library_row(r) for r in conn.execute("SELECT * FROM exercise_library ORDER BY id").fetchall()]

    def any_of(value):
        return {v.lower() for v in _as_list(value)}

    mg, tg, cat, eq, rt, mu, df = (any_of(muscle_group), any_of(tag), any_of(category), any_of(equipment),
                                   any_of(rehab_target), any_of(muscle), any_of(difficulty))
    words = [w for w in re.split(r"\s+", str(q or "").lower().strip()) if w]
    out = []
    for r in rows:
        if mg and (r.get("muscle_group") or "").lower() not in mg:
            continue
        if cat and (r.get("category") or "").lower() not in cat:
            continue
        if df and (r.get("difficulty") or "").lower() not in df:
            continue
        if tg and not tg & {t.lower() for t in r["tags"]}:
            continue
        if eq and not eq & {e.lower() for e in r["equipment"]}:
            continue
        if rt and not rt & set(r["rehab_targets"]):
            continue
        if mu and not mu & set(r["primary_muscles"] + r["secondary_muscles"]):
            continue
        if words:
            hay = " ".join([r["name"], " ".join(r["aliases"]), " ".join(r["primary_muscles"]),
                            " ".join(r["secondary_muscles"]), " ".join(r["tags"]), r.get("muscle_group") or "",
                            r.get("category") or "", r.get("movement_pattern") or "", r.get("cues") or ""]).lower()
            # Punctuation-insensitive match ("pullup" ~ "pull-up") per field, never across words.
            field_keys = [_key(x) for x in [r["name"], *r["aliases"], *r["tags"], *r["primary_muscles"],
                                             *r["secondary_muscles"], r.get("muscle_group") or ""]]
            if not all(w in hay or any(_key(w) and _key(w) in fk for fk in field_keys) for w in words):
                continue
        out.append(r)
    # Exact name, then name prefix, then name contains, then rehab work (top priority for
    # this athlete), then A-Z.
    qk = _key(q)
    def rank(r):
        nk, nl = _key(r["name"]), r["name"].lower()
        if not words:
            name_hit = 3
        elif nk == qk or any(_key(a) == qk for a in r["aliases"]):
            name_hit = 0
        elif nk.startswith(qk):
            name_hit = 1
        elif all(w in nl for w in words):
            name_hit = 2
        else:
            name_hit = 3
        return (name_hit, 0 if r["rehab_targets"] else 1, nl)
    out.sort(key=rank)
    return out[: max(1, int(limit or 300))]


def library_facets() -> dict:
    """Counts for building browse UIs: muscle groups, tags, categories, equipment, rehab targets."""
    by_id, _ = _library_index()
    facets = {"muscle_groups": {}, "tags": {}, "categories": {}, "equipment": {}, "rehab_targets": {},
              "total": len(by_id)}
    for r in by_id.values():
        for fk, vals in (("muscle_groups", [r.get("muscle_group")]), ("categories", [r.get("category")]),
                         ("tags", r["tags"]), ("equipment", r["equipment"]), ("rehab_targets", r["rehab_targets"])):
            for v in vals:
                if v:
                    facets[fk][v] = facets[fk].get(v, 0) + 1
    return facets


def get_exercise(ident) -> dict | None:
    """By integer id, or by name/alias."""
    if ident is None or ident == "":
        return None
    if isinstance(ident, int) or str(ident).isdigit():
        with get_db() as conn:
            row = conn.execute("SELECT * FROM exercise_library WHERE id = ?", (int(ident),)).fetchone()
        return _library_row(row) if row else None
    return find_exercise(str(ident))


_EDITABLE_EX = ("aliases", "muscle_group", "primary_muscles", "secondary_muscles", "category", "equipment",
                "movement_pattern", "difficulty", "tags", "rehab_targets", "schema_type", "default_sets",
                "default_reps", "cues", "caution", "video_url", "is_active")


def _ex_values(fields: dict) -> dict:
    vals = {}
    for k in _EDITABLE_EX:
        if k not in fields or fields[k] is None:
            continue
        v = fields[k]
        if k in _JSON_FIELDS:
            v = json.dumps(_as_list(v))
        elif k == "default_sets":
            v = int(v)
        elif k == "is_active":
            v = 1 if (v is True or str(v).lower() in ("1", "true", "yes")) else 0
        vals[k] = v
    if "muscle_group" in vals and vals["muscle_group"] not in MUSCLE_GROUPS:
        raise ValueError(f"muscle_group must be one of {list(MUSCLE_GROUPS)}")
    if "category" in vals and vals["category"] not in CATEGORIES:
        raise ValueError(f"category must be one of {list(CATEGORIES)}")
    if "rehab_targets" in vals:
        bad = [t for t in json.loads(vals["rehab_targets"]) if t not in REHAB_TARGETS]
        if bad:
            raise ValueError(f"rehab_targets must be from {list(REHAB_TARGETS)}")
    return vals


def add_exercise(name: str, source: str = "mcp", **fields) -> dict:
    name = str(name or "").strip()
    if not name:
        return {"error": "name is required"}
    if find_exercise(name) and _key(find_exercise(name)["name"]) == _key(name):
        return {"error": f"'{name}' is already in the library", "exercise": find_exercise(name)}
    try:
        vals = _ex_values(fields)
    except ValueError as e:
        return {"error": str(e)}
    vals.setdefault("muscle_group", exercise_muscles(name)["muscle_group"] or "full_body")
    if "primary_muscles" not in vals:
        guess = exercise_muscles(name)
        vals["primary_muscles"] = json.dumps(guess["primary"])
        vals.setdefault("secondary_muscles", json.dumps(guess["secondary"]))
    now = _now_ts()
    cols = ["name", "name_key", "source", "created_at", "updated_at"] + list(vals)
    params = [name, _key(name), source, now, now] + list(vals.values())
    with get_db() as conn:
        row = conn.execute(
            f"INSERT INTO exercise_library ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) RETURNING id",
            tuple(params),
        ).fetchone()
    _invalidate_index()
    return {"status": "added", "exercise": get_exercise(row["id"])}


def update_exercise(ident, source: str = "mcp", **fields) -> dict:
    ex = get_exercise(ident)
    if not ex:
        return {"error": f"No exercise '{ident}' in the library"}
    try:
        vals = _ex_values(fields)
    except ValueError as e:
        return {"error": str(e)}
    if not vals:
        return {"error": "nothing to update"}
    vals["updated_at"] = _now_ts()
    sets = ", ".join(f"{k} = ?" for k in vals)
    with get_db() as conn:
        conn.execute(f"UPDATE exercise_library SET {sets} WHERE id = ?", tuple(vals.values()) + (ex["id"],))
    _invalidate_index()
    return {"status": "updated", "exercise": get_exercise(ex["id"])}


# ---------------------------------------------------------------------------
# Body measurements (inches)
# ---------------------------------------------------------------------------

MEASUREMENT_FIELDS = ("waist_in", "chest_in", "shoulders_in", "hips_in", "neck_in", "left_arm_in", "right_arm_in",
                      "left_thigh_in", "right_thigh_in", "left_calf_in", "right_calf_in", "body_fat_pct")
# Accept short names from the UI / Claude ("waist", "left_arm") as well as the column names.
_MEASURE_ALIASES = {f[:-3]: f for f in MEASUREMENT_FIELDS if f.endswith("_in")}
_MEASURE_ALIASES.update({"bf": "body_fat_pct", "body_fat": "body_fat_pct", "bodyfat": "body_fat_pct"})


def _measure_values(fields: dict) -> dict:
    vals = {}
    for k, v in (fields or {}).items():
        col = k if k in MEASUREMENT_FIELDS else _MEASURE_ALIASES.get(k)
        if not col or v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number")
        if f <= 0 or f > (60 if col == "body_fat_pct" else 90):
            raise ValueError(f"{k}={v} is out of range")
        vals[col] = round(f, 2)
    return vals


def _measure_row(r) -> dict:
    d = dict(r)
    d["is_demo"] = bool(d.get("is_demo"))
    sh, w = d.get("shoulders_in"), d.get("waist_in")
    d["shoulder_waist_ratio"] = round(sh / w, 3) if sh and w else None
    return d


def log_measurements(dt: str = None, notes: str = "", source: str = "app", is_demo: bool = None, **fields) -> dict:
    dt = dt or _local_today()
    if not _valid_date(dt):
        return {"error": f"date must be YYYY-MM-DD, got {dt!r}"}
    try:
        vals = _measure_values(fields)
    except ValueError as e:
        return {"error": str(e)}
    if not vals:
        return {"error": f"give at least one measurement: {list(_MEASURE_ALIASES)}"}
    if is_demo is None:
        from database import demo_flag
        is_demo = demo_flag()
    cols = ["date", "notes", "source", "created_at", "is_demo"] + list(vals)
    params = [dt, notes or "", source, _now_ts(), 1 if is_demo else 0] + list(vals.values())
    with get_db() as conn:
        row = conn.execute(
            f"INSERT INTO body_measurements ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) RETURNING id",
            tuple(params),
        ).fetchone()
        new = conn.execute("SELECT * FROM body_measurements WHERE id = ?", (row["id"],)).fetchone()
    return {"status": "logged", "entry": _measure_row(new)}


def get_measurements(limit: int = 60, include_deleted: bool = False, include_demo: bool = True) -> list:
    where = ["1 = 1"]
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if not include_demo:
        where.append("(is_demo IS NULL OR is_demo = 0)")
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM body_measurements WHERE {' AND '.join(where)} ORDER BY date DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [_measure_row(r) for r in rows]


def update_measurement(mid: int, source: str = "correction", **fields) -> dict:
    try:
        vals = _measure_values(fields)
    except ValueError as e:
        return {"error": str(e)}
    if "notes" in fields and fields["notes"] is not None:
        vals["notes"] = fields["notes"]
    if fields.get("date"):
        if not _valid_date(fields["date"]):
            return {"error": "date must be YYYY-MM-DD"}
        vals["date"] = fields["date"]
    if not vals:
        return {"error": "nothing to update"}
    with get_db() as conn:
        old = conn.execute("SELECT * FROM body_measurements WHERE id = ?", (int(mid),)).fetchone()
        if not old:
            return {"error": f"No measurement with id {mid}"}
        # Keep the prior values in the notes trail rather than losing them.
        prior = {k: dict(old).get(k) for k in vals if k != "notes"}
        trail = f"[{_now_ts()} {source}] was {json.dumps(prior)}"
        vals["notes"] = (vals.get("notes", dict(old).get("notes") or "") + "\n" + trail).strip()
        conn.execute(f"UPDATE body_measurements SET {', '.join(f'{k} = ?' for k in vals)} WHERE id = ?",
                     tuple(vals.values()) + (int(mid),))
        new = conn.execute("SELECT * FROM body_measurements WHERE id = ?", (int(mid),)).fetchone()
    return {"status": "updated", "entry": _measure_row(new)}


def delete_measurement(mid: int, reason: str = "") -> dict:
    with get_db() as conn:
        n = conn.execute("UPDATE body_measurements SET deleted_at = ?, deleted_reason = ? WHERE id = ? AND deleted_at IS NULL",
                         (_now_ts(), reason or "", int(mid))).rowcount
    return {"status": "deleted", "id": mid} if n else {"error": f"No live measurement with id {mid}"}


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

BIRTHDAY = None  # Set from athlete profile during onboarding

# Auto-tracked metrics (computed from the record each time goals are read).
GOAL_METRICS = {
    "bodyweight_lbs": "7-day average of on-protocol weigh-ins",
    "waist_in": "latest waist measurement",
    "shoulder_waist_ratio": "latest shoulders ÷ waist",
    "sessions_per_week": "real (non-demo) sessions in the last 7 days",
    "rehab_days_per_week": "days in the last 7 with ≥1 set of this goal's rehab target",
    "longest_run_mi": "longest single treadmill/run set (mph × minutes) in the last 90 days",
    "weekly_run_mi": "treadmill/run distance in the last 7 days",
}

SEED_GOALS = []  # Goals are seeded during onboarding based on athlete selections

_GOAL_FIELDS = ("title", "category", "metric", "unit", "direction", "start_value", "current_value", "target_value",
                "target_min", "target_max", "start_date", "target_date", "status", "priority", "auto_track", "notes")
GOAL_STATUSES = ("active", "achieved", "paused", "abandoned")


def seed_goals() -> int:
    """Insert seed goals whose goal_key isn't present. Existing goals are never touched."""
    try:
        with get_db() as conn:
            have = {r["goal_key"] for r in conn.execute("SELECT goal_key FROM goals").fetchall()}
            now = _now_ts()
            added = 0
            for g in SEED_GOALS:
                if g["goal_key"] in have:
                    continue
                vals = {k: g.get(k) for k in _GOAL_FIELDS if g.get(k) is not None}
                vals.setdefault("start_date", _local_today())
                vals.setdefault("auto_track", 1)
                cols = ["goal_key", "source", "created_at", "updated_at"] + list(vals)
                conn.execute(
                    f"INSERT INTO goals ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    tuple([g["goal_key"], "seed", now, now] + list(vals.values())),
                )
                added += 1
        return added
    except Exception as e:
        print(f"[fitness] goal seed failed: {e}")
        return 0


def _goal_snapshot(conn, row: dict, change_type: str, reason: str = "", source: str = None):
    conn.execute(
        "INSERT INTO goal_history (goal_id, change_type, reason, source, changed_at, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], change_type, reason or "", source, _now_ts(), json.dumps(row, default=str)),
    )


def _goal_vals(fields: dict) -> dict:
    vals = {}
    for k in _GOAL_FIELDS:
        if k not in fields or fields[k] is None:
            continue
        v = fields[k]
        if k in ("start_value", "current_value", "target_value", "target_min", "target_max"):
            v = None if v == "" else float(v)
        elif k in ("priority",):
            v = int(v)
        elif k == "auto_track":
            v = 1 if (v is True or str(v).lower() in ("1", "true", "yes")) else 0
        elif k in ("start_date", "target_date") and v and not _valid_date(v):
            raise ValueError(f"{k} must be YYYY-MM-DD")
        vals[k] = v
    if vals.get("direction") and vals["direction"] not in ("increase", "decrease", "maintain"):
        raise ValueError("direction must be increase, decrease or maintain")
    if vals.get("status") and vals["status"] not in GOAL_STATUSES:
        raise ValueError(f"status must be one of {list(GOAL_STATUSES)}")
    return vals


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")[:60] or "goal"


def set_goal(goal_key: str = None, title: str = None, reason: str = "", source: str = "mcp",
             is_demo: bool = None, **fields) -> dict:
    """Create a goal, or update the goal with this goal_key (prior row kept in goal_history).
    During a demo, new goals are tagged is_demo and edits to real goals are recorded as
    'demo_update' so ending the demo puts them back exactly as they were."""
    from database import demo_active
    in_demo = demo_active() if is_demo is None else bool(is_demo)
    try:
        vals = _goal_vals(fields)
    except (ValueError, TypeError) as e:
        return {"error": str(e)}
    if title:
        vals["title"] = title
    key = goal_key or _slug(title)
    now = _now_ts()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM goals WHERE goal_key = ?", (key,)).fetchone()
        if row:
            row = dict(row)
            if not vals:
                return {"error": "nothing to update"}
            _goal_snapshot(conn, row, "demo_update" if in_demo and not row.get("is_demo") else "update", reason, source)
            vals["updated_at"] = now
            if row.get("deleted_at"):
                vals["deleted_at"] = None  # setting a deleted goal again brings it back
            conn.execute(f"UPDATE goals SET {', '.join(f'{k} = ?' for k in vals)} WHERE id = ?",
                         tuple(vals.values()) + (row["id"],))
            gid, status = row["id"], "updated"
        else:
            if not vals.get("title"):
                return {"error": "title is required for a new goal"}
            vals.setdefault("start_date", _local_today())
            vals.setdefault("auto_track", 1 if vals.get("metric") in GOAL_METRICS else 0)
            cols = ["goal_key", "source", "created_at", "updated_at", "is_demo"] + list(vals)
            r = conn.execute(
                f"INSERT INTO goals ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) RETURNING id",
                tuple([key, source, now, now, 1 if in_demo else 0] + list(vals.values())),
            ).fetchone()
            gid, status = r["id"], "created"
    goal = next((g for g in get_goals(include_inactive=True) if g["id"] == gid), None)
    return {"status": status, "goal": goal}


def delete_goal(goal_key: str, reason: str = "", source: str = "mcp") -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM goals WHERE goal_key = ? AND deleted_at IS NULL", (goal_key,)).fetchone()
        if not row:
            return {"error": f"No live goal '{goal_key}'"}
        from database import demo_active
        _goal_snapshot(conn, dict(row), "demo_update" if demo_active() and not row["is_demo"] else "delete", reason, source)
        conn.execute("UPDATE goals SET deleted_at = ? WHERE id = ?", (_now_ts(), row["id"]))
    return {"status": "deleted", "goal_key": goal_key}


def revert_demo_goal_edits() -> int:
    """Put every real goal edited during a demo back to its pre-demo row (the earliest
    'demo_update' snapshot), then mark those history rows reverted. Returns goals restored."""
    restored = 0
    with get_db() as conn:
        hist = conn.execute("SELECT * FROM goal_history WHERE change_type = 'demo_update' ORDER BY id").fetchall()
        first = {}
        for h in hist:
            first.setdefault(h["goal_id"], json.loads(h["snapshot"]))
        cols = _GOAL_FIELDS + ("updated_at", "deleted_at")
        for gid, snap in first.items():
            conn.execute(f"UPDATE goals SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ? AND (is_demo IS NULL OR is_demo = 0)",
                         tuple(snap.get(c) for c in cols) + (gid,))
            restored += 1
        conn.execute("UPDATE goal_history SET change_type = 'demo_update_reverted' WHERE change_type = 'demo_update'")
    return restored


def get_goal_history(goal_key: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM goals WHERE goal_key = ?", (goal_key,)).fetchone()
        if not row:
            return {"error": f"No goal '{goal_key}'"}
        hist = conn.execute("SELECT * FROM goal_history WHERE goal_id = ? ORDER BY id DESC", (row["id"],)).fetchall()
    return {"goal": dict(row), "history": [dict(h) | {"snapshot": json.loads(h["snapshot"])} for h in hist]}


# ── metric computation ──────────────────────────────────────────────────────

def _days_ago(n: int) -> str:
    return (_local_now().date() - timedelta(days=n)).isoformat()


def _real_session_filter(alias: str = "s") -> str:
    """Live, non-demo sessions (demo ids start with 'demo_'; is_demo column added in Phase 5)."""
    return (f"{alias}.deleted_at IS NULL AND substr({alias}.session_id, 1, 5) != 'demo_'")


def recent_sets(days: int = 7, end: str = None) -> list:
    """Analytic sets (live, not superseded) from real sessions dated within [end-days+1, end]."""
    end = end or _local_today()
    start = (datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=days - 1)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT ws.id, ws.exercise, ws.weight, ws.reps, ws.notes, s.date, s.session_id, s.program
                FROM workout_sets ws JOIN sessions s ON s.session_id = ws.session_id
                WHERE s.date >= ? AND s.date <= ? AND {_real_session_filter('s')} AND {ANALYTIC_SET}
                ORDER BY s.date, ws.id""",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


_MPH = re.compile(r"([\d.]+)\s*mph", re.I)
_MIN = re.compile(r"([\d.]+)\s*min", re.I)
_MILES = re.compile(r"([\d.]+)\s*(mi|mile|miles)\b", re.I)


def set_distance_mi(s: dict):
    """Distance of a run/walk set: explicit 'x mi', else mph × minutes. None if not a distance set."""
    text = " ".join(str(s.get(k) or "") for k in ("weight", "reps", "notes"))
    m = _MILES.search(text)
    if m:
        return float(m.group(1))
    sp, mn = _MPH.search(text), _MIN.search(text)
    if sp and mn:
        return round(float(sp.group(1)) * float(mn.group(1)) / 60, 2)
    return None


def _is_run(s: dict) -> bool:
    name = str(s.get("exercise") or "").lower()
    return bool(re.search(r"run|jog|treadmill|tempo|stride|interval", name))


def bodyweight_trend(days: int = 7):
    """Average of on-protocol weigh-ins in the last `days` (falls back to the latest entry)."""
    from database import get_bodyweight_history
    entries = get_bodyweight_history(120, include_off_protocol=False)
    if not entries:
        return None, None
    cutoff = _days_ago(days - 1)
    window = [e["weight_lbs"] for e in entries if e["date"] >= cutoff]
    if window:
        return round(sum(window) / len(window), 1), entries[0]["date"]
    return float(entries[0]["weight_lbs"]), entries[0]["date"]


def compute_metric(metric: str, goal_key: str = "", _cache: dict = None):
    """Current value of an auto-tracked metric, or None when there's no data yet."""
    c = _cache if _cache is not None else {}
    if metric == "bodyweight_lbs":
        return bodyweight_trend(7)[0]
    if metric in ("waist_in", "shoulder_waist_ratio"):
        if "measure" not in c:
            c["measure"] = get_measurements(60)
        for m in c["measure"]:  # newest first
            v = m.get(metric)
            if v:
                return v
        return None
    if metric == "sessions_per_week":
        with get_db() as conn:
            r = conn.execute(
                f"SELECT COUNT(DISTINCT s.session_id) AS n FROM sessions s WHERE s.date >= ? AND {_real_session_filter('s')} "
                f"AND EXISTS (SELECT 1 FROM workout_sets ws WHERE ws.session_id = s.session_id AND {ANALYTIC_SET})",
                (_days_ago(6),),
            ).fetchone()
        return float(r["n"] or 0)
    if metric == "rehab_days_per_week":
        target = goal_key.replace("rehab_", "", 1)
        if "sets7" not in c:
            c["sets7"] = recent_sets(7)
        days = {s["date"] for s in c["sets7"] if target in exercise_muscles(s["exercise"])["rehab_targets"]}
        return float(len(days))
    if metric in ("longest_run_mi", "weekly_run_mi"):
        key = "sets90" if metric == "longest_run_mi" else "sets7"
        if key not in c:
            c[key] = recent_sets(90 if key == "sets90" else 7)
        dists = [d for d in (set_distance_mi(s) for s in c[key] if _is_run(s)) if d]
        if not dists:
            return 0.0 if metric == "weekly_run_mi" else None
        return round(max(dists) if metric == "longest_run_mi" else sum(dists), 2)
    return None


def goal_progress(g: dict, cache: dict = None) -> dict:
    """Adds current, pct (0-100 toward target), in_range, expected_pct (linear pace), on_track,
    days_left. Manual current_value is used when the goal isn't auto-tracked or has no data."""
    g = dict(g)
    auto = compute_metric(g.get("metric"), g.get("goal_key", ""), cache) if g.get("auto_track") else None
    current = auto if auto is not None else g.get("current_value")
    start, target = g.get("start_value"), g.get("target_value")
    direction = g.get("direction") or "increase"
    # A goal seeded without a baseline (waist, ratio) takes its first real reading as the start.
    if start is None and current is not None:
        start = current
    pct = None
    if current is not None and target is not None:
        if direction == "maintain":
            pct = 100.0 if abs(current - target) <= max(0.5, abs(target) * 0.01) else 0.0
        elif start is not None and start != target:
            pct = (current - start) / (target - start) * 100
        else:
            pct = 100.0 if ((current >= target) if direction == "increase" else (current <= target)) else 0.0
        pct = round(max(0.0, min(100.0, pct)), 1)
    in_range = None
    if current is not None and g.get("target_min") is not None and g.get("target_max") is not None:
        in_range = g["target_min"] <= current <= g["target_max"]
    days_left = expected = None
    try:
        sd = datetime.strptime(g.get("start_date") or _local_today(), "%Y-%m-%d").date()
        td = datetime.strptime(g["target_date"], "%Y-%m-%d").date()
        today = _local_now().date()
        days_left = (td - today).days
        span = max(1, (td - sd).days)
        expected = round(max(0.0, min(100.0, (today - sd).days / span * 100)), 1)
    except (KeyError, TypeError, ValueError):
        pass
    # Weekly-rate metrics (rehab days, sessions/week) are "on track" when this week hits target.
    rate_metric = g.get("metric") in ("rehab_days_per_week", "sessions_per_week", "weekly_run_mi")
    on_track = None
    if pct is not None:
        # Moving the wrong way from the start is never "on track", however early it is.
        wrong_way = (start is not None and current is not None and
                     ((direction == "decrease" and current > start) or (direction == "increase" and current < start)))
        on_track = pct >= 100 if rate_metric else (bool(in_range) or (not wrong_way and pct >= (expected or 0) - 10))
    g.update({"current": current, "start_effective": start, "pct": pct, "in_range": in_range,
              "expected_pct": None if rate_metric else expected, "on_track": on_track, "days_left": days_left,
              "auto_value": auto, "metric_description": GOAL_METRICS.get(g.get("metric"), "manual"),
              "auto_track": bool(g.get("auto_track")), "is_demo": bool(g.get("is_demo"))})
    if g.get("target_value") is None and g.get("goal_key") == "waist_down" and current is not None:
        g["suggested_target"] = round(current - 2, 1)
    return g


def get_goals(include_inactive: bool = False, include_deleted: bool = False, category: str = None) -> list:
    where = ["1 = 1"]
    params = []
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if not include_inactive:
        where.append("status = 'active'")
    if category:
        where.append("category = ?")
        params.append(category)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM goals WHERE {' AND '.join(where)} ORDER BY priority, id", tuple(params)
        ).fetchall()
    cache = {}
    return [goal_progress(dict(r), cache) for r in rows]


# ---------------------------------------------------------------------------
# Training volume by muscle group (v5.0.2)
# ---------------------------------------------------------------------------

# Hypertrophy landmarks used for the "this week" bars (hard sets per muscle group / week).
WEEKLY_SET_TARGET = {"min": 10, "max": 20}
_NON_HYPERTROPHY = ("cardio", "mobility")
_W = re.compile(r"-?\d+(\.\d+)?")


def _set_load(s: dict) -> float:
    """weight × reps in lb for a loaded set; 0 for bodyweight / timed / cardio sets."""
    w, r = str(s.get("weight") or ""), str(s.get("reps") or "")
    if re.search(r"mph|body|^bw$|test|band", w, re.I) or re.search(r"\d\s*(s|sec|min)\b", r, re.I):
        return 0.0
    mw, mr = _W.search(w), _W.search(r.split("/")[0])
    if not mw or not mr:
        return 0.0
    return max(0.0, float(mw.group(0)) * float(mr.group(0)))


def classify_set(exercise: str) -> dict:
    """How one logged set counts toward weekly volume:
    {groups: {display_group: weight}, rehab: bool, kind}. Primary muscle groups get 1 set,
    secondary-only groups 0.5. Cardio/mobility sets don't count as hypertrophy volume;
    rehab-category work is tracked in its own 'Rehab' series."""
    m = exercise_muscles(exercise)
    row = find_exercise(exercise)
    category = (row or {}).get("category")
    group = m.get("muscle_group")
    rehab = category == "rehab" or group == "rehab"
    if rehab:
        return {"groups": {}, "rehab": True, "kind": "rehab"}
    if group in _NON_HYPERTROPHY or category in _NON_HYPERTROPHY or not m["primary"]:
        return {"groups": {}, "rehab": False, "kind": group or "other"}
    out = {}
    for mu in m["primary"]:
        g = MUSCLE_TO_GROUP.get(mu)
        if g:
            out[g] = 1.0
    for mu in m["secondary"]:
        g = MUSCLE_TO_GROUP.get(mu)
        if g and g not in out:
            out[g] = 0.5
    return {"groups": out, "rehab": False, "kind": "strength"}


def muscle_volume(weeks: int = 8) -> dict:
    """Rolling 7-day windows ending today (newest last): hard sets and load (lb) per muscle
    group, plus a Rehab series. Demo sessions and superseded/deleted sets never count."""
    weeks = max(1, min(int(weeks or 8), 26))
    today = _local_now().date()
    sets = recent_sets(weeks * 7)
    cls_cache = {}
    windows = []
    for i in range(weeks - 1, -1, -1):
        end = today - timedelta(days=7 * i)
        start = end - timedelta(days=6)
        windows.append({"start": start.isoformat(), "end": end.isoformat(),
                        "sets": {g: 0.0 for g in VOLUME_GROUPS}, "load": {g: 0.0 for g in VOLUME_GROUPS},
                        "rehab_sets": 0, "total_sets": 0, "sessions": set()})
    by_ex_this_week = {}
    for s in sets:
        w = next((w for w in windows if w["start"] <= s["date"] <= w["end"]), None)
        if not w:
            continue
        c = cls_cache.get(s["exercise"])
        if c is None:
            c = cls_cache[s["exercise"]] = classify_set(s["exercise"])
        w["sessions"].add(s["session_id"])
        w["total_sets"] += 1
        if c["rehab"]:
            w["rehab_sets"] += 1
        load = _set_load(s)
        for g, wt in c["groups"].items():
            w["sets"][g] += wt
            w["load"][g] += load * wt
        if w is windows[-1]:
            e = by_ex_this_week.setdefault(s["exercise"], {"exercise": s["exercise"], "sets": 0, "groups": c["groups"],
                                                             "rehab": c["rehab"]})
            e["sets"] += 1
    for w in windows:
        w["sessions"] = len(w["sessions"])
        w["sets"] = {g: round(v, 1) for g, v in w["sets"].items()}
        w["load"] = {g: round(v) for g, v in w["load"].items()}
    cur = windows[-1]
    status = {}
    for g in VOLUME_GROUPS:
        v = cur["sets"][g]
        status[g] = "low" if v < WEEKLY_SET_TARGET["min"] else "high" if v > WEEKLY_SET_TARGET["max"] else "ok"
    return {"weeks": windows, "groups": list(VOLUME_GROUPS), "target": WEEKLY_SET_TARGET,
            "this_week": {"sets": cur["sets"], "load": cur["load"], "rehab_sets": cur["rehab_sets"],
                          "status": status, "exercises": sorted(by_ex_this_week.values(), key=lambda e: -e["sets"])}}


def sync_signature() -> dict:
    """Cheap change-detector for the dashboard poll: tables Claude can write via MCP that
    aren't part of /api/session/current."""
    out = {}
    queries = {
        "measurements": "SELECT COUNT(*) AS n, MAX(id) AS m FROM body_measurements WHERE deleted_at IS NULL",
        "goals": "SELECT COUNT(*) AS n, MAX(updated_at) AS m FROM goals WHERE deleted_at IS NULL",
        "photos": "SELECT COUNT(*) AS n, MAX(id) AS m FROM progress_photos",
        "brief": "SELECT COUNT(*) AS n, MAX(id) AS m FROM coach_briefs",
    }
    for k, sql in queries.items():
        try:
            with get_db() as conn:
                r = conn.execute(sql).fetchone()
            out[k] = f"{r['n']}:{r['m']}"
        except Exception:
            out[k] = "na"
    return out
