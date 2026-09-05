"""Hearth MCP server: the tools an agent host such as Alexa+ calls to run a daily check-in.
Transport: Streamable HTTP (MCP spec 2025-11-25) at /mcp, mounted by app.py. The same functions are called
in-process by the simulator, so the demo exercises exactly the code path a real host would."""
from __future__ import annotations
import json
from mcp.server.mcpserver import MCPServer
from . import db, core

server = MCPServer(
    name="hearth",
    title="Hearth daily check-in",
    version="0.1.0",
    instructions=(
        "Hearth runs a short, warm daily check-in with a person who lives alone and keeps their family informed. "
        "Flow: get_checkin_context -> start_checkin -> record_answer (one per topic) -> complete_checkin. "
        "If the person asks for help or describes an emergency, call request_help immediately, then advise calling "
        "emergency services. Never give medical advice; record what they say and let the family decide."
    ),
)

FIELDS = {"mood": "1-5 how they feel", "sleep": "1-5 how they slept", "meds_taken": "yes/no", "ate": "yes/no",
          "concern": "free text: anything bothering them", "plans": "free text: plans for the day", "note": "free text: anything else"}


def _bool(v) -> int | None:
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "yeah", "yep", "took", "taken", "did"): return 1
    if s in ("0", "false", "no", "n", "nope", "not yet", "didn't", "haven't"): return 0
    return None


def _scale(v) -> int | None:
    """Map an answer to 1-5. Handles negations ('not great' is 2, not 5) and longest phrase first."""
    import re
    s = str(v).strip().lower()
    try:
        return max(1, min(5, int(float(s))))
    except ValueError:
        pass
    if re.search(r"\b(didn'?t|couldn'?t|can'?t|no|hardly|barely)\s+(sleep|slept)\b|\b(up|awake) all night\b", s): return 1
    if re.search(r"\b(not|don'?t feel|didn'?t sleep|wasn'?t)\s+(so |too |very |that )?(great|good|well|wonderful)\b", s): return 2
    if re.search(r"\bnot\s+(too |so |that )?(bad|terrible|awful)\b", s): return 3
    words = {"terrible": 1, "awful": 1, "horrible": 1, "very bad": 1, "very badly": 1, "bad": 2, "badly": 2, "poor": 2, "poorly": 2, "low": 2, "rough": 2,
             "not good": 2, "okay": 3, "ok": 3, "fine": 3, "so-so": 3, "so so": 3, "alright": 3, "all right": 3, "pretty good": 4, "good": 4, "well": 4,
             "very well": 5, "great": 5, "wonderful": 5, "excellent": 5, "fantastic": 5}
    for w, n in sorted(words.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(w)}\b", s): return n
    return None


@server.tool(description="Everything the agent needs before greeting the person: name, time of day, medications due, "
                         "yesterday's summary, open concerns, and the topics to cover. Call this first.")
def get_checkin_context(person_id: int) -> dict:
    p = db.person(person_id)
    if not p: return {"error": f"no person {person_id}"}
    st = core.status(p)
    recent = db.recent_checkins(person_id, 7)
    yesterday = recent[0] if recent and recent[0]["date"] != st["date"] else (recent[1] if len(recent) > 1 else None)
    meds = [m["name"] for m in db.medications(person_id)]
    hour = int(st["local_time"][:2])
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    topics = ["how they're feeling", "how they slept", "whether they took their medication" + (f" ({', '.join(meds)})" if meds else ""),
              "whether they've eaten", "anything bothering them", "plans for today"]
    return {"person": {"id": p["id"], "name": p["name"], "nickname": p.get("nickname") or p["name"], "notes": p.get("notes") or ""},
            "status": st, "greeting": f"{greeting}, {p.get('nickname') or p['name']}!", "medications_due": meds,
            "yesterday": {"summary": yesterday.get("summary"), "flags": yesterday.get("flags")} if yesterday else None,
            "topics_in_order": topics, "style": "warm, unhurried, one question at a time, short sentences; follow up on anything worrying",
            "safety": "no medical advice; if they describe an emergency call request_help and tell them to call emergency services"}


