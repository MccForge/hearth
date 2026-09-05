"""SQLite storage for Hearth. One file, no ORM, safe for a single-process server."""
from __future__ import annotations
import json, os, sqlite3, threading, datetime as dt
from typing import Any, Iterable

DB_PATH = os.environ.get("HEARTH_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "hearth.db"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, nickname TEXT, timezone TEXT NOT NULL DEFAULT 'America/New_York',
  window_start TEXT NOT NULL DEFAULT '08:00', window_end TEXT NOT NULL DEFAULT '11:00',
  notes TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, name TEXT NOT NULL, relation TEXT, channel TEXT NOT NULL DEFAULT 'dashboard',
  address TEXT DEFAULT '', priority INTEGER NOT NULL DEFAULT 1, FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE TABLE IF NOT EXISTS medications (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, name TEXT NOT NULL, schedule TEXT DEFAULT 'morning', FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, date TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
  mood INTEGER, sleep INTEGER, meds_taken INTEGER, ate INTEGER, concern TEXT, plans TEXT, summary TEXT,
  risk INTEGER NOT NULL DEFAULT 0, flags TEXT NOT NULL DEFAULT '[]', transcript TEXT NOT NULL DEFAULT '[]',
  UNIQUE(person_id, date), FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, created_at TEXT NOT NULL, level TEXT NOT NULL, reason TEXT NOT NULL,
  detail TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open', acknowledged_by TEXT, acknowledged_at TEXT,
  FOREIGN KEY(person_id) REFERENCES persons(id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, alert_id INTEGER, person_id INTEGER NOT NULL, contact_id INTEGER, channel TEXT NOT NULL,
  message TEXT NOT NULL, sent_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS snoozes (person_id INTEGER PRIMARY KEY, until TEXT NOT NULL);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        with _lock:
            _conn.executescript(SCHEMA)
    return _conn


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        cur = conn().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        cur = conn().execute(sql, tuple(params))
        conn().commit()
        return int(cur.lastrowid or 0)


def reset() -> None:
    """Drop everything (used by tests and the demo seed)."""
    global _conn
    if _conn is not None:
        _conn.close(); _conn = None
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn()


# ---- convenience accessors -------------------------------------------------

def person(person_id: int) -> dict | None:
    return one("SELECT * FROM persons WHERE id=?", (person_id,))


def persons() -> list[dict]:
    return q("SELECT * FROM persons ORDER BY id")


def contacts(person_id: int) -> list[dict]:
    return q("SELECT * FROM contacts WHERE person_id=? ORDER BY priority, id", (person_id,))


def medications(person_id: int) -> list[dict]:
    return q("SELECT * FROM medications WHERE person_id=? ORDER BY id", (person_id,))


def checkin_for(person_id: int, date: str) -> dict | None:
    row = one("SELECT * FROM checkins WHERE person_id=? AND date=?", (person_id, date))
    return _hydrate(row)


def checkin(checkin_id: int) -> dict | None:
    return _hydrate(one("SELECT * FROM checkins WHERE id=?", (checkin_id,)))


def recent_checkins(person_id: int, days: int = 14) -> list[dict]:
    rows = q("SELECT * FROM checkins WHERE person_id=? ORDER BY date DESC LIMIT ?", (person_id, days))
    return [_hydrate(r) for r in rows]


def _hydrate(row: dict | None) -> dict | None:
    if row is None:
        return None
    row = dict(row)
    row["flags"] = json.loads(row.get("flags") or "[]")
    row["transcript"] = json.loads(row.get("transcript") or "[]")
    return row


def update_checkin(checkin_id: int, **fields: Any) -> None:
    if not fields:
        return
    for k in ("flags", "transcript"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE checkins SET {sets} WHERE id=?", (*fields.values(), checkin_id))


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
