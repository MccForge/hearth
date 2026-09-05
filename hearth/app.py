"""Hearth web server: MCP endpoint (/mcp, Streamable HTTP), caregiver dashboard (/), simulator (/sim), JSON API (/api).
Run:  python -m hearth   (or: uvicorn hearth.app:app --port 8787)"""
from __future__ import annotations
import asyncio, base64, contextlib, datetime as dt, os
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from . import db, core, escalation, agent, auth, ui
from .mcp_server import server, TOOLS, AUTH_PROVIDER

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


def _transport_security():
    """DNS-rebinding protection: accept local hosts plus the public host (tunnel or domain) from HEARTH_PUBLIC_URL."""
    from urllib.parse import urlparse
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    origins = ["http://127.0.0.1", "http://127.0.0.1:*", "http://localhost", "http://localhost:*"]
    pu = auth.public_url()
    if pu:
        netloc = urlparse(pu).netloc
        hosts += [netloc, netloc.split(":")[0]]
        origins += [pu]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


mcp_app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=_transport_security())


def _page(name: str):
    async def handler(request: Request):
        return FileResponse(os.path.join(WEB, name))
    return handler


def _person_payload(pid: int) -> dict | None:
    p = db.person(pid)
    if not p: return None
    date = core.today_str(p)
    d = dt.date.fromisoformat(date)
    return {"person": p, "status": core.status(p), "contacts": db.contacts(pid), "active_contacts": core.active_contacts(pid, date),
            "medications": db.medications(pid), "checkins": db.recent_checkins(pid, 14), "alerts": db.alerts(pid, 30), "notifications": db.notifications(pid, 30),
            "messages": db.messages(pid, None, 40), "questions": db.questions(pid, 30), "away": db.away_all(pid), "trends": core.trends(pid, 7),
            "events": db.events_between(pid, (d - dt.timedelta(days=1)).isoformat(), (d + dt.timedelta(days=30)).isoformat())}


async def api_persons(request: Request):
    return JSONResponse([{**core.status(p), "contacts": db.contacts(p["id"]), "medications": db.medications(p["id"])} for p in db.persons()])


async def api_person(request: Request):
    payload = _person_payload(int(request.path_params["pid"]))
    return JSONResponse(payload) if payload else JSONResponse({"error": "not found"}, status_code=404)


async def api_ack(request: Request):
    aid = int(request.path_params["aid"]); body = await request.json()
    db.execute("UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=? WHERE id=?", (body.get("by", "caregiver"), db.now_iso(), aid))
    return JSONResponse({"ok": True})


async def api_settings(request: Request):
    pid = int(request.path_params["pid"]); body = await request.json()
    fields = {k: body[k] for k in ("window_start", "window_end", "timezone", "nickname", "notes") if k in body}
    if fields:
        db.execute("UPDATE persons SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), pid))
    if "contacts" in body:
        db.execute("DELETE FROM contacts WHERE person_id=?", (pid,))
        for c in body["contacts"]:
            db.execute("INSERT INTO contacts(person_id, name, relation, channel, address, priority) VALUES (?,?,?,?,?,?)",
                       (pid, c.get("name", ""), c.get("relation", ""), c.get("channel", "dashboard"), c.get("address", ""), int(c.get("priority", 1))))
    return JSONResponse({"ok": True})


# ---- family messages -------------------------------------------------------------
async def api_message_create(request: Request):
    pid = int(request.path_params["pid"]); body = await request.json()
    kind = "voice" if body.get("audio_base64") else "text"
    repeat = 1 if body.get("repeat_daily") else 0
    mid = db.execute("INSERT INTO messages(person_id, direction, from_name, contact_id, kind, transcript, mime, created_at, play_from, play_until, repeat_daily) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, "to_person", body.get("from_name") or "Family", body.get("contact_id"), kind, body.get("transcript") or "", body.get("mime") or "audio/webm",
                      db.now_iso(), body.get("play_from") or None, body.get("play_until") or None, repeat))
    if body.get("audio_base64"):
        path = db.save_media(mid, base64.b64decode(body["audio_base64"]), body.get("mime") or "audio/webm")
        db.execute("UPDATE messages SET audio_path=? WHERE id=?", (path, mid))
    return JSONResponse({"ok": True, "message_id": mid})


