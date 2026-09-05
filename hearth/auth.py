"""OAuth 2.1 authorization server and account linking for Hearth, shaped to the Alexa+ MCP Toolkit's requirements.

Two tiers, as Alexa+ expects:
  service level  - client_credentials grant, scope mcp:service, HTTP Basic client auth, resource parameter. Used by Alexa+ for
                   registration, health checks and tool discovery. No user, no refresh token, 3600 s max.
  user level     - authorization_code + PKCE (S256), scopes mcp:tools mcp:resources. The user lands on /link, says which household
                   member the device belongs to, and the token's subject becomes that person id. That is the account linking.
Alexa+ does not use dynamic client registration; it is given a fixed client id and secret (HEARTH_OAUTH_CLIENT_ID / _SECRET). DCR stays
available only when no fixed client is configured, for local tooling. The MCP SDK provides /authorize, /token (auth code + refresh),
/revoke, the metadata documents, PKCE checks and the bearer middleware; this module adds storage, the fixed client, the
client_credentials grant, the metadata patch, and the consent page. Enabled when HEARTH_PUBLIC_URL is set."""
from __future__ import annotations
import base64, json, os, secrets, time
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, OAuthAuthorizationServerProvider, RefreshToken,
                                      construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from . import db

CODE_TTL, TOKEN_TTL, REFRESH_TTL = 600, 3600, 30 * 86400
SERVICE_SCOPE = "mcp:service"
USER_SCOPES = ["mcp:tools", "mcp:resources"]
SCOPES = [SERVICE_SCOPE] + USER_SCOPES
SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending (txn TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes (code TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (token TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, expires_at REAL NOT NULL);
"""


def public_url() -> str:
    return os.environ.get("HEARTH_PUBLIC_URL", "").rstrip("/")


def enabled() -> bool:
    return bool(public_url())


def resource_uri() -> str:
    return f"{public_url()}/mcp"


def fixed_client() -> OAuthClientInformationFull | None:
    cid = os.environ.get("HEARTH_OAUTH_CLIENT_ID", "").strip()
    if not cid:
        return None
    secret = os.environ.get("HEARTH_OAUTH_CLIENT_SECRET", "").strip() or None
    uris = [u.strip() for u in os.environ.get("HEARTH_OAUTH_REDIRECT_URIS", "").split(",") if u.strip()]
    return OAuthClientInformationFull(client_id=cid, client_secret=secret, client_name="Alexa+", redirect_uris=uris or None,
                                      grant_types=["authorization_code", "refresh_token", "client_credentials"], response_types=["code"],
                                      token_endpoint_auth_method="client_secret_post" if secret else "none", scope=" ".join(SCOPES))


class HearthAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        with db._lock:
            db.conn().executescript(SCHEMA)

    # ---- clients ----
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        fc = fixed_client()
        if fc and fc.client_id == client_id:
            return fc
        row = db.one("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        db.execute("INSERT OR REPLACE INTO oauth_clients(client_id, info, created_at) VALUES (?,?,?)", (client_info.client_id, client_info.model_dump_json(), db.now_iso()))

    # ---- authorization: park the request and send the user to the consent page ----
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        txn = secrets.token_urlsafe(18)
        data = {"state": params.state, "scopes": params.scopes or USER_SCOPES, "code_challenge": params.code_challenge, "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly, "resource": params.resource}
        db.execute("INSERT INTO oauth_pending(txn, client_id, params, expires_at) VALUES (?,?,?,?)", (txn, client.client_id, json.dumps(data), time.time() + CODE_TTL))
        return f"{self.base_url}/link?txn={txn}"

    def complete_link(self, txn: str, person_id: int) -> str | None:
        """Called by the consent page: mint the authorization code and return the client's redirect URL."""
        row = db.one("SELECT * FROM oauth_pending WHERE txn=? AND expires_at > ?", (txn, time.time()))
        if not row or not db.person(person_id):
            return None
        p = json.loads(row["params"])
        code = AuthorizationCode(code=secrets.token_urlsafe(32), scopes=p["scopes"], expires_at=time.time() + CODE_TTL, client_id=row["client_id"],
                                 code_challenge=p["code_challenge"], redirect_uri=p["redirect_uri"], redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                                 resource=p.get("resource"), subject=str(person_id))
        db.execute("INSERT INTO oauth_codes(code, data, expires_at) VALUES (?,?,?)", (code.code, code.model_dump_json(), code.expires_at))
        db.execute("DELETE FROM oauth_pending WHERE txn=?", (txn,))
        extra = {"code": code.code}
        if p.get("state"): extra["state"] = p["state"]
        return construct_redirect_uri(p["redirect_uri"], **extra)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        row = db.one("SELECT data FROM oauth_codes WHERE code=? AND expires_at > ?", (authorization_code, time.time()))
        if not row: return None
        code = AuthorizationCode.model_validate_json(row["data"])
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        db.execute("DELETE FROM oauth_codes WHERE code=?", (authorization_code.code,))
        return self.issue(client.client_id, authorization_code.scopes, authorization_code.subject, authorization_code.resource, refresh=True)

    def issue(self, client_id: str, scopes: list[str], subject: str | None, resource: str | None, refresh: bool) -> OAuthToken:
        access = AccessToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes, expires_at=int(time.time() + TOKEN_TTL), resource=resource, subject=subject)
        db.execute("INSERT INTO oauth_tokens(token, kind, data, expires_at) VALUES (?,?,?,?)", (access.token, "access", access.model_dump_json(), access.expires_at))
        tok = OAuthToken(access_token=access.token, token_type="Bearer", expires_in=TOKEN_TTL, scope=" ".join(scopes))
        if refresh:
            rt = RefreshToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes, expires_at=int(time.time() + REFRESH_TTL), subject=subject)
            db.execute("INSERT INTO oauth_tokens(token, kind, data, expires_at) VALUES (?,?,?,?)", (rt.token, "refresh", rt.model_dump_json(), rt.expires_at))
            tok.refresh_token = rt.token
        db.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (time.time(),))
        return tok

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = db.one("SELECT data FROM oauth_tokens WHERE token=? AND kind='refresh' AND expires_at > ?", (refresh_token, time.time()))
        if not row: return None
        t = RefreshToken.model_validate_json(row["data"])
        return t if t.client_id == client.client_id else None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        db.execute("DELETE FROM oauth_tokens WHERE token=?", (refresh_token.token,))
        return self.issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject, None, refresh=True)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = db.one("SELECT data FROM oauth_tokens WHERE token=? AND kind='access' AND expires_at > ?", (token, time.time()))
        return AccessToken.model_validate_json(row["data"]) if row else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        db.execute("DELETE FROM oauth_tokens WHERE token=?", (token.token,))


