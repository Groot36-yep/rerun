"""Holidays: finding holiday episodes and movies, and knowing when each season is on.

An episode counts as a Christmas episode if its title or summary says so (or it's in a
Plex collection with "Christmas" in the name). During the season, regular channels
sprinkle these in, and an optional holiday channel plays nothing else.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

PRESETS: dict[str, dict] = {
    "valentines": {
        "name": "Valentine's Day", "icon": "❤️", "start": "02-01", "end": "02-14",
        "title": ["valentine", "valentine's", "valentines", "cupid", "be my valentine", "sweetheart"],
        "summary": ["valentine's day", "valentines day", "valentine"],
        "collections": ["valentine", "romance"], "genres": [],
    },
    "stpatricks": {
        "name": "St. Patrick's Day", "icon": "☘️", "start": "03-10", "end": "03-17",
        "title": ["st. patrick", "saint patrick", "st patrick", "leprechaun", "shamrock"],
        "summary": ["st. patrick's day", "saint patrick's day", "st patrick's day", "leprechaun"],
        "collections": ["st. patrick", "irish"], "genres": [],
    },
    "easter": {
        "name": "Easter", "icon": "\U0001f430", "start": "03-25", "end": "04-20",
        "title": ["easter", "egg hunt"], "summary": ["easter"],
        "collections": ["easter"], "genres": [],
    },
    "july4": {
        "name": "Fourth of July", "icon": "\U0001f386", "start": "06-30", "end": "07-04",
        "title": ["fourth of july", "4th of july", "independence day", "fireworks"],
        "summary": ["fourth of july", "4th of july", "independence day"],
        "collections": ["fourth of july", "independence"], "genres": [],
    },
    "halloween": {
        "name": "Halloween", "icon": "\U0001f383", "start": "10-01", "end": "10-31",
        "title": ["halloween", "treehouse of horror", "spooky", "haunted", "haunted house", "ghost", "ghosts",
                  "witch", "witches", "vampire", "zombie", "zombies", "werewolf", "frankenstein", "dracula",
                  "trick or treat", "trick-or-treat", "pumpkin", "monster mash", "scary"],
        "summary": ["halloween", "trick-or-treat", "trick or treat", "trick-or-treating", "haunted house",
                    "costume party"],
        "collections": ["halloween", "horror", "spooky", "scary"], "genres": ["Horror"],
    },
    "thanksgiving": {
        "name": "Thanksgiving", "icon": "\U0001f983", "start": "11-15", "end": "11-30",
        "title": ["thanksgiving", "turkey", "pilgrim", "pilgrims", "gobble"],
        "summary": ["thanksgiving"],
        "collections": ["thanksgiving"], "genres": [],
    },
    "christmas": {
        "name": "Christmas & Holidays", "icon": "\U0001f384", "start": "12-01", "end": "12-26",
        "title": ["christmas", "xmas", "x-mas", "santa", "santa claus", "holiday", "holidays", "noel", "yule",
                  "jingle", "reindeer", "rudolph", "frosty", "grinch", "nutcracker", "mistletoe", "snowman",
                  "sleigh", "elf", "scrooge", "christmas carol", "silent night", "winter wonderland",
                  "hanukkah", "chanukah", "kwanzaa", "north pole", "stocking", "gingerbread"],
        "summary": ["christmas", "xmas", "santa claus", "christmas eve", "christmas day", "hanukkah",
                    "chanukah", "kwanzaa", "holiday season", "north pole"],
        "collections": ["christmas", "xmas", "holiday", "holidays"], "genres": ["Holiday"],
    },
    "newyear": {
        "name": "New Year's", "icon": "\U0001f389", "start": "12-28", "end": "01-01",
        "title": ["new year", "new year's", "new years", "auld lang syne"],
        "summary": ["new year's eve", "new year's day", "new years eve"],
        "collections": ["new year"], "genres": [],
    },
}

DEFAULT_HOLIDAYS = {
    key: {"enabled": True, "start": p["start"], "end": p["end"], "words": [], "mix": True}
    for key, p in PRESETS.items()
}

MIX_CHANCE = 0.4  # during a holiday, 40% of a show's slots on regular channels pick a holiday episode

_DATE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_cache: dict = {}


def valid_date(s: str) -> bool:
    return bool(_DATE.match(s or ""))


def _rx(words):
    words = [w.strip() for w in words if w and w.strip()]
    if not words:
        return None
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) +
                      r")(?![\w])", re.I)


def in_window(conf: dict, t: float) -> bool:
    if not conf or not conf.get("enabled", True):
        return False
    md = time.strftime("%m-%d", time.localtime(t))
    s, e = conf.get("start", "01-01"), conf.get("end", "12-31")
    return s <= md <= e if s <= e else (md >= s or md <= e)


def next_start(conf: dict, t: float) -> float | None:
    """When the season next begins (local midnight), or None if it's switched off."""
    if not conf or not conf.get("enabled", True):
        return None
    day = datetime.fromtimestamp(t).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(370):
        day += timedelta(days=1)
        if in_window(conf, day.timestamp()):
            return day.timestamp()
    return None


def active(holidays: dict, t: float) -> list[str]:
    return [k for k, conf in (holidays or {}).items() if k in PRESETS and in_window(conf, t)]


def matches(cat, key: str, conf: dict) -> dict:
    """Ids of episodes and movies that belong to a holiday. Cached until the next scan."""
    preset = PRESETS.get(key)
    if not preset:
        return {"episodes": set(), "movies": set()}
    ck = (id(cat), cat.scanned_at, key, json.dumps(conf.get("words") or [], sort_keys=True))
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    extra = conf.get("words") or []
    title_rx = _rx(preset["title"] + extra)
    sum_rx = _rx(preset["summary"] + extra)
    col_rx = _rx(preset["collections"] + extra)
    genres = {g.lower() for g in preset["genres"]}

    def hit_text(rx, text):
        return bool(rx and text and rx.search(text))

    eps, movies = set(), set()
    for sid, show in cat.shows.items():
        whole = hit_text(sum_rx, show.get("title")) or any(hit_text(col_rx, c) for c in show.get("collections", []))
        for e in cat.episodes.get(sid, []):
            if whole or hit_text(title_rx, e.get("title")) or hit_text(sum_rx, e.get("summary")):
                eps.add(e["id"])
    for m in cat.movies.values():
        if (hit_text(title_rx, m.get("title")) or hit_text(sum_rx, m.get("summary"))
                or any(hit_text(col_rx, c) for c in m.get("collections", []))
                or genres & {g.lower() for g in m.get("genres", [])}):
            movies.add(m["id"])
    out = {"episodes": eps, "movies": movies}
    if len(_cache) > 64:
        _cache.clear()
    _cache[ck] = out
    return out


def friendly_date(md: str) -> str:
    try:
        d = datetime.strptime("2000-" + md, "%Y-%m-%d")
    except ValueError:
        return md
    return f"{d:%B} {d.day}"
