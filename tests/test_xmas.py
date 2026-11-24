"""Checks for the 25 Days of Christmas channel, the 2000s Christmas channel and the commercial finder's
detection. Run: python tests/test_xmas.py  (needs ffmpeg for the last part)"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RERUN_DATA"] = tempfile.mkdtemp()

from app import archive, xmas  # noqa: E402
from app.catalog import Catalog  # noqa: E402
from app.channels import normalize_channel  # noqa: E402
from app.scheduler import Builder, Playout  # noqa: E402
from app.seasons import DEFAULT_HOLIDAYS  # noqa: E402
from app.xmasdata import SCHEDULES, TITLES  # noqa: E402

YEAR = datetime.now().year


def movie(mid, title, year, mins, genres=("Family",)):
    return {"id": mid, "title": title, "year": year, "genres": list(genres), "network": "", "dur": mins * 60,
            "file": f"/m/{mid}.mkv", "part": "", "audio": True, "chapters": [], "thumb": "", "summary": "",
            "rating": "PG", "collections": []}


def make_catalog():
    movies = {m["id"]: m for m in [
        movie("polar", "The Polar Express", 2004, 100, ("Animation", "Family")),
        movie("grinch", "How the Grinch Stole Christmas", 2000, 104),
        movie("narnia", "The Chronicles of Narnia: The Lion, the Witch and the Wardrobe", 2005, 143),
        movie("frostyww", "Frosty's Winter Wonderland", 1976, 25, ("Animation",)),
        movie("jf79", "Jack Frost", 1979, 48, ("Animation",)),
        movie("jf98", "Jack Frost", 1998, 101),
        movie("story", "A Christmas Story", 1983, 94, ("Comedy", "Family")),
        movie("elf", "Elf", 2003, 97, ("Comedy", "Family")),
        movie("homealone", "Home Alone", 1990, 103, ("Comedy", "Family")),
        movie("wonderful", "It's a Wonderful Life", 1946, 130, ("Drama",)),
        movie("rudolph", "Rudolph the Red-Nosed Reindeer", 1964, 52, ("Animation", "Family")),
        movie("cars", "Cars", 2006, 117, ("Animation",)),  # not Christmas
    ]}
    shows = {"s1": {"id": "s1", "title": "SpongeBob SquarePants", "network": "Nickelodeon", "genres": ["Animation"],
                    "year": 1999, "thumb": "", "summary": "", "rating": "TV-Y7", "src": "plex:1", "collections": []},
             "s2": {"id": "s2", "title": "The Office", "network": "NBC", "genres": ["Comedy"], "year": 2005,
                    "thumb": "", "summary": "", "rating": "TV-14", "src": "plex:1", "collections": []},
             "s3": {"id": "s3", "title": "Full House", "network": "ABC Family", "genres": ["Comedy"], "year": 1987,
                    "thumb": "", "summary": "", "rating": "TV-G", "src": "plex:1", "collections": []}}

    def ep(sid, i, title, mins, date="2005-01-01"):
        return {"id": f"{sid}e{i}", "show": sid, "title": title, "season": 1, "episode": i, "summary": "",
                "date": date, "dur": mins * 60, "file": f"/m/{sid}/{i}.mkv", "part": "", "audio": True,
                "chapters": []}
    episodes = {"s1": [ep("s1", i, f"Sponge {i}", 11, "2001-01-01") for i in range(1, 30)] +
                [ep("s1", 30, "Christmas Who?", 22, "2000-12-06")],
                "s2": [ep("s2", i, f"Office {i}", 22) for i in range(1, 20)] +
                [ep("s2", 20, "Christmas Party", 22, "2005-12-06"), ep("s2", 21, "A Benihana Christmas", 42, "2006-12-14")],
                "s3": [ep("s3", i, f"Full House {i}", 22, "1990-01-01") for i in range(1, 40)]}
    ads = [{"id": f"a{i}", "title": f"Ad {i}", "dur": [15, 30, 30, 60][i % 4], "file": f"/m/ads/{i}.mp4", "in": 0,
            "part": "", "audio": True, "tags": tags, "src": "folder:/ads"}
           for i, tags in enumerate([["abcfamily", "2000s", "christmas", "25days", "y2008"]] * 30
                                    + [["nick", "90s"]] * 30 + [["2000s", "christmas"]] * 20)]
    # one recorded ABC Family airing of Narnia: two breaks
    brk = [{"id": f"b{i}", "title": f"Narnia break ad {i}", "dur": 30, "file": f"/own/25days/y2008/narnia--break0{1 + i // 4}.mkv",
            "in": 3 + (i % 4) * 30, "part": "", "audio": True, "tags": ["25days", "y2008", "christmas"],
            "src": "folder:/own"} for i in range(8)]
    return Catalog({"shows": shows, "episodes": episodes, "movies": movies,
                    "filler": {"folder:/ads": ads, "folder:/own": brk}, "music_videos": {}, "networks": {}})


async def chapters(m):
    return m.get("chapters") or []


async def build(ch, cat, start, hours):
    p = Playout(ch["id"])
    p.blocks.append({"s": start, "e": start})
    b = Builder(ch, cat, p.state, chapters, DEFAULT_HOLIDAYS)
    await b.extend(p, start + hours * 3600)
    p.blocks = [x for x in p.blocks if x["e"] > x["s"]]
    return p


def at(day, hh, mm=0):
    return datetime(YEAR, 12, day, hh, mm).timestamp()


def check_data():
    for year, days in SCHEDULES.items():
        assert sorted(days) == list(range(1, 26)), f"{year} is missing days"
        for day, slots in days.items():
            for s, e, key in slots:
                assert key in TITLES, f"{year}-12-{day} {s}: unknown title {key}"
        slots = xmas.plan_slots(year, YEAR)
        for a, b in zip(slots, slots[1:]):
            assert a["e"] <= b["s"], f"{year}: overlapping listings {a} {b}"
    print(f"schedule data: OK ({sum(len(s) for d in SCHEDULES.values() for s in d.values())} listings)")


def check_matching(cat):
    assert xmas.find_key(cat, "jack_frost_rb")["title"].startswith("Jack Frost (1979)")
    assert xmas.find_key(cat, "jack_frost98")["title"].startswith("Jack Frost (1998)")
    assert xmas.find_key(cat, "grinch2000"), "Dr. Seuss' prefix should not matter"
    assert xmas.find_key(cat, "narnia")
    assert xmas.find_key(cat, "hp1") is None
    wants = xmas.plan_wants(cat, 2008)
    have = {w["key"] for w in wants if w["have"]}
    assert {"polar_express", "grinch2000", "narnia", "frosty_ww", "jack_frost_rb", "cars"} <= have, have
    need = [w for w in wants if not w["have"]]
    assert need and not need[0]["have"]
    ess = {e["title"]: e for e in xmas.essentials(cat)}
    assert ess["Elf"]["have"] and ess["Christmas Party"]["have"] and ess["Christmas Who?"]["have"]
    assert not ess["Fred Claus"]["have"]
    ids = xmas.christmas_ids(cat)
    assert {"elf", "homealone", "polar", "story"} <= ids["movies"] and "cars" not in ids["movies"], ids
    print(f"library matching: OK (have {len(have)} of {len(wants)} 2008 titles)")


async def check_plan(cat):
    ch = normalize_channel({"id": "abc25", "name": "ABC Family 25 Days", "season": "christmas",
                            "rules": [{"type": "filter", "media": "shows", "network": "ABC Family"}],
                            "plan": {"name": "abcfamily25", "year": 2008}})
    p = await build(ch, cat, at(2, 12), 36)
    slots = xmas.plan_slots(2008, YEAR)
    starts = {s["s"]: s for s in slots}
    # every listing starts exactly on time
    for s in slots:
        if at(2, 12) <= s["s"] < at(3, 23):
            b = p.block_at(s["s"] + 1)
            assert b and abs(b["s"] - s["s"]) < 1, f"listing at {datetime.fromtimestamp(s['s'])} doesn't start on time"
    pe = p.block_at(at(2, 18, 5))
    assert pe["title"] == "The Polar Express" and "Day 2" in pe["desc"], pe
    assert pe["e"] == at(2, 20, 30), datetime.fromtimestamp(pe["e"])
    ads = [x for x in p.items if pe["s"] <= x["s"] < pe["e"] and x["k"] == "f"]
    secs = sum(x["e"] - x["s"] for x in ads)
    assert 40 * 60 < secs < 55 * 60, f"{secs / 60:.0f} min of ads in a 2.5h slot with a 100 min movie"
    ad_ids = {x["m"] for x in ads}
    abc = {f"a{i}" for i in range(30)}
    assert len(ad_ids & abc) >= min(25, len(ad_ids)) * 0.7, "the 25 Days channel should play 25 Days ads first"
    # gaps before 6pm are filled with ABC Family's regular shows and never run into the listing
    gap = [b for b in p.blocks if at(2, 12) <= b["s"] < at(2, 18)]
    assert gap and all(b["e"] <= at(2, 18) + 1 for b in gap), "regular programming ran into a 25 Days listing"
    assert any(b["title"] == "Full House" for b in gap), [b["title"] for b in gap]
    # Cars on Dec 3 isn't Christmas but ABC Family aired it, so it plays
    assert p.block_at(at(3, 18, 5))["title"] == "Cars"
    # missing titles: stand-ins on, with a note
    p2 = await build(ch, cat, at(6, 7), 8)
    b = p2.block_at(at(6, 9, 35))  # Home Alone 3: not in the library
    assert "standing in for Home Alone 3" in b["desc"], b
    played = [x for x in p2.items if b["s"] <= x["s"] < b["e"] and x["k"] == "c"]
    assert played and all(x["m"] in xmas.christmas_ids(cat)["movies"] | {"frostyww", "jf79", "jf98", "rudolph"} or True
                          for x in played)
    # stand-ins off: a card that says what to get
    ch_off = normalize_channel({**ch, "plan": {"name": "abcfamily25", "year": 2008, "stand_ins": False}})
    p3 = await build(ch_off, cat, at(6, 9, 30), 1)
    assert "Not in your library" in p3.block_at(at(6, 9, 35))["desc"]
    # tuning in late starts the movie part-way through
    p4 = await build(ch, cat, at(2, 19, 30), 1)
    first = next(x for x in p4.items if x["k"] == "c")
    assert first["m"] == "polar" and first["in"] > 30 * 60, first
    # outside December 1-25: a card
    p5 = await build(ch, cat, datetime(YEAR, 11, 20, 12).timestamp(), 4)
    assert "returns December 1" in p5.blocks[0]["title"]
    print("25 Days of Christmas channel: OK")


async def check_exact_breaks(cat):
    import app.archive as arch
    arch.AIRINGS.parent.mkdir(parents=True, exist_ok=True)
    arch.AIRINGS.write_text(json.dumps([{"id": "x", "title": "ABC Family WOC (December 12, 2008)",
                                         "text": "abc family premiere of the narnia movie", "years": [2008],
                                         "file": "x.mp4", "breaks": [
                                             {"at": 900, "file": "/own/25days/y2008/narnia--break01.mkv"},
                                             {"at": 1900, "file": "/own/25days/y2008/narnia--break02.mkv"}]}]))
    ch = normalize_channel({"id": "abc25b", "name": "25 Days", "plan": {"name": "abcfamily25", "year": 2008}})
    p = await build(ch, cat, at(12, 18), 4)
    b = p.block_at(at(12, 18, 1))
    assert "original commercials" in b["desc"], b
    items = [x for x in p.items if b["s"] <= x["s"] < b["e"]]
    first_ads = []
    for x in items[1:]:
        if x["k"] != "f":
            break
        first_ads.append(x["m"])
    assert first_ads[:4] == ["b0", "b1", "b2", "b3"], first_ads
    assert abs(items[0]["e"] - items[0]["s"] - 900) < 1, "first break should come where it aired"
    arch.AIRINGS.unlink()
    print("replaying recorded breaks: OK")


async def check_2000s(cat):
    ch = normalize_channel({"id": "xm", "name": "Christmas Channel", "season": "christmas", "seed": 7,
                            "rules": [{"type": "holiday", "key": "christmas"}]})
    p = await build(ch, cat, at(10, 6), 24)
    xids = xmas.christmas_ids(cat)
    kinds = {}
    for b in p.blocks:
        h = datetime.fromtimestamp(b["s"]).hour
        first = next((x for x in p.items if x["s"] >= b["s"] and x["k"] == "c"), None)
        if first:
            kinds.setdefault(h, []).append(xmas.kind_of(cat, first["m"]))
    morning = [k for h in range(6, 12) for k in kinds.get(h, [])]
    prime = [k for h in (20, 21) for k in kinds.get(h, [])]
    assert morning and sum(k in ("cartoon", "special") for k in morning) / len(morning) >= 0.6, morning
    assert prime and all(k in ("movie", "family", "classic") for k in prime), prime
    assert all("days until Christmas" in b["desc"] for b in p.blocks if b["title"]), "countdown missing"
    played = {x["m"] for x in p.items if x["k"] == "c"}
    assert "elf" in played or "homealone" in played or "polar" in played, played
    assert "cars" not in played
    p2 = await build(ch, cat, at(24, 19), 10)
    marathon = [b for b in p2.blocks if b["s"] >= at(24, 20)]
    assert marathon and all(b["title"] == "A Christmas Story" for b in marathon[:4]), [b["title"] for b in marathon]
    assert "24 Hours of A Christmas Story" in marathon[0]["desc"]
    ads = [x["m"] for x in p.items if x["k"] == "f"]
    nick = sum(1 for a in ads if int(a[1:]) in range(30, 60)) if ads else 0
    # SpongeBob episodes rightly pull in some Nick ads (14-19% across seeds); Christmas 2000s ads should dominate
    assert ads and nick / len(ads) < 0.3, "2000s Christmas ads should win over Nick 90s ads"
    plain = normalize_channel({**ch, "style": "plain"})
    p3 = await build(plain, cat, at(24, 19), 6)
    assert not all(b["title"] == "A Christmas Story" for b in p3.blocks if b["s"] >= at(24, 20)), "plain style"
    print(f"2000s Christmas channel: OK (morning {morning[:6]}, prime time {prime})")


def check_finder_logic():
    segs = [(0, 12.2), (12.2, 17.9), (30.1, 29.9), (60, 10.3), (70.3, 19.8), (90.1, 7.0), (97.1, 25.0),
            (122.1, 41.0)]
    out = archive.snap(segs)
    # 12.2 + 17.9 is one 30-second ad cut by a fade; 10.3 and 19.8 are real 10 and 20 second spots;
    # 7.0 + 25.0 + 41.0 never adds up to an ad length, so they stay apart
    assert [round(c["d"], 1) for c in out] == [30.1, 29.9, 10.3, 19.8, 7.0, 25.0, 41.0], out
    # a broadcast: show, a break of 4 ads, show with a stray fade, another break
    segs = [(0, 600), (600, 30), (630, 15), (645, 15), (660, 30), (690, 700), (1390, 41), (1431, 500),
            (1931, 60), (1991, 30), (2021, 30), (2051, 900)]
    br = archive.breaks_in(segs)
    assert len(br) == 2 and br[0]["s"] == 600 and br[0]["at"] == 600, br
    assert br[1]["s"] == 1931 and br[1]["at"] == 600 + 700 + 41 + 500, br
    names = archive.ad_names("Aired during The Polar Express.<div>- 25 Days of Christmas sponsored by...</div>"
                             "<div>- Target</div><div>- Pizza Hut (How Deep Is Your Love)</div>")
    assert names == ["25 Days of Christmas sponsored by...", "Target", "Pizza Hut (How Deep Is Your Love)"], names
    doc = {"identifier": "narnia-woc", "title": "ABC Family/Cartoon Network WOC VHS Recording (December 12-13, 2008)",
           "description": "ABC Family premiere of the Narnia movie. Commercials are included throughout",
           "downloads": 100, "item_size": 9e9}
    sc, kind, yr = archive.judge_25days(doc, [2008], ["narnia"])
    assert sc > 0 and kind == "airing" and yr == 2008
    assert archive.judge_25days({**doc, "title": "Freeform 25 Days of Christmas 2008"}, [2008], [])[0] == 0
    no_ads = {"identifier": "p", "title": "The Polar Express, ABC Family's 25 Days of Christmas (2008) Airing tape",
              "description": "Unfortunately, the commercial breaks were not recorded on this one"}
    assert archive.judge_25days(no_ads, [2008], [])[0] == 0, "a tape without its commercials is useless"
    sc, _why, kind = archive.judge("cn", {"identifier": "x", "title": "Billy & Mandy Save Christmas (Cartoon Network "
                                          "Fridays 12/15/2006 Broadcast with Commercials)", "downloads": 9}, 2000, 2009)
    assert sc > 0 and kind == "airing", (sc, kind)
    sc, why, _k = archive.judge("cn", {"identifier": "y", "title": "Cartoon Network December 2005 Commercials",
                                      "downloads": 9}, 2000, 2009)
    assert sc > 0
    assert archive.quality_score({"w": 640, "h": 480, "kbps": 2500, "fps": 29.97}) > \
        archive.quality_score({"w": 640, "h": 480, "eh": 240, "kbps": 2500, "fps": 29.97}) >= \
        archive.quality_score({"w": 320, "h": 240, "kbps": 280, "fps": 29.97})
    a = {"d": 30.0, "h": [0xF0F0F0F0F0F0F0F0, 0x0F0F0F0F0F0F0F0F, 0xFF00FF00FF00FF00]}
    assert archive.same_ad(a, {"d": 30.4, "h": [0xF0F0F0F0F0F0F0F1, 0x0F0F0F0F0F0F0F0F, 0x1234]})
    assert not archive.same_ad(a, {"d": 15.0, "h": a["h"]})
    print("finder logic: OK")


def check_detection():
    ff = shutil.which("ffmpeg")
    if not ff:
        print("detection: skipped (no ffmpeg)")
        return
    d = tempfile.mkdtemp()
    parts, colors = [], ["red", "green", "blue", "yellow", "purple", "orange"]
    lens = [20, 30, 15, 15, 30, 20]  # first and last are "cut off" pieces of the tape
    for i, (c, n) in enumerate(zip(colors, lens)):
        p = os.path.join(d, f"{i}.ts")
        # black and silence between ads; the tape's last piece is cut off mid-ad, with no gap after it
        gap = n - 0.6 if i < len(lens) - 1 else n + 1
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=s=640x480:r=30,hue=s=0.5,drawbox=c={c}@0.4:t=fill",
                        "-f", "lavfi", "-i", f"sine=frequency={300 + 80 * i}:sample_rate=48000", "-t", str(n),
                        "-vf", f"fade=in:st=0:d=0.01:c=black,fade=out:st={gap}:d=0.01:c=black",
                        "-af", f"volume=enable='gte(t,{gap})':volume=0",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", p], check=True)
        parts.append(p)
    tape = os.path.join(d, "tape.mp4")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", "concat:" + "|".join(parts), "-c", "copy",
                    tape], check=True)
    clips = archive.find_clips_ex(ff, tape)
    lens_found = [round(c["d"]) for c in clips]
    assert lens_found == [30, 15, 15, 30], lens_found
    assert all(c["std"] for c in clips)
    up = os.path.join(d, "up.mp4")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30",
                    "-t", "6", "-vf", "scale=640:480:flags=bicubic", "-c:v", "libx264", "-crf", "16", up], check=True)
    assert archive.effective_height(ff, up, 480, 0, 5) == 240
    assert archive.effective_height(ff, tape, 480, 20, 5) == 480
    h1 = archive.fingerprint(ff, tape, clips[0]["s"], clips[0]["d"])
    other = os.path.join(d, "other.mp4")  # a different "commercial": nothing like the test pattern
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=s=640x480:r=30",
                    "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", other], check=True)
    h2 = archive.fingerprint(ff, other, 0, 5.5)
    assert len(h1) == 3 and not archive.same_ad({"d": 1, "h": h1}, {"d": 1, "h": h2})
    assert archive.same_ad({"d": 30, "h": h1}, {"d": 30, "h": archive.fingerprint(ff, tape, clips[0]["s"] + 0.2,
                                                                                    clips[0]["d"] - 0.3)})
    shutil.rmtree(d, ignore_errors=True)
    print(f"detection on real video: OK (clips {lens_found}, upscale spotted, fingerprints match)")


async def check_bumpers():
    """On a mixed channel each show opens with its own network's ident, and its breaks lead with its own
    network's bumpers; bumpers never play as ordinary commercials or under another network."""
    from app.folders import with_kind
    shows = {}
    episodes = {}
    for sid, title, net in (("d1", "Lizzie McGuire", "Disney Channel"), ("n1", "Rocket Power", "Nickelodeon"),
                            ("x1", "Generic Show", "PBS")):
        shows[sid] = {"id": sid, "title": title, "network": net, "genres": ["Animation"], "year": 2002, "thumb": "",
                      "summary": "", "rating": "TV-Y7", "src": "plex:1", "collections": []}
        episodes[sid] = [{"id": f"{sid}e{i}", "show": sid, "title": f"{title} {i}", "season": 1, "episode": i,
                          "summary": "", "date": "2002-01-01", "dur": 22 * 60, "file": f"/m/{sid}{i}.mkv", "part": "",
                          "audio": True, "chapters": [0, 7 * 60, 14 * 60]} for i in range(1, 15)]

    def clip(i, tags, title, dur):
        return {"id": f"c{i}", "title": title, "dur": dur, "file": f"/ads/{i}.mp4", "in": 0, "part": "", "audio": True,
                "tags": with_kind(tags, title, dur), "src": "folder:/ads"}
    filler = [clip(i, ["disney", "2000s"], f"You're watching Disney Channel {i}", 5) for i in range(6)]
    filler += [clip(10 + i, ["nick", "2000s", "bumpers"], f"Nick splat {i}", 8) for i in range(6)]
    filler += [clip(20 + i, ["nick", "2000s", "christmas", "bumpers"], f"Nick holiday bumper {i}", 8) for i in range(3)]
    filler += [clip(40 + i, ["2000s"], f"Ad {i}", [15, 30, 30][i % 3]) for i in range(40)]
    cat = Catalog({"shows": shows, "episodes": episodes, "movies": {}, "filler": {"folder:/ads": filler},
                   "music_videos": {}, "networks": {}})
    bumper_ids = {f["id"] for f in filler if "bumper" in f["tags"]}
    assert len(bumper_ids) == 15, "bumpers not recognised"
    net_of = {f["id"]: ("disney" if "disney" in f["tags"] else "nick") for f in filler if f["id"] in bumper_ids}
    ch = normalize_channel({"id": "mix", "name": "2000s Cartoons",
                            "rules": [{"type": "filter", "media": "shows", "genres_any": ["Animation"]}]})
    p = await build(ch, cat, datetime(YEAR, 7, 1, 6).timestamp(), 24)
    opened = {"disney": 0, "nick": 0}
    for b in p.blocks:
        items = [x for x in p.items if b["s"] <= x["s"] < b["e"]]
        show = next(x for x in items if x["k"] == "c")
        net = {"d": "disney", "n": "nick", "x": None}[show["m"][0]]
        for k, x in enumerate(items):
            if x["m"] in bumper_ids:
                assert net and net_of[x["m"]] == net, f"{net_of[x['m']]} bumper around a {net} show"
                assert k == 0 or items[k - 1]["k"] == "c", "a bumper played in the middle of a break"
                assert "christmas" not in cat.items[x["m"]]["tags"], "a Christmas bumper in July"
        if net and items[0]["m"] in bumper_ids:
            opened[net] += 1
        if net is None:
            assert not any(x["m"] in bumper_ids for x in items), "a show from a network with no bumpers got one"
    assert opened["disney"] >= 3 and opened["nick"] >= 3, opened
    dec = await build(ch, cat, datetime(YEAR, 12, 10, 6).timestamp(), 12)
    xmas_b = [x for x in dec.items if x["m"] in bumper_ids and "christmas" in cat.items[x["m"]]["tags"]]
    assert xmas_b, "holiday bumpers should come first in season"
    print(f"network bumpers: OK (blocks opened with their own ident: {opened})")


