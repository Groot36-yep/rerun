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
        name = await g.for_show(c, "s1")
        assert name == "SpongeBob SquarePants.png", name
        kept = json.loads(characters.MANIFEST.read_text())["SpongeBob SquarePants"]
        assert kept["id"] == "3", kept  # English or no-language first, the jagged one refused, the bigger one kept
        assert not any("french" in u for u in asked), "a French one was fetched before the English ones ran out"
        assert images.character_for("SpongeBob SquarePants").endswith("SpongeBob SquarePants.png")
        assert await g.for_show(c, "s2") == ""
        rec = json.loads(characters.MANIFEST.read_text())["No Art Show"]
        assert rec["none"] and rec["why"] == "fanart.tv has no character art for it", rec
        assert await g.for_show(c, "s4") == "Only IMDb.png", "a show with only an IMDb id wasn't found through TVMaze"
        assert await g.for_show(c, "s5") == "Clear Art Show.png"
        assert json.loads(characters.MANIFEST.read_text())["Clear Art Show"]["kind"] == "clear art"
        assert await g.for_show(c, "s6") == ""
        assert json.loads(characters.MANIFEST.read_text())["Nobody Knows"]["why"].startswith("Couldn't identify")
        assert await g.for_show(c, "s3") == "" and png_mine.read_bytes() == png(), "replaced a cut-out you put there"
        again = await g.for_show(c, "s1")  # "Another": the one it had and the jagged one are skipped
        assert json.loads(characters.MANIFEST.read_text())["SpongeBob SquarePants"]["id"] in ("2", "4"), again
    g.remove("s1")
    assert images.character_for("SpongeBob SquarePants") is None
    assert [x["title"] for x in g.listing() if x["file"]] == ["Clear Art Show", "Mine", "Only IMDb"]
    gone = {x["title"]: x["why"] for x in g.listing() if not x["file"]}
    assert gone["SpongeBob SquarePants"] == "You removed it" and "fanart.tv has no" in gone["No Art Show"]
    # clear art sits wider on the bar, left of the words
    L = images.nick2009_layers("Clear Art Show", "", 1080, images.character_for("Clear Art Show"))
    pic = Image.open(L["char"])
    assert pic.width > pic.height and L["char_x"] + pic.width <= 652 and L["char_x"] >= 0, (pic.size, L["char_x"])
    # a later search looks again at shows that found nothing before, but not at ones you removed
    m = json.loads(characters.MANIFEST.read_text())
    m["No Art Show"]["v"] = 1
    characters.MANIFEST.write_text(json.dumps(m))
    seen = []

    async def fake(c, sid, skip=None):
        seen.append(sid)
        return ""
    g.for_show = fake
    g.progress["running"] = False
    await g._run(False)
    assert seen == ["s2"], seen
    print("picking: OK")


check_judge()
asyncio.run(check_picking())
print("ALL CHARACTER CHECKS PASSED")
