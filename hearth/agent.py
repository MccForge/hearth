"""The simulator's agent host. Stands in for Alexa+ during the demo and drives the same MCP tools a real host would.
Two modes:
  scripted  - deterministic conversation policy, no API key, no cost. Used by default.
  llm       - any OpenAI-compatible chat endpoint with tool calling (set HEARTH_LLM_BASE_URL, HEARTH_LLM_API_KEY, HEARTH_LLM_MODEL).
Every tool call is logged so the dashboard can show exactly what the host did."""
from __future__ import annotations
import inspect, json, os, re, uuid, urllib.request
from typing import Any
from . import mcp_server as M

SESSIONS: dict[str, dict] = {}


def _call(session: dict, name: str, **args) -> dict:
    result = M.TOOLS[name](**args)
    session["tool_log"].append({"tool": name, "args": args, "result": result})
    return result


# ---------------------------------------------------------------- scripted policy
STEPS = [
    ("mood", "How are you feeling today?"),
    ("sleep", "How did you sleep last night?"),
    ("meds_taken", "Did you take your {meds} this morning?"),
    ("ate", "Have you had something to eat yet?"),
    ("concern", "Is anything bothering you today, anything at all?"),
    ("plans", "What are your plans for the day?"),
]
HELP_RE = re.compile(r"\b(help|911|ambulance|emergency|call (my )?(daughter|son|family|someone)|can'?t get up)\b", re.I)
LATER_RE = re.compile(r"\b(later|not now|busy|call back|in a (bit|while|few))\b", re.I)
NEG_RE = re.compile(r"\b(no|nothing|not really|nope|all good|i'?m fine|nah)\b", re.I)


def start(person_id: int, mode: str = "scripted") -> dict:
    sid = uuid.uuid4().hex[:10]
    s = SESSIONS[sid] = {"id": sid, "person_id": person_id, "mode": mode, "step": 0, "checkin_id": None, "pending_follow_up": None,
                         "history": [], "tool_log": [], "done": False}
    ctx = _call(s, "get_checkin_context", person_id=person_id)
    if "error" in ctx:
        s["done"] = True; return {"session_id": sid, "agent": ctx["error"], "done": True, "tool_calls": s["tool_log"]}
    s["ctx"] = ctx
    if mode == "llm":
        opening = _llm_turn(s, None)
    else:
        started = _call(s, "start_checkin", person_id=person_id)
        s["checkin_id"] = started["checkin_id"]
        first = STEPS[0][1]
        opening = f"{ctx['greeting']} It's Hearth, checking in. {first}"
    s["history"].append({"role": "assistant", "content": opening})
    return {"session_id": sid, "agent": opening, "done": False, "tool_calls": s["tool_log"][-3:]}


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
    if HELP_RE.search(text) and not NEG_RE.search(text):
        r = _call(s, "request_help", person_id=pid, reason=text, urgency="urgent")
        s["done"] = True
        return r.get("say", "I've alerted your family.") + " I'll stay right here with you."
    if LATER_RE.search(text) and s["step"] == 0 and not s["pending_follow_up"]:
        r = _call(s, "snooze_checkin", person_id=pid, minutes=30)
        s["done"] = True
        return r["say"]
    if s["pending_follow_up"]:
        _call(s, "record_answer", checkin_id=cid, field="note", value=text, quote=text)
        s["pending_follow_up"] = None
        ack = "Thank you for telling me."
    else:
        field = STEPS[s["step"]][0]
        r = _call(s, "record_answer", checkin_id=cid, field=field, value=text, quote=text)
        s["step"] += 1
        ack = _ack(field, r)
        if r.get("follow_up") and r.get("flags_added"):
            s["pending_follow_up"] = r["follow_up"]
            return f"{ack} {_follow_up_question(r['flags_added'])}"
        if r.get("follow_up") and field in ("meds_taken", "mood"):
            s["pending_follow_up"] = r["follow_up"]
            return f"{ack} {'Could you take it now, and tell me when you have?' if field == 'meds_taken' else 'What is making today hard?'}"
    if s["step"] >= len(STEPS):
        done = _call(s, "complete_checkin", checkin_id=cid)
        s["done"] = True
        return f"{ack} {done.get('closing_line', 'Thanks for talking with me.')}"
    meds = ", ".join(s["ctx"].get("medications_due") or []) or "medication"
    return f"{ack} {STEPS[s['step']][1].format(meds=meds)}"


def _ack(field: str, r: dict) -> str:
    v = r.get("recorded", {}).get(field)
    if field in ("mood", "sleep") and isinstance(v, int):
        return {1: "I'm sorry to hear that.", 2: "That sounds hard.", 3: "Alright.", 4: "Glad to hear it.", 5: "That's wonderful."}[v]
    if field == "meds_taken": return "Good, thank you." if v == 1 else ("Okay." if v == 0 else "Alright.")
    if field == "ate": return "Good." if v == 1 else "Okay."
    return "Okay."


def _follow_up_question(flags: list[str]) -> str:
    if "fall" in flags: return "Are you hurt at all, and can you get up and move around normally?"
    if any(f in flags for f in ("chest_pain", "breathing", "emergency")): return "Do you need help right now? I can alert your family immediately."
    if "dizzy" in flags or "confusion" in flags: return "When did that start, and have you had some water and something to eat?"
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
    for _ in range(6):
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
            if any(t["tool"] == "complete_checkin" for t in s["tool_log"]) or any(t["tool"] == "request_help" for t in s["tool_log"]):
                s["done"] = True
            return content
        msgs.append(msg)
        for c in calls:
            name = c["function"]["name"]; args = json.loads(c["function"].get("arguments") or "{}")
            result = _call(s, name, **args) if name in M.TOOLS else {"error": "unknown tool"}
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
    return "(too many tool calls)"
