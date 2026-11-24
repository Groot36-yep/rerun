"""The JSON API behind the web page. Everything here needs you to be signed in."""
from __future__ import annotations

import asyncio
from collections import Counter
import ipaddress
import logging
import os
import random
import socket
import httpx
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from urllib.parse import quote

from . import __version__, seasons, xmas, xmasdata
from . import archive as archive_mod
from . import lineups as lineups_mod
from . import youtube as youtube_mod
from . import preview as preview_mod
from . import channels as channels_mod
from .archive import OWN as OWN_COMMERCIALS
from .guide import logo_url as plex_logo_url, tuner_id
from .store import DATA_DIR
from .channels import (
    BREAK_STYLES, HOLIDAY_CHANNEL_NAMES, channel_from_suggestion, describe_content, library_networks,
    network_logo, network_rules, next_free_number, normalize_channel, suggest, sync_auto,
)
from .images import VERSION as LOGO_V
from .logos_table import TMDB_LOGOS, WIKIPEDIA_ARTICLES
from .networks import CATEGORIES, HOLIDAY_START, family_for
from .folders import VIDEO_EXT
from .ffmpeg import RESOLUTIONS
from .metadata import test_tmdb_key
from .plex import PlexClient, PlexError, check_pin, create_pin, find_servers, pick_connection
from .security import hash_password, verify_password

COOKIE = "rerun_session"
log = logging.getLogger("rerun")
router = APIRouter(prefix="/api")
_pins: dict[int, dict] = {}


def rerun(request: Request):
    return request.app.state.rerun


async def signed_in(request: Request):
    app = rerun(request)
    if not app.sessions.valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Please sign in")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-rerun") != "1":
        raise HTTPException(403, "Request blocked")
    return app


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _set_cookie(resp: Response, request: Request, token: str):
    resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", path="/")


def _base_url(request: Request) -> str:
    host = request.headers.get("host") or f"127.0.0.1:{rerun(request).port}"
    if not all(c.isalnum() or c in ".:-[]" for c in host):
        host = f"127.0.0.1:{rerun(request).port}"
    return f"{request.url.scheme}://{host}"


# ---- sign in ------------------------------------------------------------------------------

@router.get("/bootstrap")
async def bootstrap(request: Request):
    app = rerun(request)
    return {
        "needs_password": not app.secrets.data.get("password_hash"),
        "signed_in": app.sessions.valid(request.cookies.get(COOKIE)),
        "setup_complete": app.cfg.data.get("setup_complete", False),
        "version": __version__,
    }


@router.post("/auth/setup")
async def auth_setup(request: Request, body: dict):
    app = rerun(request)
    if request.headers.get("x-rerun") != "1":
        raise HTTPException(403, "Request blocked")
    if app.secrets.data.get("password_hash"):
        raise HTTPException(409, "A password is already set")
    pw = str(body.get("password") or "")
    if len(pw) < 8:
        raise HTTPException(400, "Use at least 8 characters")
    app.secrets.data["password_hash"] = await asyncio.to_thread(hash_password, pw)
    app.secrets.save()
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, request, app.sessions.create())
    return resp


@router.post("/auth/login")
async def auth_login(request: Request, body: dict):
    app = rerun(request)
    if request.headers.get("x-rerun") != "1":
        raise HTTPException(403, "Request blocked")
    ip = _client_ip(request)
    wait = app.limiter.wait_seconds(ip)
    if wait:
        raise HTTPException(429, f"Too many tries. Wait {wait} seconds.")
    ok = await asyncio.to_thread(verify_password, str(body.get("password") or ""), app.secrets.data["password_hash"])
    if not ok:
        app.limiter.fail(ip)
        raise HTTPException(401, "That password isn't right")
    app.limiter.clear(ip)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, request, app.sessions.create())
    return resp


@router.post("/auth/logout")
async def auth_logout(request: Request):
    rerun(request).sessions.revoke(request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.post("/auth/password")
async def auth_password(request: Request, body: dict, app=Depends(signed_in)):
    ok = await asyncio.to_thread(verify_password, str(body.get("current") or ""), app.secrets.data["password_hash"])
    if not ok:
        raise HTTPException(401, "Your current password isn't right")
    new = str(body.get("new") or "")
    if len(new) < 8:
        raise HTTPException(400, "Use at least 8 characters")
    app.secrets.data["password_hash"] = await asyncio.to_thread(hash_password, new)
    app.secrets.save()
    app.sessions.revoke_all()
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, request, app.sessions.create())
    return resp


# ---- Plex ---------------------------------------------------------------------------------

def _friendly(e: Exception) -> str:
    if isinstance(e, PlexError):
        return str(e)
    return f"Couldn't reach Plex ({e.__class__.__name__})."


@router.post("/plex/pin")
async def plex_pin(app=Depends(signed_in)):
    try:
        pin = await create_pin(app.secrets.data["client_id"])
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach plex.tv to start sign-in ({e.__class__.__name__}).")
    _pins[int(pin["id"])] = {"created": time.time()}
    for pid in [p for p, v in _pins.items() if v["created"] < time.time() - 900]:
        _pins.pop(pid, None)
    return pin


@router.get("/plex/pin/{pin_id}")
async def plex_pin_check(pin_id: int, app=Depends(signed_in)):
    entry = _pins.get(pin_id)
    if entry is None:
        raise HTTPException(404, "That sign-in expired. Try again.")
    if "servers" in entry:
        return {"authorized": True, "servers": [{k: s[k] for k in ("name", "id", "owned")} for s in entry["servers"]]}
    token = await check_pin(app.secrets.data["client_id"], pin_id)
    if not token:
        return {"authorized": False}
    servers = await find_servers(app.secrets.data["client_id"], token)
    entry.update(token=token, servers=servers)
    return {"authorized": True, "servers": [{k: s[k] for k in ("name", "id", "owned")} for s in servers]}


async def _save_plex(app, url: str, token: str, name: str, server_id: str):
    app.cfg.data["plex"].update(url=url.rstrip("/"), server_name=name, server_id=server_id)
    app.secrets.data["plex_token"] = token
    app.cfg.save()
    app.secrets.save()


@router.post("/plex/use-server")
async def plex_use_server(body: dict, app=Depends(signed_in)):
    entry = _pins.get(int(body.get("pin_id") or 0)) or {}
    server = next((s for s in entry.get("servers", []) if s["id"] == body.get("server_id")), None)
    if not server:
        raise HTTPException(404, "Pick a server from the list")
    url = await pick_connection(app.secrets.data["client_id"], server)
    if not url:
        raise HTTPException(502, f"Signed in, but Rerun couldn't reach “{server['name']}” from this machine. "
                                 "Use the manual option with the server's address.")
    await _save_plex(app, url, server["token"], server["name"], server["id"])
    return {"ok": True, "url": url, "name": server["name"]}