async def api_message_delete(request: Request):
    mid = int(request.path_params["mid"])
    db.execute("UPDATE messages SET status='archived' WHERE id=?", (mid,))
    return JSONResponse({"ok": True})


async def api_media(request: Request):
    m = db.message(int(request.path_params["mid"]))
    if not m or not m.get("audio_path") or not os.path.exists(m["audio_path"]):
        return JSONResponse({"error": "no audio"}, status_code=404)
    return FileResponse(m["audio_path"], media_type=(m.get("mime") or "audio/webm").split(";")[0])


async def api_reply(request: Request):
    """Margaret's voice note to the family, from the simulator."""
    pid = int(request.path_params["pid"]); body = await request.json()
    return JSONResponse(TOOLS["record_reply"](pid, body.get("transcript") or "(voice note)", body.get("contact_name") or "", body.get("audio_base64") or "", body.get("mime") or "audio/webm"))


# ---- questions, away, events -------------------------------------------------------
async def api_question_create(request: Request):
    pid = int(request.path_params["pid"]); body = await request.json()
    qid = db.execute("INSERT INTO questions(person_id, from_name, text, created_at, ask_on) VALUES (?,?,?,?,?)",
                     (pid, body.get("from_name") or "Family", body["text"].strip(), db.now_iso(), body.get("ask_on") or None))
    return JSONResponse({"ok": True, "question_id": qid})


async def api_question_delete(request: Request):
    db.execute("UPDATE questions SET status='cancelled' WHERE id=?", (int(request.path_params["qid"]),))
    return JSONResponse({"ok": True})


async def api_away_create(request: Request):
    pid = int(request.path_params["pid"]); body = await request.json()
    aid = db.execute("INSERT INTO away(person_id, contact_id, start_date, end_date, cover_contact_id, note) VALUES (?,?,?,?,?,?)",
                     (pid, int(body["contact_id"]), body["start_date"], body["end_date"], int(body["cover_contact_id"]) if body.get("cover_contact_id") else None, body.get("note") or ""))
    return JSONResponse({"ok": True, "away_id": aid})


async def api_away_delete(request: Request):
    db.execute("DELETE FROM away WHERE id=?", (int(request.path_params["aid"]),))
    return JSONResponse({"ok": True})


async def api_event_create(request: Request):
    pid = int(request.path_params["pid"]); body = await request.json()
    return JSONResponse(TOOLS["add_event"](pid, body["date"], body["title"], body.get("time") or "", body.get("kind") or "appointment", body.get("notes") or "",
                                           body.get("added_by") or "Family", bool(body.get("remind_day_before", True))))


async def api_event_delete(request: Request):
    db.execute("UPDATE events SET status='cancelled' WHERE id=?", (int(request.path_params["eid"]),))
    return JSONResponse({"ok": True})


# ---- simulator and demo ---------------------------------------------------------------
async def api_sim_start(request: Request):
    body = await request.json()
    return JSONResponse(agent.start(int(body.get("person_id", 1)), body.get("mode", "scripted"), str(body.get("model") or "")))


async def api_sim_models(request: Request):
    """LLM hosts available to the simulator (empty when no key is configured; the scripted host always works)."""
    return JSONResponse({"models": agent.llm_models()})


async def api_sim_turn(request: Request):
    body = await request.json()
    return JSONResponse(agent.turn(body["session_id"], body.get("text", "")))


async def api_tool(request: Request):
    """Direct tool invocation for the caregiver voice demo ('how is Mom today?') and for testing."""
    body = await request.json(); name = body.get("tool")
    if name not in TOOLS: return JSONResponse({"error": "unknown tool"}, status_code=400)
    return JSONResponse(agent._serialize(TOOLS[name](**body.get("args", {}))))


