"""Scheduler checks with realistic durations. Run: python tests/test_scheduler.py"""
import asyncio
import os
import random
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RERUN_DATA"] = tempfile.mkdtemp()

from app.catalog import Catalog  # noqa: E402
from app.channels import normalize_channel, resolve, suggest  # noqa: E402
from app.scheduler import Builder, Playout, tz_offset  # noqa: E402
from app.seasons import DEFAULT_HOLIDAYS, matches  # noqa: E402
from datetime import datetime  # noqa: E402


def make_catalog():
    rnd = random.Random(1)
    shows, episodes = {}, {}

    def show(sid, title, net, genres, year, n, dur, chapters=True):
        shows[sid] = {"id": sid, "title": title, "network": net, "genres": genres, "year": year, "thumb": "",
                      "summary": "", "rating": "TV-Y7", "src": "plex:1"}
        eps = []
        for i in range(n):
            d = dur + rnd.uniform(-40, 40)
            eps.append({"id": f"{sid}e{i}", "show": sid, "title": f"{title} {i + 1}", "season": 1 + i // 20,
                        "episode": 1 + i % 20, "dur": d, "file": f"/m/{sid}/{i}.mkv", "part": "", "audio": True,
                        "chapters": [0, d * 0.33, d * 0.66] if chapters else []})
        episodes[sid] = eps

    show("p1", "Sponge", "Nickelodeon", ["Animation"], 1999, 60, 11 * 60)        # short segments
    show("p2", "Rugrats", "Nickelodeon", ["Animation", "Family"], 1991, 40, 22 * 60)
    show("p3", "Hey Arnold", "Nickelodeon", ["Animation"], 1996, 30, 22 * 60, chapters=False)
    show("p4", "Drama Hour", "Nickelodeon", ["Drama"], 2001, 20, 44 * 60)
    show("p5", "Long Ep", "Nickelodeon", ["Comedy"], 2003, 10, 31 * 60)          # awkward length
    episodes["p2"][3]["title"] = "The Santa Experience"
    episodes["p2"][7]["title"] = "Rugrats Christmas"
    episodes["p3"][2]["summary"] = "Arnold spends Christmas Eve helping Mr. Hyunh."
    episodes["p1"][5]["title"] = "Scaredy Pants (Halloween)"
    episodes["p1"][9]["title"] = "Jingle Barnacles"
    episodes["p4"][1]["title"] = "Thanksgiving Dinner"
    movies = {f"m{i}": {"id": f"m{i}", "title": f"Movie {i}", "year": 2000, "genres": ["Family"], "network": "Nickelodeon",
                        "dur": rnd.uniform(80, 120) * 60, "file": f"/m/movie{i}.mkv", "part": "", "audio": True,
                        "chapters": [], "thumb": "", "summary": "", "rating": "PG"} for i in range(4)}
    ads = [{"id": f"a{i}", "title": f"Ad {i}", "dur": rnd.choice([15, 15, 30, 30, 30, 60]), "file": f"/m/ads/{i}.mp4",
            "part": "", "audio": True, "tags": ["90s" if i % 2 else "nick"], "src": "folder:/m/ads"} for i in range(120)]
    mvs = [{"id": f"v{i}", "title": f"Song {i}", "artist": f"Band {i % 30}", "year": 1980 + i % 20,
            "dur": rnd.uniform(180, 300), "file": f"/m/mv/{i}.mp4", "part": "", "audio": True, "tags": [],
            "src": "folder:/m/mv"} for i in range(200)]
    return Catalog({"shows": shows, "episodes": episodes, "movies": movies, "filler": {"folder:/m/ads": ads},
                    "music_videos": {"folder:/m/mv": mvs}, "networks": {"Nickelodeon": {"logo": "", "category": "kids"}}})


async def chapters(m):
    return m.get("chapters") or []


async def build(ch, cat, hours=48, when=1_800_000_000):
    p = Playout(ch["id"])
    start = when - (when + tz_offset(when)) % 1800  # a local half-hour boundary
    p.blocks.append({"s": start, "e": start})
    b = Builder(ch, cat, p.state, chapters, DEFAULT_HOLIDAYS)
    await b.extend(p, start + hours * 3600)
    p.blocks = [x for x in p.blocks if x["e"] > x["s"]]
    return p, start