@router.post("/plex/new-tuner")
async def new_tuner(app=Depends(signed_in)):
    """Give the tuner a new ID so Plex treats it as a different device, forgetting old logos."""
    app.cfg.data["tuner_generation"] = int(app.cfg.data.get("tuner_generation") or 0) + 1
    app.cfg.save()
    log.info("Rerun now shows Plex a new tuner ID (%s)", tuner_id(app))
    return {"ok": True, "device_id": tuner_id(app)}


@router.post("/plex/manual")
async def plex_manual(body: dict, app=Depends(signed_in)):
    url = str(body.get("url") or "").strip().rstrip("/")
    token = str(body.get("token") or "").strip() or app.secrets.data.get("plex_token", "")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if ":" not in url.split("//", 1)[1]:
        url += ":32400"
    try:
        async with PlexClient(url, token, app.secrets.data["client_id"], timeout=8) as p:
            ident = await p.identity()
            await p.sections()
    except Exception as e:
        raise HTTPException(502, _friendly(e))
    await _save_plex(app, url, token, ident.get("friendlyName") or "Plex", ident.get("machineIdentifier", ""))
    return {"ok": True, "url": url}


@router.get("/plex/status")
async def plex_status(app=Depends(signed_in)):
    plex = app.plex()
    if not plex:
        return {"connected": False}
    try:
        await asyncio.wait_for(plex.identity(), 8)
        return {"connected": True, "url": plex.url, "name": app.cfg.data["plex"].get("server_name")}
    except Exception as e:
        return {"connected": False, "url": plex.url, "error": _friendly(e)}


@router.get("/plex/libraries")
async def plex_libraries(app=Depends(signed_in)):
    plex = app.plex()
    if not plex:
        raise HTTPException(400, "Connect Plex first")
    try:
        return await plex.sections()
    except Exception as e:
        raise HTTPException(502, _friendly(e))


# ---- sources and folders ------------------------------------------------------------------

@router.get("/sources")
async def get_sources(app=Depends(signed_in)):
    return {"sources": app.cfg.data["sources"], "media_roots": app.media_roots}


@router.put("/sources")
async def put_sources(body: dict, app=Depends(signed_in)):
    clean = {}
    for kind in ("shows", "movies", "music_videos", "commercials"):
        out = []
        for s in body.get(kind) or []:
            if s.get("type") == "plex" and s.get("id"):
                out.append({"type": "plex", "id": str(s["id"]), "name": str(s.get("name") or "")[:100]})
            elif s.get("type") == "folder" and s.get("path"):
                path = os.path.realpath(str(s["path"]))
                if not app.browse_ok(path) or not os.path.isdir(path):
                    raise HTTPException(400, f"Rerun can't use {s['path']}")
                out.append({"type": "folder", "path": path})
        clean[kind] = out
    app.cfg.data["sources"] = clean
    app.cfg.save()
    return {"ok": True}


@router.get("/folders")
async def folders(path: str = "", app=Depends(signed_in)):
    if not path:
        home = os.path.realpath(os.path.expanduser("~"))
        names = {home: "Home folder", "/mnt": "Drives (/mnt)", "/media": "Drives (/media)"}
        return {"path": "", "parent": None, "videos": 0,
                "dirs": [{"name": names.get(r, r), "path": r, "place": True} for r in app.places()]}
    real = os.path.realpath(os.path.expanduser(path.strip()))
    if not os.path.isdir(real):
        raise HTTPException(404, "There's no folder there")
    if not app.browse_ok(real):
        raise HTTPException(403, "That's a system folder; Rerun leaves those alone")
    dirs, videos = [], 0
    try:
        for e in sorted(os.scandir(real), key=lambda e: e.name.lower()):
            if e.name.startswith((".", "@")):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append({"name": e.name, "path": e.path})
            elif os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                videos += 1
    except OSError:
        raise HTTPException(403, "Rerun can't read that folder")
    parent = os.path.dirname(real)
    return {"path": real, "parent": parent if parent != real and app.browse_ok(parent) else "",
            "dirs": dirs, "videos": videos}


# ---- settings -----------------------------------------------------------------------------

@router.get("/settings")
async def get_settings(app=Depends(signed_in)):
    d = app.cfg.data
    return {
        "stream": d["stream"], "security": d["security"], "metadata": d["metadata"],
        "rescan_hour": d["rescan_hour"], "guide_hours": d["guide_hours"], "path_map": d["path_map"],
        "has_tmdb_key": bool(app.secrets.data.get("tmdb_key")),
        "encoder": app.ffmpeg.encoder_status, "ffmpeg": app.ffmpeg.version,
        "plex": {"url": d["plex"].get("url"), "name": d["plex"].get("server_name")},
        "auto": {k: v for k, v in d["auto"].items() if k != "dismissed"}, "dismissed": len(d["auto"]["dismissed"]),
        "festive_logos": d.get("festive_logos", False),
        "plex_logos": d.get("plex_logos", "public"),
    }


