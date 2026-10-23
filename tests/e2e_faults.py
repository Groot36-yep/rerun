"""Make a test channel full of problems: a corrupted episode, one with no audio, and a moved file.

    python tests/e2e_faults.py http://127.0.0.1:8410 MEDIA_DIR
"""
import os
import sys

import httpx

BASE, MEDIA = sys.argv[1], sys.argv[2]
c = httpx.Client(base_url=BASE, timeout=30, headers={"X-Rerun": "1"})
c.post("/api/auth/login", json={"password": "correct horse"}).raise_for_status()

# Corrupt one episode on disk (after the scan already saw it as fine).
bad = os.path.join(MEDIA, "tv", "Dexter Test", "S01E02.mp4")
with open(bad, "r+b") as f:
    f.seek(0)
    f.write(os.urandom(4096))
print("corrupted", bad)

for ch in c.get("/api/channels").json():
    if ch["name"] == "Fault Test":
        c.delete(f"/api/channels/{ch['id']}")
r = c.post("/api/channels", json={
    "name": "Fault Test", "number": 9, "slot_minutes": 0, "order": "in_order",
    "rules": [{"type": "show", "id": "p300"}, {"type": "show", "id": "p400"}],
    "commercials": {"enabled": True, "break_style": "end", "tags": ["90s"]},
})
print("created", r.status_code, r.json()["info"])
