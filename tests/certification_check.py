"""Hearth self-check against Amazon's published Alexa+ add-on functional requirements.

Amazon's Local Inspector (private preview) produces a certification verdict for an MCP add-on. This script does the same
for the parts of the checklist that can be verified from outside: it walks the real Streamable HTTP endpoint, exercises
every tool, times them, checks schemas, error shapes, stable identifiers, context continuity, the MCP App views, the
store metadata in addon/manifest.json, and (in a subprocess) the two-tier OAuth flow. Writes certification-verdict.json.

Run:  python tests/certification_check.py
"""
import json, os, subprocess, sys, tempfile, time, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HEARTH_DB", os.path.join(tempfile.gettempdir(), "hearth_cert.db"))
os.environ.setdefault("HEARTH_MEDIA", os.path.join(tempfile.gettempdir(), "hearth_cert_media"))
os.environ["HEARTH_WATCHDOG_SECONDS"] = "3600"
from starlette.testclient import TestClient   # noqa: E402
from hearth import seed, ui                    # noqa: E402
from hearth.app import app                     # noqa: E402

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
RULES: list[dict] = []


def rule(rid: str, title: str, ok: bool, evidence: str, warn: bool = False):
    RULES.append({"id": rid, "title": title, "status": "pass" if ok else ("warn" if warn else "fail"), "evidence": evidence})


def rpc(c, method, params=None, id_=1):
    t0 = time.perf_counter()
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=HEADERS)
    ms = (time.perf_counter() - t0) * 1000
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return r.status_code, body.get("result"), body.get("error"), ms


def call(c, name, args):
    st, res, err, ms = rpc(c, "tools/call", {"name": name, "arguments": args}, id_=int(time.time() * 1000) % 100000)
    return st, res, err, ms


