"""Listening to the commercials, so Rerun knows what each one is.

Most commercials are numbered clips cut from recorded breaks ("ABC Family Commercial Breaks (December
12, 2008) (56)"), so their names say nothing about what's in them. Each one is heard once, with an
offline speech-to-text model (faster-whisper, "base.en"; nothing leaves the server), and from what's
said Rerun learns:

  * what kind of commercial it is: cereal, toy, fast food, a snack or drink, a movie or DVD, a video
    game, music, a public service announcement, a store, a car, a service, or a network promo;
  * which shows it mentions, and how: "you're watching iCarly" (a bumper for that show), "up next,
    SpongeBob" (just before it), or a promo ("all-new Big Time Rush, Friday at 8").

The scheduler uses that to mix the kinds within a break and from break to break, and to play promos
and bumpers only where they make sense (see scheduler.Builder._fits_show).

It runs only while nobody is watching, so it can't slow live TV down, and each clip is heard once.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import subprocess
import time

from .store import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger("rerun.listen")

MODEL = "base.en"
HEARD = DATA_DIR / "commercials_heard.json"  # {clip id: {"k": what was heard, "text", "cat", "shows", "role"}}
MODELS = DATA_DIR / "models"
V = 2  # 2: the first version filed every clip it failed to hear as "Other"; those are heard again
MAX_SECONDS = 75  # a commercial longer than this is heard only for its first 75 seconds

# What kind of commercial: the kind with the most matches wins (earlier kinds win ties).
KINDS = [
    ("cereal", "Cereal", r"\bcereal\b|part of (this|a|your) (good|complete|balanced|nutritious) breakfast|lucky charms|"
     r"\btrix\b|cocoa puffs|froot loops|frosted flakes|cinnamon toast crunch|cap'?n crunch|cheerios|reese'?s puffs|"
     r"honeycomb|apple jacks|cookie crisp|golden grahams|fruity pebbles|cocoa pebbles|count chocula|\bkix\b|"
     r"corn pops|rice krispies|frosted mini|wheaties"),
    ("toy", "Toys", r"\btoys?\b|\bdolls?\b|action figures?|playset|barbie|hot wheels|\blego\b|bratz|polly pocket|"
     r"beyblade|bakugan|\bnerf\b|play-?doh|hasbro|mattel|sold separately|batteries (are )?(not )?included|"
     r"collect them all|easy-?bake|furby|transformers|my little pony|littlest pet shop|zhu zhu|tamagotchi|"
     r"yu-?gi-?oh|trading cards|\bfisher-?price\b|\bvtech\b|leapfrog|crayola|\bkit\b.*\bages\b|\btyco\b|"
     r"\bR\.?C\.?\b|remote[- ]control|milton bradley|parker brothers|board game|\bgame from\b|"
     r"ages \d+ (and|&) up|each sold separately|\bvehicles?\b|\bplay ?set\b|\bcollect\b|\bmarbles\b"),
    ("fastfood", "Fast food", r"mcdonald'?s|happy meal|burger king|wendy'?s|\bkfc\b|kentucky fried|taco bell|"
     r"pizza hut|domino'?s|\bsubway\b|chuck e\.? cheese|\bsonic\b|dairy queen|\barby'?s\b|papa john'?s|"
     r"little caesars|chick-fil-a|kids meal"),
    ("food", "Snacks & drinks", r"\bcandy\b|chocolate|\bgum\b|\bchips\b|cookies|fruit snacks|gushers|fruit by the foot|"
     r"lunchables|go-?gurt|yogurt|pop-?tarts|\bsoda\b|\bjuice\b|capri sun|kool-?aid|sunny ?d|dunkaroos|oreo|doritos|"
     r"cheetos|skittles|m&m|snickers|ice cream|popsicle|macaroni|\bkraft\b|pizza rolls|bagel bites|hot pockets|"
     r"\bsnacks?\b|delicious|\btasty\b|\bflavou?rs?\b|appetizers?|savory|\brecipe\b|flaky|pillsbury|\bbak(e|ing)\b|"
     r"\bdinner\b"),
    ("movie", "Movies & DVDs", r"in theaters|at a theater near you|rated (g|pg|pg-13|r)\b|\bpg(-13)?\b|now playing|coming soon|"
     r"\bfilm\b|"
     r"only in theaters|\bon dvd\b|blu-?ray|\bdvd\b|\bvhs\b|own it (today|now)|the motion picture|this (summer|holiday "
     r"season)|in 3-?d\b|\bimax\b"),
    ("game", "Video games", r"video games?|playstation|\bps[1-4]\b|\bxbox\b|nintendo|game ?boy|gamecube|\bwii\b|"
     r"\bds\b|rated e( for everyone|10)|\bsega\b|\bpsp\b"),
    ("music", "Music", r"\balbum\b|\bcds?\b|in stores (now|everywhere)|soundtrack|now that'?s what i call music|"
     r"\bitunes\b|ringtones?|on sale now"),
    ("psa", "Public service", r"the more you know|ad council|public service|don'?t (do drugs|smoke|drink)|say no to|"
     r"\bbully(ing)?\b|recycl|get active|let'?s move|seat ?belt|helmet|truth\.com|above the influence|"
     r"make a difference|be safe|talk to (a|your) (grown-?up|parent)|\bpbs\b.*\bsupport"),
    ("store", "Stores", r"walmart|wal-mart|\btarget\b|toys ?\"?r\"? ?us|\bkmart\b|\bsears\b|best buy|old navy|"
     r"jcpenney|\bmacy'?s\b|\bkohl'?s\b|\bmall\b|sale ends|save up to|percent off|% off|everyday low prices|"
     r"hallmark|gold crown"),
    ("home", "Home & cleaning", r"clorox|\btide\b|swiffer|febreze|bounty|charmin|detergent|laundry|\bcleaner\b|toilet|"
     r"\bstains?\b|dishwasher|vacuum|\bglade\b|lysol|mr\.? clean|scrubbing bubbles|paper towels?"),
    ("car", "Cars", r"\bcars?\b|\btrucks?\b|\bsuv\b|dealership|toyota|honda|\bford\b|chevy|chevrolet|nissan|hyundai|"
     r"\bkia\b|\bmpg\b|financing|\blease\b|test drive"),
    ("service", "Phones & services", r"insurance|\bbank\b|credit card|wireless|verizon|at&t|sprint|t-mobile|"
     r"\bcable\b|internet|lawyer|attorney|1-800|call now|operators are standing by|pharmacy|ask your doctor|"
     r"side effects|prescription|mastercard|\bvisa\b|priceless|alltel|unlimited calling|calling plan|"
     r"text messag|cell ?phones?|\bups\b|fedex|shipping"),
]
_KINDS = [(k, label, re.compile(rx, re.I)) for k, label, rx in KINDS]
KIND_LABELS = {k: label for k, label, _ in KINDS} | {"promo": "Network promos", "bumper": "Bumpers", "other": "Other"}

_WATCHING = re.compile(r"you'?re watching|you are watching|(we'?ll|we will) be right back|be right back|"
                       r"now back to|back to (the show|more)|and now,? back", re.I)
_NEXT = re.compile(r"\bup next\b|coming up next|\bnext on\b|stay tuned for|\bis next\b|coming up (after|right)|"
                   r"\bstarts? (now|next)\b", re.I)
_PROMO = re.compile(r"all[- ]new|new episodes?|brand[- ]new|premiere|\btonight\b|this (friday|saturday|sunday|monday|"
                    r"tuesday|wednesday|thursday|weekend)|\bweek(nights|days)\b|\bmarathon\b|only on|right here on|"
                    r"\bat [1-9]( ?(am|pm|:\d\d))?\b|\b[1-9] ?/ ?[1-9] ?c\b|\bspecial\b|\bmovie event\b", re.I)
# a network talking about its own schedule ("Monday at 8, only on Cartoon Network")
_NETWORK = re.compile(r"only on|right here on|\bon (nickelodeon|nick|nicktoons|nick jr|nick at nite|cartoon network|"
                      r"disney channel|disney|toon disney|playhouse disney|abc family|boomerang|the n|fox kids|"
                      r"kids'? ?wb|teen nick|disney xd|pbs kids)\b|\bnick at nite\b|\bteam nick\b", re.I)
_TAPE = re.compile(r"\(\d+\)\s*$")  # "ABC Family Commercial Breaks (December 12, 2008) (56)": a clip of a tape


def norm(name: str) -> str:
    """A show name the way it's matched: lower case, "&" as "and", no punctuation, no leading "the"."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[4:] if s.startswith("the ") else s


