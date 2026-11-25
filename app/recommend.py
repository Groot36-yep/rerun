"""Shows to get: the series Nickelodeon and Disney Channel were known for, by decade, checked against
your library.

A network's channel is built from what Plex says each show's network is, so it can only play what you
have, and only what Plex files under that network. This lists each network's best-known series of the
90s, 2000s and 2010s and says, for each: you don't have it (get it), you have it but it isn't on the
channel (Plex files it under another network: add it), or it's on the channel.

(title, year the series began, how much the network aired it, other titles it goes by). Years and
titles are the series' own, for matching your library; "staple" means the network ran it year after year.
"""
from __future__ import annotations

from . import archive
from .lineups import TIER_ORDER, find_show

DECADES = ("90s", "2000s", "2010s")

NICKELODEON = {
    "90s": [
        ("Rugrats", 1991, "staple", []),
        ("Doug", 1991, "staple", []),
        ("The Ren & Stimpy Show", 1991, "staple", ["Ren & Stimpy", "The Ren and Stimpy Show"]),
        ("Rocko's Modern Life", 1993, "staple", []),
        ("Hey Arnold!", 1996, "staple", ["Hey Arnold"]),
        ("CatDog", 1998, "staple", []),
        ("The Wild Thornberrys", 1998, "staple", []),
        ("SpongeBob SquarePants", 1999, "staple", []),
        ("Clarissa Explains It All", 1991, "staple", []),
        ("All That", 1994, "staple", []),
        ("Kenan & Kel", 1996, "staple", ["Kenan and Kel"]),
        ("Aaahh!!! Real Monsters", 1994, "regular", ["Aaahh Real Monsters", "Real Monsters"]),
        ("The Angry Beavers", 1997, "regular", []),
        ("Rocket Power", 1999, "regular", []),
        ("The Adventures of Pete & Pete", 1993, "regular", ["The Adventures of Pete and Pete"]),
        ("The Secret World of Alex Mack", 1994, "regular", []),
        ("Are You Afraid of the Dark?", 1990, "regular", ["Are You Afraid of the Dark"]),
        ("Legends of the Hidden Temple", 1993, "regular", []),
        ("The Amanda Show", 1999, "regular", []),
        ("Oh Yeah! Cartoons", 1998, "occasional", []),
        ("Salute Your Shorts", 1991, "occasional", []),
        ("Guts", 1992, "occasional", ["Global Guts"]),
        ("Figure It Out", 1997, "occasional", []),
        ("Space Cases", 1996, "occasional", []),
        ("The Journey of Allen Strange", 1997, "occasional", []),
        ("Cousin Skeeter", 1998, "occasional", []),
        ("100 Deeds for Eddie McDowd", 1999, "occasional", []),
    ],
    "2000s": [
        ("The Fairly OddParents", 2001, "staple", ["The Fairly Odd Parents"]),
        ("The Adventures of Jimmy Neutron, Boy Genius", 2002, "staple",
         ["The Adventures of Jimmy Neutron: Boy Genius", "Jimmy Neutron"]),
        ("Danny Phantom", 2004, "staple", []),
        ("Avatar: The Last Airbender", 2005, "staple", ["Avatar The Last Airbender"]),
        ("Drake & Josh", 2004, "staple", ["Drake and Josh"]),
        ("Zoey 101", 2005, "staple", []),
        ("Ned's Declassified School Survival Guide", 2004, "staple", []),
        ("iCarly", 2007, "staple", []),
        ("Invader Zim", 2001, "regular", []),
        ("ChalkZone", 2002, "regular", []),
        ("As Told by Ginger", 2000, "regular", []),
        ("All Grown Up!", 2003, "regular", ["All Grown Up"]),
        ("My Life as a Teenage Robot", 2003, "regular", []),
        ("Back at the Barnyard", 2007, "regular", []),
        ("The Penguins of Madagascar", 2008, "regular", []),
        ("Unfabulous", 2004, "regular", []),
        ("True Jackson, VP", 2008, "regular", ["True Jackson VP"]),
        ("The Naked Brothers Band", 2007, "regular", []),
        ("Big Time Rush", 2009, "regular", []),
        ("Catscratch", 2005, "occasional", []),
        ("The X's", 2005, "occasional", []),
        ("El Tigre: The Adventures of Manny Rivera", 2007, "occasional", ["El Tigre"]),
        ("Tak and the Power of Juju", 2007, "occasional", []),
        ("The Mighty B!", 2008, "occasional", ["The Mighty B"]),
        ("Fanboy & Chum Chum", 2009, "occasional", ["Fanboy and Chum Chum"]),
        ("Romeo!", 2003, "occasional", ["Romeo"]),
        ("Taina", 2001, "occasional", []),
        ("Just Jordan", 2007, "occasional", []),
    ],
    "2010s": [
        ("Victorious", 2010, "staple", []),
        ("The Legend of Korra", 2012, "staple", []),
        ("Teenage Mutant Ninja Turtles", 2012, "staple", []),
        ("The Loud House", 2016, "staple", []),
        ("Henry Danger", 2014, "staple", []),
        ("The Thundermans", 2013, "staple", []),
        ("T.U.F.F. Puppy", 2010, "regular", ["TUFF Puppy"]),
        ("Kung Fu Panda: Legends of Awesomeness", 2011, "regular", []),
        ("Sanjay and Craig", 2013, "regular", []),
        ("Breadwinners", 2014, "regular", []),
        ("Sam & Cat", 2013, "regular", ["Sam and Cat"]),
        ("Game Shakers", 2015, "regular", []),
        ("Nicky, Ricky, Dicky & Dawn", 2014, "regular", ["Nicky Ricky Dicky and Dawn"]),
        ("Harvey Beaks", 2015, "occasional", []),
        ("Pig Goat Banana Cricket", 2015, "occasional", []),
        ("Rise of the Teenage Mutant Ninja Turtles", 2018, "occasional", []),
        ("The Casagrandes", 2019, "occasional", []),
        ("Bella and the Bulldogs", 2015, "occasional", []),
        ("How to Rock", 2012, "occasional", []),
        ("Supah Ninjas", 2011, "occasional", []),
        ("School of Rock", 2016, "occasional", []),
        ("Knight Squad", 2018, "occasional", []),
    ],
}

