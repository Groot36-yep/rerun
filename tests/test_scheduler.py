"""Scheduler checks with realistic durations. Run: python tests/test_scheduler.py"""
import asyncio
import os
import random
import sys
import tempfile
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RERUN_DATA"] = tempfile.mkdtemp()

from app.catalog import Catalog  # noqa: E402
from app.channels import normalize_channel, resolve, suggest  # noqa: E402
from app.scheduler import OVERFILL_MAX, Builder, Playout, tz_offset  # noqa: E402
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
    aligned = sum(1 for b in blocks if slot and (b["s"] + tz_offset(b["s"])) % (slot * 60) < OVERFILL_MAX + 1)
    seq = [b["title"] for b in blocks]
    repeats = sum(1 for a, b, c in zip(seq, seq[1:], seq[2:]) if a == b == c)
    worst = max(((sum(x["e"] - x["s"] for x in items if b["s"] <= x["s"] < b["e"] and x["k"] != "c")) / (b["e"] - b["s"])
                 for b in blocks), default=0)
    print(f"{label:<22} blocks={len(blocks):>4} on-the-clock={aligned / max(1, len(blocks)):>4.0%}  "
          f"shows={kinds['c'] / total:.0%} ads={kinds['f'] / total:.0%} filler-card={kinds['x'] / total:.1%}  "
          f"worst-block-ads={worst:.0%}  3-in-a-row={repeats}")
    return kinds, total


def check_recommend():
    """Shows to get for Nick and Disney: matched by name and year, by decade; a show Plex files under
    another network counts as in the library but not on the channel."""
    from app import recommend
    shows = {"s1": ("SpongeBob SquarePants", 1999, "Nickelodeon"), "s2": ("Danny Phantom", 2004, "Nicktoons"),
             "s3": ("Teenage Mutant Ninja Turtles", 1987, "Syndication"), "s4": ("Kim Possible", 2002, "Disney Channel")}
    cat = Catalog({"shows": {k: {"id": k, "title": t, "year": y, "network": n} for k, (t, y, n) in shows.items()},
                   "episodes": {k: [{"id": k + "e", "show": k, "dur": 1320}] for k in shows}, "movies": {}, "filler": {},
                   "music_videos": {}, "networks": {}})
    rep = recommend.report(cat, "nick", on_channel={"s1"})
    rows = {r["title"]: r for d in rep["decades"] for r in d["shows"]}
    assert [d["decade"] for d in rep["decades"]] == ["90s", "2000s", "2010s"]
    assert rows["SpongeBob SquarePants"]["on_channel"] and rows["Danny Phantom"]["have"]
    assert not rows["Danny Phantom"]["on_channel"], "Nicktoons' Danny Phantom: in the library, not on the channel"
    assert not rows["Teenage Mutant Ninja Turtles"]["have"], "the 1987 Turtles aren't the 2012 series"
    assert recommend.report(cat, "disney")["have"] == 1
    assert recommend.channel_networks({"auto_key": "network:Nicktoons"}) == ["nick"]
    print("shows to get: OK")


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


async def check_moved_files(cat, ch):
    """Files moved after the schedule was planned: a moved file is found where it is now, and a channel
    whose planned shows left the library is planned again from now, not left on blue cards."""
    import copy
    from types import SimpleNamespace
    from app.core import App
    from app.scheduler import Schedules
    now = time.time()
    p, _ = await build(ch, cat, 12, when=now - 600)
    show = next(x for x in p.items if x["k"] == "c" and x["s"] <= now < x["e"])
    moved = copy.deepcopy(cat.to_dict())
    real = os.path.join(tempfile.mkdtemp(), "moved.mkv")
    open(real, "wb").close()
    for e in [*(e for eps in moved["episodes"].values() for e in eps), *moved["movies"].values()]:
        if e["id"] == show["m"]:
            e["file"] = real
    fake = SimpleNamespace(catalog=Catalog(moved), map_path=lambda f: f, plex=lambda: None)
    fake.local_path = lambda f: App.local_path(fake, f)
    assert App.media_input(fake, show) == real, "a moved file should be found where it is now"
    # the whole show left the library: Nick is planned again with what's still there
    sid = cat.items[show["m"]].get("show")
    if sid:
        moved["episodes"].pop(sid)
        moved["shows"].pop(sid)
    else:
        moved["movies"].pop(show["m"])
    stopped = []

    async def stop(cid):
        stopped.append(cid)
    fake = SimpleNamespace(catalog=Catalog(moved), cfg=SimpleNamespace(data={"channels": [ch], "holidays": DEFAULT_HOLIDAYS}),
                           streams=SimpleNamespace(stop_channel=stop), channel=lambda cid: ch, chapters=SimpleNamespace(get=lambda *a: None))
    sch = Schedules(fake)
    sch.playouts[ch["id"]] = p
    await sch.replan_missing()
    left = [x for x in p.items if x["e"] > time.time() + 2 and x["k"] == "c"]
    assert left and all(x["m"] in fake.catalog.items for x in left), "shows that left the library are still planned"
    assert p.end > time.time() + 5 * 3600 and stopped == [ch["id"]]
    print("moved files: OK")


