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
from pathlib import Path
import os
import re
import threading
from urllib.parse import quote, urlencode

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .channels import network_logo
from .store import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger("rerun.images")
# Logos are wide (16:9) because that's the shape of the logo space in Plex's guide; a square logo
# gets shrunk to fit its height and ends up tiny.
W, H = 640, 360
VERSION = "v7"
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FACES = {  # all SIL Open Font License, shipped with Rerun so every computer draws the same logos
    "sans": "Jost-Bold.ttf",                # geometric 1960s lettering
    "serif": "LibreCaslonText-Bold.ttf",    # printed TV Guide headline type
    "fun": "Shrikhand-Regular.ttf",         # chunky retro display letters for kids' channels
}
FALLBACKS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
WIKI_UA = "Rerun/1.0 (self-hosted personal TV app; https://www.mediawiki.org/wiki/API:Etiquette)"
CREAM, PAPER, INK, NIGHT = "#f4e7cc", "#fbf7ee", "#231c16", "#15110e"
GOLD = "#d4a028"
STRIPE = ["#c2521d", "#d4a028", "#1d6870"]
PALETTE = ["#1d6870", "#b0461a", "#8a6512", "#5c5a22", "#6c2430", "#2f4f6f", "#4a3a2e"]
KIDS = ["#f26b1d", "#7b3fb0", "#1f9e5a", "#e23c74", "#1e88c8", "#e5a50a"]
NEON = ["#ff4f9a", "#3fe0d0", "#ffd23f", "#a78bfa", "#ff7a45"]
HOLIDAY_COLORS = {
    "christmas": "#7c1d24", "halloween": "#231c16", "thanksgiving": "#6b3a1e", "valentines": "#9e2238",
    "stpatricks": "#245c3a", "easter": "#5c5296", "july4": "#1d3557", "newyear": "#1b2a44",
}
RADIUS = 30


def _font(size: int, face: str = "sans"):
    for f in [os.path.join(FONT_DIR, FACES.get(face, FACES["sans"])), *FALLBACKS]:
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


def _stripe(d: ImageDraw.ImageDraw, color: str):
    """Rerun's thin three-colour stripe along the bottom of a card."""
    for i, c in enumerate(STRIPE):
        d.rectangle((0, H - 21 + i * 7, W, H - 15 + i * 7), fill=c if c != color else CREAM)


def _tile(color: str, stripe: bool = True) -> Image.Image:
    img = Image.new("RGBA", (W, H), color)
    if stripe:
        _stripe(ImageDraw.Draw(img), color)
    return img


def _pick(name: str, options: list):
    return options[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(options)]


def _shade(color: str, f: float) -> str:
    """Lighter (f > 0) or darker (f < 0) version of a colour."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    t = 255 if f > 0 else 0
    f = abs(f)
    return "#%02x%02x%02x" % tuple(round(c + (t - c) * f) for c in (r, g, b))


# --------------------------------------------------------------------------- lettering

def _splits(words: list[str], lines: int):
    """Every way to break the words into the given number of lines, in order."""
    if lines == 1:
        yield [" ".join(words)]
        return
    for i in range(1, len(words) - lines + 2):
        for rest in _splits(words[i:], lines - 1):
            yield [" ".join(words[:i]), *rest]


def _layout(text: str, box, face: str = "sans", max_lines: int = 2, gap: float = 0.14):
    """The biggest size, and most even line breaks, at which text fits box. Measured on the
    letters themselves, so capitals really fill the space."""
    x0, y0, x1, y1 = box
    probe = _font(200, face)
    scratch = ImageDraw.Draw(Image.new("L", (1, 1)))
    words = text.split() or [text]
    best = None
    for n in range(1, min(max_lines, len(words)) + 1):
        for lines in _splits(words, n):
            boxes = [scratch.textbbox((0, 0), line, font=probe, anchor="ls") for line in lines]
            wide = max(b[2] - b[0] for b in boxes)
            cap = max(-b[1] for b in boxes)
            drop = max(0, boxes[-1][3])
            # Lines sit far enough apart that tails (g, y) never touch the next line's letters.
            inner = max([max(0, b[3]) for b in boxes[:-1]] or [0])
            step = cap + inner + gap * cap
            tall = cap + step * (n - 1) + drop
            size = 200 * min((x1 - x0) / wide, (y1 - y0) / tall)
            if best is None or size > best[0] * 1.04:  # fewer lines unless clearly bigger
                best = (size, lines, cap / 200, drop / 200, step / 200)
    size, lines, cap, drop, step = best
    size = max(12, int(size))
    step *= size
    total = cap * size + step * (len(lines) - 1) + drop * size
    y = y0 + ((y1 - y0) - total) / 2 + cap * size
    spots = []
    for line in lines:
        spots.append(((x0 + x1) / 2, y, line))
        y += step
    return _font(size, face), spots, size


def _letters(img: Image.Image, text: str, box, fill: str, face: str = "sans", max_lines: int = 2,
             stroke: float = 0, stroke_fill: str | None = None, shadow: tuple | None = None,
             glow: tuple | None = None, gap: float = 0.14) -> int:
    """Paint text into box, with an optional outline, a solid drop shadow (dx, dy as a share of
    the letter size, colour) and a neon glow (colour, radius). Returns the font size used."""
    font, spots, size = _layout(text, box, face, max_lines, gap)
    sw = int(size * stroke)
    if glow:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(layer)
        for x, y, line in spots:
            gd.text((x, y), line, font=font, fill=glow[0], anchor="ms", stroke_width=max(sw, int(size * 0.05)),
                    stroke_fill=glow[0])
        for r in (glow[1], glow[1] / 3):
            img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(r)))
    d = ImageDraw.Draw(img)
    if shadow:
        dx, dy, col = shadow
        for x, y, line in spots:
            for k in range(1, 4):  # a solid, stepped shadow like a 3D sign letter
                d.text((x + dx * k / 3 * size, y + dy * k / 3 * size), line, font=font, fill=col, anchor="ms",
                       stroke_width=sw, stroke_fill=col)
    for x, y, line in spots:
        d.text((x, y), line, font=font, fill=fill, anchor="ms", stroke_width=sw, stroke_fill=stroke_fill or fill)
    return size


def _caps(name: str) -> str:
    """Capitals, like a station card, but "80s" stays "80s"."""
    return " ".join(w if any(c.isdigit() for c in w) else w.upper() for w in name.split())


# --------------------------------------------------------------------------- ornaments

def _twinkle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: str):
    """The four-pointed atomic-age sparkle."""
    k = r * 0.16
    d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k), (cx, cy + r), (cx - k, cy + k),
               (cx - r, cy), (cx - k, cy - k)], fill=color)


def _bulbs(img: Image.Image, color: str, inset: int = 18, gap: int = 34, r: int = 7):
    """Glowing light bulbs around the edge, like a game-show set or a theatre marquee."""
    pts = []
    x0, y0, x1, y1 = inset, inset, W - inset, H - inset
    nx, ny = round((x1 - x0) / gap), round((y1 - y0) / gap)
    for i in range(nx + 1):
        x = x0 + (x1 - x0) * i / nx
        pts += [(x, y0), (x, y1)]
    for j in range(1, ny):
        y = y0 + (y1 - y0) * j / ny
        pts += [(x0, y), (x1, y)]
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for x, y in pts:
        gd.ellipse((x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2), fill=color)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(r * 1.4)))
    d = ImageDraw.Draw(img)
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=_shade(color, 0.55))
        d.ellipse((x - r * 0.45, y - r * 0.6, x + r * 0.05, y - r * 0.1), fill="#fffbea")


# --------------------------------------------------------------------------- the five looks

def _look_bubble(name: str) -> Image.Image:
    """Kids: a bright card with confetti dots and fat retro letters with a 3D shadow."""
    bg = _pick(name, KIDS)
    img = _tile(bg, stripe=False)
    d = ImageDraw.Draw(img)
    light = _shade(bg, 0.18)
    for i in range(14):
        n = int(hashlib.md5(f"{name}{i}".encode()).hexdigest(), 16)
        x, y, r = n % W, (n >> 12) % H, 10 + (n >> 24) % 26
        d.ellipse((x - r, y - r, x + r, y + r), fill=light)
    _letters(img, name, (34, 26, W - 40, H - 40), "#ffffff", face="fun", stroke=0.035, stroke_fill=INK,
             shadow=(0.035, 0.05, INK), gap=0.05)
    return img


def _look_neon(name: str) -> Image.Image:
    """Music: a night-black card, a border of bulbs and neon tube lettering."""
    tube = _pick(name, NEON)
    img = _tile("#120d0b", stripe=False)
    _bulbs(img, GOLD)
    _letters(img, _caps(name), (64, 58, W - 64, H - 58), _shade(tube, 0.7), stroke=0.012, stroke_fill=tube,
             glow=(tube, 16))
    return img


def _look_marquee(name: str) -> Image.Image:
    """Movies: velvet colour, a gold-bulb marquee and cream headline type."""
    img = _tile(_pick(name, ["#6c1a22", "#1d2f4f", "#3b2418"]), stripe=False)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((46, 46, W - 46, H - 46), radius=14, outline=GOLD, width=3)
    _bulbs(img, "#ffcf5a", inset=22, gap=30, r=6)
    _letters(img, name, (72, 66, W - 72, H - 66), CREAM, face="serif", shadow=(0.0, 0.035, "#00000088"))
    return img


def _look_station(name: str, number) -> Image.Image:
    """Classic and broadcast TV: a paper station-ID card with the channel number in a starburst."""
    img = _tile(PAPER, stripe=False)
    d = ImageDraw.Draw(img)
    _stripe(d, PAPER)
    cx, cy, r = 128, (H - 21) / 2, 112
    d.polygon(_star(cx, cy, r, 16, 0.74), fill=GOLD)
    d.ellipse((cx - r * 0.66, cy - r * 0.66, cx + r * 0.66, cy + r * 0.66), fill="#c2521d")
    if number:
        _letters(img, str(number), (cx - r * 0.5, cy - r * 0.42, cx + r * 0.5, cy + r * 0.42), CREAM, face="serif",
                 max_lines=1)
    _letters(img, name, (cx + r + 20, 38, W - 30, H - 52), INK, face="serif", max_lines=3, gap=0.22)
    return img


def _look_atomic(name: str) -> Image.Image:
    """Everything else: a solid mid-century colour, sparkles and big bold capitals."""
    bg = _pick(name, PALETTE)
    img = _tile(bg)
    d = ImageDraw.Draw(img)
    spark = _shade(bg, 0.28)
    for x, y, r in ((44, 46, 24), (W - 50, 60, 16), (W - 34, H - 74, 22), (30, H - 90, 12)):
        _twinkle(d, x, y, r, spark)
    _letters(img, _caps(name) if len(name) <= 24 else name, (46, 34, W - 46, H - 50), CREAM,
             shadow=(0.0, 0.03, _shade(bg, -0.45)))
    return img


LOOKS = {"Kids": "bubble", "Anime": "bubble", "Music": "neon", "Movies": "marquee", "Premium": "marquee",
         "Classic TV": "station", "Broadcast Networks": "station"}


def look_for(name: str, group: str = "") -> str:
    """Which look a drawn logo gets: from the channel's group, or failing that its name."""
    if group in LOOKS:
        return LOOKS[group]
    low = name.lower()
    if any(w in low for w in ("music", "video", "hits", "mtv", "vh1", "radio", "rock", "jams")):
        return "neon"
    if any(w in low for w in ("movie", "cinema", "film", "flix", "theater", "theatre")):
        return "marquee"
    if any(w in low for w in ("kid", "cartoon", "toon", "jr", "junior", "saturday morning")):
        return "bubble"
    if any(w in low for w in ("classic", "retro", "vintage", "oldies", "rerun")):
        return "station"
    return "atomic"