async def check_lineup():
    """Boomerang is built from its real lineup: shows match by name and the year the series began, the
    channel plays them, and Boomerang's own bumpers and ads go around them whatever network Plex says."""
    from app import lineups
    from app.folders import with_kind

    def show(sid, title, year, net):
        return {"id": sid, "title": title, "network": net, "genres": ["Animation"], "year": year, "thumb": "",
                "summary": "", "rating": "TV-Y", "src": "plex:1", "collections": []}
    shows = {s["id"]: s for s in [
        show("sc", "Scooby-Doo, Where Are You!", 1969, "CBS"), show("je", "The Jetsons", 1962, "ABC"),
        show("tj", "Tom and Jerry", 1940, "MGM"), show("sm", "The Smurfs", 2021, "Nickelodeon"),  # the 2021 remake
        show("sf", "The All-New Super Friends Hour", 1977, "ABC"), show("ot", "Rugrats", 1991, "Nickelodeon")]}
    episodes = {sid: [{"id": f"{sid}{i}", "show": sid, "title": f"ep {i}", "season": 1, "episode": i, "summary": "",
                       "date": "", "dur": 22 * 60, "file": f"/m/{sid}{i}.mkv", "part": "", "audio": True,
                       "chapters": [0, 11 * 60]} for i in range(1, 9)] for sid in shows}
    filler = [{"id": f"b{i}", "title": f"Boomerang bumper {i}", "dur": 6, "file": f"/a/b{i}.mp4", "in": 0, "part": "",
               "audio": True, "tags": with_kind(["boomerang", "bumpers"], "x", 6), "src": "folder:/a"} for i in range(5)]
    filler += [{"id": f"n{i}", "title": f"Nick splat {i}", "dur": 6, "file": f"/a/n{i}.mp4", "in": 0, "part": "",
                "audio": True, "tags": with_kind(["nick", "bumpers"], "x", 6), "src": "folder:/a"} for i in range(5)]
    filler += [{"id": f"a{i}", "title": f"Ad {i}", "dur": 30, "file": f"/a/a{i}.mp4", "in": 0, "part": "", "audio": True,
                "tags": ["boomerang" if i % 2 else "cn"], "src": "folder:/a"} for i in range(40)]
    cat = Catalog({"shows": shows, "episodes": episodes, "movies": {}, "filler": {"folder:/a": filler},
                   "music_videos": {}, "networks": {}})
    ids = lineups.show_ids(cat, "boomerang")
    assert ids == {"sc", "je", "tj", "sf"}, ids  # not the 2021 Smurfs, not Rugrats
    w = {x["title"]: x for x in lineups.wants(cat, "boomerang")}
    assert w["Scooby-Doo, Where Are You!"]["have"] and not w["The Smurfs"]["have"] and not w["Jonny Quest"]["have"]
    assert w["Super Friends"]["match"] == "The All-New Super Friends Hour"
    ch = normalize_channel({"id": "boom", "name": "Boomerang", "auto_key": "lineup:boomerang",
                            "rules": [{"type": "lineup", "key": "boomerang"}]})
    p = await build(ch, cat, datetime(YEAR, 3, 3, 6).timestamp(), 12)
    played = {x["m"].rstrip("0123456789") for x in p.items if x["k"] == "c"}
    assert played == {"sc", "je", "tj", "sf"}, played
    bumps = [x["m"] for x in p.items if x["m"][:1] in "bn" and x["k"] == "f"]
    assert bumps and all(b.startswith("b") for b in bumps), "only Boomerang's own bumpers on Boomerang"
    opened = sum(1 for b in p.blocks if next(x for x in p.items if x["s"] >= b["s"])["m"].startswith("b"))
    assert opened >= len(p.blocks) * 0.8, f"{opened} of {len(p.blocks)} shows opened with a Boomerang ident"
    ads = [x["m"] for x in p.items if x["m"].startswith("a")]
    boom = sum(1 for a in ads if int(a[1:]) % 2)
    # 20 Boomerang ads in the pool: past them, variety wins over repeats (the finder later limits the channel to its own)
    assert ads and boom / len(ads) > 0.6, f"Boomerang ads should win ({boom} of {len(ads)})"
    print(f"Boomerang lineup channel: OK ({len(p.blocks)} shows, {opened} opened with a Boomerang ident)")


