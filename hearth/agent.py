"""The simulator's agent host. Stands in for Alexa+ during the demo and drives the same MCP tools a real host would.
Two modes:
  scripted  - deterministic conversation policy with light natural-language handling; no API key, no cost. Default.
  llm       - any OpenAI-compatible chat endpoint with tool calling (HEARTH_LLM_BASE_URL, HEARTH_LLM_API_KEY, HEARTH_LLM_MODEL).
Every tool call is logged so the dashboard can show exactly what the host did."""
from __future__ import annotations
import datetime as dt, json, os, re, time, uuid, urllib.error, urllib.request
from typing import Any
from . import db, core
from . import mcp_server as M

SESSIONS: dict[str, dict] = {}


def _serialize(result: Any) -> Any:
    """Tool results are dicts, or lists of MCP content blocks (audio is replaced by a marker for logging)."""
    if isinstance(result, list):
        out = []
        for block in result:
            t = getattr(block, "type", None)
            if t == "text":
                try: out.append(json.loads(block.text))
                except Exception: out.append({"text": block.text})
            elif t == "audio":
                out.append({"audio": f"{len(block.data) * 3 // 4} bytes {block.mime_type}"})
            else:
                out.append(str(block))
        return out
    return result


def _call(session: dict, name: str, **args) -> Any:
    result = M.TOOLS[name](**args)
    session["tool_log"].append({"tool": name, "args": args, "result": _serialize(result)})
    return result


# ---------------------------------------------------------------- language helpers (scripted host)
HELP_RE = re.compile(r"\b(help|911|ambulance|emergency|can'?t (get|stand) up|can'?t (breathe|catch my breath)|catch my breath|short of breath|chest (pain|tight|tightness|pressure)|(tight|tightness|pressure) in (my |the )?chest|call (my )?(daughter|son|family|someone|anna|david|tom))\b", re.I)
HELP_NEG = re.compile(r"\b(don'?t|do not|no) (need )?(help|worry)|no emergency|not an emergency|i'?m (fine|okay|ok)\b", re.I)
LATER_RE = re.compile(r"^\W*(?:(?:not (?:right )?now|later|maybe later|call (?:me )?back later|can (?:we|you) (?:do this|talk|chat) later|i'?m busy(?: right now)?|in a (?:bit|little while|few minutes)|please)\W*)+$", re.I)
REPEAT_RE = re.compile(r"^\W*(what|pardon( me)?|sorry|say (that )?again|repeat that|what was that|come again|huh|i didn'?t (hear|catch) (that|you))\W*$", re.I)
WHO_RE = re.compile(r"\b(who are you|who is this|what is this|are you a (person|robot|computer))\b", re.I)
MSG_RE = re.compile(r"(?:^|[.,!?;]\s*|\b(?:and|also|oh)\s+)(?:can you |please |could you |would you )?(?:tell|let)\s+(\w+)\s+(?:know\s+)?(?:that\s+)?(.+)$|^\W*(?:send|give|leave)\s+(?:a\s+)?(?:message|note)\s+(?:to|for)\s+(\w+)[:,]?\s+(.+)$|^\W*message\s+for\s+(\w+)[:,]?\s+(.+)$", re.I)
APPT_RE = re.compile(r"\b(appointment|dentist|doctor|dr\.?\s+\w+|clinic|hairdresser|hair appointment|physio(therapy)?|check-?up|eye test|optician|podiatrist|the bank|blood test)\b", re.I)
DATE_RE = re.compile(r"\b(today|tomorrow|day after tomorrow|next week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|(?:on )?the (\d{1,2})(?:st|nd|rd|th)?|(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2})\b", re.I)
TIME_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|o'?clock|in the (?:morning|afternoon|evening))?\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

QUESTION_TEXT = {"mood": "How are you feeling today?", "sleep": "How did you sleep last night?", "meds_taken": "Did you take your {meds} this morning?",
                 "ate": "Have you had something to eat yet?", "concern": "Is anything bothering you today, anything at all?", "plans": "What are your plans for the day?"}
CLARIFY = {"mood": "Would you say good, okay, or not so good?", "sleep": "Was it a good night, an okay one, or a bad one?",
           "meds_taken": "Could you check the pill box for me and tell me if today's are gone?", "ate": "Did you have any breakfast?"}


