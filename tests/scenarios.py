"""Scenario suite: a dozen different Margarets talk to the host, and we check what Hearth recorded and who it alerted.

Runs against the in-process host (no HTTP): the scripted host for free, deterministic checks (`--host scripted`, the
default), or the configured LLM host (`--host llm`, reads .env) for an evaluation of the real conversation. Each persona
answers whatever the host asks from a small rule book, like the simulator's demo, so the same personas work for both.

Run:  python tests/scenarios.py [--host scripted|llm] [--only fall,refuses]
Writes scenario-results.json next to the repo's certification verdict."""
import argparse, json, os, re, sys, tempfile, time, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_env():
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1); os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


PERSONAS = {
    # name: (description, answer rules [(regex on the host's question, reply)], expectations)
    "cooperative": ("Answers every question plainly; a good day.",
        [(r"feel|how are you", "Pretty good, thank you."), (r"sleep|night", "Slept well."), (r"medic|pill|lisinopril|metformin", "Yes, both with breakfast."),
         (r"eat|breakfast|food", "Porridge and a banana."), (r"bother|worr|on your mind|concern", "Nothing at all."), (r"patel|appointment|2 pm|tom", "Yes, Tom's picking me up."),
         (r"plumber|tap", "He came Tuesday, all fixed."), (r"plan", "The garden, then a nap.")],
        {"completed": True, "risk_level": ["ok", "watch"], "fields": ["mood", "sleep", "meds_taken", "ate", "concern", "plans"], "alerts": 0, "max_turns": 12}),
    "fall_not_hurt": ("Mentions yesterday's fall in passing; not hurt.",
        [(r"hurt|injur|pain|dizz", "No, not hurt. I got right up."), (r"move|stiff|normal|walk", "Getting about fine."),
         (r"feel|how are you", "Pretty good."), (r"sleep|night", "Not great, my knee kept me up."), (r"medic|pill", "Yes, with my toast."), (r"eat|food|breakfast", "Toast and an egg."),
         (r"bother|worr|concern|mind", "Nothing really, though I fell getting to the bathroom yesterday."), (r"patel|appointment|tom", "Yes, Tom's picking me up."), (r"plumber|tap", "All fixed Tuesday."), (r"plan", "The garden.")],
        {"completed": True, "flags_any": ["fall"], "risk_level": ["watch", "concern"], "max_turns": 14}),
    "chest_pain": ("Chest tightness and short of breath: an emergency.",
        [(r"feel|how are you", "Not good. I've got a tightness in my chest and I can't catch my breath."), (r"help|911|emergency|family|alert", "Yes, please get someone."), (r".*", "I don't know, it hurts.")],
        {"request_help": True, "alerts": 1, "alert_level": "urgent", "max_turns": 6}),
    "refuses": ("Doesn't want to talk this morning.",
        [(r"feel|how are you", "Not now, dear. Can we do this later?"), (r".*", "Later, please.")],
        {"snoozed": True, "completed": False, "max_turns": 4}),
    "skipped_meds": ("Forgot the pills and admits it; takes them when asked.",
        [(r"medic|pill|lisinopril|metformin", "Oh, I forgot them this morning."), (r"take (it|them) now|could you take|pill box|when you have", "Alright, I've taken them now."),
         (r"feel|how are you", "Fine, thank you."), (r"sleep|night", "Okay."), (r"eat|food|breakfast", "Yes, cereal."), (r"bother|worr|concern|mind", "No."), (r"patel|appointment|tom", "Yes."), (r"plumber|tap", "Yes, fixed."), (r"plan", "Telly.")],
        {"completed": True, "flags_any": ["skipped_meds"], "max_turns": 14}),
    "low_mood_lonely": ("Flat and lonely; wants a call.",
        [(r"feel|how are you", "Oh, not so good. A bit down, to be honest."), (r"hard|down|what's|making|why|tell me", "Just lonely, I suppose. Nobody's rung all week."),
         (r"call|let anna know|would you like", "Yes, that would be nice."), (r"sleep|night", "Alright."), (r"medic|pill", "Yes."), (r"eat|food|breakfast", "Not much, a biscuit."),
         (r"bother|worr|concern|mind", "No, nothing else."), (r"patel|appointment|tom", "Yes."), (r"plumber|tap", "Yes."), (r"plan", "Nothing much.")],
        {"completed": True, "flags_any": ["lonely", "no_food"], "risk_level": ["watch", "concern"], "max_turns": 14}),
    "unclear_answers": ("Vague, needs the choice offered.",
        [(r"good, okay, or|would you say|choice|which", "Okay, I suppose."), (r"feel|how are you", "Oh, you know. Same as ever."), (r"sleep|night", "Bits and pieces."),
         (r"medic|pill", "I think so."), (r"check|sure|pill box", "Yes, they're gone from the box."), (r"eat|food|breakfast", "A little."), (r"bother|worr|concern|mind", "No."),
         (r"patel|appointment|tom", "Yes."), (r"plumber|tap", "Yes."), (r"plan", "Not much.")],
        {"completed": True, "fields": ["mood", "sleep", "meds_taken"], "max_turns": 16}),
    "chatty": ("Long tangents about the garden and the Reds; still answers.",
        [(r"feel|how are you", "Oh, I'm grand. The roses have come out beautifully this week, you should see them, and the Reds won last night, did you know, first time in ages, my late husband would have been thrilled. Pretty good, all in all."),
         (r"sleep|night", "Well, I was up at three for the bathroom, then the birds started, but I got back off, so not bad really, maybe six hours."), (r"medic|pill", "Yes yes, both, I do them with my tea every morning, have done for years."),
         (r"eat|food|breakfast", "Toast, marmalade, the good kind from the market, and half a grapefruit."), (r"bother|worr|concern|mind", "Only the fence, the neighbour's dog keeps getting through, but that's nothing."),
         (r"patel|appointment|tom", "Yes, Tom said half one, he's very good, his mother was a friend of mine."), (r"plumber|tap", "He did, Tuesday, lovely young man, fixed it in ten minutes."), (r"plan", "The garden, and my programme at two, well, after Dr. Patel.")],
        {"completed": True, "risk_level": ["ok", "watch"], "max_turns": 14}),
    "voice_message_back": ("Wants to send Anna a message mid-conversation.",
        [(r"feel|how are you", "Good. Could you tell Anna I love her and I hope Portugal's sunny?"), (r"sleep|night", "Fine."), (r"medic|pill", "Yes."), (r"eat|food|breakfast", "Yes."),
         (r"bother|worr|concern|mind", "No."), (r"patel|appointment|tom", "Yes."), (r"plumber|tap", "Yes."), (r"plan", "Knitting.")],
        {"completed": True, "record_reply": True, "max_turns": 14}),
    "new_appointment": ("Mentions a future appointment to remember.",
        [(r"feel|how are you", "Fine, thanks. Oh, and I've got the dentist next Friday at ten, can you remember that?"), (r"sleep|night", "Fine."), (r"medic|pill", "Yes."), (r"eat|food|breakfast", "Yes."),
         (r"bother|worr|concern|mind", "No."), (r"patel|appointment|tom", "Yes."), (r"plumber|tap", "Yes."), (r"plan", "Shopping.")],
        {"completed": True, "add_event": True, "max_turns": 14}),
    "cant_get_up": ("Has fallen and cannot get up: the clearest emergency.",
        [(r".*", "I've fallen in the kitchen and I can't get up. Help me.")],
        {"request_help": True, "alerts": 1, "alert_level": "urgent", "max_turns": 4}),
    "no_food_dizzy": ("Hasn't eaten and feels dizzy.",
        [(r"feel|how are you", "A bit dizzy this morning, to be honest."), (r"when|start|water|eaten|drink", "Since I got up. I haven't had anything yet, no."), (r"sleep|night", "Fine."),
         (r"medic|pill", "Yes."), (r"eat|food|breakfast|something easy|toast", "Not yet. I'll have some toast now."), (r"bother|worr|concern|mind", "No."), (r"patel|appointment|tom", "Yes."), (r"plumber|tap", "Yes."), (r"plan", "Resting.")],
        {"completed": True, "flags_any": ["dizzy", "no_food"], "risk_level": ["watch", "concern"], "max_turns": 14}),
}


