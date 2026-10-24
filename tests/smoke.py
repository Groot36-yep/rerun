"""Visit every page's data and every Plex file once and fail on any server error.

    python tests/smoke.py [http://127.0.0.1:8410] [password]
"""
import re
import sys

import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8410"
password = sys.argv[2] if len(sys.argv) > 2 else "correct horse"
c = httpx.Client(base_url=base, headers={"X-Rerun": "1"}, timeout=60)
c.post("/api/auth/login", json={"password": password}).raise_for_status()

paths = ["/", "/health", "/discover.json", "/lineup.json", "/lineup_status.json", "/xmltv.xml", "/iptv.m3u",
         "/api/bootstrap", "/api/channels", "/api/networks", "/api/holidays", "/api/settings", "/api/sources",
         "/api/scan", "/api/suggestions", "/api/catalog/facets", "/api/logos", "/api/guide?hours=6", "/api/connect",
         "/api/status", "/api/plex/status", "/api/catalog/search?q=te"]
chans = c.get("/api/channels").json()
for ch in chans:
    paths += [f"/api/channels/{ch['id']}", f"/api/channels/{ch['id']}/schedule?hours=4", ch["logo_url"]]
x = c.get("/xmltv.xml").text
paths += sorted({u.split("8410", 1)[-1] for u in re.findall(r'<icon src="([^"]+/logo/[^"]+)"', x)})
for n in c.get("/api/networks").json()["networks"][:10]:
    paths.append(n["logo_url"])
for hd in c.get("/api/holidays").json()["holidays"]:
    paths.append(hd["logo_url"])

bad = 0
for p in paths:
    r = c.get(p)
    ok = r.status_code < 500
    bad += not ok
    if not ok or "-v" in sys.argv:
        print(r.status_code, p, "" if ok else r.text[:200])
print(f"{len(paths)} requests, {bad} server errors")
sys.exit(1 if bad else 0)