def parse_date(text: str, today: dt.date) -> dt.date | None:
    m = DATE_RE.search(text)
    if not m: return None
    s = m.group(1).lower()
    if s == "today": return today
    if s == "tomorrow": return today + dt.timedelta(days=1)
    if s == "day after tomorrow": return today + dt.timedelta(days=2)
    if s == "next week": return today + dt.timedelta(days=7)
    if s in WEEKDAYS:
        delta = (WEEKDAYS.index(s) - today.weekday()) % 7 or 7
        return today + dt.timedelta(days=delta)
    if m.group(2):
        day = int(m.group(2)); y, mo = today.year, today.month
        if day < today.day: mo += 1
        if mo > 12: mo, y = 1, y + 1
        try: return dt.date(y, mo, day)
        except ValueError: return None
    parts = s.split()
    if parts[0] in MONTHS:
        try:
            d = dt.date(today.year, MONTHS.index(parts[0]) + 1, int(parts[1]))
            return d if d >= today else d.replace(year=today.year + 1)
        except ValueError: return None
    return None


def parse_time(text: str) -> str:
    text = DATE_RE.sub(" ", text)   # keep "the 12th" from being read as 12 o'clock
    m = TIME_RE.search(text)
    if not m: return ""
    h = int(m.group(1)); mi = int(m.group(2) or 0); suf = (m.group(3) or "").lower()
    if h > 23 or mi > 59: return ""
    if "pm" in suf or "p.m" in suf or "afternoon" in suf or "evening" in suf:
        if h < 12: h += 12
    elif "am" in suf or "a.m" in suf or "morning" in suf:
        if h == 12: h = 0
    elif h < 7:
        h += 12   # "at 2" is almost always 2 pm
    return f"{h:02d}:{mi:02d}"


def event_title(text: str) -> str:
    m = APPT_RE.search(text)
    if not m: return "Appointment"
    raw = m.group(1)
    with_m = re.search(r"\bwith\s+(dr\.?\s+\w+|the\s+\w+|\w+)", text, re.I)
    base = raw if not raw.lower().startswith("appointment") else "Appointment"
    if raw.lower() == "appointment" and with_m: base = f"Appointment with {with_m.group(1)}"
    return re.sub(r"\bdr\b", "Dr", base.strip().capitalize(), flags=re.I)


def extract_others(text: str, answered: set) -> dict[str, Any]:
    """Opportunistic facts from one utterance: 'slept fine and took my pills' answers two questions at once."""
    t = text.lower(); found: dict[str, Any] = {}
    m = re.search(r"\b(slept|sleep)\b", t)
    if "sleep" not in answered and m:
        clause = re.split(r",|\band\b|\bbut\b", t[m.start():])[0]      # judge only the sleep clause: "slept well and took my pills"
        v = M.parse_scale(clause)
        if v is not None: found["sleep"] = v
    if "meds_taken" not in answered and re.search(r"\b(pills?|meds|medication|medicine|tablets?)\b", t):
        v = M.parse_bool(t)
        if v is None and re.search(r"\b(took|taken|had)\b", t): v = 1
        if v is not None: found["meds_taken"] = v
    if "ate" not in answered and re.search(r"\b(ate|eaten|eat|breakfast|toast|eggs?|cereal|oatmeal|porridge|lunch|sandwich|soup)\b", t):
        v = M.parse_bool(t)
        if v is None and re.search(r"\b(had|ate|made|having)\b", t): v = 1
        if v is not None: found["ate"] = v
    return found