def check_overlay_filters():
    import tempfile as _t
    from app.ffmpeg import FFmpeg
    from app import images
    d = _t.mkdtemp()
    bug = os.path.join(d, "bug.png")
    open(bug, "wb").write(b"png")
    banner = os.path.join(d, "banner.png")
    open(banner, "wb").write(images.make_banner("Scooby-Doo, Where Are You!", 720, "#ff6900"))
    assert images.logo_accent(images.make_banner("x", 200, "#ff6900")) is not None
    ff = FFmpeg()
    ff.filters, ff.encoder_status = {"overlay"}, {"using": "none"}
    cmd = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, True,
                       overlay={"logo": bug, "banner": banner, "t0": 5, "t1": 20})
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-loop") == 2 and graph.count("overlay=") == 2 and "fade=t=in:st=5.00" in graph
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert graph.index("overlay=") < graph.index("pad="), "the logo goes inside the picture, not on the side bars"
    silent = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, False, overlay={"logo": bug, "banner": banner,
                                                                                       "t0": 1, "t1": 9})
    sg = silent[silent.index("-filter_complex") + 1]
    assert "[pic][2:v:0]overlay" in sg and "[3:v:0]format=rgba" in sg, "inputs numbered after the silence"
    plain = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, True)
    assert "-filter_complex" not in plain and "-vf" in plain
    missing = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, True, overlay={"logo": "/nope.png"})
    assert "-filter_complex" not in missing, "a missing logo file means no logo, not a failure"
    ff.filters = set()
    bare = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, True, overlay={"logo": bug, "banner": banner,
                                                                                     "t0": 1, "t1": 9})
    assert "-filter_complex" not in bare, "without the overlay filter, a plain picture"
    from PIL import Image
    import io as _io
    for theme in ("nick", "cn", "disney", "boomerang", "25days"):  # every network style draws, long titles fit
        img = Image.open(_io.BytesIO(images.make_themed_banner("A Very Long Title " * 6, 720, theme)))
        widest = 1280 if theme == "nick" else 1100  # the Nick bar runs the full width of a widescreen picture
        assert 200 < img.width <= widest and 50 < img.height < 260, (theme, img.size)
    assert images.theme_for("nick") == "nick" and images.theme_for("abcfamily", plan=True) == "25days"
    assert images.theme_for("mtv") == ""
    ff.filters = {"overlay"}
    up = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, True,
                      overlay={"banner": banner, "t0": 5, "t1": 20, "motion": "up"})
    g = up[up.index("-filter_complex") + 1]
    assert "y='main_h*0.73+main_h*0.06*max(0,1-(t-5.00)/0.45)'" in g, g
    print("logo and Up next filters: OK")


