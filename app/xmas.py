"""Christmas on Rerun: ABC Family's 25 Days of Christmas, played at the times it aired, and the 2000s
Christmas channel.

- `find()` looks a title up in your library (movies, TV specials filed as movies, or episodes) and
  `wants()` turns a list of titles into "have" / "need to get".
- `plan_slots()` lays a year's 25 Days schedule onto this December's calendar.
- `daypart()` and `kind_of()` give the Christmas channel a 2000s cable-TV day: cartoons and specials
  in the morning, sitcom Christmas episodes in the afternoon, a big movie at 8, and on Christmas Eve
  at 8pm the 24-hour "A Christmas Story" marathon.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from . import xmasdata

_cache: dict = {}
_STOP = {"the", "a", "an", "and", "of"}
_GENERIC = {"christmas", "santa", "claus", "clause", "holiday", "story", "frosty", "rudolph", "night", "winter",
            "movie", "special", "little"}
_SPECIALS_SHOW = re.compile(r"christmas|holiday|xmas|special|rankin|classic|seuss|peanuts|garfield|disney|"
                            r"winnie|pooh|mickey|hanna|cartoon|animated", re.I)


def norm(s: str) -> str:
    """"Dr. Seuss' How the Grinch Stole Christmas!" -> "how grinch stole christmas"."""
    s = (s or "").lower().replace("&", " and ").replace("’", "'")
    s = re.sub(r"^(dr\.? seuss'?s?|disney'?s|walt disney'?s|rankin[/ -]bass'?s?|abc family'?s?)\s+", "", s)
    s = re.sub(r"\((19|20)\d\d\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("'", ""))
    words = [w for w in s.split() if w not in _STOP]
    return " ".join(words)


def _index(cat) -> dict:
    """Your library by normalized title (cached until the next scan)."""
    key = (id(cat), cat.scanned_at)
    hit = _cache.get("idx")
    if hit and hit[0] == key:
        return hit[1]
    movies: dict[str, list] = {}
    for m in cat.movies.values():
        movies.setdefault(norm(m.get("title", "")), []).append(m)
    shows: dict[str, list] = {}
    for s in cat.shows.values():
        shows.setdefault(norm(s.get("title", "")), []).append(s)
    eps: dict[str, list] = {}
    for sid, lst in cat.episodes.items():
        for e in lst:
            eps.setdefault(norm(e.get("title", "")), []).append(e)
    idx = {"movies": movies, "shows": shows, "eps": eps}
    _cache["idx"] = (key, idx)
    return idx


def _year_ok(have: int, want: list[int], slack: int = 1) -> bool:
    return not have or not want or any(abs(int(have) - w) <= slack for w in want)


def find(cat, title: str, years: list[int] | None = None, kind: str = "movie", alt: list[str] | None = None,
         show: str = "") -> dict | None:
    """The library item for a title: {"id", "title", "how"}; None if you don't have it."""
    idx = _index(cat)
    years = [int(y) for y in years or [] if y]
    names = [norm(t) for t in [title, *(alt or [])] if t]
    if kind == "episode":
        want_show = norm(show)
        for sn, shows in idx["shows"].items():
            if sn != want_show and not (want_show and (want_show in sn or sn in want_show)):
                continue
            for s in shows:
                for e in cat.episodes.get(s["id"], []):
                    en = norm(e.get("title", ""))
                    if en and any(en == n or (len(n) > 6 and (n in en or en in n)) for n in names):
                        return {"id": e["id"], "title": f"{s['title']} – {e['title']}", "how": "episode"}
        return None
    for n in names:  # a movie (TV specials are often filed as movies)
        for m in idx["movies"].get(n, []):
            if _year_ok(m.get("year"), years):
                return {"id": m["id"], "title": f"{m['title']} ({m.get('year') or '?'})", "how": "movie"}
    if kind == "special":
        for n in names:  # or as an episode of a "Christmas specials" show
            for e in idx["eps"].get(n, []):
                show_t = cat.shows.get(e["show"], {}).get("title", "")
                # a short name like "Jack Frost" only counts inside a specials collection, not as a
                # sitcom episode that happens to share it
                if len(n.split()) >= 3 or _SPECIALS_SHOW.search(show_t):
                    return {"id": e["id"], "title": f"{show_t} – {e['title']}", "how": "episode"}
        for n in names:  # or as a show of its own with one episode
            for s in idx["shows"].get(n, []):
                eps = cat.episodes.get(s["id"], [])
                if eps:
                    return {"id": eps[0]["id"], "title": s["title"], "how": "show"}
    return None


