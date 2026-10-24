"""Channel logos and guide artwork, cached on disk.

Logo order: a logo you picked -> the network's official logo (built-in table from TMDB,
your library's TMDB data, or Wikipedia) -> a bold station-ID card Rerun draws itself.
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
from PIL import Image, ImageChops, ImageDraw, ImageFont

from .channels import network_logo
from .store import DATA_DIR

log = logging.getLogger("rerun.images")
# Logos are wide (16:9) because that's the shape of the logo space in Plex's guide; a square logo
# gets shrunk to fit its height and ends up tiny.
W, H = 640, 360
VERSION = "v6"
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONTS = [  # a heavy geometric sans, like 1960s station ID lettering, and readable when small
    os.path.join(FONT_DIR, "Jost-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
WIKI_UA = "Rerun/1.0 (self-hosted personal TV app; https://www.mediawiki.org/wiki/API:Etiquette)"
CREAM, PAPER, INK, NIGHT = "#f4e7cc", "#fbf7ee", "#231c16", "#15110e"
GOLD = "#d4a028"
STRIPE = ["#c2521d", "#d4a028", "#1d6870"]
PALETTE = ["#1d6870", "#b0461a", "#8a6512", "#5c5a22", "#6c2430", "#2f4f6f", "#4a3a2e"]
HOLIDAY_COLORS = {
    "christmas": "#7c1d24", "halloween": "#231c16", "thanksgiving": "#6b3a1e", "valentines": "#9e2238",
    "stpatricks": "#245c3a", "easter": "#5c5296", "july4": "#1d3557", "newyear": "#1b2a44",
}
RADIUS = 30


def _font(size: int):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _rounded(img: Image.Image) -> Image.Image:
    """Round the corners of a finished logo."""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=RADIUS, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _tile(color: str, stripe: bool = True) -> Image.Image:
    """A solid card with Rerun's thin three-colour stripe along the bottom."""
    img = Image.new("RGBA", (W, H), color)
    if stripe:
        d = ImageDraw.Draw(img)
        for i, c in enumerate(STRIPE):
            d.rectangle((0, H - 21 + i * 7, W, H - 15 + i * 7), fill=c if c != color else CREAM)
    return img


def _splits(words: list[str], lines: int):
    """Every way to break the words into the given number of lines, in order."""
    if lines == 1:
        yield [" ".join(words)]
        return
    for i in range(1, len(words) - lines + 2):
        for rest in _splits(words[i:], lines - 1):
            yield [" ".join(words[:i]), *rest]


def _fit_text(d: ImageDraw.ImageDraw, text: str, box: tuple[float, float, float, float], color: str,
              max_lines: int = 2):
    """Write text as large as it will go inside box, on the most even line breaks."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    words = text.split() or [text]
    best = None
    for n in range(1, min(max_lines, len(words)) + 1):
        for lines in _splits(words, n):
            probe = _font(100)
            wide = max(d.textlength(line, font=probe) for line in lines) / 100
            asc, desc = probe.getmetrics()
            tall = (asc + desc) / 100
            size = int(min(bw / wide, bh / (tall * n + 0.04 * (n - 1))))
            if best is None or size > best[0]:
                best = (size, lines)
    size, lines = best
    size = max(14, min(size, int(bh * 0.92)))
    font = _font(size)
    asc, desc = font.getmetrics()
    step = (asc + desc) * 1.04
    y = y0 + (bh - step * len(lines) + (asc + desc) * 0.04) / 2
    for line in lines:
        d.text(((x0 + x1) / 2, y), line, font=font, fill=color, anchor="ma")
        y += step


def draw_logo(name: str) -> bytes:
    color = PALETTE[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(PALETTE)]
    img = _tile(color)
    # Capitals, like a station card, but "80s" stays "80s".
    caps = " ".join(w if any(c.isdigit() for c in w) else w.upper() for w in name.split())
    _fit_text(ImageDraw.Draw(img), caps if len(name) <= 24 else name, (40, 34, W - 40, H - 52), CREAM)
    return _png(_rounded(img))


def _trim(logo: Image.Image) -> Image.Image:
    """Cut away empty margins, transparent or plain, so the logo itself fills the frame."""
    alpha = logo.getchannel("A")
    if alpha.getextrema()[0] < 250:
        box = alpha.point(lambda a: 255 if a > 12 else 0).getbbox()
    else:
        corner = Image.new("RGBA", logo.size, logo.getpixel((0, 0)))
        diff = ImageChops.difference(logo, corner).convert("L").point(lambda v: 255 if v > 24 else 0)
        box = diff.getbbox()
    return logo.crop(box) if box else logo


def _is_light(logo: Image.Image) -> bool:
    """True for a white or very pale logo, which needs a dark card to show up."""
    small = logo.resize((48, max(1, int(48 * logo.height / max(1, logo.width)))))
    px = [(r, g, b) for r, g, b, a in small.getdata() if a > 128]
    if not px:
        return False
    lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px) / len(px)
    return lum > 200


def frame_logo(raw: bytes) -> bytes:
    """Put an official logo, as big as it fits, on a plain card that reads in any guide."""
    logo = _trim(Image.open(io.BytesIO(raw)).convert("RGBA"))
    img = _tile(NIGHT if _is_light(logo) else PAPER, stripe=False)
    scale = min(W * 0.86 / logo.width, H * 0.80 / logo.height)
    logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.LANCZOS)
    img.alpha_composite(logo, ((W - logo.width) // 2, (H - logo.height) // 2))
    return _png(_rounded(img))


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
    img = _tile(HOLIDAY_COLORS.get(key, "#7c1d24"))
    d = ImageDraw.Draw(img)
    icon = 176
    holiday_icon(d, key, 30, (H - 18 - icon) / 2, icon)
    _fit_text(d, name, (30 + icon + 18, 36, W - 28, H - 54), CREAM, max_lines=2)
    return _png(_rounded(img))


def add_badge(png: bytes, key: str) -> bytes:
    """A small festive seal in the corner of a logo during a holiday."""
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    d = ImageDraw.Draw(img)
    r = 62
    cx, cy = W - r - 10, H - r - 10
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PAPER)
    d.ellipse((cx - r + 8, cy - r + 8, cx + r - 8, cy + r - 8), fill=HOLIDAY_COLORS.get(key, "#7c1d24"))
    holiday_icon(d, key, cx - r * 0.68, cy - r * 0.7, r * 1.36)
    return _png(img)


# --------------------------------------------------------------------------- fetching official logos

async def _fetch_tmdb(path: str) -> bytes | None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://image.tmdb.org/t/p/w500{path}")
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