@server.tool(description="Open today's check-in record for the person. Returns the checkin_id used by record_answer and complete_checkin.")
def start_checkin(person_id: int) -> dict:
    p = db.person(person_id)
    if not p: return {"error": f"no person {person_id}"}
    date = core.today_str(p)
    existing = db.checkin_for(person_id, date)
    if existing:
        return {"checkin_id": existing["id"], "date": date, "resumed": True, "completed": bool(existing.get("completed_at"))}
    cid = db.execute("INSERT INTO checkins(person_id, date, started_at) VALUES (?,?,?)", (person_id, date, db.now_iso()))
    db.execute("DELETE FROM snoozes WHERE person_id=?", (person_id,))
    return {"checkin_id": cid, "date": date, "resumed": False, "completed": False}


@server.tool(description="Record one answer. field is one of mood, sleep, meds_taken, ate, concern, plans, note. "
                         "value is the interpreted answer (1-5, yes/no, or text); quote is what the person actually said. "
                         "Returns flags detected and a follow_up hint when something needs a gentle second question.")
def record_answer(checkin_id: int, field: str, value: str, quote: str = "") -> dict:
    c = db.checkin(checkin_id)
    if not c: return {"error": f"no check-in {checkin_id}"}
    if field not in FIELDS: return {"error": f"unknown field {field}", "fields": FIELDS}
    updates: dict = {}
    if field in ("mood", "sleep"):
        updates[field] = _scale(value)
    elif field in ("meds_taken", "ate"):
        updates[field] = _bool(value)
    elif field == "note":
        updates["concern"] = ((c.get("concern") or "") + " " + str(value)).strip()
    else:
        updates[field] = str(value)
    new_flags = [f for f in core.detect_flags(f"{value} {quote}") if f not in c["flags"]]
    if field in ("mood", "sleep") and updates.get(field) is not None and updates[field] <= 2 and field == "sleep" and "no_sleep" not in c["flags"] + new_flags:
        new_flags.append("no_sleep")
    flags = c["flags"] + new_flags
    transcript = c["transcript"] + [{"field": field, "value": value, "quote": quote}]
    merged = {**c, **updates, "flags": flags}
    risk = core.risk_score(merged)
    db.update_checkin(checkin_id, flags=flags, transcript=transcript, risk=risk, **updates)
    follow_up = None
    if "fall" in new_flags: follow_up = "Ask gently whether they are hurt and whether they can get up and move around normally."
    elif "chest_pain" in new_flags or "breathing" in new_flags or "emergency" in new_flags:
        follow_up = "This may be an emergency. Ask if they need help right now; if yes call request_help with urgency=urgent and tell them to call emergency services."
    elif "dizzy" in new_flags or "confusion" in new_flags: follow_up = "Ask when it started and whether they have eaten and had water."
    elif field == "meds_taken" and updates.get("meds_taken") == 0: follow_up = "Encourage them to take it now and ask them to say when it's done."
    elif field == "mood" and updates.get("mood") is not None and updates["mood"] <= 2: follow_up = "Ask what's making today hard; listen, don't fix."
    return {"ok": True, "recorded": {field: updates.get(field, value)}, "flags_added": new_flags, "risk": risk, "risk_level": core.risk_level(risk), "follow_up": follow_up}


@server.tool(description="Finish the check-in: scores concern level, writes a one-paragraph summary for the family, sends it to the "
                         "primary contact, escalates to more contacts if the concern level is high, and clears any missed-check-in alerts.")
def complete_checkin(checkin_id: int, summary: str = "") -> dict:
    c = db.checkin(checkin_id)
    if not c: return {"error": f"no check-in {checkin_id}"}
    p = db.person(c["person_id"])
    risk = core.risk_score(c); level = core.risk_level(risk)
    text = summary.strip() or core.build_summary(p, c)
    db.update_checkin(checkin_id, completed_at=db.now_iso(), risk=risk, summary=text)
    resolved = core.resolve_missed_alerts(p["id"])
    sent = core.notify(p["id"], core.contacts_for_level(p["id"], "info"), f"Hearth daily summary: {p['name']}", text)
    escalation = None
    if level in ("concern", "urgent"):
        escalation = core.create_alert(p["id"], level, "check-in flagged concern", text)
    return {"ok": True, "risk": risk, "risk_level": level, "summary": text, "summary_sent_to": [s["contact"] for s in sent],
            "escalation": escalation, "missed_alerts_resolved": resolved,
            "closing_line": "Thanks for chatting with me. I've let " + (sent[0]["contact"] if sent else "your family") + " know you're doing okay." if level in ("ok", "watch")
            else "Thank you for telling me. I'm letting your family know right now so someone can check on you."}