async def check_break_ends(cat):
    """The seconds at the end of a break that the commercials can't fill go to one of the network's
    bumpers when one fits, so the stand-in (a station ID) is short and rare."""
    import copy
    c2 = Catalog(copy.deepcopy(cat.to_dict()))
    rb = random.Random(1)
    for i in range(12):
        c2.filler["folder:/m/ads"].append({"id": f"b{i}", "title": f"bump {i}", "dur": rb.choice([3, 5, 7.5, 10, 12]),
                                           "file": f"/m/b{i}.mp4", "part": "", "audio": True,
                                           "tags": ["nick", "bumper"], "src": "folder:/m/ads"})
    c2.reindex()
    ch = normalize_channel({"id": "nb", "name": "Nick", "seed": 3, "commercials": {"enabled": True},
                            "rules": [{"type": "filter", "media": "shows", "network": "Nickelodeon"}]})
    blank = lambda p: sum(x["e"] - x["s"] for x in p.items if x["k"] == "x" and not x.get("t"))  # noqa: E731
    ads = lambda p, b: sum(x["e"] - x["s"] for x in p.items if b["s"] <= x["s"] < b["e"] and x["k"] != "c")  # noqa: E731
    for c in (cat, c2):  # without and with bumpers: a break ends on a commercial, never on a filler card
        p, _ = await build(ch, c, 24)
        assert blank(p) == 0, blank(p)
        late = [(b["s"] + tz_offset(b["s"])) % 1800 for b in p.blocks]
        on_time = sum(1 for x in late if x <= OVERFILL_MAX + 1 or x >= 1799) / len(late)
        assert on_time >= 0.85, f"only {on_time:.0%} of shows start on the half hour"
        check(p, "Nick, breaks end whole", 30)
    # your limit: 3 minutes of commercials a half hour, whatever the clock
    capped = normalize_channel({**ch, "commercials": {"enabled": True, "per_half_hour": 3}})
    p, _ = await build(capped, c2, 24)
    worst = max(ads(p, b) for b in p.blocks)
    assert worst <= 3 * 60 * 2 + OVERFILL_MAX + 25, worst  # an hour-long show gets two half hours' worth
    print(f"break ends: OK (no filler cards; at 3 min a half hour the longest break is {worst:.0f}s)")


def check_split():
    """A costly overlay (the Nick 2009 animation) late in a long piece: the piece stops just before it, so
    the layers aren't run for the whole episode; the corner logo and earlier banners stay."""
    from app.streaming import SPLIT_BEFORE, ChannelStream
    ov = {"logo": "l.png", "anims": [{"t0": 1300.0, "len": 10.96}],
          "banners": [{"file": "b", "t0": 40.0, "t1": 50.0}, {"file": "c", "t0": 1290.0, "t1": 1300.0}]}
    dur, end, out = ChannelStream._split_for_overlay(None, ov, 1425.0, 10_000.0)
    # it stops before the banner at 1290 too, rather than cut that off part-way (the next piece shows it)
    assert dur == 1290.0 - SPLIT_BEFORE and end == 10_000.0 - 1425.0 + dur, (dur, end)
    assert out["logo"] == "l.png" and "anims" not in out and [b["file"] for b in out["banners"]] == ["b"], out
    same = ChannelStream._split_for_overlay(None, {"anims": [{"t0": 3.0}]}, 30.0, 500.0)
    assert same[0] == 30.0 and same[2]["anims"], "an animation at the start stays in its piece"
    print("split before the animation: OK")