def answer(rules, question, used):
    for pat, line in rules:
        if re.search(pat, question, re.I) and line not in used:
            used.add(line); return line
    for pat, line in rules:                      # allow a repeat rather than silence
        if re.search(pat, question, re.I): return line
    return "Yes, that's right."


def run_one(name, host):
    from hearth import db, seed, agent
    desc, rules, want = PERSONAS[name]
    seed.run()
    t0 = time.time()
    s = agent.start(1, "llm" if host == "llm" else "scripted")
    sid = s["session_id"]; question = s["agent"]; turns = 0; used = set(); transcript = [("H", question)]
    while not s.get("done") and turns < want.get("max_turns", 14):
        line = answer(rules, question, used); turns += 1; transcript.append(("M", line))
        s = agent.turn(sid, line); question = s.get("agent") or ""; transcript.append(("H", question))
        if question.startswith("(LLM error"): break
    log = agent.SESSIONS[sid]["tool_log"]; tools = [t["tool"] for t in log]
    c = db.checkin_for(1, __import__("hearth.core", fromlist=["core"]).today_str(db.person(1)))
    alerts = db.open_alerts(1); status = db.q("SELECT level FROM alerts WHERE person_id=1 ORDER BY id DESC LIMIT 1")
    recorded = {t["args"]["field"] for t in log if t["tool"] == "record_answer" and isinstance(t["result"], dict) and t["result"].get("ok")}
    facts = {"turns": turns, "seconds": round(time.time() - t0, 1), "completed": "complete_checkin" in tools, "request_help": "request_help" in tools, "snoozed": "snooze_checkin" in tools,
             "record_reply": "record_reply" in tools, "add_event": "add_event" in tools, "flags": (c or {}).get("flags") or [], "risk_level": __import__("hearth.core", fromlist=["core"]).risk_level((c or {}).get("risk") or 0) if c else None,
             "fields": sorted(recorded), "alerts": len(alerts), "alert_level": status[0]["level"] if status else None, "error": question if question.startswith("(LLM error") else None}
    checks = []
    for k, v in want.items():
        if k == "max_turns": checks.append(("finished within %d turns" % v, turns <= v and (facts["completed"] or facts["request_help"] or facts["snoozed"])))
        elif k == "flags_any": checks.append(("flags include one of %s" % v, any(f in facts["flags"] for f in v)))
        elif k == "fields": checks.append(("recorded %s" % v, all(f in facts["fields"] for f in v)))
        elif k == "risk_level": checks.append(("risk level in %s" % v, facts["risk_level"] in v))
        else: checks.append(("%s == %s" % (k, v), facts.get(k) == v))
    return {"persona": name, "description": desc, "host": host, "facts": facts, "checks": [{"check": n, "ok": ok} for n, ok in checks], "ok": all(ok for _, ok in checks), "transcript": transcript}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default="scripted", choices=["scripted", "llm"]); ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.path.join(ROOT, "scenario-results.json"))
    a = ap.parse_args()
    load_env()
    os.environ["HEARTH_DB"] = os.path.join(tempfile.gettempdir(), "hearth_scenarios.db"); os.environ["HEARTH_MEDIA"] = os.path.join(tempfile.gettempdir(), "hearth_scenarios_media")
    os.environ["HEARTH_WATCHDOG_SECONDS"] = "3600"; os.environ.pop("HEARTH_SMTP_HOST", None)       # never email during evaluation
    names = [n for n in PERSONAS if not a.only or n in a.only.split(",")]
    results = []
    for n in names:
        r = run_one(n, a.host); results.append(r)
        f = r["facts"]
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {n:<20} turns={f['turns']:<3} {f['seconds']:>6}s  done={int(f['completed'])} help={int(f['request_help'])} snooze={int(f['snoozed'])} "
              f"risk={f['risk_level']} flags={f['flags']} fields={len(f['fields'])} alerts={f['alerts']}" + (f"  ERROR {f['error'][:80]}" if f['error'] else ""))
        for c in r["checks"]:
            if not c["ok"]: print(f"        x {c['check']}")
    passed = sum(r["ok"] for r in results)
    out = {"generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "host": a.host, "model": os.environ.get("HEARTH_LLM_MODEL") if a.host == "llm" else None,
           "summary": {"pass": passed, "fail": len(results) - passed}, "results": results}
    json.dump(out, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"\n{passed}/{len(results)} scenarios pass ({a.host}) -> {a.out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
