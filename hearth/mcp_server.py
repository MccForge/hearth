"""Hearth MCP server: the tools an agent host such as Alexa+ calls to run a daily check-in.
Transport: Streamable HTTP (MCP spec 2025-11-25) at /mcp, mounted by app.py. The same functions are called
in-process by the simulator, so the demo exercises exactly the code path a real host would."""
from __future__ import annotations
import base64, datetime as dt, json, os, re
from mcp.server.mcpserver import MCPServer
from mcp.types import AudioContent, TextContent
from . import db, core

server = MCPServer(
    name="hearth",
    title="Hearth daily check-in",
    version="0.2.0",
    instructions=(
        "Hearth runs a short, warm daily check-in with a person who lives alone and keeps their family informed. "
        "Flow: get_checkin_context -> (play any family_messages with get_family_message, then mark_message_played) -> start_checkin "
        "-> record_answer per topic (including questions_from_family as field 'question:<id>' and events_today as field 'event:<id>') "
        "-> complete_checkin. If the person asks for help or describes an emergency, call request_help immediately and advise calling "
        "emergency services. If they want to send a message to family, call record_reply. If they mention a future appointment, call add_event. "
        "Never give medical advice; record what they say and let the family decide."
    ),
)

FIELDS = {"mood": "1-5 how they feel", "sleep": "1-5 how they slept", "meds_taken": "yes/no", "ate": "yes/no",
          "concern": "free text: anything bothering them", "plans": "free text: plans for the day", "note": "free text: anything else",
          "question:<id>": "answer to a question the family queued", "event:<id>": "response to today's appointment or reminder"}
YES = re.compile(r"\b(yes|yeah|yep|yup|i did|i have|took|taken|already|of course|sure did|i think so|i believe so|mm-?hmm|uh-?huh)\b", re.I)
NO = re.compile(r"\b(no|nope|not yet|didn'?t|haven'?t|forgot|not really|nah|i don'?t think so)\b", re.I)


def parse_bool(v) -> int | None:
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y"): return 1
    if s in ("0", "false", "no", "n"): return 0
    neg, pos = bool(NO.search(s)), bool(YES.search(s))
    if neg and not pos: return 0
    if pos and not neg: return 1
    if pos and neg:   # "yes I did, but not the second one" -> treat as uncertain
        return None
    return None


def parse_scale(v) -> int | None:
    """Map an answer to 1-5. Handles negations ('not great' is 2, not 5) and longest phrase first."""
    s = str(v).strip().lower()
    try:
        return max(1, min(5, int(float(s))))
    except ValueError:
        pass
    if re.search(r"\b(didn'?t|couldn'?t|can'?t|no|hardly|barely)\s+(sleep|slept)\b|\b(up|awake) (all|half the) night\b|\bterrible\b|\bawful\b|\bhorrible\b", s): return 1
    if re.search(r"\b(not|don'?t feel|wasn'?t)\s+(so |too |very |that |feeling )?(great|good|well|wonderful|myself)\b|\bnot (my )?best\b", s): return 2
    if re.search(r"\bnot\s+(too |so |that )?(bad|terrible|awful)\b|\bcould be worse\b", s): return 3
    words = {"very badly": 1, "very bad": 1, "bad": 2, "badly": 2, "poor": 2, "poorly": 2, "low": 2, "rough": 2, "tired": 2, "exhausted": 2, "not good": 2,
             "okay": 3, "ok": 3, "fine": 3, "so-so": 3, "so so": 3, "alright": 3, "all right": 3, "same as usual": 3, "pretty good": 4, "good": 4, "well": 4,
             "rested": 4, "very well": 5, "great": 5, "wonderful": 5, "excellent": 5, "fantastic": 5, "marvelous": 5, "like a baby": 5}
    for w, n in sorted(words.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(w)}\b", s): return n
    return None


def _person_or_error(person_id: int):
    p = db.person(person_id)
    return (p, None) if p else (None, {"error": f"no person {person_id}"})


def _fmt_time(t: str) -> str:
    if not t: return ""
    try:
        h, m = (int(x) for x in t.split(":"))
        suffix = "am" if h < 12 else "pm"; h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}" if m else f"{h12} {suffix}"
    except ValueError:
        return t