# ---------------------------------------------------------------- session
def start(person_id: int, mode: str = "scripted", model: str = "") -> dict:
    sid = uuid.uuid4().hex[:10]
    s = SESSIONS[sid] = {"id": sid, "person_id": person_id, "mode": mode, "model": model or "", "checkin_id": None, "queue": [], "pos": 0, "answered": set(),
                         "pending": None, "last_question": "", "clarified": set(), "history": [], "tool_log": [], "done": False, "play_audio": [], "messages": []}
    ctx = _call(s, "get_checkin_context", person_id=person_id)
    if "error" in ctx:
        s["done"] = True; return {"session_id": sid, "agent": ctx["error"], "done": True, "tool_calls": s["tool_log"]}
    s["ctx"] = ctx
    if mode == "llm":
        opening = _llm_turn(s, None)
        s["history"].append({"role": "assistant", "content": opening})
        return {"session_id": sid, "agent": opening, "done": s["done"], "tool_calls": s["tool_log"], "play_audio": s["play_audio"], "messages": s["messages"]}
    started = _call(s, "start_checkin", person_id=person_id)
    s["checkin_id"] = started["checkin_id"]
    for f in started.get("answered", []):
        if f in QUESTION_TEXT: s["answered"].add(f)
    # family messages first
    intro_bits = []
    for m in ctx.get("family_messages", []):
        blocks = _call(s, "get_family_message", message_id=m["id"])
        info = next((json.loads(b.text) for b in blocks if getattr(b, "type", "") == "text"), {})
        has_audio = any(getattr(b, "type", "") == "audio" for b in blocks)
        if has_audio: s["play_audio"].append(f"/api/media/{m['id']}")
        s["messages"].append({"from": m["from"], "transcript": m.get("transcript", ""), "has_audio": has_audio})
        intro_bits.append(info.get("say_before", f"{m['from']} left you a message."))
        _call(s, "mark_message_played", message_id=m["id"])
    short = [re.sub(r"\s+\d+\s*(mg|mcg|ml|units?)\b.*$", "", m, flags=re.I).strip() for m in (ctx.get("medications_due") or [])]
    meds = (" and ".join(short) if len(short) <= 2 else ", ".join(short[:-1]) + " and " + short[-1]) if short else "medication"
    q = [("field", f, QUESTION_TEXT[f].format(meds=meds)) for f in ("mood", "sleep", "meds_taken", "ate", "concern")]
    for e in ctx.get("events_today", []):
        when = f" at {e['time']}" if e.get("time") else " today"
        extra = f" {e['notes']}" if e.get("notes") else ""
        q.append(("event", e["id"], f"You've got {e['title']}{when}.{extra} Are you all set for that?"))
    for fq in ctx.get("questions_from_family", []):
        q.append(("question", fq["id"], f"{fq['from']} wanted me to ask: {fq['text']}"))
    q.append(("field", "plans", QUESTION_TEXT["plans"]))
    s["queue"] = q
    away = ctx.get("away") or []
    away_line = ""
    if away and away[0].get("who") and not intro_bits:      # a played family message usually covers this itself
        a = away[0]; away_line = f" {a['who']} is away until {_nice_date(a['until'])}" + (f", so {a['cover']} is your go-to this week." if a.get("cover") else ".")
    first = _next_question(s)
    opening = f"{ctx['greeting']} It's Hearth, checking in." + (" " + " ".join(intro_bits) if intro_bits else "") + away_line + (" " + first if first else "")
    s["last_question"] = first
    s["history"].append({"role": "assistant", "content": opening})
    return {"session_id": sid, "agent": opening, "done": False, "tool_calls": s["tool_log"], "play_audio": s["play_audio"], "messages": s["messages"]}


def _nice_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso); return d.strftime("%A the ") + str(d.day) + ("th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th"))
    except Exception:
        return iso


def _current(s: dict):
    while s["pos"] < len(s["queue"]):
        kind, key, text = s["queue"][s["pos"]]
        if not (kind == "field" and key in s["answered"]):
            return s["queue"][s["pos"]]
        s["pos"] += 1
    return None


def _next_question(s: dict) -> str:
    cur = _current(s)
    return cur[2] if cur else ""


def turn(session_id: str, text: str) -> dict:
    s = SESSIONS.get(session_id)
    if not s: return {"error": "unknown session"}
    if s["done"]: return {"agent": "This check-in is finished. Start a new one to talk again.", "done": True, "tool_calls": []}
    s["history"].append({"role": "user", "content": text})
    before = len(s["tool_log"])
    reply = _llm_turn(s, text) if s["mode"] == "llm" else _scripted_turn(s, text)
    s["history"].append({"role": "assistant", "content": reply})
    return {"agent": reply, "done": s["done"], "tool_calls": s["tool_log"][before:]}


def _scripted_turn(s: dict, text: str) -> str:
    pid, cid = s["person_id"], s["checkin_id"]
    t = text.strip()
    # 1. emergencies and explicit requests for help
    if HELP_RE.search(t) and not HELP_NEG.search(t):
        r = _call(s, "request_help", person_id=pid, reason=t, urgency="urgent")
        s["done"] = True
        return r.get("say", "I've alerted your family.") + " I'll stay right here with you."
    # 2. not now
    if LATER_RE.match(t):
        r = _call(s, "snooze_checkin", person_id=pid, minutes=30)
        s["done"] = True
        return r["say"]
    # 3. repeat / who are you
    if REPEAT_RE.match(t):
        return s["last_question"] or "Shall we carry on?"
    if WHO_RE.search(t):
        return "I'm Hearth, the daily check-in your family set up so they know you're doing alright. " + (s["last_question"] or "")
    # 4. message to family
    mm = MSG_RE.search(t); msg_ack = ""
    if mm:
        name = mm.group(1) or mm.group(3) or mm.group(5); body = mm.group(2) or mm.group(4) or mm.group(6)
        if name and name.lower() not in ("me", "you", "them", "him", "her"):
            r = _call(s, "record_reply", person_id=pid, transcript=body.strip(), contact_name=name)
            msg_ack = r.get("say", "I'll pass that along.")
            prefix = re.sub(r"[\s.,!?;:]+$", "", t[:mm.start()]).strip()      # "Good. Could you tell Anna..." still answers the question
            if len(prefix) < 2:
                return msg_ack + (" " + s["last_question"] if s["last_question"] else "")
            t = prefix
    return (msg_ack + " " + _scripted_answer(s, t)).strip()


