"""
database.py — Persistence for JARVIS workout data.

Uses PostgreSQL when DATABASE_URL is set (Railway), falls back to SQLite for local dev.
"""

import json
import os
from pathlib import Path
from datetime import datetime, date, timedelta
from contextlib import contextmanager

import zoneinfo

LOCAL_TZ = zoneinfo.ZoneInfo("America/New_York")

def _local_now():
    return datetime.now(LOCAL_TZ)

def _local_today():
    return _local_now().date().isoformat()

def _get_athlete_name() -> str:
    """Get the athlete's name from their profile, or 'Athlete' as fallback."""
    try:
        from mcp_server import _load, DEFAULT_PROFILE
        profile = _load("profile.json", DEFAULT_PROFILE)
        name = profile.get("name", "Athlete")
        return name if name and name != "New Athlete" else "Athlete"
    except Exception:
        return "Athlete"


DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

# SQLite fallback path
_data_dir = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", str(Path(__file__).resolve().parent)))
DB_PATH = _data_dir / "jarvis_workout.db"


class DictRow(dict):
    """Make dict rows subscriptable like sqlite3.Row."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _pg_conn():
    """Get a PostgreSQL connection with dict cursor."""
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def _pg_dict_cursor(conn):
    """Get a dict cursor for PostgreSQL."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


@contextmanager
def get_db():
    """Context manager for database connections. Works with both PG and SQLite."""
    if USE_PG:
        conn = _pg_conn()
        try:
            yield PGConnectionWrapper(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class PGConnectionWrapper:
    """Wraps psycopg2 connection to match sqlite3 interface (execute, executescript, fetchone, fetchall)."""
    
    def __init__(self, conn):
        self._conn = conn
    
    def close(self):
        self._conn.commit()
        self._conn.close()
    
    def execute(self, sql, params=None):
        """Execute SQL, translating SQLite syntax to PostgreSQL."""
        sql = _translate_sql(sql)
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return PGCursorWrapper(cur)
    
    def executescript(self, sql):
        """Execute multiple SQL statements."""
        sql = _translate_sql(sql)
        cur = self._conn.cursor()
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.upper().startswith("SELECT 1"):
                try:
                    cur.execute(stmt)
                    self._conn.commit()
                except Exception as e:
                    self._conn.rollback()
                    if "already exists" not in str(e).lower():
                        print(f"[DB] Script stmt error (skipping): {e}")
                        print(f"[DB] Statement was: {stmt[:100]}")
        return PGCursorWrapper(cur)
    
    def commit(self):
        self._conn.commit()
    
    def rollback(self):
        self._conn.rollback()


class PGCursorWrapper:
    """Wraps psycopg2 cursor to match sqlite3 cursor interface."""
    
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None
        self.rowcount = cur.rowcount if cur.rowcount >= 0 else 0
    
    def fetchone(self):
        try:
            row = self._cur.fetchone()
            return _plain_row(row) if row else None
        except psycopg2.ProgrammingError:
            return None

    def fetchall(self):
        try:
            rows = self._cur.fetchall()
            return [_plain_row(r) for r in rows]
        except psycopg2.ProgrammingError:
            return []


def _plain_row(row) -> dict:
    """Postgres returns datetime/Decimal for TIMESTAMP/NUMERIC columns; SQLite returns
    strings/floats. Normalize so callers (and json.dumps in the MCP layer) see one shape."""
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif v.__class__.__name__ == "Decimal":
            v = float(v)
        out[k] = v
    return out


def _translate_sql(sql):
    """Translate SQLite SQL to PostgreSQL."""
    if not USE_PG:
        return sql
    # AUTOINCREMENT -> SERIAL (handled by table creation)
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    # datetime('now') -> NOW()
    sql = sql.replace("datetime('now')", "NOW()")
    sql = sql.replace("date('now')", "CURRENT_DATE")
    # PRAGMA statements -> no-op
    if sql.strip().upper().startswith("PRAGMA"):
        return "SELECT 1"
    # last_insert_rowid() -> lastval()
    sql = sql.replace("last_insert_rowid()", "lastval()")
    # Boolean handling
    sql = sql.replace("is_backfill = 1", "is_backfill = TRUE")
    # BLOB -> BYTEA
    sql = sql.replace("BLOB", "BYTEA")
    # ? placeholders -> %s for psycopg2
    sql = sql.replace("?", "%s")
    return sql


# ---------------------------------------------------------------------------
# Data integrity — nothing is destroyed. Deletes are soft (deleted_at), edits are
# versioned (set_history / session_history), corrections can supersede a set, and
# every row records where it came from (source).
# ---------------------------------------------------------------------------

SOURCES = ("app", "mcp", "backfill", "correction")

# SQL predicates. "Live" rows are not soft-deleted. "Analytic" sets are live AND not
# superseded by a live correction — those are the only sets counted in volume/progress.
LIVE_SET = "ws.deleted_at IS NULL"
ANALYTIC_SET = (
    "ws.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM workout_sets sup "
    "WHERE sup.supersedes_set_id = ws.id AND sup.deleted_at IS NULL)"
)
# Backfill sessions never take part in "which session is live right now" logic.
NOT_BACKFILL = "(is_backfill IS NULL OR CAST(is_backfill AS INTEGER) = 0)"

_SESSION_DELETE_PREFIX = "[session deleted] "


def _now_ts() -> str:
    """Local wall-clock time without offset — fits a Postgres TIMESTAMP column."""
    return _local_now().replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_ts(value):
    """Parse a stored timestamp string; naive values are treated as local time."""
    if not value:
        return None
    try:
        from dateutil.parser import parse as _p
        dt = _p(str(value))
    except Exception:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _duration_min(started_at, ended_at):
    s, e = _parse_ts(started_at), _parse_ts(ended_at)
    if not s or not e:
        return None
    return round((e - s).total_seconds() / 60, 1)


def _time_of_day(ts):
    dt = _parse_ts(ts)
    if not dt:
        return None
    h = dt.hour
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening"


def _load_json(value):
    """JSONB comes back from psycopg2 as a dict; SQLite TEXT comes back as a string."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _column_exists(conn, table: str, column: str) -> bool:
    if USE_PG:
        row = conn.execute(
            "SELECT 1 AS ok FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return bool(row)
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _column_type(table: str, column: str) -> str:
    """Lower-cased declared type of a column ('' if unknown)."""
    try:
        with get_db() as conn:
            if USE_PG:
                row = conn.execute(
                    "SELECT data_type FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                    (table, column),
                ).fetchone()
                return (row or {}).get("data_type", "").lower()
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall():
                if r[1] == column:
                    return (r[2] or "").lower()
    except Exception:
        pass
    return ""


_backfill_is_bool = None


def _backfill_flag(flag: bool):
    """sessions.is_backfill was created as INTEGER, but tolerate a BOOLEAN column too."""
    global _backfill_is_bool
    if _backfill_is_bool is None:
        _backfill_is_bool = _column_type("sessions", "is_backfill") == "boolean"
    return bool(flag) if _backfill_is_bool else int(bool(flag))


def _add_column(table: str, column: str, ddl_type: str):
    """ALTER TABLE ADD COLUMN, idempotent, in its own transaction so one failure
    (e.g. a table that doesn't exist yet) can't roll back the others."""
    try:
        with get_db() as conn:
            if _column_exists(conn, table, column):
                return  # don't take an ALTER TABLE lock on every boot
            if USE_PG:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}")
            else:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    except Exception as e:
        print(f"[database] add column {table}.{column} failed: {e}")


def _run_ddl(sql: str):
    try:
        with get_db() as conn:
            conn.execute(sql)
    except Exception as e:
        print(f"[database] DDL failed: {e} :: {sql.strip()[:80]}")


_PLANNED_WORKOUTS_DDL = """
    CREATE TABLE IF NOT EXISTS planned_workouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        planned_date TEXT NOT NULL,
        program_name TEXT NOT NULL,
        workout_data TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        actual_session_id TEXT,
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_SET_DEVIATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS set_deviations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        exercise TEXT NOT NULL,
        set_number INTEGER DEFAULT 1,
        planned_weight TEXT,
        planned_reps TEXT,
        planned_sets INTEGER DEFAULT 0,
        planned_notes TEXT DEFAULT '',
        actual_weight TEXT,
        actual_reps TEXT,
        actual_sets INTEGER DEFAULT 0,
        actual_notes TEXT DEFAULT '',
        deviation_type TEXT DEFAULT 'other',
        reason_code TEXT DEFAULT 'other',
        attribution TEXT DEFAULT 'athlete_initiated',
        detail TEXT DEFAULT '',
        weight_variance TEXT DEFAULT '',
        reps_variance TEXT DEFAULT '',
        set_variance INTEGER DEFAULT 0,
        planned_weight_left TEXT DEFAULT '',
        planned_weight_right TEXT DEFAULT '',
        actual_weight_left TEXT DEFAULT '',
        actual_weight_right TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        timestamp TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""


def _migrate_integrity():
    """Additive, idempotent schema changes. Only CREATE TABLE IF NOT EXISTS and
    ALTER TABLE ADD COLUMN — never DROP, never recreate, never rewrite existing values."""
    json_type = "JSONB" if USE_PG else "TEXT"

    # Soft deletes + provenance + corrections on sets
    _add_column("workout_sets", "deleted_at", "TIMESTAMP NULL")
    _add_column("workout_sets", "deleted_reason", "TEXT")
    _add_column("workout_sets", "supersedes_set_id", "INTEGER")
    _add_column("workout_sets", "source", "TEXT")
    _add_column("workout_sets", "performed_at", "TEXT")

    # Soft deletes + provenance + frozen plan on sessions
    _add_column("sessions", "deleted_at", "TIMESTAMP NULL")
    _add_column("sessions", "deleted_reason", "TEXT")
    _add_column("sessions", "source", "TEXT")
    _add_column("sessions", "planned_workout_snapshot", json_type)

    # Demo flag (v5.0.5): every row written while a demo is running is tagged and wiped on end.
    for _t in ("sessions", "workout_sets", "bodyweight", "exercise_modifications", "set_deviations", "progress_photos"):
        _add_column(_t, "is_demo", "INTEGER DEFAULT 0")

    # Bodyweight protocol metadata
    _add_column("bodyweight", "fasted", "BOOLEAN")
    _add_column("bodyweight", "time_of_day", "TEXT")
    _add_column("bodyweight", "off_protocol", "BOOLEAN DEFAULT FALSE")

    # Plans are superseded, never deleted
    _run_ddl(_PLANNED_WORKOUTS_DDL)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_planned_date ON planned_workouts(planned_date)")
    _add_column("planned_workouts", "superseded_at", "TEXT")

    # Deviations link to the set that produced them
    _run_ddl(_SET_DEVIATIONS_DDL)
    _add_column("set_deviations", "set_id", "INTEGER")
    _add_column("set_deviations", "source", "TEXT")
    _add_column("set_deviations", "superseded_at", "TEXT")

    # Version history
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS set_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            change_type TEXT NOT NULL,
            reason TEXT DEFAULT '',
            source TEXT,
            changed_at TEXT NOT NULL,
            exercise TEXT,
            weight TEXT,
            reps TEXT,
            rpe TEXT,
            notes TEXT,
            performed_at TEXT,
            snapshot TEXT NOT NULL
        )
    """)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_set_history_set ON set_history(set_id)")
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS session_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_row_id INTEGER NOT NULL,
            session_id TEXT,
            change_type TEXT NOT NULL,
            reason TEXT DEFAULT '',
            source TEXT,
            changed_at TEXT NOT NULL,
            snapshot TEXT NOT NULL
        )
    """)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_session_history_row ON session_history(session_row_id)")
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_sets_supersedes ON workout_sets(supersedes_set_id)")


    # Feedback table (Jarvis Pro)
    _run_ddl("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'other',
            message TEXT NOT NULL,
            athlete_name TEXT DEFAULT '',
            page_context TEXT DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Label the three known historical backfill sessions (session_id 'backfill_YYYY-MM-DD').
    # Only fills a NULL source; no existing value is changed.
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE sessions SET source = ? WHERE source IS NULL AND substr(session_id, 1, 9) = ?",
                ("backfill", "backfill_"),
            )
            conn.execute(
                "UPDATE workout_sets SET source = ? WHERE source IS NULL AND substr(session_id, 1, 9) = ?",
                ("backfill", "backfill_"),
            )
    except Exception as e:
        print(f"[database] backfill source labelling skipped: {e}")


def _snapshot_session_row(conn, row: dict, change_type: str, reason: str = "", source: str = None):
    conn.execute(
        """INSERT INTO session_history (session_row_id, session_id, change_type, reason, source, changed_at, snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], row.get("session_id"), change_type, reason or "", source, _now_ts(),
         json.dumps(row, default=str)),
    )


def _snapshot_set_row(conn, row: dict, change_type: str, reason: str = "", source: str = None):
    conn.execute(
        """INSERT INTO set_history (set_id, change_type, reason, source, changed_at,
                                    exercise, weight, reps, rpe, notes, performed_at, snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], change_type, reason or "", source, _now_ts(),
         row.get("exercise"), row.get("weight"), row.get("reps"), row.get("rpe"),
         row.get("notes"), row.get("performed_at"), json.dumps(row, default=str)),
    )


def _find_session(conn, ident, include_deleted: bool = False):
    """Resolve a session by integer row id or text session_id."""
    if ident is None or ident == "":
        return None
    deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
    row = None
    if isinstance(ident, int) or str(ident).isdigit():
        row = conn.execute(f"SELECT * FROM sessions WHERE id = ?{deleted_clause}", (int(ident),)).fetchone()
    if not row:
        row = conn.execute(
            f"SELECT * FROM sessions WHERE session_id = ?{deleted_clause} ORDER BY id DESC LIMIT 1",
            (str(ident),),
        ).fetchone()
    return dict(row) if row else None


