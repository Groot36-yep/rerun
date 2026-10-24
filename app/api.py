"""The JSON API behind the web page. Everything here needs you to be signed in."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import random
import socket
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from urllib.parse import quote

from . import __version__, seasons
from .guide import logo_url as plex_logo_url
from .channels import (
    BREAK_STYLES, HOLIDAY_CHANNEL_NAMES, channel_from_suggestion, describe_content, library_networks,
    network_logo, network_rules, next_free_number, normalize_channel, suggest, sync_auto,
)
from .images import VERSION as LOGO_V
from .logos_table import TMDB_LOGOS, WIKIPEDIA_ARTICLES
from .networks import HOLIDAY_START
from .folders import VIDEO_EXT
from .ffmpeg import RESOLUTIONS
from .metadata import test_tmdb_key
from .plex import PlexClient, PlexError, check_pin, create_pin, find_servers, pick_connection
from .security import hash_password, verify_password

COOKIE = "rerun_session"
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
                if not app.path_allowed(path):
                    raise HTTPException(400, f"{s['path']} isn't inside your media folders")
                out.append({"type": "folder", "path": path})
        clean[kind] = out
    app.cfg.data["sources"] = clean
    app.cfg.save()
    return {"ok": True}


@router.get("/folders")
async def folders(path: str = "", app=Depends(signed_in)):
    if not path:
        return {"path": "", "parent": None, "videos": 0,
                "dirs": [{"name": r, "path": r} for r in app.media_roots if os.path.isdir(r)]}
    real = os.path.realpath(path)
    if not app.path_allowed(real) or not os.path.isdir(real):
        raise HTTPException(403, "That folder isn't available")
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
    return {"path": real, "parent": parent if app.path_allowed(parent) and real not in app.media_roots else "",
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
        samples = sorted({app.catalog.items[e]["title"] for e in list(found["episodes"])[:40] if e in app.catalog.items})
        samples += sorted(app.catalog.movies[m]["title"] for m in list(found["movies"])[:10])
        out.append({
            "key": key, "name": p["name"], "icon": p["icon"], "enabled": conf.get("enabled", True),
            "start": conf.get("start"), "end": conf.get("end"), "mix": conf.get("mix", True),
            "words": conf.get("words", []), "default_start": p["start"], "default_end": p["end"],
            "when": f"{seasons.friendly_date(conf.get('start', ''))} – {seasons.friendly_date(conf.get('end', ''))}",
            "episodes": len(found["episodes"]), "movies": len(found["movies"]), "samples": samples[:12],
            "on_now": seasons.in_window(conf, now), "channel": ch["id"] if ch else "",
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


@router.post("/holidays/{key}/channel")
async def holiday_channel(key: str, app=Depends(signed_in)):
    if key not in seasons.PRESETS:
        raise HTTPException(404, "No such holiday")
    d = app.cfg.data
    existing = next((c for c in d["channels"] if c.get("season") == key), None)
    if existing:
        return {"ok": True, "channel": existing["id"]}
    p = seasons.PRESETS[key]
    sugg = {"key": f"holiday:{key}", "name": HOLIDAY_CHANNEL_NAMES.get(key, f"{p['name']} TV"), "group": "Holidays",
            "rules": [{"type": "holiday", "key": key}], "number_start": HOLIDAY_START, "season": key}
    ch = channel_from_suggestion(sugg, d["channels"], bool(app.catalog.filler))
    d["channels"].append(ch)
    d["auto"]["dismissed"] = [k for k in d["auto"]["dismissed"] if k != sugg["key"]]
    app.cfg.save()
    _build_soon(app, [ch])
    return {"ok": True, "channel": ch["id"], "number": ch["number"]}


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
                    "blocks": [{k: b.get(k) for k in ("s", "e", "title", "sub", "movie")}
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


@router.get("/report")
async def report(app=Depends(signed_in)):
    return PlainTextResponse(app.diag.report(app))


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