def _scripted_answer(s: dict, t: str) -> str:
    pid, cid = s["person_id"], s["checkin_id"]
    # 5. a future appointment mentioned in passing
    cur = _current(s)
    today = dt.date.fromisoformat(s["ctx"]["status"]["date"])
    if APPT_RE.search(t) and DATE_RE.search(t) and not (cur and cur[0] == "event"):
        d = parse_date(t, today)
        if d and d >= today:
            r = _call(s, "add_event", person_id=pid, date=d.isoformat(), title=event_title(t), time=parse_time(t), kind="appointment", added_by=s["ctx"]["person"]["nickname"])
            note = _call(s, "record_answer", checkin_id=cid, field="note", value=t, quote=t)
            return r.get("say", "Noted.") + (" " + s["last_question"] if s["last_question"] and not s["pending"] else "")
    ack = ""
    # 6. answer to a pending follow-up
    if s["pending"]:
        pend = s["pending"]; s["pending"] = None
        if pend.get("field") == "meds_taken" and pend.get("kind") == "recheck":
            r = _call(s, "record_answer", checkin_id=cid, field="meds_taken", value=t, quote=t)
            v = r.get("recorded", {}).get("meds_taken")
            ack = "Good, thank you." if v == 1 else "Okay, I'll let your family know so someone can remind you." if v == 0 else "Alright."
            s["answered"].add("meds_taken")
        elif pend.get("kind") == "clarify":
            f = pend["field"]
            r = _call(s, "record_answer", checkin_id=cid, field=f, value=t, quote=t)
            if r.get("recorded", {}).get(f) is None:
                _call(s, "record_answer", checkin_id=cid, field="note", value=t, quote=t)
            s["answered"].add(f); ack = _ack(f, r)
        else:
            r = _call(s, "record_answer", checkin_id=cid, field="note", value=t, quote=t)
            emergency_hint = "emergency" in (pend.get("hint") or "").lower()
            if any(f in r.get("flags_added", []) for f in ("chest_pain", "breathing", "emergency")) or (emergency_hint and (M.parse_bool(t) == 1 or HELP_RE.search(t)) and not HELP_NEG.search(t)):
                h = _call(s, "request_help", person_id=pid, reason=t, urgency="urgent"); s["done"] = True
                return h.get("say", "I've alerted your family.") + " I'll stay right here with you."
            ack = "Thank you for telling me."
    else:
        if not cur:
            return _finish(s)
        kind, key, qtext = cur
        if kind == "field":
            r = _call(s, "record_answer", checkin_id=cid, field=key, value=t, quote=t)
            s["pos"] += 1
            ack = _ack(key, r)
            val = r.get("recorded", {}).get(key)
            if r.get("flags_added"):
                s["answered"].add(key); s["pending"] = {"field": key, "kind": "flag", "hint": r.get("follow_up")}
                q = _follow_up_question(r["flags_added"]); s["last_question"] = q
                return f"{ack} {q}"
            if key in ("mood", "sleep") and val is None and key not in s["clarified"]:
                s["clarified"].add(key); s["pos"] -= 1
                s["pending"] = {"field": key, "kind": "clarify"}; s["last_question"] = CLARIFY[key]
                return f"{ack} {CLARIFY[key]}"
            if key == "meds_taken" and val is None and key not in s["clarified"]:
                s["clarified"].add(key); s["pending"] = {"field": key, "kind": "recheck"}; s["last_question"] = CLARIFY[key]
                return f"{ack} {CLARIFY[key]}"
            if key == "meds_taken" and val == 0:
                s["answered"].add(key); s["pending"] = {"field": key, "kind": "recheck"}
                s["last_question"] = "Could you take it now, and tell me when you have?"; return f"{ack} {s['last_question']}"
            if key == "mood" and val is not None and val <= 2:
                s["answered"].add(key); s["pending"] = {"field": key, "kind": "flag"}; s["last_question"] = "What's making today hard?"
                return f"{ack} {s['last_question']}"
            s["answered"].add(key)
            for f2, v2 in extract_others(t, s["answered"]).items():
                _call(s, "record_answer", checkin_id=cid, field=f2, value=str(v2), quote=t); s["answered"].add(f2)
        elif kind == "event":
            r = _call(s, "record_answer", checkin_id=cid, field=f"event:{key}", value=t, quote=t)
            s["pos"] += 1
            ack = "Good." if M.parse_bool(t) == 1 else "Okay, I've noted that for your family." if M.parse_bool(t) == 0 else "Okay."
            if r.get("flags_added"):
                s["pending"] = {"field": "note", "kind": "flag"}; q = _follow_up_question(r["flags_added"]); s["last_question"] = q
                return f"{ack} {q}"
        elif kind == "question":
            _call(s, "record_answer", checkin_id=cid, field=f"question:{key}", value=t, quote=t)
            s["pos"] += 1; ack = "I'll let them know."
    nxt = _next_question(s)
    if not nxt:
        return f"{ack} {_finish(s)}".strip()
    s["last_question"] = nxt
    return f"{ack} {nxt}".strip()