def _last_set_time(conn, session_id: str, performed_only: bool = False):
    """Timestamp of the last live set in a session (performed_at, else logging time)."""
    rows = conn.execute(
        f"SELECT performed_at, timestamp FROM workout_sets ws WHERE ws.session_id = ? AND {LIVE_SET}",
        (session_id,),
    ).fetchall()
    best, best_dt = None, None
    for r in rows:
        v = r["performed_at"] if performed_only else (r["performed_at"] or r["timestamp"])
        dt = _parse_ts(v)
        if dt and (best_dt is None or dt > best_dt):
            best, best_dt = v, dt
    return best


def _default_end_time(conn, session: dict) -> str:
    """Session end defaults to the last set's timestamp — not whenever someone
    remembered to press END. Falls back to now when no sets were logged.
    Backfill sessions only use real performed_at times; with none, the end stays unknown (None)
    rather than being stamped with the time the backfill was typed in."""
    backfill = bool(session.get("is_backfill"))
    last = _last_set_time(conn, session["session_id"], performed_only=backfill)
    started = _parse_ts(session.get("started_at"))
    if last and (started is None or _parse_ts(last) >= started):
        return last
    return None if backfill else _local_now().isoformat()


def _close_open_sessions(conn, only_other_dates: str = None):
    """Close live, non-backfill sessions that were left open, using each one's last set time."""
    sql = f"SELECT * FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL AND {NOT_BACKFILL}"
    params = ()
    if only_other_dates:
        sql += " AND date != ?"
        params = (only_other_dates,)
    for row in conn.execute(sql, params).fetchall():
        row = dict(row)
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (_default_end_time(conn, row), row["id"]),
        )


def _new_session_id(session_date: str, program: str) -> str:
    """session_{date}_{program}_{6 hex}. The suffix means a session re-created on the
    same date+program can never inherit rows keyed to an earlier (deleted) session."""
    import uuid
    safe_program = "_".join(str(program or "Workout").split())
    return f"session_{session_date}_{safe_program}_{uuid.uuid4().hex[:6]}"


def _plan_snapshot_json(session_date: str):
    try:
        plan = get_planned_workout(session_date)
    except Exception as e:
        print(f"[database] plan snapshot lookup failed: {e}")
        plan = None
    return json.dumps(plan) if plan else None