def check(p, label, slot):
    items, blocks = p.items, p.blocks
    gaps = [(a["e"], b["s"]) for a, b in zip(items, items[1:]) if abs(a["e"] - b["s"]) > 1e-6]
    assert not gaps, f"{label}: gaps between items {gaps[:3]}"
    assert all(x["e"] > x["s"] for x in items), f"{label}: empty item"
    bgaps = [(a["e"], b["s"]) for a, b in zip(blocks, blocks[1:]) if abs(a["e"] - b["s"]) > 1e-6]
    assert not bgaps, f"{label}: gaps between blocks"
    total = items[-1]["e"] - items[0]["s"]
    kinds = Counter()
    for x in items:
        kinds[x["k"]] += x["e"] - x["s"]
    aligned = sum(1 for b in blocks if slot and abs((b["s"] + tz_offset(b["s"])) % (slot * 60)) < 1e-3)
    seq = [b["title"] for b in blocks]
    repeats = sum(1 for a, b, c in zip(seq, seq[1:], seq[2:]) if a == b == c)
    worst = max(((sum(x["e"] - x["s"] for x in items if b["s"] <= x["s"] < b["e"] and x["k"] != "c")) / (b["e"] - b["s"])
                 for b in blocks), default=0)
    print(f"{label:<22} blocks={len(blocks):>4} on-the-clock={aligned / max(1, len(blocks)):>4.0%}  "
          f"shows={kinds['c'] / total:.0%} ads={kinds['f'] / total:.0%} filler-card={kinds['x'] / total:.1%}  "
          f"worst-block-ads={worst:.0%}  3-in-a-row={repeats}")
    return kinds, total


async def check_matching():
    """Breaks match the show: its network's ads first, plain ads before another network's, holiday ads
    only in season."""
    rnd = random.Random(3)
    shows, episodes = {}, {}
    for sid, title, net, year in (("s1", "Sponge", "Nickelodeon", 2003), ("s2", "Dexter", "Cartoon Network", 1997),
                                  ("s3", "Friends", "NBC", 1995)):
        shows[sid] = {"id": sid, "title": title, "network": net, "genres": [], "year": year, "thumb": "",
                      "summary": "", "rating": "", "src": "plex:1"}
        episodes[sid] = [{"id": f"{sid}e{i}", "show": sid, "title": f"{title} {i}", "season": 1, "episode": i + 1,
                          "dur": 22 * 60, "file": f"/m/{sid}/{i}.mkv", "part": "", "audio": True,
                          "chapters": [0, 440, 880]} for i in range(20)]
    groups = [("nick", ["nick", "2000s"], 30), ("cn", ["cn", "90s"], 30), ("plain", [], 30),
              ("nickhal", ["nick", "2000s", "halloween"], 15), ("xmas", ["christmas"], 10)]
    ads = [{"id": f"{g}{i}", "title": f"{g} {i}", "dur": rnd.choice([15, 30, 30]), "file": f"/m/ads/{g}{i}.mp4",
            "part": "", "audio": True, "tags": tags, "src": "folder:/m/ads"}
           for g, tags, n in groups for i in range(n)]
    cat = Catalog({"shows": shows, "episodes": episodes, "movies": {}, "filler": {"folder:/m/ads": ads},
                   "music_videos": {}, "networks": {}})
    ch = normalize_channel({"id": "mix", "name": "Mix", "seed": 5, "rules": [{"type": "filter", "media": "shows"}],
                            "commercials": {"enabled": True, "break_style": "chapters"}})
    group_of = {a["id"]: a["id"].rstrip("0123456789") for a in ads}

    def ads_by_show(p):
        out = {}
        for b in p.blocks:
            out.setdefault(b["title"], Counter()).update(
                group_of[x["m"]] for x in p.items if x["k"] == "f" and b["s"] <= x["s"] < b["e"])
        return out

    october = datetime(2026, 10, 15, 12).timestamp()
    march = datetime(2027, 3, 10, 12).timestamp()
    po, _ = await build(ch, cat, 48, when=october)
    pm, _ = await build(ch, cat, 48, when=march)
    oct_ads, mar_ads = ads_by_show(po), ads_by_show(pm)
    for label, by in (("October", oct_ads), ("March", mar_ads)):
        for show, cnt in sorted(by.items()):
            total = sum(cnt.values())
            print(f"  {label:<8} {show:<8} " + "  ".join(f"{g}={cnt[g] / total:.0%}" for g, _t, _n in groups))
    share = lambda by, show, gs: sum(by[show][g] for g in gs) / max(1, sum(by[show].values()))  # noqa: E731
    assert share(mar_ads, "Sponge", ["nick"]) > 0.8, "Nick shows should get Nick ads"
    assert share(mar_ads, "Dexter", ["cn"]) > 0.8, "Cartoon Network shows should get Cartoon Network ads"
    assert share(mar_ads, "Friends", ["nick", "cn"]) < 0.2, "an NBC show should get plain ads, not another network's"
    assert all(by[s]["nickhal"] + by[s]["xmas"] == 0 for by in (mar_ads,) for s in by), "holiday ads aired out of season"
    assert sum(oct_ads[s]["xmas"] for s in oct_ads) == 0, "Christmas ads aired in October"
    assert share(oct_ads, "Sponge", ["nickhal"]) > 0.2, "Nick's Halloween ads should lead in October"
    print("matching breaks: OK")


