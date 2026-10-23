"""Crash-safe JSON storage.

Everything Rerun keeps lives in one data folder. Only config.json and
secrets.json matter; the catalog, schedules and caches can be deleted at any
time and are rebuilt automatically.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

from .seasons import DEFAULT_HOLIDAYS

log = logging.getLogger("rerun.store")

DATA_DIR = Path(os.environ.get("RERUN_DATA", "data")).resolve()
CONFIG_VERSION = 2
BACKUP_EVERY = 600  # seconds between config backups


def atomic_write_json(path, data, *, backups: int = 0, private: bool = False, pretty: bool = False) -> None:
    """Write to a temp file, fsync, then rename, so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    if backups and path.exists():
        _backup(path, backups)
    os.replace(tmp, path)


def _backup(path: Path, keep: int) -> None:
    bdir = path.parent / "backups"
    bdir.mkdir(exist_ok=True)
    existing = sorted(bdir.glob(f"{path.stem}-*{path.suffix}"))
    if existing and time.time() - existing[-1].stat().st_mtime < BACKUP_EVERY:
        return
    shutil.copy2(path, bdir / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
    for old in sorted(bdir.glob(f"{path.stem}-*{path.suffix}"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def read_json(path, default):
    """Read a JSON file. A damaged file is set aside and the newest backup is used instead."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError) as e:
        log.error("Could not read %s (%s); setting it aside", path.name, e)
        try:
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        for b in sorted((path.parent / "backups").glob(f"{path.stem}-*{path.suffix}"), reverse=True):
            try:
                with open(b, encoding="utf-8") as f:
                    data = json.load(f)
                log.warning("Restored %s from backup %s", path.name, b.name)
                return data
            except (OSError, ValueError):
                continue
        return copy.deepcopy(default)


def merge_defaults(data, defaults):
    """Fill in any settings an older version didn't know about, so updates never break config."""
    if not isinstance(data, dict):
        return copy.deepcopy(defaults)
    for k, v in defaults.items():
        if k not in data:
            data[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(data[k], dict):
            merge_defaults(data[k], v)
    return data


DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "setup_complete": False,
    "plex": {"url": "", "server_name": "", "server_id": ""},
    # Each source is {"type": "plex", "id": "3", "name": "TV Shows"} or {"type": "folder", "path": "/media/ads"}
    "sources": {"shows": [], "movies": [], "music_videos": [], "commercials": []},
    "metadata": {"online_lookups": True, "include_specials": False},
    "path_map": [],  # [{"from": "/data/tv", "to": "/mnt/media/tv"}]
    "stream": {
        "resolution": "720p",
        "video_kbps": 4000,
        "audio_kbps": 192,
        "hwaccel": "auto",  # auto (Intel Quick Sync / AMD if it works) | none | vaapi | nvenc
        "vaapi_device": "/dev/dri/renderD128",
        "even_volume": True,
        "tuners": 4,
    },
    "security": {"lan_only": True, "extra_networks": []},
    "rescan_hour": 4,
    "guide_hours": 72,
    # After each scan, add channels for networks (and holidays) that appear in your library.
    "auto": {"networks": True, "holidays": True, "min_shows": 2, "dismissed": []},
    "festive_logos": False,  # a small holiday badge on every channel logo while a holiday is in season
    "holidays": DEFAULT_HOLIDAYS,
    "channels": [],
}

DEFAULT_SECRETS = {
    "password_hash": "",
    "plex_token": "",
    "tmdb_key": "",
    "client_id": "",
}


def migrate(data: dict) -> dict:
    v = data.get("version", CONFIG_VERSION)
    if v > CONFIG_VERSION:
        log.warning("config.json was written by a newer Rerun (v%s); keeping unknown settings as-is", v)
    # One `if v < N:` step per change, so every older config.json is brought up to date.
    if v < 2:
        data["festive_logos"] = False  # the holiday badge on every logo was on by default; now it's opt-in
        stream = data.get("stream") or {}
        if stream.get("hwaccel") == "none":
            stream["hwaccel"] = "auto"  # use Intel Quick Sync / AMD when it works
    data["version"] = max(v, CONFIG_VERSION)
    return data


class JsonDoc:
    def __init__(self, name: str, defaults: dict, *, backups: int = 0, private: bool = False):
        self.path = DATA_DIR / name
        self.backups = backups
        self.private = private
        self.lock = threading.RLock()
        self.data = merge_defaults(read_json(self.path, defaults), defaults)

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.data, backups=self.backups, private=self.private, pretty=True)


def load_config() -> JsonDoc:
    doc = JsonDoc("config.json", DEFAULT_CONFIG, backups=10)
    before = doc.data.get("version")
    doc.data = migrate(doc.data)
    if before != doc.data["version"] and doc.path.exists():
        doc.save()
    return doc


def load_secrets() -> JsonDoc:
    doc = JsonDoc("secrets.json", DEFAULT_SECRETS, private=True)
    if not doc.data.get("client_id"):
        doc.data["client_id"] = "rerun-" + secrets.token_hex(12)
        doc.save()
    return doc
