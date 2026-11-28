"""Builds each channel's schedule ("playout") a few days ahead.

A schedule is a list of blocks (what the guide shows, e.g. one 30-minute
SpongeBob slot) made of items (the actual pieces that play: part of an
episode, a commercial, the rest of the episode, more commercials...).

Schedules are disposable: if a file is deleted or damaged, Rerun simply
starts a fresh schedule from "now". Which episode comes next is remembered
separately, so a rebuild doesn't restart every show from episode one.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import math
import os
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime

from . import seasons
from . import airtimes, archive
from . import lineups, xmas
from .channels import christmas_style, content_signature, filler_pool, network_key, resolve
from .listen import break_place, norm as show_norm
from .networks import family_for
from .xmasdata import title_info

NETWORK_TAGS = set(archive.PROFILES)
from .store import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger("rerun.scheduler")

KEEP_PAST = 6 * 3600      # keep a little history so "now playing" always resolves
FIRST_BUILD = 6 * 3600    # build this much immediately; the rest fills in the background
CHUNK = 2 * 3600          # build in pieces this big so live streams never wait long
OFFGRID_BREAK = 60        # ad time after a show that couldn't land on the half hour
MAX_GAP_FRACTION = 0.4    # never fill more than 40% of a block with ads
MIN_ALIGN_GAP = 300       # ...but it's always fine to fill up to 5 minutes
OVERFILL_MAX = 30         # a break may run this many seconds long rather than end on a filler card
RECENT_FILLER = 40


def tz_offset(t: float) -> float:
    return datetime.fromtimestamp(t).astimezone().utcoffset().total_seconds()


def floor_slot(t: float, slot: int) -> float:
    off = tz_offset(t)
    return math.floor((t + off) / slot) * slot - off


def ceil_slot(t: float, slot: int) -> float:
    off = tz_offset(t)
    return math.ceil((t + off - 1e-6) / slot) * slot - off


class Playout:
    def __init__(self, cid: str, data: dict | None = None):
        data = data or {}
        self.cid = cid
        self.items: list[dict] = data.get("items", [])
        self.blocks: list[dict] = data.get("blocks", [])
        self.state: dict = data.get("state", {})
        self.sig: str = data.get("sig", "")
        self._starts: list[float] | None = None

    @property
    def end(self) -> float:
        return self.blocks[-1]["e"] if self.blocks else 0.0

    def to_dict(self) -> dict:
        return {"v": 1, "sig": self.sig, "state": self.state, "blocks": self.blocks, "items": self.items}

    def add(self, items: list[dict], block: dict):
        self.items.extend(items)
        self.blocks.append(block)
        self._starts = None

    def index_at(self, t: float) -> int | None:
        if self._starts is None:
            self._starts = [x["s"] for x in self.items]
        i = bisect.bisect_right(self._starts, t) - 1
        if 0 <= i < len(self.items) and self.items[i]["s"] <= t < self.items[i]["e"]:
            return i
        return None

    def block_at(self, t: float) -> dict | None:
        starts = [b["s"] for b in self.blocks]
        i = bisect.bisect_right(starts, t) - 1
        if 0 <= i < len(self.blocks) and self.blocks[i]["s"] <= t < self.blocks[i]["e"]:
            return self.blocks[i]
        return None

    def prune(self, before: float):
        self.items = [x for x in self.items if x["e"] >= before]
        self.blocks = [b for b in self.blocks if b["e"] >= before]
        self._starts = None

    def keep_until_current(self, now: float):
        """Drop everything after the block that's on right now (used after an edit)."""
        self.blocks = [b for b in self.blocks if b["s"] <= now]
        cut = self.end
        self.items = [x for x in self.items if x["s"] < cut]
        self._starts = None

    def clear(self):
        self.items, self.blocks, self._starts = [], [], None

    def cut(self, t: float):
        """End the schedule at t, part-way through whatever is on (to start something new right away)."""
        self.items = [x for x in self.items if x["s"] < t]
        if self.items and self.items[-1]["e"] > t:
            self.items[-1]["e"] = t
        self.blocks = [b for b in self.blocks if b["s"] < t]
        if self.blocks and self.blocks[-1]["e"] > t:
            self.blocks[-1]["e"] = t
        self._starts = None

    def blocks_between(self, a: float, b: float) -> list[dict]:
        return [x for x in self.blocks if x["e"] > a and x["s"] < b]


_YEAR = re.compile(r"(?<!\d)(19[6-9]\d|20[0-3]\d)(?!\d)")
_years: dict[str, int] = {}


def _int_year(year) -> int:
    try:
        return int(str(year)[:4])
    except (TypeError, ValueError):
        return 0


def item_year(f: dict) -> int:
    """The year a commercial or bumper is from, from its name or its tape's ("2003 Nickelodeon Commercial
    Break", "Nick bumpers 2004.mp4"), or 0. A range ("1998-2003") counts as its middle."""
    key = f.get("id") or ""
    if key in _years:
        return _years[key]
    text = f"{f.get('title') or ''} {os.path.basename(f.get('file') or '')}"
    found = [int(y) for y in _YEAR.findall(text)]
    y = round(sum(found[:2]) / len(found[:2])) if found else 0
    if key:
        _years[key] = y
    return y


def _year_match(by: int, ctx: dict, tags: set) -> float:
    """How well a bumper's year suits the episode: one from the same year or so is best (the 2003 Nick
    splat around a 2003 SpongeBob), the same decade next, and one from a different era least."""
    ey = ctx.get("year") or 0
    if by and ey:
        d = abs(by - ey)
        return 3.0 if d <= 1 else 2.0 if d <= 3 else 1.0 if d <= 6 else -2.0 if d > 9 else 0.0
    return 1.0 if ctx.get("era") and ctx["era"] in tags else 0.0


TRIVIA_ON = False  # "Guess the show!" is put away for now; trivia.py keeps it for later
TRIVIA_ASK, TRIVIA_ANSWER = 8.0, 6.0  # seconds: the question card, and the answer card
KIDS_TAGS = {"nick", "nickjr", "nicktoons", "cn", "boomerang", "disney", "playhouse", "toondisney", "foxkids",
             "kidswb"}
KIDS_AD = re.compile(
    r"\b(?:cereal|cheerios|frosted|flakes|trix|lucky charms|cocoa (puffs|pebbles)|fruity pebbles|cinnamon toast|"
    r"pop-?tarts|eggo|lunchables|gushers|fruit by the foot|fruit roll|go-?gurt|kool-?aid|capri sun|sunny ?d|"
    r"happy meal|chuck e\.? cheese|toys?|hasbro|mattel|lego|barbie|bratz|polly pocket|hot wheels|"
    r"nerf|furby|tamagotchi|beyblade|bakugan|yu-?gi-?oh|pok[eé]mon|game ?boy|nintendo|playstation|xbox|"
    r"crayola|play-?doh|easy-?bake|super soaker|tonka|fisher-?price|leapfrog|vtech|kid cuisine|bagel bites)\b",
    re.I)


def saturday_morning(t: float) -> bool:
    """Saturday from 6am until noon, when the cartoons were new and the commercials were for cereal."""
    lt = time.localtime(t)
    return lt.tm_wday == 5 and 6 <= lt.tm_hour < 12


def _src(m: dict) -> dict:
    return {"f": m.get("file") or "", "p": m.get("part") or "", "a": bool(m.get("audio", True))}


def clip_holidays(f: dict, heard: dict | None) -> set:
    """The holidays a commercial or bumper belongs to, so it stays off the air outside their season: from
    its folder ("halloween"), its name, or what the listener heard said in it ("...your Halloween
    costume"). A clip from a tape recorded in October (or December) that hasn't been heard yet is counted
    as Halloween's (or Christmas's) until it is: those breaks are full of them. Heard, and nothing said
    about a holiday, it plays all year. (Without the listener, month tapes play all year, as before.)"""
    heard = heard or {}
    tags = {str(x).lower() for x in f.get("tags") or []}
    got = tags & set(seasons.PRESETS)
    title = f.get("title") or ""
    h = archive.holiday_of(title)
    if h:
        got.add(h)
    rec = heard.get(f.get("id")) or {}
    if rec.get("text"):
        got |= seasons.said_in(rec["text"])
    elif heard and not rec.get("silent"):
        months = (tags & {"october", "december"}) | ({archive.month_of(title)} - {None})
        got |= {{"october": "halloween", "december": "christmas"}[m] for m in months}
    return got