def _num(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


@router.put("/settings")
async def put_settings(body: dict, app=Depends(signed_in)):
    d = app.cfg.data
    old_stream = dict(d["stream"])
    if "stream" in body:
        s, cur = body["stream"], d["stream"]
        if s.get("resolution") in RESOLUTIONS:
            cur["resolution"] = s["resolution"]
        cur["video_kbps"] = _num(s.get("video_kbps", cur["video_kbps"]), 1000, 20000, 4000)
        cur["audio_kbps"] = _num(s.get("audio_kbps", cur["audio_kbps"]), 96, 320, 192)
        if s.get("hwaccel") in ("auto", "none", "vaapi", "nvenc"):
            cur["hwaccel"] = s["hwaccel"]
        if str(s.get("vaapi_device", "")).startswith("/dev/dri/"):
            cur["vaapi_device"] = s["vaapi_device"]
        cur["even_volume"] = bool(s.get("even_volume", cur["even_volume"]))
        cur["screen_bug"] = bool(s.get("screen_bug", cur.get("screen_bug", True)))
        cur["up_next"] = bool(s.get("up_next", cur.get("up_next", True)))
        cur["tuners"] = _num(s.get("tuners", cur["tuners"]), 1, 20, 4)
    if "security" in body:
        sec = body["security"]
        d["security"]["lan_only"] = bool(sec.get("lan_only", True))
        nets = []
        for n in sec.get("extra_networks") or []:
            try:
                nets.append(str(ipaddress.ip_network(str(n).strip(), strict=False)))
            except ValueError:
                raise HTTPException(400, f"“{n}” isn't a network like 203.0.113.0/24")
        d["security"]["extra_networks"] = nets
    if "metadata" in body:
        d["metadata"]["online_lookups"] = bool(body["metadata"].get("online_lookups", True))
        d["metadata"]["include_specials"] = bool(body["metadata"].get("include_specials", False))
    if "rescan_hour" in body:
        d["rescan_hour"] = _num(body["rescan_hour"], 0, 23, 4)
    if "guide_hours" in body:
        d["guide_hours"] = _num(body["guide_hours"], 24, 168, 72)
    if "path_map" in body:
        d["path_map"] = [{"from": str(m.get("from", "")), "to": str(m.get("to", ""))}
                         for m in body["path_map"] or [] if m.get("from") and m.get("to")]
    if body.get("tmdb_key") is not None:
        app.secrets.data["tmdb_key"] = str(body["tmdb_key"]).strip()
        app.secrets.save()
    if "auto" in body:
        a = body["auto"]
        d["auto"]["networks"] = bool(a.get("networks", d["auto"]["networks"]))
        d["auto"]["holidays"] = bool(a.get("holidays", d["auto"]["holidays"]))
        d["auto"]["min_shows"] = _num(a.get("min_shows", d["auto"]["min_shows"]), 1, 20, 2)
    if "festive_logos" in body:
        d["festive_logos"] = bool(body["festive_logos"])
    if body.get("plex_logos") in ("public", "direct"):
        d["plex_logos"] = body["plex_logos"]
    app.cfg.save()
    if d["stream"] != old_stream:
        await app.ffmpeg.self_test(d["stream"])
    return await get_settings(app)


@router.post("/settings/test-tmdb")
async def settings_test_tmdb(body: dict, app=Depends(signed_in)):
    key = str(body.get("key") or app.secrets.data.get("tmdb_key") or "").strip()
    if not key:
        return {"ok": False}
    return {"ok": await test_tmdb_key(key)}


# ---- scanning and suggestions -------------------------------------------------------------

@router.post("/scan")
async def start_scan(app=Depends(signed_in)):
    app.scanner.start()
    return app.scanner.progress


@router.get("/scan")
async def scan_progress(app=Depends(signed_in)):
    return {**app.scanner.progress, "catalog": app.catalog.summary()}


def _suggest(app):
    d = app.cfg.data
    return suggest(app.catalog, d["channels"], d["holidays"], d["auto"].get("min_shows", 2))


@router.get("/suggestions")
async def suggestions(app=Depends(signed_in)):
    out = _suggest(app)
    for s in out:
        s["logo_url"] = f"/netlogo/{quote(s['name'], safe='')}.png?v={LOGO_V}" if s["key"].startswith("network:") else ""
    return {"suggestions": out, "has_commercials": bool(app.catalog.filler)}


@router.post("/channels/from-suggestions")
async def from_suggestions(body: dict, app=Depends(signed_in)):
    wanted = set(body.get("keys") or [])
    d = app.cfg.data
    made = []
    for s in _suggest(app):
        if s["key"] in wanted:
            ch = channel_from_suggestion(s, d["channels"], bool(app.catalog.filler))
            d["channels"].append(ch)
            made.append(ch)
        elif body.get("dismiss_others") and s["key"].startswith(("network:", "holiday:")):
            # You unticked it during setup, so don't add it behind your back later.
            if s["key"] not in d["auto"]["dismissed"]:
                d["auto"]["dismissed"].append(s["key"])
    app.cfg.save()
    _build_soon(app, made)
    return {"created": len(made)}


def _build_soon(app, chans):
    async def go():
        for ch in chans:
            try:
                await app.schedules.ensure(ch, time.time() + 6 * 3600)
            except Exception:
                pass
    asyncio.create_task(go())


@router.post("/setup/complete")
async def setup_complete(app=Depends(signed_in)):
    app.cfg.data["setup_complete"] = True
    app.cfg.save()
    return {"ok": True}


# ---- channels -----------------------------------------------------------------------------

def _now_next(app, ch):
    p = app.schedules.get(ch["id"])
    now = time.time()
    cur = p.block_at(now)
    nxt = next((b for b in p.blocks if b["s"] >= (cur["e"] if cur else now)), None)

    def slim(b):
        return {"s": b["s"], "e": b["e"], "title": b.get("title"), "sub": b.get("sub")} if b else None

    return slim(cur), slim(nxt)


def _channel_out(app, ch, info=False):
    cur, nxt = _now_next(app, ch)
    out = {**ch, "now": cur, "next": nxt, "live": ch["id"] in app.streams.by_channel,
           "logo_url": plex_logo_url(app, "", ch), "on_air": app.channel_visible(ch)}
    if ch.get("season"):
        conf = app.cfg.data["holidays"].get(ch["season"]) or {}
        out["in_season"] = seasons.in_window(conf, time.time())
        out["season_dates"] = f"{seasons.friendly_date(conf.get('start', ''))} – {seasons.friendly_date(conf.get('end', ''))}"
    if info:
        out["info"] = describe_content(ch, app.catalog, app.cfg.data["holidays"])
    return out


@router.get("/channels")
async def list_channels(app=Depends(signed_in)):
    chans = sorted(app.cfg.data["channels"], key=lambda c: c["number"])
    return [_channel_out(app, c, info=True) for c in chans]


def _check_number(app, ch):
    clash = next((c for c in app.cfg.data["channels"] if c["number"] == ch["number"] and c["id"] != ch["id"]), None)
    if clash:
        raise HTTPException(400, f"Channel number {ch['number']} is already used by {clash['name']}")


@router.post("/channels")
async def create_channel(body: dict, app=Depends(signed_in)):
    body = {**body, "id": ""}
    body.setdefault("number", next_free_number(app.cfg.data["channels"]))
    body.setdefault("commercials", {"enabled": bool(app.catalog.filler)})
    ch = normalize_channel(body)
    _check_number(app, ch)
    app.cfg.data["channels"].append(ch)
    app.cfg.save()
    _build_soon(app, [ch])
    return _channel_out(app, ch, info=True)


@router.get("/channels/{cid}")
async def get_channel(cid: str, app=Depends(signed_in)):
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    return _channel_out(app, ch, info=True)


@router.put("/channels/{cid}")
async def update_channel(cid: str, body: dict, app=Depends(signed_in)):
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    merged = normalize_channel({**ch, **body, "id": cid, "seed": ch["seed"], "auto_key": ch.get("auto_key", "")})
    _check_number(app, merged)
    idx = app.cfg.data["channels"].index(ch)
    app.cfg.data["channels"][idx] = merged
    app.cfg.save()
    if not merged["enabled"]:
        await app.streams.stop_channel(cid)
    _build_soon(app, [merged])
    return _channel_out(app, merged, info=True)


@router.post("/channels/{cid}/reshuffle")
async def reshuffle_channel(cid: str, app=Depends(signed_in)):
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    ch["seed"] = random.randint(1, 2**31)
    app.cfg.save()
    _build_soon(app, [ch])
    return {"ok": True}


@router.delete("/channels/{cid}")
async def delete_channel(cid: str, app=Depends(signed_in)):
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    _remove_channel(app, ch)
    await app.streams.stop_channel(cid)
    app.cfg.save()
    return {"ok": True}


def _remove_channel(app, ch):
    app.cfg.data["channels"].remove(ch)
    app.schedules.remove(ch["id"])
    key = ch.get("auto_key")
    if key and key not in app.cfg.data["auto"]["dismissed"]:
        app.cfg.data["auto"]["dismissed"].append(key)  # so it isn't added back automatically


@router.post("/channels/preview")
async def preview_channel(body: dict, app=Depends(signed_in)):
    return describe_content(normalize_channel(body), app.catalog, app.cfg.data["holidays"])


@router.get("/channels/{cid}/schedule")
async def channel_schedule(cid: str, hours: int = 12, app=Depends(signed_in)):
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    p = await app.schedules.ensure(ch, time.time() + 6 * 3600)
    now = time.time()
    return [{k: b.get(k) for k in ("s", "e", "title", "sub", "sn", "en", "movie")}
            for b in p.blocks_between(now, now + min(hours, 72) * 3600)]



@router.get("/channels/{cid}/rundown")
async def channel_rundown(cid: str, hours: float = 3, start: float = 0, app=Depends(signed_in)):
    """Everything coming up, item by item: shows, idents, bumpers, commercials, and where the logo and
    "Up next" appear."""
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    now = time.time()
    return {"now": now, "items": await preview_mod.rundown(app, ch, start or now, hours)}


@router.post("/channels/{cid}/watch")
async def channel_watch_preview(cid: str, body: dict, app=Depends(signed_in)):
    """Make a short video of the channel at any time, exactly as live TV will show it."""
    ch = app.channel(cid)
    if not ch:
        raise HTTPException(404, "No such channel")
    start = float(body.get("at") or 0) or time.time()
    length = float(body.get("length") or 75)
    if start < time.time() - 120:
        raise HTTPException(400, "That's already been on")
    if start > time.time() + 7 * 86400:
        raise HTTPException(400, "Pick a time within the next week")
    try:
        path = await preview_mod.render(app, ch, start, length)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"url": f"/api/previews/{os.path.basename(path)}", "at": start, "length": length}