def _finish(s: dict) -> str:
    done = _call(s, "complete_checkin", checkin_id=s["checkin_id"])
    s["done"] = True
    heads = s["ctx"].get("events_tomorrow") or []
    tail = ""
    if heads:
        e = heads[0]; tail = f" One more thing: tomorrow you've got {e['title']}" + (f" at {e['time']}" if e.get("time") else "") + ". I'll remind you in the morning."
    return done.get("closing_line", "Thanks for talking with me.") + tail


def _ack(field: str, r: dict) -> str:
    v = r.get("recorded", {}).get(field)
    if field in ("mood", "sleep") and isinstance(v, int):
        return {1: "I'm sorry to hear that.", 2: "That sounds hard.", 3: "Alright.", 4: "Glad to hear it.", 5: "That's wonderful."}[v]
    if field == "meds_taken": return "Good, thank you." if v == 1 else ("Okay." if v == 0 else "Alright.")
    if field == "ate": return "Good." if v == 1 else ("Okay." if v == 0 else "Alright.")
    if field == "concern": return "Thank you for telling me." if not core.NEG_ANSWER.match(str(r.get("recorded", {}).get("concern", ""))) else "Good."
    return "Okay."


def _follow_up_question(flags: list[str]) -> str:
    if "fall" in flags: return "Are you hurt at all, and can you get up and move around normally?"
    if any(f in flags for f in ("chest_pain", "breathing", "emergency")): return "Do you need help right now? I can alert your family immediately."
    if "dizzy" in flags or "confusion" in flags: return "When did that start, and have you had some water and something to eat?"
    if "lonely" in flags: return "I'm sorry. Would you like me to let Anna know you'd enjoy a call today?"
    if "skipped_meds" in flags: return "Could you take them now, and tell me when you have?"
    if "no_food" in flags: return "Is there something easy you could have now, even a piece of toast?"
    return "Can you tell me a little more about that?"


# ---------------------------------------------------------------- optional LLM host

def _tool_schemas() -> list[dict]:
    """The same tool descriptions and JSON schemas a real host gets from tools/list, in OpenAI function-calling shape."""
    out = []
    for t in M.server._tool_manager.list_tools():
        if t.name not in M.TOOLS: continue
        params = dict(t.parameters or {"type": "object", "properties": {}})
        params.pop("title", None)
        out.append({"type": "function", "function": {"name": t.name, "description": (t.description or t.name)[:1000], "parameters": params}})
    return out


def _skill_text() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skill", "SKILL.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    if text.startswith("---"):                       # drop the frontmatter block
        end = text.find("\n---", 3)
        text = text[end + 4:] if end > 0 else text
    return text.strip()


def _checkin_state(s: dict) -> str:
    """Deterministic scaffold for the model: what has been recorded, what is left. The host tracks state so the model needn't."""
    ctx = s.get("ctx") or {}
    cid = None; recorded = {}; started = False; finished = False
    for t in s["tool_log"]:
        if t["tool"] == "start_checkin" and isinstance(t["result"], dict) and t["result"].get("checkin_id"):
            cid = t["result"]["checkin_id"]; started = True
            for f in t["result"].get("answered") or []: recorded.setdefault(f, "(earlier)")
        if t["tool"] == "record_answer" and isinstance(t["result"], dict) and t["result"].get("ok"):
            recorded[t["args"].get("field", "?")] = t["args"].get("value", "")
        if t["tool"] == "complete_checkin": finished = True
    topics = ["mood", "sleep", "meds_taken", "ate", "concern"] + [f"event:{e['id']}" for e in ctx.get("events_today", [])] + \
             [f"question:{q['id']}" for q in ctx.get("questions_from_family", [])] + ["plans"]
    remaining = [t for t in topics if t not in recorded]
    if finished: return "Check-in state: COMPLETED. Just respond warmly to anything else; do not record more answers."
    if not started: return "Check-in state: not started. Call start_checkin first (person_id above)."
    played = [t["args"].get("message_id") for t in s["tool_log"] if t["tool"] == "mark_message_played"]
    unplayed = [m["id"] for m in ctx.get("family_messages", []) if m["id"] not in played]
    lines = [f"Check-in state: checkin_id={cid}. Do NOT call start_checkin again.",
             "Recorded so far: " + (", ".join(f"{k}={str(v)[:30]}" for k, v in recorded.items()) or "nothing"),
             "Still to cover, in order: " + (", ".join(remaining) or "nothing; call complete_checkin now and speak its closing_line")]
    if unplayed: lines.append(f"Family messages not yet played: {unplayed} (get_family_message then mark_message_played).")
    return " ".join(lines)