def find_key(cat, key: str) -> dict | None:
    info = xmasdata.title_info(key)
    return find(cat, info["title"], info["years"], info["kind"], info["alt"])


# ---- have / need lists ------------------------------------------------------------------------

def plan_wants(cat, year: int) -> list[dict]:
    """Every title in a year's 25 Days schedule, how often it airs, and whether you have it."""
    count: dict[str, int] = {}
    first: dict[str, str] = {}
    for day, slots in sorted(xmasdata.SCHEDULES[year].items()):
        for s, _e, key in slots:
            count[key] = count.get(key, 0) + 1
            first.setdefault(key, f"Dec {day}, {_clock(s)}")
    out = []
    for key, n in count.items():
        info = xmasdata.title_info(key)
        got = find_key(cat, key)
        out.append({"key": key, "title": info["title"], "year": info["year"], "kind": info["kind"],
                    "christmas": info["christmas"], "airings": n, "first": first[key],
                    "have": bool(got), "match": got["title"] if got else ""})
    out.sort(key=lambda x: (x["have"], -x["airings"], x["title"].lower()))
    return out


def essentials(cat) -> list[dict]:
    out = []
    for title, year, kind, show in xmasdata.ESSENTIALS:
        got = find(cat, title, [year], kind, show=show)
        out.append({"title": title, "year": year, "kind": kind, "show": show, "have": bool(got),
                    "match": got["title"] if got else ""})
    return out


def christmas_ids(cat) -> dict:
    """Christmas movies, specials and episodes whose titles don't say "Christmas" (Elf, The Polar
    Express, Home Alone...), found from the lists above. Cached until the next scan."""
    key = (id(cat), cat.scanned_at)
    hit = _cache.get("xmas")
    if hit and hit[0] == key:
        return hit[1]
    movies, eps = set(), set()
    for title, year in xmasdata.EXTRA_CHRISTMAS_MOVIES:
        got = find(cat, title, [year], "special")
        if got:
            (movies if got["how"] == "movie" else eps).add(got["id"])
    for title, year, kind, show in xmasdata.ESSENTIALS:
        if kind == "episode":
            got = find(cat, title, [year], kind, show=show)
            if got:
                eps.add(got["id"])
    out = {"movies": movies, "episodes": eps}
    _cache["xmas"] = (key, out)
    return out


# ---- the 25 Days timetable --------------------------------------------------------------------

def _clock(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d}{'am' if h < 12 else 'pm'}"


def plan_slots(year: int, season: int) -> list[dict]:
    """A 25 Days schedule laid onto December of `season` (the same dates and clock times; the days of
    the week differ from the original year). [{"s", "e", "key", "day"}], sorted."""
    ck = ("plan", year, season, time.timezone)
    if ck in _cache:
        return _cache[ck]
    out = []
    for day, slots in xmasdata.SCHEDULES[year].items():
        for s, e, key in slots:
            sh, sm = map(int, s.split(":"))
            eh, em = map(int, e.split(":"))
            start = datetime(season, 12, day, sh, sm)
            end = datetime(season, 12, day, eh, em)
            if end <= start:
                end += timedelta(days=1)
            out.append({"s": start.timestamp(), "e": end.timestamp(), "key": key, "day": day})
    out.sort(key=lambda x: x["s"])
    for a, b in zip(out, out[1:]):  # listings sometimes overlap by a few minutes
        a["e"] = min(a["e"], b["s"])
    _cache[ck] = out
    return out


