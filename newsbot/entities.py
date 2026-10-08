"""Entity and topic extraction (rule-based, zero API cost).

Used before any model call to cluster stories, deduplicate and rank. The fact
extraction model later returns canonical names, which are learned into the
gazetteer so future runs match them without heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Entity
from .textutil import collapse_ws, entity_key, fold_latin

# Aliases that are ordinary English words; only matched when written capitalized.
AMBIGUOUS_ALIASES = {
    "switch", "wow", "lol", "ff", "cod", "dice", "mac", "destiny", "persona", "gears", "control",
    "steam", "mario", "halo", "forza", "fallout", "diablo", "windows", "gta", "ea", "2k", "egs",
    "series x", "quest 3", "android", "linux",
}

HEADLINE_MARKERS = {
    # prepositions that separate names
    "for", "in", "on", "to", "with", "from", "at", "after", "before", "as", "by", "via", "about",
    "into", "over", "vs", "versus", "than", "during", "amid", "despite", "without", "like",
    # verbs and generic nouns
    "is", "are", "was", "were", "will", "would", "could", "can", "may", "might", "should", "has",
    "have", "had", "gets", "get", "getting", "got", "adds", "add", "added", "receives", "receive",
    "received", "launches", "launch", "launched", "launching", "release", "releases", "released",
    "releasing", "reveals", "reveal", "revealed", "announces", "announce", "announced", "shows",
    "show", "showcases", "showcased", "delayed", "delay", "delays", "coming", "comes", "come", "hits",
    "hit", "arrives", "arrive", "arriving", "out", "now", "available", "update", "updates", "patch",
    "patches", "trailer", "trailers", "teaser", "review", "reviews", "preview", "previews", "dlc",
    "expansion", "gameplay", "beta", "demo", "sales", "sold", "sells", "players", "player", "director",
    "producer", "ceo", "confirms", "confirmed", "teases", "teased", "leaks", "leak", "leaked", "rumor",
    "rumored", "rumour", "rumoured", "reportedly", "finally", "officially", "says", "said", "explains",
    "details", "detailed", "introduces", "unveils", "unveiled", "celebrates", "drops", "dropped",
    "brings", "bring", "lets", "makes", "made", "takes", "gives", "offers", "features", "includes",
    "including", "price", "priced", "costs", "cost", "free", "discount", "deal", "deals", "sale",
    "off", "mode", "map", "maps", "character", "characters", "boss", "bosses", "studio",
    "developer", "developers", "publisher", "team", "fans", "gamers", "report", "reports",
    "according", "rating", "rated", "roadmap", "date", "dates", "time", "times", "specs",
    "requirements", "performance", "benchmark", "benchmarks", "settings", "guide", "tips", "how",
    "why", "what", "when", "where", "who", "here", "heres", "here's", "this", "that", "these",
    "those", "it", "its", "it's", "you", "your", "we", "our", "i", "my", "new", "first", "look",
    "looks", "isn't", "won't", "doesn't", "don't", "can't", "set", "sets", "goes", "go", "returns",
    "return", "back", "still", "already", "again", "soon", "next", "week", "month", "year", "today",
    "tomorrow", "official", "big", "huge", "massive", "major", "minor", "latest", "upcoming",
    "planned", "plans", "wants", "needs", "admits", "hints", "hint", "shares", "share",
    "season", "seasons", "chapter", "episode", "episodes", "port", "ports", "unofficial", "fan",
    "mod", "mods", "native", "remaster", "remastered",
}

PREPOSITIONS = {"for", "in", "on", "to", "with", "from", "at", "about", "into", "over"}

_DATE_LIKE = re.compile(r"^(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|q[1-4]|20\d\d|"
                        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")

# Kinds that describe where/when something happened rather than what it is about.
NON_TOPIC_KINDS = {"platform", "event"}


@dataclass
class _Entry:
    display: str
    kind: str


class Gazetteer:
    def __init__(self, cfg: dict | None = None, learned: dict[str, str] | None = None):
        cfg = cfg or {}
        self.index: dict[str, _Entry] = {}
        self.ambiguous: set[str] = set(AMBIGUOUS_ALIASES)
        self.max_tokens = 1
        for name, aliases in (cfg.get("platforms") or {}).items():
            self.add(name, "platform", aliases or [])
        for name in cfg.get("companies") or []:
            self.add(name, "company", [])
        for name, aliases in (cfg.get("franchises") or {}).items():
            self.add(name, "franchise", aliases or [])
        for name, aliases in (cfg.get("games") or {}).items():
            self.add(name, "game", aliases or [])
        for name, aliases in (cfg.get("events") or {}).items():
            self.add(name, "event", aliases or [])
        for name, kind in (learned or {}).items():
            self.add(name, kind or "game", [], overwrite=False)
        self.hardware_res = [re.compile(p, re.I) for p in (cfg.get("hardware_patterns") or [])]
        self.generic = {entity_key(g) for g in (cfg.get("generic_phrases") or [])}

    def add(self, name: str, kind: str, aliases: list[str], overwrite: bool = True) -> None:
        name = collapse_ws(str(name))
        if not name:
            return
        entry = _Entry(display=name, kind=kind)
        for label in [name, *[str(a) for a in aliases]]:
            key = entity_key(label)
            if not key:
                continue
            if not overwrite and key in self.index:
                continue
            # A specific game beats a franchise/company sharing the same key.
            existing = self.index.get(key)
            if existing and existing.kind == "game" and kind != "game":
                continue
            self.index[key] = entry
            self.max_tokens = max(self.max_tokens, len(key.split()))

    def kind_of(self, key: str) -> str | None:
        entry = self.index.get(key)
        return entry.kind if entry else None

    def _capitalized_in(self, original: str, phrase_key: str) -> bool:
        words = phrase_key.split()
        pattern = r"\b" + r"[\s\-:'’]*".join(re.escape(w) for w in words) + r"\b"
        for match in re.finditer(pattern, fold_latin(original), flags=re.I):
            text = match.group(0)
            if text[:1].isupper() or text[:1].isdigit():
                return True
        return False

    def match(self, text: str, confidence: float = 1.0) -> list[Entity]:
        found = self._match(text, confidence)
        stripped = strip_possessives(text)
        if stripped != text:
            keys = {e.key for e in found}
            found.extend(e for e in self._match(stripped, confidence) if e.key not in keys)
        return found

    def _match(self, text: str, confidence: float) -> list[Entity]:
        tokens = entity_key(text).split()
        found: list[Entity] = []
        i = 0
        while i < len(tokens):
            matched = False
            for n in range(min(self.max_tokens, len(tokens) - i), 0, -1):
                phrase = " ".join(tokens[i : i + n])
                entry = self.index.get(phrase)
                if not entry:
                    continue
                if phrase in self.ambiguous and not self._capitalized_in(text, phrase):
                    continue
                found.append(Entity(key=entity_key(entry.display), display=entry.display,
                                    kind=entry.kind, confidence=confidence))
                i += n
                matched = True
                break
            if not matched:
                i += 1
        return found


def strip_possessives(text: str) -> str:
    """"Ghost of Yotei's co-op mode" -> "Ghost of Yotei co-op mode" (names keep inner apostrophes)."""
    return re.sub(r"(?<=\w)['’]s\b", "", text or "")


def _split_title_chunks(title: str) -> list[str]:
    title = re.sub(r"[“”\"]", '"', title or "")
    return [c.strip() for c in re.split(r"\s[-–—|]\s|[:?!,;()\[\]]", title) if c.strip()]


def headline_candidates(title: str, generic: set[str]) -> list[tuple[str, float]]:
    """Likely names in a Title Case headline: leading subject, quoted text, after prepositions."""
    out: list[tuple[str, float]] = []
    title = fold_latin(title or "")
    for quoted in re.findall(r'"([^"]{2,60})"|‘([^’]{2,60})’', title or ""):
        phrase = quoted[0] or quoted[1]
        if phrase:
            out.append((phrase.strip(), 0.8))

    for chunk_index, chunk in enumerate(_split_title_chunks(title)):
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.&+\-]*", chunk)
        segment: list[str] = []
        prev_marker = ""
        segment_index = 0

        def flush(chunk_index: int = chunk_index, prev_marker: str = prev_marker):
            nonlocal segment, segment_index
            seg = list(segment)
            segment = []
            while seg and entity_key(seg[0]) in generic:
                seg = seg[1:]
            if not seg or len(seg) > 6:
                segment_index += 1
                return
            phrase = " ".join(seg)
            key = entity_key(phrase)
            first = seg[0]
            if (not key or key in generic or re.fullmatch(r"[\d ]+", key) or len(key) < 3
                    or _DATE_LIKE.match(key)
                    or not (first[:1].isupper() or first[:1].isdigit())):
                segment_index += 1
                return
            if chunk_index == 0 and segment_index == 0:
                conf = 0.7
            elif prev_marker in PREPOSITIONS:
                conf = 0.55
            else:
                conf = 0.4
            out.append((phrase, conf))
            segment_index += 1

        for word in words:
            low = word.lower().strip("'’.")
            if low in HEADLINE_MARKERS:
                flush(chunk_index, prev_marker)
                prev_marker = low
            else:
                segment.append(word.strip("'’.") if word.endswith((".", "'")) else word)
        flush(chunk_index, prev_marker)
    return out


@dataclass
class EntityExtractor:
    gazetteer: Gazetteer

    def extract(self, title: str, summary: str = "") -> list[Entity]:
        entities: dict[str, Entity] = {}

        def put(entity: Entity):
            if not entity.key:
                return
            existing = entities.get(entity.key)
            if existing is None or entity.confidence > existing.confidence:
                entities[entity.key] = entity

        for entity in self.gazetteer.match(title, 1.0):
            put(entity)
        for entity in self.gazetteer.match((summary or "")[:500], 0.75):
            put(entity)
        text = f"{title} {(summary or '')[:500]}"
        for pattern in self.gazetteer.hardware_res:
            for match in pattern.finditer(text):
                display = collapse_ws(match.group(0))
                put(Entity(key=entity_key(display), display=display, kind="hardware", confidence=0.9))

        known = dict(entities)
        for phrase, conf in headline_candidates(title, self.gazetteer.generic):
            if re.search(r"\w['’]s\b", phrase) and self.gazetteer.kind_of(entity_key(strip_possessives(phrase))):
                continue
            key = entity_key(phrase)
            plain_key = entity_key(strip_possessives(phrase))
            if any(f" {k} " in f" {plain_key} " for k, e in known.items() if e.kind in ("game", "hardware")):
                continue  # "Ghost of Yotei's Legends Mode" is about a known game, not a new title
            if self.gazetteer.kind_of(key) or key in known:
                continue  # already matched via gazetteer
            padded = f" {key} "
            # A fragment of a known name ("Hollow Knight" of "Hollow Knight: Silksong").
            if any(f" {key} " in f" {k} " for k in known):
                continue
            contained = [e for k, e in known.items() if f" {k} " in padded]
            if contained:
                if any(e.kind in ("game", "hardware", "event") for e in contained):
                    continue  # "Nvidia GeForce RTX 5090" wraps known names; nothing new
                franchise = [e for e in contained if e.kind == "franchise"]
                if franchise and key.startswith(franchise[0].key):
                    # Franchise + subtitle/number = a specific game ("Battlefield 6").
                    put(Entity(key=key, display=phrase, kind="game", confidence=0.85))
                    continue
                if all(e.kind in ("company", "platform") for e in contained):
                    rest = padded
                    for e in contained:
                        rest = rest.replace(f" {e.key} ", " ")
                    if len(rest.split()) < 2:
                        continue  # "Ubisoft Forward", "PC Port": no new name
            if conf < 0.5 and len(key.split()) == 1:
                continue
            put(Entity(key=key, display=phrase, kind="unknown", confidence=conf))
        return sorted(entities.values(), key=lambda e: (-e.confidence, _position(title, e.display)))


def _position(title: str, display: str) -> int:
    idx = entity_key(title).find(entity_key(display))
    return idx if idx >= 0 else 999


def primary_entity(entities: list[Entity], title: str) -> Entity | None:
    rank = {"game": 0, "hardware": 1, "franchise": 2, "unknown": 3, "company": 4}
    candidates = [e for e in entities if e.kind not in NON_TOPIC_KINDS]
    if not candidates:
        return None
    return sorted(candidates, key=lambda e: (rank.get(e.kind, 5) if e.confidence >= 0.55 else 6,
                                             _position(title, e.display), -e.confidence))[0]


# ---------------------------------------------------------------------------
# Event / kind classification
# ---------------------------------------------------------------------------
_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

KIND_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("sponsored", re.compile(r"\b(sponsored|partner content|promoted|advertorial)\b", re.I)),
    ("guide", re.compile(r"\b(how to|guide|walkthrough|tips and tricks|tips for|where to (?:find|get|buy)|"
                         r"explained|codes?\b(?! of)|wordle|connections hints?|strands|answers? (?:for|today)|"
                         r"best (?:settings|builds?|loadouts?|weapons?|classes)|tier list|all locations?)\b", re.I)),
    ("deal", re.compile(r"(\bdeals?\b|% off|\bdiscount(?:ed)?\b|\blowest price\b|\bprice drop\b|\bcheapest\b|"
                        r"\bbargain\b|\bsave \$|\bfree to keep\b|\bprime day\b|\bblack friday\b|\bcyber monday\b|"
                        r"\bbundle (?:offer|deal)\b|\bcoupon\b)", re.I)),
    ("review", re.compile(r"(\breview\b(?! bomb)(?!s are in)|\breview-in-progress\b|\bhands[- ]on\b|"
                          r"\bpreview\b|\bimpressions\b|\bi played\b|\bwe played\b|\bi tried\b|\btested:)", re.I)),
    ("list", re.compile(r"(^\s*(?:the )?\d+\s+(?:best|games|things|reasons|ways|biggest|most)\b|\btop \d+\b|"
                        r"\branked\b|\bbest (?:\w+ )?games\b|\bgames like\b|\bevery\b.+\branked\b)", re.I)),
    ("opinion", re.compile(r"(^\s*my\b|\bi think\b|\bi love\b|\bi hate\b|\bi'm\b|\bi've\b|\bi was\b|\bwe need\b|"
                           r"\bopinion\b|\beditorial\b|\bunpopular\b|\bhot take\b|\bshould you\b|\bis it worth\b|"
                           r"\bmy favorite\b|\bmy favourite\b)", re.I)),
    ("other", re.compile(r"\b(quiz|poll|podcast|livestream|watch live|newsletter|crossword|giveaway)\b", re.I)),
]

RUMOR_RE = re.compile(r"\b(rumou?r(?:ed|s)?|leak(?:ed|s)?|reportedly|insider|datamine(?:d|rs)?|allegedly|"
                      r"supposedly|could be|may be|might be|hinted|spotted|listing suggests|according to sources)\b",
                      re.I)

EVENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("delay", re.compile(r"\b(delay(?:ed|s)?|postpone(?:d|s)?|pushed back|push(?:es)? back|slips? to)\b", re.I)),
    ("cancellation", re.compile(r"\b(cancel(?:led|ed|s)?|shut(?:s|ting)? down|shutdown|closure|"
                                r"clos(?:e|es|ing) (?:its )?(?:servers|studio)|end of service|delisted)\b", re.I)),
    ("dlc_expansion", re.compile(r"\b(dlc|expansion|season pass|add-?on|story pack)\b", re.I)),
    ("release_date", re.compile(r"\b(release date|launch date|release window|releases? (?:on|in) |"
                                r"launch(?:es)? (?:on|in) |coming .{0,30}?(?:on|in) (?:" + _MONTHS + r"|q[1-4]|20\d\d|"
                                r"early|late|spring|summer|fall|autumn|winter)|arriv(?:es|ing) (?:on|in) |"
                                r"out (?:on|in) " + _MONTHS + r"|dated for|release time|"
                                r"(?:launch(?:es|ing)?|releas(?:es|ing)|arriv(?:es|ing)|coming|out) (?:on )?" + _MONTHS
                                + r" \d|"
                                r"(?:confirms?|sets?|reveals?|announces?|gets?|has) (?:an? )?(?:\w+ )?(?:release|launch)|"
                                + _MONTHS + r" (?:\d{1,2}(?:st|nd|rd|th)?,? )?(?:20\d\d )?(?:release|launch))", re.I)),
    ("patch_update", re.compile(r"\b(patch(?:es)?|hotfix|update|updates|version \d|v\d+\.\d|patch notes|"
                                r"title update|season \d+|roadmap|nerf(?:s|ed)?|buff(?:s|ed)?)\b", re.I)),
    ("launch", re.compile(r"\b(out now|now available|available now|launch(?:es|ed)? today|has launched|is out|"
                          r"released today|launch trailer|goes live|available today|now live|officially launched|"
                          r"launches)\b", re.I)),
    ("price", re.compile(r"(\bprice (?:increase|hike|cut|drop)\b|\bpriced at\b|\bcosts? \$|\$\d+|"
                         r"\braises? (?:the )?prices?\b|\bprice\b)", re.I)),
    ("sales_numbers", re.compile(r"\b(sold|sales|million (?:copies|players|units)|player count|"
                                 r"concurrent players|peak (?:players|concurrent)|copies)\b", re.I)),
    ("business", re.compile(r"\b(layoffs?|laid off|lays? off|acquir(?:e|es|ed|ition)|merger|lawsuit|sue[sd]?|earnings|"
                            r"revenue|ceo|ipo|stock|investment|funding|union|strike|fined|antitrust)\b", re.I)),
    ("beta_playtest", re.compile(r"\b(beta|playtest|demo|early access|alpha|test (?:weekend|period))\b", re.I)),
    ("hardware_spec", re.compile(r"\b(specs?|benchmark(?:s|ed)?|performance|fps|system requirements|"
                                 r"pc requirements|ray tracing|dlss|fsr|driver)\b", re.I)),
    ("trailer", re.compile(r"\b(trailer|teaser|gameplay (?:video|reveal|footage|overview)|showcase(?:d)?|"
                           r"first look)\b", re.I)),
    ("port_platform", re.compile(r"\b(coming to (?:pc|ps5|switch|xbox|steam)|pc port|ported|now on (?:pc|ps5)|"
                                 r"heading to|launches on (?:pc|ps5|switch|xbox))\b", re.I)),
    ("content_update", re.compile(r"\b(adds?|added|new (?:modes?|maps?|characters?|content|levels?|missions?|"
                                  r"species|cars?|weapons?|heroes?|operators?))\b", re.I)),
    ("announcement", re.compile(r"\b(announc(?:e|es|ed|ement)|reveal(?:s|ed)?|unveil(?:s|ed)?|confirm(?:s|ed)?|"
                                r"in development|greenlit|teases?|officially)\b", re.I)),
    ("event_show", re.compile(r"\b(state of play|nintendo direct|(?:xbox|playstation) (?:games )?showcase|"
                              r"summer game fest|game awards|gamescom|tokyo game show|tgs|ces|computex|"
                              r"bitsummit|ubisoft forward|pc gaming show)\b", re.I)),
    ("esports", re.compile(r"\b(esports?|tournament|championship|world cup|major)\b", re.I)),
]

EVENT_TYPES = [name for name, _ in EVENT_PATTERNS] + ["other"]

# Events that describe the same reveal when they happen within a day or two.
COMPATIBLE_EVENTS = [
    {"announcement", "trailer", "event_show", "release_date", "port_platform"},
    {"launch", "release_date", "dlc_expansion"},
    {"patch_update", "dlc_expansion", "content_update"},
]

PERSIAN_EVENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("delay", re.compile(r"تاخیر|تأخیر|به تعویق")),
    ("cancellation", re.compile(r"لغو|تعطیل")),
    ("release_date", re.compile(r"تاریخ (?:انتشار|عرضه)|زمان (?:انتشار|عرضه)|منتشر (?:خواهد شد|می‌شود|میشود)|"
                                r"عرضه (?:خواهد شد|می‌شود|میشود)")),
    ("launch", re.compile(r"منتشر شد|عرضه شد|در دسترس قرار گرفت|هم‌اکنون|همینک")),
    ("dlc_expansion", re.compile(r"DLC|بسته (?:الحاقی|گسترش)|محتوای دانلودی")),
    ("patch_update", re.compile(r"آپدیت|به‌روزرسانی|بروزرسانی|به روزرسانی|پچ|وصله")),
    ("price", re.compile(r"قیمت")),
    ("sales_numbers", re.compile(r"فروش|میلیون نسخه|تعداد بازیکنان")),
    ("business", re.compile(r"اخراج|تعدیل|تصاحب|خرید استودیو|درآمد|شکایت")),
    ("beta_playtest", re.compile(r"بتا|دمو|نسخه آزمایشی|دسترسی زودهنگام")),
    ("trailer", re.compile(r"تریلر|تیزر|ویدیو|گیم‌پلی")),
    ("announcement", re.compile(r"معرفی|رونمایی|تایید|تأیید|اعلام")),
    ("hardware_spec", re.compile(r"سیستم (?:مورد نیاز|موردنیاز)|مشخصات|بنچمارک|عملکرد")),
]
PERSIAN_RUMOR_RE = re.compile(r"شایعه|لیک|فاش شد|ادعا")


def classify_kind(title: str, summary: str = "") -> str:
    title = title or ""
    for kind, pattern in KIND_PATTERNS:
        if pattern.search(title):
            return kind
    if RUMOR_RE.search(title):
        return "rumor"
    return "news"


def classify_event(title: str, summary: str = "") -> str:
    for name, pattern in EVENT_PATTERNS:
        if pattern.search(title or ""):
            return name
    for name, pattern in EVENT_PATTERNS:
        if pattern.search((summary or "")[:300]):
            return name
    return "other"


def classify_persian_event(title: str) -> str:
    for name, pattern in PERSIAN_EVENT_PATTERNS:
        if pattern.search(title or ""):
            return name
    return "other"


def events_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    if "other" in (a, b):
        return True
    return any(a in group and b in group for group in COMPATIBLE_EVENTS)


def latin_entity_keys(text: str, gazetteer: Gazetteer) -> set[str]:
    """Entity keys of English names embedded in a Persian title (old posts keep names in English)."""
    keys = {e.key for e in gazetteer.match(text) if e.kind not in NON_TOPIC_KINDS}
    for run in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’:&.\- ]{2,80}", text or ""):
        run = collapse_ws(run).strip(" -:")
        key = entity_key(run)
        if len(key) >= 3 and not key.isdigit() and gazetteer.kind_of(key) not in NON_TOPIC_KINDS:
            keys.add(key)
    return keys