async def check_heard(cat):
    """With every commercial heard (see listen.py): "you're watching" bumpers only during their show, "up
    next" ones only around it, promos only for shows the channel plays, and a mix of kinds in a break."""
    ads = [f for f in cat.all_filler() if "bumper" not in (f.get("tags") or [])]
    kinds = ["cereal", "toy", "food", "movie", "game", "store"]
    heard = {}
    for i, f in enumerate(ads):
        heard[f["id"]] = {"cat": kinds[i % len(kinds)], "shows": [], "role": ""}
    special = {ads[0]["id"]: {"cat": "promo", "shows": ["Rugrats"], "role": "watching"},
               ads[1]["id"]: {"cat": "promo", "shows": ["Hey Arnold!"], "role": "next"},
               ads[2]["id"]: {"cat": "promo", "shows": ["Big Time Rush"], "role": "promo"},  # not on this channel
               ads[3]["id"]: {"cat": "promo", "shows": ["Rugrats"], "role": "promo"}}
    heard.update(special)
    ch = normalize_channel({"id": "hn", "name": "Nick", "seed": 11, "commercials": {"enabled": True, "break_style": "chapters"},
                            "rules": [{"type": "filter", "media": "shows", "network": "Nickelodeon"}]})
    p = Playout(ch["id"])
    start = 1_800_000_000 - (1_800_000_000 + tz_offset(1_800_000_000)) % 1800
    p.blocks.append({"s": start, "e": start})
    b = Builder(ch, cat, p.state, chapters, DEFAULT_HOLIDAYS, heard)
    await b.extend(p, start + 72 * 3600)
    p.blocks = [x for x in p.blocks if x["e"] > x["s"]]
    titles = {sid: s["title"] for sid, s in cat.shows.items()}
    show_of = lambda x: titles.get((cat.items.get(x["m"]) or {}).get("show"), "")  # noqa: E731
    seen = Counter()
    for blk in p.blocks:
        items = [x for x in p.items if blk["s"] <= x["s"] < blk["e"]]
        on = {show_of(x) for x in items if x["k"] == "c"}
        nxt = next((bb for bb in p.blocks if bb["s"] >= blk["e"]), None)
        for x in items:
            if x["m"] == ads[0]["id"]:
                seen["watching"] += 1
                assert "Rugrats" in on, f"'you're watching Rugrats' during {on}"
            if x["m"] == ads[1]["id"]:
                seen["next"] += 1
                assert "Hey Arnold" in on or (nxt and nxt["title"].startswith("Hey Arnold")), f"'up next Hey Arnold' in {on}"
            assert x["m"] != ads[2]["id"], "a promo for a show this channel doesn't play"
        # the kinds in each break: never the same kind twice running
        runs, run = [], []
        for x in items + [{"k": "c", "m": ""}]:  # each break on its own
            if x["k"] == "f" and x["m"] in heard:
                run.append(heard[x["m"]]["cat"])
            elif x["k"] == "c" and run:
                runs.append(run)
                run = []
        for run in runs:
            assert not any(a == b_ and a != "promo" for a, b_ in zip(run, run[1:])), run
    assert seen["watching"], "the Rugrats bumper never played (during Rugrats it should)"
    print(f"heard commercials: OK (you're-watching {seen['watching']}x, up-next {seen['next']}x, never out of place)")


def check_cushion():
    """A break's run of short commercials, each losing time to start up (2.6 s at play speed with
    "even volume", ~0.2 s with a 3 s head start): the stream must stay well ahead of the clock, as it
    didn't before (it wore down to ~2 s, and a hiccup froze the picture)."""
    from app.streaming import TARGET_LEAD, head_start
    lead, low = 0.0, 99.0
    for i in range(200):
        burst = head_start(i, lead, True)
        startup = 2.6 if burst < 3 else 0.2
        piece = random.Random(i).choice([5, 15, 15, 30, 30, 60, 300])
        lead += min(burst, piece) - startup  # made ahead by the head start, lost to starting up
        if i > 3:
            low = min(low, lead)
    assert low >= TARGET_LEAD - 3, f"the cushion fell to {low:.1f}s"
    assert lead < TARGET_LEAD + 14, f"ran too far ahead ({lead:.1f}s)"
    print(f"cushion: OK (never below {low:.1f}s ahead)")


