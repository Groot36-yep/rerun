"""Exercise networks, holidays, collections and logos on a running Rerun (after e2e_setup.py).

    python tests/e2e_features.py http://127.0.0.1:8410
"""
import sys

import httpx

c = httpx.Client(base_url=sys.argv[1], headers={"X-Rerun": "1"}, timeout=60)
c.post("/api/auth/login", json={"password": "correct horse"}).raise_for_status()
c.put("/api/settings", json={"metadata": {"online_lookups": True}}).raise_for_status()

for n in c.get("/api/networks").json()["networks"]:
    print("network", n["name"], n["shows"], "eligible" if n["eligible"] else "small", "sisters", n["sisters"],
          "channel", n["number"])
for hd in c.get("/api/holidays").json()["holidays"]:
    if hd["episodes"] or hd["movies"]:
        print("holiday", hd["key"], hd["episodes"], "eps", hd["movies"], "movies", hd["samples"])
f = c.get("/api/catalog/facets").json()
print("collections", f["show_collections"], f["movie_collections"])
print("xmas channel", c.post("/api/holidays/christmas/channel").json())
print("CN channel", c.post("/api/networks/Cartoon%20Network/channel").json())
coll = c.post("/api/channels", json={"name": "Saturday Morning", "rules": [
    {"type": "filter", "media": "shows", "collection": "Saturday Morning"}]}).json()
print("collection channel", coll["number"], coll["info"]["show_list"])
for ch in c.get("/api/channels").json():
    now = (ch["now"] or {}).get("title")
    print(f"ch {ch['number']:>3} {ch['name']:<20} group={ch['group']!r} logo={ch['logo']!r} now={now!r}")
    r = c.get(ch["logo_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png", ch["name"]
r = c.delete("/api/networks/Cartoon%20Network/channel")
print("CN off", r.json(), "dismissed:", c.get("/api/settings").json()["dismissed"])
x = c.get("/xmltv.xml").text
print("xmltv channels:", x.count("<channel "), "programmes:", x.count("<programme"))
print("lineup:", [(l["GuideNumber"], l["GuideName"]) for l in c.get("/lineup.json").json()])
print("logos available:", len(c.get("/api/logos").json()))
for path in ("/netlogo/Nickelodeon.png", "/netlogo/Kids'%20WB.png", "/holidaylogo/halloween.png"):
    r = c.get(path)
    print(path, r.status_code, len(r.content))