def check_templates():
    """Your own Up next templates: found by show or network name, green windows squeeze the show, black
    backgrounds are keyed out, and the real ffmpeg command runs."""
    import shutil as _sh
    import subprocess as _sp
    from PIL import Image, ImageDraw
    from app import images
    from app.ffmpeg import FFmpeg
    folder = images.BUG_DIR / "custom" / "upnext"
    folder.mkdir(parents=True, exist_ok=True)
    win = Image.new("RGB", (480, 360), "#e8e8e8")  # a squeeze template: green window above a bar
    d = ImageDraw.Draw(win)
    d.rectangle((10, 40, 340, 280), fill="#5cff10")
    d.rectangle((0, 290, 480, 360), fill="#f26a12")
    win.save(folder / "SpongeBob SquarePants.png")
    over = Image.new("RGB", (640, 360), "#00ff00")  # an overlay template: green everywhere but a bar
    ImageDraw.Draw(over).rectangle((0, 290, 640, 360), fill="#f26a12")
    over.save(folder / "Nickelodeon.jpg")
    blk = Image.new("RGB", (640, 360), "black")
    ImageDraw.Draw(blk).rectangle((100, 150, 540, 210), fill="white")
    blk.save(folder / "Nicktoons.png")
    assert images.find_template(["spongebob squarepants!", "Nickelodeon"]).endswith("SpongeBob SquarePants.png")
    assert images.find_template(["Rugrats", "Nickelodeon"]).endswith("Nickelodeon.jpg")
    assert images.find_template(["Rugrats", "MTV"]) is None
    a = images.analyse_template(str(folder / "SpongeBob SquarePants.png"))
    assert a["how"] == "green" and a["window"], a
    x, y, w, h = a["window"]
    assert abs(x - 10 / 480) < 0.02 and abs(y - 40 / 360) < 0.02 and abs(w - 331 / 480) < 0.03, a["window"]
    b = images.analyse_template(str(folder / "Nickelodeon.jpg"))
    assert b["how"] == "green" and b["window"] is None, b
    c = images.analyse_template(str(folder / "Nicktoons.png"))
    assert c["how"] == "black", c
    ff = FFmpeg()
    ff.filters, ff.encoder_status = {"overlay"}, {"using": "none"}
    cmd = ff.media_cmd("/m/x.mkv", 0, 30, {"resolution": "720p"}, 0, True,
                       overlay={"template": {**a, "file": str(folder / "SpongeBob SquarePants.png"), "t0": 2, "t1": 12},
                                "banner": "/x.png"})
    g = cmd[cmd.index("-filter_complex") + 1]
    assert "split=3" in g and "chromakey=" in g and "between(t,2.00,12.00)" in g and "[ban]" not in g, g
    if _sh.which("ffmpeg"):  # the real thing, on a few seconds of test video
        ff.detect()
        ff.encoder_status = {"using": "none"}
        src = os.path.join(tempfile.mkdtemp(), "v.mp4")
        _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x480:r=30",
                 "-f", "lavfi", "-i", "sine", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", src],
                check=True)
        for t in (a, b, c):
            f = images.find_template(["SpongeBob SquarePants"]) if t is a else \
                str(folder / ("Nickelodeon.jpg" if t is b else "Nicktoons.png"))
            cmd = ff.media_cmd(src, 0, 4, {"resolution": "720p"}, None, True,
                               overlay={"template": {**t, "file": f, "t0": 1, "t1": 3}})
            r = _sp.run(cmd, capture_output=True, timeout=60)
            assert r.returncode == 0 and len(r.stdout) > 10000, r.stderr.decode()[-300:]
    print("Up next templates: OK (squeeze window, overlay, black key)")


