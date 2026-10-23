"""What Plex (and other players) read: the tuner description, channel list, TV guide and M3U."""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

from . import __version__
from .images import VERSION as LOGO_V

_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(s) -> str:
    return escape(_BAD_XML.sub("", str(s or "")))


def _a(s) -> str:
    return quoteattr(_BAD_XML.sub("", str(s or "")))


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def device_id(client_id: str) -> str:
    return hashlib.sha1(client_id.encode()).hexdigest()[:8].upper()


def enabled_channels(app) -> list[dict]:
    return sorted((c for c in app.cfg.data["channels"] if app.channel_visible(c)), key=lambda c: c["number"])


def logo_url(app, base: str, ch: dict) -> str:
    # Plex keeps a logo by its address, so the address only changes when the picture really does:
    # a new Rerun logo style, or a holiday badge (if festive logos are on) starting or ending.
    festive = app.festive()
    return f"{base}/logo/{ch['id']}.png?v={LOGO_V}" + (f"-{festive}" if festive else "")


def discover(app, base: str) -> dict:
    return {
        "FriendlyName": "Rerun",
        "Manufacturer": "Silicondust",
        "ManufacturerURL": "https://www.silicondust.com/",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomeruntc_atsc",
        "FirmwareVersion": "20240101",
        "TunerCount": int(app.cfg.data["stream"].get("tuners") or 4),
        "DeviceID": device_id(app.secrets.data["client_id"]),
        "DeviceAuth": "rerun",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
    }


def lineup(app, base: str) -> list[dict]:
    return [
        {"GuideNumber": str(c["number"]), "GuideName": c["name"], "URL": f"{base}/stream/{c['number']}.ts", "HD": 1}
        for c in enabled_channels(app)
    ]


def lineup_status() -> dict:
    return {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]}


def m3u(app, base: str) -> str:
    lines = [f'#EXTM3U url-tvg="{base}/xmltv.xml" x-tvg-url="{base}/xmltv.xml"']
    for c in enabled_channels(app):
        name = c["name"].replace(",", " ").replace('"', "'")
        lines.append(
            f'#EXTINF:-1 tvg-id="{c["number"]}" tvg-chno="{c["number"]}" tvg-name="{name}" '
            f'tvg-logo="{logo_url(app, base, c)}" group-title="{c.get("group") or "Rerun"}",{name}'
        )
        lines.append(f"{base}/stream/{c['number']}.ts")
    return "\n".join(lines) + "\n"


def xmltv(app, base: str) -> str:
    now = time.time()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<tv generator-info-name="Rerun {__version__}" source-info-name="Rerun">']
    chans = enabled_channels(app)
    for c in chans:
        logo = logo_url(app, base, c)
        out.append(f'  <channel id="{c["number"]}">')
        out.append(f"    <display-name>{_x(c['name'])}</display-name>")
        out.append(f"    <display-name>{c['number']}</display-name>")
        out.append(f"    <icon src={_a(logo)} />")
        out.append("  </channel>")
    for c in chans:
        p = app.schedules.get(c["id"])
        for b in p.blocks_between(now - 3 * 3600, now + 14 * 86400):
            out.append(f'  <programme start="{_ts(b["s"])}" stop="{_ts(b["e"])}" channel="{c["number"]}">')
            out.append(f'    <title lang="en">{_x(b.get("title") or c["name"])}</title>')
            if b.get("sub"):
                out.append(f'    <sub-title lang="en">{_x(b["sub"])}</sub-title>')
            if b.get("desc"):
                out.append(f'    <desc lang="en">{_x(b["desc"])}</desc>')
            date = (b.get("date") or "").replace("-", "")[:8] or (str(b["year"]) if b.get("year") else "")
            if date:
                out.append(f"    <date>{_x(date)}</date>")
            for cat in b.get("cats") or []:
                out.append(f'    <category lang="en">{_x(cat)}</category>')
            if b.get("img"):
                img = f"{base}/img/{b['img']}.jpg"
                out.append(f"    <icon src={_a(img)} />")
            if b.get("sn") or b.get("en"):
                sn, en = int(b.get("sn") or 0), int(b.get("en") or 0)
                out.append(f'    <episode-num system="xmltv_ns">{max(sn - 1, 0)}.{max(en - 1, 0)}.0/1</episode-num>')
                out.append(f'    <episode-num system="onscreen">S{sn:02d}E{en:02d}</episode-num>')
            if b.get("rating"):
                out.append(f'    <rating system="MPAA"><value>{_x(b["rating"])}</value></rating>')
            if not b.get("movie"):
                out.append("    <previously-shown />")
            out.append("  </programme>")
    out.append("</tv>")
    return "\n".join(out) + "\n"
