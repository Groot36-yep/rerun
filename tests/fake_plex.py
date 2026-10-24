"""A pretend Plex server for testing. Serves the library made by make_media.py.

    MEDIA=path python -m uvicorn tests.fake_plex:app --port 32499
"""
import os
import subprocess

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

MEDIA = os.path.abspath(os.environ.get("MEDIA", "testmedia"))
TOKEN = "test-token"
app = FastAPI()

SHOWS = {
    "100": ("Sponge Show", "Sponge Show", "Nickelodeon", ["Animation", "Comedy"], 1999, "75886"),
    "200": ("Rugrats Test", "Rugrats Test", "Nicktoons", ["Animation", "Family"], 1991, "76180"),
    "300": ("Dexter Test", "Dexter Test", "Cartoon Network", ["Animation"], 1996, "75009"),
    "400": ("Sitcom Test", "Sitcom Test", "NBC", ["Comedy"], 1994, "79168"),
}


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return int(float(out) * 1000) if out else 40000  # a damaged file: pretend, like Plex's stale metadata


def media(rk, path, audio=True):
    dur = probe(path)
    m = {"duration": dur, "videoCodec": "h264", "height": 480,
         "Part": [{"key": f"/library/parts/{rk}/1/file{os.path.splitext(path)[1]}", "file": path, "duration": dur}]}
    if audio:
        m["audioCodec"] = "aac"
    return [m]


SPECIAL_TITLES = {"10002": "Christmas Who?", "20003": "A Rugrats Halloween", "40001": "The Thanksgiving Show"}
COLLECTIONS = {"1": {"5001": ("Saturday Morning", ["100", "300"])}, "2": {"5002": ("Christmas Movies", ["900"])}}


def episodes():
    out = []
    for srk, (folder, title, studio, genres, year, tvdb) in SHOWS.items():
        d = os.path.join(MEDIA, "tv", folder)
        for i, fn in enumerate(sorted(os.listdir(d)), 1):
            rk = f"{srk}{i:02d}"
            audio = not (folder == "Sitcom Test" and i == 2)
            out.append({"ratingKey": rk, "type": "episode", "title": SPECIAL_TITLES.get(rk, f"{title} episode {i}"), "grandparentRatingKey": srk,
                        "grandparentTitle": title, "parentIndex": 1, "index": i, "summary": f"Episode {i} summary",
                        "duration": 0, "Media": media(rk, os.path.join(d, fn), audio)})
    return out


def check(request: Request):
    return request.headers.get("x-plex-token") == TOKEN or request.query_params.get("X-Plex-Token") == TOKEN


def mc(**kw):
    return {"MediaContainer": kw}


@app.get("/identity")
def identity():
    return mc(machineIdentifier="fake-machine", friendlyName="Fake Plex", version="1.0")


@app.get("/library/sections")
def sections(request: Request):
    if not check(request):
        return Response(status_code=401)
    return mc(Directory=[
        {"key": "1", "title": "TV Shows", "type": "show"},
        {"key": "2", "title": "Movies", "type": "movie"},
        {"key": "3", "title": "Commercials", "type": "movie"},
    ])


@app.get("/library/sections/{sid}/all")
def all_items(sid: str, request: Request, type: int = 0):
    if not check(request):
        return Response(status_code=401)
    start = int(request.query_params.get("X-Plex-Container-Start", 0))
    size = int(request.query_params.get("X-Plex-Container-Size", 50))
    items = []
    if sid == "1" and type == 2:
        items = [{"ratingKey": k, "type": "show", "title": t, "studio": st, "year": y, "contentRating": "TV-Y7",
                  "Genre": [{"tag": g} for g in gs], "thumb": f"/library/metadata/{k}/thumb/1",
                  "Guid": [{"id": f"tvdb://{tv}"}]}
                 for k, (_, t, st, gs, y, tv) in SHOWS.items()]
    elif sid == "1" and type == 4:
        items = episodes()
    elif sid == "2" and type == 1:
        p = os.path.join(MEDIA, "movies", "Test Movie (1999).mp4")
        items = [{"ratingKey": "900", "type": "movie", "title": "Test Movie", "year": 1999, "studio": "Nickelodeon Movies",
                  "Genre": [{"tag": "Family"}], "thumb": "/library/metadata/900/thumb/1", "Media": media("900", p)}]
    page = items[start:start + size]
    return mc(size=len(page), totalSize=len(items), Metadata=page)


@app.get("/library/sections/{sid}/collections")
def collections(sid: str, request: Request):
    if not check(request):
        return Response(status_code=401)
    return mc(Metadata=[{"ratingKey": rk, "title": t, "childCount": len(kids)} for rk, (t, kids) in COLLECTIONS.get(sid, {}).items()])


@app.get("/library/collections/{rk}/children")
def collection_children(rk: str, request: Request):
    if not check(request):
        return Response(status_code=401)
    for cols in COLLECTIONS.values():
        if rk in cols:
            return mc(Metadata=[{"ratingKey": k} for k in cols[rk][1]])
    return mc(Metadata=[])


@app.get("/library/metadata/{rk}")
def metadata(rk: str, request: Request):
    if not check(request):
        return Response(status_code=401)
    chapters = [{"startTimeOffset": 0}, {"startTimeOffset": 18000}, {"startTimeOffset": 34000}] if rk.startswith("100") else []
    return mc(Metadata=[{"ratingKey": rk, "Chapter": chapters}])


@app.get("/library/parts/{rk}/1/{name}")
def part(rk: str, name: str, request: Request):
    if not check(request):
        return Response(status_code=401)
    for e in episodes():
        if e["ratingKey"] == rk:
            return FileResponse(e["Media"][0]["Part"][0]["file"])
    return Response(status_code=404)


@app.get("/photo/:/transcode")
def photo(request: Request):
    if not check(request):
        return Response(status_code=401)
    # 1x1 white JPEG
    return Response(bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720"
        "222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405"
        "060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282"
        "090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495"
        "969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008"
        "010100003f00fbd3ffd9"), media_type="image/jpeg")


# What a real Plex reports while someone watches Live TV with transcoding on the processor.
@app.get("/:/prefs")
def prefs(request: Request):
    return mc(Setting=[{"id": "HardwareAcceleratedCodecs", "value": False},
                       {"id": "HardwareAcceleratedEncoders", "value": False},
                       {"id": "TranscoderTempDirectory", "value": ""},
                       {"id": "FriendlyName", "value": "Fake Plex"}])


@app.get("/transcode/sessions")
def transcode_sessions(request: Request):
    return mc(TranscodeSession=[{"key": "abc", "throttled": False, "speed": 1.1, "sourceVideoCodec": "h264",
                                 "videoCodec": "h264", "videoDecision": "transcode", "sourceAudioCodec": "aac",
                                 "audioCodec": "aac", "audioDecision": "copy", "protocol": "hls"}])


@app.get("/status/sessions")
def status_sessions(request: Request):
    return mc(Metadata=[{"title": "Nickelodeon", "type": "episode", "live": "1",
                         "Player": {"product": "Plex Web", "state": "playing"},
                         "TranscodeSession": {"videoDecision": "transcode", "audioDecision": "copy"}}])