def _events_for_context(p: dict, date: str) -> tuple[list[dict], list[dict]]:
    d = dt.date.fromisoformat(date)
    today = [e for e in db.events_between(p["id"], date, date) if e["status"] in ("pending", "mentioned")]
    tomorrow = [e for e in db.events_between(p["id"], (d + dt.timedelta(days=1)).isoformat(), (d + dt.timedelta(days=1)).isoformat())
                if e.get("remind_day_before") and e["status"] in ("pending", "mentioned")]
    fmt = lambda e: {"id": e["id"], "title": e["title"], "time": _fmt_time(e.get("time") or ""), "kind": e["kind"], "notes": e.get("notes") or "",
                     "added_by": e.get("added_by") or "", "date": e["date"]}
    return [fmt(e) for e in today], [fmt(e) for e in tomorrow]


@server.tool(description="Everything the agent needs before greeting the person: name, time of day, medications due, yesterday's summary, "
                         "family voice messages to play first, questions the family asked, today's appointments and reminders, who is away, "
                         "trend insights, the topics to cover, tone, and safety rules. Call this first.")
def get_checkin_context(person_id: int) -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    st = core.status(p)
    recent = db.recent_checkins(person_id, 7)
    yesterday = next((c for c in recent if c["date"] != st["date"] and c.get("completed_at")), None)
    meds = [m["name"] for m in db.medications(person_id)]
    hour = int(st["local_time"][:2])
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    msgs = [{"id": m["id"], "from": m["from_name"], "kind": m["kind"], "transcript": m.get("transcript") or "", "has_audio": bool(m.get("audio_path"))}
            for m in db.pending_messages(person_id, st["date"])]
    qs = [{"id": x["id"], "from": x["from_name"], "text": x["text"]} for x in db.pending_questions(person_id, st["date"])]
    ev_today, ev_tomorrow = _events_for_context(p, st["date"])
    topics = ["how they're feeling", "how they slept", "whether they took their medication" + (f" ({', '.join(meds)})" if meds else ""),
              "whether they've eaten", "anything bothering them"]
    if ev_today: topics.append("today's appointments and reminders: " + "; ".join(f"{e['title']}{' at ' + e['time'] if e['time'] else ''}" for e in ev_today))
    if qs: topics.append("questions from family: " + "; ".join(f"{q['from']} asks: {q['text']}" for q in qs))
    topics.append("plans for the day")
    if ev_tomorrow: topics.append("heads-up for tomorrow: " + "; ".join(f"{e['title']}{' at ' + e['time'] if e['time'] else ''}" for e in ev_tomorrow))
    tr = core.trends(person_id, 7, today=st["date"])
    return {"person": {"id": p["id"], "name": p["name"], "nickname": p.get("nickname") or p["name"], "notes": p.get("notes") or ""},
            "status": st, "greeting": f"{greeting}, {p.get('nickname') or p['name']}!", "medications_due": meds,
            "yesterday": {"summary": yesterday.get("summary"), "flags": yesterday.get("flags")} if yesterday else None,
            "family_messages": msgs, "questions_from_family": qs, "events_today": ev_today, "events_tomorrow": ev_tomorrow,
            "away": st["away"], "trends": tr["insights"], "topics_in_order": topics,
            "style": "warm, unhurried, one question at a time, short sentences; acknowledge before moving on; follow up on anything worrying",
            "safety": "no medical advice; if they describe an emergency call request_help and tell them to call emergency services"}


@server.tool(description="Fetch a family message to play to the person: returns the audio (if recorded) and the transcript. "
                         "Play it right after the greeting, then call mark_message_played.")
def get_family_message(message_id: int) -> list:
    m = db.message(message_id)
    if not m: return [TextContent(type="text", text=json.dumps({"error": f"no message {message_id}"}))]
    out: list = []
    if m.get("audio_path") and os.path.exists(m["audio_path"]):
        with open(m["audio_path"], "rb") as f:
            out.append(AudioContent(type="audio", data=base64.b64encode(f.read()).decode(), mime_type=(m.get("mime") or "audio/webm").split(";")[0]))
    out.append(TextContent(type="text", text=json.dumps({"id": m["id"], "from": m["from_name"], "kind": m["kind"], "transcript": m.get("transcript") or "",
                                                          "say_before": f"{m['from_name']} left you a message." if m.get("audio_path") else f"{m['from_name']} sent you a message: {m.get('transcript') or ''}"})))
    return out


