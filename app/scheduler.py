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
import random
import time
from collections import defaultdict
from datetime import datetime

from . import seasons
from . import archive
from . import lineups, xmas
from .channels import christmas_style, content_signature, filler_pool, network_key, resolve
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

    def blocks_between(self, a: float, b: float) -> list[dict]:
        return [x for x in self.blocks if x["e"] > a and x["s"] < b]


def _src(m: dict) -> dict:
    return {"f": m.get("file") or "", "p": m.get("part") or "", "a": bool(m.get("audio", True))}


class Builder:
    def __init__(self, ch: dict, cat, state: dict, chapters, holidays: dict | None = None):
        self.ch = ch
        self.cat = cat
        self.st = state
        self.chapters = chapters
        self.holidays = holidays or seasons.DEFAULT_HOLIDAYS
        self._shelf, self._shelf_day = set(), ""
        self.slot = int(ch.get("slot_minutes") or 0) * 60
        self.programs = {p["key"]: p for p in resolve(ch, cat, self.holidays)}
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
        self.xstyle = christmas_style(ch)
        self._groups: dict | None = None
        self._airings: list | None = None
        self._by_file: dict | None = None
        self._found: dict = {}
        self._story: str | None = None
        for k in ("bags", "seq", "lastpick"):
            self.st.setdefault(k, {})
        self.st.setdefault("recent", [])
        self.st.setdefault("recent_b", [])

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
            in_order = self.ch.get("order") == "in_order"
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
        return ctx

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
                "active": set(active), "month": time.strftime("%B", time.localtime(t)).lower()}

    def _bumper(self, ctx: dict | None, room: float) -> dict | None:
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
            hol = tags & set(seasons.PRESETS)
            if hol and not hol & active:
                continue  # a Christmas bumper in July
            score = (3 if hol else 0) + (1 if ctx.get("era") and ctx["era"] in tags else 0) \
                - (5 if b["id"] in recent else 0)
            cands.append((score, b))
        if not cands:
            return None
        best = max(s for s, _ in cands)
        pick = self._rng().choice([b for s, b in cands if s == best])
        keep = max(1, min(12, len(self.bumpers) // 2))
        self.st["recent_b"] = (self.st["recent_b"] + [pick["id"]])[-keep:]
        return pick

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
        hol = tags & set(seasons.PRESETS)
        if hol and hol & ({ctx["hol"]} if ctx.get("hol") else ctx.get("active", set())):
            score += 3 if ctx.get("hol") else 2  # Halloween ads around Halloween shows, or just in October
        if ctx.get("month") and ctx["month"] in tags:
            score += 1.5  # a tape recorded this time of year
        if ctx.get("era") and ctx["era"] in tags:
            score += 1  # 2000s ads around a 2000s episode
        for tag, weight in ctx.get("prefer", ()):
            if tag in tags:
                score += weight  # e.g. the 25 Days channel wants that year's real 25 Days ads
        return score

    def _pick_filler(self, seconds: float, ctx: dict | None = None) -> tuple[list[dict], float]:
        if not self.filler or seconds < self.min_filler:
            return [], seconds
        rng = self._rng()
        ctx = ctx or {}
        # Holiday commercials stay off the air outside their season, like holiday episodes.
        active = ctx.get("active", set())
        pool = [f for f in self.filler
                if not ({str(x).lower() for x in f.get("tags") or []} & set(seasons.PRESETS)) - active
                or ({str(x).lower() for x in f.get("tags") or []} & active)]
        rng.shuffle(pool)
        if ctx:  # best matches first; the shuffle keeps equally good ones varied
            pool.sort(key=lambda f: -self._ad_score(f, ctx))
        recent = set(self.st["recent"])
        chosen, used = [], set()
        remaining = seconds
        for allow_recent in (False, True):
            for f in pool:
                if remaining < self.min_filler:
                    break
                if f["id"] in used or (not allow_recent and f["id"] in recent):
                    continue
                if f["dur"] <= remaining:
                    chosen.append(f)
                    used.add(f["id"])
                    remaining -= f["dur"]
        keep = min(RECENT_FILLER, max(0, len(self.filler) // 2))
        self.st["recent"] = (self.st["recent"] + [f["id"] for f in chosen])[-keep:] if keep else []
        return chosen, remaining

    def _plan_end(self, t: float, dur: float) -> float:
        if self.slot and self.filler:
            end = ceil_slot(t + dur, self.slot)
            if end - (t + dur) > max(MIN_ALIGN_GAP, MAX_GAP_FRACTION * (end - t)):
                end = t + dur + OFFGRID_BREAK
            return end
        if self.filler:
            return t + dur + max(OFFGRID_BREAK, round(dur / 900) * 120)
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
        title = m.get("title", "")
        if m.get("artist"):
            title = f"{m['artist']} – {title}"
        items.append({"s": pos, "e": pos + length, "k": "c", "m": m["id"], "in": round(start_in, 3),
                      "t": title, **_src(m)})
        return pos + length

    def _ads(self, items: list, pos: float, seconds: float, *, final: bool,
             ctx: dict | None = None) -> tuple[float, float]:
        chosen, left = self._pick_filler(seconds, ctx)
        for f in chosen:
            items.append({"s": pos, "e": pos + f["dur"], "k": "f", "m": f["id"], "in": f.get("in", 0),
                          "t": f.get("title", ""),
                          **_src(f)})
            pos += f["dur"]
        if final and left > 0.05:
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
        if not media:
            return self._slate_block(t, 300, "Coming up", "")
        if self.quick:
            return self._quick_block(prog, media, t)
        dur = sum(m["dur"] for m in media)
        end = extra.get("end") or self._plan_end(t, dur)
        items, block = await self._layout(prog, media, t, end, extra=extra)
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
        if bumpers:  # the network's ident right before the show starts
            intro = self._bumper(ctx, min(20.0, ad_time / 3))
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
                    lead = self._bumper(ctx, min(15.0, (share + carry) / 4)) if bumpers and share + carry >= 60 else None
                    if lead:  # "we'll be right back" into the break
                        pos = self._put(items, pos, lead)
                        carry -= lead["dur"]
                    pos, carry = self._ads(items, pos, share + carry, final=False, ctx=ctx)
            if m["dur"] - seg > 0.05:
                pos = self._content(items, m, seg, m["dur"] - seg, pos)
            acc += m["dur"]
        if end - pos > 0.05:
            pos, _ = self._ads(items, pos, end - pos, final=True, ctx=ctx)
        items[-1]["e"] = max(end, items[-1]["s"] + 0.05)
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
        intro = self._bumper(ctx, 20.0)
        if intro:
            pos = self._put(items, pos, intro)
        first = min(part, max(10.0, m["dur"] / 2))
        start_in = min(max(0.0, m["dur"] * 0.1), max(0.0, m["dur"] - 2 * first))  # skip the cold open
        pos = self._content(items, m, start_in, first, pos)
        for last in (False, True):
            lead = self._bumper(ctx, 15.0)
            if lead:
                pos = self._put(items, pos, lead)
            pos, _ = self._ads(items, pos, 50.0 if not last else 70.0, final=last, ctx=ctx)
            if not last:
                pos = self._content(items, m, start_in + first, min(part, max(10.0, m["dur"] - start_in - first)), pos)
        block = self._describe(prog, [m], t, items[-1]["e"])
        return items, block

    def _pod(self, items: list, pos: float, clips: list[dict], seconds: float) -> tuple[float, float]:
        """A recorded break, in its original order, as much of it as there's room for."""
        left = seconds
        for f in clips:
            if f["dur"] > left + 2:
                break
            items.append({"s": pos, "e": pos + f["dur"], "k": "f", "m": f["id"], "in": f.get("in", 0),
                          "t": f.get("title", ""), **_src(f)})
            pos += f["dur"]
            left -= f["dur"]
        return pos, left

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
        items[-1]["e"] = t + length
        block = {"s": t, "e": t + length, "title": coming, "sub": "", "desc": "ABC Family's 25 Days of Christmas",
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

    def sig(self, ch: dict) -> str:
        return content_signature(ch, self.app.cfg.data.get("holidays"))

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
                b = Builder(ch, self.app.catalog, p.state, self.app.chapters.get, self.app.cfg.data.get("holidays"))
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

    async def ensure_all(self, horizon: float | None = None):
        for ch in list(self.app.cfg.data["channels"]):
            if not ch.get("enabled", True):
                continue
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