async def main():
    cat = make_catalog()

    nick = normalize_channel({"id": "nick", "name": "Nick", "seed": 7, "rules": [
        {"type": "filter", "media": "shows", "network": "Nickelodeon"},
        {"type": "filter", "media": "movies", "network": "Nickelodeon"}]})
    p, _ = await build(nick, cat)
    kinds, total = check(p, "Nick (30-min slots)", 30)
    assert kinds["x"] / total < 0.02, "too much blank filler"
    assert any(len([1 for x in p.items if x["s"] >= b["s"] and x["e"] <= b["e"] and x["k"] == "c"]) >= 3 for b in p.blocks), \
        "expected mid-show breaks"
    packed = [b for b in p.blocks if b["title"] == "Sponge" and " / " in b["sub"]]
    assert packed, "short episodes should share a half hour"

    # Same inputs give the same schedule (so restarts don't reshuffle what's on).
    p2, _ = await build(nick, cat)
    assert [x["m"] for x in p.items] == [x["m"] for x in p2.items], "schedule isn't deterministic"

    nick_ads = normalize_channel({**nick, "id": "nick2", "commercials": {"enabled": True, "tags": ["nick"]}})
    p3, _ = await build(nick_ads, cat, 6)
    ad_ids = {x["m"] for x in p3.items if x["k"] == "f"}
    assert all(cat.items[a]["tags"] == ["nick"] for a in ad_ids), "folder filter ignored"
    check(p3, "Nick (nick ads only)", 30)

    plain = normalize_channel({"id": "plain", "name": "No ads", "seed": 3, "commercials": {"enabled": False},
                               "rules": [{"type": "filter", "media": "shows", "network": "Nickelodeon"}]})
    p4, _ = await build(plain, cat, 12)
    k4, _ = check(p4, "No commercials", 0)
    assert k4["f"] == 0 and k4["x"] == 0

    mtv = normalize_channel({"id": "mtv", "name": "MTV", "seed": 5, "commercials": {"enabled": True, "break_style": "end"},
                             "rules": [{"type": "filter", "media": "music_videos"}]})
    p5, _ = await build(mtv, cat, 24)
    check(p5, "MTV (videos)", 30)
    titles = [x["t"] for x in p5.items if x["k"] == "c"]
    assert len(titles) - len(set(titles)) < len(titles) * 0.5

    movies = normalize_channel({"id": "mov", "name": "Movies", "seed": 9, "rules": [{"type": "filter", "media": "movies"}]})
    p6, _ = await build(movies, cat, 24)
    check(p6, "Movies", 30)

    inorder = normalize_channel({**plain, "id": "ord", "order": "in_order"})
    p7, _ = await build(inorder, cat, 24)
    rug = [x["m"] for x in p7.items if x["m"].startswith("p2e")]
    assert rug == sorted(rug, key=lambda m: int(m[3:])), "in-order episodes out of order"

    # Holidays
    xmas = matches(cat, "christmas", DEFAULT_HOLIDAYS["christmas"])
    names = sorted(cat.items[e]["title"] for e in xmas["episodes"])
    print("christmas episodes found:", names)
    assert names == ["Hey Arnold 3", "Jingle Barnacles", "Rugrats Christmas", "The Santa Experience"], names
    assert len(matches(cat, "halloween", DEFAULT_HOLIDAYS["halloween"])["episodes"]) == 1

    xchan = normalize_channel({"id": "xmas", "name": "Christmas Channel", "seed": 4, "season": "christmas",
                               "rules": [{"type": "holiday", "key": "christmas"}]})
    july = datetime(2027, 7, 10, 12).timestamp()
    p8, _ = await build(xchan, cat, 24, when=july)
    assert all(b["title"] == "Christmas Channel returns December 1" for b in p8.blocks), p8.blocks[0]["title"]
    print("off-season:", p8.blocks[0]["title"], f"({len(p8.blocks)} blocks)")
    dec = datetime(2026, 12, 10, 12).timestamp()
    p9, _ = await build(xchan, cat, 24, when=dec)
    played = {x["m"] for x in p9.items if x["k"] == "c"}
    assert played and played <= xmas["episodes"] | xmas["movies"], "holiday channel played non-holiday content"
    check(p9, "Christmas channel (Dec)", 30)

    pdec, _ = await build(nick, cat, 72, when=dec)
    pjan, _ = await build(nick, cat, 72)
    share = lambda p: sum(1 for x in p.items if x["k"] == "c" and x["m"] in xmas["episodes"]) / max(1, sum(1 for x in p.items if x["k"] == "c"))  # noqa: E731
    print(f"holiday episodes on Nick: December {share(pdec):.0%}, January {share(pjan):.0%}")
    assert share(pdec) > 0.1, "holiday episodes should show up in December"
    assert share(pjan) == 0, "Christmas episodes shouldn't air in January"
    rug_jan = [x["m"] for x in p7.items if x["m"].startswith("p2e")]
    assert "p2e3" not in rug_jan and "p2e7" not in rug_jan and rug_jan, "in-order channel should skip out-of-season episodes"

    await check_matching()

    print("suggestions:", [s["name"] for s in suggest(cat, [])])
    print("programs:", [(x["key"], x["weight"]) for x in resolve(nick, cat)])
    print("ALL SCHEDULER CHECKS PASSED")


asyncio.run(main())
