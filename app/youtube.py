"""YouTube (and the other sites yt-dlp knows), for commercials, bumpers and "Up next" templates.

Lots of what made 2000s TV is on YouTube: commercial-break compilations, bumper collections, network
idents and green-screen "Up next" templates. This looks things up and downloads them with yt-dlp; what
happens to a download (split into commercials, filed as a template) is up to archive.Finder.
"""
from __future__ import annotations

import logging
import os
import shutil

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


def js_runtime() -> str:
    """YouTube wants a JavaScript runtime for some videos; yt-dlp uses deno or Node if there is one."""
    return next((n for n in ("deno", "node") if shutil.which(n)), "")


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
    rt = js_runtime()
    if rt:
        opts["js_runtimes"] = {rt: {}}
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

    quiet = _Quiet()
    opts = _opts(logger=quiet, progress_hooks=[hook], noplaylist=True, merge_output_format="mp4",
                 format=f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
                 outtmpl=os.path.join(folder, name + ".%(ext)s"), overwrites=True)
    if ffmpeg and os.path.dirname(ffmpeg):
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    files = [d.get("filepath") for d in (info or {}).get("requested_downloads") or [] if d.get("filepath")]
    path = next((f for f in files if os.path.isfile(f)), "")
    if not path:
        raise RuntimeError(quiet.errors[-1] if quiet.errors else "The download didn't produce a video")
    return path, info or {}