def known_shows(catalog=None) -> dict[str, str]:
    """{matched name: the show's own title}: every show in your library, Rerun's classic network
    lineups and its "shows to get" lists, with their other names."""
    out: dict[str, str] = {}
    try:
        from .lineups import LINEUPS
        for lu in LINEUPS.values():
            for t, _y, _tier, alt, *_ in lu["shows"]:
                for n in (t, *alt):
                    out.setdefault(norm(n), t)
    except Exception:
        pass
    try:
        from .recommend import LISTS
        for _label, by_decade in LISTS.values():
            for shows in by_decade.values():
                for t, _y, _tier, alt in shows:
                    for n in (t, *alt):
                        out.setdefault(norm(n), t)
    except Exception:
        pass
    for s in (getattr(catalog, "shows", None) or {}).values():
        if s.get("title"):
            out[norm(s["title"])] = s["title"]
    # names too short or too ordinary to find safely in what people say
    return {k: v for k, v in out.items() if len(k) >= 4}


_NAME_INDEX: dict = {}


def _index(names: dict[str, str]) -> dict:
    """The names, without their spaces, grouped by first letter: for quick close-enough matching."""
    key = (id(names), len(names))
    if _NAME_INDEX.get("key") != key:
        by: dict[str, list] = {}
        for n, t in names.items():
            c = n.replace(" ", "")
            by.setdefault(c[:1], []).append((c, n, t))
        _NAME_INDEX.clear()
        _NAME_INDEX.update(key=key, by=by)
    return _NAME_INDEX["by"]