async def main():
    cat = make_catalog()

    # By default shows and movies play straight through, with the commercials between them.
    plain_nick = normalize_channel({"id": "pn", "name": "Nick", "seed": 7, "rules": [
        {"type": "filter", "media": "shows", "network": "Nickelodeon"},
        {"type": "filter", "media": "movies", "network": "Nickelodeon"}]})
    assert plain_nick["commercials"]["break_style"] == "end"
    pp, _ = await build(plain_nick, cat, 24)
    check(pp, "Nick (between shows)", 30)
    for b in pp.blocks:
        parts = [x for x in pp.items if b["s"] <= x["s"] < b["e"] and x["k"] == "c"]
        assert len({x["m"] for x in parts}) == len(parts), f"a show was cut by a break in {b['title']}"
        # each episode or movie plays whole (a break may come between two short episodes sharing a slot)
        assert all(x["in"] == 0 and abs((x["e"] - x["s"]) - cat.items[x["m"]]["dur"]) < 0.01 for x in parts),             f"a part of {b['title']} was cut short"

    # Breaks inside shows, at their act breaks, for those who want them.
    nick = normalize_channel({"id": "nick", "name": "Nick", "seed": 7, "commercials": {"enabled": True, "break_style": "chapters"},
                              "rules": [{"type": "filter", "media": "shows", "network": "Nickelodeon"},
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

    # What goes over the shows: "Up next" on each block's last part, "After the break" before every
    # mid-show break, and at most one promo a block, for something later on (never the very next block).
    from app import onair
    s_on = {"up_next": True, "promos": True}
    kinds_seen = Counter()
    for i, x in enumerate(p.items):
        if x["k"] != "c":
            continue
        b = p.block_at(x["s"])
        texts = onair.plan(p, i, x, b, s_on, b["title"])
        kinds_seen.update(g["kind"] for g in texts)
        spans = sorted((g["t0"], g["t1"]) for g in texts)
        assert all(x["s"] <= a < c <= x["e"] for a, c in spans), (x, texts)
        assert all(c1 < a2 for (_a1, c1), (a2, _c2) in zip(spans, spans[1:])), f"overlapping texts {texts}"
        nxt = p.items[i + 1] if i + 1 < len(p.items) else None
        teaser = [g for g in texts if g["kind"] == "teaser"]
        if teaser:
            assert nxt and nxt["k"] != "c" and nxt["e"] <= b["e"], "a teaser should lead into a mid-show break"
        for g in texts:
            if g["kind"] == "promo":
                assert g["block"]["s"] > b["e"] + 1 and g["label"].split()[0] in ("Tonight", "Today", "Tomorrow",
                                                                                   "All-new")
    promos_per_block = Counter(p.block_at(x["s"])["s"] for i, x in enumerate(p.items) if x["k"] == "c"
                               for g in onair.plan(p, i, x, p.block_at(x["s"]), s_on) if g["kind"] == "promo")
    assert max(promos_per_block.values(), default=0) <= 1
    print("on-screen texts:", dict(kinds_seen))
    assert kinds_seen["teaser"] > 20 and kinds_seen["promo"] > 10 and kinds_seen["up_next"] > 20
    assert not any(onair.plan(p, i, x, p.block_at(x["s"]), {"up_next": True, "promos": False})
                   and any(g["kind"] != "up_next" for g in onair.plan(p, i, x, p.block_at(x["s"]), {"promos": False}))
                   for i, x in enumerate(p.items) if x["k"] == "c"), "promos off should mean only Up next"
    print("tonight at 8:", onair.when_label(datetime(2026, 12, 1, 20).timestamp(), datetime(2026, 12, 1, 17, 10).timestamp()),
          onair.when_label(datetime(2026, 12, 2, 8, 30).timestamp(), datetime(2026, 12, 1, 23).timestamp()))

    # Bumpers from the episode's year win: a 2003 bumper around a 2003 episode, not a 2015 one.
    from app.scheduler import _year_match, item_year
    assert item_year({"id": "y1", "title": "Nickelodeon bumpers 2003"}) == 2003
    assert _year_match(2003, {"year": 2004}, set()) > _year_match(1999, {"year": 2004}, set()) > _year_match(2016, {"year": 2004}, set())

    # Trivia is put away for now: none on the air.
    assert not any(x["k"] == "q" for x in p.items), "trivia is switched off for now"
    assert not any(x["k"] == "q" for x in p5.items), "no trivia on a music channel"

    # Saturday mornings: on a kids' channel each show's next episode in order, marked new; on a general
    # channel, kids' commercials take over the breaks.
    sat = datetime(2027, 3, 13, 6, 0).timestamp()
    assert time.localtime(sat).tm_wday == 5
    ps, start = await build(nick, cat, 6, when=sat)
    new = [b for b in ps.blocks if b.get("new")]
    morning = [b for b in ps.blocks if time.localtime(b["s"]).tm_hour < 12 and not b.get("movie")]
    afternoon = [b for b in ps.blocks if time.localtime(b["s"]).tm_hour >= 12]
    assert new and all(b.get("new") for b in morning) and not any(b.get("new") for b in afternoon)
    by_show = {}
    for x in ps.items:
        b = ps.block_at(x["s"])
        if x["k"] == "c" and b.get("new") and x["m"].startswith("p2e"):
            by_show.setdefault("p2", []).append(int(x["m"][3:]))
    eps = list(dict.fromkeys(by_show.get("p2", [])))
    assert eps == sorted(eps) or eps == sorted(eps)[eps.index(min(eps)):] + sorted(eps)[:eps.index(min(eps))], eps
    general_cat = make_catalog()
    for i, f in enumerate(general_cat.filler.get("folder:/m/ads", [])):
        f["title"] = (f"Kellogg's Frosted Flakes {i}" if i % 4 == 0 else f"Ford Trucks {i}")
        f["tags"] = []
    for s in general_cat.shows.values():
        s["network"] = "NBC"
    general_cat = Catalog({"shows": general_cat.shows, "episodes": general_cat.episodes, "movies": {},
                           "filler": general_cat.filler, "music_videos": {}, "networks": {}})
    gen = normalize_channel({"id": "gen", "name": "Comedy", "seed": 5, "rules": [{"type": "filter", "media": "shows"}]})
    pg, _ = await build(gen, general_cat, 6, when=sat)
    morning_ads = [general_cat.items[x["m"]]["title"] for x in pg.items if x["k"] == "f"
                   and time.localtime(x["s"]).tm_hour < 12]
    cereal = sum("Frosted" in t for t in morning_ads) / max(1, len(morning_ads))
    pw, _ = await build(normalize_channel({**gen, "id": "gen2"}), general_cat, 6,
                        when=datetime(2027, 3, 10, 6, 0).timestamp())
    weekday_ads = [general_cat.items[x["m"]]["title"] for x in pw.items if x["k"] == "f"]
    weekday_cereal = sum("Frosted" in t for t in weekday_ads) / max(1, len(weekday_ads))
    print(f"Saturday morning: {len(new)} new blocks; cereal ads {cereal:.0%} on Saturday vs {weekday_cereal:.0%} on a weekday")
    assert cereal > 0.5 and cereal > weekday_cereal * 1.5
    assert not any(x["k"] == "q" for x in pg.items), "no trivia on a general channel"

    # Test mode starting or stopping: the schedule ends right now, mid-show, and starts again from there.
    cut = Playout("cut", {"items": [dict(x) for x in p.items], "blocks": [dict(b) for b in p.blocks]})
    mid = p.items[len(p.items) // 2]
    t_cut = (mid["s"] + mid["e"]) / 2
    cut.cut(t_cut)
    assert cut.items[-1]["e"] == t_cut and cut.blocks[-1]["e"] == t_cut and cut.end == t_cut
    assert all(x["s"] < t_cut for x in cut.items) and cut.index_at(t_cut - 0.5) is not None

    # A test channel: the same shows a minute or so at a time, every break coming round within minutes,
    # and the Christmas channel's test plays even out of season.
    quick = normalize_channel({**nick, "id": "quick", "quick": {"of": "nick", "key": "network:Nickelodeon", "part": 60}})
    pq, _ = await build(quick, cat, 1)
    check(pq, "Test channel", 0)
    assert all(b["e"] - b["s"] < 6 * 60 for b in pq.blocks), "test channel blocks should be minutes long"
    assert all(x["e"] - x["s"] <= 60.01 for x in pq.items if x["k"] == "c"), "test channel show parts too long"
    assert len(pq.blocks) >= 15 and sum(x["k"] == "f" for x in pq.items) > 40, "expected lots of breaks"
    assert all(sum(1 for x in pq.items if x["k"] == "c" and b["s"] <= x["s"] < b["e"]) == 2 for b in pq.blocks)
    xquick = normalize_channel({"id": "xq", "name": "T", "season": "christmas", "rules": [{"type": "holiday", "key": "christmas"}],
                                "quick": {"of": "xmas"}})
    pxq, _ = await build(xquick, cat, 1, when=datetime(2027, 7, 10, 12).timestamp())
    assert any(x["k"] == "c" for x in pxq.items), "a test of a holiday channel should play out of season"

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
    check_recommend()
    await check_moved_files(cat, plain_nick)
    check_cushion()
    check_split()
    await check_heard(cat)
    await check_break_ends(cat)

    print("suggestions:", [s["name"] for s in suggest(cat, [])])
    print("programs:", [(x["key"], x["weight"]) for x in resolve(nick, cat)])
    print("ALL SCHEDULER CHECKS PASSED")


asyncio.run(main())
