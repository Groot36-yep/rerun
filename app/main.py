"""The web server: Plex tuner endpoints, the guide, live streams and the web page."""
from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api, guide, images, seasons
from .channels import HOLIDAY_CHANNEL_NAMES
from .core import App
from .security import ip_allowed
from .streaming import TunersBusy

log = logging.getLogger("rerun")
mimetypes.add_type("font/woff2", ".woff2")  # older systems don't know it
STATIC = Path(__file__).parent / "static"
PORT = int(os.environ.get("RERUN_PORT", "8410"))

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


class Guard:
    """Answer only devices on your home network, and add browser security headers."""

    def __init__(self, asgi, get_app):
        self.asgi = asgi
        self.get_app = get_app
        self.refused: set[str] = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.asgi(scope, receive, send)
        app = self.get_app()
        host = (scope.get("client") or ("", 0))[0]
        if app and app.cfg.data["security"].get("lan_only", True) and not ip_allowed(host, app.extra_networks()):
            if app.diag.wants(scope.get("path", "")):
                app.diag.request(host, scope.get("method", ""), scope.get("path", ""), 403, 0, "refused: not home network")
            if host not in self.refused:
                self.refused.add(host)
                log.warning("Refused %s from %s: it isn't on your home network. If that's your Plex server, "
                            "add its address under Settings → Privacy & security.", scope.get("path"), host)
            resp = PlainTextResponse("Rerun only answers devices on your home network.", status_code=403)
            return await resp(scope, receive, send)

        path = scope.get("path", "")
        static = path.startswith("/static/")
        record = app is not None and app.diag.wants(path)
        began = time.time()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                if record:
                    agent = next((v.decode(errors="replace") for k, v in scope.get("headers", []) if k == b"user-agent"), "")
                    app.diag.request(host, scope.get("method", ""), path, message["status"],
                                     (time.time() - began) * 1000, agent)
                h = MutableHeaders(scope=message)
                if static:
                    h["Cache-Control"] = "no-cache"  # always check for a newer page after an update
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "no-referrer")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
            await send(message)

        await self.asgi(scope, receive, send_wrapper)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    app = App(PORT)
    fastapi_app.state.rerun = app
    await app.start()
    log.info("Rerun is ready on port %s", PORT)
    try:
        yield
    finally:
        await app.stop()


web = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
web.add_middleware(Guard, get_app=lambda: getattr(web.state, "rerun", None))
web.include_router(api.router)
web.mount("/static", StaticFiles(directory=STATIC), name="static")


def _app(request: Request) -> App:
    return request.app.state.rerun


def _base(request: Request) -> str:
    return api._base_url(request)


def _index_html() -> str:
    # Stamp the page's script and stylesheet with their contents, so a browser never mixes an old copy
    # with a new one after an update.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        stamp = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={stamp}")
    return html


INDEX_HTML = _index_html()