def in_season(f: dict, heard: dict | None, active) -> bool:
    hol = clip_holidays(f, heard)
    return not hol or bool(hol & set(active))


def promo_rule(rec: dict | None) -> dict | None:
    """When a heard commercial can air, if it's a network promo that names a day or "tonight" (see
    airtimes). Only promos: a toy ad's "get yours today!" isn't a time."""
    if not rec or rec.get("cat") != "promo" or not rec.get("text"):
        return None
    return airtimes.rule(rec["text"])


def airs_now(f: dict, heard: dict | None, t: float) -> bool:
    return airtimes.fits(promo_rule((heard or {}).get(f.get("id"))), t)


class Builder:
    def __init__(self, ch: dict, cat, state: dict, chapters, holidays: dict | None = None, heard: dict | None = None):
        self.ch = ch
        # what each commercial is about, from listening to it (see listen.py): {clip id: {"cat", "shows", "role"}}
        self.heard = heard or {}
        self._hol_cache: dict = {}
        self._when_cache: dict = {}
        self._place_cache: dict = {}
        self._break_places: set = set()  # "into"/"out" clips already in the break being made
        self.cat = cat
        self.st = state
        self.chapters = chapters
        self.holidays = holidays or seasons.DEFAULT_HOLIDAYS
        self._shelf, self._shelf_day = set(), ""
        self.slot = int(ch.get("slot_minutes") or 0) * 60
        self.programs = {p["key"]: p for p in resolve(ch, cat, self.holidays)}
        # the shows this channel plays: a promo for one of them is fine here, a promo for anything else isn't
        self.airs = {show_norm(cat.shows[p["show"]].get("title", "")) for p in self.programs.values()
                     if p.get("kind") == "show" and p.get("show") in cat.shows}
        com = ch.get("commercials") or {}
        pool = filler_pool(ch, cat) if com.get("enabled") else []
        # Bumpers and idents only play in their own places (see _bumper), never as a commercial.
        self.bumpers = [f for f in pool if "bumper" in (f.get("tags") or [])]
        self.filler = [f for f in pool if "bumper" not in (f.get("tags") or [])]
        self.min_filler = min((f["dur"] for f in self.filler), default=0)
        # A network's own channel (Nickelodeon, or Boomerang built from its real lineup) shows its own
        # bumpers and logo whatever network a show first aired on; mixed channels use each show's.
        auto = network_key(ch)
        self.quick = ch.get("quick") or {}
        self.lineup = auto[len("lineup:"):] if auto.startswith("lineup:") else ""
        self.home_net = (archive.tag_for_network(auto[len("network:"):]) if auto.startswith("network:")
                         else lineups.ads_tag(self.lineup) or None)
        self.style = com.get("break_style", "chapters")
        try:  # your limit on commercial time, minutes per half hour of show (0: none)
            self.per_half_hour = max(0.0, float(com.get("per_half_hour") or 0)) * 60
        except (TypeError, ValueError):
            self.per_half_hour = 0.0
        self.xstyle = christmas_style(ch)
        self.kids = self._is_kids()
        self._groups: dict | None = None
        self._airings: list | None = None
        self._by_file: dict | None = None
        self._found: dict = {}
        self._story: str | None = None
        for k in ("bags", "seq", "lastpick"):
            self.st.setdefault(k, {})
        self.st.setdefault("recent", [])
        self.st.setdefault("recent_b", [])

    def _is_kids(self) -> bool:
        """A kids' channel: a kids' network's own (Nickelodeon, Boomerang, Nick Jr.), or one whose shows
        mostly first aired on kids' networks (a "Saturday Morning" channel of your own)."""
        auto = network_key(self.ch)
        if auto.startswith("lineup:"):
            return True
        if auto.startswith("network:"):
            return family_for(auto[len("network:"):])[1] == "kids"
        nets = [(self.cat.shows.get(p.get("show"), {}) or {}).get("network") for p in self.programs.values()
                if p["kind"] == "show"]
        nets = [n for n in nets if n]
        return bool(nets) and sum(family_for(n)[1] == "kids" for n in nets) >= len(nets) * 0.6

    # ---- deterministic randomness -------------------------------------------------
    def _rng(self) -> random.Random:
        self.st["n"] = self.st.get("n", 0) + 1
        return random.Random(f"{self.ch.get('seed', 0)}:{self.ch['id']}:{self.st['n']}")

    # ---- choosing what plays ------------------------------------------------------
    def _next_program(self) -> dict:
        bag = [k for k in self.st.get("bag", []) if k in self.programs]
        if not bag:
            for k, p in self.programs.items():
                bag += [k] * p["weight"]
            self._rng().shuffle(bag)
            for i in range(1, len(bag)):  # spread out repeats of the same show
                if bag[i] == bag[i - 1]:
                    for j in range(i + 1, len(bag)):
                        if bag[j] != bag[i - 1]:
                            bag[i], bag[j] = bag[j], bag[i]
                            break
            last = self.st.get("last")
            if len(bag) > 1 and bag[0] == last:
                j = next((j for j in range(1, len(bag)) if bag[j] != last), 0)
                bag[0], bag[j] = bag[j], bag[0]
        key = bag.pop(0)
        self.st["bag"] = bag
        self.st["last"] = key
        return self.programs[key]

    def _all_shelved(self, prog: dict, t: float) -> bool:
        """True when everything in a program is holiday content that's out of season today."""
        if self.ch.get("season") or prog["kind"] == "music":
            return False
        shelf = self._shelved(t)
        if not shelf:
            return False
        ids = [e["id"] for e in self.cat.episodes.get(prog["show"], [])] if prog["kind"] == "show" else prog["items"]
        return bool(ids) and all(i in shelf for i in ids)

    def _bag(self, key: str, ids: list[str]) -> list[str]:
        valid = set(ids)
        bag = [i for i in self.st["bags"].get(key, []) if i in valid]
        if not bag:
            bag = list(ids)
            self._rng().shuffle(bag)
            last = self.st["lastpick"].get(key)
            if len(bag) > 1 and bag[-1] == last:
                bag[0], bag[-1] = bag[-1], bag[0]
        self.st["bags"][key] = bag
        return bag

    def _peek(self, key: str, ids: list[str], in_order: bool, skip: set | frozenset = frozenset()) -> str | None:
        """The next id to play. `skip` holds holiday episodes that are out of season right now."""
        if not ids:
            return None
        if in_order:
            last = self.st["seq"].get(key)
            start = ids.index(last) + 1 if last in ids else 0
            for i in range(len(ids)):
                nid = ids[(start + i) % len(ids)]
                if nid not in skip:
                    return nid
            return ids[start % len(ids)]
        return self._bag(key, [i for i in ids if i not in skip] or ids)[-1]

    def _shelved(self, t: float) -> set:
        """Holiday episodes and movies that stay on the shelf today because it isn't their season."""
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if self._shelf_day != day:
            on = set(seasons.active(self.holidays, t))
            shelf, keep = set(), set()
            for k, conf in self.holidays.items():
                if k not in seasons.PRESETS or not conf.get("enabled", True):
                    continue
                found = seasons.matches(self.cat, k, conf)
                (keep if k in on else shelf).update(found["episodes"] | found["movies"])
            self._shelf, self._shelf_day = shelf - keep, day
        return self._shelf

    def _take(self, key: str, ids: list[str], in_order: bool, skip: set | frozenset = frozenset()) -> str | None:
        nid = self._peek(key, ids, in_order, skip)
        if nid is None:
            return None
        if in_order:
            self.st["seq"][key] = nid
        else:
            self.st["bags"][key].pop()
            self.st["lastpick"][key] = nid
        return nid

    def _pack(self, key: str, ids: list[str], in_order: bool, out: list[dict], skip=frozenset()) -> list[dict]:
        """Short cartoons share a slot, like two 11-minute SpongeBob segments."""
        items = self.cat.items
        if not self.slot or out[0]["dur"] >= self.slot * 0.5:
            return out
        total = out[0]["dur"]
        while len(out) < 4:
            nid = self._peek(key, ids, in_order, skip)
            nxt = items.get(nid) if nid else None
            if not nxt or nid in {m["id"] for m in out} or nxt["dur"] >= self.slot * 0.5:
                break
            if total + nxt["dur"] > math.ceil(total / self.slot) * self.slot - 60:
                break
            self._take(key, ids, in_order, skip)
            out.append(nxt)
            total += nxt["dur"]
        return out

    def _holiday_pick(self, t: float, pool: list[str], tag: str) -> str | None:
        """During a holiday, sometimes play a holiday episode/movie from this channel's own lineup."""
        if self.ch.get("season") or not self.ch.get("holiday_mix", True):
            return None
        for key in seasons.active(self.holidays, t):
            conf = self.holidays[key]
            if not conf.get("mix", True):
                continue
            found = seasons.matches(self.cat, key, conf)
            hits = [i for i in pool if i in found["episodes"] or i in found["movies"]]
            if hits and self._rng().random() < seasons.MIX_CHANCE:
                return self._take(f"mix:{key}:{tag}", hits, False)
        return None

    def _next_media(self, prog: dict, t: float = 0) -> list[dict]:
        items = self.cat.items
        shelf = frozenset() if self.ch.get("season") else self._shelved(t)
        if prog["kind"] == "show":
            ids = [e["id"] for e in self.cat.episodes.get(prog["show"], [])]
            special = self._holiday_pick(t, ids, prog["show"])
            if special:
                return [items[special]]
            # Saturday mornings on a kids' channel: each show's next episode in order, like that week's
            # new ones, rather than a random rerun
            in_order = self.ch.get("order") == "in_order" or (self.kids and saturday_morning(t))
            first = self._take(prog["key"], ids, in_order, shelf)
            if not first:
                return []
            return self._pack(prog["key"], ids, in_order, [items[first]], shelf)
        if prog["kind"] == "episodes":
            first = self._take("episodes", prog["items"], False)
            return self._pack("episodes", prog["items"], False, [items[first]]) if first else []
        if prog["kind"] == "movies":
            mid = self._holiday_pick(t, prog["items"], "movies") or self._take("movies", prog["items"], False, shelf)
            return [items[mid]] if mid else []
        if prog["kind"] == "music":
            target = (self.slot - 150) if self.slot >= 1800 else 900
            out, total, seen = [], 0.0, set()
            while total < target:
                nid = self._peek("music", prog["items"], False)
                v = items.get(nid) if nid else None
                if not v or nid in seen or (out and total + v["dur"] > target):
                    break
                self._take("music", prog["items"], False)
                out.append(v)
                seen.add(nid)
                total += v["dur"]
            return out
        return []

    # ---- commercials --------------------------------------------------------------
    def _break_context(self, prog: dict, media: list[dict], t: float, extra: dict | None = None) -> dict:
        """What the commercials around this show should match: its network, its era, and the holiday
        (on a holiday channel, during a holiday episode, or any holiday that's in season). The 25 Days
        channel wants that year's real 25 Days ads; the 2000s Christmas channel 2000s holiday ads."""
        ctx = self._base_context(prog, media, t)
        if (extra or {}).get("plan"):
            ctx.update(hol="christmas", active=ctx["active"] | {"christmas"}, prefer=self._plan_prefer(),
                       era=archive.era_tag(extra["plan"]), net="abcfamily", has_net=True)
        elif self.xstyle == "2000s":
            ctx.update(hol=ctx["hol"] or "christmas", prefer=[("2000s", 2.5)])
        if self.lineup and self.home_net:  # Boomerang ads around Scooby-Doo, not CBS-era ones
            ctx.update(net=self.home_net, has_net=True)
        if saturday_morning(t):  # cereal and toys take over the breaks, on every channel
            ctx["kids_ads"] = 4.0 if not self.kids else 1.5
        if self.heard:  # the show on now, and the one most likely next, for promos and bumpers that name a show
            first = media[0] if media else {}
            sid = first.get("show") or prog.get("show")
            ctx["cur"] = show_norm((self.cat.shows.get(sid) or {}).get("title", "")) if sid else ""
            nxt = next((self.programs[k] for k in self.st.get("bag") or [] if k in self.programs), None)
            ctx["nxt"] = show_norm((self.cat.shows.get(nxt.get("show")) or {}).get("title", "")) if nxt and nxt.get("show") else ""
        return ctx

    def _fits(self, f: dict, ctx: dict | None) -> tuple[bool, float]:
        """Whether a commercial that names a show belongs here, and how much it's wanted: "you're watching
        iCarly" only during iCarly, "up next, SpongeBob" only around SpongeBob, a promo only for a show
        this channel plays (best when it's on now or next). One that names no show always fits."""
        rec = self.heard.get(f["id"])
        if rec and ctx and ctx.get("at") and not airtimes.fits(self._when(f, rec), ctx["at"]):
            return False, 0.0  # "this Friday" on a Saturday, "tonight at 8" at 11pm
        if not rec or not rec.get("shows"):
            return True, 0.0
        shows = {show_norm(x) for x in rec["shows"]}
        cur, nxt = (ctx or {}).get("cur") or "", (ctx or {}).get("nxt") or ""
        if (ctx or {}).get("closing") and self._place(f) == "out":
            return nxt in shows, 5.0  # "now back to Rugrats" leads into the next show: only if that's Rugrats
        here = {x for x in (cur, nxt) if x}
        role = rec.get("role")
        if role == "watching":
            return cur in shows, 5.0
        if role == "next":
            return bool(shows & here), 5.0 if nxt in shows else 2.0
        if shows & here:
            return True, 3.0
        return bool(shows & self.airs), 1.0

    def _place(self, f: dict) -> str:
        """Where in a break a heard clip belongs: "into" (first), "out" (last), or "" (anywhere)."""
        key = f.get("id") if isinstance(f, dict) else f
        got = self._place_cache.get(key)
        if got is None:
            got = self._place_cache[key] = break_place((self.heard.get(key) or {}).get("text", ""))
        return got

    def _when(self, f: dict, rec: dict) -> dict | None:
        """When a heard promo can air (see airtimes); None for any time."""
        key = f.get("id")
        if key not in self._when_cache:
            self._when_cache[key] = promo_rule(rec)
        return self._when_cache[key]

    def _hol(self, f: dict) -> set:
        key = f.get("id")
        got = self._hol_cache.get(key)
        if got is None:
            got = self._hol_cache[key] = clip_holidays(f, self.heard)
        return got

    def _in_season(self, f: dict, active: set) -> bool:
        hol = self._hol(f)
        return not hol or bool(hol & active)

    def _kind(self, f: dict) -> str:
        """The kind of commercial (cereal, toy, promo...), if it's been heard; "" if not."""
        rec = self.heard.get(f["id"])
        return "" if not rec or rec.get("cat") in (None, "other") else rec["cat"]

    def _base_context(self, prog: dict, media: list[dict], t: float) -> dict:
        first = media[0] if media else {}
        if prog["kind"] in ("show", "episodes"):
            show = self.cat.shows.get(first.get("show") or prog.get("show"), {})
            net, year = show.get("network", ""), (first.get("date") or "")[:4] or show.get("year")
        else:
            net, year = first.get("network", ""), first.get("year")
        active = seasons.active(self.holidays, t)
        hol = self.ch.get("season") or next(
            (k for k in active if first.get("id") in self._holiday_ids(k)), None)
        return {"net": archive.tag_for_network(net), "has_net": bool(net), "era": archive.era_tag(year), "hol": hol,
                "year": _int_year(year), "active": set(active), "month": time.strftime("%B", time.localtime(t)).lower(),
                "at": t}  # when the break airs (moved along break by break), for promos that name a day

    def _bumper(self, ctx: dict | None, room: float, where: str = "out") -> dict | None:
        """A bumper or ident from the show's own network ("You're watching Disney Channel" before a Disney
        show, a Nick splat going into a Nick break), or None. On a mixed channel each show gets its own
        network's; a show from a network with no bumpers gets none rather than someone else's. In season,
        that holiday's bumpers come first; out of season they stay on the shelf."""
        if not self.bumpers or not ctx or room < 3:
            return None
        nets = {n for n in (self.home_net or ctx.get("net"),) if n}
        if ctx.get("prefer") and any(t == "25days" for t, _w in ctx["prefer"]):
            nets.add("25days")
        if not nets:
            return None
        active = ctx.get("active", set())
        recent = set(self.st["recent_b"])
        cands = []
        for b in self.bumpers:
            tags = {str(x).lower() for x in b.get("tags") or []}
            if not tags & nets or b["dur"] > room:
                continue
            place = self._place(b)
            if place and place != where:
                continue  # "now back to the show" going into a break, or "we'll be right back" coming out
            fits, want = self._fits(b, ctx)
            if not fits:
                continue  # "you're watching iCarly" during SpongeBob
            hol = self._hol(b)
            if hol and not hol & active:
                continue  # a Christmas bumper in July
            score = (3 if hol else 0) + _year_match(item_year(b), ctx, tags) - (5 if b["id"] in recent else 0) + want
            cands.append((score, b))
        if not cands:
            return None
        best = max(s for s, _ in cands)
        pick = self._rng().choice([b for s, b in cands if s == best])
        keep = max(1, min(12, len(self.bumpers) // 2))
        self.st["recent_b"] = (self.st["recent_b"] + [pick["id"]])[-keep:]
        return pick

    def _overfill(self, left: float, ctx: dict | None) -> dict | None:
        """The commercial (or bumper) that fills `left` seconds while running over by the least, and at
        most OVERFILL_MAX: the next show then starts a few seconds after the half hour (the guide still
        says the half hour) instead of the break ending on a filler card."""
        ctx = ctx or {}
        active = ctx.get("active", set())
        recent = set(self.st["recent"])
        best = None
        for f in self.filler:
            if not left <= f["dur"] <= left + OVERFILL_MAX:
                continue
            if not self._in_season(f, active):
                continue  # a Christmas commercial in July
            if self._place(f):
                continue  # "we'll be right back" / "now back to": they have their own places
            if not self._fits(f, ctx)[0]:
                continue
            key = (f["id"] in recent, round(f["dur"] - left), -(self._ad_score(f, ctx) if ctx else 0))
            if best is None or key < best[0]:
                best = (key, f)
        if best:
            self.st["recent"] = (self.st["recent"] + [best[1]["id"]])[-max(1, min(RECENT_FILLER, len(self.filler) // 2)):]
        return best[1] if best else None

    def _put(self, items: list, pos: float, f: dict) -> float:
        items.append({"s": pos, "e": pos + f["dur"], "k": "f", "m": f["id"], "in": f.get("in", 0),
                      "t": f.get("title", ""), **_src(f)})
        return pos + f["dur"]

    def _holiday_ids(self, key: str) -> set:
        found = seasons.matches(self.cat, key, self.holidays.get(key) or {})
        return found["episodes"] | found["movies"]

    def _ad_score(self, f: dict, ctx: dict) -> float:
        tags = {str(x).lower() for x in f.get("tags") or []}
        score = 0.0
        if ctx.get("net") and ctx["net"] in tags:
            score += 4  # a Disney episode gets Disney commercials
        elif ctx.get("has_net") and tags & NETWORK_TAGS:
            score -= 2  # ...and a Cartoon Network show would rather have plain ads than Nick's
        hol = self._hol(f)
        if hol and hol & ({ctx["hol"]} if ctx.get("hol") else ctx.get("active", set())):
            score += 3 if ctx.get("hol") else 2  # Halloween ads around Halloween shows, or just in October
        if ctx.get("month") and ctx["month"] in tags:
            score += 1.5  # a tape recorded this time of year
        if ctx.get("era") and ctx["era"] in tags:
            score += 1  # 2000s ads around a 2000s episode
        fy, ey = item_year(f), ctx.get("year")
        if fy and ey and abs(fy - ey) <= 2:
            score += 1.5  # and ads from around the year it aired
        for tag, weight in ctx.get("prefer", ()):
            if tag in tags:
                score += weight  # e.g. the 25 Days channel wants that year's real 25 Days ads
        if ctx.get("kids_ads"):
            if KIDS_AD.search(f.get("title") or ""):
                score += ctx["kids_ads"]  # a cereal or toy commercial
            elif tags & KIDS_TAGS:
                score += ctx["kids_ads"] * 0.6  # one taped off a kids' network
        return score

    def _pick_filler(self, seconds: float, ctx: dict | None = None) -> tuple[list[dict], float]:
        if not self.filler or seconds < self.min_filler:
            return [], seconds
        rng = self._rng()
        ctx = ctx or {}
        # Holiday commercials stay off the air outside their season, like holiday episodes.
        active = ctx.get("active", set())
        pool = [f for f in self.filler if self._in_season(f, active)]
        rng.shuffle(pool)
        if self.heard:  # promos and bumpers that name a show only where they make sense
            pool = [f for f in pool if self._fits(f, ctx)[0]]
        recent_kinds = self.st.get("recent_kinds") or []
        if ctx or self.heard:  # best matches first; the shuffle keeps equally good ones varied
            pool.sort(key=lambda f: -(self._ad_score(f, ctx) if ctx else 0.0) - self._fits(f, ctx)[1]
                      + (0.6 * recent_kinds.count(self._kind(f)) if self._kind(f) else 0.0))
        recent = set(self.st["recent"])
        chosen, used = [], set()
        remaining = seconds
        kinds: list[str] = []

        def same_again(f) -> bool:  # not two cereal ads in a row, nor more than two of a kind a break
            k = self._kind(f)
            return bool(k) and ((kinds and kinds[-1] == k) or kinds.count(k) >= 2)
        for allow_recent, mix in ((False, True), (True, True), (False, False), (True, False)):
            # past the strict passes, the kinds used least in this break come first
            for f in (pool if mix or not self.heard else
                      sorted(pool, key=lambda f: kinds.count(self._kind(f)) if self._kind(f) else 0)):
                if remaining < self.min_filler:
                    break
                if f["id"] in used or (not allow_recent and f["id"] in recent) or (mix and same_again(f)):
                    continue
                place = self._place(f)
                if place and place in self._break_places:
                    continue  # one "we'll be right back" and one "now back to" a break
                if f["dur"] <= remaining:
                    if place:
                        self._break_places.add(place)
                    chosen.append(f)
                    kinds.append(self._kind(f))
                    used.add(f["id"])
                    remaining -= f["dur"]
        if self.heard:
            self.st["recent_kinds"] = (recent_kinds + [k for k in kinds if k])[-12:]
            # play them in an order that never puts the same kind back to back, where that can be helped
            spread: list[dict] = []
            while chosen:
                last = self._kind(spread[-1]) if spread else ""
                j = next((i for i, f in enumerate(chosen) if not last or self._kind(f) != last), 0)
                spread.append(chosen.pop(j))
            chosen = spread
        # Tighten: swap a chosen one for an unused one a little longer, to use up what's left over (else
        # it's a few seconds of stand-in card at the end of the break). Best matches first, as above.
        spare = [f for f in pool if f["id"] not in used]
        for _ in range(8):
            if remaining < 0.5 or not chosen:
                break
            best = None
            for i, c in enumerate(chosen):
                for f in spare:
                    gain = f["dur"] - c["dur"]
                    if 0 < gain <= remaining + 1e-6 and (best is None or gain > best[0] + 1e-6):
                        best = (gain, i, f)
            if best is None:
                break
            gain, i, f = best
            spare.remove(f)
            spare.append(chosen[i])
            used.discard(chosen[i]["id"])
            used.add(f["id"])
            chosen[i] = f
            remaining -= gain
        keep = min(RECENT_FILLER, max(0, len(self.filler) // 2))
        self.st["recent"] = (self.st["recent"] + [f["id"] for f in chosen])[-keep:] if keep else []
        return chosen, remaining

    def _plan_end(self, t: float, dur: float) -> float:
        # your limit: so many minutes a half hour of show (a 22-minute episode counts as one half hour)
        cap = self.per_half_hour * max(1, round(dur / 1800)) if self.per_half_hour else None
        if self.slot and self.filler:
            end = ceil_slot(t + dur, self.slot)
            limit = max(MIN_ALIGN_GAP, MAX_GAP_FRACTION * (end - t))
            if cap is not None:
                limit = min(limit, cap)
            if end - (t + dur) > limit:  # it can't reach the half hour: a break of its own length
                end = t + dur + (min(OFFGRID_BREAK, cap) if cap is not None else OFFGRID_BREAK)
            return end
        if self.filler:
            base = max(OFFGRID_BREAK, round(dur / 900) * 120)
            return t + dur + (min(base, cap) if cap is not None else base)
        return t + dur

    async def _break_points(self, media: list[dict], dur: float, ad_time: float) -> list[float]:
        if not self.filler or ad_time < 90:
            return []
        every = 480 if dur < 4200 else 720
        n = min(int(dur // every), int(ad_time // 45))
        if n <= 0:
            return []
        cands, acc = [], 0.0
        for m in media[:-1]:
            acc += m["dur"]
            cands.append(acc)
        if self.style == "chapters":
            acc = 0.0
            for m in media:
                try:
                    chs = await self.chapters(m)
                except Exception:
                    chs = []
                cands += [acc + c for c in chs if 120 < c < m["dur"] - 120]
                acc += m["dur"]
        spacing = dur / (n + 1)
        chosen: list[float] = []
        for k in range(1, n + 1):
            target = spacing * k
            ok = [c for c in cands if all(abs(c - x) > 120 for x in chosen)]
            best = min(ok, key=lambda c: abs(c - target), default=None)
            if best is not None and abs(best - target) <= spacing / 2:
                chosen.append(best)
            elif self.style == "even" and 120 < target < dur - 120 and all(abs(target - x) > 120 for x in chosen):
                chosen.append(target)
        return sorted(chosen)

    # ---- putting a block together ---------------------------------------------------
    def _content(self, items: list, m: dict, start_in: float, length: float, pos: float) -> float:
        self._break_places = set()  # the next break is a new one
        title = m.get("title", "")
        if m.get("artist"):
            title = f"{m['artist']} – {title}"
        items.append({"s": pos, "e": pos + length, "k": "c", "m": m["id"], "in": round(start_in, 3),
                      "t": title, **_src(m)})
        return pos + length

    def _ads(self, items: list, pos: float, seconds: float, *, final: bool,
             ctx: dict | None = None) -> tuple[float, float]:
        if ctx is not None:
            ctx["at"] = pos
        if ctx is not None:
            ctx["closing"] = final  # the break before the next show, not back to this one
        chosen, left = self._pick_filler(seconds, ctx)
        for f in chosen:
            items.append({"s": pos, "e": pos + f["dur"], "k": "f", "m": f["id"], "in": f.get("in", 0),
                          "t": f.get("title", ""),
                          **_src(f)})
            pos += f["dur"]
        # Time the commercials can't fill: one of the network's bumpers if one fits, and if some is still
        # left, one more commercial with the break running a little long. The bumper goes last, coming out
        # of the break into the show.
        b = self._bumper(ctx, left) if final and left >= 3 else None
        rest = left - (b["dur"] if b else 0.0)
        over = self._overfill(rest, ctx) if final and rest > 1.0 else None
        if over:
            pos = self._put(items, pos, over)
            rest = 0.0
        if b:
            pos = self._put(items, pos, b)
        if final and (b or over):
            left = rest
        if final and left > 1.0:  # nothing fits at all: the network's logo (a station ID), see streaming
            items.append({"s": pos, "e": pos + left, "k": "x", "m": "", "in": 0, "t": ""})
            pos += left
            left = 0.0
        return pos, left

    def _describe(self, prog: dict, media: list[dict], s: float, e: float) -> dict:
        b = {"s": s, "e": e, "title": "", "sub": "", "desc": "", "sn": 0, "en": 0, "img": "", "cats": [],
             "movie": False, "year": 0, "date": "", "rating": ""}
        first = media[0]
        if prog["kind"] in ("show", "episodes"):
            show = self.cat.shows.get(first.get("show") or prog.get("show"), {})
            b.update(title=show.get("title", ""), img=show.get("id", ""), year=show.get("year", 0),
                     rating=show.get("rating", ""), cats=list(show.get("genres", []))[:3] + ["Series"],
                     sn=first.get("season", 0), en=first.get("episode", 0), date=first.get("date", ""))
            b["sub"] = " / ".join(m.get("title", "") for m in media)
            b["desc"] = first.get("summary", "") or show.get("summary", "")
        elif prog["kind"] == "movies":
            b.update(title=first.get("title", ""), img=first["id"], year=first.get("year", 0),
                     rating=first.get("rating", ""), cats=["Movie"] + list(first.get("genres", []))[:3],
                     movie=True, desc=first.get("summary", ""), date=first.get("date", ""))
        else:
            names = [f"{m.get('artist')} – {m.get('title')}" if m.get("artist") else m.get("title", "")
                     for m in media]
            b.update(title="Music Videos", sub=", ".join(names[:3]) + ("…" if len(names) > 3 else ""),
                     desc="\n".join(names), cats=["Music"])
        return b

    def _slate_block(self, t: float, length: float, title: str, desc: str):
        items = [{"s": t, "e": t + length, "k": "x", "m": "", "in": 0, "t": title}]
        block = {"s": t, "e": t + length, "title": title, "sub": "", "desc": desc, "sn": 0, "en": 0, "img": "",
                 "cats": [], "movie": False, "year": 0, "date": "", "rating": ""}
        return items, block

    def _off_season_block(self, t: float):
        conf = self.holidays.get(self.ch["season"]) or {}
        nxt = seasons.next_start(conf, t)
        end = t + 6 * 3600 if nxt is None else min(nxt, t + 6 * 3600)
        if end != nxt and self.slot:
            end = max(floor_slot(end, self.slot), t + self.slot)
        if nxt is None:
            title = f"{self.ch['name']} is taking a break"
            desc = "This holiday is switched off in Rerun's Holidays page."
        else:
            title = f"{self.ch['name']} returns {seasons.friendly_date(conf.get('start', ''))}"
            desc = "This holiday channel is on the air during the season."
        return self._slate_block(t, end - t, title, desc)

    async def build_block(self, t: float):
        if self.ch.get("plan"):
            return await self._plan_block(t)
        if self.ch.get("season") and not self.quick and not seasons.in_window(self.holidays.get(self.ch["season"]) or {}, t):
            return self._off_season_block(t)
        if not self.programs:
            length = (ceil_slot(t + 1, self.slot) - t) if self.slot else 1800
            return self._slate_block(t, max(length, 60), "Nothing scheduled yet",
                                     "This channel has no shows yet. Add some in Rerun.")
        extra: dict = {}
        picked = self._pick_2000s(t) if self.xstyle == "2000s" else None
        if picked:
            prog, media, extra = picked
        else:
            prog = self._next_program()
            for _ in range(len(self.programs) * 4):  # skip e.g. a Christmas-only special in July
                if not self._all_shelved(prog, t):
                    break
                prog = self._next_program()
            media = self._next_media(prog, t)
            for _ in range(len(self.programs) * 2):  # a show with nothing it can play right now: try another
                if media:
                    break
                prog = self._next_program()
                if not self._all_shelved(prog, t):
                    media = self._next_media(prog, t)
        if not media:
            return self._slate_block(t, 300, "Coming up", "")
        if self.quick:
            return self._quick_block(prog, media, t)
        dur = sum(m["dur"] for m in media)
        end = extra.get("end") or self._plan_end(t, dur)
        items, block = await self._layout(prog, media, t, end, extra=extra)
        if self.kids and saturday_morning(t) and prog["kind"] in ("show", "episodes") and not self.quick:
            block["new"] = True  # Plex shows these as NEW in its guide; promos say "All-new!"
            block["desc"] = "All-new! " + block["desc"] if block["desc"] else "All-new!"
        return items, block

    async def _layout(self, prog: dict, media: list[dict], t: float, end: float, *, first_in: float = 0.0,
                      pods: list | None = None, extra: dict | None = None):
        """Lay out a block: the program cut at its breaks, commercials in between, ending at `end`.
        `first_in` starts the first item part-way (tuning in late); `pods` are recorded breaks to replay
        as they aired: [(seconds into the program, [commercials])]."""
        extra = extra or {}
        full = sum(m["dur"] for m in media)
        dur = full - first_in
        end = max(end, t + dur)
        ad_time = end - t - dur
        ctx = self._break_context(prog, media, t, extra) if self.filler or self.bumpers else None
        breaks = [b for b in await self._break_points(media, full, ad_time) if b > first_in + 60]
        pods = {at: clips for at, clips in (pods or []) if first_in + 60 < at < full - 30 and clips}
        if pods:  # recorded breaks where they aired, ordinary ones where the recording has none
            breaks = sorted([*pods, *(b for b in breaks if all(abs(b - a) > 240 for a in pods))])
        share = ad_time / (len(breaks) + 1)
        items: list[dict] = []
        pos, acc, carry = t, 0.0, 0.0
        bumpers = prog["kind"] != "music" and not first_in
        quizzed = False  # one trivia question a show, in its first break
        if bumpers:  # the network's ident right before the show starts
            intro = self._bumper(ctx, min(20.0, ad_time / 3), "out")
            if intro:
                pos = self._put(items, pos, intro)
                carry -= intro["dur"]
        for i, m in enumerate(media):
            seg = first_in if i == 0 else 0.0
            for bp in [b - acc for b in breaks if acc + seg < b <= acc + m["dur"] + 1e-6]:
                pos = self._content(items, m, seg, bp - seg, pos)
                seg = bp
                if bp + acc in pods:
                    pos, carry = self._pod(items, pos, pods[bp + acc], share + carry)
                else:
                    if ctx is not None:
                        ctx["at"] = pos
                    lead = self._bumper(ctx, min(15.0, (share + carry) / 4), "into") if bumpers and share + carry >= 60 else None
                    if lead:  # "we'll be right back" into the break
                        pos = self._put(items, pos, lead)
                        carry -= lead["dur"]
                    quiz = self._trivia(prog, media) if (TRIVIA_ON and self.kids and bumpers and not quizzed
                                                         and share + carry >= 75) else None
                    if quiz:  # "Guess the show!" now, the answer at the end of the break
                        quizzed = True
                        pos = self._quiz_card(items, pos, quiz, "ask")
                        pos, carry = self._ads(items, pos, share + carry - TRIVIA_ASK - TRIVIA_ANSWER, final=False,
                                               ctx=ctx)
                        pos = self._quiz_card(items, pos, quiz, "answer")
                    else:
                        pos, carry = self._ads(items, pos, share + carry, final=False, ctx=ctx)
            if m["dur"] - seg > 0.05:
                pos = self._content(items, m, seg, m["dur"] - seg, pos)
            acc += m["dur"]
        if end - pos > 0.05:
            pos, _ = self._ads(items, pos, end - pos, final=True, ctx=ctx)
        self._spread_breaks(items)
        # a hair short of the half hour: stretched to it; a break that ran long keeps its last one whole
        items[-1]["e"] = max(end, items[-1]["e"], items[-1]["s"] + 0.05)
        block = self._describe(prog, media, t, items[-1]["e"])
        if extra.get("title"):
            block.update(title=extra["title"], sub=extra.get("sub", ""))
        notes = [x for x in (extra.get("note"), self._countdown(t)) if x]
        if notes:
            block["desc"] = " · ".join(notes) + ("\n" + block["desc"] if block["desc"] else "")
        return items, block

    def _quick_block(self, prog: dict, media: list[dict], t: float):
        """A test channel's block: the show's ident, a short bit of the show, a bumper into a short break,
        a little more of the show (with "Up next" near its end), then the closing break. The same pieces
        as a real block, just minutes long, so everything can be checked in Plex without waiting."""
        part = float(self.quick.get("part") or 75)
        m = media[0]
        ctx = self._break_context(prog, media, t) if self.filler or self.bumpers else None
        items: list[dict] = []
        pos = t
        intro = self._bumper(ctx, 20.0, "out")
        if intro:
            pos = self._put(items, pos, intro)
        first = min(part, max(10.0, m["dur"] / 2))
        start_in = min(max(0.0, m["dur"] * 0.1), max(0.0, m["dur"] - 2 * first))  # skip the cold open
        pos = self._content(items, m, start_in, first, pos)
        for last in (False, True):
            lead = self._bumper(ctx, 15.0, "into")
            if lead:
                pos = self._put(items, pos, lead)
            pos, _ = self._ads(items, pos, 50.0 if not last else 70.0, final=last, ctx=ctx)
            if not last:
                pos = self._content(items, m, start_in + first, min(part, max(10.0, m["dur"] - start_in - first)), pos)
        block = self._describe(prog, [m], t, items[-1]["e"])
        return items, block

    def _trivia(self, prog: dict, media: list[dict]) -> dict | None:
        """A show for "Guess the show!": another of this channel's shows, and a moment from one of its
        episodes to show (a third to two thirds of the way in, past the opening titles)."""
        now_show = (media[0] if media else {}).get("show") or prog.get("show")
        shows = sorted({p["show"] for p in self.programs.values() if p["kind"] == "show" and p.get("show")
                        and p["show"] != now_show and self.cat.episodes.get(p["show"])})
        if not shows:
            return None
        rng = self._rng()
        sid = rng.choice(shows)
        ep = rng.choice(self.cat.episodes[sid])
        show = self.cat.shows.get(sid, {})
        return {"show": sid, "answer": show.get("title", ""), "ep": ep,
                "at": round(ep["dur"] * rng.uniform(0.33, 0.66), 1)}

    def _quiz_card(self, items: list, pos: float, quiz: dict, phase: str) -> float:
        dur = TRIVIA_ASK if phase == "ask" else TRIVIA_ANSWER
        items.append({"s": pos, "e": pos + dur, "k": "q", "m": quiz["ep"]["id"], "in": quiz["at"],
                      "t": "Guess the show!" if phase == "ask" else f"It's {quiz['answer']}!",
                      "q": {"phase": phase, "show": quiz["show"], "answer": quiz["answer"]}, **_src(quiz["ep"])})
        return pos + dur

    def _pod(self, items: list, pos: float, clips: list[dict], seconds: float) -> tuple[float, float]:
        """A recorded break, in its original order, as much of it as there's room for."""
        left = seconds
        for f in clips:
            if f["dur"] > left + 2:
                break
            items.append({"s": pos, "e": pos + f["dur"], "k": "f", "m": f["id"], "in": f.get("in", 0),
                          "t": f.get("title", ""), "aired": 1, **_src(f)})
            pos += f["dur"]
            left -= f["dur"]
        return pos, left

    def _spread_breaks(self, items: list):
        """Every break in the block, put in an order that never has the same kind of commercial twice in a
        row where that can be helped (a break is often several picks back to back: the last part's, the
        closing one's, one more to fill). Bumpers keep their places, first or last; a recorded break
        replayed as it aired keeps its order."""
        if not self.heard:
            return
        bumper = {b["id"] for b in self.bumpers}
        kind = lambda x: (self.heard.get(x["m"]) or {}).get("cat") or ""  # noqa: E731
        i = 0
        while i < len(items):
            if items[i]["k"] != "f":
                i += 1
                continue
            j = i
            while j < len(items) and items[j]["k"] == "f":
                j += 1
            run = items[i:j]
            a, b = 0, len(run)
            while a < b and run[a]["m"] in bumper:
                a += 1
            while b > a and run[b - 1]["m"] in bumper:
                b -= 1
            middle = run[a:b]
            if any(x.get("aired") for x in middle):  # a recorded break replayed as it aired keeps its order
                i = j
                continue
            # "we'll be right back" right after the bumpers going in; "now back to the show" last of all,
            # right before the show
            into = [x for x in middle if self._place(x["m"]) == "into"]
            outs = [x for x in middle if self._place(x["m"]) == "out"]
            moved = bool(into or outs)
            if moved:
                middle = [x for x in middle if x not in into and x not in outs]
                run = run[:a] + into + middle + run[b:] + outs
                a, b = a + len(into), a + len(into) + len(middle)
            if len(middle) > 2:
                out: list = []
                while middle:  # the kind with the most still to place goes next, if it isn't the last one's
                    last = kind(out[-1]) if out else (kind(run[a - 1]) if a else "")
                    left = Counter(kind(x) for x in middle if kind(x) not in ("", "other"))
                    ok = [n for n, x in enumerate(middle) if kind(x) in ("", "other") or kind(x) != last]
                    k = max(ok, key=lambda n: (left.get(kind(middle[n]), 0), -n)) if ok else 0
                    out.append(middle.pop(k))
                run[a:b] = out
                moved = True
            if moved:
                pos = items[i]["s"]  # where the break starts (its first item may have moved)
                for x in run:
                    d = x["e"] - x["s"]
                    x["s"], x["e"] = pos, pos + d
                    pos += d
                items[i:j] = run
            i = j

    def _countdown(self, t: float) -> str:
        if not (self.ch.get("plan") or self.xstyle):
            return ""
        n = xmas.days_until_christmas(t)
        if n is None:
            return ""
        return "Christmas Eve!" if n == 1 else f"{n} days until Christmas"

    # ---- the 2000s Christmas channel ------------------------------------------------------
    def _pick_2000s(self, t: float):
        """A 2000s cable Christmas day: what's on depends on the time (see xmas.DAYPARTS)."""
        cat = self.cat
        if self._groups is None:
            ids = set()
            for p in self.programs.values():
                if p["kind"] == "show":
                    ids.update(e["id"] for e in cat.episodes.get(p["show"], []))
                elif p["kind"] in ("episodes", "movies"):
                    ids.update(p["items"])
            groups: dict[str, dict[str, list]] = {}
            for i in sorted(ids):
                if i not in cat.items:
                    continue
                x = cat.items[i]
                year = x.get("year") or int((x.get("date") or "0")[:4] or 0) or \
                    cat.shows.get(x.get("show"), {}).get("year", 0)
                sub = "2000s" if 2000 <= int(year or 0) <= 2009 else "other"
                groups.setdefault(xmas.kind_of(cat, i), {}).setdefault(sub, []).append(i)
            self._groups = groups
            self._story = xmas.christmas_story(cat)
        rng = self._rng()
        if self._story and xmas.in_marathon(t) and self._story in cat.items:
            m = cat.items[self._story]
            end = t + 7200 if m["dur"] <= 6600 else ceil_slot(t + m["dur"] + 900, 1800)
            prog = {"kind": "movies", "key": "marathon", "items": [self._story]}
            return prog, [m], {"end": end, "note": "24 Hours of A Christmas Story"}
        part, mix = xmas.daypart(t)
        kinds = [(k, w) for k, w in mix.items() if self._groups.get(k)]
        if not kinds:
            return None
        kind = rng.choices([k for k, _ in kinds], weights=[w for _, w in kinds])[0]
        subs = self._groups[kind]
        sub = "2000s" if subs.get("2000s") and (not subs.get("other") or rng.random() < 0.6) else "other"
        ids = subs[sub]
        pick = self._take(f"x:{kind}:{sub}", ids, False)
        if not pick:
            return None
        media = [cat.items[pick]]
        if pick not in cat.movies:
            media = self._pack(f"x:{kind}:{sub}", ids, False, media)
        prog = {"kind": "movies" if pick in cat.movies else "episodes", "key": f"x:{kind}", "items": ids,
                "show": media[0].get("show")}
        return prog, media, {"note": part}

    # ---- ABC Family's 25 Days of Christmas -------------------------------------------------
    def _plan_item(self, key: str) -> dict | None:
        cache = self._found  # per build, so titles you add are picked up
        if key not in cache:
            got = xmas.find_key(self.cat, key)
            cache[key] = got["id"] if got else ""
        return self.cat.items.get(cache[key]) if cache[key] else None

    def _pods_for(self, key: str, year: int, full: float) -> list:
        if self._airings is None:
            try:
                import json
                self._airings = json.loads(archive.AIRINGS.read_text())
            except (OSError, ValueError):
                self._airings = []
        rec = xmas.match_airing(self._airings, key, year)
        if not rec:
            return []
        if self._by_file is None:
            self._by_file = defaultdict(list)
            for f in self.cat.all_filler():
                if f.get("file"):
                    self._by_file[f["file"]].append(f)
            for lst in self._by_file.values():
                lst.sort(key=lambda f: f.get("in", 0))
        return [(b["at"], self._by_file.get(b["file"], [])) for b in rec["breaks"] if b["at"] < full - 30]

    def _stand_in(self, length: float) -> list[dict]:
        """Something Christmassy from your library that fits a slot you don't have the title for."""
        found = seasons.matches(self.cat, "christmas", self.holidays.get("christmas") or {})
        ids = sorted(i for i in found["movies"] | found["episodes"] if i in self.cat.items)
        fit = [i for i in ids if self.cat.items[i]["dur"] <= length * 0.85]
        if not fit:
            return []
        rng = self._rng()
        best = [i for i in fit if self.cat.items[i]["dur"] >= length * 0.45] or fit
        out, total = [], 0.0
        for i in rng.sample(best, len(best)):
            d = self.cat.items[i]["dur"]
            if total + d <= length * 0.85:
                out.append(self.cat.items[i])
                total += d
            if total >= length * 0.55 or len(out) >= 4:
                break
        return out

    async def _plan_block(self, t: float):
        plan = self.ch["plan"]
        year = int(plan["year"])
        season = datetime.fromtimestamp(t).year
        w0, w1 = xmas.plan_window(year, season)
        if not (w0 <= t < w1):
            nxt = w0 if t < w0 else xmas.plan_window(year, season + 1)[0]
            end = min(nxt, t + 6 * 3600)
            return self._slate_block(t, max(60.0, end - t), f"{self.ch['name']} returns December 1",
                                     f"ABC Family's 25 Days of Christmas, as it aired in {year}.")
        slots = xmas.plan_slots(year, season)
        cur = next((s for s in slots if s["s"] <= t < s["e"]), None)
        if cur is None:
            nxt = next((s["s"] for s in slots if s["s"] > t), w1)
            return await self._gap_block(t, nxt, slots)
        info = title_info(cur["key"])
        length = cur["e"] - cur["s"]
        item = self._plan_item(cur["key"])
        media = [item] if item else (self._stand_in(length) if plan.get("stand_ins", True) else [])
        day = f"25 Days of Christmas · Day {cur['day']}"
        if not media:
            return self._slate_block(t, cur["e"] - t, info["title"],
                                     f"{day}. Not in your library yet — see the 25 Days page for what to get.")
        full = sum(m["dur"] for m in media)
        first_in = 0.0
        late = t - cur["s"]
        if late > 20 * 60 and len(media) == 1:  # tuning in part-way through, like real TV
            first_in = min(full - 120, max(0.0, late * full / length))
        pods = self._pods_for(cur["key"], year, full) if item and plan.get("exact_breaks", True) else []
        prog = {"kind": "movies" if media[0]["id"] in self.cat.movies else "episodes", "key": f"plan:{cur['key']}",
                "items": [m["id"] for m in media], "show": media[0].get("show")}
        if item:
            extra = {"title": info["title"], "note": day + (" · with its original commercials" if pods else "")}
        else:
            extra = {"note": f"{day} · standing in for {info['title']}, which isn't in your library"}
        extra["plan"] = year
        return await self._layout(prog, media, t, cur["e"], first_in=first_in, pods=pods, extra=extra)

    async def _gap_block(self, t: float, limit: float, slots: list[dict]):
        """Between listings ABC Family ran its regular shows: this channel's own lineup fills in, never
        running into the next listing."""
        room = limit - t
        nxt = next((s for s in slots if s["s"] >= limit - 1), None)
        coming = f"Coming up: {title_info(nxt['key'])['title']}" if nxt else "Coming up"
        extra = {"plan": int(self.ch["plan"]["year"])}
        if room >= 240 and self.programs:
            for _ in range(12):
                prog = self._next_program()
                media = self._next_media(prog, t)
                dur = sum(m["dur"] for m in media)
                if media and dur <= room:
                    end = self._plan_end(t, dur)
                    if end > limit or limit - end < 300:
                        end = limit
                    return await self._layout(prog, media, t, end, extra=extra)
        length = min(room, 1800) if room > 2400 else room
        items: list[dict] = []
        if self.filler:
            ctx = {"hol": "christmas", "active": {"christmas"}, "prefer": self._plan_prefer(), "month": "december"}
            self._ads(items, t, length, final=True, ctx=ctx)
        else:
            items = [{"s": t, "e": t + length, "k": "x", "m": "", "in": 0, "t": coming}]
        items[-1]["e"] = max(t + length, items[-1]["e"])
        block = {"s": t, "e": items[-1]["e"], "title": coming, "sub": "", "desc": "ABC Family's 25 Days of Christmas",
                 "sn": 0, "en": 0, "img": "", "cats": [], "movie": False, "year": 0, "date": "", "rating": ""}
        return items, block

    def _plan_prefer(self) -> list:
        year = int(self.ch["plan"]["year"])
        return [("25days", 4), (f"y{year}", 4), (f"y{year - 1}", 2), (f"y{year + 1}", 2), ("abcfamily", 3),
                (archive.era_tag(year) or "2000s", 1)]

    async def extend(self, p: Playout, until: float):
        t = p.end
        guard = 0
        while t < until and guard < 20000:
            items, block = await self.build_block(t)
            p.add(items, block)
            t = block["e"]
            guard += 1


class Schedules:
    def __init__(self, app):
        self.app = app
        self.playouts: dict[str, Playout] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.dir = DATA_DIR / "playouts"

    def get(self, cid: str) -> Playout:
        p = self.playouts.get(cid)
        if p is None:
            p = Playout(cid, read_json(self.dir / f"{cid}.json", {}))
            self.playouts[cid] = p
        return p

    def save(self, p: Playout):
        atomic_write_json(self.dir / f"{p.cid}.json", p.to_dict())

    def remove(self, cid: str):
        self.playouts.pop(cid, None)
        try:
            (self.dir / f"{cid}.json").unlink()
        except OSError:
            pass

    def _guide_changed(self):
        tell = getattr(self.app, "guide_changed", None)
        if tell:
            tell()

    def sig(self, ch: dict) -> str:
        return content_signature(ch, self.app.cfg.data.get("holidays"))

    async def replan_breaks(self):
        """Plan every channel with commercials again from the end of what's on now: after commercials were
        removed, so no break is left pointing at files that are gone."""
        now = time.time()
        for ch in list(self.app.cfg.data["channels"]):
            if not (ch.get("commercials") or {}).get("enabled"):
                continue
            p = self.get(ch["id"])
            async with self.locks[ch["id"]]:
                p.keep_until_current(now)
                self.save(p)
            self._guide_changed()
            try:
                await self.ensure(ch, now + 6 * 3600)
            except Exception:
                log.exception("Could not rebuild the schedule for %s", ch.get("name"))

    async def replan_missing(self):
        """After a library scan: a channel whose planned shows are no longer in the library (moved to
        another library, deleted, or given a new id by Plex) is planned again from now, instead of
        playing "We'll be right back" until the old plan runs out days later."""
        now = time.time()
        have = self.app.catalog.items
        for ch in list(self.app.cfg.data["channels"]):
            if not ch.get("enabled", True):
                continue
            p = self.get(ch["id"])
            gone = [x for x in p.items if x["e"] > now and x.get("k") == "c" and x.get("m") and x["m"] not in have]
            if not gone:
                continue
            log.info("%s had %s planned shows that aren't in the library any more; planning it again",
                     ch.get("name"), len(gone))
            current = p.block_at(now)
            current_ok = current is not None and not any(x["s"] < current["e"] for x in gone)
            async with self.locks[ch["id"]]:
                p.cut(current["e"] if current_ok else now + 1)
                if p.end < now + 1:
                    p.blocks.append({"s": now + 1, "e": now + 1, "title": "", "sub": "", "desc": "", "sn": 0,
                                     "en": 0, "img": "", "cats": [], "movie": False, "year": 0, "date": "",
                                     "rating": ""})
                self.save(p)
            self._guide_changed()
            if not current_ok:
                await self.app.streams.stop_channel(ch["id"])  # anyone watching tunes straight into the new plan
            try:
                await self.ensure(ch, now + 6 * 3600)
            except Exception:
                log.exception("Could not rebuild the schedule for %s", ch.get("name"))

    def _start_time(self, ch: dict, now: float) -> float:
        slot = int(ch.get("slot_minutes") or 0) * 60
        return floor_slot(now, slot) if slot else now

    async def ensure(self, ch: dict, until: float | None = None) -> Playout:
        """Make sure the schedule reaches `until`. Builds in small chunks so a live stream
        asking for its next piece never waits long behind a big background build."""
        lock = self.locks[ch["id"]]
        p = self.get(ch["id"])
        now = time.time()
        if until is None:
            until = now + self.app.cfg.data.get("guide_hours", 72) * 3600
        changed = False
        async with lock:
            sig = self.sig(ch)
            if p.sig != sig:
                if p.sig:
                    p.keep_until_current(now)
                    self._guide_changed()
                p.sig = sig
                changed = True
            if p.end < now:
                # Never built, or the server was off for a while: start fresh from now.
                p.clear()
                start = self._start_time(ch, now)
                p.blocks.append({"s": start, "e": start, "title": "", "sub": "", "desc": "", "sn": 0, "en": 0,
                                 "img": "", "cats": [], "movie": False, "year": 0, "date": "", "rating": ""})
                changed = True
        while p.end < until:
            async with lock:
                if p.sig != self.sig(ch):
                    break  # edited while building; the next call starts over from the new rules
                listener = getattr(self.app, "listener", None)
                b = Builder(ch, self.app.catalog, p.state, self.app.chapters.get, self.app.cfg.data.get("holidays"),
                            listener.heard if listener else None)
                await b.extend(p, min(until, p.end + CHUNK))
                p.blocks = [x for x in p.blocks if x["e"] > x["s"]]
                changed = True
        if changed:
            async with lock:
                p._starts = None
                p.prune(now - KEEP_PAST)
                if self.app.channel(ch["id"]):
                    self.save(p)
        return p

    async def restart_now(self, ch: dict):
        """Throw away what's planned from now on and plan the channel afresh from this moment (test mode
        starting or stopping), instead of waiting for the current show to end."""
        now = time.time()
        p = self.get(ch["id"])
        async with self.locks[ch["id"]]:
            p.cut(now + 1)
            p.sig = self.sig(ch)
            if p.end < now + 1:
                p.blocks.append({"s": now + 1, "e": now + 1, "title": "", "sub": "", "desc": "", "sn": 0, "en": 0,
                                 "img": "", "cats": [], "movie": False, "year": 0, "date": "", "rating": ""})
            self.save(p)
        await self.ensure(ch, now + 3 * 3600)

    async def end_test_if_over(self, ch: dict) -> bool:
        """Test mode switches itself off when its time is up. True if it just did."""
        until = (ch.get("quick") or {}).get("until")
        if not until or time.time() < until:
            return False
        ch["quick"] = {}
        self.app.cfg.save()
        await self.app.streams.stop_channel(ch["id"])
        await self.restart_now(ch)
        log.info("Test mode on %s is over", ch.get("name"))
        return True

    async def ensure_all(self, horizon: float | None = None):
        for ch in list(self.app.cfg.data["channels"]):
            if not ch.get("enabled", True):
                continue
            try:
                await self.end_test_if_over(ch)
            except Exception:
                log.exception("Couldn't end test mode on %s", ch.get("name"))
            try:
                await self.ensure(ch, None if horizon is None else time.time() + horizon)
            except Exception:
                log.exception("Could not build the schedule for %s", ch.get("name"))
            await asyncio.sleep(0)

    async def loop(self):
        await self.ensure_all(FIRST_BUILD)
        while True:
            await self.ensure_all()
            self.app.chapters.save()
            await asyncio.sleep(600)
