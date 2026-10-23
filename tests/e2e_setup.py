"""Drive a fresh Rerun through first-time setup against fake_plex.py.

    python tests/e2e_setup.py http://127.0.0.1:8410 MEDIA_DIR
"""
import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8410"
MEDIA = sys.argv[2]
H = {"X-Rerun": "1"}

c = httpx.Client(base_url=BASE, timeout=30, headers=H)
for _ in range(60):
    try:
        c.get("/health").raise_for_status()
        break
    except Exception:
        time.sleep(0.5)

boot = c.get("/api/bootstrap").json()
print("bootstrap", boot)
if boot["needs_password"]:
    c.post("/api/auth/setup", json={"password": "correct horse"}).raise_for_status()
else:
    c.post("/api/auth/login", json={"password": "correct horse"}).raise_for_status()

r = c.post("/api/plex/manual", json={"url": "http://127.0.0.1:32499", "token": "test-token"})
print("plex manual", r.status_code, r.json())
print("libraries", c.get("/api/plex/libraries").json())
r = c.put("/api/sources", json={
    "shows": [{"type": "plex", "id": "1", "name": "TV Shows"}],
    "movies": [{"type": "plex", "id": "2", "name": "Movies"}],
    "music_videos": [{"type": "folder", "path": f"{MEDIA}/music"}],
    "commercials": [{"type": "folder", "path": f"{MEDIA}/commercials"}],
})
print("sources", r.status_code, r.json())
print("settings", c.put("/api/settings", json={"metadata": {"online_lookups": False}}).status_code)
print("folders", c.get("/api/folders").json())

c.post("/api/scan").raise_for_status()
while True:
    p = c.get("/api/scan").json()
    if not p["running"]:
        break
    time.sleep(0.5)
print("scan", json.dumps(p, indent=1))

sug = c.get("/api/suggestions").json()
for s in sug["suggestions"]:
    print(" suggestion", s["key"], s["name"], s["why"], s["info"]["hours"], "h", "recommended" if s["recommended"] else "")
keys = [s["key"] for s in sug["suggestions"]]
print("created", c.post("/api/channels/from-suggestions", json={"keys": keys}).json())
c.post("/api/setup/complete").raise_for_status()
time.sleep(2)
for ch in c.get("/api/channels").json():
    now = ch["now"] or {}
    print(f" ch {ch['number']:>2} {ch['name']:<18} now={now.get('title')!r} sub={now.get('sub')!r} "
          f"shows={ch['info']['shows']} ads={ch['info']['commercials']}")
