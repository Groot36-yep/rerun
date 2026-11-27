"""Character cut-outs from fanart.tv: the quality checks and the picking, against a stand-in fanart.tv.
Run: python tests/test_characters.py"""
import asyncio
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RERUN_DATA"] = tempfile.mkdtemp()

import httpx  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from app import characters, images  # noqa: E402
from app.catalog import Catalog  # noqa: E402


def png(size=512, soft=True, figure=(120, 40, 392, 500), alpha=True, fill_all=False) -> bytes:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0 if alpha else 255))
    d = ImageDraw.Draw(im)
    if fill_all:
        d.rectangle((0, 0, size, size), fill=(250, 200, 30, 255))
    else:
        d.ellipse(figure, fill=(250, 200, 30, 255))
    if soft:
        a = im.getchannel("A").filter(ImageFilter.GaussianBlur(1.5))
        im.putalpha(a)
    buf = io.BytesIO()
    (im if alpha else im.convert("RGB")).save(buf, "PNG")
    return buf.getvalue()


def check_judge():
    assert characters.judge(png())[0] > 0, characters.judge(png())
    assert characters.judge(png(alpha=False))[1] == "no see-through background"
    assert characters.judge(png(size=300))[1].startswith("too small")
    assert characters.judge(png(soft=False))[1] == "jagged edges"
    assert characters.judge(png(fill_all=True))[1] == "the background isn't see-through"
    assert characters.judge(png(figure=(120, -80, 392, 400)))[1] == "cut off at the edge"
    assert characters.judge(png(figure=(60, 330, 452, 470)))[1] == "the character is too small"
    print("quality checks: OK")


