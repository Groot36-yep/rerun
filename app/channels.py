"""Channels: what they contain, and the automatic suggestions made from your library.

A channel's content is a list of rules, for example:
  {"type": "filter", "media": "shows", "network": "Nickelodeon"}
  {"type": "filter", "media": "movies", "genres_any": ["Horror"]}
  {"type": "filter", "media": "shows", "collection": "Saturday Morning"}
  {"type": "filter", "media": "music_videos"}
  {"type": "holiday", "key": "christmas"}
  {"type": "show", "id": "p1234"}
Rules are re-applied after every scan, so new shows join automatically.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import secrets
from collections import defaultdict

from . import seasons
from .catalog import Catalog
from .logos_table import TMDB_LOGOS, WIKIPEDIA_ARTICLES
from .networks import CATEGORIES, GENRE_START, HOLIDAY_START, MOVIES_START, family_for, parent_of

KID_GENRES = ["Animation", "Kids", "Children", "Family"]
SCIFI_GENRES = ["Science Fiction", "Sci-Fi", "Sci-Fi & Fantasy", "Fantasy"]
HOLIDAY_CHANNEL_NAMES = {"christmas": "Christmas Channel", "halloween": "Halloween Channel"}
# Commercials you removed on the Commercials page (ids); set by the app from config.
hidden_filler: set[str] = set()

CHANNEL_DEFAULTS = {
    "id": "",
    "number": 1,
    "name": "New Channel",
    "enabled": True,
    "group": "",
    "rules": [],
    "exclude": [],
    "order": "shuffle",  # shuffle | in_order
    "slot_minutes": 30,  # 0 = back to back
    "commercials": {"enabled": True, "break_style": "chapters", "sources": [], "tags": []},
    "logo": "",  # "tmdb:/abc.png", "wiki:Article title", or "" for automatic
    "auto_key": "",
    "season": "",  # a holiday key: the channel only airs during that holiday
    "off_season": "card",  # card (shows "back on ...") | hide (leaves the guide)
    "holiday_mix": True,  # sprinkle in holiday episodes during the holidays
    "seed": 0,
}

BREAK_STYLES = ("chapters", "even", "end")
_LOGO = re.compile(r"^(tmdb:/[A-Za-z0-9_.-]{1,100}|wiki:[\w .,'&+!()\-]{1,80})$")


def normalize_channel(ch: dict) -> dict:
    out = copy.deepcopy(CHANNEL_DEFAULTS)
    for k, v in (ch or {}).items():
        if k in out:
            out[k] = v
    com = copy.deepcopy(CHANNEL_DEFAULTS["commercials"])
    com.update({k: v for k, v in ((ch or {}).get("commercials") or {}).items() if k in com})
    out["commercials"] = com
    out["id"] = str(out["id"] or secrets.token_hex(4))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", out["id"]):
        out["id"] = secrets.token_hex(4)
    out["name"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(out["name"])).strip()[:60] or "Channel"
    out["group"] = re.sub(r"[\x00-\x1f\x7f\",]", " ", str(out["group"] or "")).strip()[:40]
    out["number"] = max(1, min(9999, int(out["number"] or 1)))
    out["enabled"] = bool(out["enabled"])
    out["order"] = out["order"] if out["order"] in ("shuffle", "in_order") else "shuffle"
    out["slot_minutes"] = int(out["slot_minutes"]) if int(out["slot_minutes"] or 0) in (0, 15, 30, 60) else 30
    if com["break_style"] not in BREAK_STYLES:
        com["break_style"] = "chapters"
    com["enabled"] = bool(com["enabled"])
    com["sources"] = [str(s) for s in com["sources"] or []]
    com["tags"] = [str(t).lower() for t in com["tags"] or []]
    out["rules"] = [r for r in (out["rules"] or []) if isinstance(r, dict) and r.get("type")]
    out["exclude"] = [str(x) for x in out["exclude"] or []]
    out["logo"] = str(out["logo"] or "")
    if not _LOGO.match(out["logo"]) or ".." in out["logo"]:
        out["logo"] = ""
    out["season"] = out["season"] if out["season"] in seasons.PRESETS else ""
    out["off_season"] = out["off_season"] if out["off_season"] in ("card", "hide") else "card"
    out["holiday_mix"] = bool(out["holiday_mix"])
    out["seed"] = int(out["seed"] or random.randint(1, 2**31))
    return out


def content_signature(ch: dict, holidays: dict | None = None) -> str:
    """Changes whenever an edit should rebuild the schedule."""
    keys = ("rules", "exclude", "order", "slot_minutes", "commercials", "seed", "season", "holiday_mix")
    blob = (json.dumps({k: ch.get(k) for k in keys}, sort_keys=True) + json.dumps(holidays or {}, sort_keys=True)
            + ",".join(sorted(hidden_filler)))
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _lower(lst) -> set[str]:
    return {str(x).lower() for x in lst or []}


def _match(rule: dict, x: dict) -> bool:
    net = (x.get("network") or "").lower()
    if rule.get("network") and net != str(rule["network"]).lower():
        return False
    if rule.get("networks") and net not in _lower(rule["networks"]):
        return False
    if rule.get("collection") and str(rule["collection"]).lower() not in _lower(x.get("collections")):
        return False
    g = _lower(x.get("genres"))
    if rule.get("genres_any") and not (g & _lower(rule["genres_any"])):
        return False
    if rule.get("genres_none") and (g & _lower(rule["genres_none"])):
        return False
    year = int(x.get("year") or 0)
    if rule.get("year_from") and (not year or year < int(rule["year_from"])):
        return False
    if rule.get("year_to") and (not year or year > int(rule["year_to"])):
        return False
    if rule.get("sources") and x.get("src") not in rule["sources"]:
        return False
    if rule.get("tags") and not (_lower(x.get("tags")) & _lower(rule["tags"])):
        return False
    return True


def resolve(ch: dict, cat: Catalog, holidays: dict | None = None) -> list[dict]:
    """Turn a channel's rules into "programs" the scheduler rotates through."""
    holidays = holidays or seasons.DEFAULT_HOLIDAYS
    shows, movies, videos, episodes = set(), set(), set(), set()
    for rule in ch.get("rules", []):
        t = rule.get("type")
        if t == "show" and rule.get("id") in cat.shows:
            shows.add(rule["id"])
        elif t == "movie" and rule.get("id") in cat.movies:
            movies.add(rule["id"])
        elif t == "holiday" and rule.get("key") in seasons.PRESETS:
            found = seasons.matches(cat, rule["key"], holidays.get(rule["key"]) or {})
            net = str(rule.get("network") or "").lower()
            kind = rule.get("media")  # "episodes" for a holiday TV channel, "movies" for a movie channel
            for eid in (found["episodes"] if kind != "movies" else ()):
                e = cat.items.get(eid)
                if e and (not net or cat.shows.get(e["show"], {}).get("network", "").lower() == net):
                    episodes.add(eid)
            if kind != "episodes":
                movies |= {m for m in found["movies"] if not net or cat.movies[m].get("network", "").lower() == net}
        elif t == "filter":
            media = rule.get("media")
            if media == "shows":
                shows |= {s["id"] for s in cat.shows.values() if _match(rule, s)}
            elif media == "movies":
                movies |= {m["id"] for m in cat.movies.values() if _match(rule, m)}
            elif media == "music_videos":
                videos |= {v["id"] for v in cat.all_music_videos() if _match(rule, v)}
    excluded = set(ch.get("exclude", []))
    programs = []
    for sid in sorted(shows - excluded):
        n = len(cat.episodes.get(sid, []))
        if n:
            programs.append({"key": f"show:{sid}", "kind": "show", "show": sid,
                             "weight": max(1, min(4, round(math.sqrt(n) / 3)))})
    ep_ids = sorted(e for e in episodes if cat.items.get(e, {}).get("show") not in excluded)
    if ep_ids:
        programs.append({"key": "episodes", "kind": "episodes", "items": ep_ids,
                         "weight": max(1, min(6, len(ep_ids) // 8))})
    movie_ids = sorted(movies - excluded)
    if movie_ids:
        weight = max(1, min(3, len(movie_ids) // 15)) if ep_ids and not shows else 1
        programs.append({"key": "movies", "kind": "movies", "items": movie_ids, "weight": weight})
    video_ids = sorted(videos - excluded)
    if video_ids:
        weight = max(1, sum(p["weight"] for p in programs if p["kind"] == "show"))
        programs.append({"key": "music", "kind": "music", "items": video_ids, "weight": weight})
    return programs


def filler_pool(ch: dict, cat: Catalog) -> list[dict]:
    com = ch.get("commercials") or {}
    srcs = set(com.get("sources") or [])
    want = _lower(com.get("tags"))
    pool = []
    for key, items in cat.filler.items():
        if srcs and key not in srcs:
            continue
        for x in items:
            if want and not (_lower(x.get("tags")) & want):
                continue
            if x["id"] in hidden_filler:
                continue
            if x["dur"] >= 1:
                pool.append(x)
    return pool


def describe_content(ch: dict, cat: Catalog, holidays: dict | None = None) -> dict:
    progs = resolve(ch, cat, holidays)
    shows = [p["show"] for p in progs if p["kind"] == "show"]
    movies = next((p["items"] for p in progs if p["kind"] == "movies"), [])
    videos = next((p["items"] for p in progs if p["kind"] == "music"), [])
    specials = next((p["items"] for p in progs if p["kind"] == "episodes"), [])
    secs = sum(e["dur"] for s in shows for e in cat.episodes.get(s, []))
    secs += sum(cat.movies[m]["dur"] for m in movies if m in cat.movies)
    secs += sum(cat.items[v]["dur"] for v in videos + specials if v in cat.items)
    show_list = sorted(
        ({"id": s, "title": cat.shows[s]["title"], "episodes": len(cat.episodes.get(s, []))} for s in shows),
        key=lambda x: x["title"].lower(),
    )
    return {
        "shows": len(shows), "movies": len(movies), "music_videos": len(videos), "specials": len(specials),
        "episodes": sum(x["episodes"] for x in show_list) + len(specials),
        "hours": round(secs / 3600, 1),
        "show_list": show_list[:300],
        "commercials": len(filler_pool(ch, cat)),
    }


# --------------------------------------------------------------------------- logos

def network_logo(name: str, cat: Catalog | None = None) -> str:
    """The best logo we know of for a network: built-in TMDB logo, your library's, or Wikipedia's."""
    fam, _ = family_for(name)
    if fam in TMDB_LOGOS:
        return "tmdb:" + TMDB_LOGOS[fam]
    lib = ((cat.networks if cat else {}).get(fam) or {}).get("logo")
    if lib:
        return "tmdb:" + lib
    if fam in WIKIPEDIA_ARTICLES:
        return "wiki:" + WIKIPEDIA_ARTICLES[fam]
    return ""


# --------------------------------------------------------------------------- networks found in the library

def library_networks(cat: Catalog, min_shows: int = 2) -> list[dict]:
    """Every network in your library, with sister channels folded in when they're too small alone."""
    by_net: dict[str, list[dict]] = defaultdict(list)
    for s in cat.shows.values():
        if s.get("network"):
            by_net[s["network"]].append(s)
    movie_nets = defaultdict(int)
    for m in cat.movies.values():
        if m.get("network"):
            movie_nets[m["network"]] += 1

    def eps(shows):
        return sum(len(cat.episodes.get(s["id"], [])) for s in shows)

    def big_enough(fam):
        return len(by_net.get(fam, [])) >= min_shows or eps(by_net.get(fam, [])) >= 40

    children = defaultdict(list)
    for fam in by_net:
        p = parent_of(fam)
        if p and not big_enough(fam):
            children[p].append(fam)
    out = []
    for fam in set(by_net) | set(children):
        own = by_net.get(fam, [])
        small = sorted(children.get(fam, []))
        if not own and not small:
            continue
        shows = own + [s for c in small for s in by_net[c]]
        category = (cat.networks.get(fam) or {}).get("category") or family_for(fam)[1]
        label, start = CATEGORIES.get(category, CATEGORIES["other"])
        out.append({
            "name": fam, "category": category, "group": label, "number_start": start,
            "shows": len(shows), "own_shows": len(own), "episodes": eps(shows), "movies": movie_nets.get(fam, 0),
            "sisters": small, "parent": parent_of(fam),
            "eligible": len(shows) >= min_shows or eps(shows) >= 40,
            "show_titles": sorted(s["title"] for s in shows)[:12],
        })
    out.sort(key=lambda n: (n["number_start"], -n["shows"], n["name"]))
    return out


def network_rules(net: dict, cat: Catalog) -> list[dict]:
    rules = [{"type": "filter", "media": "shows", "network": net["name"]}]
    rules += [{"type": "filter", "media": "shows", "network": c, "auto": True} for c in net["sisters"]]
    if net["movies"]:
        rules.append({"type": "filter", "media": "movies", "network": net["name"]})
    if net["name"] == "MTV" and cat.all_music_videos():
        rules.append({"type": "filter", "media": "music_videos"})
    return rules


# --------------------------------------------------------------------------- suggestions

def suggest(cat: Catalog, existing: list[dict], holidays: dict | None = None, min_shows: int = 2) -> list[dict]:
    holidays = holidays or seasons.DEFAULT_HOLIDAYS
    taken = {c.get("auto_key") for c in existing if c.get("auto_key")}
    has_ads = bool(cat.filler)
    out: list[dict] = []

    def add(key, name, rules, group, start, *, why, kind="tv", recommended=True, logo="", slot=30,
            order="shuffle", break_style="chapters", season=""):
        if key in taken or any(s["key"] == key for s in out):
            return
        probe = normalize_channel({"rules": rules, "commercials": {"enabled": has_ads}})
        info = describe_content(probe, cat, holidays)
        if not (info["shows"] or info["movies"] or info["music_videos"] or info["specials"]):
            return
        out.append({
            "key": key, "name": name, "rules": rules, "group": group, "why": why, "kind": kind,
            "recommended": recommended, "logo": logo, "slot_minutes": slot, "order": order,
            "break_style": break_style, "info": info, "number_start": start, "season": season,
        })

    # 1. A channel for every network in your library.
    mv_count = len(cat.all_music_videos())
    for net in library_networks(cat, min_shows):
        if not net["eligible"]:
            continue
        why = f"{net['shows']} shows that aired on {net['name']}"
        if net["sisters"]:
            why += f" (including {', '.join(net['sisters'])})"
        if net["name"] == "MTV" and mv_count:
            why += ", plus your music videos"
        add(f"network:{net['name']}", net["name"], network_rules(net, cat), net["group"], net["number_start"],
            why=why, logo=network_logo(net["name"], cat), recommended=True)

    # 2. Genre and era channels.
    def shows_where(**rule):
        r = {"type": "filter", "media": "shows", **rule}
        return r, [s for s in cat.shows.values() if _match(r, s)]

    r, lst = shows_where(genres_any=["Animation"], genres_none=["Anime"])
    if len(lst) >= 4:
        add("genre:cartoons", "Cartoons", [r], "Favorites", GENRE_START, why=f"{len(lst)} animated shows")
    for start, label in ((1960, "60s & 70s"), (1980, "80s"), (1990, "90s"), (2000, "2000s")):
        end = start + 19 if start == 1960 else start + 9
        r, lst = shows_where(genres_any=KID_GENRES, year_from=start, year_to=end)
        if len(lst) >= 4:
            add(f"era:kids:{start}", f"{label} Cartoons", [r], "Favorites", GENRE_START,
                why=f"{len(lst)} kids' shows from the {label}", recommended=False)
    r, lst = shows_where(genres_any=["Anime"])
    if len(lst) >= 3:
        add("genre:anime", "Anime", [r], "Favorites", GENRE_START, why=f"{len(lst)} anime series", recommended=False)
    r, lst = shows_where(genres_any=["Comedy"], genres_none=["Animation"])
    if len(lst) >= 4:
        add("genre:sitcoms", "Sitcoms", [r], "Favorites", GENRE_START, why=f"{len(lst)} live-action comedies",
            recommended=False)
    r, lst = shows_where(genres_any=["Drama"], genres_none=["Animation"])
    if len(lst) >= 4:
        add("genre:drama", "Drama", [r], "Favorites", GENRE_START, why=f"{len(lst)} drama series",
            recommended=False)
    r, lst = shows_where(genres_any=SCIFI_GENRES, genres_none=["Animation"])
    if len(lst) >= 3:
        add("genre:scifi", "Sci-Fi", [r], "Favorites", GENRE_START, why=f"{len(lst)} sci-fi & fantasy series",
            recommended=False)
    r, lst = shows_where(year_to=1979)
    if len(lst) >= 4:
        add("era:classic", "Classic TV", [r], "Favorites", GENRE_START, why=f"{len(lst)} shows from before 1980",
            recommended=False)

    # 3. Movies.
    if len(cat.movies) >= 5:
        add("movies:all", "Movies", [{"type": "filter", "media": "movies"}], "Movies", MOVIES_START, kind="movies",
            why=f"All {len(cat.movies)} of your movies")
    movie_genres = [
        ("Family", ["Family", "Animation"], "Family Movies"), ("Action", ["Action"], "Action Movies"),
        ("Comedy", ["Comedy"], "Comedy Movies"), ("Horror", ["Horror"], "Fright Night"),
        ("SciFi", SCIFI_GENRES, "Sci-Fi Movies"),
    ]
    for key, genres, name in movie_genres:
        r = {"type": "filter", "media": "movies", "genres_any": genres}
        n = sum(1 for m in cat.movies.values() if _match(r, m))
        if n >= 12:
            add(f"movies:{key.lower()}", name, [r], "Movies", MOVIES_START, kind="movies", why=f"{n} movies",
                recommended=False)

    # 4. Music videos (an MTV-style channel).
    if mv_count:
        music_start = CATEGORIES["music"][1]
        add("music:all", "MTV" if "network:MTV" not in {s["key"] for s in out} | taken else "Music Videos",
            [{"type": "filter", "media": "music_videos"}], "Music", music_start, kind="music",
            why=f"{mv_count} music videos", break_style="end", logo="tmdb:" + TMDB_LOGOS["MTV"])
        for start, label in ((1980, "80s"), (1990, "90s"), (2000, "2000s")):
            r = {"type": "filter", "media": "music_videos", "year_from": start, "year_to": start + 9}
            n = sum(1 for v in cat.all_music_videos() if _match(r, v))
            if n >= 40:
                add(f"music:{start}", f"{label} Videos", [r], "Music", music_start, kind="music",
                    why=f"{n} videos from the {label}", recommended=False, break_style="end")

    # 5. Holidays.
    for key, preset in seasons.PRESETS.items():
        conf = holidays.get(key) or {}
        if not conf.get("enabled", True):
            continue
        found = seasons.matches(cat, key, conf)
        n = len(found["episodes"]) + len(found["movies"])
        if n >= 5:
            when = f"{seasons.friendly_date(conf.get('start', preset['start']))} to " \
                   f"{seasons.friendly_date(conf.get('end', preset['end']))}"
            add(f"holiday:{key}", HOLIDAY_CHANNEL_NAMES.get(key, f"{preset['name']} TV"),
                [{"type": "holiday", "key": key}], "Holidays", HOLIDAY_START, kind="holiday", season=key,
                why=f"{len(found['episodes'])} holiday episodes and {len(found['movies'])} movies, on the air {when}",
                recommended=key in ("christmas", "halloween") and n >= 10)
    return out


def channel_from_suggestion(s: dict, channels: list[dict], has_ads: bool) -> dict:
    return normalize_channel({
        "number": next_free_number(channels, s.get("number_start", 1)), "name": s["name"], "group": s["group"],
        "rules": s["rules"], "slot_minutes": s.get("slot_minutes", 30), "order": s.get("order", "shuffle"),
        "logo": s.get("logo", ""), "auto_key": s["key"], "season": s.get("season", ""),
        "commercials": {"enabled": has_ads, "break_style": s.get("break_style", "chapters")},
    })


def next_free_number(channels: list[dict], start: int = 1) -> int:
    used = {c["number"] for c in channels}
    n = start
    while n in used:
        n += 1
    return n


def sync_auto(cfg: dict, cat: Catalog) -> list[dict]:
    """After a scan, add channels for new networks (and holidays), unless you've turned that off."""
    auto = cfg["auto"]
    chans = cfg["channels"]
    min_shows = int(auto.get("min_shows") or 2)
    dismissed = set(auto.get("dismissed") or [])
    made = []
    for s in suggest(cat, chans, cfg.get("holidays"), min_shows):
        if s["key"] in dismissed:
            continue
        if s["key"].startswith("network:") and auto.get("networks"):
            pass
        elif s["key"].startswith("holiday:") and auto.get("holidays") and s["recommended"]:
            pass
        else:
            continue
        ch = channel_from_suggestion(s, chans, bool(cat.filler))
        chans.append(ch)
        made.append(ch)
    # Once a sister network has its own channel, stop doubling its shows on the parent's channel.
    own = {c["auto_key"][len("network:"):] for c in chans if c.get("auto_key", "").startswith("network:")}
    for c in chans:
        if c.get("auto_key", "").startswith("network:"):
            c["rules"] = [r for r in c["rules"] if not (r.get("auto") and r.get("network") in own)]
    return made
