"""'Guess the show!': a trivia card in a kids' channel's break, the way kids' networks quizzed viewers
between the cartoons.

The question shows a close-up from one of the channel's own shows ("Guess the show!"); the answer, at
the end of the same break, shows the whole picture and the show's name. Both are drawn in the network's
style (see THEMES), or over a background of your choosing: any clip or picture in Rerun's data folder
under bugs/custom/trivia/, named after the network or its tag ("Nickelodeon.mp4"), or picked on the
On-air look page. A real trivia bumper from YouTube makes the look exact.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import subprocess

from PIL import Image, ImageDraw, ImageFilter, ImageStat

from .store import DATA_DIR

log = logging.getLogger("rerun.trivia")

STILLS = DATA_DIR / "trivia"
CARD_V = 1
BG_DIR = DATA_DIR / "bugs" / "custom" / "trivia"
BG_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".png", ".jpg", ".jpeg", ".webp")

# Each network's colours: background, its pattern, the accent shape, and the lettering.
THEMES = {
    "nick": {"bg": (245, 125, 13), "bg2": (255, 168, 76), "accent": (92, 42, 134), "ink": (255, 255, 255),
             "shadow": (150, 55, 0), "font": "fun", "decor": "plaid", "shape": "splat"},
    "cn": {"bg": (12, 12, 12), "bg2": (40, 40, 40), "accent": (255, 255, 255), "ink": (255, 255, 255),
           "shadow": (0, 0, 0), "font": "sans", "decor": "checker", "shape": "box", "upper": True},
    "disney": {"bg": (24, 60, 190), "bg2": (110, 50, 200), "accent": (255, 255, 255), "ink": (255, 255, 255),
               "shadow": (10, 20, 90), "font": "sans", "decor": "sparkle", "shape": "circle"},
    "boomerang": {"bg": (246, 236, 214), "bg2": (29, 143, 147), "accent": (239, 111, 46), "ink": (35, 28, 22),
                  "shadow": (200, 186, 160), "font": "serif", "decor": "rule", "shape": "burst"},
    "25days": {"bg": (142, 18, 32), "bg2": (222, 184, 98), "accent": (222, 184, 98), "ink": (255, 255, 255),
               "shadow": (70, 5, 12), "font": "serif", "decor": "snow", "shape": "circle"},
    "": {"bg": (21, 17, 30), "bg2": (60, 48, 80), "accent": (212, 160, 40), "ink": (255, 253, 248),
         "shadow": (0, 0, 0), "font": "sans", "decor": "", "shape": "circle"},
}


def _font(size, face):
    from .images import _font as f
    return f(max(8, int(size)), face)


def background(names: list[str], chosen: str = "") -> str | None:
    """The trivia background: the one picked on the On-air look page, or one named after the network."""
    if chosen == "none":
        return None
    if chosen.startswith("file:"):
        p = BG_DIR / chosen[5:]
        return str(p) if p.is_file() else None
    if not BG_DIR.is_dir():
        return None
    from .images import _slug
    files = {_slug(f.stem): str(f) for f in BG_DIR.iterdir() if f.suffix.lower() in BG_EXT and f.is_file()}
    return next((files[_slug(n)] for n in names if n and _slug(n) in files), None)


def has_audio(path: str, ffprobe: str = "ffprobe") -> bool:
    if not path or path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        return False
    cache = path + ".rerun-audio.json"
    try:
        hit = json.load(open(cache, encoding="utf-8"))
        if hit.get("m") == int(os.path.getmtime(path)):
            return bool(hit["a"])
    except (OSError, ValueError, KeyError):
        pass
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20).stdout
        a = bool(out.strip())
    except (OSError, subprocess.TimeoutExpired):
        a = False
    try:
        json.dump({"m": int(os.path.getmtime(path)), "a": a}, open(cache, "w", encoding="utf-8"))
    except OSError:
        pass
    return a


async def still(app, item: dict) -> bytes | None:
    """A moment from the episode (made once and kept), avoiding a black frame between scenes."""
    src = app.media_input(item)
    if not src:
        return None
    STILLS.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{item.get('m')}:{item.get('in')}".encode()).hexdigest()[:16]
    path = STILLS / f"{key}.jpg"
    if path.exists():
        return path.read_bytes()
    at = float(item.get("in") or 0)
    for t in (at, at * 0.8, at * 1.15, at * 0.6):
        cmd = [app.ffmpeg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}",
               "-i", src if src.startswith("http") else "file:" + src, "-frames:v", "1",
               "-vf", "scale=-2:540", "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=60)
        if res.returncode == 0 and res.stdout:
            img = Image.open(io.BytesIO(res.stdout)).convert("L")
            st = ImageStat.Stat(img)
            if st.mean[0] > 28 and st.stddev[0] > 18:  # not a black or flat frame
                path.write_bytes(res.stdout)
                return res.stdout
    return None


def _decor(img, d, th, W, H):
    k = H / 720
    if th["decor"] == "plaid":
        band = int(70 * k)
        for x in range(-H, W + H, band * 2):
            d.polygon([(x, 0), (x + band, 0), (x + band - H, H), (x - H, H)], fill=th["bg2"] + (60,))
            d.polygon([(x, H), (x + band, H), (x + band - H, 0), (x - H, 0)], fill=th["bg2"] + (60,))
    elif th["decor"] == "checker":
        sq = int(28 * k)
        for y in (0, H - sq * 2):
            for i in range(W // sq + 1):
                for j in range(2):
                    d.rectangle((i * sq, y + j * sq, i * sq + sq - 1, y + j * sq + sq - 1),
                                fill=(255, 255, 255) if (i + j) % 2 else (0, 0, 0))
    elif th["decor"] == "sparkle":
        for x in range(W):
            f = x / W
            c = tuple(int(th["bg"][i] * (1 - f) + th["bg2"][i] * f) for i in range(3))
            d.line((x, 0, x, H), fill=c)
        import random
        rng = random.Random(7)
        for _ in range(28):
            sx, sy, s = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(4, 14) * k
            d.polygon([(sx, sy - s), (sx + s * .25, sy - s * .25), (sx + s, sy), (sx + s * .25, sy + s * .25),
                       (sx, sy + s), (sx - s * .25, sy + s * .25), (sx - s, sy), (sx - s * .25, sy - s * .25)],
                      fill=(255, 255, 255, 170))
    elif th["decor"] == "rule":
        d.rectangle((0, H - int(36 * k), W, H - int(22 * k)), fill=th["bg2"])
        d.rectangle((0, int(22 * k), W, int(36 * k)), fill=th["bg2"])
    elif th["decor"] == "snow":
        import random
        rng = random.Random(25)
        for _ in range(60):
            x, y, s = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(3, 8) * k
            for a in range(3):
                ang = math.pi * a / 3
                d.line((x - s * math.cos(ang), y - s * math.sin(ang), x + s * math.cos(ang), y + s * math.sin(ang)),
                       fill=(255, 255, 255, 110), width=max(1, int(2 * k)))


def _shape(d, th, cx, cy, r):
    if th["shape"] == "splat":
        from .images import _splat
        d.polygon(_splat(cx, cy, r / 1.4, seed=11), fill=th["accent"])
    elif th["shape"] == "burst":
        pts = []
        for i in range(24):
            rr = r if i % 2 == 0 else r * 0.68
            a = math.pi * i / 12 - math.pi / 2
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(pts, fill=th["accent"])
    elif th["shape"] == "box":
        d.rectangle((cx - r, cy - r, cx + r, cy + r), fill=th["accent"])
    else:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=th["accent"])


def card(still_jpg: bytes | None, phase: str, answer: str, theme: str, W: int, H: int, on_bg: bool) -> bytes:
    """The question or answer card as a W x H picture. With on_bg, only the still and the lettering
    (see-through around them), to go over a background clip."""
    th = THEMES.get(theme) or THEMES[""]
    k = H / 720
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0) if on_bg else th["bg"] + (255,))
    d = ImageDraw.Draw(img, "RGBA")
    if not on_bg:
        _decor(img, d, th, W, H)
    up = (lambda t: t.upper()) if th.get("upper") else (lambda t: t)
    # the still, in a white frame, tilted a little
    if still_jpg:
        pic = Image.open(io.BytesIO(still_jpg)).convert("RGB")
        if phase == "ask":  # a close-up: harder to guess
            w, h = pic.size
            pic = pic.crop((int(w * 0.22), int(h * 0.18), int(w * 0.78), int(h * 0.82)))
        ph = int(H * 0.52)
        pic = pic.resize((int(ph * pic.width / pic.height), ph), Image.LANCZOS)
        b = int(10 * k)
        framed = Image.new("RGBA", (pic.width + b * 2, pic.height + b * 2), (255, 255, 255, 255))
        framed.paste(pic, (b, b))
        framed = framed.rotate(-3 if phase == "ask" else 2, resample=Image.BICUBIC, expand=True)
        sh = Image.new("RGBA", framed.size, (0, 0, 0, 0))
        sh.putalpha(framed.getchannel("A").point(lambda v: min(v, 120)))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12 * k)), (int(W * 0.08) + int(8 * k), int(H * 0.25) + int(12 * k)))
        img.alpha_composite(framed, (int(W * 0.08), int(H * 0.25)))
        pic_right = int(W * 0.08) + framed.width
    else:
        pic_right = int(W * 0.08)
    d = ImageDraw.Draw(img, "RGBA")
    head = _font(92 * k, th["font"])
    line = up("Guess the show!") if phase == "ask" else up("It's…")
    hx, hy = int(W * 0.08), int(H * 0.08)
    d.text((hx + int(5 * k), hy + int(5 * k)), line, font=head, fill=th["shadow"] + (255,))
    d.text((hx, hy), line, font=head, fill=th["ink"] + (255,))
    cx, cy = (pic_right + W) / 2, H * 0.58
    room = W - pic_right - W * 0.06
    if phase == "ask":
        r = min(room * 0.42, H * 0.26)
        _shape(d, th, cx, cy, r)
        q = _font(r * 1.4, th["font"])
        qb = q.getbbox("?")
        ink = (255, 255, 255) if th["accent"] != (255, 255, 255) else (0, 0, 0)
        d.text((cx - (qb[2] + qb[0]) / 2, cy - (qb[3] + qb[1]) / 2), "?", font=q, fill=ink + (255,))
        small = _font(34 * k, "sans")
        tip = up("The answer's after the break")
        tw = small.getlength(tip)
        d.text((max(int(W * 0.08), (W - tw) / 2), H * 0.85), tip, font=small, fill=th["ink"] + (230,))
    else:
        name = up(answer or "?")
        size = 84 * k
        while True:  # big, on up to three lines; smaller only when even that doesn't fit
            f = _font(size, th["font"])
            lines, cur = [], ""
            for w in name.split():
                if not cur or f.getlength(cur + " " + w) <= room:
                    cur = (cur + " " + w).strip()
                else:
                    lines, cur = lines + [cur], w
            lines.append(cur)
            if (len(lines) <= 3 and all(f.getlength(ln) <= room for ln in lines)) or size < 30 * k:
                break
            size *= 0.9
        lh = size * 1.15
        y = cy - lh * len(lines) / 2
        for ln in lines:
            lw = f.getlength(ln)
            d.text((cx - lw / 2 + 4 * k, y + 4 * k), ln, font=f, fill=th["shadow"] + (255,))
            d.text((cx - lw / 2, y), ln, font=f, fill=th["ink"] + (255,))
            y += lh
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def make(app, item: dict, theme: str, W: int, H: int, bg: str | None) -> str | None:
    """The card's file for this break's question or answer, made once and kept."""
    q = item.get("q") or {}
    shot = await still(app, item)
    if not shot and q.get("phase") == "ask":
        return None  # a question with nothing to look at isn't a question
    key = hashlib.sha1(f"{CARD_V}:{item.get('m')}:{item.get('in')}:{q.get('phase')}:{q.get('answer')}:{theme}:"
                       f"{W}x{H}:{bool(bg)}".encode()).hexdigest()[:16]
    STILLS.mkdir(parents=True, exist_ok=True)
    path = STILLS / f"card-{key}.png"
    if not path.exists():
        png = await asyncio.to_thread(card, shot, q.get("phase", "ask"), q.get("answer", ""), theme, W, H, bool(bg))
        path.write_bytes(png)
    return str(path)
