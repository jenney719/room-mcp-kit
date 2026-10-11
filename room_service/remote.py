"""Authenticated, single-port Room website and Streamable HTTP MCP gateway."""
from __future__ import annotations

from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from .app import create_app as create_rest_app
from .config import ROOT, RoomSettings
from .mcp_server import create_server

COOKIE = "room_session"
SESSION_SECONDS = 7 * 24 * 3600
LOGIN_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>进入 Room</title><style>
body{margin:0;background:#eadfce;color:#43372c;font:16px system-ui;display:grid;place-items:center;min-height:100vh}
main{background:#fffaf2;padding:32px;border-radius:18px;width:min(320px,75vw);box-shadow:0 8px 30px #43372c20}
h1{margin-top:0}input,button{box-sizing:border-box;width:100%;padding:12px;font:inherit;border-radius:8px;border:1px solid #c8b99e}
button{margin-top:16px;background:#755c46;color:white;cursor:pointer}p{line-height:1.6}
</style><main><h1>Room</h1><p>输入房间密码，进入我们的像素房间。</p>
<form method="post" action="/login"><label for="password">房间密码</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus maxlength="256">
<button>进入房间</button></form>MESSAGE</main></html>"""


@dataclass(frozen=True)
class RemoteSettings:
    mcp_token: str
    web_password: str
    public_url: str
    db_path: Path

    def validate(self):
        if min(len(self.mcp_token), len(self.web_password)) < 32:
            raise ValueError("MCP_AUTH_TOKEN and ROOM_WEB_PASSWORD must have at least 32 characters")
        if self.mcp_token == self.web_password:
            raise ValueError("Website and MCP credentials must be different")
        parsed = urlsplit(self.public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in {None, 443}
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("ROOM_PUBLIC_URL must be an HTTPS origin without a path")
        return self

    @classmethod
    def from_env(cls):
        return cls(
            mcp_token=os.environ.get("MCP_AUTH_TOKEN", ""),
            web_password=os.environ.get("ROOM_WEB_PASSWORD", ""),
            public_url=os.environ.get("ROOM_PUBLIC_URL", "").rstrip("/"),
            db_path=Path(os.environ.get("ROOM_DB", ROOT / "room_service/data/room.db")),
        ).validate()


def session_cookie(password: str, expires: int) -> str:
    value = str(expires)
    signature = hmac.new(password.encode(), ("room-web-session:" + value).encode(), hashlib.sha256).hexdigest()
    return value + "." + signature


def valid_session(cookie: str, password: str) -> bool:
    try:
        expires = int(cookie.split(".", 1)[0])
        now = int(time.time())
        return now < expires <= now + SESSION_SECONDS and hmac.compare_digest(cookie, session_cookie(password, expires))
    except (ValueError, TypeError):
        return False


class RoomAccess:
    def __init__(self, app, *, settings: RemoteSettings, rest_token: str):
        self.app, self.settings, self.rest_token = app, settings, rest_token
        self.hostname = urlsplit(settings.public_url).hostname

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope["path"]

        async def reply(response):
            return await response(scope, receive, safe_send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in {b"cache-control", b"referrer-policy", b"x-content-type-options"}]
                message = {**message, "headers": headers + [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"), (b"x-content-type-options", b"nosniff")]}
            await send(message)

        if request.url.hostname not in {self.hostname, "localhost", "127.0.0.1", "::1"}:
            return await reply(JSONResponse({"error": "invalid_host"}, status_code=400))
        if path in {"/health", "/mcp"}:
            expected = "Bearer " + self.settings.mcp_token
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
                return await reply(JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}))
        else:
            if request.method not in {"GET", "HEAD"}:
                # Cookie authentication never grants cross-origin write access.
                origins = {self.settings.public_url.rstrip("/"), str(request.base_url).rstrip("/")}
                if request.headers.get("origin", "") not in origins:
                    return await reply(JSONResponse({"error": "invalid_origin"}, status_code=403))
            if path != "/login" and not valid_session(request.cookies.get(COOKIE, ""), self.settings.web_password):
                if path.startswith("/api/") or request.method not in {"GET", "HEAD"}:
                    return await reply(JSONResponse({"error": "login_required"}, status_code=401))
                return await reply(RedirectResponse("/login", status_code=303))
        if path.startswith("/api/v1/room/"):
            allowed = ((request.method == "GET" and path.rsplit("/", 1)[-1] in {"state", "events"})
                       or (request.method == "POST" and path in {"/api/v1/room/characters/owner/move", "/api/v1/room/characters/owner/use", "/api/v1/room/characters/owner/stop"})
                       or (request.method == "PUT" and path == "/api/v1/room/layout"))
            if not allowed:
                return await reply(JSONResponse({"error": "not_found"}, status_code=404))
            scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k.lower() != b"x-room-token"] + [(b"x-room-token", self.rest_token.encode())]}
        limit = 4096 if path == "/login" else 4 * 1024 * 1024 if path == "/mcp" else 65536
        try:
            if int(request.headers.get("content-length", "0")) > limit:
                return await reply(JSONResponse({"error": "body_too_large"}, status_code=413))
        except ValueError:
            return await reply(JSONResponse({"error": "invalid_length"}, status_code=400))
        # Bound chunked bodies too, before handlers process them; SSE GET has no body.
        if request.method not in {"GET", "HEAD"}:
            chunks, total = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                total += len(message.get("body", b""))
                if total > limit:
                    return await reply(JSONResponse({"error": "body_too_large"}, status_code=413))
                chunks.append(message)
                if not message.get("more_body", False):
                    break
            original_receive = receive

            async def replay():
                return chunks.pop(0) if chunks else await original_receive()

            receive = replay
        await self.app(scope, receive, safe_send)


def create_remote_app(settings: RemoteSettings | None = None):
    settings = (settings or RemoteSettings.from_env()).validate()
    rest_token = secrets.token_urlsafe(32)
    rest = create_rest_app(RoomSettings(rest_token, settings.db_path,
        ROOT / "web/room/data/initial-state.json", ROOT / "web/room/data/room-map.json"))
    store = rest.state.store
    hostname = urlsplit(settings.public_url).hostname
    mcp = create_server(store).streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[hostname, hostname + ":443", "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[settings.public_url.rstrip("/"), "http://127.0.0.1:*", "http://localhost:*"],
        ),
    )
    failed_logins: dict[str, deque] = {}

    async def login(request: Request):
        if request.method == "GET":
            return HTMLResponse(LOGIN_HTML.replace("MESSAGE", ""))
        now = time.monotonic()
        key = request.client.host if request.client else "unknown"
        # Bound memory even when many distinct addresses try passwords.
        for peer in list(failed_logins):
            while failed_logins[peer] and failed_logins[peer][0] < now - 900:
                failed_logins[peer].popleft()
            if not failed_logins[peer]:
                del failed_logins[peer]
        failures = failed_logins.get(key, deque())
        if len(failures) >= 10 or len(failed_logins) >= 4096:
            return HTMLResponse(LOGIN_HTML.replace("MESSAGE", "<p>尝试次数过多，请稍后再试。</p>"), status_code=429, headers={"Retry-After": "900"})
        body = (await request.body()).decode("utf-8", errors="replace")
        password = parse_qs(body).get("password", [""])[0]
        if not hmac.compare_digest(password.encode(), settings.web_password.encode()):
            failures.append(now)
            failed_logins[key] = failures
            return HTMLResponse(LOGIN_HTML.replace("MESSAGE", "<p>密码不正确。</p>"), status_code=401)
        failed_logins.pop(key, None)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, session_cookie(settings.web_password, int(time.time()) + SESSION_SECONDS),
                            max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="strict")
        return response

    async def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        return response

    async def health(request: Request):
        state = store.snapshot(scope="characters")
        return JSONResponse({"status": "ok", "auth": "enabled", "service": "room-mcp-kit", "revision": state["revision"]})

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(rest.router.lifespan_context(rest))
            await stack.enter_async_context(mcp.router.lifespan_context(mcp))
            yield

    app = Starlette(routes=[Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]), Route("/health", health),
        Route("/api/v1/room/{room_path:path}", rest), *mcp.routes,
        Mount("/", StaticFiles(directory=ROOT / "web", html=True))], lifespan=lifespan)
    app.state.store = store
    return RoomAccess(app, settings=settings, rest_token=rest_token)


def main():
    host = os.environ.get("ROOM_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Remote Room must listen on loopback behind an HTTPS proxy")
    uvicorn.run(create_remote_app(), host=host, port=int(os.environ.get("ROOM_PORT", "18460")),
                access_log=False, proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1")


if __name__ == "__main__":
    main()