def _system_prompt(s: dict) -> str:
    return (_skill_text() + "\n\n## This session\n\n" + M.daily_checkin(s["person_id"]) +
            f" The person_id is {s['person_id']}. You are speaking aloud through a device: plain spoken sentences, no markdown, no lists, "
            "no numbers or scales read out to the person, no stage directions or parentheticals, under 30 words per reply, one question at a time, no need to repeat back what they said. "
            "Interpret answers yourself and record them with record_answer before you reply: value is a 1-5 number for mood and sleep, yes or no for meds_taken and ate, and a short phrase for concern, plans, note, event and question fields; quote is always their exact words. Say medication names without the dosages. "
            "When a family message has audio, the device plays it when you call get_family_message; just say who it is from and continue. "
            "Finish with complete_checkin and speak its closing_line.\n\n" + _checkin_state(s) +
            ("\n\nContext from get_checkin_context: " + json.dumps(s["ctx"])[:6000] if s.get("ctx") else ""))


def _call_chat_completions(base: str, key: str, model: str, system: str, history: list[dict]) -> dict:
    """OpenAI-style chat completions (Bedrock Mantle, Groq, OpenAI, vLLM...). Returns an OpenAI-style assistant message."""
    body = {"model": model, "messages": [{"role": "system", "content": system}] + history, "tools": _tool_schemas(), "temperature": 0.4, "max_tokens": 1200}
    if model.startswith("openai.gpt-oss"): body["reasoning_effort"] = "low"
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
    msg = resp["choices"][0]["message"]
    msg["content"] = re.sub(r"<reasoning>.*?</reasoning>\s*", "", msg.get("content") or "", flags=re.S).strip()
    return {"role": "assistant", "content": msg["content"], "tool_calls": msg.get("tool_calls") or []}