def settings(base_url: str) -> AuthSettings:
    return AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp", required_scopes=None,
                        client_registration_options=ClientRegistrationOptions(enabled=fixed_client() is None, valid_scopes=SCOPES, default_scopes=USER_SCOPES),
                        revocation_options=RevocationOptions(enabled=True))


# ---- routes layered in front of the SDK's: client_credentials grant, metadata patch, consent page ----

def _sdk_endpoint(mcp_app, path: str):
    for r in mcp_app.routes:
        if isinstance(r, Route) and r.path == path:
            return r.endpoint
    return None


async def _invoke(endpoint, request: Request):
    """Call an SDK route endpoint whether it is a plain request handler or an ASGI app (the SDK wraps some in CORS middleware)."""
    import inspect
    from starlette.responses import Response
    if inspect.iscoroutinefunction(endpoint) and len(inspect.signature(endpoint).parameters) == 1:
        return await endpoint(request)
    body = await request.body()
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await endpoint(request.scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    headers = {k.decode(): v.decode() for k, v in start.get("headers", []) if k.decode().lower() not in ("content-length",)}
    return Response(payload, status_code=start["status"], headers=headers)


def _oauth_error(code: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": desc}, status_code=status, headers={"Cache-Control": "no-store"})


async def _authenticate_client(provider: HearthAuthProvider, request: Request, form) -> OAuthClientInformationFull | None:
    client_id, secret = form.get("client_id"), form.get("client_secret")
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
            client_id, secret = raw.split(":", 1)
        except Exception:
            return None
    if not client_id:
        return None
    client = await provider.get_client(str(client_id))
    if not client:
        return None
    if client.client_secret and not (secret and secrets.compare_digest(str(secret), client.client_secret)):
        return None
    return client


def override_routes(mcp_app, provider: HearthAuthProvider) -> list[Route]:
    from mcp.server.auth.routes import build_metadata
    sdk_token = _sdk_endpoint(mcp_app, "/token")
    cfg = settings(provider.base_url)

    async def token(request: Request):
        from urllib.parse import parse_qs
        body = await request.body()                      # cached on the request, so the SDK handler can still read it
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        if form.get("grant_type") != "client_credentials":
            return await _invoke(sdk_token, request)
        client = await _authenticate_client(provider, request, form)
        if not client:
            return _oauth_error("invalid_client", "client authentication failed", 401)
        if "client_credentials" not in (client.grant_types or []):
            return _oauth_error("unauthorized_client", "client may not use client_credentials")
        scopes = str(form.get("scope") or SERVICE_SCOPE).split()
        if any(s != SERVICE_SCOPE for s in scopes):
            return _oauth_error("invalid_scope", f"client_credentials tokens may only carry {SERVICE_SCOPE}")
        resource = str(form.get("resource") or "")
        if resource and resource.rstrip("/") != resource_uri().rstrip("/"):
            return _oauth_error("invalid_target", f"resource must be {resource_uri()}")
        tok = provider.issue(client.client_id, [SERVICE_SCOPE], None, resource or resource_uri(), refresh=False)
        return JSONResponse(tok.model_dump(exclude_none=True), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def metadata(request: Request):
        meta = build_metadata(cfg.issuer_url, cfg.service_documentation_url, cfg.client_registration_options, cfg.revocation_options)
        body = json.loads(meta.model_dump_json(exclude_none=True))
        body["grant_types_supported"] = sorted(set(body.get("grant_types_supported", [])) | {"client_credentials"})
        body["token_endpoint_auth_methods_supported"] = ["client_secret_basic", "client_secret_post"]
        body["scopes_supported"] = SCOPES
        return JSONResponse(body, headers={"Cache-Control": "public, max-age=3600"})

    routes = [Route("/token", token, methods=["POST"]), Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"])]
    return routes + link_routes(provider)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Link Alexa+ to Hearth</title>
<style>body{margin:0;font:16px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;background:#f7f3ee;color:#2b2622;display:grid;place-items:center;min-height:100vh}
.card{background:#fff;border:1px solid #e7dfd6;border-radius:16px;padding:28px 30px;max-width:440px;width:92%%}h1{margin:0 0 6px;font-size:24px}h1 span{color:#b5542d}
p{color:#7a716a;margin:6px 0 16px}label{display:block;font-size:14px;color:#7a716a;margin-top:12px}select{font:inherit;width:100%%;padding:9px;border:1px solid #e7dfd6;border-radius:9px;margin-top:4px}
button{font:inherit;margin-top:18px;width:100%%;padding:11px;border-radius:10px;border:0;background:#b5542d;color:#fff;font-weight:600;cursor:pointer}.note{font-size:13px;color:#7a716a;margin-top:12px}</style></head>
<body><div class="card"><h1><span>Hearth</span> · link your Alexa+</h1><p>Alexa+ is asking to run daily check-ins through Hearth. Choose whose home this device is in. Hearth will only ever talk about that person and only to their listed contacts.</p>
<form method="post" action="/link"><input type="hidden" name="txn" value="%(txn)s"><label>This device belongs to</label><select name="person_id">%(options)s</select>
<button type="submit">Link and allow check-ins</button></form><div class="note">Prototype: no password. A real deployment would sign the caregiver in first.</div></div></body></html>"""


def link_routes(provider: HearthAuthProvider) -> list[Route]:
    async def link_get(request: Request):
        txn = request.query_params.get("txn", "")
        if not db.one("SELECT txn FROM oauth_pending WHERE txn=? AND expires_at > ?", (txn, time.time())):
            return HTMLResponse("<p style='font-family:sans-serif'>This link request has expired. Start again from Alexa+.</p>", status_code=400)
        options = "".join(f'<option value="{p["id"]}">{p["name"]}</option>' for p in db.persons())
        return HTMLResponse(PAGE % {"txn": txn, "options": options})

    async def link_post(request: Request):
        form = await request.form()
        try:
            url = provider.complete_link(str(form.get("txn", "")), int(form.get("person_id", "0")))
        except ValueError:
            url = None
        if not url:
            return HTMLResponse("<p style='font-family:sans-serif'>This link request has expired. Start again from Alexa+.</p>", status_code=400)
        return RedirectResponse(url, status_code=302)

    return [Route("/link", link_get, methods=["GET"]), Route("/link", link_post, methods=["POST"])]


def linked_person_id() -> int | None:
    """The person bound to the caller's access token, if the request came through OAuth with a user-level token."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        tok = get_access_token()
        return int(tok.subject) if tok and tok.subject else None
    except Exception:
        return None
