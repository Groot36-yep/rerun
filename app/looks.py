"""The On-air look page: for each network, which "Up next" (one of Rerun's drawn styles, or a real
template you have) and which trivia background it uses, and finding real ones on YouTube.

Choices live in the config under "looks": {tag: {"up_next": "auto" | "style:<name>" | "file:<name>",
"trivia_bg": "" | "none" | "file:<name>"}}. "auto" (the default) is a template named after the show or
network if there is one, otherwise the network's own drawn style.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess

from . import archive, images, trivia
from .store import DATA_DIR

UPNEXT_DIR = DATA_DIR / "bugs" / "custom" / "upnext"
TRIVIA_DIR = trivia.BG_DIR
THUMBS = DATA_DIR / "bugs" / "thumbs"
FOLDERS = {"upnext": UPNEXT_DIR, "trivia": TRIVIA_DIR}

# Rerun's drawn "Up next" styles, as the page lists them
STYLES = [("nick", "Nickelodeon: orange plaid bar"), ("nick_splat", "Nickelodeon: splat"),
          ("cn", "Cartoon Network: checkerboard"), ("disney", "Disney Channel: 2000s"),
          ("disney_glow", "Disney Channel: glow"), ("boomerang", "Boomerang: starburst"),
          ("25days", "25 Days of Christmas"), ("plain", "Plain, in the logo's colour")]
STYLE_KEYS = {k for k, _ in STYLES}

# what to search YouTube for, per kind
SEARCH = {"upnext": "{label} up next green screen template", "trivia": "{label} bumper 2000s"}


def choice(cfg: dict, tag: str | None, what: str) -> str:
    return ((cfg.get("looks") or {}).get(tag or "") or {}).get(what, "")


def upnext_override(cfg: dict, tag: str | None) -> tuple[str | None, str | None]:
    """(template file, drawn style) chosen for a network's "Up next", either or neither."""
    c = choice(cfg, tag, "up_next")
    if c.startswith("file:"):
        p = UPNEXT_DIR / c[5:]
        return (str(p) if p.is_file() else None), None
    if c.startswith("style:") and c[6:] in STYLE_KEYS:
        return None, ("" if c[6:] == "plain" else c[6:])
    return None, None


def networks(cfg: dict) -> list[dict]:
    """The networks with a look to choose: those with a drawn style, and any with a channel of their own."""
    from urllib.parse import quote
    from .lineups import ads_tag
    wanted = {"nick", "nickjr", "nicktoons", "cn", "boomerang", "disney", "playhouse", "toondisney", "abcfamily"}
    on_air = set()
    for ch in cfg.get("channels") or []:
        key = ch.get("auto_key") or ""
        t = archive.tag_for_network(key[8:]) if key.startswith("network:") else (
            ads_tag(key[7:]) if key.startswith("lineup:") else None)
        if t:
            on_air.add(t)
    out = []
    for tag in sorted(wanted | on_air, key=lambda t: (t not in on_air, archive.PROFILES.get(t, (t,))[0].lower())):
        if tag not in archive.PROFILES:
            continue
        label, _must, _never, chans, _q = archive.PROFILES[tag]
        out.append({"tag": tag, "label": label, "style": images.theme_for(tag) or "plain", "has_channel": tag in on_air,
                    "logo_url": f"/netlogo/{quote(chans[0] if chans else label, safe='')}.png",
                    "up_next": choice(cfg, tag, "up_next") or "auto",
                    "trivia_bg": choice(cfg, tag, "trivia_bg") or ""})
    return out


def files(kind: str) -> list[dict]:
    folder = FOLDERS[kind]
    if not folder.is_dir():
        return []
    ext = images.TEMPLATE_EXT if kind == "upnext" else trivia.BG_EXT
    return [{"name": f.name, "video": f.suffix.lower() in images.VIDEO_EXT_T, "bytes": f.stat().st_size}
            for f in sorted(folder.iterdir(), key=lambda f: f.name.lower())
            if f.is_file() and f.suffix.lower() in ext]


def safe_name(name: str) -> str:
    return re.sub(r"[^\w .,'&()!-]+", "", name).strip()[:90] or "template"