def plan_window(year: int, season: int) -> tuple[float, float]:
    slots = plan_slots(year, season)
    start = datetime(season, 12, 1).timestamp()
    return start, max(slots[-1]["e"], datetime(season, 12, 26).timestamp())


def days_until_christmas(t: float) -> int | None:
    d = datetime.fromtimestamp(t)
    if d.month != 12 or d.day > 24:
        return None
    return 25 - d.day


def match_airing(airings: list[dict], key: str, year: int) -> dict | None:
    """A recorded broadcast of this title (its breaks can replay at the same places), preferring the
    same year's."""
    info = xmasdata.title_info(key)
    names = [norm(t) for t in [info["title"], *info["alt"]]]
    words = {w for w in norm(info["title"]).split() if len(w) >= 5 and w not in _GENERIC}
    numbers = set(re.findall(r"\b\d\b", norm(info["title"])))  # "Santa Clause 2" isn't "Santa Clause 3"
    best, best_score = None, 0.0
    for a in airings:
        text = a.get("text", "")
        tw = set(text.split())
        if any(n and n in text for n in names):
            score = 10.0
        elif words and len(words & tw) >= max(1, len(words) // 3) and numbers <= tw:
            score = len(words & tw) / len(words) * 5
        else:
            continue
        score += 3 if year in (a.get("years") or []) else 0
        if score > best_score:
            best, best_score = a, score
    return best


# ---- the 2000s Christmas channel -------------------------------------------------------------

KID = {"animation", "kids", "children", "family"}

# hour -> how likely each kind of program is (cartoon, sitcom, special, family movie, movie, classic)
DAYPARTS = [
    (6, 12, "Holiday Morning", {"cartoon": 5, "special": 4, "family": 1}),
    (12, 17, "Holiday Afternoon", {"sitcom": 4, "family": 2, "movie": 1, "cartoon": 1, "special": 1}),
    (17, 20, "Family Specials", {"special": 4, "cartoon": 2, "family": 2}),
    (20, 22, "Holiday Movie Night", {"movie": 4, "family": 3}),
    (22, 26, "Late Night Holiday Movies", {"movie": 3, "classic": 2, "sitcom": 1}),
    (2, 6, "Overnight", {"classic": 2, "sitcom": 2, "special": 1, "movie": 1, "family": 1}),
]


def daypart(t: float) -> tuple[str, dict]:
    h = datetime.fromtimestamp(t).hour
    for a, b, name, mix in DAYPARTS:
        if a <= h < b or a <= h + 24 < b:
            return name, mix
    return DAYPARTS[-1][2], DAYPARTS[-1][3]


def kind_of(cat, item_id: str) -> str:
    """What sort of holiday program something is, for the 2000s Christmas day."""
    m = cat.movies.get(item_id)
    if m:
        genres = {g.lower() for g in m.get("genres", [])}
        if m["dur"] <= 65 * 60:
            return "special"
        if (m.get("year") or 2000) < 1975:
            return "classic"
        return "family" if genres & KID else "movie"
    e = cat.items.get(item_id) or {}
    show = cat.shows.get(e.get("show"), {})
    genres = {g.lower() for g in show.get("genres", [])}
    if e.get("dur", 0) > 65 * 60:
        return "movie"
    return "cartoon" if genres & KID else "sitcom"


def christmas_story(cat) -> str | None:
    """A Christmas Story (1983), for the 24-hour marathon."""
    got = find(cat, "A Christmas Story", [1983], "movie")
    return got["id"] if got else None


def in_marathon(t: float) -> bool:
    """Christmas Eve 8pm to Christmas Day 8pm."""
    d = datetime.fromtimestamp(t)
    return d.month == 12 and ((d.day == 24 and d.hour >= 20) or (d.day == 25 and d.hour < 20))