@router.get("/previews/{name}")
async def preview_file(name: str, app=Depends(signed_in)):
    if not re.fullmatch(r"[\w.-]+\.mp4", name):
        raise HTTPException(404, "No such preview")
    path = preview_mod.PREVIEW_DIR / name
    if not path.is_file():
        raise HTTPException(404, "That preview has been cleared away; make it again")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


# ---- the test channel: any channel, minutes long, to check everything in Plex itself -------------

TEST_NAME = "Rerun Test"


def _test_channel(app) -> dict | None:
    return next((c for c in app.cfg.data["channels"] if c.get("quick")), None)


@router.get("/test-channel")
async def test_channel(app=Depends(signed_in)):
    ch = _test_channel(app)
    return {"channel": _channel_out(app, ch) if ch else None,
            "of": (ch.get("quick") or {}).get("of") if ch else None}


@router.post("/test-channel")
async def make_test_channel(body: dict, app=Depends(signed_in)):
    """A copy of a channel whose shows are cut to a minute or so each, so the logo, "Up next", bumpers,
    commercials and idents all come round within a few minutes, in Plex, exactly as they will for real."""
    from .images import logo_ref
    src = app.channel(str(body.get("of") or ""))
    if not src:
        raise HTTPException(404, "No such channel")
    if src.get("plan") and not src.get("rules"):
        raise HTTPException(400, "This channel follows a fixed historical schedule, so it can't be sped up. "
                                 "Use Preview on its page instead.")
    old = _test_channel(app)
    if old:
        _remove_channel(app, old)
        await app.streams.stop_channel(old["id"])
    chans = app.cfg.data["channels"]
    number = 999 if all(c["number"] != 999 for c in chans) else next_free_number(chans, 990)
    ch = normalize_channel({
        "number": number, "name": TEST_NAME, "group": "Testing", "enabled": True,
        "rules": src.get("rules") or [], "exclude": src.get("exclude") or [], "order": "shuffle",
        "slot_minutes": 0, "commercials": src.get("commercials") or {}, "season": src.get("season", ""),
        "holiday_mix": src.get("holiday_mix", True), "style": "plain",
        "logo": src.get("logo") or logo_ref(src, app.catalog),
        "quick": {"of": src["id"], "key": src.get("auto_key") or "", "part": body.get("part") or 0},
    })
    chans.append(ch)
    app.cfg.save()
    await app.schedules.ensure(ch, time.time() + 3 * 3600)
    plex, told = app.plex(), False
    if plex:
        try:
            told = await asyncio.wait_for(plex.reload_guide(), 20) > 0
        except Exception as e:
            log.info("Couldn't ask Plex to reload the guide (%s)", e.__class__.__name__)
    return {"channel": _channel_out(app, ch), "plex_reloaded": told}


@router.delete("/test-channel")
async def remove_test_channel(app=Depends(signed_in)):
    ch = _test_channel(app)
    if ch:
        _remove_channel(app, ch)
        await app.streams.stop_channel(ch["id"])
        app.cfg.save()
    return {"ok": True}

# ---- catalog, guide, status ---------------------------------------------------------------

@router.get("/catalog/facets")
async def catalog_facets(app=Depends(signed_in)):
    holidays = [{"key": k, "name": p["name"], "icon": p["icon"]} for k, p in seasons.PRESETS.items()]
    return {**app.catalog.facets(), "summary": app.catalog.summary(), "break_styles": BREAK_STYLES,
            "holidays": holidays}


@router.get("/logos")
async def logo_choices(app=Depends(signed_in)):
    """Official logos you can put on any channel."""
    names = sorted(set(TMDB_LOGOS) | set(WIKIPEDIA_ARTICLES), key=str.lower)
    return [{"name": n, "ref": network_logo(n), "url": f"/netlogo/{quote(n, safe='')}.png?v={LOGO_V}"} for n in names]


# ---- networks: pick which ones become channels ---------------------------------------------

def _network_channel(app, name: str):
    return next((c for c in app.cfg.data["channels"] if c.get("auto_key") == f"network:{name}"), None)


@router.get("/networks")
async def networks(app=Depends(signed_in)):
    d = app.cfg.data
    out = []
    for n in library_networks(app.catalog, d["auto"].get("min_shows", 2)):
        ch = _network_channel(app, n["name"])
        out.append({**n, "channel": ch["id"] if ch else "", "number": ch["number"] if ch else None,
                    "logo_url": f"/netlogo/{quote(n['name'], safe='')}.png?v={LOGO_V}",
                    "dismissed": f"network:{n['name']}" in d["auto"]["dismissed"]})
    return {"networks": out, "auto": d["auto"]["networks"], "min_shows": d["auto"].get("min_shows", 2)}


@router.post("/networks/{name}/channel")
async def network_on(name: str, app=Depends(signed_in)):
    d = app.cfg.data
    if _network_channel(app, name):
        return {"ok": True}
    net = next((n for n in library_networks(app.catalog, 1) if n["name"] == name), None)
    if not net:
        raise HTTPException(404, "That network isn't in your library")
    sugg = {"key": f"network:{name}", "name": name, "group": net["group"], "rules": network_rules(net, app.catalog),
            "number_start": net["number_start"], "logo": network_logo(name, app.catalog)}
    ch = channel_from_suggestion(sugg, d["channels"], bool(app.catalog.filler))
    d["channels"].append(ch)
    d["auto"]["dismissed"] = [k for k in d["auto"]["dismissed"] if k != sugg["key"]]
    app.cfg.save()
    _build_soon(app, [ch])
    return {"ok": True, "channel": ch["id"], "number": ch["number"]}