def draw_logo(name: str, group: str = "", number=None) -> bytes:
    look = look_for(name, group)
    if look == "bubble":
        img = _look_bubble(name)
    elif look == "neon":
        img = _look_neon(name)
    elif look == "marquee":
        img = _look_marquee(name)
    elif look == "station":
        img = _look_station(name, number)
    else:
        img = _look_atomic(name)
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
    icon = 150
    holiday_icon(d, key, 22, (H - 18 - icon) / 2, icon)
    _letters(img, name, (22 + icon + 14, 26, W - 22, H - 44), CREAM, face="fun", max_lines=2,
             shadow=(0.0, 0.04, _shade(HOLIDAY_COLORS.get(key, "#7c1d24"), -0.5)), gap=0.08)
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
    if img.status_code == 200:
        _WIKI_SRC[title] = thumb
    return img.content if img.status_code == 200 else None


_WIKI_SRC: dict[str, str] = {}  # Wikipedia article -> the logo picture's public address


# The logos networks had in earlier years, by the commercials tag of the network (archive.PROFILES):
# (first year, last year, picture). From the networks' Wikipedia articles and Wikimedia Commons; years
# outside these use today's logo. Your own pictures in bugs/custom/ win (see custom_bug).
ERA_LOGOS = {
    "nick": [(1984, 2009, "commons:Nickelodeon_logo_2009.png"),          # the orange splat
             (2009, 2022, "commons:Nickelodeon_2017_logo.svg")],          # the 2009 wordmark
    "cn": [(1992, 2004, "commons:Cartoon_Network_1992-2004.svg"),
           (2004, 2010, "commons:Cartoon_Network_logo_(2004-2010).svg"),
           (2010, 2030, "commons:Cartoon_Network_2010_logo.svg")],
    "disney": [(1997, 2010, "commons:2002_Disney_Channel_logo.svg")],
    "boomerang": [(2000, 2014, "commons:Boomerang_tv_logo_(2000).svg")],
    "playhouse": [(1997, 2011, "commons:2000_Playhouse_Disney_logo.svg")],
    "toondisney": [(1998, 2009, "commons:Toon_Disney_logo.svg")],
    "nicktoons": [(2002, 2009, "commons:Nicktoons-Network-original-balloon-text-logo.png")],
    "foxkids": [(1990, 2002, "commons:FOX_Kids_logo.svg")],
    "kidswb": [(1995, 1996, "enwiki:Kids_WB_95.svg"), (1997, 2008, "enwiki:Kids'_WB!_(logo).png")],
}


def era_logo(tag: str | None, year) -> str:
    """The logo a network had in a year ("commons:..."), or "" for today's."""
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return ""
    return next((ref for a, b, ref in ERA_LOGOS.get(tag or "", []) if a <= y <= b), "")


async def _fetch_wikifile(ref: str) -> bytes | None:
    """A picture file from Wikimedia Commons or Wikipedia, as a PNG (SVGs are drawn by Wikimedia)."""
    host = "https://commons.wikimedia.org" if ref.startswith("commons:") else "https://en.wikipedia.org"
    name = ref.split(":", 1)[1]
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": WIKI_UA}) as c:
        r = await c.get(f"{host}/wiki/Special:FilePath/{quote(name)}", params={"width": 600})
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
        return r.content
    return None