@server.tool(description="The person asked for help or described an emergency. Alerts every contact immediately with the reason. "
                         "urgency: urgent (default) or concern.")
def request_help(person_id: int, reason: str, urgency: str = "urgent") -> dict:
    p = db.person(person_id)
    if not p: return {"error": f"no person {person_id}"}
    level = "urgent" if urgency != "concern" else "concern"
    alert = core.create_alert(person_id, level, "asked for help", reason)
    return {"ok": True, **alert, "say": "I've alerted your family. If this is an emergency, please call 911 now."}


@server.tool(description="Caregiver query, e.g. 'how is Mom today?': today's check-in state, concern level, summary, open alerts.")
def get_status(person_id: int) -> dict:
    p = db.person(person_id)
    if not p: return {"error": f"no person {person_id}"}
    st = core.status(p)
    st["open_alert_details"] = [{"level": a["level"], "reason": a["reason"], "detail": a["detail"], "at": a["created_at"]} for a in db.open_alerts(person_id)]
    return st


@server.tool(description="The person wants to talk later. Pauses the missed-check-in escalation for the given minutes (max 180).")
def snooze_checkin(person_id: int, minutes: int = 30) -> dict:
    import datetime as dt
    minutes = max(5, min(180, int(minutes)))
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    db.execute("INSERT INTO snoozes(person_id, until) VALUES (?,?) ON CONFLICT(person_id) DO UPDATE SET until=excluded.until", (person_id, until))
    return {"ok": True, "snoozed_until": until, "say": f"No problem, I'll check back in {minutes} minutes."}


@server.tool(description="Record that a medication was taken (or explicitly not taken) outside the check-in flow.")
def log_medication(person_id: int, medication: str, taken: bool = True) -> dict:
    p = db.person(person_id)
    if not p: return {"error": f"no person {person_id}"}
    date = core.today_str(p)
    c = db.checkin_for(person_id, date)
    if not c:
        cid = db.execute("INSERT INTO checkins(person_id, date, started_at) VALUES (?,?,?)", (person_id, date, db.now_iso()))
        c = db.checkin(cid)
    db.update_checkin(c["id"], meds_taken=1 if taken else 0, transcript=c["transcript"] + [{"field": "meds_taken", "value": medication, "quote": "logged"}])
    return {"ok": True, "medication": medication, "taken": taken}


@server.tool(description="List the people this Hearth instance looks after (a real deployment maps the device to one person via account linking).")
def list_persons() -> dict:
    return {"persons": [{"id": p["id"], "name": p["name"], "nickname": p.get("nickname"), "timezone": p["timezone"], "window": f"{p['window_start']}-{p['window_end']}"} for p in db.persons()]}


@server.resource("hearth://persons/{person_id}/today", description="Today's check-in status for a person, as JSON")
def today_resource(person_id: int) -> str:
    p = db.person(int(person_id))
    return json.dumps(core.status(p) if p else {"error": "no such person"})


@server.prompt(description="Conversation guide for running the daily check-in with a specific person")
def daily_checkin(person_id: int) -> str:
    ctx = get_checkin_context(int(person_id))
    if "error" in ctx: return ctx["error"]
    meds = ", ".join(ctx["medications_due"]) or "none listed"
    return (f"You are running Hearth's daily check-in with {ctx['person']['nickname']}. Start with: \"{ctx['greeting']}\" "
            f"Cover, one at a time: {'; '.join(ctx['topics_in_order'])}. Medications due: {meds}. "
            f"Yesterday: {ctx['yesterday']['summary'] if ctx['yesterday'] else 'no record'}. "
            "Call start_checkin first, record_answer after each answer with the interpreted value and their exact words, "
            "follow any follow_up hint you get back, then complete_checkin. If they ask for help, call request_help at once. "
            "Style: warm, unhurried, short sentences. Never give medical advice.")


TOOLS = {f.__name__: f for f in (get_checkin_context, start_checkin, record_answer, complete_checkin, request_help, get_status, snooze_checkin, log_medication, list_persons)}