@router.delete("/networks/{name}/channel")
async def network_off(name: str, app=Depends(signed_in)):
    ch = _network_channel(app, name)
    key = f"network:{name}"
    if ch:
        _remove_channel(app, ch)
        await app.streams.stop_channel(ch["id"])
    elif key not in app.cfg.data["auto"]["dismissed"]:
        app.cfg.data["auto"]["dismissed"].append(key)
    app.cfg.save()
    return {"ok": True}


@router.post("/auto/restore")
async def auto_restore(app=Depends(signed_in)):
    app.cfg.data["auto"]["dismissed"] = []
    made = sync_auto(app.cfg.data, app.catalog)
    app.cfg.save()
    _build_soon(app, made)
    return {"created": len(made)}


# ---- holidays -----------------------------------------------------------------------------

@router.get("/holidays")
async def holidays(app=Depends(signed_in)):
    conf_all = app.cfg.data["holidays"]
    now = time.time()
    out = []
    for key, p in seasons.PRESETS.items():
        conf = conf_all.get(key) or seasons.DEFAULT_HOLIDAYS[key]
        found = seasons.matches(app.catalog, key, conf)
        ch = next((c for c in app.cfg.data["channels"] if c.get("season") == key), None)
        mine = [{"id": c["id"], "name": c["name"], "number": c["number"], "kind": _holiday_kind(c)}
                for c in app.cfg.data["channels"] if c.get("season") == key]
        samples = sorted({app.catalog.items[e]["title"] for e in list(found["episodes"])[:40] if e in app.catalog.items})
        samples += sorted(app.catalog.movies[m]["title"] for m in list(found["movies"])[:10])
        out.append({
            "key": key, "name": p["name"], "icon": p["icon"], "enabled": conf.get("enabled", True),
            "start": conf.get("start"), "end": conf.get("end"), "mix": conf.get("mix", True),
            "words": conf.get("words", []), "default_start": p["start"], "default_end": p["end"],
            "when": f"{seasons.friendly_date(conf.get('start', ''))} – {seasons.friendly_date(conf.get('end', ''))}",
            "episodes": len(found["episodes"]), "movies": len(found["movies"]), "samples": samples[:12],
            "on_now": seasons.in_window(conf, now), "channel": ch["id"] if ch else "", "channels": mine,
            "logo_url": f"/holidaylogo/{key}.png?v={LOGO_V}",
            "starts_in": 0 if seasons.in_window(conf, now) else (seasons.next_start(conf, now) or now + 400 * 86400) - now,
        })
    out.sort(key=lambda x: (not x["enabled"], x["starts_in"]))  # what's on now, then what's coming up next
    return {"holidays": out, "festive_logos": app.cfg.data.get("festive_logos", False)}


@router.put("/holidays/{key}")
async def holiday_update(key: str, body: dict, app=Depends(signed_in)):
    if key not in seasons.PRESETS:
        raise HTTPException(404, "No such holiday")
    conf = app.cfg.data["holidays"].setdefault(key, dict(seasons.DEFAULT_HOLIDAYS[key]))
    for field in ("start", "end"):
        if field in body:
            if not seasons.valid_date(str(body[field])):
                raise HTTPException(400, "Dates look like 12-01 (month-day)")
            conf[field] = str(body[field])
    if "enabled" in body:
        conf["enabled"] = bool(body["enabled"])
    if "mix" in body:
        conf["mix"] = bool(body["mix"])
    if "words" in body:
        conf["words"] = [str(w).strip()[:40] for w in body["words"] or [] if str(w).strip()][:40]
    app.cfg.save()
    return {"ok": True}


def _holiday_kind(ch: dict) -> str:
    """ "tv", "movies" or "both" — what a holiday channel plays."""
    kinds = {r.get("media") for r in ch.get("rules", []) if r.get("type") == "holiday"}
    return "tv" if kinds == {"episodes"} else "movies" if kinds == {"movies"} else "both"


@router.post("/holidays/{key}/channel")
async def holiday_channel(key: str, body: dict | None = None, app=Depends(signed_in)):
    """A holiday channel: "tv" (episodes only), "movies" (movies only) or "both"."""
    if key not in seasons.PRESETS:
        raise HTTPException(404, "No such holiday")
    kind = (body or {}).get("kind", "both")
    if kind not in ("tv", "movies", "both"):
        raise HTTPException(400, "kind is tv, movies or both")
    d = app.cfg.data
    existing = next((c for c in d["channels"] if c.get("season") == key and _holiday_kind(c) == kind), None)
    if existing:
        return {"ok": True, "channel": existing["id"], "number": existing["number"]}
    p = seasons.PRESETS[key]
    name = {"tv": f"{p['name']} TV", "movies": f"{p['name']} Movies"}.get(
        kind, HOLIDAY_CHANNEL_NAMES.get(key, f"{p['name']} TV"))
    rule = {"type": "holiday", "key": key, **({"media": "episodes"} if kind == "tv" else
                                              {"media": "movies"} if kind == "movies" else {})}
    sugg = {"key": f"holiday:{key}" + ("" if kind == "both" else f":{kind}"), "name": name, "group": "Holidays",
            "rules": [rule], "number_start": HOLIDAY_START, "season": key}
    ch = channel_from_suggestion(sugg, d["channels"], bool(app.catalog.filler))
    d["channels"].append(ch)
    d["auto"]["dismissed"] = [k for k in d["auto"]["dismissed"] if k != sugg["key"]]
    app.cfg.save()
    _build_soon(app, [ch])
    return {"ok": True, "channel": ch["id"], "number": ch["number"]}


# ---- ABC Family's 25 Days of Christmas, and the 2000s Christmas channel ----------------------

def _plan_channel(app) -> dict | None:
    return next((c for c in app.cfg.data["channels"] if (c.get("plan") or {}).get("name") == "abcfamily25"), None)


def _year(year) -> int:
    y = int(year or 2008)
    if y not in xmasdata.SCHEDULES:
        raise HTTPException(400, f"Rerun has the 25 Days schedule for {', '.join(map(str, xmasdata.SCHEDULES))}")
    return y