async def api_ui_manifest(request: Request):
    """Tool -> view mapping and view metadata, as a host learns them from tools/list and resources/list (MCP Apps)."""
    return JSONResponse(ui.manifest(server))


async def api_ui_resource(request: Request):
    """The HTML of one ui:// view, as resources/read returns it."""
    if request.query_params.get("reload"):               # development aid: pick up edits to ui.py without a restart
        import importlib; importlib.reload(ui)
    v = ui.VIEWS.get(request.query_params.get("uri", ""))
    return HTMLResponse(v["html"]) if v else JSONResponse({"error": "unknown view"}, status_code=404)


async def api_watchdog(request: Request):
    return JSONResponse({"created": escalation.run_once()})


async def api_demo_missed(request: Request):
    """Demo control: evaluate the ladder as if it were N minutes past the window end today. Clearly a simulation."""
    body = await request.json(); pid = int(body.get("person_id", 1)); minutes = int(body.get("minutes_past", 0))
    p = db.person(pid)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    _, end = core.window_bounds(p, core.today_str(p))
    fake_now = (end + dt.timedelta(minutes=minutes)).astimezone(dt.timezone.utc)
    return JSONResponse({"simulated_time": fake_now.isoformat(), "created": escalation.run_once(fake_now)})


async def api_demo_reset(request: Request):
    from . import seed
    seed.run()
    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(app):
    db.conn()
    if not db.persons():
        from . import seed
        seed.run()
    task = asyncio.create_task(escalation.watchdog(int(os.environ.get("HEARTH_WATCHDOG_SECONDS", "60"))))
    async with server.session_manager.run():
        yield
    task.cancel()


app = Starlette(routes=[
    Route("/", _page("index.html")), Route("/sim", _page("sim.html")),
    Route("/api/persons", api_persons), Route("/api/persons/{pid:int}", api_person),
    Route("/api/persons/{pid:int}/settings", api_settings, methods=["POST"]),
    Route("/api/persons/{pid:int}/messages", api_message_create, methods=["POST"]), Route("/api/messages/{mid:int}", api_message_delete, methods=["DELETE"]),
    Route("/api/media/{mid:int}", api_media), Route("/api/persons/{pid:int}/replies", api_reply, methods=["POST"]),
    Route("/api/persons/{pid:int}/questions", api_question_create, methods=["POST"]), Route("/api/questions/{qid:int}", api_question_delete, methods=["DELETE"]),
    Route("/api/persons/{pid:int}/away", api_away_create, methods=["POST"]), Route("/api/away/{aid:int}", api_away_delete, methods=["DELETE"]),
    Route("/api/persons/{pid:int}/events", api_event_create, methods=["POST"]), Route("/api/events/{eid:int}", api_event_delete, methods=["DELETE"]),
    Route("/api/alerts/{aid:int}/ack", api_ack, methods=["POST"]),
    Route("/api/sim/start", api_sim_start, methods=["POST"]), Route("/api/sim/turn", api_sim_turn, methods=["POST"]), Route("/api/sim/models", api_sim_models),
    Route("/api/tool", api_tool, methods=["POST"]), Route("/api/watchdog/run", api_watchdog, methods=["POST"]),
    Route("/api/ui/manifest", api_ui_manifest), Route("/api/ui/resource", api_ui_resource),
    Route("/api/demo/missed", api_demo_missed, methods=["POST"]), Route("/api/demo/reset", api_demo_reset, methods=["POST"]),
    *(auth.override_routes(mcp_app, AUTH_PROVIDER) if AUTH_PROVIDER else []),
    Mount("/static", app=StaticFiles(directory=WEB), name="static"),
    Mount("/assets", app=StaticFiles(directory=os.path.join(os.path.dirname(WEB), "assets"), check_dir=False), name="assets"),
    Mount("/", app=mcp_app),
], lifespan=lifespan)


def main():
    import uvicorn
    uvicorn.run("hearth.app:app", host=os.environ.get("HEARTH_HOST", "127.0.0.1"), port=int(os.environ.get("HEARTH_PORT", "8787")), log_level="info")