DISNEY_CHANNEL = {
    "90s": [
        ("The Famous Jett Jackson", 1998, "staple", []),
        ("So Weird", 1999, "staple", []),
        ("Recess", 1997, "staple", []),
        ("Boy Meets World", 1993, "regular", []),
        ("Bug Juice", 1998, "regular", []),
        ("Pepper Ann", 1997, "regular", []),
        ("Adventures in Wonderland", 1992, "regular", []),
        ("The All-New Mickey Mouse Club", 1989, "regular", ["The Mickey Mouse Club", "MMC"]),
        ("Smart Guy", 1997, "occasional", []),
        ("Flash Forward", 1995, "occasional", []),
        ("Brand Spankin' New! Doug", 1996, "occasional", ["Disney's Doug", "Brand Spankin New Doug"]),
    ],
    "2000s": [
        ("Even Stevens", 2000, "staple", []),
        ("Lizzie McGuire", 2001, "staple", []),
        ("The Proud Family", 2001, "staple", []),
        ("Kim Possible", 2002, "staple", []),
        ("That's So Raven", 2003, "staple", ["Thats So Raven"]),
        ("The Suite Life of Zack & Cody", 2005, "staple", ["The Suite Life of Zack and Cody"]),
        ("Hannah Montana", 2006, "staple", []),
        ("Wizards of Waverly Place", 2007, "staple", []),
        ("Phineas and Ferb", 2007, "staple", []),
        ("The Weekenders", 2000, "regular", []),
        ("Lilo & Stitch: The Series", 2003, "regular", ["Lilo and Stitch The Series"]),
        ("Phil of the Future", 2004, "regular", []),
        ("American Dragon: Jake Long", 2005, "regular", ["American Dragon Jake Long"]),
        ("The Replacements", 2006, "regular", []),
        ("Cory in the House", 2007, "regular", []),
        ("The Suite Life on Deck", 2008, "regular", []),
        ("Sonny with a Chance", 2009, "regular", []),
        ("Dave the Barbarian", 2004, "occasional", []),
        ("Brandy & Mr. Whiskers", 2004, "occasional", ["Brandy and Mr. Whiskers"]),
        ("The Buzz on Maggie", 2005, "occasional", []),
        ("Fillmore!", 2002, "occasional", ["Fillmore"]),
        ("JONAS", 2009, "occasional", ["Jonas", "Jonas L.A."]),
    ],
    "2010s": [
        ("Good Luck Charlie", 2010, "staple", []),
        ("Gravity Falls", 2012, "staple", []),
        ("Jessie", 2011, "staple", []),
        ("Austin & Ally", 2011, "staple", ["Austin and Ally"]),
        ("Shake It Up", 2010, "regular", []),
        ("A.N.T. Farm", 2011, "regular", ["ANT Farm"]),
        ("Dog with a Blog", 2012, "regular", []),
        ("Liv and Maddie", 2013, "regular", []),
        ("Girl Meets World", 2014, "regular", []),
        ("K.C. Undercover", 2015, "regular", ["KC Undercover"]),
        ("Bunk'd", 2015, "regular", ["Bunkd"]),
        ("Star vs. the Forces of Evil", 2015, "regular", ["Star vs the Forces of Evil"]),
        ("Andi Mack", 2017, "regular", []),
        ("Raven's Home", 2017, "regular", ["Ravens Home"]),
        ("Big City Greens", 2018, "regular", []),
        ("Amphibia", 2019, "regular", []),
        ("Fish Hooks", 2010, "occasional", []),
        ("Wander Over Yonder", 2013, "occasional", []),
        ("Best Friends Whenever", 2015, "occasional", []),
        ("Stuck in the Middle", 2016, "occasional", []),
        ("Tangled: The Series", 2017, "occasional", ["Rapunzel's Tangled Adventure"]),
        ("Coop & Cami Ask the World", 2018, "occasional", []),
        ("Sydney to the Max", 2019, "occasional", []),
    ],
}