@router.get("/25days")
async def twentyfive(year: int = 0, app=Depends(signed_in)):
    ch = _plan_channel(app)
    year = _year(year or (ch["plan"]["year"] if ch else 2008))
    wants = xmas.plan_wants(app.catalog, year)
    now = time.time()
    season = time.localtime(now).tm_year
    slots = xmas.plan_slots(year, season)
    on_now = next((s for s in slots if s["s"] <= now < s["e"]), None)
    up_next = next((s for s in slots if s["s"] > now), None)
    days = {}
    for d, lst in sorted(xmasdata.SCHEDULES[year].items()):
        days[d] = [{"start": s, "end": e, "key": k, "title": xmasdata.TITLES[k][0]} for s, e, k in lst]
    return {
        "years": list(xmasdata.SCHEDULES), "year": year, "source": xmasdata.SOURCES.get(year, ""),
        "wants": wants, "have": sum(1 for w in wants if w["have"]), "total": len(wants),
        "days": days, "scanned_at": app.catalog.scanned_at, "scan": app.scanner.progress,
        "channel": {"id": ch["id"], "number": ch["number"], "name": ch["name"], "year": ch["plan"]["year"],
                    "stand_ins": ch["plan"].get("stand_ins", True), "exact_breaks": ch["plan"].get("exact_breaks", True)}
        if ch else None,
        "on_now": {"title": xmasdata.TITLES[on_now["key"]][0], "e": on_now["e"]} if on_now else None,
        "up_next": {"title": xmasdata.TITLES[up_next["key"]][0], "s": up_next["s"]} if up_next else None,
        "finder": app.finder.summary_25days(),
    }


@router.get("/25days/need.txt")
async def twentyfive_need(year: int = 2008, app=Depends(signed_in)):
    """The titles you still need, as a plain list to copy or print."""
    year = _year(year)
    need = [w for w in xmas.plan_wants(app.catalog, year) if not w["have"]]
    lines = [f"ABC Family's 25 Days of Christmas {year} — {len(need)} titles to get", ""]
    for kind, label in (("movie", "Movies"), ("special", "TV specials")):
        group = [w for w in need if w["kind"] == kind]
        if group:
            lines.append(f"{label}:")
            lines += [f"  {w['title']} ({w['year']})" + (f" — airs {w['airings']} times" if w["airings"] > 1 else "")
                      for w in sorted(group, key=lambda w: w["title"].lower())]
            lines.append("")
    return PlainTextResponse("\n".join(lines))


@router.post("/25days/channel")
async def twentyfive_channel(body: dict | None = None, app=Depends(signed_in)):
    """Make (or update) the 25 Days of Christmas channel."""
    body = body or {}
    year = _year(body.get("year"))
    plan = {"name": "abcfamily25", "year": year, "stand_ins": bool(body.get("stand_ins", True)),
            "exact_breaks": bool(body.get("exact_breaks", True))}
    d = app.cfg.data
    ch = _plan_channel(app)
    if ch:
        merged = normalize_channel({**ch, "plan": plan})
        d["channels"][d["channels"].index(ch)] = merged
        app.cfg.save()
        _build_soon(app, [merged])
        return {"ok": True, "channel": merged["id"], "number": merged["number"]}
    # Between listings ABC Family ran its regular shows: yours if you have them, else Christmas episodes.
    rules = [{"type": "filter", "media": "shows", "networks": ["ABC Family", "Fox Family", "Freeform"]},
             {"type": "holiday", "key": "christmas", "media": "episodes"}]
    sugg = {"key": "plan:abcfamily25", "name": "ABC Family 25 Days of Christmas", "group": "Holidays",
            "rules": rules, "number_start": HOLIDAY_START, "season": "christmas",
            "logo": network_logo("ABC Family", app.catalog)}
    ch = channel_from_suggestion(sugg, d["channels"], bool(app.catalog.filler))
    ch = normalize_channel({**ch, "plan": plan})
    d["channels"].append(ch)
    d["auto"]["dismissed"] = [k for k in d["auto"]["dismissed"] if k != sugg["key"]]
    app.cfg.save()
    _build_soon(app, [ch])
    return {"ok": True, "channel": ch["id"], "number": ch["number"]}


@router.post("/25days/finder")
async def twentyfive_finder(body: dict | None = None, app=Depends(signed_in)):
    body = body or {}
    try:
        gb = min(max(float(body.get("budget_gb", 4)), 0.2), 50)
        app.finder.start_25days(_year(body.get("year")), int(gb * 1e9), int(body.get("min_height") or 0),
                                bool(body.get("nearby", True)), bool(body.get("youtube", True)))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return app.finder.summary_25days()


# ---- classic channel lineups (Boomerang) ---------------------------------------------------

def _lineup(key: str) -> dict:
    if key not in lineups_mod.LINEUPS:
        raise HTTPException(404, "No such channel lineup")
    return lineups_mod.LINEUPS[key]


def _lineup_channel(app, key: str) -> dict | None:
    return next((c for c in app.cfg.data["channels"] if c.get("auto_key") == f"lineup:{key}"), None)


@router.get("/lineups")
async def lineups_list(app=Depends(signed_in)):
    out = []
    for key, ln in lineups_mod.LINEUPS.items():
        w = lineups_mod.wants(app.catalog, key)
        ch = _lineup_channel(app, key)
        out.append({"key": key, "name": ln["name"], "years": ln["years"], "about": ln["about"],
                    "have": sum(1 for x in w if x["have"]), "total": len(w),
                    "logo_url": f"/netlogo/{quote(ln['network'])}.png",
                    "channel": {"id": ch["id"], "number": ch["number"]} if ch else None})
    return {"lineups": out}


@router.get("/lineups/{key}")
async def lineup(key: str, app=Depends(signed_in)):
    ln = _lineup(key)
    w = lineups_mod.wants(app.catalog, key)
    ch = _lineup_channel(app, key)
    tag = ln["ads"]
    ads = [f for f in app.catalog.all_filler() if tag in (f.get("tags") or []) and not f.get("dup")]
    return {"key": key, "name": ln["name"], "years": ln["years"], "about": ln["about"], "wants": w,
            "have": sum(1 for x in w if x["have"]), "total": len(w),
            "staples": {"have": sum(1 for x in w if x["have"] and x["tier"] == "staple"),
                        "total": sum(1 for x in w if x["tier"] == "staple")},
            "logo_url": f"/netlogo/{quote(ln['network'])}.png",
            "commercials": sum(1 for f in ads if "bumper" not in f["tags"]),
            "bumpers": sum(1 for f in ads if "bumper" in f["tags"]),
            "channel": {"id": ch["id"], "number": ch["number"], "name": ch["name"]} if ch else None,
            "scanned_at": app.catalog.scanned_at, "scan": app.scanner.progress, "finder": app.finder.progress}


@router.get("/lineups/{key}/need.txt")
async def lineup_need(key: str, app=Depends(signed_in)):
    ln = _lineup(key)
    need = [w for w in lineups_mod.wants(app.catalog, key) if not w["have"]]
    lines = [f"{ln['name']} ({ln['years']}) — {len(need)} shows to get", ""]
    for tier, label in (("staple", "Aired all the time"), ("regular", "Aired often"), ("occasional", "Aired now and then")):
        group = [w for w in need if w["tier"] == tier]
        if group:
            lines.append(f"{label}:")
            lines += [f"  {w['title']} ({w['year']})" for w in sorted(group, key=lambda w: w["title"].lower())]
            lines.append("")
    return PlainTextResponse("\n".join(lines))