@web.get("/", include_in_schema=False)
async def index():
    return Response(INDEX_HTML, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@web.get("/health", include_in_schema=False)
async def health(request: Request):
    app = _app(request)
    return {"ok": True, "streams": len(app.streams.status()), "ffmpeg": app.ffmpeg.available}


# ---- what Plex talks to (an HDHomeRun-style tuner) ---------------------------------------

@web.api_route("/discover.json", methods=["GET", "HEAD"])
@web.get("/device.json")
async def discover(request: Request):
    return guide.discover(_app(request), _base(request))


@web.api_route("/lineup_status.json", methods=["GET", "HEAD"])
async def lineup_status():
    return guide.lineup_status()


@web.api_route("/lineup.json", methods=["GET", "HEAD"])
async def lineup(request: Request):
    return guide.lineup(_app(request), _base(request))


@web.api_route("/lineup.post", methods=["GET", "POST"])
async def lineup_post():
    return Response(status_code=200)


@web.api_route("/xmltv.xml", methods=["GET", "HEAD"])
async def xmltv(request: Request):
    return Response(guide.xmltv(_app(request), _base(request)), media_type="application/xml")


@web.api_route("/iptv.m3u", methods=["GET", "HEAD"])
async def m3u(request: Request):
    return Response(guide.m3u(_app(request), _base(request)), media_type="audio/x-mpegurl")


@web.api_route("/logo/{ver}/{cid}.png", methods=["GET", "HEAD"])
@web.api_route("/logo/{cid}.png", methods=["GET", "HEAD"])
async def logo(cid: str, request: Request, ver: str = ""):
    app = _app(request)
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404)
    data = await images.channel_logo(ch, app.catalog, app.festive(), app.cfg.data["metadata"].get("online_lookups", True))
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@web.api_route("/netlogo/{name}.png", methods=["GET", "HEAD"])
async def network_logo_preview(name: str, request: Request):
    """A network's logo, for picking networks before they have a channel."""
    app = _app(request)
    if len(name) > 80:
        raise HTTPException(404)
    pseudo = {"id": "net-" + hashlib.sha1(name.encode()).hexdigest()[:12], "name": name, "auto_key": f"network:{name}"}
    data = await images.channel_logo(pseudo, app.catalog, "", app.cfg.data["metadata"].get("online_lookups", True))
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@web.api_route("/holidaylogo/{key}.png", methods=["GET", "HEAD"])
async def holiday_logo_preview(key: str, request: Request):
    if key not in seasons.PRESETS:
        raise HTTPException(404)
    name = HOLIDAY_CHANNEL_NAMES.get(key, f"{seasons.PRESETS[key]['name']} TV")
    data = await images.channel_logo({"id": f"hol-{key}", "name": name, "season": key}, None, "", False)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@web.api_route("/img/{media_id}.jpg", methods=["GET", "HEAD"])
async def artwork(media_id: str, request: Request):
    data = await images.artwork(_app(request), media_id)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@web.get("/stream/{number}.ts")
@web.get("/auto/v{number}")
async def stream(number: int, request: Request):
    app = _app(request)
    ch = app.channel_by_number(number)
    if not ch:
        raise HTTPException(404, "No such channel")
    began = time.time()
    already_on = ch["id"] in app.streams.by_channel
    try:
        who = f"{request.client.host if request.client else '?'} since {time.strftime('%H:%M:%S')}"
        sub, st = await app.streams.subscribe(ch, who)
    except TunersBusy:
        log.warning("Channel %s was asked for, but all %s tuners are in use", number,
                    app.cfg.data["stream"].get("tuners"))
        raise HTTPException(503, "All tuners are busy")
    agent = request.headers.get("user-agent", "")

    async def body():
        first = True
        try:
            while True:
                chunk = await sub.q.get()
                if chunk is None:
                    break
                if first:
                    first = False
                    app.diag.tune(number, time.time() - began, already_on, agent)
                yield chunk
        finally:
            st.unsubscribe(sub)

    return StreamingResponse(body(), media_type="video/mp2t", headers={"Cache-Control": "no-store"})


# ---- used only by Rerun's own ffmpeg, from this machine -----------------------------------

def _local_only(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        raise HTTPException(403)


@web.get("/internal/list/{token}", include_in_schema=False)
async def internal_list(token: str, request: Request):
    _local_only(request)
    text = _app(request).streams.playlist(token)
    if text is None:
        raise HTTPException(404)
    return PlainTextResponse(text)


@web.get("/internal/piece/{token}/{n}", include_in_schema=False)
async def internal_piece(token: str, n: int, request: Request):
    _local_only(request)
    app = _app(request)
    if token not in app.streams.by_token:
        raise HTTPException(404)
    return StreamingResponse(app.streams.piece(token), media_type="video/mp2t")


@web.get("/internal/report", include_in_schema=False)
async def internal_report(request: Request):
    """The diagnostic report for "rerun report" on this computer."""
    _local_only(request)
    app = _app(request)
    if not hmac.compare_digest(request.headers.get("x-report-key", ""), app.report_key):
        raise HTTPException(403)
    return PlainTextResponse(await app.diag.report(app))


@web.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"error": exc.detail or "Error"}, status_code=exc.status_code)
