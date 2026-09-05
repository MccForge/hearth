"""SQLite storage for Hearth. One file, no ORM, safe for a single-process server.
Schema changes bump SCHEMA_VERSION; an older database is moved aside and recreated (prototype behaviour)."""
from __future__ import annotations
import json, os, shutil, sqlite3, threading, datetime as dt
from typing import Any, Iterable

ROOT = os.path.dirname(os.path.dirname(__file__))
DB_PATH = os.environ.get("HEARTH_DB", os.path.join(ROOT, "hearth.db"))
MEDIA_DIR = os.environ.get("HEARTH_MEDIA", os.path.join(ROOT, "data", "media"))
SCHEMA_VERSION = 2
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, nickname TEXT, timezone TEXT NOT NULL DEFAULT 'America/New_York',
  window_start TEXT NOT NULL DEFAULT '08:00', window_end TEXT NOT NULL DEFAULT '11:00',
  notes TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, name TEXT NOT NULL, relation TEXT, channel TEXT NOT NULL DEFAULT 'dashboard',
  address TEXT DEFAULT '', priority INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS medications (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, name TEXT NOT NULL, schedule TEXT DEFAULT 'morning');
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, date TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
  mood INTEGER, sleep INTEGER, meds_taken INTEGER, ate INTEGER, concern TEXT, plans TEXT, summary TEXT,
  risk INTEGER NOT NULL DEFAULT 0, flags TEXT NOT NULL DEFAULT '[]', transcript TEXT NOT NULL DEFAULT '[]', superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS checkins_person_date ON checkins(person_id, date);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, created_at TEXT NOT NULL, level TEXT NOT NULL, reason TEXT NOT NULL,
  detail TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open', acknowledged_by TEXT, acknowledged_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, alert_id INTEGER, person_id INTEGER NOT NULL, contact_id INTEGER, channel TEXT NOT NULL,
  message TEXT NOT NULL, sent_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS snoozes (person_id INTEGER PRIMARY KEY, until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, direction TEXT NOT NULL, from_name TEXT NOT NULL, contact_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'text', transcript TEXT DEFAULT '', audio_path TEXT, mime TEXT, created_at TEXT NOT NULL,
  play_from TEXT, play_until TEXT, repeat_daily INTEGER NOT NULL DEFAULT 0, played_at TEXT, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, from_name TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
  ask_on TEXT, asked_at TEXT, answer TEXT, checkin_id INTEGER, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS away (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, contact_id INTEGER NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  cover_contact_id INTEGER, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, date TEXT NOT NULL, time TEXT DEFAULT '', title TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'appointment', notes TEXT DEFAULT '', added_by TEXT DEFAULT '', remind_day_before INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, mentioned_at TEXT, response TEXT, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS events_person_date ON events(person_id, date);
"""

_conn: sqlite3.Connection | None = None


def _open() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            c = _open()
            try:
                v = c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                current = int(v[0]) if v else 0
            except sqlite3.OperationalError:   # no meta table: either a brand-new file or a pre-versioning (v1) database
                has_tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='persons'").fetchone()
                current = 1 if has_tables else 0
            stale = False
            if current:
                cols = {r[1] for r in c.execute("PRAGMA table_info(checkins)").fetchall()}
                stale = current != SCHEMA_VERSION or (cols and "superseded" not in cols)
            if stale:
                c.close()
                shutil.move(DB_PATH, DB_PATH + f".v{current}.bak")
                c = _open()
            c.executescript(SCHEMA)
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            c.commit()
            _conn = c
        os.makedirs(MEDIA_DIR, exist_ok=True)
        return _conn


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        cur = conn().execute(sql, tuple(params))
        conn().commit()
        return int(cur.lastrowid or 0)


def reset() -> None:
    """Drop everything (tests and the demo seed)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close(); _conn = None
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        if os.path.isdir(MEDIA_DIR):
            for f in os.listdir(MEDIA_DIR):
                try: os.remove(os.path.join(MEDIA_DIR, f))
                except OSError: pass
        conn()


# ---- accessors ------------------------------------------------------------------

def person(person_id: int) -> dict | None:
    return one("SELECT * FROM persons WHERE id=?", (person_id,))


def persons() -> list[dict]:
    return q("SELECT * FROM persons ORDER BY id")


def contacts(person_id: int) -> list[dict]:
    return q("SELECT * FROM contacts WHERE person_id=? ORDER BY priority, id", (person_id,))


def contact(contact_id: int) -> dict | None:
    return one("SELECT * FROM contacts WHERE id=?", (contact_id,))


def medications(person_id: int) -> list[dict]:
    return q("SELECT * FROM medications WHERE person_id=? ORDER BY id", (person_id,))


def _hydrate(row: dict | None) -> dict | None:
    if row is None:
        return None
    row = dict(row)
    row["flags"] = json.loads(row.get("flags") or "[]")
    row["transcript"] = json.loads(row.get("transcript") or "[]")
    return row


def checkin_for(person_id: int, date: str) -> dict | None:
    """The current (non-superseded) check-in for a date."""
    return _hydrate(one("SELECT * FROM checkins WHERE person_id=? AND date=? AND superseded=0 ORDER BY id DESC LIMIT 1", (person_id, date)))


def checkin(checkin_id: int) -> dict | None:
    return _hydrate(one("SELECT * FROM checkins WHERE id=?", (checkin_id,)))


def recent_checkins(person_id: int, days: int = 14) -> list[dict]:
    rows = q("SELECT * FROM checkins WHERE person_id=? AND superseded=0 ORDER BY date DESC, id DESC LIMIT ?", (person_id, days))
    return [_hydrate(r) for r in rows]


def update_checkin(checkin_id: int, **fields: Any) -> None:
    if not fields:
        return
    for k in ("flags", "transcript"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    execute("UPDATE checkins SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), checkin_id))


def open_alerts(person_id: int | None = None) -> list[dict]:
    if person_id is None:
        return q("SELECT * FROM alerts WHERE status='open' ORDER BY created_at DESC")
    return q("SELECT * FROM alerts WHERE status='open' AND person_id=? ORDER BY created_at DESC", (person_id,))


def alerts(person_id: int | None = None, limit: int = 50) -> list[dict]:
    if person_id is None:
        return q("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,))
    return q("SELECT * FROM alerts WHERE person_id=? ORDER BY created_at DESC LIMIT ?", (person_id, limit))


def notifications(person_id: int, limit: int = 50) -> list[dict]:
    return q("SELECT * FROM notifications WHERE person_id=? ORDER BY sent_at DESC LIMIT ?", (person_id, limit))


# ---- messages, questions, away ----------------------------------------------------

def pending_messages(person_id: int, date: str) -> list[dict]:
    """Family messages due to play on this date: 'next check-in' (no play_from) or a date/range that covers today."""
    return q("SELECT * FROM messages WHERE person_id=? AND direction='to_person' AND status='pending' "
             "AND (play_from IS NULL OR play_from <= ?) AND (play_until IS NULL OR play_until >= ?) ORDER BY id", (person_id, date, date))


def messages(person_id: int, direction: str | None = None, limit: int = 50) -> list[dict]:
    if direction:
        return q("SELECT * FROM messages WHERE person_id=? AND direction=? ORDER BY id DESC LIMIT ?", (person_id, direction, limit))
    return q("SELECT * FROM messages WHERE person_id=? ORDER BY id DESC LIMIT ?", (person_id, limit))


def message(message_id: int) -> dict | None:
    return one("SELECT * FROM messages WHERE id=?", (message_id,))


def pending_questions(person_id: int, date: str) -> list[dict]:
    return q("SELECT * FROM questions WHERE person_id=? AND status='pending' AND (ask_on IS NULL OR ask_on <= ?) ORDER BY id", (person_id, date))


def questions(person_id: int, limit: int = 50) -> list[dict]:
    return q("SELECT * FROM questions WHERE person_id=? ORDER BY id DESC LIMIT ?", (person_id, limit))


def away_on(person_id: int, date: str) -> list[dict]:
    return q("SELECT * FROM away WHERE person_id=? AND start_date <= ? AND end_date >= ? ORDER BY id", (person_id, date, date))


def away_all(person_id: int) -> list[dict]:
    return q("SELECT * FROM away WHERE person_id=? ORDER BY start_date DESC", (person_id,))


def events_between(person_id: int, start: str, end: str) -> list[dict]:
    return q("SELECT * FROM events WHERE person_id=? AND date >= ? AND date <= ? AND status != 'cancelled' ORDER BY date, time, id", (person_id, start, end))


def event(event_id: int) -> dict | None:
    return one("SELECT * FROM events WHERE id=?", (event_id,))


def save_media(message_id: int, data: bytes, mime: str) -> str:
    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav", "audio/x-wav": "wav"}.get(mime.split(";")[0], "bin")
    path = os.path.join(MEDIA_DIR, f"msg_{message_id}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path
