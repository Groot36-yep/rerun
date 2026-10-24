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
from urllib.parse import urlencode

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


async def official_logo(ref: str) -> bytes | None:
    for attempt in range(3):  # a brief network hiccup shouldn't leave a channel with a stand-in logo
        try:
            if ref.startswith("tmdb:/"):
                return await _fetch_tmdb(ref[5:])
            if ref.startswith("wiki:"):
                return await _fetch_wiki(ref[5:])
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