async def check_picking():
    wide = Image.new("RGBA", (1000, 562), (0, 0, 0, 0))  # clear art: characters and the logo, side by side
    ImageDraw.Draw(wide).ellipse((80, 40, 520, 540), fill=(250, 120, 30, 255))
    ImageDraw.Draw(wide).rectangle((560, 300, 960, 460), fill=(120, 60, 200, 255))
    wide.putalpha(wide.getchannel("A").filter(ImageFilter.GaussianBlur(1.5)))
    buf = io.BytesIO()
    wide.save(buf, "PNG")
    files = {"jagged": png(soft=False), "good": png(), "better": png(figure=(60, 20, 452, 510)), "french": png(),
             "clear": buf.getvalue()}
    arts = [{"id": "1", "url": "https://img/jagged", "lang": "en", "likes": "9"},
            {"id": "2", "url": "https://img/good", "lang": "en", "likes": "3"},
            {"id": "3", "url": "https://img/better", "lang": "00", "likes": "4"},
            {"id": "4", "url": "https://img/french", "lang": "fr", "likes": "30"}]
    asked = []

    def handler(req: httpx.Request):
        asked.append(str(req.url))
        if req.url.host == "webservice.fanart.tv":
            if req.headers.get("api-key") != "k" or req.headers.get("client-key") != "c":
                return httpx.Response(401)
            if req.url.path.endswith("/75886"):
                return httpx.Response(200, json={"name": "SpongeBob", "characterart": arts})
            if req.url.path.endswith("/2"):  # only jagged character art, but good clear art
                return httpx.Response(200, json={"characterart": [{"id": "21", "url": "https://img/jagged", "lang": "en"}],
                                                 "hdclearart": [{"id": "22", "url": "https://img/clear", "lang": "en"}]})
            return httpx.Response(404)
        if req.url.host == "api.tvmaze.com":  # the IMDb id -> TheTVDB id
            if req.url.params.get("imdb") == "tt0206512":
                return httpx.Response(200, json={"externals": {"thetvdb": 75886}})
            return httpx.Response(404)
        if req.url.host == "www.wikidata.org":
            return httpx.Response(200, json={"query": {"search": []}})
        return httpx.Response(200, content=files[req.url.path.strip("/")])

    cat = Catalog({"shows": {"s1": {"id": "s1", "title": "SpongeBob SquarePants", "guids": {"tvdb": "75886"}},
                             "s2": {"id": "s2", "title": "No Art Show", "guids": {"tvdb": "1"}},
                             "s3": {"id": "s3", "title": "Mine", "guids": {"tvdb": "75886"}},
                             "s4": {"id": "s4", "title": "Only IMDb", "guids": {"imdb": "tt0206512"}},
                             "s5": {"id": "s5", "title": "Clear Art Show", "guids": {"tvdb": "2"}},
                             "s6": {"id": "s6", "title": "Nobody Knows", "guids": {"imdb": "tt9"}}}, "episodes": {}})
    app = SimpleNamespace(catalog=cat, secrets=SimpleNamespace(data={"fanart_key": "k", "fanart_client": "c"}))
    os.makedirs(characters.DIR, exist_ok=True)
    png_mine = characters.DIR / "Mine.png"
    png_mine.write_bytes(png())
    g = characters.Grabber(app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        names = await g.for_show(c, "s1")
        sb = "SpongeBob SquarePants"
        assert names == [f"{sb}.png", f"{sb} #2.png", f"{sb} #3.png"], names
        kept = json.loads(characters.MANIFEST.read_text())[sb]["pics"]
        # three for variety: the English (or wordless) ones first, best first; the jagged one refused; the
        # French one only because there weren't three good English ones
        assert [x["id"] for x in kept] == ["3", "2", "4"], kept
        assert len(images.characters_for(sb)) == 3
        assert {images.character_for(sb, k) for k in range(3)} == set(images.characters_for(sb)), "they don't take turns"
        assert await g.for_show(c, "s2") == []
        rec = json.loads(characters.MANIFEST.read_text())["No Art Show"]
        assert rec["none"] and rec["why"] == "fanart.tv has no character art for it", rec
        assert "Only IMDb.png" in await g.for_show(c, "s4"), "a show with only an IMDb id wasn't found through TVMaze"
        assert await g.for_show(c, "s5") == ["Clear Art Show.png"]
        assert json.loads(characters.MANIFEST.read_text())["Clear Art Show"]["pics"][0]["kind"] == "clear art"
        assert await g.for_show(c, "s6") == []
        assert json.loads(characters.MANIFEST.read_text())["Nobody Knows"]["why"].startswith("Couldn't identify")
        await g.for_show(c, "s3")  # yours is kept, and found ones join it
        assert png_mine.read_bytes() == png() and len(images.characters_for("Mine")) == 4, images.characters_for("Mine")
        assert await g.for_show(c, "s1", more=True) == [], "found one more when fanart.tv has nothing new"
        assert json.loads(characters.MANIFEST.read_text())[sb]["why"] == "No more good ones on fanart.tv"
    g.remove("s1", f"{sb} #2.png")  # one picture: the others stay, and it isn't fetched again
    assert len(images.characters_for(sb)) == 2 and "2" in json.loads(characters.MANIFEST.read_text())[sb]["tried"]
    g.remove("s1")  # all of them: the show goes without, and isn't looked for again
    assert images.character_for(sb) is None
    assert [x["title"] for x in g.listing() if x["file"]] == ["Clear Art Show", "Mine", "Only IMDb"]
    gone = {x["title"]: x["why"] for x in g.listing() if not x["file"]}
    assert gone[sb] == "You removed them" and "fanart.tv has no" in gone["No Art Show"]
    # clear art sits wider on the bar, left of the words
    L = images.nick2009_layers("Clear Art Show", "", 1080, images.character_for("Clear Art Show"))
    pic = Image.open(L["char"])
    assert pic.width > pic.height and L["char_x"] + pic.width <= 652 and L["char_x"] >= 0, (pic.size, L["char_x"])
    # a later search looks again at shows that found nothing (or fewer than three) before, but not at ones
    # you removed, nor ones already looked at with this search
    m = json.loads(characters.MANIFEST.read_text())
    m["No Art Show"]["v"] = 1
    characters.MANIFEST.write_text(json.dumps(m))
    seen = []

    async def fake(c, sid, more=False):
        seen.append(sid)
        return []
    g.for_show = fake
    g.progress["running"] = False
    await g._run(False)
    assert seen == ["s2"], seen
    print("picking: OK")


def check_upload():
    """Your own cut-out, dropped on the page: a see-through PNG is kept as the show's (and never replaced
    by the finder); a picture with no see-through background is refused with the reason."""
    cat = Catalog({"shows": {"i1": {"id": "i1", "title": "iCarly", "guids": {}}}, "episodes": {}})
    g = characters.Grabber(SimpleNamespace(catalog=cat, secrets=SimpleNamespace(data={})))
    assert g.upload("i1", png()) == "iCarly.png" and images.character_for("iCarly")
    assert g.upload("i1", png(figure=(60, 20, 452, 510))) == "iCarly #2.png", "a second picture didn't join the first"
    row = next(x for x in g.listing() if x["show"] == "i1")
    assert [p["own"] for p in row["pics"]] == [True, True], row
    try:
        g.upload("i1", png(alpha=False))
        raise AssertionError("a picture with no see-through background was accepted")
    except ValueError as e:
        assert "see-through" in str(e)
    g.remove("i1")
    assert images.character_for("iCarly") is None
    print("your own cut-outs: OK")


def check_listen_failures():
    """When hearing fails (the model can't load or every clip errors), nothing is filed as heard, and
    the run stops with the reason instead of claiming it's done."""
    from app import listen
    clips = [{"id": f"c{i}", "title": f"Tape ({i})", "file": "/x.mp4", "in": 0, "dur": 30} for i in range(20)]
    app = SimpleNamespace(catalog=SimpleNamespace(all_filler=lambda: clips, shows={}), streams=None)
    L = listen.Listener(app)
    L.heard = {}
    L._load = lambda: object()

    def boom(f):
        raise PermissionError("[Errno 30] Read-only file system: '/home/me/.cache'")
    L._hear = boom
    L.progress["running"] = True
    asyncio.run(L._run())
    assert not L.heard, "failed clips were filed as heard"
    assert "Read-only" in L.progress["error"] and L.progress["done"] < len(clips), L.progress
    # listening again: every clip once more, or just one; until then, what was heard before stays
    L._hear = lambda f: "Kix cereal, kid tested, mother approved." if f["id"] != "c3" else "Buy the new Lego set today."
    L.progress["running"] = True
    asyncio.run(L._run())
    assert len(L.heard) == len(clips) and L.heard["c3"]["cat"] == "toy", L.heard["c3"]
    assert L.todo() == [], "everything was heard, yet some are still to do"
    L.heard["c5"]["text"] = "old"
    L._hear = lambda f: "Lucky Charms, they're magically delicious."
    L.progress["running"] = True
    asyncio.run(L._run([f for f in clips if f["id"] == "c3"]))
    assert L.heard["c3"]["cat"] == "cereal" and L.heard["c5"]["text"] == "old", "listening to one redid others"
    L.progress["running"] = True
    asyncio.run(L._run(list(clips)))
    assert all(r["text"].startswith("Lucky") for r in L.heard.values()), "listening to all again missed some"
    # only what's new is heard: a new commercial, and ones the old versions filed empty; not the rest
    L.heard["c7"] = {**L.heard["c7"], "text": "", "cat": "other"}              # filed empty by the old bug
    L.heard["c8"] = {**L.heard["c8"], "text": "", "cat": "other", "silent": True}  # really has nothing said
    clips.append({"id": "new1", "title": "Tape (99)", "file": "/x.mp4", "in": 0, "dur": 30})
    assert [f["id"] for f in L.todo()] == ["c7", "new1"], [f["id"] for f in L.todo()]
    assert L.summary()["heard"] == len(clips) - 2
    L._hear = lambda f: ""
    L.progress["running"] = True
    asyncio.run(L._run())
    assert L.todo() == [] and L.heard["new1"]["silent"], "a commercial with nothing said is asked again and again"
    clips.pop()
    # heard by the version that briefly called itself 3: not heard again after updating
    L.heard["c1"]["k"] = "3:" + L.heard["c1"]["k"].split(":", 1)[1]
    assert L.todo() == [], [f["id"] for f in L.todo()]
    assert L.record("c3")["kind"] and L.record("nope") == {"heard": False, "text": "", "kind": "", "shows": [], "role": "", "season": [], "when": ""}
    # the sound comes from the clip's own file (a library clip says "file", not a planned item's "f"), and a
    # clip whose file can't be found isn't filed as heard-but-silent
    asked = []
    app.media_input = lambda item: asked.append(item) or None
    del L._hear
    try:
        L._hear(clips[0])
        raise AssertionError("a clip with no file was heard as silence")
    except FileNotFoundError:
        pass
    assert asked[0]["f"] == "/x.mp4", asked
    # and the analysis of what is heard
    names = listen.known_shows(None) | {"icarly": "iCarly", "fairly oddparents": "The Fairly OddParents"}
    a = listen.analyse("Now back to more Fairly Odd Paris on Nickelodeon.", "Tape (3)", names)
    assert a["shows"] == ["The Fairly OddParents"] and a["role"] == "watching", a
    assert listen.analyse("You can have the power of the force with the new Lego Star Wars minis.", "", names)["cat"] == "toy"
    assert listen.analyse("Holiday Special. Monday at 8, only on Cartoon Network.", "WOC VHS Recording (5)", names)["cat"] == "promo"
    assert listen.analyse("Make your portraits magical, all that and more.", "", names)["shows"] == [], "a phrase taken for a show"
    print("listening: OK (failures aren't filed; kinds and shows read right)")


check_judge()
check_upload()
check_listen_failures()
asyncio.run(check_picking())
print("ALL CHARACTER CHECKS PASSED")