@router.post("/lineups/{key}/channel")
async def lineup_channel(key: str, app=Depends(signed_in)):
    ln = _lineup(key)
    d = app.cfg.data
    ch = _lineup_channel(app, key)
    if ch:
        return {"ok": True, "channel": ch["id"], "number": ch["number"]}
    _fam, category = family_for(ln["network"])
    group, start = CATEGORIES.get(category, CATEGORIES["other"])
    sugg = {"key": f"lineup:{key}", "name": ln["name"], "group": group, "rules": [{"type": "lineup", "key": key}],
            "number_start": start, "logo": network_logo(ln["network"], app.catalog)}
    ch = channel_from_suggestion(sugg, d["channels"], bool(app.catalog.filler))
    d["channels"].append(ch)
    d["auto"]["dismissed"] = [k for k in d["auto"]["dismissed"] if k != sugg["key"]]
    app.cfg.save()
    _build_soon(app, [ch])
    return {"ok": True, "channel": ch["id"], "number": ch["number"]}


@router.post("/lineups/{key}/finder")
async def lineup_finder(key: str, body: dict | None = None, app=Depends(signed_in)):
    """Bumpers, idents and commercials for a classic channel (Boomerang's own)."""
    ln = _lineup(key)
    body = body or {}
    try:
        gb = min(max(float(body.get("budget_gb", 3)), 0.2), 50)
        app.finder.start([ln["ads"]], ["2000s"], int(gb * 1e9), ["christmas"] if body.get("christmas") else [],
                         int(body.get("min_height") or 0), True)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/christmas")
async def christmas(app=Depends(signed_in)):
    """The 2000s Christmas essentials (have / need) and the Christmas channels' style."""
    ess = xmas.essentials(app.catalog)
    chans = [{"id": c["id"], "name": c["name"], "number": c["number"], "style": channels_mod.christmas_style(c)}
             for c in app.cfg.data["channels"] if c.get("season") == "christmas" and not c.get("plan")]
    return {"essentials": ess, "have": sum(1 for e in ess if e["have"]), "total": len(ess), "channels": chans,
            "has_story": bool(xmas.christmas_story(app.catalog)),
            "dayparts": [{"from": a % 24, "to": b % 24, "name": n} for a, b, n, _m in xmas.DAYPARTS]}


@router.get("/catalog/search")
async def catalog_search(q: str = "", app=Depends(signed_in)):
    q = q.strip().lower()
    if len(q) < 2:
        return {"shows": [], "movies": []}
    shows = [{"id": s["id"], "title": s["title"], "year": s.get("year"), "network": s.get("network"),
              "episodes": len(app.catalog.episodes.get(s["id"], []))}
             for s in app.catalog.shows.values() if q in s["title"].lower()]
    movies = [{"id": m["id"], "title": m["title"], "year": m.get("year")}
              for m in app.catalog.movies.values() if q in m["title"].lower()]
    return {"shows": sorted(shows, key=lambda x: x["title"])[:40], "movies": sorted(movies, key=lambda x: x["title"])[:40]}


@router.get("/catalog/names")
async def catalog_names(ids: str = "", app=Depends(signed_in)):
    out = {}
    for i in ids.split(",")[:500]:
        x = app.catalog.shows.get(i) or app.catalog.movies.get(i)
        if x:
            out[i] = x["title"]
    return out


@router.get("/guide")
async def guide(hours: int = 6, app=Depends(signed_in)):
    now = time.time()
    start = now - (now % 1800)
    end = start + max(1, min(hours, 24)) * 3600
    out = []
    for ch in sorted(app.cfg.data["channels"], key=lambda c: c["number"]):
        if not ch.get("enabled", True):
            continue
        p = app.schedules.get(ch["id"])
        out.append({"id": ch["id"], "number": ch["number"], "name": ch["name"],
                    "blocks": [{**{k: b.get(k) for k in ("s", "e", "title", "sub", "movie")},
                                "card": not b.get("cats") and not b.get("img")}  # "returns December 1" and the like
                               for b in p.blocks_between(start, end)]})
    return {"start": start, "end": end, "channels": out}


