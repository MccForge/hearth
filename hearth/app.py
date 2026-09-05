"""Hearth web server: MCP endpoint (/mcp, Streamable HTTP), caregiver dashboard (/), simulator (/sim), JSON API (/api).
Run:  python -m hearth   (or: uvicorn hearth.app:app --port 8787)"""
from __future__ import annotations
import asyncio, contextlib, datetime as dt, os
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from . import db, core, escalation, agent
from .mcp_server import server, TOOLS

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
mcp_app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True)


def _page(name: str):
    async def handler(request: Request):
        return FileResponse(os.path.join(WEB, name))
    return handler


async def api_persons(request: Request):
    return JSONResponse([{**core.status(p), "contacts": db.contacts(p["id"]), "medications": db.medications(p["id"])} for p in db.persons()])


async def api_person(request: Request):
    pid = int(request.path_params["pid"])
    p = db.person(pid)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"person": p, "status": core.status(p), "contacts": db.contacts(pid), "medications": db.medications(pid),
                         "checkins": db.recent_checkins(pid, 14), "alerts": db.alerts(pid, 30), "notifications": db.notifications(pid, 30)})


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


async def api_sim_start(request: Request):
    body = await request.json()
    return JSONResponse(agent.start(int(body.get("person_id", 1)), body.get("mode", "scripted")))


async def api_sim_turn(request: Request):
    body = await request.json()
    return JSONResponse(agent.turn(body["session_id"], body.get("text", "")))


async def api_tool(request: Request):
    """Direct tool invocation for the caregiver voice demo ('how is Mom today?') and for testing."""
    body = await request.json(); name = body.get("tool")
    if name not in TOOLS: return JSONResponse({"error": "unknown tool"}, status_code=400)
    return JSONResponse(TOOLS[name](**body.get("args", {})))


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
    Route("/api/alerts/{aid:int}/ack", api_ack, methods=["POST"]),
    Route("/api/sim/start", api_sim_start, methods=["POST"]), Route("/api/sim/turn", api_sim_turn, methods=["POST"]),
    Route("/api/tool", api_tool, methods=["POST"]), Route("/api/watchdog/run", api_watchdog, methods=["POST"]),
    Route("/api/demo/missed", api_demo_missed, methods=["POST"]), Route("/api/demo/reset", api_demo_reset, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=WEB), name="static"),
    Mount("/", app=mcp_app),
], lifespan=lifespan)


def main():
    import uvicorn
    uvicorn.run("hearth.app:app", host=os.environ.get("HEARTH_HOST", "127.0.0.1"), port=int(os.environ.get("HEARTH_PORT", "8787")), log_level="info")