def _cue(sentence: str) -> bool:
    """Whether a sentence is talking about a show ("you're watching", "up next", "new episodes",
    "only on Nickelodeon")."""
    return bool(_WATCHING.search(sentence) or _NEXT.search(sentence) or _PROMO.search(sentence)
                or _NETWORK.search(sentence))


def find_shows(said: str, names: dict[str, str]) -> list[str]:
    """The shows named in what was said. Speech-to-text mishears names ("Fairly Odd Paris"), so a run of
    words close enough to a long name counts too. A short or everyday-sounding name ("Doug", "All That")
    only counts in a sentence that's clearly about a show, so "...all that and more" isn't All That."""
    found: list[str] = []
    by = _index(names)
    for sentence in re.split(r"(?<=[.!?])\s+", said):
        words = norm(sentence).split()
        cue = _cue(sentence)
        for i in range(len(words)):
            for k in range(1, 6):
                if i + k > len(words):
                    break
                gram = "".join(words[i:i + k])
                for c, n, t in by.get(gram[:1], ()):
                    if t in found or ((" " not in n or len(c) < 10) and not cue):
                        continue
                    if gram == c or (len(c) >= 9 and 0.8 * len(c) <= len(gram) <= 1.25 * len(c)
                                     and difflib.SequenceMatcher(None, gram, c).ratio() >= 0.85):
                        found.append(t)
    return found


def analyse(text: str, title: str, names: dict[str, str]) -> dict:
    """What a commercial is, from what's said in it, and its name unless that's just a tape's name with
    a clip number ("...VHS Recording (3)" says nothing about clip 3): {"cat", "shows", "role"}."""
    said = f"{'' if _TAPE.search(title or '') else title or ''} . {text or ''}"
    low = said.lower()
    promo_cue = bool(_PROMO.search(low))
    shows = find_shows(said, names)
    shows = [t for t in shows if not any(t != o and norm(t) in norm(o) for o in shows)]  # "Doug" in "Brand Spanking New! Doug"
    role = ("watching" if shows and _WATCHING.search(low) else "next" if shows and _NEXT.search(low)
            else "promo" if shows else "")
    scores = {k: len(rx.findall(low)) for k, _label, rx in _KINDS}
    best = max(scores, key=lambda k: (scores[k], -[x[0] for x in KINDS].index(k)))
    cat = best if scores[best] else ("promo" if shows or promo_cue else "other")
    if shows and role in ("watching", "next"):
        cat = "promo"
    elif _NETWORK.search(low) and (promo_cue or shows) and cat in ("movie", "music", "other", "promo"):
        cat = "promo"  # "Holiday Special, Monday at 8, only on Cartoon Network": the network's own, not a movie ad
    return {"cat": cat, "shows": shows, "role": role}


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


