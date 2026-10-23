"""Turn the many spellings of TV networks into tidy channel names.

"Nicktoons (US)", "[adult swim]" and "Nickelodeon Animation Studio" all become
proper network names. Smaller sister channels name a parent (Nicktoons ->
Nickelodeon, Boomerang -> Cartoon Network): their shows also air on the
parent's channel, like they did in real life.
"""
from __future__ import annotations

import re

# (network, category, parent or "", patterns matched against a lower-cased name)
FAMILIES: list[tuple[str, str, str, list[str]]] = [
    # Kids
    ("Nick Jr.", "kids", "Nickelodeon", [r"^nick jr", r"^noggin"]),
    ("Nicktoons", "kids", "Nickelodeon", [r"^nicktoons"]),
    ("TeenNick", "kids", "Nickelodeon", [r"^teennick", r"^teen nick", r"^the n$"]),
    ("Nick at Nite", "classic", "", [r"^nick at nite"]),
    ("Nickelodeon", "kids", "", [r"nickelodeon", r"^nick$"]),
    ("Adult Swim", "adult", "", [r"adult swim", r"williams street"]),
    ("Boomerang", "kids", "Cartoon Network", [r"^boomerang"]),
    ("Cartoonito", "kids", "Cartoon Network", [r"^cartoonito"]),
    ("Cartoon Network", "kids", "", [r"cartoon network", r"^toonami", r"^hanna[- ]barbera"]),
    ("Disney XD", "kids", "Disney Channel", [r"^disney xd", r"^jetix"]),
    ("Disney Junior", "kids", "Disney Channel", [r"^disney junior", r"^disney jr", r"^playhouse disney"]),
    ("Toon Disney", "kids", "Disney Channel", [r"^toon disney"]),
    ("Disney Channel", "kids", "", [
        r"^disney channel", r"^the disney channel", r"^abc kids", r"^disney television animation",
        r"^walt disney", r"^disney$",
    ]),
    ("Fox Kids", "kids", "", [r"^fox kids", r"^fox box", r"^4kids", r"^fox family"]),
    ("Kids' WB", "kids", "", [r"^kids'? ?wb", r"^the wb kids", r"^wb kids"]),
    ("PBS Kids", "kids", "", [r"^pbs kids", r"^sprout"]),
    ("Universal Kids", "kids", "", [r"^universal kids"]),
    ("Discovery Family", "kids", "", [r"^discovery family", r"^the hub$", r"^hub network", r"^hub$"]),
    ("Discovery Kids", "kids", "Discovery Family", [r"^discovery kids"]),
    ("Qubo", "kids", "", [r"^qubo"]),
    # Music
    ("MTV2", "music", "MTV", [r"^mtv2", r"^mtv 2"]),
    ("MTV", "music", "", [r"^mtv"]),
    ("VH1", "music", "", [r"^vh1", r"^vh-1"]),
    ("CMT", "music", "", [r"^cmt", r"^country music television"]),
    # Cable entertainment
    ("Comedy Central", "cable", "", [r"^comedy central", r"^comedy channel", r"^ha!$"]),
    ("FXX", "cable", "FX", [r"^fxx"]),
    ("FX", "cable", "", [r"^fx$", r"^fx networks", r"^fx on hulu", r"^fx \("]),
    ("AMC", "cable", "", [r"^amc$", r"^amc\+", r"^amc \("]),
    ("TBS", "cable", "", [r"^tbs$", r"^tbs superstation", r"^superstation"]),
    ("TNT", "cable", "", [r"^tnt$", r"^tnt \("]),
    ("USA Network", "cable", "", [r"^usa network", r"^usa$"]),
    ("Syfy", "cable", "", [r"^sy ?fy", r"^sci[- ]?fi"]),
    ("A&E", "cable", "", [r"^a ?& ?e", r"^arts & entertainment"]),
    ("Bravo", "cable", "", [r"^bravo"]),
    ("E!", "cable", "", [r"^e!", r"^e! entertainment"]),
    ("Lifetime", "cable", "", [r"^lifetime"]),
    ("Hallmark Channel", "cable", "", [r"^hallmark"]),
    ("ABC Family", "cable", "Freeform", [r"^abc family"]),
    ("Freeform", "cable", "", [r"^freeform"]),
    ("TV Land", "classic", "", [r"^tv land"]),
    ("IFC", "cable", "", [r"^ifc"]),
    ("BBC America", "cable", "", [r"^bbc america"]),
    ("Spike", "cable", "Paramount Network", [r"^spike"]),
    ("Paramount Network", "cable", "", [r"^paramount network"]),
    ("truTV", "cable", "", [r"^tru ?tv", r"^court tv"]),
    ("Oxygen", "cable", "", [r"^oxygen"]),
    ("BET", "cable", "", [r"^bet$", r"^bet\+"]),
    # Documentary & lifestyle
    ("Investigation Discovery", "factual", "Discovery", [r"^investigation discovery", r"^id$"]),
    ("Science Channel", "factual", "Discovery", [r"^science( channel)?$"]),
    ("Discovery", "factual", "", [r"^discovery( channel)?$", r"^discovery\+"]),
    ("History", "factual", "", [r"^history", r"^the history channel"]),
    ("National Geographic", "factual", "", [r"^national geographic", r"^nat ?geo"]),
    ("TLC", "factual", "", [r"^tlc$", r"^the learning channel"]),
    ("Animal Planet", "factual", "", [r"^animal planet"]),
    ("HGTV", "factual", "", [r"^hgtv"]),
    ("Food Network", "factual", "", [r"^food network", r"^cooking channel"]),
    ("Travel Channel", "factual", "", [r"^travel channel"]),
    # Premium
    ("HBO", "premium", "", [r"^hbo$", r"^hbo \(", r"^hbo family", r"^hbo canada"]),
    ("Cinemax", "premium", "", [r"^cinemax"]),
    ("Showtime", "premium", "", [r"^showtime"]),
    ("Starz", "premium", "", [r"^starz"]),
    ("MGM+", "premium", "", [r"^mgm\+", r"^epix"]),
    # Streaming
    ("Netflix", "streaming", "", [r"^netflix"]),
    ("Disney+", "streaming", "", [r"^disney\+", r"^disney plus"]),
    ("Hulu", "streaming", "", [r"^hulu"]),
    ("Prime Video", "streaming", "", [r"^amazon", r"^prime video"]),
    ("Apple TV+", "streaming", "", [r"^apple tv"]),
    ("Paramount+", "streaming", "", [r"^paramount\+", r"^cbs all access"]),
    ("Peacock", "streaming", "", [r"^peacock"]),
    ("Max", "streaming", "", [r"^max$", r"^hbo max"]),
    ("YouTube Premium", "streaming", "", [r"^youtube"]),
    ("Crunchyroll", "anime", "", [r"^crunchyroll", r"^funimation"]),
    # Broadcast
    ("NBC", "broadcast", "", [r"^nbc$", r"^nbc \("]),
    ("CBS", "broadcast", "", [r"^cbs$", r"^cbs \("]),
    ("ABC", "broadcast", "", [r"^abc$", r"^abc \(us\)", r"^american broadcasting"]),
    ("FOX", "broadcast", "", [r"^fox$", r"^fox \(us\)", r"^fox network", r"^fox broadcasting"]),
    ("The WB", "broadcast", "", [r"^the wb$", r"^wb$", r"^the wb television"]),
    ("UPN", "broadcast", "", [r"^upn$", r"^united paramount"]),
    ("The CW", "broadcast", "", [r"^the cw$", r"^cw$"]),
    ("PBS", "broadcast", "", [r"^pbs$", r"^pbs \("]),
    ("Syndication", "classic", "", [r"syndicat", r"^first[- ]run"]),
    ("DuMont", "classic", "", [r"^dumont"]),
    ("Telemundo", "broadcast", "", [r"^telemundo"]),
    ("Univision", "broadcast", "", [r"^univision"]),
    # International
    ("BBC", "international", "", [r"^bbc"]),
    ("ITV", "international", "", [r"^itv"]),
    ("E4", "international", "Channel 4", [r"^e4$"]),
    ("Channel 4", "international", "", [r"^channel 4"]),
    ("CBC", "international", "", [r"^cbc"]),
    ("Sky", "international", "", [r"^sky"]),
    ("Anime", "anime", "", [
        r"^tv tokyo", r"^fuji tv", r"^tokyo mx", r"^nippon tv", r"^ntv$", r"^mbs$", r"^at-x", r"^tv asahi",
        r"^wowow", r"^tbs \(japan\)", r"^animax", r"^bs11",
    ]),
]

