"""MCP Apps surface: tools advertise ui:// views, views are readable resources with the MCP Apps mime type, and the
bridge contract the views rely on is present. Exercised over the real Streamable HTTP endpoint with plain JSON-RPC."""
import json, os, tempfile
os.environ.setdefault("HEARTH_DB", os.path.join(tempfile.gettempdir(), "hearth_test.db"))
os.environ.setdefault("HEARTH_MEDIA", os.path.join(tempfile.gettempdir(), "hearth_test_media"))
os.environ["HEARTH_WATCHDOG_SECONDS"] = "3600"
import pytest                                  # noqa: E402
from starlette.testclient import TestClient   # noqa: E402
from hearth import ui, seed                    # noqa: E402
from hearth.app import app                     # noqa: E402
from hearth.mcp_server import server           # noqa: E402


@pytest.fixture(scope="module")
def client():
    """One app lifespan for the module: the MCP session manager can only be started once per process."""
    seed.run()
    with TestClient(app, base_url="http://localhost") as c:
        yield c

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}


def rpc(c, method, params=None, id_=1):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_manifest_maps_tools_to_views():
    m = ui.manifest(server)
    assert m["protocolVersion"] == "2026-01-26"
    assert m["tools"]["get_checkin_context"] == "ui://hearth/checkin" and m["tools"]["record_answer"] == "ui://hearth/checkin"
    assert m["tools"]["list_events"] == "ui://hearth/calendar" and m["tools"]["get_status"] == "ui://hearth/status"
    assert "list_persons" not in m["tools"] and "mark_message_played" not in m["tools"]
    for uri, r in m["resources"].items():
        assert uri.startswith("ui://hearth/") and r["mimeType"] == "text/html;profile=mcp-app" and "ui" in r["_meta"]


def test_views_over_streamable_http(client):
    c = client
    if True:
        rpc(c, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        tools = rpc(c, "tools/list", id_=2)["tools"]
        by_name = {t["name"]: t for t in tools}
        assert by_name["get_checkin_context"]["_meta"]["ui"]["resourceUri"] == "ui://hearth/checkin"
        assert by_name["get_status"]["_meta"]["ui"]["resourceUri"] == "ui://hearth/status"
        assert "_meta" not in by_name["list_persons"] or not (by_name["list_persons"].get("_meta") or {}).get("ui")
        res = rpc(c, "resources/list", id_=3)["resources"]
        uis = {r["uri"]: r for r in res if r["uri"].startswith("ui://")}
        assert set(uis) == {"ui://hearth/checkin", "ui://hearth/calendar", "ui://hearth/status"}
        for r in uis.values():
            assert r["mimeType"] == "text/html;profile=mcp-app" and r["_meta"]["ui"]["prefersBorder"] is False
        read = rpc(c, "resources/read", {"uri": "ui://hearth/checkin"}, id_=4)["contents"][0]
        assert read["mimeType"] == "text/html;profile=mcp-app"
        html = read["text"]
        for needle in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-input", "ui/notifications/tool-result", "ui/resource-teardown", "Content-Security-Policy"):
            assert needle in html, needle
        # a tool call over HTTP carries structuredContent the view renders from
        call = rpc(c, "tools/call", {"name": "get_checkin_context", "arguments": {"person_id": 1}}, id_=5)
        assert call["structuredContent"]["person"]["name"] == "Margaret Hale" and not call.get("isError")


def test_simulator_endpoints_serve_views(client):
    c = client
    if True:
        m = c.get("/api/ui/manifest").json()
        assert m["tools"]["complete_checkin"] == "ui://hearth/checkin"
        page = c.get("/api/ui/resource", params={"uri": "ui://hearth/status"})
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and "get_status" in page.text
        assert c.get("/api/ui/resource", params={"uri": "ui://nope"}).status_code == 404
