"""Classic channel lineups: the shows a channel really aired, for channels that can't be built from your
library's "network" field.

Boomerang ran other networks' cartoons: Plex lists Scooby-Doo under CBS and The Jetsons under ABC, so
a Boomerang channel is built from Boomerang's own lineup instead. `wants()` says which of its shows you
have and which you still need; `show_ids()` is what the channel plays.

Boomerang's US lineup for 2000–2009 is from Wikipedia's "List of programs broadcast by Boomerang"
(staples are the shows it ran year after year; occasional ones aired for a season or less).
"""
from __future__ import annotations

from .xmas import norm

# (title as usually listed, year the series began, how much Boomerang aired it, other titles, years on Boomerang)
# tier: "staple" | "regular" | "occasional"
BOOMERANG = [
    # Hanna-Barbera, the heart of the channel
    ("Scooby-Doo, Where Are You!", 1969, "staple", [], "2000s"),
    ("The Flintstones", 1960, "staple", [], "from 2000"),
    ("The Jetsons", 1962, "staple", [], "2000s"),
    ("The Yogi Bear Show", 1961, "staple", ["Yogi Bear"], "2000s"),
    ("Jonny Quest", 1964, "staple", ["The Adventures of Jonny Quest"], "2000s"),
    ("The Scooby-Doo Show", 1976, "staple", [], "2000s"),
    ("The New Scooby-Doo Movies", 1972, "staple", [], "2000s"),
    ("Jabberjaw", 1976, "staple", [], "2000s"),
    ("Josie and the Pussycats", 1970, "staple", [], "2000s"),
    ("Dastardly and Muttley in Their Flying Machines", 1969, "staple", ["Dastardly and Muttley"], "2001–2012"),
    ("The Atom Ant Show", 1965, "staple", ["Atom Ant"], "from 2000"),
    ("The Smurfs", 1981, "staple", [], "2000s"),
    ("Scooby-Doo and Scrappy-Doo", 1979, "regular", [], "2000s"),
    ("The New Scooby and Scrappy-Doo Show", 1983, "regular", [], "2000s"),
    ("The New Scooby-Doo Mysteries", 1984, "regular", [], "2000s"),
    ("The 13 Ghosts of Scooby-Doo", 1985, "regular", [], "from 2003"),
    ("A Pup Named Scooby-Doo", 1988, "regular", [], "from 2003"),
    ("What's New, Scooby-Doo?", 2002, "regular", [], "from 2006"),
    ("The Pebbles and Bamm-Bamm Show", 1971, "regular", [], "2000s"),
    ("Thundarr the Barbarian", 1980, "regular", [], "2000s"),
    ("Josie and the Pussycats in Outer Space", 1972, "regular", [], "2001–2011"),
    ("Speed Buggy", 1973, "regular", [], "2001–2014"),
    ("Wally Gator", 1962, "regular", [], "from 2001"),
    ("Inch High, Private Eye", 1973, "regular", [], "2001–2011"),
    ("Captain Caveman and the Teen Angels", 1977, "regular", [], "2002–2007"),
    ("The Funky Phantom", 1971, "regular", [], "2001–2013"),
    ("Cattanooga Cats", 1969, "regular", [], "2001–2013"),
    ("The Amazing Chan and the Chan Clan", 1972, "regular", [], "2001–2011"),
    ("Pac-Man", 1982, "regular", [], "from 2002"),
    ("The Peter Potamus Show", 1964, "regular", ["Peter Potamus"], "2002–2011"),
    ("Clue Club", 1976, "regular", [], "2002–2011"),
    ("The Kwicky Koala Show", 1981, "regular", [], "2002–2011"),
    ("Galaxy Goof-Ups", 1978, "regular", [], "from 2001"),
    ("The Adventures of Gulliver", 1968, "regular", [], "2002–2011"),
    ("Super Friends", 1973, "regular", ["The All-New Super Friends Hour", "Challenge of the Superfriends"], "2000s"),
    ("Shazzan", 1967, "regular", [], "2000s"),
    ("Captain Planet and the Planeteers", 1990, "regular", ["Captain Planet"], "from 2005"),
    ("2 Stupid Dogs", 1993, "regular", [], "from 2005"),
    ("The Addams Family", 1992, "regular", [], "from 2006"),
    ("Where's Huddles?", 1970, "occasional", [], "2001–2007"),
    ("Devlin", 1974, "occasional", [], "2001–2008"),
    ("Snooper and Blabber", 1959, "occasional", [], "2002"),
    ("Moby Dick and Mighty Mightor", 1967, "occasional", [], "2000s"),
    ("Young Samson & Goliath", 1967, "occasional", ["Young Samson"], "2000s"),
    ("Goober and the Ghost Chasers", 1973, "occasional", [], "2000s"),
    ("Birdman and the Galaxy Trio", 1967, "occasional", [], "2009"),
    ("The Fantastic Four", 1967, "occasional", [], "2009"),
    ("Frankenstein Jr. and The Impossibles", 1966, "occasional", [], "2006"),
    ("Godzilla", 1978, "occasional", ["The Godzilla Power Hour"], "2003"),
    ("The Ruff and Reddy Show", 1957, "occasional", [], "2004"),
    ("Wait Till Your Father Gets Home", 1972, "occasional", [], "2006–2008"),
    ("Fantastic Max", 1988, "occasional", [], "2009"),
    ("The Completely Mental Misadventures of Ed Grimley", 1988, "occasional", [], "2002–2007"),
    ("The New Shmoo", 1979, "occasional", [], "2006"),
    ("Trollkins", 1981, "occasional", [], "2003"),
    ("The Roman Holidays", 1972, "occasional", [], "2004"),
    ("The Skatebirds", 1977, "occasional", [], "2004"),
    ("CB Bears", 1977, "occasional", [], "2004"),
    ("Wheelie and the Chopper Bunch", 1974, "occasional", [], "2004"),
    ("Butch Cassidy and the Sundance Kids", 1973, "occasional", [], "2001–2004"),
    ("Galtar and the Golden Lance", 1985, "occasional", [], "2002"),
    ("Sky Commanders", 1987, "occasional", [], "2001–2005"),
    ("Casper and the Angels", 1979, "occasional", [], "2001–2004"),
    ("The New Adventures of Huckleberry Finn", 1968, "occasional", [], "2004"),
    ("Lippy the Lion and Hardy Har Har", 1962, "occasional", [], "2007"),
    ("Touché Turtle and Dum Dum", 1962, "occasional", [], "2006"),
    ("Paw Paws", 1985, "occasional", [], "2008"),
    ("Loopy De Loop", 1959, "occasional", [], "2009"),
    # Ruby-Spears
    ("Centurions", 1986, "regular", [], "2000–2010"),
    ("Heathcliff", 1980, "regular", ["Heathcliff and Dingbat"], "2002–2007"),
    ("Fangface", 1978, "occasional", [], "2002–2011"),
    ("Mister T", 1983, "occasional", [], "2006"),
    ("Dink, the Little Dinosaur", 1989, "occasional", [], "2008–2011"),
    ("Chuck Norris: Karate Kommandos", 1986, "occasional", [], "2002–2005"),
    ("Goldie Gold and Action Jack", 1981, "occasional", [], "2002–2004"),
    ("Dragon's Lair", 1984, "occasional", [], "2006"),
    ("Young Robin Hood", 1991, "occasional", [], "2007"),
    # Warner Bros. and MGM
    ("Looney Tunes", 1930, "staple", ["Merrie Melodies", "Looney Tunes Golden Collection"], "2000s"),
    ("Tom and Jerry", 1940, "staple", ["Tom & Jerry", "Tom and Jerry Golden Collection"], "2000s"),
    ("The Bugs Bunny Show", 1960, "staple", [], "2002–2007"),
    ("The Bugs Bunny/Road Runner Hour", 1968, "regular", ["The Road Runner Show"], "from 2004"),
    ("The Bugs and Daffy Show", 1996, "regular", [], "from 2005"),
    ("Baby Looney Tunes", 2002, "regular", [], "from 2005"),
    ("Duck Dodgers", 2003, "regular", [], "from 2005"),
    ("Tom & Jerry Kids", 1990, "regular", ["Tom and Jerry Kids Show"], "from 2005"),
    ("Popeye the Sailor", 1960, "staple", ["Popeye"], "2000s"),
    ("The Popeye Show", 2001, "occasional", [], "2006–2007"),
    ("ToonHeads", 1992, "occasional", [], "2005–2006"),
    ("Batman: The Animated Series", 1992, "regular", [], "from 2007"),
    ("Superman: The Animated Series", 1996, "regular", [], "2000s"),
    ("Justice League", 2001, "regular", [], "2006–2007"),
    ("Justice League Unlimited", 2004, "regular", [], "2007–2010"),
    ("The Batman", 2004, "regular", [], "2007–2011"),
    ("Teen Titans", 2003, "regular", [], "from 2009"),
    ("Krypto the Superdog", 2005, "regular", [], "2007–2014"),
    # Cartoon Network's own, which moved to Boomerang late in the decade
    ("Dexter's Laboratory", 1996, "staple", [], "from 2006"),
    ("Johnny Bravo", 1997, "staple", [], "from 2008"),
    ("The Powerpuff Girls", 1998, "staple", [], "from December 2008"),
    ("Courage the Cowardly Dog", 1999, "regular", [], "from October 2009"),
    ("Cow and Chicken", 1997, "regular", [], "from 2007"),
    ("I Am Weasel", 1997, "regular", [], "from 2007"),
    ("Samurai Jack", 2001, "regular", [], "from 2009"),
    ("Mike, Lu & Og", 1999, "regular", [], "2006–2011"),
    # bought from others
    ("The Pink Panther Show", 1969, "staple", ["The Pink Panther"], "2005–2012"),
    ("Garfield and Friends", 1988, "staple", [], "from 2006"),
    ("The Dudley Do-Right Show", 1969, "regular", ["Dudley Do-Right"], "2002–2006"),
    ("The Mr. Magoo Show", 1960, "regular", ["Mr. Magoo"], "2004–2006"),
    ("Dennis the Menace", 1986, "regular", [], "2007–2010"),
    ("Voltron: Defender of the Universe", 1984, "regular", ["Voltron"], "2008–2009"),
    ("Underdog", 1964, "regular", [], "2000s"),
    ("Casper and Friends", 1990, "regular", [], "2003–2013"),
    ("Tennessee Tuxedo and His Tales", 1963, "occasional", [], "2007"),
    ("Battle of the Planets", 1978, "occasional", [], "2004"),
    ("Batfink", 1967, "occasional", [], "2006–2007"),
    ("Pokémon", 1997, "occasional", ["Pokemon"], "2006–2017"),
    ("He-Man and the Masters of the Universe", 1983, "occasional", [], "2006"),
    ("Fantastic Four: World's Greatest Heroes", 2006, "occasional", [], "2008–2009"),
    ("The Mr. Men Show", 2008, "occasional", [], "2009–2013"),
    ("Gerald McBoing-Boing", 2005, "occasional", [], "2005–2009"),
    ("The Munsters", 1964, "occasional", [], "2000s"),
]