@server.tool(description="Mark a family message as played so it is not repeated (daily-repeat messages play again tomorrow).")
def mark_message_played(message_id: int) -> dict:
    m = db.message(message_id)
    if not m: return {"error": f"no message {message_id}"}
    if m.get("repeat_daily") and m.get("play_until") and m["play_until"] >= dt.date.today().isoformat():
        db.execute("UPDATE messages SET played_at=? WHERE id=?", (db.now_iso(), message_id))
        db.execute("UPDATE messages SET play_from=? WHERE id=?", ((dt.date.today() + dt.timedelta(days=1)).isoformat(), message_id))
    else:
        db.execute("UPDATE messages SET played_at=?, status='played' WHERE id=?", (db.now_iso(), message_id))
    return {"ok": True}


@server.tool(description="Open today's check-in record. A completed check-in from earlier today is kept in history and a fresh record is started; "
                         "an unfinished one is resumed. Returns the checkin_id used by record_answer and complete_checkin.")
def start_checkin(person_id: int) -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    date = core.today_str(p)
    existing = db.checkin_for(person_id, date)
    if existing and not existing.get("completed_at"):
        return {"checkin_id": existing["id"], "date": date, "resumed": True, "answered": [t["field"] for t in existing["transcript"]]}
    if existing:
        db.update_checkin(existing["id"], superseded=1)
    cid = db.execute("INSERT INTO checkins(person_id, date, started_at) VALUES (?,?,?)", (person_id, date, db.now_iso()))
    db.execute("DELETE FROM snoozes WHERE person_id=?", (person_id,))
    return {"checkin_id": cid, "date": date, "resumed": False, "answered": []}


@server.tool(description="Record one answer. field: mood, sleep, meds_taken, ate, concern, plans, note, 'question:<id>' or 'event:<id>'. "
                         "value is the interpreted answer (1-5, yes/no, or text); quote is what the person actually said. "
                         "Returns flags detected, the concern score, and a follow_up hint when something deserves a gentle second question.")
def record_answer(checkin_id: int, field: str, value: str, quote: str = "") -> dict:
    c = db.checkin(checkin_id)
    if not c: return {"error": f"no check-in {checkin_id}"}
    updates: dict = {}
    if field.startswith("question:"):
        qid = int(field.split(":")[1]); qrow = db.one("SELECT * FROM questions WHERE id=?", (qid,))
        if not qrow: return {"error": f"no question {qid}"}
        db.execute("UPDATE questions SET status='answered', asked_at=?, answer=?, checkin_id=? WHERE id=?", (db.now_iso(), quote or str(value), checkin_id, qid))
    elif field.startswith("event:"):
        eid = int(field.split(":")[1]); erow = db.event(eid)
        if not erow: return {"error": f"no event {eid}"}
        db.execute("UPDATE events SET status='mentioned', mentioned_at=?, response=? WHERE id=?", (db.now_iso(), quote or str(value), eid))
    elif field in ("mood", "sleep"):
        updates[field] = parse_scale(value)
    elif field in ("meds_taken", "ate"):
        updates[field] = parse_bool(value)
    elif field == "note":
        updates["concern"] = ((c.get("concern") or "") + " " + str(value)).strip()
    elif field in ("concern", "plans"):
        updates[field] = str(value)
    else:
        return {"error": f"unknown field {field}", "fields": FIELDS}
    new_flags = [f for f in core.detect_flags(f"{value} {quote}") if f not in c["flags"]]
    if field == "sleep" and updates.get("sleep") is not None and updates["sleep"] <= 2 and "no_sleep" not in c["flags"] + new_flags:
        new_flags.append("no_sleep")
    if field == "meds_taken" and updates.get("meds_taken") == 0 and "skipped_meds" not in c["flags"] + new_flags:
        new_flags.append("skipped_meds")
    flags = c["flags"] + new_flags
    transcript = c["transcript"] + [{"field": field, "value": str(updates.get(field, value)), "quote": quote}]
    merged = {**c, **updates, "flags": flags}
    risk = core.risk_score(merged)
    db.update_checkin(checkin_id, flags=flags, transcript=transcript, risk=risk, **updates)
    follow_up = None
    if "fall" in new_flags: follow_up = "Ask gently whether they are hurt and whether they can get up and move around normally."
    elif any(f in new_flags for f in ("chest_pain", "breathing", "emergency")):
        follow_up = "This may be an emergency. Ask if they need help right now; if yes call request_help with urgency=urgent and tell them to call emergency services."
    elif "dizzy" in new_flags or "confusion" in new_flags: follow_up = "Ask when it started and whether they have eaten and had water."
    elif field == "meds_taken" and updates.get("meds_taken") == 0: follow_up = "Encourage them to take it now and ask them to say when it's done."
    elif field == "meds_taken" and updates.get("meds_taken") is None: follow_up = "They weren't sure. Ask them to check the pill box and tell you."
    elif field == "mood" and updates.get("mood") is not None and updates["mood"] <= 2: follow_up = "Ask what's making today hard; listen, don't fix."
    elif field in ("mood", "sleep") and updates.get(field) is None: follow_up = "Answer was unclear. Offer a choice: good, okay, or not so good."
    return {"ok": True, "recorded": {field: updates.get(field, value)}, "flags_added": new_flags, "risk": risk, "risk_level": core.risk_level(risk), "follow_up": follow_up}