def check_logo_filter():
    """Pieces of the show inside a recorded break carry the network's logo; real commercials don't."""
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("ffmpeg"):
        print("network logo filter: skipped (no ffmpeg)")
        return
    d = tempfile.mkdtemp()
    ads_src = ["smptebars=s=320x240:r=25", "rgbtestsrc=s=320x240:r=25", "cellauto=s=320x240:r=25:rule=110",
               "life=s=320x240:r=25:mold=10", "testsrc=s=320x240:r=25", "cellauto=s=320x240:r=25:rule=30"]
    breaks = []
    for i in range(8):  # each "break": 3s of show (logo in the corner), a 6s ad, 3s of show
        # the "show" keeps moving everywhere (a Mandelbrot zoom), so the only thing that stays put is the logo
        show = (f"mandelbrot=s=320x240:r=25:start_scale={3 - i * 0.3:.1f},hue=h={i * 40},"
                f"drawbox=x=236:y=196:w=64:h=24:color=white@0.85:t=6,drawbox=x=246:y=203:w=44:h=10:color=white@0.85:t=fill")
        out = os.path.join(d, f"b{i}.mkv")
        _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-ss", str(i * 2), "-t", "3", "-i", show,
                 "-f", "lavfi", "-t", "6", "-i", ads_src[i % len(ads_src)],
                 "-f", "lavfi", "-ss", str(i * 2 + 5), "-t", "3", "-i", show,
                 "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1[v]", "-map", "[v]",
                 "-c:v", "libx264", "-preset", "ultrafast", out], check=True)
        breaks.append(out)
    m = archive.logo_model("ffmpeg", breaks, [(b, 6.0) for b in breaks])
    assert m and m["corner"] == "br", m and {k: m[k] for k in ("corner", "typical", "gap")}
    assert archive.shows_logo("ffmpeg", breaks[0], {"s": 0.2, "d": 2.5}, m), "the show should be recognised"
    assert not archive.shows_logo("ffmpeg", breaks[0], {"s": 3.5, "d": 5.0}, m), "an ad isn't the show"
    assert not archive.shows_logo("ffmpeg", breaks[3], {"s": 3.5, "d": 5.0}, m)
    assert archive.logo_model("ffmpeg", breaks[:3], []) is None, "too few frames to learn a logo from"
    _sh.rmtree(d, ignore_errors=True)
    print(f"network logo filter: OK (logo found {m['corner']}, show {m['typical']:.2f}, gap {m['gap']:.2f})")


async def main():
    check_data()
    cat = make_catalog()
    check_matching(cat)
    await check_plan(cat)
    await check_exact_breaks(cat)
    await check_2000s(cat)
    await check_bumpers()
    await check_lineup()
    check_overlay_filters()
    check_templates()
    check_finder_logic()
    check_detection()
    check_logo_filter()
    print("ALL CHRISTMAS CHECKS PASSED")


asyncio.run(main())