LISTS = {"nick": ("Nickelodeon", NICKELODEON), "disney": ("Disney Channel", DISNEY_CHANNEL)}
# networks whose channels get a network's list (Nicktoons shows are Nick shows)
FAMILY = {"nick": "nick", "nicktoons": "nick", "disney": "disney", "toondisney": "disney"}


def which(name: str) -> str | None:
    """The list for a network name ("Nickelodeon", "Nicktoons" -> "nick"), if there is one."""
    return FAMILY.get(archive.tag_for_network(name) or "")


def report(cat, key: str, on_channel: set | None = None) -> dict:
    """Every show on the network's list, by decade: in the library or not, and on the channel or not."""
    label, data = LISTS[key]
    out = {"key": key, "network": label, "decades": []}
    for dec in DECADES:
        rows = []
        for title, year, tier, alt in data[dec]:
            s = find_show(cat, title, year, alt)
            rows.append({"title": title, "year": year, "tier": tier, "have": bool(s), "id": s["id"] if s else "",
                         "match": s["title"] if s else "", "episodes": len(cat.episodes.get(s["id"], [])) if s else 0,
                         "on_channel": bool(s and on_channel is not None and s["id"] in on_channel)})
        rows.sort(key=lambda r: (r["have"], TIER_ORDER[r["tier"]], r["title"].lower()))
        out["decades"].append({"decade": dec, "shows": rows, "have": sum(r["have"] for r in rows), "total": len(rows)})
    out["have"] = sum(d["have"] for d in out["decades"])
    out["total"] = sum(d["total"] for d in out["decades"])
    return out


def need_text(cat, key: str) -> str:
    rep = report(cat, key)
    lines = [f"{rep['network']}: shows to get ({rep['total'] - rep['have']} of {rep['total']})", ""]
    names = {"staple": "aired all the time", "regular": "aired often", "occasional": "aired now and then"}
    for d in rep["decades"]:
        need = [r for r in d["shows"] if not r["have"]]
        if need:
            lines.append(f"{d['decade']}:")
            lines += [f"  {r['title']} ({r['year']}) - {names[r['tier']]}" for r in need]
            lines.append("")
    return "\n".join(lines)


def channel_networks(ch: dict) -> list[str]:
    """Which lists a channel gets: from its network ("network:Nickelodeon") and its network rules."""
    names = []
    key = ch.get("auto_key") or (ch.get("quick") or {}).get("key") or ""
    if key.startswith("network:"):
        names.append(key[8:])
    for r in ch.get("rules") or []:
        if r.get("network"):
            names.append(r["network"])
        names += list(r.get("networks") or [])
    out = []
    for n in names:
        k = which(n)
        if k and k not in out:
            out.append(k)
    return out