def _lan_ip() -> str | None:
    """This machine's address on the home network (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


@router.get("/connect")
async def connect_info(request: Request, app=Depends(signed_in)):
    base = _base_url(request)
    note = ""
    host = request.url.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        lan = _lan_ip()
        if lan:
            base = f"{request.url.scheme}://{lan}:{app.port}"
            note = (f"You opened Rerun on the server itself, so these addresses use the server's network address "
                    f"({lan}). If Plex can't reach it, try 127.0.0.1:{app.port} instead.")
    return {"base": base, "tuner": base, "xmltv": f"{base}/xmltv.xml", "m3u": f"{base}/iptv.m3u", "note": note,
            "channels": len([c for c in app.cfg.data["channels"] if c.get("enabled", True)])}


# ---- the Commercials page ------------------------------------------------------------------

def _ad_source(app, f: dict) -> str:
    path = os.path.realpath(f.get("file") or "")
    own = os.path.realpath(str(OWN_COMMERCIALS))
    if path.startswith(own + os.sep):
        return "found"
    return "folder" if f.get("file") else "plex"


@router.get("/commercials")
async def commercials_list(app=Depends(signed_in)):
    hidden = set(app.cfg.data.get("hidden_filler") or [])
    items = [{"id": f["id"], "title": f.get("title", ""), "dur": round(f["dur"], 1),
              "tags": f.get("tags", []), "source": _ad_source(app, f), "hidden": f["id"] in hidden,
              "h": int(f.get("h") or 0), "q": f.get("q") or None}
             for f in app.catalog.all_filler() if not f.get("dup")]  # a better copy of a duplicate airs
    items.sort(key=lambda x: (x["title"].lower(), x["id"]))
    tags = Counter(t for x in items for t in x["tags"])
    on_air = [x for x in items if not x["hidden"]]
    return {"items": items, "tags": [{"name": k, "count": n} for k, n in tags.most_common()],
            "hidden": len(hidden & {x["id"] for x in items}), "labels": _tag_labels(tags),
            "groups": {"network": [k for k in archive_mod.PROFILES if k in tags] + (["25days"] if "25days" in tags else []),
                       "era": [e for e in archive_mod.ERAS if e in tags],
                       "season": [s for s in ("christmas", "halloween", "december", "october") if s in tags],
                       "bumpers": tags.get("bumper", 0)},
            "stats": {"on_air": len(on_air), "seconds": round(sum(x["dur"] for x in on_air)),
                      "dupes": sum(1 for f in app.catalog.all_filler() if f.get("dup"))}}


def _tag_labels(tags) -> dict:
    """Friendly names for commercial tags: "nick" -> "Nickelodeon", "y2008" -> "December 2008"."""
    out = {}
    for t in tags:
        if t in archive_mod.PROFILES:
            out[t] = archive_mod.PROFILES[t][0].replace(" / Fox Family", "")
        elif t == "25days":
            out[t] = "25 Days of Christmas"
        elif t == "bumper":
            out[t] = "Bumper or ident"
        elif len(t) == 5 and t[0] == "y" and t[1:].isdigit():
            out[t] = f"December {t[1:]}"
        elif t in ("october", "december"):
            out[t] = f"{t.title()} tapes"
        elif t in archive_mod.ERAS:
            out[t] = t
        else:
            out[t] = t[:1].upper() + t[1:]
    return out


def _filler(app, fid: str) -> dict:
    f = app.catalog.items.get(fid)
    if not f or fid not in {x["id"] for x in app.catalog.all_filler()}:
        raise HTTPException(404, "No such commercial")
    src = app.media_input({"f": f.get("file") or "", "p": f.get("part") or ""})
    if not src:
        raise HTTPException(404, "That commercial's file can't be found")
    return {**f, "_src": src}


@router.get("/commercials/{fid}/thumb.jpg")
async def commercial_thumb(fid: str, app=Depends(signed_in)):
    f = _filler(app, fid)
    path = DATA_DIR / "thumbs" / f"c2-{''.join(ch for ch in fid if ch.isalnum())}.jpg"
    if not path.exists():
        at = float(f.get("in") or 0) + min(f["dur"] * 0.35, 8)  # past the fade in, before the end card
        src = f["_src"] if f["_src"].startswith("http") else "file:" + f["_src"]
        proc = await asyncio.create_subprocess_exec(
            app.ffmpeg.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", src, "-frames:v", "1",
            "-vf", "scale=320:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        data, _ = await asyncio.wait_for(proc.communicate(), 30)
        if not data:
            raise HTTPException(404, "Couldn't make a picture")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return Response(path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/commercials/{fid}/preview.mp4")
async def commercial_preview(fid: str, app=Depends(signed_in)):
    """The commercial, made playable in a web browser."""
    f = _filler(app, fid)
    src = f["_src"] if f["_src"].startswith("http") else "file:" + f["_src"]
    proc = await asyncio.create_subprocess_exec(
        app.ffmpeg.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{float(f.get('in') or 0):.2f}", "-i", src,
        "-t", f"{f['dur']:.2f}", "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def body():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return StreamingResponse(body(), media_type="video/mp4")


@router.post("/commercials/hide")
async def commercials_hide(body: dict, app=Depends(signed_in)):
    """Take commercials off the air (or put them back). Nothing is deleted."""
    ids = {str(i) for i in body.get("ids") or []}
    hidden = set(app.cfg.data.get("hidden_filler") or [])
    hidden = hidden | ids if body.get("hidden", True) else hidden - ids
    app.cfg.data["hidden_filler"] = sorted(hidden)
    app.cfg.save()
    channels_mod.hidden_filler = hidden
    return {"ok": True, "hidden": len(hidden)}


# ---- YouTube ----------------------------------------------------------------------------------

@router.get("/youtube")
async def youtube_status(app=Depends(signed_in)):
    return {"available": youtube_mod.available(), "version": youtube_mod.version(), "js": youtube_mod.js_runtime(),
            "networks": [{"key": k, "label": v[0].replace(" / Fox Family", "")} for k, v in archive_mod.PROFILES.items()],
            "eras": list(archive_mod.ERAS)}


def _need_youtube():
    if not youtube_mod.available():
        raise HTTPException(400, "The YouTube downloader isn't installed. Run “rerun update” on the server.")


@router.get("/youtube/search")
async def youtube_search(q: str = "", app=Depends(signed_in)):
    _need_youtube()
    q = q.strip()[:200]
    if len(q) < 2:
        return {"results": []}
    try:
        return {"results": await asyncio.to_thread(youtube_mod.search, q, 24)}
    except Exception as e:
        raise HTTPException(502, f"YouTube search didn't work: {str(e)[:200]}")


@router.get("/youtube/thumb/{vid}.jpg")
async def youtube_thumb(vid: str, app=Depends(signed_in)):
    """A video's thumbnail, fetched by Rerun (the page only loads pictures from Rerun itself)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        raise HTTPException(404, "No such video")
    path = DATA_DIR / "thumbs" / f"yt-{vid}.jpg"
    if not path.exists():
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
        except httpx.HTTPError:
            raise HTTPException(404, "No thumbnail")
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise HTTPException(404, "No thumbnail")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return Response(path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/youtube/links")
async def youtube_links(body: dict, app=Depends(signed_in)):
    """The videos behind pasted links (a playlist becomes its videos)."""
    _need_youtube()
    out, bad = [], []
    for url in [u.strip() for u in str(body.get("text") or "").split() if u.strip()][:20]:
        if not url.startswith("http"):
            continue
        try:
            out += await asyncio.to_thread(youtube_mod.expand, url, 200)
        except Exception as e:
            bad.append(f"{url[:60]}: {str(e)[:120]}")
    return {"results": out[:200], "problems": bad}


@router.post("/youtube")
async def youtube_download(body: dict, app=Depends(signed_in)):
    _need_youtube()
    try:
        app.finder.start_youtube(list(body.get("items") or []), str(body.get("kind") or ""),
                                 str(body.get("network") or ""), str(body.get("era") or ""),
                                 str(body.get("holiday") or ""), str(body.get("name") or ""))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/finder")
async def finder_status(app=Depends(signed_in)):
    return app.finder.summary()


@router.post("/finder")
async def finder_start(body: dict, app=Depends(signed_in)):
    try:
        gb = min(max(float(body.get("budget_gb", 1.5)), 0.2), 50)
        app.finder.start(list(body.get("networks") or []), list(body.get("eras") or []), int(gb * 1e9),
                         list(body.get("holidays") or []), int(body.get("min_height") or 0),
                         bool(body.get("airings", True)), bool(body.get("youtube", False)))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return app.finder.summary()


@router.post("/finder/stop")
async def finder_stop(app=Depends(signed_in)):
    app.finder.stop = True
    return {"ok": True}


@router.delete("/finder/{key}")
async def finder_remove(key: str, app=Depends(signed_in)):
    if app.finder.task and not app.finder.task.done():
        raise HTTPException(400, "Wait for the search to finish first")
    app.finder.remove(key)
    return app.finder.summary()


@router.get("/report")
async def report(app=Depends(signed_in)):
    return PlainTextResponse(await app.diag.report(app))


@router.get("/status")
async def status(app=Depends(signed_in)):
    warnings = []
    if not app.ffmpeg.available:
        warnings.append("ffmpeg wasn't found, so channels can't play.")
    if app.ffmpeg.encoder_status.get("message"):
        warnings.append(app.ffmpeg.encoder_status["message"])
    if app.scanner.progress.get("error"):
        warnings.append(app.scanner.progress["error"])
    if not app.cfg.data["security"].get("lan_only", True):
        warnings.append("Rerun is answering devices outside your home network. Turn on “Home network only” "
                        "in Settings unless you meant to do this.")
    return {
        "version": __version__, "uptime": int(time.time() - app.started), "ffmpeg": app.ffmpeg.version,
        "encoder": app.ffmpeg.encoder_status, "streams": app.streams.status(), "catalog": app.catalog.summary(),
        "scan": app.scanner.progress, "warnings": warnings, "logs": list(app.logs.lines)[-200:],
    }