class Listener:
    def __init__(self, app):
        self.app = app
        self.heard: dict[str, dict] = read_json(HEARD, {})
        self.progress = {"running": False, "done": 0, "total": 0, "step": "", "error": "", "paused": False,
                         "finished": 0}
        self.task: asyncio.Task | None = None
        self._model = None
        self.stop = False

    @staticmethod
    def _key(f: dict) -> str:
        return f"{V}:{f.get('file')}:{f.get('in', 0)}:{round(float(f.get('dur') or 0), 1)}"

    def clips(self) -> list[dict]:
        return self.app.catalog.all_filler()

    def todo(self) -> list[dict]:
        return [f for f in self.clips() if (self.heard.get(f["id"]) or {}).get("k") != self._key(f)]

    def summary(self) -> dict:
        clips = self.clips()
        kinds: dict[str, int] = {}
        for f in clips:
            rec = self.heard.get(f["id"])
            if rec:
                k = "bumper" if "bumper" in (f.get("tags") or []) else rec.get("cat", "other")
                kinds[k] = kinds.get(k, 0) + 1
        return {"available": available(), "heard": sum(1 for f in clips if f["id"] in self.heard), "total": len(clips),
                "kinds": [{"key": k, "label": KIND_LABELS.get(k, k), "count": n}
                          for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])],
                "progress": self.progress}

    def start(self) -> bool:
        if self.progress["running"]:
            return False
        if not available():
            raise RuntimeError("The listening part isn't installed. Run 'rerun update' to add it.")
        self.stop = False
        self.progress.update(running=True, done=0, total=0, step="Starting", error="", paused=False)
        self.task = asyncio.create_task(self._run())
        return True

    def _watching(self) -> bool:
        streams = getattr(self.app, "streams", None)
        return bool(streams and any(not getattr(s, "done", False) for s in streams.by_channel.values()))

    def _load(self):
        if self._model is None:
            # The service can only write to its own data folder (your home folder is read-only to it), and
            # the model's downloader keeps a cache in ~/.cache unless told otherwise: there, it failed.
            MODELS.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(MODELS / "hf")
            os.environ["XDG_CACHE_HOME"] = str(MODELS / "cache")
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            from faster_whisper import WhisperModel
            # two threads, so the rest of the server keeps its share even while this runs
            self._model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=2,
                                       download_root=str(MODELS))
        return self._model

    def _hear(self, f: dict) -> str:
        """What's said in one clip."""
        import numpy as np
        src = self.app.media_input(f)
        if not src:
            return ""
        dur = min(float(f.get("dur") or MAX_SECONDS), MAX_SECONDS)
        cmd = [self.app.ffmpeg.ffmpeg, "-nostdin", "-loglevel", "error", "-ss", f"{float(f.get('in') or 0):.3f}",
               "-t", f"{dur:.3f}", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
        raw = subprocess.run(cmd, capture_output=True, timeout=120).stdout
        if len(raw) < 16000:  # under half a second of sound
            return ""
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._load().transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                                  condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()

    async def _run(self):
        try:
            todo = self.todo()
            self.progress["total"] = len(todo)
            names = known_shows(self.app.catalog)
            last_save = time.time()
            failed, heard_any = 0, False
            if todo:
                self.progress["step"] = "Getting the listening model ready (the first time it's downloaded, about 145 MB)"
                try:
                    await asyncio.to_thread(self._load)
                except Exception as e:
                    log.exception("Couldn't load the listening model")
                    self.progress["error"] = f"Couldn't get the listening model ready: {e.__class__.__name__}: {e}"[:300]
                    return
            for i, f in enumerate(todo):
                if self.stop:
                    break
                while self._watching() and not self.stop:  # never while anyone's watching
                    self.progress.update(paused=True, step="Waiting until nobody's watching")
                    await asyncio.sleep(30)
                self.progress.update(paused=False, done=i, step=f.get("title") or "")
                try:
                    text = await asyncio.to_thread(self._hear, f)
                except Exception as e:  # one bad clip doesn't stop the rest, and isn't filed as heard
                    failed += 1
                    log.warning("Couldn't listen to %s (%s: %s)", f.get("title"), e.__class__.__name__, e)
                    if failed >= 5 and not heard_any:  # it's not the clips, it's the listening: stop and say why
                        self.progress["error"] = f"Listening isn't working: {e.__class__.__name__}: {e}"[:300]
                        break
                    continue
                heard_any = True
                self.heard[f["id"]] = {"k": self._key(f), "text": text[:600], **analyse(text, f.get("title", ""), names)}
                if time.time() - last_save > 20:
                    await asyncio.to_thread(atomic_write_json, HEARD, self.heard)
                    last_save = time.time()
            # forget clips that aren't in the library any more
            ids = {f["id"] for f in self.clips()}
            self.heard = {k: v for k, v in self.heard.items() if k in ids}
            await asyncio.to_thread(atomic_write_json, HEARD, self.heard)
            if not self.progress["error"]:
                self.progress["done"] = self.progress["total"]
            log.info("Listened to %s commercials (%s couldn't be heard)", self.progress["total"] - failed, failed)
            # Breaks planned from now on use what was heard; the ones already planned (and in the guide) stay.
        except Exception as e:
            log.exception("Listening to the commercials failed")
            self.progress["error"] = f"{e.__class__.__name__}: {e}"
        finally:
            self._model = None  # give the memory back
            self.progress.update(running=False, paused=False, step="", finished=time.time())


def reanalyse(heard: dict, catalog) -> dict:
    """Re-read what was already heard with the current lists of shows (a new show in the library)."""
    names = known_shows(catalog)
    titles = {f["id"]: f.get("title", "") for f in catalog.all_filler()}
    return {k: {**v, **analyse(v.get("text", ""), titles.get(k, ""), names)} for k, v in heard.items()}


def dump(path: str):  # for checking by hand: python -m app.listen <file>
    print(json.dumps(read_json(path, {}), indent=1)[:4000])
