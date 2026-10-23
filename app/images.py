"""Channel logos and guide artwork, cached on disk.

Logo order: a logo you picked -> the network's official logo (built-in table from TMDB,
your library's TMDB data, or Wikipedia) -> a 60s marquee-style card Rerun draws itself.
Holiday channels get a drawn holiday logo, and during a holiday every other channel's
logo gets a small festive badge. Show and movie posters are fetched from Plex by Rerun,
so your Plex token never ends up inside the guide.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os

import httpx
from PIL import Image, ImageDraw, ImageFont

from .channels import network_logo
from .store import DATA_DIR

log = logging.getLogger("rerun.images")
SIZE = 400
VERSION = "v5"
FONTS = [  # a bold serif, like the lettering on a 1960s station ID card
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
WIKI_UA = "Rerun/1.0 (self-hosted personal TV app; https://www.mediawiki.org/wiki/API:Etiquette)"
CREAM, PAPER, RULE, INK = "#f4e7cc", "#fbf7ee", "#d9cbb0", "#231c16"
GOLD = "#d4a028"
PALETTE = ["#1d6870", "#c2521d", "#a97c16", "#6f6c2a", "#6c2430", "#2f4f6f", "#4a3a2e"]
HOLIDAY_COLORS = {
    "christmas": "#7c1d24", "halloween": "#231c16", "thanksgiving": "#6b3a1e", "valentines": "#9e2238",
    "stpatricks": "#245c3a", "easter": "#5c5296", "july4": "#1d3557", "newyear": "#1b2a44",
}


def _font(size: int):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _card(d: ImageDraw.ImageDraw, color: str):
    """A rounded card with a thin inset frame, like a printed station identification slide."""
    d.rounded_rectangle((4, 4, SIZE - 4, SIZE - 4), radius=44, fill=color)
    d.rounded_rectangle((24, 24, SIZE - 24, SIZE - 24), radius=28, outline=CREAM, width=3)


def _sparkle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: str):
    d.line((cx - r, cy, cx + r, cy), fill=color, width=3)
    d.line((cx, cy - r, cx, cy + r), fill=color, width=3)
    k = r * 0.55
    d.line((cx - k, cy - k, cx + k, cy + k), fill=color, width=2)
    d.line((cx - k, cy + k, cx + k, cy - k), fill=color, width=2)


def _text_block(d, text: str, box: tuple[int, int, int, int], color: str = CREAM, max_lines: int = 3):
    x0, y0, x1, y1 = box
    for size in (96, 84, 72, 62, 54, 46, 40, 34):
        font = _font(size)
        lines = _wrap(d, text, font, x1 - x0)
        h = len(lines) * size * 1.16
        if len(lines) <= max_lines and h <= y1 - y0 and all(d.textlength(l, font=font) <= x1 - x0 for l in lines):
            break
    y = y0 + (y1 - y0 - h) / 2
    for line in lines:
        w = d.textlength(line, font=font)
        d.text((x0 + (x1 - x0 - w) / 2, y), line, font=font, fill=color)
        y += size * 1.16


def draw_logo(name: str) -> bytes:
    color = PALETTE[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(PALETTE)]
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _card(d, color)
    _sparkle(d, SIZE / 2, 74, 16, GOLD)
    _text_block(d, name, (60, 104, SIZE - 60, SIZE - 62))
    return _png(img)


def frame_logo(raw: bytes) -> bytes:
    """Put an official logo on a paper-white card with a thin rule so it reads in any guide."""
    logo = Image.open(io.BytesIO(raw)).convert("RGBA")
    logo.thumbnail((SIZE - 110, SIZE - 110))
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, SIZE - 4, SIZE - 4), radius=44, fill=PAPER, outline=RULE, width=4)
    img.alpha_composite(logo, ((SIZE - logo.width) // 2, (SIZE - logo.height) // 2))
    return _png(img)


# --------------------------------------------------------------------------- holiday art

def _star(cx, cy, r, points=5, inner=0.45, rot=-90):
    pts = []
    for i in range(points * 2):
        rr = r if i % 2 == 0 else r * inner
        a = math.radians(rot + i * 180 / points)
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def _heart(d, cx, cy, s, fill):
    r = s * 0.27
    d.ellipse((cx - 2 * r, cy - r * 1.6, cx, cy + r * 0.4), fill=fill)
    d.ellipse((cx, cy - r * 1.6, cx + 2 * r, cy + r * 0.4), fill=fill)
    d.polygon([(cx - 1.93 * r, cy - 0.3 * r), (cx + 1.93 * r, cy - 0.3 * r), (cx, cy + 2.1 * r)], fill=fill)


def holiday_icon(d: ImageDraw.ImageDraw, key: str, x: float, y: float, s: float):
    """Draw a holiday picture inside the square (x, y, x+s, y+s)."""
    cx = x + s / 2
    if key == "christmas":
        d.rectangle((cx - s * 0.07, y + s * 0.78, cx + s * 0.07, y + s * 0.96), fill="#7a4a21")
        for top, base, half, col in ((0.14, 0.44, 0.22, "#2e9e4f"), (0.30, 0.64, 0.32, "#278a45"),
                                     (0.48, 0.84, 0.42, "#1f7a3a")):
            d.polygon([(cx, y + s * top), (cx - s * half, y + s * base), (cx + s * half, y + s * base)], fill=col)
        for ox, oy, col in ((-0.12, 0.40, "#e63946"), (0.10, 0.55, "#ffd23f"), (-0.18, 0.70, "#ffd23f"),
                            (0.22, 0.76, "#e63946"), (0.02, 0.68, "#4cc9f0")):
            r = s * 0.04
            d.ellipse((cx + ox * s - r, y + oy * s - r, cx + ox * s + r, y + oy * s + r), fill=col)
        d.polygon(_star(cx, y + s * 0.13, s * 0.11), fill="#ffd23f")
    elif key == "halloween":
        body = (y + s * 0.22, y + s * 0.92)
        for ox, w, col in ((-0.24, 0.42, "#e8721c"), (0.24, 0.42, "#e8721c"), (0, 0.5, "#f28c28")):
            d.ellipse((cx + ox * s - w * s / 2, body[0], cx + ox * s + w * s / 2, body[1]), fill=col,
                      outline="#b8560f", width=max(2, int(s * 0.012)))
        d.rounded_rectangle((cx - s * 0.05, y + s * 0.08, cx + s * 0.05, y + s * 0.27), radius=s * 0.03,
                            fill="#3d7a2a")
        for ex in (-0.15, 0.15):
            d.polygon([(cx + ex * s - s * 0.08, y + s * 0.52), (cx + ex * s + s * 0.08, y + s * 0.52),
                       (cx + ex * s, y + s * 0.38)], fill="#2b1708")
        mouth = [(cx - s * 0.26, y + s * 0.64)]
        for i in range(1, 8):
            mouth.append((cx - s * 0.26 + i * s * 0.52 / 8, y + s * (0.70 if i % 2 else 0.64)))
        mouth += [(cx + s * 0.26, y + s * 0.64), (cx + s * 0.18, y + s * 0.80), (cx - s * 0.18, y + s * 0.80)]
        d.polygon(mouth, fill="#2b1708")
    elif key == "thanksgiving":
        fan = (cx - s * 0.46, y + s * 0.10, cx + s * 0.46, y + s * 1.02)
        cols = ["#c0392b", "#e67e22", "#f1c40f", "#8e5a2b"]
        for i in range(9):
            d.pieslice(fan, 180 + i * 20, 180 + (i + 1) * 20, fill=cols[i % 4], outline="#5a3416")
        d.ellipse((cx - s * 0.22, y + s * 0.42, cx + s * 0.22, y + s * 0.92), fill="#8e5a2b")
        d.ellipse((cx - s * 0.11, y + s * 0.26, cx + s * 0.11, y + s * 0.50), fill="#9c6633")
        for ex in (-0.045, 0.045):
            d.ellipse((cx + ex * s - s * 0.02, y + s * 0.33, cx + ex * s + s * 0.02, y + s * 0.37), fill="#1b1b1b")
        d.polygon([(cx - s * 0.04, y + s * 0.40), (cx + s * 0.04, y + s * 0.40), (cx, y + s * 0.47)], fill="#f39c12")
        d.ellipse((cx + s * 0.01, y + s * 0.42, cx + s * 0.06, y + s * 0.53), fill="#d62828")
    elif key == "valentines":
        _heart(d, cx, y + s * 0.5, s * 0.95, "#e63946")
        _heart(d, cx + s * 0.22, y + s * 0.24, s * 0.35, "#ffb4c2")
    elif key == "stpatricks":
        r = s * 0.17
        for ox, oy in ((0, -0.20), (-0.19, 0.03), (0.19, 0.03)):
            _heart(d, cx + ox * s, y + s * (0.45 + oy), r * 2.1, "#2a9d4b")
        d.line((cx, y + s * 0.5, cx + s * 0.12, y + s * 0.93), fill="#1e6b3a", width=int(s * 0.05))
    elif key == "easter":
        egg = (cx - s * 0.3, y + s * 0.06, cx + s * 0.3, y + s * 0.96)
        d.ellipse(egg, fill="#f7b2d9")
        band = [(cx - s * 0.30, y + s * 0.52)]
        for i in range(1, 10):
            band.append((cx - s * 0.30 + i * s * 0.6 / 10, y + s * (0.44 if i % 2 else 0.52)))
        band += [(cx + s * 0.29, y + s * 0.52), (cx + s * 0.28, y + s * 0.60), (cx - s * 0.28, y + s * 0.60)]
        d.polygon(band, fill="#8ecae6")
        for ox, oy in ((-0.12, 0.28), (0.1, 0.22), (0.0, 0.76), (-0.14, 0.72), (0.14, 0.78)):
            r = s * 0.035
            d.ellipse((cx + ox * s - r, y + oy * s - r, cx + ox * s + r, y + oy * s + r), fill="#ffd166")
    elif key == "july4":
        for bx, by, r, col in ((0.30, 0.32, 0.26, "#e63946"), (0.70, 0.40, 0.24, "#f1faee"),
                               (0.50, 0.72, 0.22, "#4cc9f0")):
            px, py = x + bx * s, y + by * s
            for i in range(12):
                a = math.radians(i * 30)
                d.line((px + math.cos(a) * r * s * 0.25, py + math.sin(a) * r * s * 0.25,
                        px + math.cos(a) * r * s, py + math.sin(a) * r * s), fill=col, width=max(3, int(s * 0.022)))
                d.ellipse((px + math.cos(a) * r * s - 4, py + math.sin(a) * r * s - 4,
                           px + math.cos(a) * r * s + 4, py + math.sin(a) * r * s + 4), fill=col)
    elif key == "newyear":
        r = s * 0.36
        cy = y + s * 0.52
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=CREAM, outline=GOLD, width=int(s * 0.04))
        for i in range(12):
            a = math.radians(i * 30 - 90)
            d.line((cx + math.cos(a) * r * 0.78, cy + math.sin(a) * r * 0.78, cx + math.cos(a) * r * 0.9,
                    cy + math.sin(a) * r * 0.9), fill="#14213d", width=max(2, int(s * 0.015)))
        d.line((cx, cy, cx, cy - r * 0.72), fill="#14213d", width=int(s * 0.035))
        d.line((cx, cy, cx + r * 0.08, cy - r * 0.5), fill="#14213d", width=int(s * 0.045))
        for ox, oy, col in ((-0.4, 0.1, "#ffd23f"), (0.42, 0.14, "#e63946"), (-0.36, 0.86, "#4cc9f0"),
                            (0.4, 0.88, "#ffd23f"), (0.0, 0.06, "#e63946")):
            d.rectangle((cx + ox * s - 5, y + oy * s - 5, cx + ox * s + 5, y + oy * s + 5), fill=col)


def draw_holiday_logo(name: str, key: str) -> bytes:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _card(d, HOLIDAY_COLORS.get(key, "#7c1d24"))
    holiday_icon(d, key, 118, 52, 164)
    _text_block(d, name, (58, 232, SIZE - 58, SIZE - 52), max_lines=2)
    return _png(img)


def add_badge(png: bytes, key: str) -> bytes:
    """A small festive seal in the corner of a logo during a holiday."""
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    d = ImageDraw.Draw(img)
    r = 70
    cx, cy = SIZE - r - 6, SIZE - r - 6
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PAPER, outline=RULE, width=4)
    d.ellipse((cx - r + 10, cy - r + 10, cx + r - 10, cy + r - 10), fill=HOLIDAY_COLORS.get(key, "#7c1d24"))
    holiday_icon(d, key, cx - r * 0.68, cy - r * 0.7, r * 1.36)
    return _png(img)


# --------------------------------------------------------------------------- fetching official logos

async def _fetch_tmdb(path: str) -> bytes | None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://image.tmdb.org/t/p/w300{path}")
    return r.content if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") else None


async def _fetch_wiki(title: str) -> bytes | None:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": WIKI_UA}) as c:
        r = await c.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + title.replace(" ", "_"))
        if r.status_code != 200:
            return None
        thumb = (r.json().get("thumbnail") or {}).get("source", "")
        name = thumb.rsplit("/", 1)[-1].lower()
        words = [w for w in title.lower().replace("(", " ").split() if len(w) > 2]
        if not thumb or not ("logo" in name or (name.split("?")[0].endswith(".png") and any(w in name for w in words))):
            return None  # the article's picture isn't a logo (e.g. a photo of a building)
        img = await c.get(thumb)
    return img.content if img.status_code == 200 else None


async def official_logo(ref: str) -> bytes | None:
    try:
        if ref.startswith("tmdb:/"):
            return await _fetch_tmdb(ref[5:])
        if ref.startswith("wiki:"):
            return await _fetch_wiki(ref[5:])
    except Exception as e:
        log.info("Couldn't fetch logo %s (%s)", ref, e.__class__.__name__)
    return None


def logo_ref(ch: dict, catalog=None) -> str:
    """Which official logo a channel should use, if any."""
    if ch.get("logo"):
        return ch["logo"]
    key = ch.get("auto_key") or ""
    name = key[len("network:"):] if key.startswith("network:") else ch["name"]
    return network_logo(name, catalog)


_logo_locks: dict[str, asyncio.Lock] = {}


async def channel_logo(ch: dict, catalog=None, festive: str = "", online: bool = True) -> bytes:
    # Plex asks for every logo at once; one lock per channel means each is only made once.
    async with _logo_locks.setdefault(ch["id"], asyncio.Lock()):
        return await _channel_logo(ch, catalog, festive, online)


async def _channel_logo(ch: dict, catalog=None, festive: str = "", online: bool = True) -> bytes:
    holiday = ch.get("season") and not ch.get("logo")
    ref = "" if holiday else logo_ref(ch, catalog)
    badge = festive if festive and not ch.get("season") else ""
    key = hashlib.sha1(f"{ch['name']}|{ref}|{ch.get('season')}|{badge}|{online}|{VERSION}".encode()).hexdigest()[:12]
    path = DATA_DIR / "logos" / f"{ch['id']}-{key}.png"
    if path.exists():
        return path.read_bytes()
    data = None
    if holiday:
        data = await asyncio.to_thread(draw_holiday_logo, ch["name"], ch["season"])
    elif ref and online:
        raw = await official_logo(ref)
        if raw:
            try:
                data = await asyncio.to_thread(frame_logo, raw)
            except Exception:
                data = None
    if data is None:
        data = await asyncio.to_thread(draw_logo, ch["name"])
    if badge:
        data = await asyncio.to_thread(add_badge, data, badge)
    path.parent.mkdir(parents=True, exist_ok=True)
    for old in path.parent.glob(f"{ch['id']}-*.png"):
        old.unlink(missing_ok=True)
    path.write_bytes(data)
    return data


async def artwork(app, media_id: str) -> bytes | None:
    thumb = app.catalog.artwork(media_id)
    if not thumb:
        return None
    safe = "".join(ch for ch in media_id if ch.isalnum())
    path = DATA_DIR / "images" / f"{safe}.jpg"
    if path.exists():
        return path.read_bytes()
    plex = app.plex()
    if not plex:
        return None
    try:
        data = await plex.image(thumb, 400, 600)
    except Exception:
        return None
    if data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data