@server.tool(description="Finish the check-in: scores concern level, writes a one-paragraph summary for the family (with weekly trend insights when notable), "
                         "sends it to the primary contact, escalates to more contacts if the concern level is high, and clears missed-check-in alerts.")
def complete_checkin(checkin_id: int, summary: str = "") -> dict:
    c = db.checkin(checkin_id)
    if not c: return {"error": f"no check-in {checkin_id}"}
    p = db.person(c["person_id"])
    risk = core.risk_score(c); level = core.risk_level(risk)
    text = summary.strip() or core.build_summary(p, c)
    answered = db.q("SELECT from_name, text, answer FROM questions WHERE checkin_id=?", (checkin_id,))
    for qa in answered:
        text += f' {qa["from_name"]} asked "{qa["text"]}" and she said: "{(qa["answer"] or "")[:120]}".'
    mentioned = db.q("SELECT title, time, response FROM events WHERE person_id=? AND date=? AND status='mentioned'", (p["id"], c["date"]))
    for e in mentioned:
        text += f" Reminded about {e['title']}{' at ' + _fmt_time(e['time']) if e.get('time') else ''}" + (f': "{e["response"][:100]}".' if e.get("response") else ".")
    insights = core.trends(p["id"], 7, today=c["date"])["insights"]
    if insights: text += " This week: " + " ".join(insights)
    db.update_checkin(checkin_id, completed_at=db.now_iso(), risk=risk, summary=text)
    db.execute("UPDATE events SET status='done' WHERE person_id=? AND date=? AND status='mentioned'", (p["id"], c["date"]))
    resolved = core.resolve_missed_alerts(p["id"])
    primary = core.contacts_for_level(p["id"], "info")
    sent = core.notify(p["id"], primary, f"Hearth daily summary: {p['name']}", text)
    escalation = core.create_alert(p["id"], level, "check-in flagged concern", text) if level in ("concern", "urgent") else None
    first = sent[0]["contact"] if sent else "your family"
    closing = (f"Thanks for chatting with me. I've let {first} know you're doing okay." if level in ("ok", "watch")
               else "Thank you for telling me. I'm letting your family know right now so someone can check on you.")
    return {"ok": True, "risk": risk, "risk_level": level, "summary": text, "summary_sent_to": [s["contact"] for s in sent],
            "escalation": escalation, "missed_alerts_resolved": resolved, "closing_line": closing}


@server.tool(description="The person asked for help or described an emergency. Alerts every contact immediately with the reason. urgency: urgent (default) or concern.")
def request_help(person_id: int, reason: str, urgency: str = "urgent") -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    level = "urgent" if urgency != "concern" else "concern"
    alert = core.create_alert(person_id, level, "asked for help", reason)
    return {"ok": True, **alert, "say": "I've alerted your family. If this is an emergency, please call 911 now."}


@server.tool(description="The person wants to send a message to family ('tell Anna I love her'). Stores the transcript and optional audio "
                         "(base64) as a voice note the family sees in the dashboard. contact_name is optional; defaults to the primary contact.")
def record_reply(person_id: int, transcript: str, contact_name: str = "", audio_base64: str = "", mime: str = "audio/webm") -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    named = next((c for c in db.contacts(person_id) if contact_name and contact_name.lower() in c["name"].lower()), None)
    cs = core.active_contacts(person_id)
    target = named or (cs[0] if cs else None)
    mid = db.execute("INSERT INTO messages(person_id, direction, from_name, contact_id, kind, transcript, mime, created_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
                     (person_id, "to_family", p.get("nickname") or p["name"], target["id"] if target else None, "voice" if audio_base64 else "text", transcript, mime, db.now_iso(), "delivered"))
    if audio_base64:
        path = db.save_media(mid, base64.b64decode(audio_base64), mime)
        db.execute("UPDATE messages SET audio_path=? WHERE id=?", (path, mid))
    if target:
        core.notify(person_id, [target], f"Voice note from {p.get('nickname') or p['name']}", f"“{transcript}”")
    return {"ok": True, "message_id": mid, "to": target["name"] if target else None, "say": f"I'll pass that along to {target['name']}." if target else "I've saved that for your family."}