def thumb(kind: str, name: str, ffmpeg: str, ffprobe: str = "ffprobe") -> str | None:
    """A small picture of a template or background (a frame from a video), made once."""
    src = FOLDERS[kind] / name
    if not src.is_file() or os.sep in name or "/" in name:
        return None
    THUMBS.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{kind}:{name}:{int(src.stat().st_mtime)}".encode()).hexdigest()[:16]
    out = THUMBS / f"{key}.jpg"
    if out.exists():
        return str(out)
    if src.suffix.lower() in images.VIDEO_EXT_T:
        dur = 0.0
        try:
            probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(src)], capture_output=True, text=True, timeout=20).stdout.strip()
            dur = float(probe or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        res = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{dur * 0.45:.2f}", "-i", str(src), "-frames:v", "1",
                              "-vf", "scale=480:-2", "-y", str(out)], capture_output=True, timeout=60)
        return str(out) if res.returncode == 0 and out.exists() else None
    from PIL import Image
    im = Image.open(src).convert("RGB")
    im.thumbnail((480, 270))
    im.save(out, "JPEG", quality=85)
    return str(out)


def style_preview(style: str, label: str) -> bytes:
    """What a drawn style looks like, over a plain picture, for the page."""
    from PIL import Image
    theme = "" if style == "plain" else style
    path = images.up_next_banner("SpongeBob SquarePants", 360, "#f57d0d", theme, None, "", None)
    ban = Image.open(path).convert("RGBA")
    pic = Image.new("RGBA", (640, 360), (58, 74, 96, 255))
    x = 0 if images.PLACE.get(theme) == "edge" else int(640 * 0.012) if images.PLACE.get(theme) == "bottom" else int(640 * 0.055)
    y = 360 - ban.height if images.PLACE.get(theme) in ("edge", "bottom") else int(360 * 0.73)
    pic.alpha_composite(ban, (x, min(y, 360 - ban.height)))
    buf = io.BytesIO()
    pic.convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue()


def trivia_preview(theme: str) -> bytes:
    from PIL import Image, ImageDraw
    shot = Image.new("RGB", (720, 540), (90, 140, 200))
    d = ImageDraw.Draw(shot)
    d.ellipse((260, 150, 460, 350), fill=(250, 220, 60))
    d.rectangle((0, 420, 720, 540), fill=(70, 160, 90))
    buf = io.BytesIO()
    shot.save(buf, "JPEG")
    png = trivia.card(buf.getvalue(), "ask", "", theme, 640, 360, False)
    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue()


def bumpers(cat, tag: str, hidden: set) -> list[dict]:
    """The bumpers and idents Rerun has for a network: what plays before its shows and into its breaks."""
    out = [{"id": f["id"], "title": f.get("title", ""), "dur": round(f["dur"], 1), "tags": f.get("tags", []),
            "h": int(f.get("h") or 0), "q": f.get("q") or None, "hidden": f["id"] in hidden}
           for f in cat.all_filler() if "bumper" in (f.get("tags") or []) and tag in (f.get("tags") or [])
           and not f.get("dup")]
    return sorted(out, key=lambda x: (x["hidden"], x["title"].lower()))


def channels_using(cfg: dict, tag: str) -> list[dict]:
    """The channels that are this network's own (its logo, bumpers and Up next on everything they play)."""
    from .lineups import ads_tag
    out = []
    for ch in cfg.get("channels") or []:
        key = ch.get("auto_key") or (ch.get("quick") or {}).get("key") or ""
        t = archive.tag_for_network(key[8:]) if key.startswith("network:") else (
            ads_tag(key[7:]) if key.startswith("lineup:") else None)
        if t == tag:
            out.append({"id": ch["id"], "name": ch["name"], "number": ch["number"]})
    return out


def in_use(cfg: dict, tag: str) -> dict:
    """What "Up next" this network actually gets on the air, in words and a picture, for the page."""
    tpl, style = upnext_override(cfg, tag)
    label = archive.PROFILES.get(tag, (tag, "", "", [tag]))[0]
    chans = archive.PROFILES.get(tag, (tag, "", "", []))[3]
    if tpl is None and style is None:  # automatic: a template named after the network, else its drawing
        tpl = images.find_template([*chans, tag, label])
        style = None if tpl else (images.theme_for(tag) or "")
    if tpl:
        name = os.path.basename(tpl)
        return {"kind": "file", "name": name, "label": os.path.splitext(name)[0],
                "img": f"/api/looks/thumb/upnext/{name}"}
    key = style or "plain"
    return {"kind": "style", "name": key, "label": dict(STYLES).get(key, "Rerun's drawing"),
            "img": f"/api/looks/style/{key}.jpg"}