LINEUPS = {
    "boomerang": {
        "name": "Boomerang", "years": "2000–2009", "network": "Boomerang", "ads": "boomerang",
        "about": "Boomerang from Cartoon Network: Hanna-Barbera, Looney Tunes and Tom and Jerry all day, "
                 "and by the end of the decade the Cartoon Cartoons too.",
        "shows": BOOMERANG,
    },
}

TIER_ORDER = {"staple": 0, "regular": 1, "occasional": 2}
_cache: dict = {}


def _by_title(cat) -> dict:
    key = (id(cat), cat.scanned_at)
    hit = _cache.get("idx")
    if hit and hit[0] == key:
        return hit[1]
    out: dict[str, list] = {}
    for s in cat.shows.values():
        out.setdefault(norm(s.get("title", "")), []).append(s)
    _cache["idx"] = (key, out)
    return out


def find_show(cat, title: str, year: int, alt: list[str]) -> dict | None:
    """Your library's copy of a series: same name (or another name it goes by), and a first year within
    two of the original's (Plex sometimes dates a series by its first season on DVD)."""
    idx = _by_title(cat)
    for i, name in enumerate([title, *alt]):
        slack = 2 if i == 0 else 8  # another name is often a later version of the same show
        for s in idx.get(norm(name), []):
            if not s.get("year") or abs(int(s["year"]) - year) <= slack:
                return s
    return None


def wants(cat, key: str) -> list[dict]:
    out = []
    for title, year, tier, alt, aired in LINEUPS[key]["shows"]:
        s = find_show(cat, title, year, alt)
        out.append({"title": title, "year": year, "tier": tier, "aired": aired, "have": bool(s),
                    "match": s["title"] if s else "", "episodes": len(cat.episodes.get(s["id"], [])) if s else 0})
    out.sort(key=lambda w: (w["have"], TIER_ORDER[w["tier"]], w["title"].lower()))
    return out


def show_ids(cat, key: str) -> set[str]:
    if key not in LINEUPS:
        return set()
    ck = ("ids", key, id(cat), cat.scanned_at)
    if ck not in _cache:
        found = (find_show(cat, t, y, alt) for t, y, _tier, alt, _a in LINEUPS[key]["shows"])
        _cache[ck] = {s["id"] for s in found if s}
    return _cache[ck]


def ads_tag(key: str) -> str:
    return (LINEUPS.get(key) or {}).get("ads", "")
