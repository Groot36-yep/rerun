"""When a promo makes sense to air, from what's said in it.

Network promos name a day and time: "this Friday at 8", "tonight", "all-new, Saturday morning". Played
on the wrong day they give the game away ("this Friday" on a Saturday, "tonight at 8" at 11pm), so a
promo that names one airs only in the days leading up to it, and on the day itself until its time:

  * "this Friday" (or "Friday at 8", "Friday night"): Monday to Thursday, and Friday until 8pm;
  * "tonight": until 8pm (or its time, "tonight at 7");
  * "today" / "this afternoon": until 5pm (or its time);
  * "this weekend": Monday to Friday.

One that names no day, says "Fridays" (every Friday), or a run of days ("Monday through Friday") airs
any time. The time is read as evening unless it says morning or am ("Saturday morning at 10"), since
that's when most promoted shows aired.
"""
from __future__ import annotations

import re
import time

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LEAD_DAYS = 4  # a promo for a day plays on the four days before it ("this Friday": Monday to Thursday)
EVENING = 20   # "tonight" with no time: until 8pm
AFTERNOON = 17

# "Fridays", and "Friday's" (how speech-to-text often writes it: "Cartoon Cartoon Friday's"), mean every week
_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)('?s)?\b", re.I)
_RUN = re.compile(r"\b(through|thru|until|every|weekdays?|weeknights?|each)\b", re.I)  # "all week until Friday"
_TONIGHT = re.compile(r"\btonight\b|\bthis evening\b", re.I)
_TODAY = re.compile(r"\btoday\b|\bthis afternoon\b", re.I)
_WEEKEND = re.compile(r"\bthis weekend\b", re.I)
_MORNING = re.compile(r"\bmorning\b|\ba\.?m\b", re.I)
_TIME = re.compile(r"\bat (\d{1,2})(?:[:.](\d\d))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?|\b(\d{1,2})(?::(\d\d))?\s*/\s*\d{1,2}\s*c\b"
                   r"|\b(\d{1,2})(?::(\d\d))?\s*(a\.?m\.?|p\.?m\.?)|\bat (noon)\b", re.I)


def _hour(text: str) -> float | None:
    """The time named in a promo, as an hour of the day (20.5 for "8:30"), or None."""
    m = _TIME.search(text or "")
    if not m:
        return None
    if m.group(9):
        return 12.0
    if m.group(1):  # "at 8", "at 7:30 pm"
        h, mins, suffix = m.group(1), m.group(2), m.group(3)
    elif m.group(4):  # "8/7c"
        h, mins, suffix = m.group(4), m.group(5), None
    else:  # "8pm"
        h, mins, suffix = m.group(6), m.group(7), m.group(8)
    if (suffix or "").lower().startswith("o"):
        suffix = None
    h = int(h)
    if not 1 <= h <= 12:
        return None
    near = text[max(0, m.start() - 30):m.end() + 20]  # "Saturday morning at 10", "at 10 in the morning"
    am = (suffix or "").lower().startswith("a") or (not suffix and _MORNING.search(near) and h >= 6)
    if h == 12:
        h = 0 if am else 12
    elif not am:
        h += 12
    return h + (int(mins) / 60 if mins else 0.0)


def rule(text: str) -> dict | None:
    """When a promo can air: {"day": 0-6 or None, "until": hour on that day, "weekend": bool}, or None
    for any time."""
    text = text or ""
    days = {d.lower() for d, plural in _DAY.findall(text) if not plural}
    plural = any(p for _d, p in _DAY.findall(text))
    at = _hour(text)
    if len(days) == 1 and not plural and not _RUN.search(_around(text, next(iter(days)))):
        day = DAYS.index(next(iter(days)))
        return {"day": day, "until": at if at is not None else (12.0 if re.search(
            rf"\b{DAYS[day]} morning\b", text, re.I) else EVENING), "weekend": False}
    if days or plural:
        return None  # every Friday, or a run of days: any day will do
    if _WEEKEND.search(text):
        return {"day": None, "until": None, "weekend": True}
    if _TONIGHT.search(text):
        return {"day": -1, "until": at if at is not None and at >= 17 else EVENING, "weekend": False}
    if _TODAY.search(text):
        return {"day": -1, "until": at if at is not None else AFTERNOON, "weekend": False}
    return None


def _around(text: str, day: str) -> str:
    """The words either side of a day's name ("Monday through Friday" names a run, not one day)."""
    m = re.search(rf"(\S+\s+){{0,2}}\b{day}\b(\s+\S+){{0,2}}", text, re.I)
    return re.sub(rf"\b{day}\b", " ", m.group(0), flags=re.I) if m else ""


def fits(r: dict | None, t: float) -> bool:
    """Whether a promo with rule `r` makes sense at time `t` (local time)."""
    if not r:
        return True
    lt = time.localtime(t)
    hour = lt.tm_hour + lt.tm_min / 60
    if r.get("weekend"):
        return lt.tm_wday <= 4
    if r["day"] == -1:  # tonight / today
        return 5 <= hour < r["until"]
    ahead = (r["day"] - lt.tm_wday) % 7
    if ahead == 0:
        return 5 <= hour < r["until"]
    return ahead <= LEAD_DAYS


def describe(r: dict | None) -> str:
    """The rule in words, for the commercial's page: "plays Mon–Thu, and Fri until 8pm"."""
    if not r:
        return ""
    if r.get("weekend"):
        return "plays Monday to Friday (it says “this weekend”)"

    def clock(h: float) -> str:
        hh, mm = int(h), int(round((h % 1) * 60))
        if hh == 12 and not mm:
            return "noon"
        suffix = "am" if hh < 12 else "pm"
        hh = hh % 12 or 12
        return f"{hh}{':%02d' % mm if mm else ''}{suffix}"
    if r["day"] == -1:
        return f"plays until {clock(r['until'])} each day (it says “tonight” or “today”)"
    first = (r["day"] - LEAD_DAYS) % 7
    return (f"plays {SHORT[first]}–{SHORT[(r['day'] - 1) % 7]}, and {SHORT[r['day']]} until "
            f"{clock(r['until'])} (it says “{DAYS[r['day']].title()}”)")
