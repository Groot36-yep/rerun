"""What appears over a show besides its logo: when, and what it says.

- "Up next": near the end of the last part before the closing break (in the network's style).
- "After the break": the last seconds of a part going into a mid-show break, naming what's coming
  (more of the same show, or the next episode when two share a half hour).
- A promo lower third: early in a block's first part, "Tonight at 8: Jimmy Neutron: Boy Genius", for a
  movie or a big show coming up later on the same channel, the way networks pushed their big nights.

All times here are absolute (seconds since the epoch). Live TV, the preview and the program log use
the same plan, so what the log says is what plays.
"""
from __future__ import annotations

import time

UP_NEXT = (45.0, 15.0)        # from 45 to 15 seconds before the part ends
TEASER = (13.0, 3.0)          # the last seconds before a mid-show break
PROMO_LEN = 9.0
PROMO_AHEAD = 5 * 3600         # promote things up to five hours away
MIN_PART = 60.0                # parts shorter than this get no promo or teaser


def _clock(t: float) -> str:
    lt = time.localtime(t)
    h = lt.tm_hour % 12 or 12
    return f"{h}" if lt.tm_min == 0 else f"{h}:{lt.tm_min:02d}"


def when_label(target: float, now: float, new: bool = False) -> tuple[str, str]:
    """("Tonight at 8", "at 8") for a time later today, or tomorrow; ("All-new at 10", ...) for one of
    Saturday morning's new episodes."""
    a, b = time.localtime(now), time.localtime(target)
    c = _clock(target)
    if new:
        return f"All-new at {c}", f"at {c}"
    if (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday):
        return (f"Tonight at {c}" if b.tm_hour >= 17 else f"Today at {c}"), f"at {c}"
    return f"Tomorrow at {c}", f"at {c}"


def _promo_target(p, block: dict, start: float) -> dict | None:
    """Something worth promoting later on this channel: a movie first, then whatever starts at 8pm,
    then a show starting on the hour. Never the very next block (that's what "Up next" is for)."""
    later = [b for b in p.blocks if b["s"] > block["e"] + 1 and b["s"] <= start + PROMO_AHEAD
             and b.get("title") and b["title"] != block.get("title") and (b.get("cats") or b.get("img"))]
    if not later:
        return None
    for test in (lambda b: b.get("movie"),
                 lambda b: time.localtime(b["s"]).tm_hour == 20 and time.localtime(b["s"]).tm_min == 0,
                 lambda b: time.localtime(b["s"]).tm_min == 0 and b["s"] - start > 3600):
        hit = next((b for b in later if test(b)), None)
        if hit:
            return hit
    return None


def plan(p, idx: int, item: dict, block: dict, s: dict, show_title: str = "") -> list[dict]:
    """What goes over this part of a show: [{kind, t0, t1, title, label, short, block}]."""
    if item.get("k") != "c" or not block:
        return []
    out: list[dict] = []
    in_block = [x for x in p.items[idx + 1:] if x["s"] < block["e"]]
    later = next((x for x in in_block if x["k"] == "c"), None)
    length = item["e"] - item["s"]
    if s.get("up_next", True) and not later:
        nb = p.block_at(block["e"] + 1)
        if nb and nb.get("title") and (nb.get("cats") or nb.get("img")):
            new = bool(nb.get("new"))  # Saturday morning: "All-new, up next"
            out.append({"kind": "up_next", "t0": item["e"] - UP_NEXT[0], "t1": item["e"] - UP_NEXT[1],
                        "title": nb["title"], "label": "All-new, up next" if new else "",
                        "short": "new!" if new else "", "block": nb})
    if not s.get("promos", True) or length < MIN_PART:
        return out
    if later and in_block and in_block[0]["k"] != "c":  # a break comes after this part
        name = show_title or block.get("title") or ""
        if later.get("m") != item.get("m") and later.get("t") and not block.get("movie"):
            title, label, short = later["t"], "Coming up", "next"
        else:
            title, label, short = name or "Stay tuned", "More after the break", "more"
        out.append({"kind": "teaser", "t0": item["e"] - TEASER[0], "t1": item["e"] - TEASER[1],
                    "title": title, "label": label, "short": short, "block": None})
    first = next((x for x in p.items if x["k"] == "c" and block["s"] <= x["s"] < block["e"]), None)
    if first is item:
        target = _promo_target(p, block, item["s"])
        if target:
            t0 = item["s"] + max(10.0, min(length * 0.3, 120.0))
            t1 = t0 + PROMO_LEN
            busy = [(o["t0"], o["t1"]) for o in out]
            if t1 < item["e"] - 5 and all(t1 + 3 < a or t0 > b + 3 for a, b in busy):
                label, short = when_label(target["s"], item["s"], bool(target.get("new")))
                out.append({"kind": "promo", "t0": t0, "t1": t1, "title": target["title"], "label": label,
                            "short": short, "block": target})
    return out
