"""YouTube (and the other sites yt-dlp knows), for commercials, bumpers and "Up next" templates.

Lots of what made 2000s TV is on YouTube: commercial-break compilations, bumper collections, network
idents and green-screen "Up next" templates. This looks things up and downloads them with yt-dlp; what
happens to a download (split into commercials, filed as a template) is up to archive.Finder.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time

log = logging.getLogger("rerun.youtube")

MAX_HEIGHT = 1080  # TV pictures are at most 1080 lines; more is just bigger files


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except ImportError:
        return ""


def ejs() -> bool:
    """yt-dlp's scripts for YouTube's JavaScript challenges (the yt-dlp-ejs package, "yt-dlp[default]")."""
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except ImportError:
        return False


def _runtime() -> tuple[str, str]:
    """(name, path) of the JavaScript runtime yt-dlp should use, or ("", "").

    YouTube makes every player solve a JavaScript challenge before it hands out working video links;
    without one solved, yt-dlp gets only a few formats, and those often fail partway with "HTTP Error
    403: Forbidden". The installer puts Deno (the runtime yt-dlp recommends) in Rerun's own Python
    environment, which isn't on the service's PATH, so it's looked for there first."""
    try:
        import deno
        path = deno.find_deno_bin()
        if path and os.path.isfile(path):
            return "deno", path
    except Exception:
        pass
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            return name, path
    return "", ""


def js_runtime() -> str:
    return _runtime()[0]


def problems() -> str:
    """What's missing for YouTube to work well, in words; "" if nothing."""
    missing = [w for w, ok in (("yt-dlp's YouTube scripts (yt-dlp-ejs)", ejs()),
                               ("a JavaScript runtime (Deno)", bool(js_runtime()))) if not ok]
    return ("Missing " + " and ".join(missing) + ". Run “rerun update” on the server to add them.") if missing else ""


class _Quiet:
    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if "Deprecated Feature" not in msg:
            log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        if "Deprecated Feature" not in msg:
            self.errors.append(msg)


def _opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Quiet(), "socket_timeout": 30,
            "retries": 3}
    # yt-dlp and Deno keep caches (YouTube's player, its solved challenges), by default in your home
    # folder, which the service can't write to: in Rerun's data folder, they're kept between downloads
    from .store import DATA_DIR
    opts["cachedir"] = str(DATA_DIR / "cache" / "yt-dlp")
    name, path = _runtime()
    if name:
        opts["js_runtimes"] = {name: {"path": path}}
        if name == "deno":
            os.environ.setdefault("DENO_DIR", str(DATA_DIR / "cache" / "deno"))
    opts.update(extra)
    return opts


def _entry(e: dict) -> dict:
    vid = e.get("id") or ""
    url = e.get("webpage_url") or e.get("url") or ""
    if url and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    thumbs = e.get("thumbnails") or []
    thumb = (thumbs[-1].get("url") if thumbs else "") or (f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else "")
    return {"id": vid, "url": url, "title": e.get("title") or vid, "duration": int(e.get("duration") or 0),
            "uploader": e.get("uploader") or e.get("channel") or "", "views": int(e.get("view_count") or 0),
            "thumb": thumb}


def search(query: str, n: int = 24) -> list[dict]:
    """YouTube search results (no downloading)."""
    import yt_dlp
    n = max(1, min(int(n), 50))
    with yt_dlp.YoutubeDL(_opts(extract_flat="in_playlist")) as y:
        res = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [_entry(e) for e in (res or {}).get("entries") or [] if e]


def expand(url: str, limit: int = 200) -> list[dict]:
    """The videos behind a link: one for a video, all of them for a playlist or channel."""
    import yt_dlp
    with yt_dlp.YoutubeDL(_opts(extract_flat="in_playlist", playlistend=limit)) as y:
        res = y.extract_info(url, download=False)
    if not res:
        return []
    if res.get("entries") is not None:
        return [_entry(e) for e in res["entries"] if e][:limit]
    return [_entry(res)]


def download(url: str, folder: str, name: str, ffmpeg: str, on_progress=None, stop=lambda: False,
             max_height: int = MAX_HEIGHT) -> tuple[str, dict]:
    """Download one video as an mp4 (at most max_height lines), to folder/name.mp4. Returns (file, its info)."""
    import yt_dlp
    os.makedirs(folder, exist_ok=True)

    def hook(d):
        if stop():
            raise yt_dlp.utils.DownloadCancelled("Stopped")
        if on_progress and d.get("status") == "downloading":
            on_progress(int(d.get("downloaded_bytes") or 0), int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0))

    fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b"
    last: Exception | None = None
    for n, (clients, _why) in enumerate(ATTEMPTS):
        if n and stop():
            break
        quiet = _Quiet()
        opts = _opts(logger=quiet, progress_hooks=[hook], noplaylist=True, merge_output_format="mp4", format=fmt,
                     outtmpl=os.path.join(folder, name + ".%(ext)s"), overwrites=n == 0,
                     # asked for in pieces, a long video isn't cut off partway as often
                     http_chunk_size=10 * 1024 * 1024, retries=5, fragment_retries=10)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
        if ffmpeg and os.path.dirname(ffmpeg):
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=True)
            files = [d.get("filepath") for d in (info or {}).get("requested_downloads") or [] if d.get("filepath")]
            path = next((f for f in files if os.path.isfile(f)), "")
            if not path:
                raise RuntimeError(quiet.errors[-1] if quiet.errors else "The download didn't produce a video")
            if n:
                log.info("Downloaded %s on try %s (%s)", url, n + 1, _why)
            return path, info or {}
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as e:
            last = e
            if not retryable(str(e)):
                raise RuntimeError(friendly(str(e))) from e
            if n + 1 < len(ATTEMPTS):
                log.info("YouTube refused %s (%s); trying again %s", url, str(e)[:120], ATTEMPTS[n + 1][1])
                for _ in range(4 * (n + 1)):  # a short pause, longer each time
                    if stop():
                        break
                    time.sleep(1)
    raise RuntimeError(friendly(str(last or "Stopped")))


# Each try asks YouTube again for fresh links: first as yt-dlp normally does, then as other players
# (YouTube's TV app, Safari, the mobile site), whose links it often still honours when one is refused.
ATTEMPTS = [
    (None, "as usual"),
    (["tv", "web_safari"], "as YouTube's TV app"),
    (["mweb", "web_embedded"], "as YouTube's mobile site"),
]
_NO_RETRY = re.compile(r"private video|video unavailable|has been removed|copyright|not available in your country|"
                       r"members-only|join this channel|confirm your age", re.I)
_RETRY = re.compile(r"\b403\b|forbidden|requested format is not available|unable to download|fragment|"
                    r"timed out|connection (reset|aborted|refused)|incomplete|produce a video|\b5\d\d\b", re.I)


def retryable(msg: str) -> bool:
    """A refusal or a broken connection, which another try may get past (not a private or removed video)."""
    return not _NO_RETRY.search(msg) and bool(_RETRY.search(msg))


def friendly(msg: str) -> str:
    """The error, plus what to do about it when this machine is missing what YouTube needs."""
    msg = re.sub(r"^ERROR:\s*(\[[^\]]*\]\s*[\w-]+:\s*)?", "", msg.strip())
    if re.search(r"\b403\b|forbidden", msg, re.I):
        fix = problems()
        msg += (" — " + fix) if fix else " — YouTube refused it after three tries; it'll be tried again next time."
    return msg