_COMPILED = [(name, cat, parent, [re.compile(p) for p in pats]) for name, cat, parent, pats in FAMILIES]
PARENT = {name: parent for name, _, parent, _ in FAMILIES if parent}

# Movie studios only count toward these channels (a Paramount movie isn't "on" a network).
MOVIE_FAMILIES = {"Nickelodeon", "Cartoon Network", "Disney Channel", "MTV", "HBO", "Comedy Central"}

# Guide groups and where their channel numbers start, like a cable lineup.
CATEGORIES = {
    "broadcast": ("Broadcast Networks", 2),
    "kids": ("Kids", 20),
    "cable": ("Entertainment", 40),
    "adult": ("Entertainment", 40),
    "classic": ("Classic TV", 70),
    "factual": ("Documentary & Lifestyle", 80),
    "premium": ("Premium", 100),
    "streaming": ("Streaming Originals", 120),
    "international": ("International", 150),
    "anime": ("Anime", 170),
    "music": ("Music", 180),
    "other": ("Entertainment", 40),
}
GENRE_START, MOVIES_START, HOLIDAY_START = 200, 250, 300


def _clean(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().lower())
    n = re.sub(r"\s*\((us|usa|u\.s\.)\)$", "", n)
    return n.strip("[] ")


def family_for(name: str | None) -> tuple[str, str]:
    """Return (network name, category) for a network or studio name."""
    if not name or not name.strip():
        return "", "other"
    n = _clean(name)
    for fam, cat, _, pats in _COMPILED:
        if any(p.search(n) for p in pats):
            return fam, cat
    return name.strip(), "other"


def parent_of(family: str) -> str:
    return PARENT.get(family, "")


def movie_family(studio: str | None) -> str:
    fam, _ = family_for(studio)
    return fam if fam in MOVIE_FAMILIES else ""
