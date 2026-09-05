"""Domain logic shared by the MCP tools, the watchdog, and the dashboard API.
No medical advice lives here: Hearth records what the person says, scores how worried a caregiver should be, and escalates."""
from __future__ import annotations
import datetime as dt, json, os, re, smtplib, urllib.request
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from . import db

# ---- time -------------------------------------------------------------------

def tz(person: dict) -> ZoneInfo:
    try:
        return ZoneInfo(person.get("timezone") or "America/New_York")
    except Exception:
        return ZoneInfo("America/New_York")


def local_now(person: dict, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(tz(person))


def today_str(person: dict, now: dt.datetime | None = None) -> str:
    return local_now(person, now).date().isoformat()


def window_bounds(person: dict, date: str) -> tuple[dt.datetime, dt.datetime]:
    d = dt.date.fromisoformat(date)
    hs, ms = (int(x) for x in (person.get("window_start") or "08:00").split(":"))
    he, me = (int(x) for x in (person.get("window_end") or "11:00").split(":"))
    z = tz(person)
    return dt.datetime(d.year, d.month, d.day, hs, ms, tzinfo=z), dt.datetime(d.year, d.month, d.day, he, me, tzinfo=z)


# ---- language signals ---------------------------------------------------------
# Keyword flags are deliberately simple and transparent. The agent host (Alexa+) does the understanding;
# these catch the words a caregiver would never want missed even if the agent under-reports them.
FLAG_PATTERNS: dict[str, tuple[str, int, str]] = {
    # code: (regex, risk weight, caregiver-facing label)
    "emergency": (r"\b(911|ambulance|emergency|can'?t (get|stand) up|help me)\b", 80, "asked for help"),
    "chest_pain": (r"\b(chest (pain|tight|pressure)|heart)\b", 60, "mentioned chest pain"),
    "breathing": (r"\b(can'?t breathe|short(ness)? of breath|breathing)\b", 60, "mentioned trouble breathing"),
    "fall": (r"\b(fell|fall|fallen|slipped|tripped)\b", 40, "mentioned a fall"),
    "confusion": (r"\b(confused|can'?t remember|what day|forgot where)\b", 30, "sounded confused"),
    "dizzy": (r"\b(dizzy|lightheaded|faint)\b", 25, "felt dizzy"),
    "pain": (r"\b(pain|hurts?|ache|aching|sore)\b", 15, "mentioned pain"),
    "no_sleep": (r"\b(didn'?t sleep|no sleep|couldn'?t sleep|awake all night)\b", 10, "slept badly"),
    "skipped_meds": (r"\b(forgot (my )?(pills|meds|medication)|skipped (my )?(pills|meds)|haven'?t taken)\b", 15, "may have skipped medication"),
    "no_food": (r"\b(haven'?t eaten|not hungry|skipped (breakfast|lunch|dinner)|no appetite)\b", 10, "hasn't eaten"),
    "lonely": (r"\b(lonely|alone|sad|down|blue|miss(ing)? (him|her|them))\b", 10, "sounded low"),
}


NEGATION = re.compile(r"\b(not|no|never|didn'?t|don'?t|wasn'?t|haven'?t|isn'?t|without)\s+(\w+\s+){0,2}$")


def detect_flags(text: str) -> list[str]:
    """Keyword flags with a small negation guard: 'I am not hurt' and 'no pain' do not flag."""
    t = (text or "").lower()
    out = []
    for code, (pat, _, _) in FLAG_PATTERNS.items():
        for m in re.finditer(pat, t):
            if code != "emergency" and NEGATION.search(t[:m.start()]):
                continue
            out.append(code); break
    return out


def flag_label(code: str) -> str:
    return FLAG_PATTERNS.get(code, ("", 0, code))[2]


# ---- risk ----------------------------------------------------------------------

def risk_score(c: dict) -> int:
    score = 0
    if c.get("mood") is not None and c["mood"] <= 2: score += 25
    if c.get("sleep") is not None and c["sleep"] <= 2: score += 10
    if c.get("meds_taken") == 0: score += 15
    if c.get("ate") == 0: score += 10
    for f in c.get("flags") or []:
        score += FLAG_PATTERNS.get(f, ("", 0, ""))[1]
    return min(score, 100)


def risk_level(score: int) -> str:
    return "urgent" if score >= 80 else "concern" if score >= 50 else "watch" if score >= 25 else "ok"


MOOD_WORDS = {1: "very low", 2: "low", 3: "okay", 4: "good", 5: "great"}
SLEEP_WORDS = {1: "very badly", 2: "badly", 3: "so-so", 4: "well", 5: "very well"}


def build_summary(person: dict, c: dict) -> str:
    who = person.get("nickname") or person["name"]
    parts = []
    if c.get("mood") is not None: parts.append(f"feeling {MOOD_WORDS.get(c['mood'], c['mood'])}")
    if c.get("sleep") is not None: parts.append(f"slept {SLEEP_WORDS.get(c['sleep'], c['sleep'])}")
    if c.get("meds_taken") is not None: parts.append("took medication" if c["meds_taken"] else "has NOT taken medication")
    if c.get("ate") is not None: parts.append("has eaten" if c["ate"] else "hasn't eaten yet")
    s = f"{who} checked in" + (": " + ", ".join(parts) if parts else "") + "."
    labels = [flag_label(f) for f in (c.get("flags") or [])]
    if labels: s += " Flagged: " + ", ".join(labels) + "."
    concern = (c.get("concern") or "").strip()
    if concern and not re.fullmatch(r"(no|nope|nothing|nothing really|not really|no,? nothing( really)?|all good|i'?m fine|fine)\W*", concern.lower()):
        s += f' Said: "{concern[:160]}"'
    if c.get("plans"): s += f" Plans: {c['plans'].strip()[:120]}."
    return s


# ---- notifications and alerts ---------------------------------------------------

def _send(channel: str, address: str, subject: str, message: str) -> bool:
    """Best-effort delivery. The dashboard feed always gets a copy; other channels are opt-in via config."""
    try:
        if channel == "webhook" and address.startswith("http"):
            req = urllib.request.Request(address, data=json.dumps({"subject": subject, "message": message}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
            return True
        if channel == "email" and address and os.environ.get("HEARTH_SMTP_HOST"):
            msg = EmailMessage(); msg["Subject"] = subject; msg["From"] = os.environ.get("HEARTH_SMTP_FROM", "hearth@localhost"); msg["To"] = address
            msg.set_content(message)
            with smtplib.SMTP(os.environ["HEARTH_SMTP_HOST"], int(os.environ.get("HEARTH_SMTP_PORT", "587"))) as s:
                s.starttls(); s.login(os.environ.get("HEARTH_SMTP_USER", ""), os.environ.get("HEARTH_SMTP_PASS", "")); s.send_message(msg)
            return True
        if channel == "sms":
            return False   # SMS providers need paid credentials; wire one up here (Twilio-compatible) when deploying
    except Exception:
        return False
    return channel == "dashboard"


def notify(person_id: int, contact_list: list[dict], subject: str, message: str, alert_id: int | None = None) -> list[dict]:
    sent = []
    for ct in contact_list:
        delivered = _send(ct.get("channel") or "dashboard", ct.get("address") or "", subject, message)
        nid = db.execute("INSERT INTO notifications(alert_id, person_id, contact_id, channel, message, sent_at, delivered) VALUES (?,?,?,?,?,?,?)",
                         (alert_id, person_id, ct["id"], ct.get("channel") or "dashboard", f"To {ct['name']}: {message}", db.now_iso(), int(delivered)))
        sent.append({"id": nid, "contact": ct["name"], "channel": ct.get("channel") or "dashboard", "delivered": bool(delivered)})
    return sent


def contacts_for_level(person_id: int, level: str) -> list[dict]:
    cs = db.contacts(person_id)
    if not cs:
        return []
    if level in ("info", "watch", "missed_1"):
        return [c for c in cs if c["priority"] == 1] or cs[:1]
    if level in ("concern", "missed_2"):
        return [c for c in cs if c["priority"] <= 2] or cs[:1]
    return cs   # urgent / missed_3: everyone


def create_alert(person_id: int, level: str, reason: str, detail: str = "") -> dict:
    person = db.person(person_id) or {"name": "?"}
    alert_id = db.execute("INSERT INTO alerts(person_id, created_at, level, reason, detail) VALUES (?,?,?,?,?)",
                          (person_id, db.now_iso(), level, reason, detail))
    subject = f"Hearth {level.replace('_', ' ')}: {person['name']}"
    sent = notify(person_id, contacts_for_level(person_id, level), subject, detail or reason, alert_id)
    return {"alert_id": alert_id, "level": level, "reason": reason, "notified": sent}


def already_alerted(person_id: int, reason: str, date: str) -> bool:
    return db.one("SELECT id FROM alerts WHERE person_id=? AND reason=? AND created_at >= ?", (person_id, reason, date)) is not None


def resolve_missed_alerts(person_id: int) -> int:
    rows = db.q("SELECT id FROM alerts WHERE person_id=? AND status='open' AND reason LIKE 'missed%'", (person_id,))
    for r in rows:
        db.execute("UPDATE alerts SET status='resolved', acknowledged_by='check-in completed', acknowledged_at=? WHERE id=?", (db.now_iso(), r["id"]))
    return len(rows)


# ---- status ---------------------------------------------------------------------
LADDER = [(0, "missed_1"), (30, "missed_2"), (90, "missed_3")]   # minutes past the end of the window


def status(person: dict, now: dt.datetime | None = None) -> dict:
    date = today_str(person, now)
    c = db.checkin_for(person["id"], date)
    lnow = local_now(person, now)
    start, end = window_bounds(person, date)
    snooze = db.one("SELECT until FROM snoozes WHERE person_id=?", (person["id"],))
    snoozed_until = snooze["until"] if snooze and snooze["until"] > db.now_iso() else None
    overdue_min = int((lnow - end).total_seconds() // 60) if lnow > end else 0
    if c and c.get("completed_at"):
        state = "checked_in"
    elif c:
        state = "in_progress"
    elif lnow < start:
        state = "before_window"
    elif lnow <= end or snoozed_until:
        state = "waiting"
    else:
        state = "overdue"
    return {"person_id": person["id"], "name": person["name"], "date": date, "local_time": lnow.strftime("%H:%M"), "state": state,
            "window": f"{person.get('window_start')}-{person.get('window_end')}", "overdue_minutes": overdue_min,
            "snoozed_until": snoozed_until, "risk": c.get("risk") if c else None, "risk_level": risk_level(c["risk"]) if c else None,
            "summary": c.get("summary") if c else None, "flags": c.get("flags") if c else [], "open_alerts": len(db.open_alerts(person["id"]))}
