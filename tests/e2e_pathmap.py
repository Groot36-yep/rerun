"""Point TV paths somewhere that doesn't exist so Rerun must fetch episodes through Plex.

    python tests/e2e_pathmap.py http://127.0.0.1:8410 MEDIA_DIR [clear]
"""
import os
import sys

import httpx

BASE, MEDIA = sys.argv[1], sys.argv[2]
c = httpx.Client(base_url=BASE, timeout=30, headers={"X-Rerun": "1"})
c.post("/api/auth/login", json={"password": "correct horse"}).raise_for_status()
tv = os.path.join(os.path.normpath(MEDIA), "tv")
mapping = [] if "clear" in sys.argv else [{"from": tv, "to": "Z:/not-mounted/tv"}]
r = c.put("/api/settings", json={"path_map": mapping})
print(r.status_code, r.json()["path_map"])