async def official_logo(ref: str) -> bytes | None:
    for attempt in range(3):  # a brief network hiccup shouldn't leave a channel with a stand-in logo
        try:
            if ref.startswith("tmdb:/"):
                return await _fetch_tmdb(ref[5:])
            if ref.startswith("wiki:"):
                return await _fetch_wiki(ref[5:])
            if ref.startswith(("commons:", "enwiki:")):
                return await _fetch_wikifile(ref)
            return None
        except (httpx.TransportError, httpx.TimeoutException) as e:
            if attempt == 2:
                log.info("Couldn't fetch logo %s (%s)", ref, e.__class__.__name__)
            else:
                await asyncio.sleep(1 + attempt)
        except Exception as e:
            log.info("Couldn't fetch logo %s (%s)", ref, e.__class__.__name__)
            return None
    return None


# --------------------------------------------------------------------------- the on-screen logo ("bug")

BUG_DIR = DATA_DIR / "bugs"
BUG_V = 4
_bug_misses: dict[str, float] = {}


def _ink(logo: Image.Image, alpha: Image.Image) -> Image.Image:
    """How strongly each pixel is part of the logo's drawing: dark or colourful parts are ink, white parts
    are gaps (so the B inside Boomerang's cube stays a B). An all-white logo is all ink."""
    dark = ImageChops.invert(logo.convert("L"))
    colour = logo.convert("RGB").convert("HSV").getchannel("S")
    ink = ImageChops.lighter(dark, colour).point(lambda v: min(255, int(v * 1.7)))
    ink = ImageChops.multiply(ink, alpha)
    area = sum(alpha.point(lambda a: 1 if a > 128 else 0).getdata()) or 1
    inked = sum(ink.point(lambda a: 1 if a > 128 else 0).getdata())
    hsv = logo.convert("RGB").convert("HSV")
    hues = [h * 12 // 256 for (h, s, _v), a in zip(hsv.getdata(), alpha.getdata()) if a > 128 and s > 80]
    many = sum(1 for b in range(12) if hues.count(b) > len(hues) * 0.12) >= 2 if hues else False
    if inked / area > 0.93 and many:
        # Colourful all over (Playhouse Disney's yellow cloud, Fox Kids, Toon Disney's red Mickey): the
        # darker letters are the drawing and the lighter colour around them the gaps, or it'd be a blob.
        lum = [v for v, a in zip(dark.getdata(), alpha.getdata()) if a > 128]
        lum.sort()
        lo, hi = lum[len(lum) // 10], lum[len(lum) * 9 // 10]
        if hi - lo > 40:
            span = hi - lo
            ink = ImageChops.multiply(dark.point(lambda v: max(0, min(255, int((v - lo) * 255 / span)))), alpha)
            inked = sum(ink.point(lambda a: 1 if a > 128 else 0).getdata())
    return ink if inked / area > 0.12 else alpha


def make_bug(raw: bytes, height: int) -> bytes | None:
    """A network logo as a see-through white mark, the way channels kept a quiet logo in the corner of
    the picture. A faint shadow keeps it readable on a white scene."""
    logo = _trim(Image.open(io.BytesIO(raw)).convert("RGBA"))
    alpha = logo.getchannel("A")
    if alpha.getextrema()[0] >= 250:  # no transparency: the logo is whatever differs from its background
        corner = Image.new("RGBA", logo.size, logo.getpixel((0, 0)))
        alpha = ImageChops.difference(logo, corner).convert("L").point(lambda v: 255 if v > 40 else 0)
        if not alpha.getbbox():
            return None
    alpha = _ink(logo, alpha)
    target_h = max(18, round(height * 0.075))
    scale = min(target_h / logo.height, height * 0.26 / logo.width)
    size = (max(1, round(logo.width * scale)), max(1, round(logo.height * scale)))
    alpha = alpha.resize(size, Image.LANCZOS)
    pad = 4
    out = Image.new("RGBA", (size[0] + pad * 2, size[1] + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", size, (0, 0, 0, 255))
    shadow.putalpha(alpha.point(lambda a: int(a * 0.28)))
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(1.5)), (pad + 1, pad + 2))
    white = Image.new("RGBA", size, (255, 255, 255, 255))
    white.putalpha(alpha.point(lambda a: int(a * 0.52)))
    out.alpha_composite(white, (pad, pad))
    return _png(out)


_YEARS_IN_NAME = re.compile(r"^(.*?)\s+((?:19|20)\d\d)(?:\s*-\s*((?:19|20)\d\d))?$")


def custom_bug(names: list[str], year=None) -> str | None:
    """Your own on-screen logo for a network, if you've put one in the "bugs" folder of Rerun's data
    folder: named after the network ("Nickelodeon.png") or its commercials tag ("nick.png"). For the logo
    it had in certain years, add them to the name: "Nickelodeon 1984-2009.png", "cn 1998.png"; shows
    from those years get it, and the plain one (if any) the rest."""
    folder = BUG_DIR / "custom"
    try:
        y = int(str(year)[:4]) if year else 0
    except ValueError:
        y = 0
    if y and folder.is_dir():
        wanted = {n.lower() for n in names if n}
        for f in sorted(folder.iterdir()):
            m = _YEARS_IN_NAME.match(f.stem)
            if m and f.suffix.lower() in (".png", ".webp", ".jpg") and m.group(1).lower() in wanted:
                a = int(m.group(2))
                if a <= y <= int(m.group(3) or a):
                    return str(f)
    for n in names:
        for ext in (".png", ".webp", ".jpg"):
            p = folder / f"{n}{ext}"
            if n and p.is_file():
                return str(p)
    return None


async def screen_bug(ref: str, height: int, online: bool = True, names: list[str] | None = None,
                     year=None) -> str | None:
    """The file of a network's on-screen logo at this picture height, made once and kept. With a year,
    the logo the network had then (your own picture for those years, or one from ERA_LOGOS)."""
    own = custom_bug(names or [], year)
    if own:
        ref = f"file:{own}:{int(os.path.getmtime(own))}"
    if not ref:
        return None
    path = BUG_DIR / f"{hashlib.sha1(f'{BUG_V}:{ref}:{height}'.encode()).hexdigest()[:16]}.png"
    if path.exists():
        return str(path)
    if _bug_misses.get(ref, 0) > asyncio.get_event_loop().time() or not (online or own):
        return None
    raw = open(own, "rb").read() if own else await official_logo(ref)
    try:
        png = await asyncio.to_thread(make_bug, raw, height) if raw else None
    except Exception as e:  # a logo file Pillow can't read
        log.info("Couldn't make the on-screen logo for %s (%s)", ref, e.__class__.__name__)
        png = None
    if not png:
        _bug_misses[ref] = asyncio.get_event_loop().time() + 3600  # don't ask again for an hour
        return None
    BUG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    try:
        colour = await asyncio.to_thread(logo_accent, raw)
        if colour:
            path.with_suffix(".colour").write_text(colour)
    except Exception:
        pass
    return str(path)


def bug_accent(bug_path: str | None) -> str | None:
    """The colour of the logo a bug was made from ("#f57d0d" for Nick's orange), if it has one."""
    try:
        return open(os.path.splitext(bug_path)[0] + ".colour", encoding="utf-8").read().strip() if bug_path else None
    except OSError:
        return None


def logo_accent(raw: bytes) -> str | None:
    """A logo's main colour, if it has a strong one: Nickelodeon's orange, Disney's blue. Black-and-white
    logos (Boomerang's cube, Cartoon Network's letters) have none."""
    logo = Image.open(io.BytesIO(raw)).convert("RGBA")
    logo.thumbnail((96, 96))
    hsv = logo.convert("RGB").convert("HSV")
    px = [(rgb, sv) for rgb, sv, a in zip(logo.convert("RGB").getdata(), hsv.getdata(), logo.getchannel("A").getdata())
          if a > 128]
    strong = [rgb for rgb, (h, sat, val) in px if sat > 115 and val > 90]
    if not px or len(strong) < len(px) * 0.08:
        return None
    r, g, b = (sorted(c[i] for c in strong)[len(strong) // 2] for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


BANNER_V = 3
_LABEL = threading.local()  # what the banner says instead of "Up next" ("Tonight at 8", "After the break")


def _lab(default: str) -> str:
    """The banner's label in this style's own lettering: "COMING UP NEXT" -> "TONIGHT AT 8"."""
    o = getattr(_LABEL, "text", "")
    if not o:
        return default
    if default.isupper():
        return o.upper()
    if default.islower():
        return o.lower()
    return o[:1].upper() + o[1:]


def _short(default: str) -> str:
    """The one-word label for styles that only have room for a word ("next" in Nick's splat)."""
    o = getattr(_LABEL, "short", "")
    if not o:
        return default
    o = o.upper() if default.isupper() else o.lower() if default.islower() else o
    return o + "!" if default.endswith("!") and not o.endswith("!") else o
GOLD_ACCENT = "#d4a028"


def _ink_on(colour: str) -> tuple:
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return (21, 17, 14, 255) if 0.299 * r + 0.587 * g + 0.114 * b > 150 else (255, 253, 248, 255)


def make_banner(title: str, height: int, accent: str | None = None) -> bytes:
    """The "Up next" banner: a coloured tab in the network's colour, then the next show's title on a dark
    bar, with rounded ends and a soft shadow. Drawn twice as big and shrunk, so the edges are smooth."""
    accent = accent if accent and re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else GOLD_ACCENT
    k = 2
    bh = round(height * 0.1) * k
    radius = round(bh * 0.22)
    label_font = _font(round(bh * 0.25), "sans")
    title_font = _font(round(bh * 0.47), "sans")
    label = _lab("UP NEXT")
    tracking = round(bh * 0.035)
    label_w = sum(label_font.getlength(ch) for ch in label) + tracking * (len(label) - 1)
    tab_w = round(label_w + bh * 0.62)
    max_title_w = height * 1.02 * k - tab_w
    title = " ".join((title or "").split())
    while title_font.getlength(title) > max_title_w and len(title) > 4:
        title = title[:-2].rstrip(" ,.-:") + "…"
    title_w = round(title_font.getlength(title) + bh * 0.7)
    w = tab_w + title_w
    shadow_pad = round(bh * 0.25)
    img = Image.new("RGBA", (w + shadow_pad * 2, bh + shadow_pad * 2), (0, 0, 0, 0))
    # shadow
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((shadow_pad, shadow_pad + round(bh * 0.06), shadow_pad + w,
                                          shadow_pad + bh + round(bh * 0.06)), radius, fill=(0, 0, 0, 120))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(bh * 0.09)))
    d = ImageDraw.Draw(img)
    x0, y0 = shadow_pad, shadow_pad
    # the dark bar for the title, then the coloured tab over its left end
    d.rounded_rectangle((x0, y0, x0 + w, y0 + bh), radius, fill=(20, 16, 13, 218))
    d.rounded_rectangle((x0, y0, x0 + tab_w + radius, y0 + bh), radius, fill=accent)
    d.rectangle((x0 + tab_w, y0, x0 + tab_w + radius, y0 + bh), fill=(20, 16, 13, 218))
    d.line((x0 + tab_w + radius * 0.4, y0 + bh - k * 2, x0 + w - radius, y0 + bh - k * 2), fill=accent, width=k * 2)
    # "UP NEXT", spaced out, centred in the tab
    ink = _ink_on(accent)
    lx = x0 + (tab_w - label_w) / 2
    ly = y0 + (bh - (label_font.getbbox("UP")[3] - label_font.getbbox("UP")[1])) / 2 - label_font.getbbox("UP")[1]
    for ch in label:
        d.text((lx, ly), ch, font=label_font, fill=ink)
        lx += label_font.getlength(ch) + tracking
    # the title, centred on the bar's height
    tb = title_font.getbbox("Hg")
    ty = y0 + (bh - (tb[3] - tb[1])) / 2 - tb[1]
    d.text((x0 + tab_w + bh * 0.35, ty), title, font=title_font, fill=(255, 253, 248, 255))
    out = img.resize((max(1, img.width // k), max(1, img.height // k)), Image.LANCZOS)
    return _png(out)


def up_next_banner(title: str, height: int, accent: str | None = None, theme: str = "", art: bytes | None = None,
                   art_key: str = "", logo: str | None = None, label: str = "", short: str = "") -> str | None:
    """The banner's file, made once per title, size, colour, style, artwork and logo. `label` changes what
    it says instead of "Up next" ("Tonight at 8"), `short` the one word some styles have room for."""
    if not title:
        return None
    folder = BUG_DIR / "banners"
    key = f"{BANNER_V}:{title}:{height}:{accent}:{theme}:{art_key if art else ''}:{logo or ''}:{label}:{short}"
    path = folder / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.png"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        parts: dict = {}
        _LABEL.text, _LABEL.short = label, short
        try:
            png = (make_themed_banner(title, height, theme, art, logo, parts) if theme in THEME_DRAW
                   else make_banner(title, height, accent))
        finally:
            _LABEL.text = _LABEL.short = ""
        if parts.get("art"):
            path.with_suffix(".art.png").write_bytes(parts["art"])
        path.write_bytes(png)
    return str(path)


def banner_art(banner: str | None) -> str | None:
    """The artwork that goes with a banner, drawn as its own layer (Nick's poster), if it has one."""
    if not banner:
        return None
    art = Path(banner).with_suffix(".art.png")
    return str(art) if art.exists() else None


def logo_ref(ch: dict, catalog=None) -> str:
    """Which official logo a channel should use, if any."""
    if ch.get("logo"):
        return ch["logo"]
    key = ch.get("auto_key") or ""
    name = key[len("network:"):] if key.startswith("network:") else ch["name"]
    return network_logo(name, catalog)


_logo_locks: dict[str, asyncio.Lock] = {}
LOGO_STATUS: dict[str, str] = {}  # channel id -> which logo it got, for the diagnostic report


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
        LOGO_STATUS.setdefault(ch["id"], "ready (made earlier)")
        if ref and online and needs_public(ref):
            raw = await official_logo(ref)
            if raw:
                _learn_public(ref, raw)
        return path.read_bytes()
    data = None
    if holiday:
        data = await asyncio.to_thread(draw_holiday_logo, ch["name"], ch["season"])
        LOGO_STATUS[ch["id"]] = "holiday card"
    elif ref and online:
        raw = await official_logo(ref)
        if raw:
            try:
                data = await asyncio.to_thread(frame_logo, raw)
                LOGO_STATUS[ch["id"]] = f"official ({ref.split(':')[0]})"
                _learn_public(ref, raw)
            except Exception as e:
                log.warning("Couldn't use the official logo for %s: %s", ch["name"], e)
                data = None
    if data is None:
        data = await asyncio.to_thread(draw_logo, ch["name"], ch.get("group", ""), ch.get("number"))
        LOGO_STATUS[ch["id"]] = "drawn" + (" (official logo couldn't be downloaded)" if ref and online else "")
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


# --------------------------------------------------------------------------- logos for Plex apps
#
# Plex apps load channel logos themselves, straight from the address in the guide. Over a secure
# connection (the Apple TV and phone apps, app.plex.tv) they only load public https addresses, so
# a picture served by Rerun on the home network never shows. For those apps the guide points at
# two free public image services instead, styled like Plex's own channel logos: no box, just the
# logo on the guide's dark background.
#   * wsrv.nl trims a network's official logo (from TMDB or Wikipedia) and sizes it to sit inside
#     Plex's logo space with a margin; black logos are turned white so they show on dark.
#   * placehold.co letters the name of a channel without an official logo, in the channel's colour,
#     and wsrv.nl trims and sizes that the same way.
# They see which logo or channel name is being drawn, nothing else.

PUBLIC_V = 2
_PUBLIC_FILE = DATA_DIR / "logos" / "public.json"
_public_cache: dict | None = None
# Bright versions of each look's colour, for lettering on Plex's dark guide.
ON_DARK = {"bubble": ["#ff8a3d", "#c08cff", "#4cd98a", "#ff6fa8", "#5cc8ff", "#ffd23f"],
           "neon": NEON, "marquee": ["#ffcf5a"], "station": [CREAM],
           "atomic": ["#6fd3d9", "#ff9a5c", "#f2c14e", "#c7c36a", "#ff8fa0", "#8fb8e8", "#e0c9a6"]}
HOLIDAY_ON_DARK = {"christmas": "#ff6b6b", "halloween": "#ff9a3c", "thanksgiving": "#f2a65a",
                   "valentines": "#ff7aa8", "stpatricks": "#4cd98a", "easter": "#c3a6ff", "july4": "#7ab8ff",
                   "newyear": "#ffd23f"}


def _public() -> dict:
    global _public_cache
    if _public_cache is None:
        _public_cache = read_json(_PUBLIC_FILE, {})
    return _public_cache


def needs_public(ref: str) -> bool:
    return bool(ref) and _public().get(ref, {}).get("v") != PUBLIC_V


def _is_dark(logo: Image.Image) -> bool:
    """True for a black or near-black logo, which would vanish on Plex's dark guide."""
    small = logo.resize((64, max(1, int(64 * logo.height / max(1, logo.width)))))
    px = [(r, g, b) for r, g, b, a in small.getdata() if a > 128]
    if not px:
        return False
    dark = sum(1 for r, g, b in px if 0.299 * r + 0.587 * g + 0.114 * b < 80
               and (max(r, g, b) - min(r, g, b)) < 60)
    return dark / len(px) > 0.45


def _learn_public(ref: str, raw: bytes):
    """Remember where an official logo lives publicly, its shape, and whether it's black."""
    if ref.startswith("tmdb:/"):
        src = f"image.tmdb.org/t/p/w500{ref[5:]}"
    elif ref.startswith("wiki:") and ref[5:] in _WIKI_SRC:
        src = _WIKI_SRC[ref[5:]]
    else:
        return
    try:
        logo = _trim(Image.open(io.BytesIO(raw)).convert("RGBA"))
        dark, aspect = _is_dark(logo), logo.width / max(1, logo.height)
    except Exception:
        dark, aspect = False, 2.0
    entry = {"src": src, "dark": dark, "aspect": round(aspect, 2), "v": PUBLIC_V}
    if _public().get(ref) != entry:
        _public()[ref] = entry
        _PUBLIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_PUBLIC_FILE, _public())


def _canvas(aspect: float) -> tuple[int, int]:
    """A picture shape that leaves a margin once Plex fits it into its 16:9 logo space: wide logos
    get a slightly narrow canvas (space at the sides), the rest a wide one (space above and below)."""
    return (563, 360) if aspect >= 2.0 else (640, 281)


def _fit(url: str, aspect: float, extra: dict | None = None) -> str:
    w, h = _canvas(aspect)
    return "https://wsrv.nl/?" + urlencode({"url": url, "w": w, "h": h, "fit": "contain", "cbg": "00000000",
                                            "tbg": "00000000", "trim": 10, **(extra or {}), "output": "png"})


def _two_lines(text: str) -> list[str]:
    words = text.split()
    if len(words) < 2 or len(text) <= 9:
        return [text]
    best = min(range(1, len(words)), key=lambda i: max(len(" ".join(words[:i])), len(" ".join(words[i:]))))
    return [" ".join(words[:best]), " ".join(words[best:])]


def public_logo_url(ch: dict, catalog=None) -> str:
    """A public https address for this channel's logo, for Plex apps."""
    holiday = ch.get("season") and not ch.get("logo")
    ref = "" if holiday else logo_ref(ch, catalog)
    known = _public().get(ref) if ref else None
    if known and known.get("v") == PUBLIC_V:
        return _fit(known["src"], known["aspect"], {"filt": "negate"} if known["dark"] else None)
    name = ch["name"]
    if holiday:
        fg = HOLIDAY_ON_DARK.get(ch["season"], CREAM)
    else:
        fg = _pick(name, ON_DARK.get(look_for(name, ch.get("group", "")), ON_DARK["atomic"]))
    lines = _two_lines(_caps(name))
    # Oswald capitals are about half as wide as they are tall; two lines stack about 2.2 high.
    aspect = 0.5 * max(len(line) for line in lines) / (1 if len(lines) == 1 else 2.2)
    letters = (f"placehold.co/1200x500/transparent/{fg[1:]}/png?"
               + urlencode({"text": "\\n".join(lines), "font": "oswald"}))
    return _fit(letters, aspect)


# ---- "Up next" banners in each network's 2000s style ------------------------------------------
# Not the networks' own artwork (Rerun doesn't have it): each is drawn in the spirit of that channel's
# on-air look in the 2000s, with the fonts Rerun ships. Others get the plain style above.

def theme_for(tag: str | None, plan: bool = False) -> str:
    if plan:
        return "25days"
    return {"nick": "nick", "nicktoons": "nick", "cn": "cn", "disney": "disney", "toondisney": "disney",
            "playhouse": "disney", "boomerang": "boomerang"}.get(tag or "", "")


# where each style sits: "bottom" runs along the bottom edge of the picture, the rest sit low on the left
# ("edge": right up against the picture's left edge, running off its right edge)
PLACE = {"nick": "edge", "disney": "bottom"}

# how each style comes on screen: from where it slides
MOTION = {"nick": "rise", "cn": "left", "disney": "left", "boomerang": "right", "25days": "up", "": "left"}


def _splat(cx, cy, r, seed=7, bumps=7):
    """A paint splat: a rounded blob with a few soft lobes."""
    import random as _r
    rng = _r.Random(seed)
    phases = [rng.random() * 6.28 for _ in range(3)]
    pts = []
    for i in range(240):
        a = 2 * math.pi * i / 240
        bump = (0.09 * math.sin(a * bumps + phases[0]) + 0.05 * math.sin(a * 3 + phases[1])
                + 0.03 * math.sin(a * 11 + phases[2]))
        pts.append((cx + r * (1 + bump) * math.cos(a) * 1.4, cy + r * (1 + bump) * math.sin(a)))
    return pts


def _fit(title, font, max_w):
    title = " ".join((title or "").split())
    while font.getlength(title) > max_w and len(title) > 4:
        title = title[:-2].rstrip(" ,.-:") + "…"
    return title


def _shadowed(img, pad, blur, alpha=120, dy=0.0):
    """A soft drop shadow under everything already drawn on img."""
    a = img.getchannel("A").point(lambda v: min(alpha, v))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.putalpha(a)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.alpha_composite(sh, (0, round(dy)))
    out.alpha_composite(img)
    return out


def _banner_nick(title, bh, k, art=None, logo=None):
    big = _font(round(bh * 0.5), "fun")
    tfont = _font(round(bh * 0.5), "fun")
    t = _fit(title, tfont, bh * 7.2)
    sr = bh * 0.58
    w = round(sr * 2.9 + tfont.getlength(t) + bh * 1.0)
    h = round(bh * 1.55)
    img = Image.new("RGBA", (w + 40 * k, h + 40 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = 20 * k + sr * 1.45, 20 * k + h / 2
    # the title on a dark, slightly tilted slab tucked behind the splat
    slab = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(slab)
    sd.rounded_rectangle((cx, cy - bh * 0.42, 20 * k + w, cy + bh * 0.42), round(bh * 0.2), fill=(30, 18, 60, 225))
    slab = slab.rotate(-2, resample=Image.BICUBIC, center=(cx, cy))
    img.alpha_composite(slab)
    d = ImageDraw.Draw(img)
    d.polygon(_splat(cx, cy, sr), fill="#f57d0d")
    d.ellipse((cx + sr * 1.25, cy - sr * 1.0, cx + sr * 1.45, cy - sr * 0.8), fill="#f57d0d")  # flecks of paint
    d.ellipse((cx - sr * 1.75, cy + sr * 0.5, cx - sr * 1.58, cy + sr * 0.67), fill="#f57d0d")
    label = _short("next")
    lw = big.getlength(label)
    lb = big.getbbox(label)
    d.text((cx - lw / 2, cy - (lb[3] + lb[1]) / 2), label, font=big, fill="white",
           stroke_width=round(bh * 0.05), stroke_fill="#7a2f00")
    tb = tfont.getbbox("Hg")
    d.text((cx + sr * 1.65, cy - (tb[3] + tb[1]) / 2 - bh * 0.02), t, font=tfont, fill="white",
           stroke_width=round(bh * 0.04), stroke_fill="#1a0f33")
    return _shadowed(img, 20 * k, bh * 0.08, 140, bh * 0.05)


def _banner_cn(title, bh, k, art=None, logo=None):
    lab = _font(round(bh * 0.3), "sans")
    tfont = _font(round(bh * 0.5), "sans")
    t = _fit(title.upper(), tfont, bh * 7.5)
    sq = round(bh * 0.16)
    tw = round(tfont.getlength(t) + bh * 0.8)
    said = _lab("COMING UP NEXT")
    lw = round(lab.getlength(said) + bh * 0.1)
    w = max(tw, lw + bh) + sq * 2
    h = round(bh * 1.25) + sq * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # the checkerboard edge, then a black slab
    for i in range(0, w // sq + 1):
        for j in range(2):
            if (i + j) % 2 == 0:
                d.rectangle((i * sq, j * sq, i * sq + sq - 1, j * sq + sq - 1), fill="black")
            else:
                d.rectangle((i * sq, j * sq, i * sq + sq - 1, j * sq + sq - 1), fill="white")
    d.rectangle((0, sq * 2, w, h), fill=(0, 0, 0, 235))
    d.text((bh * 0.4, sq * 2 + bh * 0.1), said, font=lab, fill="white")
    d.line((bh * 0.4, sq * 2 + bh * 0.5, bh * 0.4 + lw, sq * 2 + bh * 0.5), fill="white", width=k * 2)
    d.text((bh * 0.4, sq * 2 + bh * 0.55), t, font=tfont, fill="white")
    return img


def _banner_disney(title, bh, k, art=None, logo=None):
    lab = _font(round(bh * 0.32), "serif")
    tfont = _font(round(bh * 0.5), "sans")
    t = _fit(title, tfont, bh * 7.5)
    said = _lab("up next")
    w = round(max(tfont.getlength(t), lab.getlength(said) + bh) + bh * 1.4)
    h = round(bh * 1.3)
    pad = 24 * k
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    # a glowing blue-to-purple pill
    grad = Image.new("RGBA", (w, h))
    gd = ImageDraw.Draw(grad)
    for x in range(w):
        f = x / max(1, w - 1)
        gd.line((x, 0, x, h), fill=(round(24 + 70 * f), round(70 - 30 * f), round(190 + 20 * f), 235))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=255)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((pad, pad, pad + w, pad + h), h // 2, fill=(140, 170, 255, 160))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(bh * 0.18)))
    img.paste(grad, (pad, pad), mask)
    d = ImageDraw.Draw(img)
    # a little sparkle
    sx, sy, s = pad + bh * 0.62, pad + h * 0.36, bh * 0.16
    d.polygon([(sx, sy - s), (sx + s * 0.25, sy - s * 0.25), (sx + s, sy), (sx + s * 0.25, sy + s * 0.25), (sx, sy + s),
               (sx - s * 0.25, sy + s * 0.25), (sx - s, sy), (sx - s * 0.25, sy - s * 0.25)], fill="white")
    d.text((pad + bh * 0.9, pad + h * 0.12), said, font=lab, fill=(220, 230, 255, 255))
    d.text((pad + bh * 0.62, pad + h * 0.43), t, font=tfont, fill="white")
    return img


def _banner_boomerang(title, bh, k, art=None, logo=None):
    lab = _font(round(bh * 0.36), "fun")
    tfont = _font(round(bh * 0.46), "serif")
    t = _fit(title, tfont, bh * 7.2)
    burst_r = bh * 0.78
    tw = round(tfont.getlength(t) + bh * 1.0)
    h = round(bh * 1.1)
    w = round(burst_r * 2 + tw)
    pad = 16 * k
    img = Image.new("RGBA", (w + pad * 2, round(burst_r * 2) + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cy = pad + burst_r
    # cream card with a teal rule, the 1960s way
    d.rounded_rectangle((pad + burst_r, cy - h / 2, pad + w, cy + h / 2), round(bh * 0.12), fill=(246, 236, 214, 240))
    d.rectangle((pad + burst_r, cy + h / 2 - k * 6, pad + w - round(bh * 0.12), cy + h / 2 - k * 2), fill="#1d8f93")
    # an atomic starburst for "next"
    cx = pad + burst_r
    pts = []
    for i in range(24):
        r = burst_r if i % 2 == 0 else burst_r * 0.68
        a = math.pi * i / 12 - math.pi / 2
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill="#ef6f2e")
    for i, label in enumerate((_short("next!"),)):
        lw = lab.getlength(label)
        lb = lab.getbbox(label)
        d.text((cx - lw / 2, cy - (lb[3] + lb[1]) / 2), label, font=lab, fill="white", stroke_width=k, stroke_fill="#9b3a12")
    tb = tfont.getbbox("Hg")
    d.text((cx + burst_r + bh * 0.25, cy - (tb[3] + tb[1]) / 2 - k * 2), t, font=tfont, fill=(35, 28, 22, 255))
    return _shadowed(img, pad, bh * 0.07, 110, bh * 0.05)


def _banner_25days(title, bh, k, art=None, logo=None):
    lab = _font(round(bh * 0.3), "serif")
    tfont = _font(round(bh * 0.48), "serif")
    t = _fit(title, tfont, bh * 7.5)
    said = _lab("Coming up next")
    w = round(max(tfont.getlength(t), lab.getlength(said)) + bh * 1.6)
    h = round(bh * 1.3)
    pad = 14 * k
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((pad, pad, pad + w, pad + h), round(bh * 0.14), fill=(142, 18, 32, 238),
                        outline=(222, 184, 98, 255), width=k * 3)
    import random as _r
    rng = _r.Random(25)
    x_from = pad + bh * 0.5 + lab.getlength(said) + bh * 0.4
    for _ in range(6):  # a few snowflakes, above the title and right of the label
        x, y, s = rng.uniform(x_from, pad + w - bh * 0.25), pad + rng.uniform(bh * 0.18, h * 0.36), rng.uniform(3, 6) * k
        for a in range(3):
            ang = math.pi * a / 3
            d.line((x - s * math.cos(ang), y - s * math.sin(ang), x + s * math.cos(ang), y + s * math.sin(ang)),
                   fill=(255, 255, 255, 90), width=k)
    d.text((pad + bh * 0.5, pad + h * 0.1), said, font=lab, fill=(236, 205, 130, 255))
    d.text((pad + bh * 0.5, pad + h * 0.42), t, font=tfont, fill="white")
    return _shadowed(img, pad, bh * 0.07, 110, bh * 0.05)



def _banner_nick_bar(title, bh, k, art=None, logo=None, parts=None):
    """Nickelodeon in the 2000s: a full-width orange plaid bar along the bottom with a big white UP NEXT,
    the show's name under it, and the show's own artwork popping up out of the bar where Nick put its
    characters. As wide as a widescreen picture; a 4:3 picture just shows the left part. With `parts`,
    the artwork is handed back on its own (parts["art"]) so it can be placed from the picture's right
    edge, whatever shape the picture is."""
    H = round(bh / 0.1)                     # the picture height this is drawn for
    W43, W = round(H * 4 / 3), round(H * 16 / 9)
    bar_h = round(H * 0.2)
    lift = round(bar_h * 0.75) if art else 0  # room for the artwork to stick up out of the bar
    img = Image.new("RGBA", (W, bar_h + lift), (0, 0, 0, 0))
    bar = Image.new("RGBA", (W, bar_h), "#f26a12")
    plaid = Image.new("RGBA", (W, bar_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(plaid)
    band = round(bar_h * 0.28)
    for x in range(-bar_h, W + bar_h, band * 2):  # diagonal gingham, like the 2000s on-air graphics
        pd.polygon([(x, 0), (x + band, 0), (x + band - bar_h, bar_h), (x - bar_h, bar_h)], fill=(255, 176, 92, 70))
        pd.polygon([(x, bar_h), (x + band, bar_h), (x + band - bar_h, 0), (x - bar_h, 0)], fill=(255, 176, 92, 70))
    for x in range(-bar_h, W + bar_h, band):
        pd.line((x, 0, x - bar_h, bar_h), fill=(255, 240, 220, 60), width=k)
    bar.alpha_composite(plaid)
    bd = ImageDraw.Draw(bar)
    bd.rectangle((0, 0, W, k * 3), fill=(255, 255, 255, 235))           # a white edge on top
    bd.rectangle((0, bar_h - k * 4, W, bar_h), fill=(160, 60, 0, 255))   # and a darker one below
    img.alpha_composite(bar, (0, lift))
    d = ImageDraw.Draw(img)
    big = _font(round(bar_h * 0.42), "sans")
    small = _font(round(bar_h * 0.2), "sans")
    said = _lab("UP NEXT")
    if big.getlength(said) > W43 * 0.55:  # "TONIGHT AT 8:30" is longer than "UP NEXT"
        big = _font(round(bar_h * 0.42 * W43 * 0.55 / big.getlength(said)), "sans")
    x0 = round(W43 * 0.07)
    art_w = 0
    if art:
        try:
            poster = Image.open(io.BytesIO(art)).convert("RGBA")
            ph = round((bar_h + lift) * 0.92)
            poster = poster.resize((round(ph * poster.width / poster.height), ph), Image.LANCZOS)
            card = Image.new("RGBA", (poster.width + k * 8, poster.height + k * 8), "white")
            card.alpha_composite(poster, (k * 4, k * 4))
            card = card.rotate(4, resample=Image.BICUBIC, expand=True)
            card = _shadowed(card, 0, bh * 0.06, 150, bh * 0.03)
            art_w = card.width
            if parts is not None:
                parts["art"] = card
            else:
                img.alpha_composite(card, (W43 - card.width - round(W43 * 0.05), img.height - card.height - k * 4))
        except Exception:
            art_w = 0
    label_y = lift + bar_h * 0.12
    d.text((x0 + k * 3, label_y + k * 3), said, font=big, fill=(150, 55, 0, 255))   # a hard drop shadow
    d.text((x0, label_y), said, font=big, fill="white")
    t = _fit(title, small, W43 - x0 - art_w - W43 * 0.1)
    d.text((x0 + k, lift + bar_h * 0.62), t, font=small, fill=(255, 248, 236, 255))
    return img


def _ears(d, cx, cy, r, width, fill, outline):
    """Three circles: a head and two ears, drawn as one outlined shape."""
    er = r * 0.62
    shapes = [(cx, cy, r), (cx - r * 0.92, cy - r * 0.92, er), (cx + r * 0.92, cy - r * 0.92, er)]
    for x, y, rr in shapes:  # the outline first, all three, then the fill over it
        d.ellipse((x - rr - width, y - rr - width, x + rr + width, y + rr + width), fill=outline)
    for x, y, rr in shapes:
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill)


def _banner_disney_2000s(title, bh, k, art=None, logo=None):
    """Disney Channel in the 2000s: the channel's logo inside an outline of Mickey ears at the bottom
    left, and a royal-blue bar with light blue edges running out to the right: "Coming next  Show"."""
    H = round(bh / 0.1)
    r = round(H * 0.118)                          # the head circle
    ring = max(3, round(r * 0.11))
    bar_h = round(H * 0.085)
    font = _font(round(bar_h * 0.52), "sans")
    label, gap = _lab("Coming next"), bar_h * 0.6
    bar_x0 = round(r * 1.55)
    text_x = round(r * 1.05) + r * 1.15 + bar_h * 0.6        # where the words start, just right of the ears
    room = H * 4 / 3 * 0.96 - text_x - font.getlength(label) - gap - bar_h * 0.9  # the bar fits a 4:3 picture
    t = _fit(title, font, room)
    text_w = font.getlength(label) + gap + font.getlength(t)
    w = round(text_x + text_w + bar_h * 0.9)
    head_cx, head_cy = round(r * 1.05), round(r * 1.95)
    hgt = round(head_cy + r + ring + k * 2)
    img = Image.new("RGBA", (w, hgt), (0, 0, 0, 0))
    # the bar, behind the ears: dark royal blue with light blue edges
    by0 = round(head_cy - bar_h * 0.15)
    bar = Image.new("RGBA", (w - bar_x0, bar_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for y in range(bar_h):  # a little lighter at the top, like a lit plastic bar
        f = y / max(1, bar_h - 1)
        bd.line((0, y, w, y), fill=(round(40 - 14 * f), round(36 - 12 * f), round(190 - 30 * f), 238))
    edge = max(2, round(bar_h * 0.09))
    bd.rectangle((0, 0, w, edge), fill=(120, 170, 255, 255))
    bd.rectangle((0, bar_h - edge, w, bar_h), fill=(70, 140, 235, 255))
    img.alpha_composite(bar, (bar_x0, by0))
    d = ImageDraw.Draw(img)
    # the ears: a teal outline, the inside a soft see-through cream
    _ears(d, head_cx, head_cy, r, ring, (236, 226, 196, 120), (78, 196, 214, 255))
    inner = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _ears(ImageDraw.Draw(inner), head_cx, head_cy, r - ring * 1.6, max(2, ring // 2), (0, 0, 0, 0), (40, 150, 175, 200))
    img.alpha_composite(inner)
    # the channel's logo inside the head, in warm cream
    if logo and os.path.isfile(logo):
        try:
            mark = Image.open(logo).convert("RGBA")
            a = mark.getchannel("A").point(lambda v: min(255, v * 2))
            box = r * 1.45
            sc = min(box / mark.width, box * 0.62 / mark.height)
            size = (max(1, round(mark.width * sc)), max(1, round(mark.height * sc)))
            tint = Image.new("RGBA", size, (246, 232, 190, 255))
            tint.putalpha(a.resize(size, Image.LANCZOS))
            img.alpha_composite(tint, (round(head_cx - size[0] / 2), round(head_cy - size[1] / 2 + r * 0.1)))
        except Exception:
            pass
    # "Coming next   The Proud Family", in lavender
    tb = font.getbbox("Hg")
    ty = by0 + (bar_h - (tb[3] - tb[1])) / 2 - tb[1]
    x = text_x
    d.text((x, ty), label, font=font, fill=(214, 206, 255, 255))
    d.text((x + font.getlength(label) + gap, ty), t, font=font, fill=(236, 232, 255, 255))
    return img

THEME_DRAW = {"nick": _banner_nick_bar, "nick_splat": _banner_nick, "cn": _banner_cn, "disney": _banner_disney_2000s, "disney_glow": _banner_disney, "boomerang": _banner_boomerang,
              "25days": _banner_25days}


def make_themed_banner(title: str, height: int, theme: str, art: bytes | None = None, logo: str | None = None,
                       parts: dict | None = None) -> bytes:
    """The banner as a PNG. With `parts`, a style whose artwork is placed on its own (Nick's) leaves it out
    and puts it in parts["art"] as a PNG."""
    k = 2
    bh = round(height * 0.1) * k
    if THEME_DRAW[theme] is _banner_nick_bar and parts is not None:
        img = _banner_nick_bar(title, bh, k, art, logo, parts)
        if parts.get("art") is not None:
            a = parts["art"]
            parts["art"] = _png(a.resize((max(1, a.width // k), max(1, a.height // k)), Image.LANCZOS))
    else:
        img = THEME_DRAW[theme](title, bh, k, art, logo)
    img = img.crop(img.getbbox() or (0, 0, 1, 1))
    return _png(img.resize((max(1, img.width // k), max(1, img.height // k)), Image.LANCZOS))


# ---- your own "Up next" templates ---------------------------------------------------------------
# A green-screen picture or video in Rerun's data folder under bugs/custom/upnext/, named after a show
# ("SpongeBob SquarePants.png", used whenever it's up next) or a network ("Nickelodeon.mp4"). The green
# (or black, for templates on black) is see-through. When the green is a window rather than the whole
# background, the show squeezes into it, the way networks squeezed the credits for a promo.

TEMPLATE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv", ".m4v")
VIDEO_EXT_T = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def find_template(names: list[str]) -> str | None:
    """The first template named after one of `names` (show first, then network), ignoring case and
    punctuation: "spongebob squarepants.PNG" is SpongeBob SquarePants's."""
    folder = BUG_DIR / "custom" / "upnext"
    if not folder.is_dir():
        return None
    files = {}
    for f in folder.iterdir():
        if f.suffix.lower() in TEMPLATE_EXT and f.is_file():
            files.setdefault(_slug(f.stem), str(f))
    for n in names:
        if n and _slug(n) in files:
            return files[_slug(n)]
    return None


def analyse_template(path: str, ffmpeg: str = "ffmpeg") -> dict | None:
    """How to use a template: its size and length, the colour to make see-through, and the window the
    show squeezes into (as fractions of the picture), if the see-through part is a window. Remembered
    until the file changes."""
    import json
    import subprocess
    cache = path + ".rerun.json"
    mtime = int(os.path.getmtime(path))
    try:
        info = json.load(open(cache, encoding="utf-8"))
        if info.get("m") == mtime:
            return info
    except (OSError, ValueError):
        pass
    video = path.lower().endswith(VIDEO_EXT_T)
    dur = 0.0
    if video:
        probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if os.path.dirname(ffmpeg) else "ffprobe"
        out = subprocess.run([probe if os.path.exists(probe) or os.path.exists(probe + ".exe") else "ffprobe",
                              "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True).stdout.strip()
        try:
            dur = float(out)
        except ValueError:
            return None
        raw = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{min(dur * 0.4, 3):.2f}", "-i", path,
                              "-frames:v", "1", "-f", "image2", "-c:v", "png", "pipe:1"], capture_output=True).stdout
        if not raw:
            return None
        frame = Image.open(io.BytesIO(raw)).convert("RGB")
    else:
        frame = Image.open(path).convert("RGB")
    w, h = frame.size
    small = frame.resize((160, max(1, round(160 * h / w))))
    sw, sh = small.size
    px = list(small.getdata())
    green = [(i, c) for i, c in enumerate(px) if c[1] > 90 and c[1] > c[0] * 1.35 and c[1] > c[2] * 1.35]
    black = [i for i, c in enumerate(px) if max(c) < 28]
    info = {"m": mtime, "video": video, "dur": round(dur, 2), "w": w, "h": h, "window": None}
    if len(green) > len(px) * 0.12:
        cols = sorted(c for _i, c in green)
        r, g, b = (sorted(c[k] for c in cols)[len(cols) // 2] for k in range(3))
        info.update(key=f"0x{r:02x}{g:02x}{b:02x}", how="green")
        xs = [i % sw for i, _c in green]
        ys = [i // sw for i, _c in green]
        x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
        if (x1 - x0) < sw * 0.92 and (y1 - y0) < sh * 0.92 and (x1 - x0) * (y1 - y0) > sw * sh * 0.2:
            info["window"] = [x0 / sw, y0 / sh, (x1 - x0) / sw, (y1 - y0) / sh]  # the show squeezes in here
    elif len(black) > len(px) * 0.4:
        info.update(key="0x000000", how="black")
    else:
        return None  # nothing to see through
    try:
        json.dump(info, open(cache, "w", encoding="utf-8"))
    except OSError:
        pass
    return info


def template_title(title: str, path: str, height: int) -> tuple[str, dict] | None:
    """The next show's name for a template, if its settings file (same name, ending .json) asks for it:
    {"title": {"x": 0.42, "y": 0.9, "size": 0.05, "color": "#ffffff"}}, in fractions of the picture."""
    import json
    try:
        spec = json.load(open(os.path.splitext(path)[0] + ".json", encoding="utf-8")).get("title")
    except (OSError, ValueError, AttributeError):
        return None
    if not spec or not title:
        return None
    size = max(12, round(height * float(spec.get("size", 0.05))))
    font = _font(size, "sans")
    w = round(font.getlength(title)) + 8
    img = Image.new("RGBA", (w, round(size * 1.4)), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((4, 0), title, font=font, fill=spec.get("color", "#ffffff"))
    folder = BUG_DIR / "banners"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"t-{hashlib.sha1(f'{title}:{height}:{json.dumps(spec)}'.encode()).hexdigest()[:16]}.png"
    if not out.exists():
        out.write_bytes(_png(img))
    return str(out), {"x": float(spec.get("x", 0.1)), "y": float(spec.get("y", 0.85))}