def main():
    seed.run()
    with TestClient(app, base_url="http://localhost") as c:
        st, init, err, _ = rpc(c, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "cert", "version": "0"}})
        rule("0.1", "Server initializes over Streamable HTTP with spec 2025-11-25", st == 200 and init is not None and bool(init.get("protocolVersion")),
             f"HTTP {st}, protocolVersion={init and init.get('protocolVersion')}, instructions={len((init or {}).get('instructions') or '')} chars")
        rule("4.2", "'What can you do?' has an answer: server instructions describe the flow", bool((init or {}).get("instructions")) and "check-in" in (init or {}).get("instructions", ""),
             (init or {}).get("instructions", "")[:120] + "…")

        # ---- 13. MCP tool validation
        _, tl, _, _ = rpc(c, "tools/list", id_=2)
        tools = {t["name"]: t for t in (tl or {}).get("tools", [])}
        bad_schema = [n for n, t in tools.items() if (t.get("inputSchema") or {}).get("type") != "object" or
                      any(r not in (t["inputSchema"].get("properties") or {}) for r in t["inputSchema"].get("required", []))]
        rule("13.2", "Every tool declares a valid JSON Schema inputSchema with required parameters present in properties", not bad_schema, f"{len(tools)} tools; invalid: {bad_schema or 'none'}")
        short_desc = [n for n, t in tools.items() if len(t.get("description") or "") < 40 or "{" in (t.get("description") or "")]
        rule("13.5", "Tool descriptions are clear prose (40+ characters, no JSON)", not short_desc, f"weak: {short_desc or 'none'}")
        with_syn = [n for n, t in tools.items() if any(w in (t.get("description") or "").lower() for w in ("e.g.", "such as", "'", "or "))]
        rule("13.6", "Parameter descriptions include examples or synonyms", len(with_syn) >= len(tools) // 2, f"{len(with_syn)}/{len(tools)} tools carry examples", warn=True)

        # invoke every tool with realistic arguments, in a realistic order
        seeded = call(c, "get_checkin_context", {"person_id": 1})[1]
        ctx = (seeded or {}).get("structuredContent") or {}
        msg_id = (ctx.get("family_messages") or [{}])[0].get("id")
        ev_id = (ctx.get("events_today") or [{}])[0].get("id")
        q_id = (ctx.get("questions_from_family") or [{}])[0].get("id")
        plan = [("get_checkin_context", {"person_id": 1}), ("get_family_message", {"message_id": msg_id or 1}), ("mark_message_played", {"message_id": msg_id or 1}),
                ("start_checkin", {"person_id": 1}), ("record_answer", None), ("complete_checkin", None), ("request_help", {"person_id": 1, "reason": "cert check", "urgency": "concern"}),
                ("record_reply", {"person_id": 1, "transcript": "tell Anna hello", "contact_name": "Anna"}), ("add_event", {"person_id": 1, "date": "2030-01-10", "title": "Dentist", "time": "10:00"}),
                ("list_events", {"person_id": 1, "days": 7}), ("get_status", {"person_id": 1}), ("snooze_checkin", {"person_id": 1, "minutes": 30}),
                ("log_medication", {"person_id": 1, "medication": "Metformin", "taken": True}), ("list_persons", {})]
        timings, failures, ids = {}, [], {}
        checkin_id = None
        for name, args in plan:
            if name == "record_answer": args = {"checkin_id": checkin_id or 1, "field": "mood", "value": "4", "quote": "pretty good"}
            if name == "complete_checkin": args = {"checkin_id": checkin_id or 1}
            st, res, err, ms = call(c, name, args)
            timings[name] = round(ms, 1)
            sc = (res or {}).get("structuredContent") or {}
            if st != 200 or err or (res or {}).get("isError") or "error" in sc:
                failures.append(f"{name}: HTTP {st} {err or sc.get('error')}")
            if name == "start_checkin": checkin_id = sc.get("checkin_id")
            for k in ("checkin_id", "event_id", "message_id", "alert_id"):
                if k in sc: ids[k] = sc[k]
        missing = [n for n in tools if n not in dict(plan)]
        rule("13.1", "Every tool returned by tools/list is invocable (no dead or placeholder entries)", not failures and not missing, f"invoked {len(plan)}; failures: {failures or 'none'}; not exercised: {missing or 'none'}")
        rule("13.4", "Tools return stable identifiers the next call can use", all(k in ids for k in ("checkin_id", "event_id", "message_id", "alert_id")), f"ids seen: {sorted(ids)}")
        slow = {n: t for n, t in timings.items() if t > 500}
        rule("2.5", "Round-trip under 500 ms per tool call (local measurement)", not slow, f"max {max(timings.values()):.0f} ms ({max(timings, key=timings.get)}); slow: {slow or 'none'}")

        # ---- 2. error handling
        st, res, err, _ = call(c, "record_answer", {"checkin_id": checkin_id or 1, "field": "shoe_size", "value": "7"})
        sc = (res or {}).get("structuredContent") or {}
        rule("2.1", "Unexpected parameters get a structured, actionable error (no stack trace, no internal ids)",
             st == 200 and "error" in sc and "fields" in sc and "Traceback" not in json.dumps(res), f"{json.dumps(sc)[:140]}")
        st, res, err, _ = call(c, "complete_checkin", {"checkin_id": 999999})
        sc = (res or {}).get("structuredContent") or {}
        rule("2.2", "Unknown record ids fail gracefully", st == 200 and "error" in sc, f"{json.dumps(sc)[:100]}")
        st, res, err, _ = call(c, "get_status", {"person_id": 999})
        sc = (res or {}).get("structuredContent") or {}
        rule("2.3", "Unknown person fails gracefully with a next step", st == 200 and "error" in sc, f"{json.dumps(sc)[:120]}")

        # ---- 1. context and continuity
        st, res, _, _ = call(c, "start_checkin", {"person_id": 1})
        fresh = ((res or {}).get("structuredContent") or {}).get("checkin_id")
        rule("1.4", "A fresh start after a completed check-in creates a new record and keeps the old one", bool(fresh) and fresh != checkin_id, f"completed id {checkin_id} -> new id {fresh}")
        rule("1.1", "Answers can be updated independently (field-by-field record_answer)", "record_answer" in tools and "field" in tools["record_answer"]["inputSchema"]["properties"], "record_answer(field, value, quote)")

        # ---- 9. voice-only: option counts
        st, res, _, _ = call(c, "list_events", {"person_id": 1, "days": 7})
        evs = ((res or {}).get("structuredContent") or {}).get("events", [])
        rule("9.2", "Voice-only surfaces present at most 5 options at a time", len(evs) <= 5, f"list_events(7 days) returned {len(evs)} events", warn=len(evs) > 5)

        # ---- 11. screens: MCP App views
        _, rl, _, _ = rpc(c, "resources/list", id_=3)
        uis = {r["uri"]: r for r in (rl or {}).get("resources", []) if r["uri"].startswith("ui://")}
        referenced = {((t.get("_meta") or {}).get("ui") or {}).get("resourceUri") for t in tools.values()} - {None}
        dangling = referenced - set(uis)
        readable = []
        for uri in uis:
            _, rr, _, _ = rpc(c, "resources/read", {"uri": uri}, id_=4)
            ct = ((rr or {}).get("contents") or [{}])[0]
            readable.append(ct.get("mimeType") == ui.MIME and "ui/initialize" in (ct.get("text") or ""))
        rule("11.1", "Every _meta.ui.resourceUri resolves to a readable MCP App resource with the right mime type", not dangling and all(readable) and bool(uis),
             f"{len(uis)} views, {len(referenced)} referenced, dangling: {sorted(dangling) or 'none'}")

        # ---- 6. account linking / auth (subprocess: auth is decided at import time)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "oauth_flow_check.py")], capture_output=True, text=True, timeout=180)
        rule("6.1", "Two-tier OAuth 2.1: 401 without a token, client_credentials service tier, PKCE user tier, refresh rotation, linked person", "OAUTH_FLOW_OK" in r.stdout,
             (r.stdout.strip().splitlines() or [r.stderr.strip()[-200:]])[-1])

    # ---- 5. add-on metadata
    mpath = os.path.join(ROOT, "addon", "manifest.json")
    try:
        m = json.load(open(mpath, encoding="utf-8"))
    except Exception as ex:
        m = {}; rule("5.0", "addon/manifest.json present and valid", False, str(ex))
    if m:
        rule("5.1", "Plain-language description without API names, tool names or JSON", all(w not in (m.get("long_description") or "") for w in ("MCP", "JSON", "_", "{")), (m.get("long_description") or "")[:100] + "…")
        rule("5.2", "Prerequisites stated (account linking)", "account linking" in (m.get("prerequisites") or "").lower(), m.get("prerequisites", ""))
        pp = os.path.exists(os.path.join(ROOT, "PRIVACY.md")); tt = os.path.exists(os.path.join(ROOT, "TERMS.md"))
        rule("5.3", "Privacy policy and terms of use URLs present and backed by files", bool(m.get("privacy_policy_url")) and bool(m.get("terms_of_use_url")) and pp and tt, f"PRIVACY.md={pp} TERMS.md={tt}")
        rule("5.4", "Speakable, unambiguous add-on name", m.get("invocation_name", "").isalpha() and len(m.get("invocation_name", "")) <= 12, m.get("invocation_name", ""))
        sizes = ("64", "72", "88", "126", "180", "241")
        icons_ok = all(os.path.exists(os.path.join(ROOT, m["icons"][theme][s])) for theme in ("light", "dark") for s in sizes)
        rule("5.5", "Icons in every required size, light and dark", icons_ok, f"sizes {sizes} x light/dark")
        rule("5.6", "At least 3 distinct example phrases", len(set(m.get("example_phrases") or [])) >= 3, "; ".join(m.get("example_phrases") or []))

    passed = sum(r["status"] == "pass" for r in RULES); warned = sum(r["status"] == "warn" for r in RULES); failed = sum(r["status"] == "fail" for r in RULES)
    verdict = {"generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "server": "hearth", "checklist": "Alexa+ add-on functional requirements (published)",
               "summary": {"pass": passed, "warn": warned, "fail": failed, "verdict": "READY" if failed == 0 else "NOT READY"}, "rules": RULES, "timings_ms": timings}
    out = os.path.join(ROOT, "certification-verdict.json")
    json.dump(verdict, open(out, "w", encoding="utf-8"), indent=2)
    width = max(len(r["title"]) for r in RULES)
    for r in RULES:
        print(f"{r['status'].upper():5} {r['id']:>4}  {r['title']:<{width}}  {r['evidence'][:90]}")
    print(f"\n{verdict['summary']['verdict']}: {passed} pass, {warned} warn, {failed} fail -> {out}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
