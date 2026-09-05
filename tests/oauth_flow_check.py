"""End-to-end OAuth check against the real app, the way the Alexa+ MCP Toolkit exercises it.
Runs in its own process because auth is decided at import time from HEARTH_PUBLIC_URL. Prints OAUTH_FLOW_OK on success."""
import base64, hashlib, json, os, secrets, sys, tempfile
from urllib.parse import parse_qs, urlparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["HEARTH_DB"] = os.path.join(tempfile.gettempdir(), "hearth_oauth_test.db")
os.environ["HEARTH_MEDIA"] = os.path.join(tempfile.gettempdir(), "hearth_oauth_media")
os.environ["HEARTH_PUBLIC_URL"] = "http://localhost"
os.environ["HEARTH_OAUTH_CLIENT_ID"] = "alexa-plus"
os.environ["HEARTH_OAUTH_CLIENT_SECRET"] = "s3cret-from-console"
os.environ["HEARTH_OAUTH_REDIRECT_URIS"] = "https://alexa.example/callback"
os.environ["HEARTH_WATCHDOG_SECONDS"] = "3600"

from starlette.testclient import TestClient  # noqa: E402
from hearth import seed                       # noqa: E402
seed.run()
from hearth.app import app                     # noqa: E402

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
RESOURCE = "http://localhost/mcp"


def rpc(c, token, method, params=None, id_=1):
    h = dict(MCP_HEADERS)
    if token: h["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=h)


with TestClient(app, base_url="http://localhost") as c:
    # 1. no token -> 401
    r = rpc(c, None, "tools/list")
    assert r.status_code == 401, r.text

    # 2. authorization server metadata advertises both tiers and PKCE S256; protected resource metadata exists
    m = c.get("/.well-known/oauth-authorization-server").json()
    assert "client_credentials" in m["grant_types_supported"] and "authorization_code" in m["grant_types_supported"], m
    assert "S256" in m["code_challenge_methods_supported"] and m["token_endpoint"].endswith("/token"), m
    assert "registration_endpoint" not in m or not m["registration_endpoint"], "DCR should be off with a fixed client"
    prm = c.get("/.well-known/oauth-protected-resource/mcp")
    if prm.status_code != 200:
        prm = c.get("/.well-known/oauth-protected-resource")
    assert prm.status_code == 200 and "authorization_servers" in prm.json(), prm.text

    # 3. service level: client_credentials with HTTP Basic, scope mcp:service, resource
    basic = base64.b64encode(b"alexa-plus:s3cret-from-console").decode()
    t = c.post("/token", data={"grant_type": "client_credentials", "scope": "mcp:service", "resource": RESOURCE}, headers={"Authorization": f"Basic {basic}"})
    assert t.status_code == 200, t.text
    svc = t.json(); assert svc["token_type"].lower() == "bearer" and svc["expires_in"] <= 3600 and "refresh_token" not in svc, svc
    bad = c.post("/token", data={"grant_type": "client_credentials", "scope": "mcp:service", "resource": RESOURCE}, headers={"Authorization": "Basic " + base64.b64encode(b"alexa-plus:wrong").decode()})
    assert bad.status_code == 401, bad.text
    wrong_res = c.post("/token", data={"grant_type": "client_credentials", "scope": "mcp:service", "resource": "https://evil.example/mcp"}, headers={"Authorization": f"Basic {basic}"})
    assert wrong_res.status_code == 400 and wrong_res.json()["error"] == "invalid_target", wrong_res.text

    # service token can discover tools but cannot act for a person
    r = rpc(c, svc["access_token"], "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "alexa-plus-check", "version": "0"}})
    assert r.status_code == 200, r.text
    r = rpc(c, svc["access_token"], "tools/list", id_=2)
    assert r.status_code == 200 and "get_checkin_context" in r.text, r.text[:300]
    r = rpc(c, svc["access_token"], "tools/call", {"name": "get_status", "arguments": {"person_id": 0}}, id_=3)
    assert r.status_code == 200 and "isn't linked" in r.text, r.text[:300]

    # 4. user level: authorization_code + PKCE through the consent page
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    a = c.get("/authorize", params={"response_type": "code", "client_id": "alexa-plus", "redirect_uri": "https://alexa.example/callback", "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": "xyz123", "scope": "mcp:tools mcp:resources", "resource": RESOURCE}, follow_redirects=False)
    assert a.status_code in (302, 303, 307), a.text
    loc = a.headers["location"]; assert "/link?txn=" in loc, loc
    txn = loc.split("txn=")[1]
    page = c.get("/link", params={"txn": txn})
    assert page.status_code == 200 and "Margaret Hale" in page.text
    p = c.post("/link", data={"txn": txn, "person_id": "1"}, follow_redirects=False)
    assert p.status_code == 302, p.text
    cb = p.headers["location"]; assert cb.startswith("https://alexa.example/callback") and "state=xyz123" in cb, cb
    code = parse_qs(urlparse(cb).query)["code"][0]

    # wrong verifier is rejected, right one is accepted
    t_bad = c.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": "nope" * 12, "client_id": "alexa-plus", "client_secret": "s3cret-from-console",
                                   "redirect_uri": "https://alexa.example/callback", "resource": RESOURCE})
    assert t_bad.status_code == 400, t_bad.text
    t2 = c.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": "alexa-plus", "client_secret": "s3cret-from-console",
                                "redirect_uri": "https://alexa.example/callback", "resource": RESOURCE})
    assert t2.status_code == 200, t2.text
    tok = t2.json(); assert tok.get("refresh_token") and "mcp:tools" in tok["scope"], tok

    # 5. the user token is bound to Margaret: person_id 0 resolves to her
    r = rpc(c, tok["access_token"], "tools/call", {"name": "get_status", "arguments": {"person_id": 0}}, id_=4)
    assert r.status_code == 200 and "Margaret Hale" in r.text, r.text[:300]
    r = rpc(c, tok["access_token"], "tools/call", {"name": "get_checkin_context", "arguments": {}}, id_=5)
    assert r.status_code == 200 and "Good" in r.text and "Anna" in r.text, r.text[:300]

    # 6. refresh rotates the token
    t3 = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": "alexa-plus", "client_secret": "s3cret-from-console"})
    assert t3.status_code == 200 and t3.json()["access_token"] != tok["access_token"], t3.text
    t4 = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": "alexa-plus", "client_secret": "s3cret-from-console"})
    assert t4.status_code == 400, "old refresh token must not work twice"

print("OAUTH_FLOW_OK")