async def sample(app, tag: str) -> str:
    """A short video of a real show of this network ending with its "Up next", drawn by live TV's code."""
    import asyncio
    import random
    import shutil
    import tempfile
    import time as _time
    from .ffmpeg import RESOLUTIONS
    from .preview import PREVIEW_DIR, _run_to
    cat = app.catalog
    nets = {n.lower() for n in archive.PROFILES[tag][3]} | {archive.PROFILES[tag][0].lower()}
    mine = [sid for sid, sh in cat.shows.items() if (sh.get("network") or "").lower() in nets and cat.episodes.get(sid)]
    pool = mine or [sid for sid in cat.shows if cat.episodes.get(sid)]
    if not pool:
        raise RuntimeError("There are no shows in your library to try it on")
    rng = random.Random()
    sid = rng.choice(pool)
    nxt = cat.shows[rng.choice([x for x in pool if x != sid] or pool)]
    ep = rng.choice(cat.episodes[sid])
    src = app.media_input({"f": ep.get("file") or "", "p": ep.get("part") or ""})
    if not src:
        raise RuntimeError(f"Couldn't open {cat.shows[sid]['title']}")
    s = dict(app.cfg.data["stream"])
    s["video_kbps"] = min(int(s.get("video_kbps") or 4000), 2500)
    H = RESOLUTIONS.get(s.get("resolution"), RESOLUTIONS["720p"])[1]
    length = 14.0
    ov: dict = {}
    year = (ep.get("date") or "")[:4] or cat.shows[sid].get("year")
    try:
        ref = images.era_logo(tag, year) or images.network_logo(archive.PROFILES[tag][3][0], cat)
        logo = await images.screen_bug(ref, H, app.cfg.data["metadata"].get("online_lookups", True),
                                       [archive.PROFILES[tag][3][0], tag], year)
        if logo:
            ov["logo"] = logo
    except Exception:
        pass
    use = in_use(app.cfg.data, tag)
    if use["kind"] == "file":
        f = str(UPNEXT_DIR / use["name"])
        tpl = await asyncio.to_thread(images.analyse_template, f, app.ffmpeg.ffmpeg)
        if not tpl:
            raise RuntimeError("Rerun can't use that template (no green or black to see through)")
        ov["template"] = {**tpl, "file": f, "t0": 1.0, "t1": min(length - 0.5, 1.0 + (tpl["dur"] or 10.0))}
        titled = images.template_title(nxt["title"], f, H)
        if titled:
            ov["template"].update(title_png=titled[0], title_x=titled[1]["x"], title_y=titled[1]["y"])
    else:
        theme = "" if use["name"] == "plain" else use["name"]
        art = await images.artwork(app, nxt["id"]) if theme == "nick" else None
        ban = await asyncio.to_thread(images.up_next_banner, nxt["title"], H, images.bug_accent(ov.get("logo")),
                                      theme, art, nxt["id"], ov.get("logo"))
        ov["banners"] = [{"file": ban, "art": images.banner_art(ban), "t0": 1.0, "t1": length - 2,
                          "motion": images.MOTION.get(theme, "left"), "place": images.PLACE.get(theme, "")}]
    work = tempfile.mkdtemp(prefix="rerun-look-")
    try:
        ts = os.path.join(work, "a.ts")
        cmd = app.ffmpeg.media_cmd(src, max(0.0, ep["dur"] * 0.5), length, s, None, bool(ep.get("audio", True)), overlay=ov)
        if await asyncio.to_thread(_run_to, cmd, ts) != 0:
            cmd = app.ffmpeg.media_cmd(src, max(0.0, ep["dur"] * 0.5), length, s, None, False, overlay=ov)
            if await asyncio.to_thread(_run_to, cmd, ts) != 0:
                raise RuntimeError("Couldn't make the sample")
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        out = PREVIEW_DIR / f"look-{tag}-{int(_time.time())}.mp4"
        res = await asyncio.to_thread(subprocess.run, [app.ffmpeg.ffmpeg, "-v", "error", "-y", "-i", ts, "-c", "copy",
                                                       "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)],
                                      capture_output=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError("Couldn't make the sample")
        return str(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