def _to_converse(history: list[dict]) -> list[dict]:
    """OpenAI-style history -> Bedrock Converse messages (tool results merge into one user message)."""
    out = []
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": [{"text": m["content"] or "..."}]})
        elif m["role"] == "assistant":
            blocks = [{"text": m["content"]}] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                try: inp = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError: inp = {}
                blocks.append({"toolUse": {"toolUseId": c["id"], "name": c["function"]["name"], "input": inp}})
            out.append({"role": "assistant", "content": blocks or [{"text": "..."}]})
        elif m["role"] == "tool":
            try: payload = json.loads(m["content"])
            except json.JSONDecodeError: payload = {"text": m["content"]}
            if not isinstance(payload, dict): payload = {"content": payload}
            block = {"toolResult": {"toolUseId": m["tool_call_id"], "content": [{"json": payload}]}}
            if out and out[-1]["role"] == "user" and any("toolResult" in b for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def _call_converse(base: str, key: str, model: str, system: str, history: list[dict]) -> dict:
    """Bedrock Converse API (Claude, Nova, ...) with a Bedrock API key. Returns an OpenAI-style assistant message."""
    tools = [{"toolSpec": {"name": t["function"]["name"], "description": t["function"]["description"], "inputSchema": {"json": t["function"]["parameters"]}}} for t in _tool_schemas()]
    body = {"system": [{"text": system}], "messages": _to_converse(history), "toolConfig": {"tools": tools}, "inferenceConfig": {"maxTokens": 700, "temperature": 0.4}}
    req = urllib.request.Request(f"{base}/model/{model}/converse", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
    content, calls = [], []
    for b in resp["output"]["message"]["content"]:
        if "text" in b: content.append(b["text"])
        if "toolUse" in b:
            tu = b["toolUse"]; calls.append({"id": tu["toolUseId"], "type": "function", "function": {"name": tu["name"], "arguments": json.dumps(tu.get("input") or {})}})
    text = re.sub(r"<thinking>.*?</thinking>\s*", "", "\n".join(content), flags=re.S).strip()
    return {"role": "assistant", "content": text, "tool_calls": calls}


def _checkin_state(s: dict) -> str:
    """Deterministic scaffold for the model: what has been recorded, what is left. The host tracks state so the model needn't."""
    ctx = s.get("ctx") or {}
    cid = None; recorded = {}; started = False; finished = False
    for t in s["tool_log"]:
        if t["tool"] == "start_checkin" and isinstance(t["result"], dict) and t["result"].get("checkin_id"):
            cid = t["result"]["checkin_id"]; started = True
            for f in t["result"].get("answered") or []: recorded.setdefault(f, "(earlier)")
        if t["tool"] == "record_answer" and isinstance(t["result"], dict) and t["result"].get("ok"):
            recorded[t["args"].get("field", "?")] = t["args"].get("value", "")
        if t["tool"] == "complete_checkin": finished = True
    topics = ["mood", "sleep", "meds_taken", "ate", "concern"] + [f"event:{e['id']}" for e in ctx.get("events_today", [])] + \
             [f"question:{q['id']}" for q in ctx.get("questions_from_family", [])] + ["plans"]
    remaining = [t for t in topics if t not in recorded]
    if finished: return "Check-in state: COMPLETED. Just respond warmly to anything else; do not record more answers."
    if not started: return "Check-in state: not started. Call start_checkin first (person_id above)."
    played = [t["args"].get("message_id") for t in s["tool_log"] if t["tool"] == "mark_message_played"]
    unplayed = [m["id"] for m in ctx.get("family_messages", []) if m["id"] not in played]
    lines = [f"Check-in state: checkin_id={cid}. Do NOT call start_checkin again.",
             "Recorded so far: " + (", ".join(f"{k}={str(v)[:30]}" for k, v in recorded.items()) or "nothing"),
             "Still to cover, in order: " + (", ".join(remaining) or "nothing; call complete_checkin now and speak its closing_line")]
    if unplayed: lines.append(f"Family messages not yet played: {unplayed} (get_family_message then mark_message_played).")
    return " ".join(lines)


def _system_prompt(s: dict) -> str:
    return (_skill_text() + "\n\n## This session\n\n" + M.daily_checkin(s["person_id"]) +
            f" The person_id is {s['person_id']}. You are speaking aloud through a device: plain spoken sentences, no markdown, no lists, "
            "no numbers or scales read out to the person, no stage directions or parentheticals, under 40 words per reply, one question at a time. "
            "Interpret answers yourself (a 1-5 number or yes/no in value; their exact words in quote) and record them with record_answer before you reply. "
            "When a family message has audio, the device plays it when you call get_family_message; just say who it is from and continue. "
            "Finish with complete_checkin and speak its closing_line.\n\n" + _checkin_state(s) +
            ("\n\nContext from get_checkin_context: " + json.dumps(s["ctx"])[:6000] if s.get("ctx") else ""))


def _call_chat_completions(base: str, key: str, model: str, system: str, history: list[dict]) -> dict:
    """OpenAI-style chat completions (Bedrock Mantle, Groq, OpenAI, vLLM...). Returns an OpenAI-style assistant message."""
    body = {"model": model, "messages": [{"role": "system", "content": system}] + history, "tools": _tool_schemas(), "temperature": 0.4, "max_tokens": 1200}
    if model.startswith("openai.gpt-oss"): body["reasoning_effort"] = "low"
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
    msg = resp["choices"][0]["message"]
    msg["content"] = re.sub(r"<reasoning>.*?</reasoning>\s*", "", msg.get("content") or "", flags=re.S).strip()
    return {"role": "assistant", "content": msg["content"], "tool_calls": msg.get("tool_calls") or []}


def _to_converse(history: list[dict]) -> list[dict]:
    """OpenAI-style history -> Bedrock Converse messages (tool results merge into one user message)."""
    out = []
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": [{"text": m["content"] or "..."}]})
        elif m["role"] == "assistant":
            blocks = [{"text": m["content"]}] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                try: inp = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError: inp = {}
                blocks.append({"toolUse": {"toolUseId": c["id"], "name": c["function"]["name"], "input": inp}})
            out.append({"role": "assistant", "content": blocks or [{"text": "..."}]})
        elif m["role"] == "tool":
            try: payload = json.loads(m["content"])
            except json.JSONDecodeError: payload = {"text": m["content"]}
            if not isinstance(payload, dict): payload = {"content": payload}
            block = {"toolResult": {"toolUseId": m["tool_call_id"], "content": [{"json": payload}]}}
            if out and out[-1]["role"] == "user" and any("toolResult" in b for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def _call_converse(base: str, key: str, model: str, system: str, history: list[dict]) -> dict:
    """Bedrock Converse API (Claude, Nova, ...) with a Bedrock API key. Returns an OpenAI-style assistant message."""
    tools = [{"toolSpec": {"name": t["function"]["name"], "description": t["function"]["description"], "inputSchema": {"json": t["function"]["parameters"]}}} for t in _tool_schemas()]
    body = {"system": [{"text": system}], "messages": _to_converse(history), "toolConfig": {"tools": tools}, "inferenceConfig": {"maxTokens": 700, "temperature": 0.4}}
    req = urllib.request.Request(f"{base}/model/{model}/converse", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
    content, calls = [], []
    for b in resp["output"]["message"]["content"]:
        if "text" in b: content.append(b["text"])
        if "toolUse" in b:
            tu = b["toolUse"]; calls.append({"id": tu["toolUseId"], "type": "function", "function": {"name": tu["name"], "arguments": json.dumps(tu.get("input") or {})}})
    text = re.sub(r"<thinking>.*?</thinking>\s*", "", "\n".join(content), flags=re.S).strip()
    return {"role": "assistant", "content": text, "tool_calls": calls}


def llm_models() -> list[dict]:
    """Hosts the simulator can offer: the configured model plus, on Bedrock, the two model families Alexa+ itself runs on."""
    base = os.environ.get("HEARTH_LLM_BASE_URL", ""); default = os.environ.get("HEARTH_LLM_MODEL", "")
    if not base or not os.environ.get("HEARTH_LLM_API_KEY"): return []
    out = []
    if "bedrock-runtime" in base:
        out = [{"id": "us.anthropic.claude-sonnet-4-6", "label": "Claude Sonnet 4.6 on Amazon Bedrock"}, {"id": "us.amazon.nova-2-lite-v1:0", "label": "Amazon Nova 2 Lite on Amazon Bedrock"}]
    if default and default not in [m["id"] for m in out]:
        out.insert(0, {"id": default, "label": f"{default} (configured)"})
    out.sort(key=lambda m: m["id"] != default)          # the configured model first
    return out


def _llm_turn(s: dict, user_text: str | None) -> str:
    base = os.environ.get("HEARTH_LLM_BASE_URL", "").rstrip("/"); key = os.environ.get("HEARTH_LLM_API_KEY", ""); model = s.get("model") or os.environ.get("HEARTH_LLM_MODEL", "")
    protocol = os.environ.get("HEARTH_LLM_PROTOCOL", "converse" if "bedrock-runtime" in base else "chat")
    if not base or not model:
        s["done"] = True
        return "LLM mode needs HEARTH_LLM_BASE_URL, HEARTH_LLM_API_KEY and HEARTH_LLM_MODEL. Use scripted mode instead."
    if user_text is None:
        s["history"].append({"role": "user", "content": "(The person is listening. Greet them and begin.)"})
    call = _call_converse if protocol == "converse" else _call_chat_completions
    for _ in range(8):
        msg = None
        for attempt, pause in enumerate((0, 3, 6, 10, 15)):
            if pause: time.sleep(pause)
            try:
                msg = call(base, key, model, _system_prompt(s), s["history"]); break
            except urllib.error.HTTPError as ex:
                detail = ex.read().decode(errors="replace")[:300]
                transient = ex.code in (429, 500, 502, 503, 504) or (ex.code == 404 and "use case" in detail)   # throttling, or access still propagating
                if not transient or attempt == 4:
                    return f"(LLM error: HTTP {ex.code}: {detail})"
            except Exception as ex:
                return f"(LLM error: {ex})"
        calls = msg.get("tool_calls") or []
        content = re.sub(r"\((?:wait|user|pause|listen)[^)]*\)", "", msg.get("content") or "", flags=re.I)
        content = re.sub(r"[*_#`]+", "", content).strip()                      # spoken aloud: no markdown
        if not calls:
            if not content and not s.get("_nudged"):              # recorded but said nothing: ask for the spoken line once
                s["_nudged"] = True
                s["history"].append({"role": "user", "content": "(Say your next line to the person now.)"})
                continue
            if s.get("_nudged"):
                s["_nudged"] = False
                s["history"] = [m for m in s["history"] if m.get("content") != "(Say your next line to the person now.)"]
            if any(t["tool"] in ("complete_checkin", "request_help", "snooze_checkin") for t in s["tool_log"]):
                s["done"] = True
            return content or "I'm here whenever you're ready."
        s["history"].append({"role": "assistant", "content": content, "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name in M.TOOLS:
                try:
                    result = _serialize(_call(s, name, **args))
                except TypeError as ex:
                    result = {"error": f"bad arguments: {ex}"}
            else:
                result = {"error": "unknown tool"}
            if name == "get_family_message":
                mid = args.get("message_id"); m = db.message(int(mid)) if mid else None
                if m:
                    if m.get("audio_path"): s["play_audio"].append(f"/api/media/{mid}")
                    s["messages"].append({"from": m["from_name"], "transcript": m.get("transcript") or "", "has_audio": bool(m.get("audio_path"))})
            s["history"].append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
    return "(too many tool calls)"
