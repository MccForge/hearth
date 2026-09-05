"""The simulator's agent host. Stands in for Alexa+ during the demo and drives the same MCP tools a real host would.
Two modes:
  scripted  - deterministic conversation policy with light natural-language handling; no API key, no cost. Default.
  llm       - any OpenAI-compatible chat endpoint with tool calling (HEARTH_LLM_BASE_URL, HEARTH_LLM_API_KEY, HEARTH_LLM_MODEL).
Every tool call is logged so the dashboard can show exactly what the host did."""
from __future__ import annotations
import datetime as dt, inspect, json, os, re, uuid, urllib.request
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
HELP_RE = re.compile(r"\b(help|911|ambulance|emergency|can'?t get up|can'?t breathe|chest pain|call (my )?(daughter|son|family|someone|anna|david|tom))\b", re.I)
HELP_NEG = re.compile(r"\b(don'?t|do not|no) (need )?(help|worry)|no emergency|not an emergency|i'?m (fine|okay|ok)\b", re.I)
LATER_RE = re.compile(r"^\W*(?:(?:not (?:right )?now|later|maybe later|call (?:me )?back later|can (?:we|you) (?:do this|talk|chat) later|i'?m busy(?: right now)?|in a (?:bit|little while|few minutes)|please)\W*)+$", re.I)
REPEAT_RE = re.compile(r"^\W*(what|pardon( me)?|sorry|say (that )?again|repeat that|what was that|come again|huh|i didn'?t (hear|catch) (that|you))\W*$", re.I)
WHO_RE = re.compile(r"\b(who are you|who is this|what is this|are you a (person|robot|computer))\b", re.I)
MSG_RE = re.compile(r"^\W*(?:can you |please |could you )?(?:tell|let)\s+(\w+)\s+(?:know\s+)?(?:that\s+)?(.+)$|^\W*(?:send|give|leave)\s+(?:a\s+)?(?:message|note)\s+(?:to|for)\s+(\w+)[:,]?\s+(.+)$|^\W*message\s+for\s+(\w+)[:,]?\s+(.+)$", re.I)
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
def start(person_id: int, mode: str = "scripted") -> dict:
    sid = uuid.uuid4().hex[:10]
    s = SESSIONS[sid] = {"id": sid, "person_id": person_id, "mode": mode, "checkin_id": None, "queue": [], "pos": 0, "answered": set(),
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
    mm = MSG_RE.match(t)
    if mm:
        name = mm.group(1) or mm.group(3) or mm.group(5); body = mm.group(2) or mm.group(4) or mm.group(6)
        if name and name.lower() not in ("me", "you", "them"):
            r = _call(s, "record_reply", person_id=pid, transcript=body.strip(), contact_name=name)
            return r.get("say", "I'll pass that along.") + (" " + s["last_question"] if s["last_question"] else "")
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
            if any(f in r.get("flags_added", []) for f in ("chest_pain", "breathing", "emergency")):
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
    out = []
    for name, f in M.TOOLS.items():
        props, req = {}, []
        for pname, p in inspect.signature(f).parameters.items():
            t = {int: "integer", bool: "boolean", str: "string"}.get(p.annotation, "string")
            props[pname] = {"type": t}
            if p.default is inspect._empty: req.append(pname)
        out.append({"type": "function", "function": {"name": name, "description": (f.__doc__ or name)[:400], "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


def _llm_turn(s: dict, user_text: str | None) -> str:
    base = os.environ.get("HEARTH_LLM_BASE_URL", "").rstrip("/"); key = os.environ.get("HEARTH_LLM_API_KEY", ""); model = os.environ.get("HEARTH_LLM_MODEL", "")
    if not base or not model:
        s["done"] = True
        return "LLM mode needs HEARTH_LLM_BASE_URL, HEARTH_LLM_API_KEY and HEARTH_LLM_MODEL. Use scripted mode instead."
    system = M.daily_checkin(s["person_id"]) + " Speak as the assistant; keep each reply under 40 words."
    msgs = [{"role": "system", "content": system}] + s["history"]
    if user_text is None: msgs.append({"role": "user", "content": "(The person is listening. Greet them and begin.)"})
    for _ in range(8):
        body = json.dumps({"model": model, "messages": msgs, "tools": _tool_schemas(), "temperature": 0.4}).encode()
        req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        except Exception as ex:
            return f"(LLM error: {ex})"
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            content = msg.get("content") or "..."
            if any(t["tool"] in ("complete_checkin", "request_help", "snooze_checkin") for t in s["tool_log"]):
                s["done"] = True
            return content
        msgs.append(msg)
        for c in calls:
            name = c["function"]["name"]; args = json.loads(c["function"].get("arguments") or "{}")
            result = _serialize(_call(s, name, **args)) if name in M.TOOLS else {"error": "unknown tool"}
            if name == "get_family_message":
                mid = args.get("message_id"); m = db.message(int(mid)) if mid else None
                if m and m.get("audio_path"): s["play_audio"].append(f"/api/media/{mid}")
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
    return "(too many tool calls)"