@server.tool(description="Add an appointment or reminder for the person ('remind me I have the dentist Friday at 10'). date is YYYY-MM-DD, time HH:MM optional. "
                         "It is raised during the check-in on that day, with a heads-up the day before when remind_day_before is true.")
def add_event(person_id: int, date: str, title: str, time: str = "", kind: str = "appointment", notes: str = "", added_by: str = "", remind_day_before: bool = True) -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        return {"error": "date must be YYYY-MM-DD"}
    eid = db.execute("INSERT INTO events(person_id, date, time, title, kind, notes, added_by, remind_day_before, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (person_id, date, time or "", title.strip(), kind, notes, added_by or (p.get("nickname") or p["name"]), int(bool(remind_day_before)), db.now_iso()))
    return {"ok": True, "event_id": eid, "say": f"Got it, {title} on {dt.date.fromisoformat(date).strftime('%A %B %-d') if os.name != 'nt' else dt.date.fromisoformat(date).strftime('%A %B %d').replace(' 0', ' ')}{' at ' + _fmt_time(time) if time else ''}. I'll remind you."}


@server.tool(description="Upcoming appointments and reminders for the next N days (default 7).")
def list_events(person_id: int, days: int = 7) -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    d = dt.date.fromisoformat(core.today_str(p))
    rows = db.events_between(person_id, d.isoformat(), (d + dt.timedelta(days=max(1, min(60, days)))).isoformat())
    return {"events": [{"id": e["id"], "date": e["date"], "time": _fmt_time(e.get("time") or ""), "title": e["title"], "kind": e["kind"], "status": e["status"], "notes": e.get("notes") or ""} for e in rows]}


@server.tool(description="Caregiver query, e.g. 'how is Mom today?': today's check-in state, concern level, summary, open alerts, pending messages, who is away.")
def get_status(person_id: int) -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
    st = core.status(p)
    st["open_alert_details"] = [{"level": a["level"], "reason": a["reason"], "detail": a["detail"], "at": a["created_at"]} for a in db.open_alerts(person_id)]
    st["trend_insights"] = core.trends(person_id, 7)["insights"]
    return st


@server.tool(description="The person wants to talk later. Pauses the missed-check-in escalation for the given minutes (max 180).")
def snooze_checkin(person_id: int, minutes: int = 30) -> dict:
    minutes = max(5, min(180, int(minutes)))
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    db.execute("INSERT INTO snoozes(person_id, until) VALUES (?,?) ON CONFLICT(person_id) DO UPDATE SET until=excluded.until", (person_id, until))
    return {"ok": True, "snoozed_until": until, "say": f"No problem, I'll check back in {minutes} minutes."}


@server.tool(description="Record that a medication was taken (or explicitly not taken) outside the check-in flow.")
def log_medication(person_id: int, medication: str, taken: bool = True) -> dict:
    p, err = _person_or_error(person_id)
    if err: return err
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
    extras = []
    if ctx["family_messages"]: extras.append("Play the family message(s) first with get_family_message, then mark_message_played.")
    if ctx["events_today"]: extras.append("Raise today's appointments and reminders and record the response with field 'event:<id>'.")
    if ctx["questions_from_family"]: extras.append("Ask the family's questions and record answers with field 'question:<id>'.")
    if ctx["away"]: extras.append("Mention who is away and who is covering, if it comes up.")
    return (f"You are running Hearth's daily check-in with {ctx['person']['nickname']}. Start with: \"{ctx['greeting']}\" "
            f"Cover, one at a time: {'; '.join(ctx['topics_in_order'])}. Medications due: {meds}. "
            f"Yesterday: {ctx['yesterday']['summary'] if ctx['yesterday'] else 'no record'}. " + " ".join(extras) + " "
            "Call start_checkin first, record_answer after each answer with the interpreted value and their exact words, "
            "follow any follow_up hint you get back, then complete_checkin. If they ask for help, call request_help at once. "
            "If they want to send a message to family, call record_reply. If they mention a future appointment, call add_event. "
            "Style: warm, unhurried, short sentences. Never give medical advice.")


TOOLS = {f.__name__: f for f in (get_checkin_context, get_family_message, mark_message_played, start_checkin, record_answer, complete_checkin,
                                  request_help, record_reply, add_event, list_events, get_status, snooze_checkin, log_medication, list_persons)}