def _insert_session(conn, session_date: str, program: str, started_at, source: str,
                    notes: str = "", is_backfill: bool = False, ended_at=None) -> dict:
    session_id = _new_session_id(session_date, program)
    row = conn.execute(
        """INSERT INTO sessions (session_id, date, session_date, program, started_at, ended_at, notes,
                                 is_backfill, created_at, source, planned_workout_snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (session_id, session_date, session_date, program, started_at, ended_at, notes or "",
         _backfill_flag(is_backfill), _local_now().isoformat(), source, _plan_snapshot_json(session_date)),
    ).fetchone()
    return {"id": row["id"], "session_id": session_id}


def init_db():
    if USE_PG:
        # PostgreSQL — just create tables, no file management needed
        _init_db_impl()
        return
    # SQLite: If the database exists but doesn't have the right schema, delete and start fresh
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
            conn.close()
            if 'is_backfill' not in cols:
                print("[database] Old schema detected — rebuilding database from scratch")
                DB_PATH.unlink()
        except Exception:
            DB_PATH.unlink()  # Corrupted or unreadable, start fresh
    
    _init_db_impl()

def _init_db_impl():
    """Create tables if they don't exist. Called on app startup."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                date TEXT NOT NULL,
                session_date TEXT,
                program TEXT DEFAULT 'Arsenal',
                started_at TEXT,
                ended_at TEXT,
                notes TEXT DEFAULT '',
                is_backfill INTEGER DEFAULT 0,
                created_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS workout_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                exercise TEXT NOT NULL,
                weight TEXT NOT NULL DEFAULT 'bodyweight',
                reps TEXT DEFAULT '',
                rpe TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                timestamp TEXT DEFAULT '',
                set_index INTEGER,
                logged_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                message_type TEXT DEFAULT 'general',
                exercise_context TEXT,
                tags TEXT DEFAULT '[]',
                sentiment TEXT DEFAULT 'neutral',
                source TEXT DEFAULT 'typed',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_chat_history_date ON chat_history(session_date DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_history_type ON chat_history(message_type);
            CREATE INDEX IF NOT EXISTS idx_chat_history_exercise ON chat_history(exercise_context);
            CREATE INDEX IF NOT EXISTS idx_chat_history_tags ON chat_history(tags);

            CREATE TABLE IF NOT EXISTS bodyweight (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                weight_lbs REAL NOT NULL,
                notes TEXT DEFAULT '',
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS carry_forward (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                created_date TEXT NOT NULL,
                resolved_date TEXT,
                resolved INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_sets_session ON workout_sets(session_id);

            CREATE TABLE IF NOT EXISTS exercise_modifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                original_exercise TEXT NOT NULL,
                action TEXT NOT NULL,
                replacement_exercise TEXT,
                replacement_weight TEXT,
                replacement_reps TEXT,
                replacement_sets INTEGER,
                reason TEXT DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_mods_session ON exercise_modifications(session_id);

            CREATE TABLE IF NOT EXISTS progress_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL DEFAULT (date('now')),
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                angle TEXT DEFAULT 'front',
                bodyweight REAL,
                notes TEXT DEFAULT '',
                mime_type TEXT DEFAULT 'image/jpeg',
                photo_data BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_photos_date ON progress_photos(date);
            CREATE INDEX IF NOT EXISTS idx_sets_exercise ON workout_sets(exercise);
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_log(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
            CREATE INDEX IF NOT EXISTS idx_bodyweight_date ON bodyweight(date);
        """)

        # Carry-forward items are created by Claude during coaching, not seeded.

    _migrate_integrity()

    # Exercise library, measurements, goals (fitness.py) — additive, idempotent.
    try:
        from fitness import migrate_fitness
        migrate_fitness()
    except Exception as e:
        print(f"[database] fitness migration failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def get_or_create_today_session() -> str:
    """Return today's session_id. Closes stale sessions from other dates."""
    today = _local_today()
    with get_db() as conn:
        # Close any open sessions from OTHER dates (stale sessions) at their last set time
        _close_open_sessions(conn, only_other_dates=today)
        # Find open session for today
        row = conn.execute(
            f"SELECT session_id FROM sessions WHERE date = ? AND ended_at IS NULL AND deleted_at IS NULL AND {NOT_BACKFILL} ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
        if row:
            return row["session_id"]

        # Second: find today's session (even if ended)
        row = conn.execute(
            f"SELECT session_id FROM sessions WHERE date = ? AND deleted_at IS NULL AND {NOT_BACKFILL} ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
        if row:
            return row["session_id"]

        # Last resort: create a new session for today
        return _insert_session(conn, today, "Workout", _local_now().isoformat(), source="app")["session_id"]


def _set_volume(sets) -> int:
    total = 0
    for s in sets:
        try:
            w = float(str(s["weight"]).replace("lb", "").replace("each", "").strip().split()[0])
            r = int(str(s["reps"]).split("/")[0].strip().split()[0])
            total += round(w * r)
        except (ValueError, IndexError, TypeError):
            pass
    return total


def create_workout_session(program: str = "Strength B", notes: str = "", source: str = "app") -> dict:
    """Explicitly start a new workout session. Auto-closes any open sessions first."""
    purge_demo_sessions()
    today = _local_today()
    now = _local_now().isoformat()
    with get_db() as conn:
        # Auto-close ALL open live sessions — never allow two live sessions
        _close_open_sessions(conn)
        created = _insert_session(conn, today, program, now, source=source, notes=notes)
    return {
        "id": created["id"],
        "session_id": created["session_id"],
        "status": "started",
        "date": today,
        "program": program,
        "started_at": now,
        "notes": notes or "",
    }


def end_workout_session(session_id: str | None = None, notes: str = "", end_time: str = None,
                        source: str = "app") -> dict:
    """End a workout session and return a summary. ended_at defaults to the last set's time."""
    today = _local_today()
    with get_db() as conn:
        if session_id is None:
            row = conn.execute(
                f"SELECT * FROM sessions WHERE date = ? AND ended_at IS NULL AND deleted_at IS NULL AND {NOT_BACKFILL} ORDER BY id DESC LIMIT 1",
                (today,),
            ).fetchone()
            session = dict(row) if row else None
        else:
            session = _find_session(conn, session_id)
        if not session:
            return {"status": "no_active_session"}
        session_id = session["session_id"]

        ended_at = end_time or _default_end_time(conn, session)
        new_notes = session.get("notes") or ""
        if notes:
            new_notes = f"{new_notes}\n{notes}".strip() if new_notes else notes
        if notes or session.get("ended_at"):
            # Record what was there before we touch notes or overwrite an existing end time
            _snapshot_session_row(conn, session, "end", reason="end_session", source=source)
        conn.execute(
            "UPDATE sessions SET ended_at = ?, notes = ? WHERE id = ?",
            (ended_at, new_notes, session["id"]),
        )

        sets = conn.execute(
            f"SELECT ws.id, ws.exercise, ws.weight, ws.reps, ws.rpe, ws.notes FROM workout_sets ws WHERE ws.session_id = ? AND {ANALYTIC_SET} ORDER BY ws.id",
            (session_id,),
        ).fetchall()

    # Build summary
    exercises_done = {}
    for s in sets:
        exercises_done.setdefault(s["exercise"], []).append(f"{s['weight']} x {s['reps']}")

    return {
        "status": "ended",
        "id": session["id"],
        "session_id": session_id,
        "started_at": session.get("started_at"),
        "ended_at": ended_at,
        "duration_min": _duration_min(session.get("started_at"), ended_at),
        "notes": new_notes,
        "total_sets": len(sets),
        "total_volume": _set_volume(sets),
        "exercises": exercises_done,
        "sets": [dict(s) for s in sets],
    }


# ---------------------------------------------------------------------------
# Demo sessions — real DB rows (so Claude/MCP writes land in them and the panel
# reflects them), tagged by a "demo_" session_id and hard-deleted on end.
# ---------------------------------------------------------------------------

DEMO_PREFIX = "demo_"
# Tables whose rows are keyed by the text session_id.
_DEMO_CHILD_TABLES = ("workout_sets", "exercise_modifications", "set_deviations", "session_coaching_notes")


def _is_demo_id(session_id) -> bool:
    return isinstance(session_id, str) and session_id.startswith(DEMO_PREFIX)


def _delete_demo_rows(session_id: str) -> dict:
    """Delete a demo session and everything keyed to it.
    Each table gets its own transaction: on Postgres one failed statement (e.g. a table
    that doesn't exist yet) would otherwise abort — and roll back — all the others."""
    if not _is_demo_id(session_id):
        raise ValueError("refusing to delete non-demo session")
    counts = {}
    for table in _DEMO_CHILD_TABLES + ("sessions",):
        try:
            with get_db() as conn:
                counts[table] = conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,)).rowcount
        except Exception as e:
            if table == "sessions":
                raise
            counts[table] = 0  # optional table missing
            print(f"[database] demo cleanup skipped {table}: {e}")
    return counts


# Every table that can carry demo rows (is_demo = 1). Order: children before sessions.
DEMO_TABLES = ("workout_sets", "exercise_modifications", "set_deviations", "session_coaching_notes",
               "bodyweight", "body_measurements", "progress_photos", "coach_briefs", "goals", "sessions")
DEMO_MAX_AGE_H = 12  # an open demo older than this no longer tags new writes


def demo_active() -> bool:
    """True while a demo session is open (and was started in the last DEMO_MAX_AGE_H hours).
    Writes made meanwhile — by the panel or by Claude over MCP — are tagged is_demo."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT started_at FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL "
                "AND substr(session_id, 1, 5) = ? ORDER BY id DESC LIMIT 1", (DEMO_PREFIX,)
            ).fetchone()
    except Exception:
        return False
    if not row:
        return False
    started = _parse_ts(row["started_at"])
    return bool(started and (_local_now() - started).total_seconds() < DEMO_MAX_AGE_H * 3600)


def demo_flag() -> int:
    return 1 if demo_active() else 0


def wipe_demo_data() -> dict:
    """Hard-delete every demo row in every table and revert real goals edited during the demo.
    Demo rows are never part of the athlete's record. Each table gets its own transaction so a
    missing optional table can't roll back the rest."""
    counts = {}
    try:
        from fitness import revert_demo_goal_edits
        counts["goals_reverted"] = revert_demo_goal_edits()
    except Exception as e:
        print(f"[database] demo goal revert skipped: {e}")
    for table in DEMO_TABLES:
        n = 0
        try:
            with get_db() as conn:
                n += conn.execute(f"DELETE FROM {table} WHERE is_demo = 1").rowcount
        except Exception as e:
            print(f"[database] demo wipe skipped {table}: {e}")
        if table in _DEMO_CHILD_TABLES or table == "sessions":
            try:
                with get_db() as conn:
                    n += conn.execute(f"DELETE FROM {table} WHERE substr(session_id, 1, 5) = ?", (DEMO_PREFIX,)).rowcount
            except Exception as e:
                print(f"[database] demo wipe (by id) skipped {table}: {e}")
        counts[table] = n
    return counts


def purge_demo_sessions() -> list:
    """Wipe every demo session and all demo-tagged data. Called before any real session starts,
    so a forgotten demo can never swallow real sets. Never raises — it must not block a real start."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE substr(session_id, 1, 5) = ?", (DEMO_PREFIX,)
            ).fetchall()
        ids = [r["session_id"] for r in rows]
        wipe_demo_data()
        return ids
    except Exception as e:
        print(f"[database] purge_demo_sessions failed: {e}")
        return []


def start_demo_session(program: str = "Demo") -> dict:
    """Open a demo session. Refuses while a real session is live (never touches real data)."""
    with get_db() as conn:
        live = conn.execute(
            f"SELECT session_id FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL AND {NOT_BACKFILL} AND substr(session_id, 1, 5) != ? ORDER BY id DESC LIMIT 1",
            (DEMO_PREFIX,),
        ).fetchone()
    if live:
        return {"error": "real_session_live", "session_id": live["session_id"]}
    purge_demo_sessions()
    now = _local_now()
    today = now.date().isoformat()
    session_id = f"{DEMO_PREFIX}{today}_{now.strftime('%H%M%S')}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, date, session_date, program, started_at, notes, is_demo) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (session_id, today, today, f"DEMO · {program}", now.replace(tzinfo=None).isoformat(timespec="seconds"),
             "Demo session — auto-deleted on end"),
        )
    return {"session_id": session_id, "status": "started", "demo": True, "date": today,
            "program": f"DEMO · {program}", "started_at": now.isoformat()}


def end_demo_session(session_id: str = None) -> dict:
    """Summarize, then wipe the demo: the session, its sets/notes/added exercises, and every
    row tagged is_demo anywhere (weigh-ins, measurements, photos, goals…). Only accepts demo ids;
    with no id, ends whichever demo is open."""
    if not session_id:
        with get_db() as conn:
            row = conn.execute("SELECT session_id FROM sessions WHERE substr(session_id, 1, 5) = ? ORDER BY id DESC LIMIT 1",
                               (DEMO_PREFIX,)).fetchone()
        session_id = row["session_id"] if row else ""
    if not _is_demo_id(session_id):
        return {"error": "not_a_demo_session"}
    with get_db() as conn:
        sets = conn.execute(
            "SELECT weight, reps FROM workout_sets WHERE session_id = ?", (session_id,)
        ).fetchall()
    total_volume = 0
    for s in sets:
        try:
            w = float(str(s["weight"]).replace("lb", "").replace("each", "").strip().split()[0])
            r = int(str(s["reps"]).split("/")[0].strip())
            total_volume += round(w * r)
        except (ValueError, IndexError):
            pass
    deleted = wipe_demo_data()
    return {"status": "deleted", "demo": True, "session_id": session_id,
            "total_sets": len(sets), "total_volume": total_volume, "deleted": deleted,
            "total_rows_deleted": sum(v for k, v in deleted.items() if k != "goals_reverted")}


def get_current_session_data() -> dict | None:
    """Return the current active session with all logged sets, or None."""
    cols = "id, session_id, date, program, started_at, ended_at, notes"
    with get_db() as conn:
        # First check for any open live session
        session = conn.execute(
            f"SELECT {cols} FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL AND {NOT_BACKFILL} ORDER BY id DESC LIMIT 1",
        ).fetchone()
        # If no open session, check today's most recent
        if not session:
            today = _local_today()
            session = conn.execute(
                f"SELECT {cols} FROM sessions WHERE date = ? AND deleted_at IS NULL AND {NOT_BACKFILL} ORDER BY id DESC LIMIT 1",
                (today,),
            ).fetchone()
        if not session:
            return None

        sets = conn.execute(
            f"SELECT ws.id, ws.exercise, ws.weight, ws.reps, ws.rpe, ws.notes, ws.timestamp, ws.performed_at, ws.supersedes_set_id FROM workout_sets ws WHERE ws.session_id = ? AND {ANALYTIC_SET} ORDER BY ws.id",
            (session["session_id"],),
        ).fetchall()

    # Group sets by exercise with set numbers
    exercise_sets = {}
    for s in sets:
        ex = s["exercise"]
        if ex not in exercise_sets:
            exercise_sets[ex] = []
        exercise_sets[ex].append({
            "id": s["id"],
            "set_num": len(exercise_sets[ex]) + 1,
            "weight": s["weight"],
            "reps": s["reps"],
            "rpe": s["rpe"],
            "notes": s["notes"],
            "timestamp": s["timestamp"],
            "performed_at": s["performed_at"],
            "supersedes_set_id": s["supersedes_set_id"],
        })

    total_volume = _set_volume(sets)

    return {
        "id": session["id"],
        "notes": session["notes"] or "",
        "session_id": session["session_id"],
        "date": session["date"],
        "program": session["program"],
        "started_at": session["started_at"],
        "ended_at": session["ended_at"],
        "active": session["ended_at"] is None,
        "exercise_sets": exercise_sets,
        "total_sets": len(sets),
        "total_volume": total_volume,
    }


# ---------------------------------------------------------------------------
# Workout set logging
# ---------------------------------------------------------------------------

def log_workout_set(
    exercise: str,
    weight: str,
    reps: str,
    rpe: str = "",
    notes: str = "",
    session_id: str | None = None,
    source: str = "app",
    performed_at: str | None = None,
    supersedes_set_id: int | None = None,
) -> dict:
    """Log a single workout set. Returns the new set's integer id and its set_num.

    performed_at: when the set was actually done (for after-the-fact logging). Defaults to
    now for live logging; left NULL for backfill so no time is invented.
    supersedes_set_id: this set is a correction of an earlier set, which stays in the record
    but drops out of analytics.
    """
    if source not in SOURCES:
        return {"error": f"source must be one of {list(SOURCES)}"}
    if session_id is None:
        session_id = get_or_create_today_session()

    now = _local_now().isoformat()
    with get_db() as conn:
        session = conn.execute(
            "SELECT id, session_id, deleted_at, is_backfill FROM sessions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if session and session["deleted_at"]:
            return {"error": f"Session {session_id} is deleted. Restore it first or log to another session."}
        if session and session["is_backfill"] and source in ("app", "mcp"):
            source = "backfill"
        if performed_at is None and source != "backfill" and not (session and session["is_backfill"]):
            performed_at = now

        if supersedes_set_id is not None:
            old = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(supersedes_set_id),)).fetchone()
            if not old:
                return {"error": f"No set with id {supersedes_set_id} to supersede"}
            supersedes_set_id = int(supersedes_set_id)

        # Count existing (analytic) sets for this exercise in this session
        _row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM workout_sets ws WHERE ws.session_id = ? AND ws.exercise = ? AND {ANALYTIC_SET}",
            (session_id, exercise),
        ).fetchone()
        count = _row['cnt'] if isinstance(_row, dict) else _row[0]
        # A correction takes the place of the set it supersedes rather than adding one
        set_num = count if supersedes_set_id is not None and count else count + 1

        row = conn.execute(
            """INSERT INTO workout_sets
               (session_id, exercise, weight, reps, rpe, notes, timestamp, logged_at,
                source, performed_at, supersedes_set_id, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (session_id, exercise, str(weight), str(reps), str(rpe), notes, now, now,
             source, performed_at, supersedes_set_id, 1 if _is_demo_id(session_id) else 0),
        ).fetchone()
        set_id = row["id"]

    deviation = None
    try:
        deviation = auto_log_set_deviation(set_id)
    except Exception as e:
        print(f"[database] auto deviation failed for set {set_id}: {e}")

    return {
        "status": "logged",
        "set_id": set_id,
        "set_num": set_num,
        "deviation": deviation,
        "entry": {
            "id": set_id,
            "exercise": exercise,
            "weight": str(weight),
            "reps": str(reps),
            "rpe": str(rpe),
            "notes": notes,
            "timestamp": now,
            "performed_at": performed_at,
            "source": source,
            "supersedes_set_id": supersedes_set_id,
            "session_id": session_id,
            "set_num": set_num,
        },
    }


# ---------------------------------------------------------------------------
# Chat logging
# ---------------------------------------------------------------------------

def log_chat(role: str, content: str, session_id: str | None = None):
    """Log a chat message (user or assistant)."""
    if session_id is None:
        session_id = "chat_" + date.today().isoformat()

    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )


# ---------------------------------------------------------------------------
# Body weight
# ---------------------------------------------------------------------------

def _opt_bool(value):
    """None stays None (unknown); accepts bools, 0/1 and 'true'/'false' strings."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def log_bodyweight_entry(weight_lbs: float, dt: str | None = None, notes: str = "",
                         fasted=None, time_of_day: str | None = None, off_protocol=False) -> dict:
    """Log a bodyweight measurement.

    fasted: True/False, or None when unknown. time_of_day: free text ('06:10', 'morning').
    off_protocol: True for weigh-ins that break the fasted-morning protocol (post-meal,
    travel scale…) — kept in the record but excluded from trend analytics.
    """
    dt = dt or _local_today()
    if not _valid_date(dt):
        return {"error": f"date must be YYYY-MM-DD, got {dt!r}"}
    now = _local_now().isoformat()
    if time_of_day is None and dt == _local_today():
        time_of_day = _local_now().strftime("%H:%M")
    fasted = _opt_bool(fasted)
    off_protocol = bool(_opt_bool(off_protocol))
    with get_db() as conn:
        row = conn.execute(
            """INSERT INTO bodyweight (date, weight_lbs, notes, timestamp, fasted, time_of_day, off_protocol, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (dt, weight_lbs, notes, now, fasted, time_of_day, off_protocol, demo_flag()),
        ).fetchone()
    return {"status": "logged", "entry": {
        "id": row["id"], "date": dt, "weight_lbs": weight_lbs, "notes": notes, "is_demo": demo_active(),
        "fasted": fasted, "time_of_day": time_of_day, "off_protocol": off_protocol,
    }}


def get_bodyweight_history(limit: int = 30, include_off_protocol: bool = True) -> list[dict]:
    """Return recent bodyweight entries. Pass include_off_protocol=False for trend analytics."""
    where = "" if include_off_protocol else " WHERE off_protocol IS NULL OR off_protocol = ?"
    params = () if include_off_protocol else (False,)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, date, weight_lbs, notes, fasted, time_of_day, off_protocol, is_demo FROM bodyweight{where} ORDER BY date DESC, id DESC LIMIT ?",
            params + (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["fasted"] = None if d.get("fasted") is None else bool(d["fasted"])
        d["off_protocol"] = bool(d.get("off_protocol"))
        d["is_demo"] = bool(d.get("is_demo"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# History queries — for system prompt injection
# ---------------------------------------------------------------------------

def get_last_workout() -> dict | None:
    """Return the most recent workout session with its sets."""
    with get_db() as conn:
        session = conn.execute(
            "SELECT session_id, date, program FROM sessions WHERE deleted_at IS NULL ORDER BY date DESC, id DESC LIMIT 1"
        ).fetchone()
        if not session:
            return None

        sets = conn.execute(
            f"SELECT ws.id, ws.exercise, ws.weight, ws.reps, ws.rpe, ws.notes FROM workout_sets ws WHERE ws.session_id = ? AND {ANALYTIC_SET} ORDER BY ws.id",
            (session["session_id"],),
        ).fetchall()

        return {
            "date": session["date"],
            "program": session["program"],
            "sets": [dict(s) for s in sets],
        }


_SET_COLS = ("ws.id, ws.session_id, ws.exercise, ws.weight, ws.reps, ws.rpe, ws.notes, ws.timestamp, "
             "ws.performed_at, ws.source, ws.supersedes_set_id, ws.deleted_at, ws.deleted_reason")


def _session_sets(conn, session_id: str, include_deleted: bool = False) -> dict:
    """Split a session's sets into analytic sets, superseded sets and (optionally) deleted sets."""
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_SET_COLS} FROM workout_sets ws WHERE ws.session_id = ? ORDER BY ws.id",
        (session_id,),
    ).fetchall()]
    live = [r for r in rows if not r["deleted_at"]]
    superseded_by = {r["supersedes_set_id"]: r["id"] for r in live if r["supersedes_set_id"] is not None}
    sets, superseded = [], []
    for r in live:
        if r["id"] in superseded_by:
            r["superseded_by_set_id"] = superseded_by[r["id"]]
            superseded.append(r)
        else:
            sets.append(r)
    out = {"sets": sets, "superseded_sets": superseded}
    if include_deleted:
        out["deleted_sets"] = [r for r in rows if r["deleted_at"]]
    return out


def get_recent_sessions(limit: int = 5, include_deleted: bool = False) -> list[dict]:
    """Return summaries of recent workout sessions with duration and timing.

    Every set carries its integer id. `sets` holds only analytic sets (live, not superseded);
    superseded sets are listed separately. include_deleted adds soft-deleted sessions and
    a `deleted_sets` list per session.
    """
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    with get_db() as conn:
        sessions = conn.execute(
            f"""SELECT id, session_id, date,
                       COALESCE(session_date, date) as session_date,
                       program, started_at, ended_at, notes, is_backfill,
                       source, deleted_at, deleted_reason, planned_workout_snapshot
                FROM sessions {where} ORDER BY date DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

        results = []
        prev_date = None
        for sess in sessions:
            split = _session_sets(conn, sess["session_id"], include_deleted)
            sets = split["sets"]

            exercises_done = list(dict.fromkeys(s["exercise"] for s in sets))

            duration_min = _duration_min(sess["started_at"], sess["ended_at"])
            time_of_day = _time_of_day(sess["started_at"]) if duration_min is not None else None

            days_since = None
            if prev_date and sess["date"]:
                try:
                    from datetime import datetime as _dt
                    d1 = _dt.strptime(prev_date, "%Y-%m-%d")
                    d2 = _dt.strptime(sess["date"], "%Y-%m-%d")
                    days_since = (d1 - d2).days
                except Exception:
                    pass
            prev_date = sess["date"]
            
            results.append({
                "id": sess["id"],
                "session_id": sess["session_id"],
                "date": sess["date"],
                "program": sess["program"],
                "exercises": exercises_done,
                "total_sets": len(sets),
                "sets": sets,
                "superseded_sets": split["superseded_sets"],
                "started_at": sess["started_at"],
                "ended_at": sess["ended_at"],
                "duration_min": duration_min,
                "time_of_day": time_of_day,
                "days_since_previous": days_since,
                "notes": sess["notes"] or "",
                "is_backfill": bool(sess["is_backfill"]) if sess["is_backfill"] is not None else False,
                "source": sess["source"],
                "has_plan_snapshot": sess["planned_workout_snapshot"] is not None,
            })
            if include_deleted:
                results[-1]["deleted_at"] = sess["deleted_at"]
                results[-1]["deleted_reason"] = sess["deleted_reason"]
                results[-1]["deleted_sets"] = split["deleted_sets"]

        return results


def get_recent_history_for_prompt(session_limit: int = 5) -> str:
    """
    Build a text block summarizing recent workout history,
    suitable for injection into the system prompt.
    """
    lines = []

    # Last workout
    last = get_last_workout()
    if last and last["sets"]:
        lines.append(f"LAST WORKOUT: {last['date']} ({last['program']})")
        exercise_summary = {}
        for s in last["sets"]:
            ex = s["exercise"]
            if ex not in exercise_summary:
                exercise_summary[ex] = []
            exercise_summary[ex].append(f"{s['weight']} x {s['reps']}")
        for ex, detail in exercise_summary.items():
            lines.append(f"  {ex}: {', '.join(detail)}")
    else:
        lines.append("LAST WORKOUT: No sessions logged yet")

    # Recent sessions summary
    recent = get_recent_sessions(session_limit)
    if len(recent) > 1:
        lines.append("")
        lines.append(f"RECENT SESSIONS (last {len(recent)}):")
        for sess in recent:
            ex_list = ", ".join(sess["exercises"][:5])
            lines.append(f"  {sess['date']}: {sess['total_sets']} sets -- {ex_list}")

    # Bodyweight trend (protocol weigh-ins only)
    bw = get_bodyweight_history(10, include_off_protocol=False)
    if bw:
        lines.append("")
        lines.append("BODYWEIGHT TREND:")
        for entry in bw[:5]:
            note = f" ({entry['notes']})" if entry.get("notes") else ""
            lines.append(f"  {entry['date']}: {entry['weight_lbs']} lb{note}")
        if len(bw) >= 2:
            delta = bw[0]["weight_lbs"] - bw[-1]["weight_lbs"]
            direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
            lines.append(f"  Trend over last {len(bw)} entries: {direction} ({delta:+.1f} lb)")

    # Carry-forward items
    with get_db() as conn:
        cf_rows = conn.execute(
            "SELECT item FROM carry_forward WHERE resolved = 0 ORDER BY id"
        ).fetchall()
    if cf_rows:
        lines.append("")
        lines.append("CARRY-FORWARD ITEMS:")
        for row in cf_rows:
            lines.append(f"  - {row['item']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Carry-forward management
# ---------------------------------------------------------------------------

def get_carry_forward_items() -> list[dict]:
    """Return unresolved carry-forward items."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, item, created_date FROM carry_forward WHERE resolved = 0 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_carry_forward(item_id: int):
    """Mark a carry-forward item as resolved."""
    with get_db() as conn:
        conn.execute(
            "UPDATE carry_forward SET resolved = 1, resolved_date = ? WHERE id = ?",
            (date.today().isoformat(), item_id),
        )


def add_carry_forward(item: str):
    """Add a new carry-forward item."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO carry_forward (item, created_date) VALUES (?, ?)",
            (item, date.today().isoformat()),
        )


# ---------------------------------------------------------------------------
# Full history for MCP tools (mirrors the old JSON-based approach)
# ---------------------------------------------------------------------------

def get_full_history(limit: int = 5, include_deleted: bool = False) -> dict:
    """Return session data in the same shape the old JSON history used."""
    sessions = get_recent_sessions(limit, include_deleted=include_deleted)
    return {"sessions": sessions, "total": len(sessions), "include_deleted": include_deleted}


# Initialize on import
try:
    init_db()
except Exception as e:
    print(f"[database] init_db failed: {e}")


# ---------------------------------------------------------------------------
# V2 Session management (with proper dates and backfill support)
# ---------------------------------------------------------------------------

def start_session_v2(program: str, session_date: str = None, start_time: str = None,
                     notes: str = "", source: str = "mcp") -> dict:
    """Create a workout session, snapshotting the day's plan into planned_workout_snapshot.

    Today: auto-closes other open live sessions (at their last set time). If a live session
    for the same date+program is already open, it is returned instead of creating a duplicate.
    If one exists but was ended, it is reopened — the prior end time is kept in session_history.

    Past date: a backfill session (is_backfill, source 'backfill'). Live sessions are not
    touched. Actual-only when no plan existed for that date.
    """
    purge_demo_sessions()
    today = _local_today()
    session_date = session_date or today
    if not _valid_date(session_date):
        return {"error": f"date must be YYYY-MM-DD, got {session_date!r}"}
    if session_date > today:
        return {"error": f"Cannot start a session in the future ({session_date})"}
    is_backfill = session_date < today
    if is_backfill:
        source = "backfill"
    if source not in SOURCES:
        return {"error": f"source must be one of {list(SOURCES)}"}
    start_time = start_time or (None if is_backfill else _local_now().isoformat())

    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM sessions WHERE COALESCE(session_date, date) = ? AND program = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
            (session_date, program),
        ).fetchone()
        existing = dict(existing) if existing else None

        if not is_backfill:
            # Never allow two live sessions (the matching one, if open, is kept open)
            for row in conn.execute(
                f"SELECT * FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL AND {NOT_BACKFILL}"
            ).fetchall():
                row = dict(row)
                if existing and row["id"] == existing["id"]:
                    continue
                conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?",
                             (_default_end_time(conn, row), row["id"]))

        if existing:
            status = "already_open"
            if existing["ended_at"] and not is_backfill:
                _snapshot_session_row(conn, existing, "reopen", reason="start_session on same date+program", source=source)
                conn.execute("UPDATE sessions SET ended_at = NULL WHERE id = ?", (existing["id"],))
                status = "reopened"
            elif existing["ended_at"]:
                status = "existing"
            if notes:
                _snapshot_session_row(conn, existing, "update", reason="start_session notes", source=source)
                merged = f"{existing['notes']}\n{notes}".strip() if existing.get("notes") else notes
                conn.execute("UPDATE sessions SET notes = ? WHERE id = ?", (merged, existing["id"]))
            return {"session_id": existing["session_id"], "db_id": existing["id"], "status": status,
                    "date": session_date, "program": program, "is_backfill": is_backfill,
                    "has_plan_snapshot": existing.get("planned_workout_snapshot") is not None}

        created = _insert_session(conn, session_date, program, start_time, source=source,
                                  notes=notes, is_backfill=is_backfill)
        snap = conn.execute("SELECT planned_workout_snapshot FROM sessions WHERE id = ?", (created["id"],)).fetchone()
        return {"session_id": created["session_id"], "db_id": created["id"], "status": "created",
                "date": session_date, "program": program, "started_at": start_time,
                "is_backfill": is_backfill, "notes": notes or "",
                "has_plan_snapshot": snap["planned_workout_snapshot"] is not None}


def find_or_create_session_for_date(session_date: str, program: str = "") -> dict:
    """Used by log_set(date=…): attach to an existing live session on that date (matching
    program if given), otherwise create one. Past dates become backfill sessions."""
    if not _valid_date(session_date):
        return {"error": f"date must be YYYY-MM-DD, got {session_date!r}"}
    if session_date > _local_today():
        return {"error": f"Cannot log sets in the future ({session_date})"}
    with get_db() as conn:
        sql = "SELECT id, session_id, program FROM sessions WHERE COALESCE(session_date, date) = ? AND deleted_at IS NULL"
        params = [session_date]
        if program:
            sql += " AND program = ?"
            params.append(program)
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", tuple(params)).fetchone()
    if row:
        return {"session_id": row["session_id"], "db_id": row["id"], "status": "existing"}
    return start_session_v2(program or "Workout", session_date,
                            source="backfill" if session_date < _local_today() else "mcp")


def end_session_v2(session_id=None, end_time: str = None, notes: str = "", source: str = "mcp") -> dict:
    """End a workout session (integer id or text session_id; default: most recent open one).
    end_time defaults to the last set's timestamp, not now."""
    with get_db() as conn:
        if session_id:
            session = _find_session(conn, session_id)
            if not session:
                return {"error": f"No session {session_id}"}
        else:
            row = conn.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
            ).fetchone()
            if not row:
                return {"status": "no_active_session"}
            session = dict(row)
    return end_workout_session(session["session_id"], notes=notes, end_time=end_time, source=source)


# ---------------------------------------------------------------------------
# Soft deletes, restores and versioned edits
# ---------------------------------------------------------------------------

def delete_set(set_id: int) -> dict:
    """Preview the set that would be soft-deleted."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            return {"error": f"No set with id {set_id}"}
        return {"preview": dict(row), "note": "Soft delete: the set is kept and can be restored with restore_set. Pass confirm=true to delete."}


def confirm_delete_set(set_id: int, reason: str = "", source: str = "mcp") -> dict:
    """Soft-delete a set: stamps deleted_at/deleted_reason; the row stays and is restorable."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            return {"error": f"No set with id {set_id}"}
        record = dict(row)
        if record.get("deleted_at"):
            return {"status": "already_deleted", "record": record}
        _snapshot_set_row(conn, record, "delete", reason=reason, source=source)
        conn.execute(
            "UPDATE workout_sets SET deleted_at = ?, deleted_reason = ? WHERE id = ?",
            (_now_ts(), reason or "", int(set_id)),
        )
        return {"status": "deleted", "soft": True, "record": record}


def restore_set(set_id: int, reason: str = "", source: str = "mcp") -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            return {"error": f"No set with id {set_id}"}
        record = dict(row)
        if not record.get("deleted_at"):
            return {"status": "not_deleted", "record": record}
        sess = conn.execute(
            "SELECT deleted_at FROM sessions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (record["session_id"],),
        ).fetchone()
        if sess and sess["deleted_at"]:
            return {"error": f"Its session {record['session_id']} is deleted — use restore_session."}
        _snapshot_set_row(conn, record, "restore", reason=reason, source=source)
        conn.execute("UPDATE workout_sets SET deleted_at = NULL, deleted_reason = NULL WHERE id = ?", (int(set_id),))
    return {"status": "restored", "set_id": int(set_id)}


def delete_session(session_id) -> dict:
    """Preview the session (and its live sets) that would be soft-deleted."""
    with get_db() as conn:
        row = _find_session(conn, session_id, include_deleted=True)
        if not row:
            return {"error": f"No session with id {session_id}"}
        sets = conn.execute(
            f"SELECT {_SET_COLS} FROM workout_sets ws WHERE ws.session_id = ? AND {LIVE_SET} ORDER BY ws.id",
            (row["session_id"],),
        ).fetchall()
        return {"session": row, "sets": [dict(s) for s in sets], "set_count": len(sets),
                "note": "Soft delete: session and sets are kept and can be restored with restore_session. Pass confirm=true to delete."}


def confirm_delete_session(session_id, reason: str = "", source: str = "mcp") -> dict:
    """Soft-delete a session and cascade the soft delete to its live sets."""
    stamp = _now_ts()
    with get_db() as conn:
        row = _find_session(conn, session_id, include_deleted=True)
        if not row:
            return {"error": f"No session with id {session_id}"}
        if row.get("deleted_at"):
            return {"status": "already_deleted", "record": row}
        _snapshot_session_row(conn, row, "delete", reason=reason, source=source)
        conn.execute("UPDATE sessions SET deleted_at = ?, deleted_reason = ? WHERE id = ?",
                     (stamp, reason or "", row["id"]))
        live = conn.execute(
            f"SELECT * FROM workout_sets ws WHERE ws.session_id = ? AND {LIVE_SET}", (row["session_id"],)
        ).fetchall()
        for s in live:
            _snapshot_set_row(conn, dict(s), "delete", reason=_SESSION_DELETE_PREFIX + (reason or ""), source=source)
        conn.execute(
            f"UPDATE workout_sets SET deleted_at = ?, deleted_reason = ? WHERE session_id = ? AND deleted_at IS NULL",
            (stamp, _SESSION_DELETE_PREFIX + (reason or ""), row["session_id"]),
        )
        return {"status": "deleted", "soft": True, "record": row, "sets_deleted": len(live)}


def restore_session(session_id, reason: str = "", source: str = "mcp") -> dict:
    """Undo a soft delete. Restores the sets that were deleted by the session delete
    (sets deleted individually beforehand stay deleted)."""
    with get_db() as conn:
        row = _find_session(conn, session_id, include_deleted=True)
        if not row:
            return {"error": f"No session with id {session_id}"}
        if not row.get("deleted_at"):
            return {"status": "not_deleted", "record": row}
        _snapshot_session_row(conn, row, "restore", reason=reason, source=source)
        conn.execute("UPDATE sessions SET deleted_at = NULL, deleted_reason = NULL WHERE id = ?", (row["id"],))
        cascaded = [dict(s) for s in conn.execute(
            "SELECT * FROM workout_sets WHERE session_id = ? AND deleted_at IS NOT NULL AND substr(deleted_reason, 1, ?) = ?",
            (row["session_id"], len(_SESSION_DELETE_PREFIX), _SESSION_DELETE_PREFIX),
        ).fetchall()]
        for s in cascaded:
            _snapshot_set_row(conn, s, "restore", reason=reason, source=source)
            conn.execute("UPDATE workout_sets SET deleted_at = NULL, deleted_reason = NULL WHERE id = ?", (s["id"],))
    return {"status": "restored", "session_id": row["session_id"], "id": row["id"], "sets_restored": len(cascaded)}


_SET_EDITABLE = ("exercise", "weight", "reps", "rpe", "notes", "performed_at")


def update_set(set_id: int, reason: str = "", source: str = "correction", **fields) -> dict:
    """Edit a set in place. The prior values are written to set_history first.
    Only fields passed (not None) change. Past sets are editable — a frozen plan
    doesn't freeze the actuals."""
    changes = {k: (str(v) if k in ("weight", "reps", "rpe") else v)
               for k, v in fields.items() if k in _SET_EDITABLE and v is not None}
    if not changes:
        return {"error": f"Nothing to update. Editable fields: {list(_SET_EDITABLE)}"}
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            return {"error": f"No set with id {set_id}"}
        before = dict(row)
        if before.get("deleted_at"):
            return {"error": f"Set {set_id} is deleted. Restore it before editing."}
        changes = {k: v for k, v in changes.items() if str(before.get(k)) != str(v)}
        if not changes:
            return {"status": "unchanged", "set": before}
        _snapshot_set_row(conn, before, "update", reason=reason, source=source)
        assignments = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE workout_sets SET {assignments} WHERE id = ?", tuple(changes.values()) + (int(set_id),))
        after = dict(conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone())

    deviation = None
    try:
        deviation = auto_log_set_deviation(int(set_id))
    except Exception as e:
        print(f"[database] auto deviation failed for set {set_id}: {e}")
    return {"status": "updated", "set_id": int(set_id), "changed": list(changes),
            "before": {k: before.get(k) for k in changes}, "after": {k: after.get(k) for k in changes},
            "set": after, "deviation": deviation}


def get_set_history(set_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            return {"error": f"No set with id {set_id}"}
        history = conn.execute(
            "SELECT id, change_type, reason, source, changed_at, exercise, weight, reps, rpe, notes, performed_at "
            "FROM set_history WHERE set_id = ? ORDER BY id DESC",
            (int(set_id),),
        ).fetchall()
        superseded_by = conn.execute(
            "SELECT id FROM workout_sets WHERE supersedes_set_id = ? AND deleted_at IS NULL", (int(set_id),)
        ).fetchall()
    return {"current": dict(row), "history": [dict(h) for h in history],
            "superseded_by_set_ids": [r["id"] for r in superseded_by],
            "note": "history rows hold the values as they were BEFORE each change, newest first"}


_SESSION_EDITABLE = ("notes", "program", "started_at", "ended_at")


def update_session(session_id, reason: str = "", source: str = "mcp", notes_mode: str = "replace", **fields) -> dict:
    """Edit session notes/program/times. The prior row is written to session_history first.
    notes_mode 'append' adds to the existing notes instead of replacing them."""
    changes = {k: v for k, v in fields.items() if k in _SESSION_EDITABLE and v is not None}
    if not changes:
        return {"error": f"Nothing to update. Editable fields: {list(_SESSION_EDITABLE)}"}
    with get_db() as conn:
        row = _find_session(conn, session_id, include_deleted=True)
        if not row:
            return {"error": f"No session {session_id}"}
        if "notes" in changes and notes_mode == "append" and row.get("notes"):
            changes["notes"] = f"{row['notes']}\n{changes['notes']}"
        changes = {k: v for k, v in changes.items() if str(row.get(k)) != str(v)}
        if not changes:
            return {"status": "unchanged", "session": row}
        _snapshot_session_row(conn, row, "update", reason=reason, source=source)
        assignments = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", tuple(changes.values()) + (row["id"],))
        after = _find_session(conn, row["id"], include_deleted=True)
    return {"status": "updated", "id": row["id"], "session_id": row["session_id"], "changed": list(changes),
            "before": {k: row.get(k) for k in changes}, "after": {k: after.get(k) for k in changes}}


def get_session_history(session_id) -> dict:
    with get_db() as conn:
        row = _find_session(conn, session_id, include_deleted=True)
        if not row:
            return {"error": f"No session {session_id}"}
        history = conn.execute(
            "SELECT id, change_type, reason, source, changed_at, snapshot FROM session_history WHERE session_row_id = ? ORDER BY id DESC",
            (row["id"],),
        ).fetchall()
    row["planned_workout_snapshot"] = _load_json(row.get("planned_workout_snapshot"))
    out = []
    for h in history:
        h = dict(h)
        snap = _load_json(h.pop("snapshot")) or {}
        snap.pop("planned_workout_snapshot", None)
        h["before"] = snap
        out.append(h)
    return {"current": row, "history": out}


def get_session_log(include_deleted: bool = False) -> list:
    """Return session log with training days, gaps, and the integer id of every set."""
    where = "" if include_deleted else "WHERE s.deleted_at IS NULL"
    with get_db() as conn:
        try:
            rows = conn.execute(f"""
                SELECT s.id, s.session_id, COALESCE(s.session_date, s.date) as day,
                       s.program, s.started_at, s.ended_at,
                       s.notes, s.is_backfill, s.source, s.deleted_at
                FROM sessions s
                {where}
                ORDER BY COALESCE(s.session_date, s.date) DESC, s.id DESC
                LIMIT 30
            """).fetchall()

            results = []
            prev_date = None
            for r in rows:
                entry = dict(r)
                if not include_deleted:
                    entry.pop("deleted_at", None)
                split = _session_sets(conn, entry["session_id"], include_deleted)
                entry["total_sets"] = len(split["sets"])
                entry["set_ids"] = [s["id"] for s in split["sets"]]
                if split["superseded_sets"]:
                    entry["superseded_set_ids"] = [s["id"] for s in split["superseded_sets"]]
                if include_deleted:
                    entry["deleted_set_ids"] = [s["id"] for s in split["deleted_sets"]]
                entry["duration_min"] = _duration_min(entry["started_at"], entry["ended_at"])
                if prev_date and entry['day']:
                    d1 = datetime.strptime(prev_date, '%Y-%m-%d')
                    d2 = datetime.strptime(entry['day'], '%Y-%m-%d')
                    entry['days_since_previous'] = (d1 - d2).days
                else:
                    entry['days_since_previous'] = None
                prev_date = entry['day']
                results.append(entry)

            return results
        except Exception as e:
            print(f'[database] get_session_log error: {e}')
            return []


def get_planned_vs_actual(session_id=None, date: str = None) -> dict:
    """The plan frozen at session start next to what was actually done (analytic sets only).
    Actual-only when no plan existed."""
    with get_db() as conn:
        if session_id:
            session = _find_session(conn, session_id)
        else:
            date = date or _local_today()
            row = conn.execute(
                "SELECT * FROM sessions WHERE COALESCE(session_date, date) = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (date,),
            ).fetchone()
            session = dict(row) if row else None
        if not session:
            return {"error": "No session found"}
        sets = _session_sets(conn, session["session_id"])["sets"]
    plan = _load_json(session.get("planned_workout_snapshot"))
    actual = {}
    for s in sets:
        actual.setdefault(s["exercise"], []).append(
            {k: s[k] for k in ("id", "weight", "reps", "rpe", "notes", "performed_at")})
    comparison = []
    if plan:
        seen = set()
        for section in ("warmup", "exercises", "carry_forward"):
            for ex in plan.get(section, []) or []:
                name = ex.get("name", "") if isinstance(ex, dict) else str(ex)
                done = next((v for k, v in actual.items() if k.lower() == name.lower()), [])
                seen.add(name.lower())
                comparison.append({"section": section, "exercise": name,
                                   "planned": ex, "actual_sets": done,
                                   "planned_sets": (ex.get("sets") if isinstance(ex, dict) else None),
                                   "actual_set_count": len(done)})
        for k, v in actual.items():
            if k.lower() not in seen:
                comparison.append({"section": "unplanned", "exercise": k, "planned": None,
                                   "actual_sets": v, "planned_sets": 0, "actual_set_count": len(v)})
    return {"session_id": session["session_id"], "id": session["id"],
            "date": session.get("session_date") or session.get("date"), "program": session.get("program"),
            "mode": "planned_vs_actual" if plan else "actual_only",
            "plan": plan, "actual": actual, "comparison": comparison}



# ---------------------------------------------------------------------------
# Robust Chat History
# ---------------------------------------------------------------------------

def log_chat_message(
    role: str,
    content: str,
    message_type: str = "general",
    exercise_context: str = None,
    tags: list = None,
    sentiment: str = "neutral",
    source: str = "typed",
    session_date: str = None,
) -> dict:
    """Log a chat message with rich metadata."""
    session_date = session_date or date.today().isoformat()
    now = datetime.now().isoformat()
    tags_json = json.dumps(tags or [])
    
    with get_db() as conn:
        conn.execute(
            """INSERT INTO chat_history 
               (session_date, timestamp, role, content, message_type, exercise_context, tags, sentiment, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_date, now, role, content, message_type, exercise_context, tags_json, sentiment, source)
        )
    
    return {"status": "logged", "timestamp": now}


def get_recent_chat_history(limit: int = 50, days: int = 7) -> list:
    """Get recent chat messages for context injection."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT session_date, timestamp, role, content, message_type, 
                      exercise_context, tags, sentiment, source
               FROM chat_history 
               WHERE session_date >= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (cutoff, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def search_chat_history(
    query: str = None,
    tags: list = None,
    message_type: str = None,
    exercise: str = None,
    sentiment: str = None,
    days: int = 30,
    limit: int = 20,
) -> list:
    """Search chat history with flexible filters."""
    conditions = []
    params = []
    
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conditions.append("session_date >= ?")
    params.append(cutoff)
    
    if query:
        conditions.append("content LIKE ?")
        params.append(f"%{query}%")
    
    if tags:
        for tag in tags:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")
    
    if message_type:
        conditions.append("message_type = ?")
        params.append(message_type)
    
    if exercise:
        conditions.append("exercise_context LIKE ?")
        params.append(f"%{exercise}%")
    
    if sentiment:
        conditions.append("sentiment = ?")
        params.append(sentiment)
    
    where = " AND ".join(conditions) if conditions else "1=1"
    
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT session_date, timestamp, role, content, message_type,
                       exercise_context, tags, sentiment
                FROM chat_history 
                WHERE {where}
                ORDER BY timestamp DESC LIMIT ?""",
            params + [limit]
        ).fetchall()
    return [dict(r) for r in rows]


def get_chat_history_for_prompt(limit: int = 30, days: int = 7) -> str:
    """Build a text block of recent chat history for system prompt injection."""
    messages = get_recent_chat_history(limit, days)
    if not messages:
        return "CHAT HISTORY: No recent conversations logged."
    
    lines = ["RECENT CONVERSATION HISTORY (last 7 days):"]
    messages.reverse()  # chronological order
    
    current_date = None
    for msg in messages:
        if msg["session_date"] != current_date:
            current_date = msg["session_date"]
            lines.append(f"\n--- {current_date} ---")
        
        role_label = _get_athlete_name() if msg["role"] == "user" else "JARVIS"
        tags = json.loads(msg["tags"]) if msg["tags"] else []
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        
        # Truncate long messages for prompt efficiency
        content = msg["content"][:200] + "..." if len(msg["content"]) > 200 else msg["content"]
        lines.append(f"  {role_label}: {content}{tag_str}")
    
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# V2 Schema Migration (runs inline on startup, idempotent)
# ---------------------------------------------------------------------------
def _migrate_v2():

    """Add calendar_days, update sessions schema, backfill real sessions."""
    with get_db() as conn:
        tables = []
        try:
            if USE_PG:
                rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()
                tables = [r.get("table_name", "") if isinstance(r, dict) else r[0] for r in rows]
            else:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        except Exception as e:
            print(f"[DB] Table list error: {e}")
        
        # Check if backfill already done (safely)
        try:
            if USE_PG:
                # Check if sessions table has is_backfill column
                row = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sessions' AND column_name='is_backfill'").fetchone()
                if row:
                    cnt = conn.execute("SELECT COUNT(*) as cnt FROM sessions WHERE is_backfill = TRUE").fetchone()
                    if cnt and (cnt.get('cnt', 0) if isinstance(cnt, dict) else cnt[0]) >= 3:
                        print('[database] V2 migration already complete')
                        return
            else:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
                if 'is_backfill' in cols and 'session_date' in cols:
                    _r = conn.execute("SELECT COUNT(*) as cnt FROM sessions WHERE is_backfill = 1").fetchone()
                    count = _r['cnt'] if isinstance(_r, dict) else _r[0]
                    if count >= 3:
                        print('[database] V2 migration already complete')
                        return
        except Exception as e:
            print(f'[database] Migration check: {e}')
        
        print("[database] Running V2 migration...")
        
        # 1. Create calendar_days
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS calendar_days (
                day TEXT PRIMARY KEY,
                day_of_year INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                weekday_name TEXT NOT NULL,
                is_weekend INTEGER NOT NULL DEFAULT 0
            );
        """)
        
        # Populate calendar_days 2026-2027
        conn.execute("""
            INSERT OR IGNORE INTO calendar_days (day, day_of_year, day_of_week, weekday_name, is_weekend)
            WITH RECURSIVE dates(d) AS (
                VALUES('2026-01-01')
                UNION ALL
                SELECT date(d, '+1 day') FROM dates WHERE d < '2027-12-31'
            )
            SELECT d,
                CAST(strftime('%j', d) AS INTEGER),
                CAST(strftime('%w', d) AS INTEGER),
                CASE CAST(strftime('%w', d) AS INTEGER)
                    WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday'
                    WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday'
                    WHEN 6 THEN 'Saturday' END,
                CASE WHEN CAST(strftime('%w', d) AS INTEGER) IN (0, 6) THEN 1 ELSE 0 END
            FROM dates
        """)
        
        # 2. Add new columns to sessions if missing
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if 'session_date' not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN session_date TEXT")
        if 'is_backfill' not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN is_backfill INTEGER DEFAULT 0")
        if 'created_at' not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT DEFAULT ''")
        
        # Backfill session_date from date column for existing rows
        conn.execute("UPDATE sessions SET session_date = date WHERE session_date IS NULL AND date IS NOT NULL")
        
        # 3. Add set_index to workout_sets if missing
        set_cols = [r[1] for r in conn.execute("PRAGMA table_info(workout_sets)").fetchall()]
        if 'set_index' not in set_cols:
            conn.execute("ALTER TABLE workout_sets ADD COLUMN set_index INTEGER")
        if 'logged_at' not in set_cols:
            conn.execute("ALTER TABLE workout_sets ADD COLUMN logged_at TEXT")
        
        # 4. Create views
        conn.execute("DROP VIEW IF EXISTS v_sessions")
        conn.execute("""
            CREATE VIEW v_sessions AS
            SELECT s.*,
                CASE WHEN s.started_at IS NOT NULL AND s.ended_at IS NOT NULL
                    THEN ROUND((julianday(s.ended_at) - julianday(s.started_at)) * 1440, 1)
                    ELSE NULL END AS duration_min,
                CASE WHEN s.started_at IS NOT NULL THEN
                    CASE WHEN CAST(strftime('%H', s.started_at) AS INTEGER) < 12 THEN 'morning'
                         WHEN CAST(strftime('%H', s.started_at) AS INTEGER) < 17 THEN 'afternoon'
                         ELSE 'evening' END
                    ELSE NULL END AS time_of_day
            FROM sessions s
        """)
        
        conn.execute("DROP VIEW IF EXISTS v_session_log")
        conn.execute("""
            CREATE VIEW v_session_log AS
            SELECT 
                cd.day, cd.weekday_name, cd.is_weekend,
                s.id as session_id, s.program,
                s.started_at, s.ended_at, s.notes as session_notes,
                s.is_backfill,
                (SELECT COUNT(*) FROM workout_sets ws WHERE ws.session_id = CAST(s.id AS TEXT)) as total_sets
            FROM calendar_days cd
            LEFT JOIN sessions s ON s.session_date = cd.day
            WHERE cd.day BETWEEN '2026-09-01' AND '2026-12-31'
            ORDER BY cd.day DESC
        """)
        
        # 5. (Removed) This step used to run DELETE FROM workout_sets / sessions whenever
        # fewer than 3 backfill sessions were found — one deleted backfill session would
        # have wiped every logged workout on the next boot. Nothing here may destroy data.

        # 6. Backfill 3 real sessions (skips dates that already have a backfill session)
        _backfill_sessions(conn)
        
        print("[database] V2 migration complete — 3 sessions backfilled")


def _backfill_sessions(conn):
    """Insert the 3 real workout sessions."""
    
    sessions_data = [
        {
            "date": "2026-09-14",
            "program": "Strength A",
            "notes": "~60 min, evening. Bone-on-bone at start, no flare, thoracic tension only.",
            "sets": [
                ("Goblet squat to box", "30", "8", "", ""),
                ("Goblet squat to box", "30", "8", "", ""),
                ("Goblet squat to box", "30", "8", "", ""),
                ("Cable pull-through", "60", "10", "", "single pulley, easy"),
                ("Cable pull-through", "100", "10", "", "single pulley, easy"),
                ("Cable pull-through", "140", "10", "", "single pulley, easy"),
                ("Seated low row", "140", None, "", "dual pulley ~70 lb effective; reps not recorded"),
                ("Seated low row", "140", None, "", "dual pulley ~70 lb effective; reps not recorded"),
                ("Seated low row", "140", None, "", "dual pulley ~70 lb effective; reps not recorded"),
                ("Push-ups", "bodyweight", "25", "", ""),
                ("Push-ups", "bodyweight", "15", "", ""),
                ("Push-ups", "bodyweight", "12", "", ""),
                ("Pallof press", "60", "10", "", "single pulley, half-kneeling, per side"),
                ("Pallof press", "60", "10", "", "single pulley, half-kneeling, per side"),
                ("Pallof press", "60", "10", "", "single pulley, half-kneeling, per side"),
                ("Side plank", "bodyweight", None, "", "20s hold"),
                ("Side plank", "bodyweight", None, "", "20s hold"),
                ("Side plank", "bodyweight", None, "", "20s hold"),
                ("Hollow body hold", "bodyweight", None, "", "20s hold"),
                ("Hollow body hold", "bodyweight", None, "", "20s hold"),
                ("Hollow body hold", "bodyweight", None, "", "20s hold"),
            ]
        },
        {
            "date": "2026-09-15",
            "program": "Strength B",
            "notes": "Partial — cut short for a call, not fatigue. Back fine throughout.",
            "sets": [
                ("Step-ups (20in box)", "15 lb each", "8", "", "per side"),
                ("Step-ups (20in box)", "20 lb each", "10", "", "per side, ~6-7 RIR"),
                ("Step-ups (20in box)", "20 lb each", "10", "", "per side, ~6-7 RIR"),
                ("Half-kneeling 1-arm press", "20", "15", "", "too light"),
                ("Half-kneeling 1-arm press", "30", "8", "", "left side caught at 30"),
                ("Half-kneeling 1-arm press", "25", "8", "", "25 is the working cap"),
                ("DB hip thrust", "30", "10", "", ""),
                ("DB hip thrust", "50", "10", "", "no back issue at top"),
                ("DB hip thrust", "50", "10", "", "no back issue at top"),
                ("Neutral-grip pulldown", "140", "10", "", "dual pulley, very easy"),
                ("Neutral-grip pulldown", "200", "12", "", "dual pulley, easy"),
                ("Neutral-grip pulldown", "238", "10", "", "dual pulley; limiter is anchoring, not lats"),
            ]
        },
        {
            "date": "2026-09-17",
            "program": "Strength A",
            "notes": "Evening. 41 min at the four-lift mark. Row form correction mid-session: elbows to ribs, blades down first, cap 8-10 reps. Push-up cue is now glutes-squeezed-first.",
            "sets": [
                ("Goblet squat to box", "50", "10", "", ""),
                ("Goblet squat to box", "30", "8", "", "mis-dialed, intended 60"),
                ("Goblet squat to box", "60", "8", "", "working weight is 60, even dials"),
                ("Cable pull-through", "160", "10", "", "single pulley at 4:1"),
                ("Cable pull-through", "180", "10", "", "single pulley at 4:1"),
                ("Cable pull-through", "200", "10", "", "single pulley at 4:1 — ratio is the ceiling, switch to 2:1"),
                ("Seated low row", "160", "18", "", ""),
                ("Seated low row", "200", "10", "", ""),
                ("Seated low row", "200", "10", "", "neck tension from rep 8 on this set"),
                ("Push-ups", "bodyweight", "28", "", ""),
                ("Push-ups", "bodyweight", "20", "", ""),
                ("Push-ups", "bodyweight", "12", "", ""),
                ("Pallof press", "80", "10", "", "single pulley, half-kneeling, per side"),
                ("Pallof press", "80", "10", "", "single pulley, half-kneeling, per side"),
                ("Pallof press", "80", "10", "", "single pulley, half-kneeling, per side"),
                ("Side plank", "bodyweight", None, "", "20s hold"),
                ("Side plank", "bodyweight", None, "", "20s hold"),
                ("Side plank", "bodyweight", None, "", "20s hold"),
                ("Hollow body hold", "bodyweight", None, "", "20s hold"),
                ("Hollow body hold", "bodyweight", None, "", "20s hold"),
                ("Hollow body hold", "bodyweight", None, "", "20s hold"),
            ]
        },
    ]
    
    now = datetime.now().isoformat()
    
    for sess in sessions_data:
        # Check if already exists
        existing = conn.execute(
            "SELECT id FROM sessions WHERE session_date = ? AND is_backfill = 1",
            (sess["date"],)
        ).fetchone()
        if existing:
            continue
        
        text_session_id = f"backfill_{sess['date']}"
        conn.execute(
            "INSERT INTO sessions (session_id, date, session_date, program, started_at, notes, is_backfill, created_at) VALUES (?, ?, ?, ?, NULL, ?, 1, ?)",
            (text_session_id, sess["date"], sess["date"], sess["program"], sess["notes"], now)
        )
        session_id = text_session_id
        
        for idx, (exercise, weight, reps, rpe, notes) in enumerate(sess["sets"], 1):
            conn.execute(
                "INSERT INTO workout_sets (session_id, exercise, weight, reps, rpe, notes, timestamp, set_index, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(session_id), exercise, weight, reps or "", rpe, notes, now, idx, now)
            )


try:
    _migrate_v2()
except Exception as e:
    print(f"[database] V2 migration failed (non-fatal): {e}")

def _seed_bodyweight():
    try:
        with get_db() as conn:
            _r2 = conn.execute('SELECT COUNT(*) as cnt FROM bodyweight').fetchone()
            count = _r2['cnt'] if isinstance(_r2, dict) else _r2[0]
            if count == 0:
                from datetime import datetime
                now = datetime.now().isoformat()
                conn.execute('INSERT INTO bodyweight (date, weight_lbs, notes, timestamp) VALUES (?, ?, ?, ?)',
                    ('2026-01-15', 218, 'Starting weight before cut', now))
                conn.execute('INSERT INTO bodyweight (date, weight_lbs, notes, timestamp) VALUES (?, ?, ?, ?)',
                    ('2026-06-01', 194, 'Low point before going dormant', now))
                conn.execute('INSERT INTO bodyweight (date, weight_lbs, notes, timestamp) VALUES (?, ?, ?, ?)',
                    ('2026-09-14', 200, 'Approximate current weight', now))
                print('[database] Seeded bodyweight history')
    except Exception as e:
        print(f'[database] Bodyweight seed skipped: {e}')

_seed_bodyweight()


# ---------------------------------------------------------------------------
# Exercise Modifications (skip/replace)
# ---------------------------------------------------------------------------

def log_exercise_modification(
    session_id: str,
    original_exercise: str,
    action: str,
    replacement_exercise: str = None,
    replacement_weight: str = None,
    replacement_reps: str = None,
    replacement_sets: int = None,
    reason: str = "",
) -> dict:
    """Log a skip or replace action for an exercise."""
    if session_id is None:
        session_id = get_or_create_today_session()
    
    with get_db() as conn:
        conn.execute(
            """INSERT INTO exercise_modifications 
               (session_id, original_exercise, action, replacement_exercise,
                replacement_weight, replacement_reps, replacement_sets, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, original_exercise, action, replacement_exercise,
             replacement_weight, replacement_reps, replacement_sets, reason)
        )
    
    return {
        "status": "logged",
        "action": action,
        "original": original_exercise,
        "replacement": replacement_exercise,
    }


def get_session_modifications(session_id: str = None) -> list:
    """Get all exercise modifications for a session."""
    if session_id is None:
        today = _local_today()
        with get_db() as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE date = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (today,)
            ).fetchone()
            if row:
                session_id = row["session_id"]
            else:
                return []
    
    with get_db() as conn:
        rows = conn.execute(
            """SELECT original_exercise, action, replacement_exercise,
                      replacement_weight, replacement_reps, replacement_sets, reason
               FROM exercise_modifications WHERE session_id = ? ORDER BY id""",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
# PROGRESS PHOTOS
# ═══════════════════════════════════════════════════════════════════════════════

# These used to call get_db() without `with` (it's a context manager), so every photo
# endpoint 500'd. They also stamp date/timestamp explicitly: the table's DEFAULTs were
# written for SQLite and the server's UTC clock would put evening photos on tomorrow.

def save_progress_photo(photo_bytes: bytes, angle: str = "front", bodyweight: float = None, notes: str = "",
                        mime_type: str = "image/jpeg", photo_date: str = None) -> int:
    """Save a progress photo to the database. Returns the photo ID."""
    photo_date = photo_date if photo_date and _valid_date(photo_date) else _local_today()
    with get_db() as conn:
        row = conn.execute(
            """INSERT INTO progress_photos (date, timestamp, angle, bodyweight, notes, mime_type, photo_data, is_demo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (photo_date, _now_ts(), angle, bodyweight, notes, mime_type,
             psycopg2.Binary(photo_bytes) if USE_PG else photo_bytes, demo_flag()),
        ).fetchone()
    return row["id"]


def get_progress_photos(limit: int = 20, date_from: str = None, date_to: str = None) -> list:
    """Get progress photo metadata (without photo data). Returns list of dicts."""
    query = "SELECT id, date, timestamp, angle, bodyweight, notes, mime_type, is_demo, length(photo_data) as size_bytes FROM progress_photos"
    params = []
    conditions = []
    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def get_progress_photo_data(photo_id: int) -> dict:
    """Get a single progress photo with its binary data (always bytes)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, date, timestamp, angle, bodyweight, notes, mime_type, photo_data FROM progress_photos WHERE id = ?",
            (photo_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["photo_data"] = bytes(d["photo_data"]) if d.get("photo_data") is not None else b""
    return d


def delete_progress_photo(photo_id: int) -> bool:
    """Delete a progress photo. Returns True if found and deleted."""
    with get_db() as conn:
        n = conn.execute("DELETE FROM progress_photos WHERE id = ?", (photo_id,)).rowcount
    return n > 0


# ═══════════════════════════════════════════════════════════════════════════════

def _raw_conn():
    """Get a raw connection (not context managed). Works with both PG and SQLite."""
    if USE_PG:
        return PGConnectionWrapper(_pg_conn())
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn

# PLANNED WORKOUT SCHEDULE (2-week programs from Claude Desktop project)
# ═══════════════════════════════════════════════════════════════════════════════

def save_planned_schedule(schedule: list) -> dict:
    """
    Save a multi-day workout schedule from the Claude Desktop project.
    Each item in schedule should have: date, program_name, exercises (list), warmup (list), notes.

    Past plans are immutable: any date before today (local) rejects the whole request.
    A new plan for a date supersedes the previous one (superseded_at is stamped; the old row stays).
    """
    today = _local_today()
    bad = [d.get("date") for d in schedule if d.get("date") and not _valid_date(d["date"])]
    if bad:
        return {"error": f"Invalid dates (need YYYY-MM-DD): {bad}", "saved": 0}
    past = sorted({d["date"] for d in schedule if d.get("date") and d["date"] < today})
    if past:
        return {"error": f"Past plans are immutable — refusing dates before {today}: {past}. Nothing was saved.",
                "rejected_dates": past, "saved": 0}

    saved = 0
    stamp = _now_ts()
    with get_db() as conn:
        for day in schedule:
            planned_date = day.get("date")
            if not planned_date:
                continue

            # Supersede (not delete) the existing plan for this date
            conn.execute(
                "UPDATE planned_workouts SET superseded_at = ? WHERE planned_date = ? AND superseded_at IS NULL",
                (stamp, planned_date),
            )

            workout_data = json.dumps({
                "warmup": day.get("warmup", []),
                "exercises": day.get("exercises", []),
                "carry_forward": day.get("carry_forward", []),
                "description": day.get("description", ""),
            })

            conn.execute(
                """INSERT INTO planned_workouts (planned_date, program_name, workout_data, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (planned_date, day.get("program_name", ""), workout_data, day.get("notes", ""), stamp, stamp)
            )
            saved += 1

    return {"saved": saved, "dates": [d["date"] for d in schedule if d.get("date")]}


_CURRENT_PLAN = "superseded_at IS NULL"


def _plan_row_to_dict(row) -> dict:
    workout_data = _load_json(row["workout_data"]) or {}
    return {
        "id": row["id"],
        "date": row["planned_date"],
        "program_name": row["program_name"],
        "warmup": workout_data.get("warmup", []),
        "exercises": workout_data.get("exercises", []),
        "carry_forward": workout_data.get("carry_forward", []),
        "description": workout_data.get("description", ""),
        "status": row["status"],
        "notes": row["notes"],
    }


def get_planned_workout(target_date: str = None) -> dict:
    """
    Get the planned workout for a specific date (defaults to today in local timezone).
    If no plan exists for that date, returns None.
    """
    if not target_date:
        target_date = _local_today()
    try:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT * FROM planned_workouts WHERE planned_date = ? AND {_CURRENT_PLAN} ORDER BY id DESC LIMIT 1",
                (target_date,)
            ).fetchone()
    except Exception:
        return None
    return _plan_row_to_dict(row) if row else None


def get_planned_schedule(days: int = 14) -> list:
    """Get the full planned schedule for the next N days."""
    today = _local_now().date()
    end_date = today + timedelta(days=int(days))
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""SELECT * FROM planned_workouts
                   WHERE planned_date >= ? AND planned_date <= ? AND {_CURRENT_PLAN}
                   ORDER BY planned_date ASC, id ASC""",
                (today.isoformat(), end_date.isoformat())
            ).fetchall()
    except Exception:
        return []
    return [_plan_row_to_dict(r) for r in rows]


def mark_planned_workout_done(planned_date: str, session_id: str = None) -> bool:
    """Mark a planned workout as completed."""
    try:
        with get_db() as conn:
            cursor = conn.execute(
                f"""UPDATE planned_workouts SET status = 'completed', actual_session_id = ?,
                   updated_at = ? WHERE planned_date = ? AND {_CURRENT_PLAN}""",
                (session_id, _now_ts(), planned_date)
            )
            return cursor.rowcount > 0
    except Exception:
        return False


def mark_planned_workout_skipped(planned_date: str, reason: str = "") -> bool:
    """Mark a planned workout as skipped. The skip reason is appended to the plan's notes."""
    try:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT id, notes FROM planned_workouts WHERE planned_date = ? AND {_CURRENT_PLAN} ORDER BY id DESC LIMIT 1",
                (planned_date,),
            ).fetchone()
            if not row:
                return False
            notes = row["notes"] or ""
            if reason:
                notes = f"{notes}\nSkipped: {reason}".strip()
            cursor = conn.execute(
                "UPDATE planned_workouts SET status = 'skipped', notes = ?, updated_at = ? WHERE id = ?",
                (notes, _now_ts(), row["id"])
            )
            return cursor.rowcount > 0
    except Exception:
        return False


def get_workout_compliance(days: int = 14) -> dict:
    """Get compliance stats: planned vs completed vs skipped."""
    _now2 = _local_now().date()
    start = (_now2 - timedelta(days=int(days))).isoformat()
    today = _now2.isoformat()
    result = {"planned": 0, "completed": 0, "skipped": 0, "pending": 0}
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT status, COUNT(*) as cnt FROM planned_workouts WHERE planned_date >= ? AND planned_date <= ? AND {_CURRENT_PLAN} GROUP BY status",
                (start, today)
            ).fetchall()
    except Exception:
        return result
    for r in rows:
        result[r["status"]] = r["cnt"]
        result["planned"] += r["cnt"]

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# COACHING CONTEXT (rich knowledge base synced from Claude Desktop project)
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_context_table():
    """Create coaching_context table if it doesn't exist."""
    conn = _raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coaching_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT DEFAULT 'project',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(category, key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_cat ON coaching_context(category)")
    conn.commit()
    conn.close()


def upsert_context(category: str, key: str, content: str, source: str = "project") -> dict:
    """
    Insert or update a context document.
    Categories: 'injury', 'form', 'programming', 'profile', 'preferences', 'equipment', 'history', 'coaching_notes'
    Key: specific topic within category (e.g., 'left_shoulder', 'squat_depth', 'progressive_overload_rules')
    Content: free-text knowledge from the project
    """
    _ensure_context_table()
    conn = _raw_conn()
    conn.execute("""
        INSERT INTO coaching_context (category, key, content, source, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(category, key) DO UPDATE SET
            content = excluded.content,
            source = excluded.source,
            updated_at = datetime('now')
    """, (category, key, content, source))
    conn.commit()
    conn.close()
    return {"ok": True, "category": category, "key": key}


def get_context(category: str = None, key: str = None) -> list:
    """Get context documents, optionally filtered by category and/or key."""
    _ensure_context_table()
    conn = _raw_conn()
    query = "SELECT category, key, content, source, updated_at FROM coaching_context"
    params = []
    conditions = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if key:
        conditions.append("key = ?")
        params.append(key)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY category, key"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_full_coaching_context() -> str:
    """
    Build a complete context document for JARVIS's system prompt.
    Returns a formatted text block with all coaching knowledge.
    """
    _ensure_context_table()
    docs = get_context()
    if not docs:
        return ""
    
    sections = {}
    for doc in docs:
        cat = doc["category"]
        if cat not in sections:
            sections[cat] = []
        sections[cat].append(f"[{doc['key']}] {doc['content']}")
    
    lines = ["=== COACHING CONTEXT (synced from project) ==="]
    for cat, items in sections.items():
        lines.append(f"\n--- {cat.upper().replace('_', ' ')} ---")
        for item in items:
            lines.append(item)
    lines.append("\n=== END COACHING CONTEXT ===")
    return "\n".join(lines)


def delete_context(category: str, key: str) -> bool:
    """Delete a specific context document."""
    _ensure_context_table()
    conn = _raw_conn()
    cursor = conn.execute(
        "DELETE FROM coaching_context WHERE category = ? AND key = ?",
        (category, key)
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# PLANNED VS ACTUAL — EXERCISE DEVIATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_deviations_table():
    """Create set_deviations table if it doesn't exist (columns added by _migrate_integrity)."""
    _run_ddl(_SET_DEVIATIONS_DDL)
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_deviations_session ON set_deviations(session_id)")
    _run_ddl("CREATE INDEX IF NOT EXISTS idx_deviations_exercise ON set_deviations(exercise)")


# Deviations that still describe the record: not replaced by a newer auto-evaluation,
# and not attached to a set that has since been deleted or superseded.
_CURRENT_DEVIATION = (
    "d.superseded_at IS NULL AND (d.set_id IS NULL OR EXISTS ("
    f"SELECT 1 FROM workout_sets ws WHERE ws.id = d.set_id AND {ANALYTIC_SET}))"
)


def log_deviation(exercise: str, planned_weight: str = "", planned_reps: str = "",
                  actual_weight: str = "", actual_reps: str = "",
                  deviation_type: str = "other", reason: str = "",
                  planned_notes: str = "", actual_notes: str = "",
                  set_number: int = 1, session_id: str = None, set_id: int = None,
                  source: str = "manual") -> dict:
    """
    Log a deviation between planned and actual exercise performance.
    deviation_type: intensity_reduction, volume_reduction, exercise_swap, early_stop,
                    weight_increase, weight_decrease, form_modification, skipped, other
    """
    with get_db() as conn:
        row = conn.execute(
            """INSERT INTO set_deviations
               (session_id, exercise, set_number, planned_weight, planned_reps, planned_notes,
                actual_weight, actual_reps, actual_notes, deviation_type, reason, set_id, source, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (session_id, exercise, int(set_number or 1), planned_weight, planned_reps, planned_notes,
             actual_weight, actual_reps, actual_notes, deviation_type, reason,
             int(set_id) if set_id is not None else None, source, _now_ts())
        ).fetchone()
    return {"ok": True, "id": row["id"], "exercise": exercise, "deviation_type": deviation_type}


def _first_number(value):
    import re as _re
    m = _re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else None


def _rep_range(value):
    """'8-10' -> (8, 10); '10/side' -> (10, 10); 'max' / '20s hold' -> None (not comparable)."""
    import re as _re
    s = str(value or "").strip().lower()
    if not s or "max" in s or _re.search(r"\d\s*(s|sec|secs|seconds|min|mins|minutes)\b", s):
        return None
    m = _re.match(r"^\s*(\d+)\s*(?:-|–|to)\s*(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _re.match(r"^\s*(\d+)", s)
    return (int(m.group(1)), int(m.group(1))) if m else None


def _weight_comparable(value) -> bool:
    s = str(value or "").lower()
    return bool(s) and "%" not in s and "bodyweight" not in s and _first_number(s) is not None


def auto_log_set_deviation(set_id: int):
    """Compare a set with the plan frozen on its session (falling back to the date's current
    plan when the session predates snapshots) and log a deviation when they differ.

    Any earlier auto-deviation for this set is marked superseded (not deleted) first, so an
    edited set is always judged on its current values. Returns the deviation logged, or None.
    No plan -> actual-only, nothing is logged.
    """
    with get_db() as conn:
        s = conn.execute("SELECT * FROM workout_sets WHERE id = ?", (int(set_id),)).fetchone()
        if not s:
            return None
        s = dict(s)
        sess = conn.execute(
            "SELECT session_id, COALESCE(session_date, date) AS day, planned_workout_snapshot FROM sessions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (s["session_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE set_deviations SET superseded_at = ? WHERE set_id = ? AND source = ? AND superseded_at IS NULL",
            (_now_ts(), int(set_id), "auto"),
        )
        if s.get("deleted_at") or not sess:
            return None
        plan = _load_json(sess["planned_workout_snapshot"])
        if plan is None and sess["day"]:
            plan = get_planned_workout(sess["day"])
        if not plan:
            return None

        planned = None
        for section in ("exercises", "warmup", "carry_forward"):
            for ex in plan.get(section, []) or []:
                if isinstance(ex, dict) and str(ex.get("name", "")).strip().lower() == s["exercise"].strip().lower():
                    planned = ex
                    break
            if planned:
                break

        # Position of this set among the exercise's analytic sets
        prior = conn.execute(
            f"SELECT ws.id FROM workout_sets ws WHERE ws.session_id = ? AND ws.exercise = ? AND {ANALYTIC_SET} ORDER BY ws.id",
            (s["session_id"], s["exercise"]),
        ).fetchall()
        ids = [r["id"] for r in prior]
        set_number = ids.index(s["id"]) + 1 if s["id"] in ids else len(ids) + 1

    if planned is None:
        return log_deviation(
            exercise=s["exercise"], actual_weight=s["weight"], actual_reps=s["reps"],
            deviation_type="unplanned_exercise", reason="auto: exercise not in plan",
            set_number=set_number, session_id=s["session_id"], set_id=s["id"], source="auto")

    p_weight = str(planned.get("weight") or planned.get("working_weight") or "")
    p_reps = str(planned.get("reps") or "")
    try:
        p_sets = int(planned.get("sets") or 0)
    except (TypeError, ValueError):
        p_sets = 0

    findings = []
    dev_type = None
    if _weight_comparable(p_weight) and _weight_comparable(s["weight"]):
        pw, aw = _first_number(p_weight), _first_number(s["weight"])
        if aw != pw:
            dev_type = "weight_increase" if aw > pw else "weight_decrease"
            findings.append(f"weight {aw:g} vs planned {pw:g} ({aw - pw:+g} lb)")
    rng = _rep_range(p_reps)
    actual_reps = _first_number(s["reps"])
    if rng and actual_reps is not None:
        lo, hi = rng
        if actual_reps < lo:
            dev_type = dev_type or "volume_reduction"
            findings.append(f"reps {actual_reps:g} below planned {p_reps}")
        elif actual_reps > hi:
            dev_type = dev_type or "exceeded_prescription"
            findings.append(f"reps {actual_reps:g} above planned {p_reps}")
    if p_sets and set_number > p_sets:
        dev_type = dev_type or "exceeded_prescription"
        findings.append(f"set {set_number} beyond planned {p_sets} sets")

    if not findings:
        return None
    return log_deviation(
        exercise=s["exercise"], planned_weight=p_weight, planned_reps=p_reps,
        actual_weight=s["weight"], actual_reps=s["reps"], deviation_type=dev_type,
        reason="auto: " + "; ".join(findings), actual_notes=s.get("notes") or "",
        set_number=set_number, session_id=s["session_id"], set_id=s["id"], source="auto")


def get_session_deviations(session_id: str = None, date: str = None, include_superseded: bool = False) -> list:
    """Get deviations for a session or date (current ones only unless include_superseded)."""
    cond = "1=1" if include_superseded else _CURRENT_DEVIATION
    with get_db() as conn:
        if session_id:
            rows = conn.execute(
                f"SELECT d.* FROM set_deviations d WHERE d.session_id = ? AND {cond} ORDER BY d.timestamp",
                (session_id,)
            ).fetchall()
        elif date:
            rows = conn.execute(
                f"SELECT d.* FROM set_deviations d WHERE date(d.timestamp) = ? AND {cond} ORDER BY d.timestamp",
                (date,)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT d.* FROM set_deviations d WHERE {cond} ORDER BY d.timestamp DESC LIMIT 50"
            ).fetchall()
    return [dict(r) for r in rows]


def get_recent_deviations(days: int = 14) -> list:
    """Get current deviations from the past N days for nightly sync."""
    cutoff = (_local_now().date() - timedelta(days=int(days))).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT d.* FROM set_deviations d WHERE date(d.timestamp) >= ? AND {_CURRENT_DEVIATION} ORDER BY d.timestamp DESC",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
# EXERCISE SCHEMAS — defines input types and parameters per exercise
# ═══════════════════════════════════════════════════════════════════════════════

# Default schema definitions
EXERCISE_TYPES = {
    "strength_standard": {
        "label": "Standard Strength",
        "fields": ["weight", "reps", "rpe"],
        "units": {"weight": "lb", "reps": "reps", "rpe": "RPE"},
        "defaults": {"rpe": ""},
    },
    "strength_unilateral": {
        "label": "Unilateral Strength",
        "fields": ["weight", "reps_per_side", "rpe"],
        "units": {"weight": "lb each", "reps_per_side": "reps/side", "rpe": "RPE"},
        "defaults": {"rpe": ""},
    },
    "treadmill": {
        "label": "Treadmill",
        "fields": ["speed", "incline", "duration"],
        "units": {"speed": "mph", "incline": "%", "duration": "min"},
        "defaults": {"speed": "3.0", "incline": "2"},
    },
    "duration_hold": {
        "label": "Timed Hold",
        "fields": ["duration"],
        "units": {"duration": "sec"},
        "defaults": {},
    },
    "mobility": {
        "label": "Mobility/Movement",
        "fields": ["reps"],
        "units": {"reps": "reps"},
        "defaults": {},
        "quick_complete": True,
    },
    "mobility_bilateral": {
        "label": "Mobility (Each Side)",
        "fields": ["reps_per_side"],
        "units": {"reps_per_side": "each side"},
        "defaults": {},
        "quick_complete": True,
    },
    "carry": {
        "label": "Carry",
        "fields": ["weight", "distance"],
        "units": {"weight": "lb", "distance": "steps"},
        "defaults": {},
    },
    "cardio_zone": {
        "label": "Zone Cardio",
        "fields": ["duration", "hr_target"],
        "units": {"duration": "min", "hr_target": "bpm"},
        "defaults": {"hr_target": "130-140"},
    },
    "test": {
        "label": "Assessment Test",
        "fields": ["result"],
        "units": {"result": ""},
        "defaults": {},
    },
}

# Map exercises to their types
EXERCISE_SCHEMA_MAP = {
    # Warm-ups
    "Treadmill Walk/Jog": "treadmill",
    "Arm Circles": "mobility",
    "Leg Swings": "mobility_bilateral",
    "Hip Openers": "mobility_bilateral",
    "Band Pull-Aparts": "mobility",
    "Warm-Up Set 1": "strength_standard",
    "Warm-Up Set 2": "strength_standard",
    # Ironforge
    "Goblet Squat to Box": "strength_standard",
    "Cable Pull-Through": "strength_standard",
    "Seated Low Row": "strength_standard",
    "Push-Ups": "strength_standard",
    "Pallof Press": "strength_unilateral",
    "Side Plank": "duration_hold",
    "Hollow Body Hold": "duration_hold",
    # Arsenal
    "Step-Ups (20in box)": "strength_unilateral",
    "Half-Kneeling 1-Arm Press": "strength_unilateral",
    "DB Hip Thrust": "strength_standard",
    "Neutral-Grip Pulldown": "strength_standard",
    # Carry-forward
    "Chin-Up Test": "test",
    "Face Pulls": "strength_standard",
    "Bird Dog": "mobility_bilateral",
    "Suitcase Carry": "carry",
    "Dead Hang": "duration_hold",
    # PT exercises
    "Y-Balance": "test",
    "Long-Lever Glute Bridge": "strength_standard",
    "Ankle Isometrics": "duration_hold",
    "Bulgarian Split Squat": "strength_unilateral",
    "Seated Hamstring Curl": "strength_standard",
    "Single-Leg Calf Raise": "strength_unilateral",
    # Cardio
    "Treadmill incline walk": "treadmill",
    "Long easy walk": "cardio_zone",
    "Stretching strap work": "mobility",
    # General
    "Cat Cow": "mobility",
    "Glute Bridge": "strength_standard",
    "Wall Breathing": "mobility",
    "Flat DB Bench Press": "strength_standard",
    "Half-kneeling 1-arm cable row": "strength_unilateral",
}


def get_exercise_schema(exercise_name: str) -> dict:
    """Get the schema for an exercise. Falls back to strength_standard."""
    schema_type = EXERCISE_SCHEMA_MAP.get(exercise_name, "strength_standard")
    schema = EXERCISE_TYPES.get(schema_type, EXERCISE_TYPES["strength_standard"])
    return {
        "type": schema_type,
        "exercise": exercise_name,
        **schema,
    }


def get_all_exercise_schemas() -> dict:
    """Get the full schema map and type definitions."""
    return {
        "types": EXERCISE_TYPES,
        "exercises": EXERCISE_SCHEMA_MAP,
    }


def set_exercise_schema(exercise_name: str, schema_type: str) -> dict:
    """Set or update the schema type for an exercise."""
    if schema_type not in EXERCISE_TYPES:
        return {"error": f"Unknown type: {schema_type}. Valid: {list(EXERCISE_TYPES.keys())}"}
    EXERCISE_SCHEMA_MAP[exercise_name] = schema_type
    return {"ok": True, "exercise": exercise_name, "type": schema_type}


# ═══════════════════════════════════════════════════════════════════════════════
# BUDGET VS ACTUAL — AUTOMATIC VARIANCE TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

# Controlled vocabulary for deviation reasons
DEVIATION_REASONS = [
    "pain_or_symptom",
    "coach_directed_stop",
    "form_breakdown",
    "equipment",
    "time",
    "felt_light",
    "exceeded_prescription",
    "other",
]

DEVIATION_ATTRIBUTION = ["athlete_initiated", "coach_directed"]


def compute_and_log_variance(session_id: str, exercise: str,
                              planned_sets: int = 0, planned_reps: str = "",
                              planned_weight: str = "",
                              actual_sets: int = 0, actual_reps: str = "",
                              actual_weight: str = "",
                              reason_code: str = "other",
                              attribution: str = "athlete_initiated",
                              detail: str = "",
                              planned_weight_left: str = "",
                              planned_weight_right: str = "",
                              actual_weight_left: str = "",
                              actual_weight_right: str = "") -> dict:
    """
    Compute and log variance between planned and actual for an exercise.
    Supports laterality (left/right weights).
    """
    _ensure_deviations_table()
    
    # Compute set variance
    set_variance = actual_sets - planned_sets if planned_sets else 0
    
    # Determine deviation type
    if set_variance < 0:
        dev_type = "volume_reduction"
    elif set_variance > 0:
        dev_type = "exceeded_prescription"
    else:
        # Check weight/reps changes
        try:
            pw = float(str(planned_weight).replace("lb", "").replace("each", "").strip().split()[0])
            aw = float(str(actual_weight).replace("lb", "").replace("each", "").strip().split()[0])
            if aw < pw:
                dev_type = "weight_decrease"
            elif aw > pw:
                dev_type = "weight_increase"
            else:
                dev_type = "other"
        except (ValueError, IndexError):
            dev_type = "other"
    
    conn = _raw_conn()
    # Compute weight and reps variance
    weight_var = ""
    reps_var = ""
    try:
        pw = float(str(planned_weight).replace("lb", "").replace("each", "").replace("selected", "").strip().split()[0])
        aw = float(str(actual_weight).replace("lb", "").replace("each", "").replace("selected", "").strip().split()[0])
        weight_var = f"{aw - pw:+.0f} lb"
    except (ValueError, IndexError):
        pass
    try:
        pr = int(str(planned_reps).split("/")[0].strip())
        ar = int(str(actual_reps).split("/")[0].strip())
        reps_var = f"{ar - pr:+d} reps"
    except (ValueError, IndexError):
        pass

    conn.execute(
        """INSERT INTO set_deviations 
           (session_id, exercise, planned_weight, planned_reps, planned_sets,
            actual_weight, actual_reps, actual_sets, deviation_type, 
            reason_code, attribution, detail, weight_variance, reps_variance,
            set_variance, planned_weight_left, planned_weight_right,
            actual_weight_left, actual_weight_right)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, exercise, planned_weight, planned_reps, planned_sets,
         actual_weight, actual_reps, actual_sets, dev_type,
         reason_code, attribution, detail, weight_var, reps_var,
         set_variance,
         planned_weight_left, planned_weight_right,
         actual_weight_left, actual_weight_right)
    )
    conn.commit()  # SQLite's close() does not commit
    conn.close()

    return {
        "ok": True,
        "exercise": exercise,
        "set_variance": set_variance,
        "deviation_type": dev_type,
        "reason_code": reason_code,
        "attribution": attribution,
    }


def get_session_variance_report(session_id: str = None, date: str = None) -> dict:
    """Get a variance report comparing planned vs actual for a session."""
    _ensure_deviations_table()
    conn = _raw_conn()
    
    if not session_id and date:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE date = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
            (date,)
        ).fetchone()
        if row:
            session_id = row["session_id"]

    if not session_id:
        conn.close()
        return {"error": "No session found"}

    deviations = conn.execute(
        f"SELECT d.* FROM set_deviations d WHERE d.session_id = ? AND {_CURRENT_DEVIATION} ORDER BY d.timestamp",
        (session_id,)
    ).fetchall()
    conn.close()
    
    report = {
        "session_id": session_id,
        "total_deviations": len(deviations),
        "under_budget": 0,
        "over_budget": 0,
        "on_plan": 0,
        "deviations": [],
        "reason_summary": {},
    }
    
    for d in deviations:
        dd = dict(d)
        dev_type = dd.get("deviation_type", "")
        if "reduction" in dev_type or "decrease" in dev_type:
            report["under_budget"] += 1
        elif "increase" in dev_type or "exceeded" in dev_type:
            report["over_budget"] += 1
        else:
            report["on_plan"] += 1
        
        # Parse reason
        reason_parts = dd.get("reason", "").split("|")
        reason_code = reason_parts[0] if reason_parts else "other"
        report["reason_summary"][reason_code] = report["reason_summary"].get(reason_code, 0) + 1
        
        report["deviations"].append(dd)
    
    return report


def mark_planned_workout_replaced(planned_date: str = None, replacement_program: str = "") -> bool:
    """Mark a planned workout as replaced (user chose a different workout). Also supersedes it."""
    if not planned_date:
        planned_date = _local_today()
    stamp = _now_ts()
    conn = _raw_conn()
    try:
        cursor = conn.execute(
            "UPDATE planned_workouts SET status = 'replaced', superseded_at = ?, notes = ?, updated_at = ? WHERE planned_date = ? AND superseded_at IS NULL AND deleted_at IS NULL",
            (stamp, f"Replaced with: {replacement_program}", stamp, planned_date)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG — append-only event trail
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_audit_table():
    """Create audit_log table if it doesn't exist."""
    conn = _raw_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                event TEXT NOT NULL,
                detail TEXT DEFAULT '',
                source TEXT DEFAULT 'system',
                session_id TEXT DEFAULT '',
                set_id INTEGER DEFAULT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event)")
        conn.commit()
    except Exception as e:
        print(f"[audit] table creation: {e}")
    finally:
        conn.close()


def audit(event: str, detail: str = "", source: str = "system", session_id: str = "", set_id: int = None):
    """Write an audit log entry. Fire and forget — never blocks the caller."""
    try:
        _ensure_audit_table()
        conn = _raw_conn()
        conn.execute(
            "INSERT INTO audit_log (event, detail, source, session_id, set_id) VALUES (?, ?, ?, ?, ?)",
            (event, detail[:500], source, session_id, set_id)
        )
        conn.close()
    except Exception:
        pass  # Audit failures must never break the app


def get_audit_log(limit: int = 50, event: str = None) -> list:
    """Read recent audit entries."""
    _ensure_audit_table()
    conn = _raw_conn()
    if event:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE event = ? ORDER BY timestamp DESC LIMIT ?",
            (event, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
