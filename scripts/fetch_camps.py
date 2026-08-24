#!/usr/bin/env python3
"""
HorseCamp Data Fetcher
Runs nightly via GitHub Actions.
Calls Recreation.gov (RIDB) and NPS APIs, validates the final public feed,
and writes camps.json served at horsecampfinder.com/camps.json for the mobile apps.

The final-feed safety checks live here so the pipeline does not need a separate
validation Python script.

Required GitHub Secrets:
  RIDB_API_KEY  — from ridb.recreation.gov/profile
  NPS_API_KEY   — from developer.nps.gov/signup
"""

import os, json, time, re, math, html, random, subprocess, requests
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

RIDB_KEY   = os.environ.get("RIDB_API_KEY", "")
NPS_KEY    = os.environ.get("NPS_API_KEY", "")

RIDB_BASE = "https://ridb.recreation.gov/api/v1"
NPS_BASE  = "https://developer.nps.gov/api/v1"

# Defensive pagination guards prevent a malformed or repeated RIDB page from
# creating an endless loop while preserving normal multi-page results.
RIDB_PAGE_SIZE = 50
RIDB_MAX_PAGES_PER_SEARCH = 100
RIDB_STATE_TIMEOUT_SECONDS = 180
RIDB_PROGRESS_EVERY_PAGES = 10

# Official U.S. Forest Service Recreation Opportunities service. This public
# ArcGIS layer is used by the USFS website, RIDB, and its Interactive Visitor Map.
USFS_RECREATION_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
    "EDW_RecreationOpportunities_01/MapServer/0/query"
)

# Official U.S. Census Bureau geographic lookup service. The Forest Service
# layer does not expose a state field, so this resolves each USFS coordinate to
# its actual postal abbreviation instead of publishing the incorrect "US" value.
US_CENSUS_COORDINATES_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"

STATE_FIPS_TO_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "78": "VI",
}

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
HORSEMOTEL_FEED_URL = "https://raw.githubusercontent.com/andetero/HorseMotel/main/horsemotel.json"
HORSEMOTEL_MIN_LISTINGS = 700
PRIVATE_CAMPS_FILE = DATA_DIR / "private_camps.json"
STATE_PARKS_DIR = DATA_DIR / "state_parks"
GEOCODE_CACHE_FILE = DATA_DIR / "geocode_cache.json"
USFS_WEBSITE_SUPPLEMENT_CACHE_FILE = DATA_DIR / "usfs_website_supplement.json"

STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
    "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
    "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]


# ── GEOCODE CACHE ──────────────────────────────────────────────────────
# Persistent key→[lat, lon] store in data/geocode_cache.json.
# Keys are query strings passed to Nominatim.
# Cache entries never expire — park coordinates don't move.

_geocode_cache: dict = {}
_geocode_stats = {"hits": 0, "misses": 0}
_usfs_state_stats = {"hits": 0, "misses": 0, "stored": 0, "unresolved": 0}


def load_geocode_cache():
    global _geocode_cache
    if GEOCODE_CACHE_FILE.exists():
        try:
            data = json.loads(GEOCODE_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _geocode_cache = data
                print(f"  Geocode cache loaded: {len(_geocode_cache)} entries")
            else:
                print("  WARNING: geocode_cache.json is not a JSON object — starting empty")
        except Exception as e:
            print(f"  WARNING: Could not load geocode cache ({e}) — starting empty")
    else:
        print("  Geocode cache not found — will create data/geocode_cache.json on first run")


def write_geocode_cache():
    if _geocode_stats["misses"] == 0 and _usfs_state_stats["stored"] == 0:
        return  # nothing new to write
    try:
        GEOCODE_CACHE_FILE.write_text(
            json.dumps(dict(sorted(_geocode_cache.items())), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  Geocode cache saved: {len(_geocode_cache)} entries → data/geocode_cache.json")
    except Exception as e:
        print(f"  WARNING: Could not write geocode cache: {e}")


def _cache_lookup(key):
    """Return (lat, lon) from cache or None on miss."""
    entry = _geocode_cache.get(key)
    if entry and isinstance(entry, list) and len(entry) == 2:
        _geocode_stats["hits"] += 1
        return float(entry[0]), float(entry[1])
    return None


def _cache_store(key, lat, lon):
    """Store a result. Only saves valid coordinates; always increments miss counter."""
    if abs(lat) > 0.1 or abs(lon) > 0.1:
        _geocode_cache[key] = [lat, lon]
    _geocode_stats["misses"] += 1


def load_manual_state_parks(state_code):
    """Load manually curated state-park listings from data/state_parks/<state>.json."""
    path = STATE_PARKS_DIR / f"{state_code.lower()}.json"
    if not path.exists():
        raise RuntimeError(f"Manual state parks file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {path}: {e}") from e

    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a top-level JSON array")

    required_fields = {"id", "name", "location", "state", "latitude", "longitude"}
    for i, camp in enumerate(data):
        if not isinstance(camp, dict):
            raise RuntimeError(f"{path} entry #{i+1} must be an object")
        missing = sorted(required_fields - set(camp.keys()))
        if missing:
            raise RuntimeError(f"{path} entry #{i+1} is missing required fields: {', '.join(missing)}")
        if camp.get("state") != state_code:
            raise RuntimeError(
                f"{path} entry #{i+1} has state={camp.get('state')!r}; expected {state_code!r}"
            )

    print(f"  Loaded {len(data)} manual {state_code} state-park listings from {path.relative_to(REPO_ROOT)}")
    return data

EQUESTRIAN_KEYWORDS = [
    "horse", "equestrian", "corral", "stall", "horseback",
    "highline", "high line", "tie rail", "paddock", "horse camp",
    "horse trail", "pack station", "mule", "llama"
]

INVALID_EQUESTRIAN_PATTERNS = [
    re.compile(r"\bhas no equestrian sites\b", re.I),
    re.compile(r"\bhorses are not allowed in (?:the )?campground\b", re.I),
    re.compile(r"\bhorses are not allowed in campgrounds\b", re.I),
    re.compile(r"\bno horses allowed in (?:the )?campground\b", re.I),
    re.compile(r"\bhorse corrals \(no horses allowed in the campground\)", re.I),
    re.compile(r"\bhorses are not allowed at the cabin\b", re.I),
    re.compile(r"\bhorses are not allowed at the pavilion and campground\b", re.I),
    re.compile(r"\bhorses are not allowed near the .*guard station\b", re.I),
]

HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl", "dt", "dd",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
HTML_DISCARD_TAGS = {"script", "style", "iframe", "noscript", "svg"}
HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);")


class _DescriptionHTMLParser(HTMLParser):
    """Convert source HTML to safe, readable plain text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.discard_depth = 0

    def _separator(self):
        if self.parts and not self.parts[-1].endswith((" ", "\n")):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in HTML_DISCARD_TAGS:
            self.discard_depth += 1
            return
        if self.discard_depth:
            return
        if tag == "li":
            self._separator()
            self.parts.append("• ")
        elif tag in HTML_BLOCK_TAGS:
            self._separator()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in HTML_DISCARD_TAGS:
            if self.discard_depth:
                self.discard_depth -= 1
            return
        if self.discard_depth:
            return
        if tag in HTML_BLOCK_TAGS:
            self._separator()

    def handle_data(self, data):
        if not self.discard_depth and data:
            self.parts.append(data)


def sanitize_html_text(value):
    """Decode entities and remove HTML while retaining readable text and list markers."""
    raw = str(value or "").replace("\x00", " ")
    if not raw:
        return ""

    for _ in range(2):
        decoded = html.unescape(raw)
        if decoded == raw:
            break
        raw = decoded

    parser = _DescriptionHTMLParser()
    try:
        parser.feed(raw)
        parser.close()
        cleaned = "".join(parser.parts)
    except Exception:
        cleaned = raw

    cleaned = HTML_TAG_RE.sub(" ", html.unescape(cleaned))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned


def strip_html(text):
    return sanitize_html_text(text)

def is_equestrian(text_blob):
    low = text_blob.lower()
    return any(k in low for k in EQUESTRIAN_KEYWORDS)


# RIDB/Recreation.gov often tags ordinary campgrounds with activity=9
# (Horseback Riding) when there are horseback-riding trails nearby. HorseCamp
# should only import RIDB facilities when the listing itself has a clear onsite
# horse-camping signal such as equestrian camping, horse sites, corrals, stalls,
# highlines, tie rails, stock sites, etc. Nearby horseback riding alone is not
# enough.
RIDB_HORSE_CAMPING_PATTERNS = [
    re.compile(r"\bequestrian\s+(?:camp(?:ground|ing)?|camps?|site|sites|area|areas|loop|loops|facility|facilities)\b", re.I),
    re.compile(r"\bhorse\s+(?:camp(?:ground|ing)?|camps?|site|sites|area|areas|corral|corrals|stall|stalls|paddock|paddocks)\b", re.I),
    re.compile(r"\bstock\s+(?:camp(?:ground|ing)?|camps?|site|sites|area|areas|use|facility|facilities|corral|corrals|stall|stalls)\b", re.I),
    re.compile(r"\bcamp(?:ground|ing|site|sites)?\s+(?:with|for)\s+(?:your\s+)?horses\b", re.I),
    re.compile(r"\b(?:corrals?|stalls?|highlines?|high\s+lines?|tie\s+rails?|hitching\s+rails?|paddocks?)\b", re.I),
    re.compile(r"\bhorse\s+trailer\s+parking\b", re.I),
    re.compile(r"\bpack\s+station\b", re.I),
]


def has_ridb_horse_camping_signal(name, amenities, desc):
    """Return True only for clear onsite RIDB horse-camping signals.

    Deliberately ignores RIDB ACTIVITY names because activity=9 / Horseback
    Riding commonly means trail access near the campground, not horse camping
    at the facility.
    """
    blob = " ".join([str(name or ""), *(str(a or "") for a in amenities or []), str(desc or "")])
    return any(pattern.search(blob) for pattern in RIDB_HORSE_CAMPING_PATTERNS)


def is_invalid_equestrian_listing(camp):
    """Reject entries that keyword-match horses but explicitly do not allow horse camping."""
    try:
        lat = float(camp.get("latitude", 0) or 0)
        lng = float(camp.get("longitude", 0) or 0)
    except (TypeError, ValueError):
        return True
    if lat == 0 and lng == 0:
        return True

    text = " ".join(str(camp.get(k, "")) for k in ("name", "description", "location", "source"))
    return any(pattern.search(text) for pattern in INVALID_EQUESTRIAN_PATTERNS)


def remove_invalid_equestrian_listings(camps_dict):
    bad_ids = [cid for cid, camp in camps_dict.items() if is_invalid_equestrian_listing(camp)]
    for cid in bad_ids:
        del camps_dict[cid]
    print(f"  Invalid/non-horse listings removed: {len(bad_ids)}")
    return len(bad_ids)

def safe_get(url, headers=None, params=None, retries=3):
    """Fetch and decode one JSON response with bounded connect/read timeouts."""
    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=(8, 20),
            )
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                print("  Rate limited — waiting 10s...", flush=True)
                time.sleep(10)
                continue

            print(f"  HTTP {response.status_code} for {url}", flush=True)
            return None
        except requests.RequestException as error:
            print(
                f"  Request error (attempt {attempt + 1}/{retries}): {error}",
                flush=True,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            print(
                f"  Invalid JSON response (attempt {attempt + 1}/{retries}): {error}",
                flush=True,
            )

        if attempt + 1 < retries:
            time.sleep(3)

    return None


def print_section(title):
    print(f"\n=== {title} ===")


def print_metric(label, value, width=28):
    print(f"  {label + ':':<{width}} {value}")


def haversine_meters(lat1, lon1, lat2, lon2):
    import math
    radius_m = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_near_existing_camp(camp, existing_camps, threshold_m=500):
    """Return (existing_camp, distance_m) when camp is near an existing listing."""
    try:
        lat = float(camp["latitude"])
        lng = float(camp["longitude"])
    except (KeyError, TypeError, ValueError):
        return None, None

    nearest_existing = None
    nearest_distance = None
    for existing in existing_camps.values():
        try:
            distance_m = haversine_meters(lat, lng, float(existing["latitude"]), float(existing["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        if distance_m < threshold_m and (nearest_distance is None or distance_m < nearest_distance):
            nearest_existing = existing
            nearest_distance = distance_m

    return nearest_existing, nearest_distance


def is_near_existing_camp(camp, existing_camps, threshold_m=500):
    existing, _ = find_near_existing_camp(camp, existing_camps, threshold_m=threshold_m)
    return existing is not None


def merge_camps_by_id_and_proximity(camps, all_camps, threshold_m=500):
    """Merge camps into all_camps while skipping duplicate IDs and nearby duplicates.

    Returns (added, duplicate_id_skips, proximity_skips, proximity_details). This keeps source merge
    behavior consistent for partner/curated sources and makes workflow logs clearer.
    """
    added = 0
    duplicate_id_skips = 0
    proximity_skips = 0
    proximity_details = []

    for camp in camps:
        cid = camp["id"]
        if cid in all_camps:
            duplicate_id_skips += 1
            continue

        existing, distance_m = find_near_existing_camp(camp, all_camps, threshold_m=threshold_m)
        if existing is not None:
            proximity_skips += 1
            proximity_details.append({
                "incoming": camp,
                "existing": existing,
                "distance_m": distance_m,
            })
            continue

        all_camps[cid] = camp
        added += 1

    return added, duplicate_id_skips, proximity_skips, proximity_details


def print_nearby_duplicate_details(label, details):
    if not details:
        return

    print(f"  {label} nearby duplicate details:")
    for item in details:
        incoming = item["incoming"]
        existing = item["existing"]
        distance_m = item.get("distance_m") or 0
        distance_mi = distance_m / 1609.344
        print(
            "    - "
            f"Skipped: {incoming.get('name', 'Unknown')} "
            f"[{incoming.get('source', 'Unknown')}, {incoming.get('id', 'no-id')}] "
            f"near Existing: {existing.get('name', 'Unknown')} "
            f"[{existing.get('source', 'Unknown')}, {existing.get('id', 'no-id')}] "
            f"({distance_m:.0f} m / {distance_mi:.2f} mi)"
        )


# RIDB and the Forest Service sometimes publish the same federal campground
# with slightly different coordinates. The generic 500 m proximity merge catches
# the closest cases, but intentionally cannot use a large radius because different
# campgrounds can sit near one another. This targeted pass is stricter: it only
# compares RIDB against USFS, requires the same state and a normalized name match,
# and allows up to three miles for known coordinate disagreement. RIDB is retained
# because its record generally has the richer reservation, amenity, and photo data.
RIDB_USFS_DUPLICATE_MAX_DISTANCE_M = 3 * 1609.344
RIDB_USFS_NAME_NOISE = {
    "and", "area", "at", "camp", "campground", "campgrounds", "camping",
    "camps", "campsite", "campsites", "equestrian", "facilities", "facility",
    "family", "for", "group", "horse", "horses", "of", "recreation", "rv",
    "site", "sites", "the", "with",
}
RIDB_REFERENCES_OTHER_HORSE_CAMP_RE = re.compile(
    r"\bshares?\s+(?:the\s+)?(?:area|facility|campground|site|sites)\s+with\b"
    r".{0,160}\b(?:horse|equestrian)\s+camp",
    re.I | re.S,
)


def _ridb_usfs_name_signature(name):
    """Return a conservative comparison key for an RIDB/USFS campground name."""
    text = str(name or "").lower().replace("&", " and ")
    # Expand fused forms before stripping generic camping words.
    text = re.sub(r"\bhorse\s*camp(?:ground|ing)?s?\b", " horse camp ", text)
    text = re.sub(r"\bhorsecamp(?:ground|ing)?s?\b", " horse camp ", text)
    text = re.sub(r"\b(?:campgrounds?|camping|campsites?)\b", " camp ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)

    tokens = []
    for token in text.split():
        if token in RIDB_USFS_NAME_NOISE:
            continue
        # Normalize only ordinary trailing plurals. Keep proper names such as
        # Texas and Palisades intact.
        if len(token) > 3 and token.endswith("s") and token not in {"texas", "palisades"}:
            token = token[:-1]
        tokens.append(token)
    return " ".join(tokens)


def _ridb_mentions_another_horse_camp(camp):
    """Avoid treating a nearby non-horse RIDB campground as the horse camp it mentions."""
    name = str(camp.get("name") or "")
    # A camp explicitly named for horses/equestrian use is its own horse camp,
    # even if the description also says it shares the area with another facility.
    if re.search(r"\b(?:horse|equestrian|stock)\b|horsecamp", name, re.I):
        return False
    description = strip_html(str(camp.get("description") or ""))
    return bool(RIDB_REFERENCES_OTHER_HORSE_CAMP_RE.search(description))


def remove_ridb_usfs_name_location_duplicates(camps_dict):
    """Drop USFS copies of the same RIDB campground; return audit details.

    This deliberately does not merge broad nearby matches. A site is removed only
    when RIDB and USFS have the same state, matching normalized names, and are no
    more than three miles apart. The ridb record remains the authoritative entry.
    """
    ridb_camps = [camp for camp in camps_dict.values() if camp.get("source") == "RIDB"]
    usfs_camps = [camp for camp in camps_dict.values() if camp.get("source") == "U.S. Forest Service"]
    removed = []

    for usfs in usfs_camps:
        usfs_signature = _ridb_usfs_name_signature(usfs.get("name"))
        if not usfs_signature:
            continue

        best_match = None
        best_distance_m = None
        for ridb in ridb_camps:
            if ridb.get("state") != usfs.get("state"):
                continue
            if _ridb_mentions_another_horse_camp(ridb):
                continue
            if _ridb_usfs_name_signature(ridb.get("name")) != usfs_signature:
                continue
            try:
                distance_m = haversine_meters(
                    float(ridb["latitude"]), float(ridb["longitude"]),
                    float(usfs["latitude"]), float(usfs["longitude"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if distance_m > RIDB_USFS_DUPLICATE_MAX_DISTANCE_M:
                continue
            if best_distance_m is None or distance_m < best_distance_m:
                best_match = ridb
                best_distance_m = distance_m

        if best_match is not None:
            camps_dict.pop(usfs["id"], None)
            removed.append({
                "ridb": best_match,
                "usfs": usfs,
                "distance_m": best_distance_m,
            })

    return removed


def print_ridb_usfs_duplicate_details(details):
    if not details:
        return
    print("  RIDB ↔ USFS duplicate details (kept RIDB):")
    for item in details:
        ridb = item["ridb"]
        usfs = item["usfs"]
        distance_m = item["distance_m"]
        print(
            "    - "
            f"Removed USFS: {usfs.get('name', 'Unknown')} [{usfs.get('id', 'no-id')}] "
            f"matched RIDB: {ridb.get('name', 'Unknown')} [{ridb.get('id', 'no-id')}] "
            f"({distance_m:.0f} m / {distance_m / 1609.344:.2f} mi)"
        )



def _compact_selected_array_fields(json_text, field_names):
    """Collapse selected array fields onto a single line after pretty-printing JSON.

    This keeps the overall 2-space-indented structure, while matching the manual
    style used for short arrays like accommodations and imageColors.
    """
    field_pattern = "|".join(re.escape(name) for name in field_names)
    pattern = re.compile(
        rf'(?P<indent>^[ \t]*)"(?P<field>{field_pattern})": \[\n'
        rf'(?P<body>(?:^[ \t]+.*\n)*?)'
        rf'(?P=indent)\]',
        flags=re.MULTILINE,
    )

    def repl(match):
        body = match.group("body")
        # Build a valid JSON array from the pretty-printed body, then re-emit it
        # compactly to guarantee correct escaping and commas.
        array_text = "[\n" + body + match.group("indent") + "]"
        values = json.loads(array_text)
        compact = json.dumps(values, ensure_ascii=False)
        return f'{match.group("indent")}"{match.group("field")}": {compact}'

    return pattern.sub(repl, json_text)


# The mobile apps tolerate omitted non-core fields and supply defaults. Keep the
# public feed lean by omitting provenance/admin-only values and the now-unused
# rating/reviewCount placeholders. Fields used to render a usable camp are checked
# below immediately before camps.json is written.
PUBLIC_FEED_OMIT_FIELDS = {
    "submittedIssueNumber",
    "sourceUrl",
    "attribution",
    "coordinateSource",
    "lastSynced",
    "locationConfidence",
    "mapSearchAddress",
    "mapStatus",
    "partner",
    "address",
    "addressPreferredForMaps",
    "category",
    "city",
    "sourceDetail",
    "rating",
    "reviewCount",
}
PUBLIC_FEED_MIN_EXPECTED_CAMPS = 1000
PUBLIC_FEED_REQUIRED_FIELDS = ("id", "name", "location", "state", "source", "latitude", "longitude")
PUBLIC_FEED_BLOCKED_SOURCES = {"OSM", "OpenStreetMap"}
HORSEMOTEL_STATE_PAGE_RE = re.compile(r"^https?://(?:www\.)?horsemotel\.com/[A-Za-z-]+\.html$", re.I)


def strip_public_feed_fields(camps):
    """Remove retired/default-only fields from the public camps.json feed."""
    removed = 0
    for camp in camps:
        for field in PUBLIC_FEED_OMIT_FIELDS:
            if field in camp:
                del camp[field]
                removed += 1
    return removed


def _is_nonblank(value):
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def validate_public_feed(camps):
    """Fail before publishing an unusable public feed.

    This intentionally validates only delivery-critical data quality. It does not
    impose a closed JSON schema: newer fields may be added without an app update,
    and the iOS/Android decoders default absent non-core fields safely.
    """
    errors = []
    if len(camps) < PUBLIC_FEED_MIN_EXPECTED_CAMPS:
        errors.append(
            f"only {len(camps)} camps generated; expected at least {PUBLIC_FEED_MIN_EXPECTED_CAMPS}"
        )

    seen_ids = set()
    for index, camp in enumerate(camps):
        label = f"camps[{index}]"
        if not isinstance(camp, dict):
            errors.append(f"{label} is not an object")
            continue

        camp_id = str(camp.get("id") or "").strip()
        if not camp_id:
            errors.append(f"{label} has a blank id")
        elif camp_id in seen_ids:
            errors.append(f"duplicate camp id: {camp_id}")
        seen_ids.add(camp_id)

        for field in PUBLIC_FEED_REQUIRED_FIELDS:
            if not _is_nonblank(camp.get(field)):
                errors.append(f"{label} ({camp_id or 'no-id'}) has blank {field}")

        try:
            latitude = float(camp.get("latitude"))
            longitude = float(camp.get("longitude"))
            if not math.isfinite(latitude) or not math.isfinite(longitude):
                raise ValueError("not finite")
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("out of range")
            if latitude == 0 and longitude == 0:
                raise ValueError("zero coordinate")
        except (TypeError, ValueError):
            errors.append(f"{label} ({camp_id or 'no-id'}) has invalid coordinates")

        source = str(camp.get("source") or "")
        if source in PUBLIC_FEED_BLOCKED_SOURCES or camp_id.startswith("osm-"):
            errors.append(f"blocked OSM/OpenStreetMap record: {camp_id or label}")

        website = str(camp.get("website") or "").strip()
        if source == "HorseMotel.com" and website and HORSEMOTEL_STATE_PAGE_RE.match(website):
            errors.append(f"HorseMotel generic state-page URL: {camp_id or label}")

        description = str(camp.get("description") or "")
        if HTML_TAG_RE.search(description):
            errors.append(f"{label} ({camp_id or 'no-id'}) description contains HTML tags")
        if HTML_ENTITY_RE.search(description):
            errors.append(f"{label} ({camp_id or 'no-id'}) description contains encoded HTML entities")

        retired = sorted(PUBLIC_FEED_OMIT_FIELDS & set(camp.keys()))
        if retired:
            errors.append(f"{label} ({camp_id or 'no-id'}) retains omitted fields: {', '.join(retired)}")

    if errors:
        print("Public feed validation failed; camps.json was not written.")
        for error in errors[:50]:
            print(f"ERROR: {error}")
        if len(errors) > 50:
            print(f"ERROR: ... {len(errors) - 50} additional validation failures")
        raise RuntimeError("Public feed validation failed")

    print(f"Public feed validation passed: {len(camps)} camps; {len(seen_ids)} unique IDs")


def normalize_description_text(value):
    """Store description values as safe, decoded, readable plain text."""
    return sanitize_html_text(value)


def normalize_simple_phone(value):
    """Normalize only simple single US/Canada phone numbers to 123-456-7890.

    Complex/labeled/multiple-number contact strings are intentionally preserved.
    """
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{10}", raw):
        return f"{raw[0:3]}-{raw[3:6]}-{raw[6:10]}"
    match = re.fullmatch(r"\((\d{3})\)\s*(\d{3})-(\d{4})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return raw


def normalize_description_fields(value):
    """Recursively collapse description whitespace and normalize simple phone values before writing JSON."""
    if isinstance(value, list):
        return [normalize_description_fields(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "description":
                normalized[key] = normalize_description_text(item)
            elif key == "phone":
                normalized[key] = normalize_simple_phone(item)
            else:
                normalized[key] = normalize_description_fields(item)
        return normalized
    return value


def write_camps_json(path, payload):
    """Write camps.json with stable pretty-printing and compact selected arrays."""
    payload = normalize_description_fields(payload)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    rendered = _compact_selected_array_fields(rendered, {"hookups", "accommodations", "imageColors", "photoURLs"})
    path.write_text(rendered + "\n", encoding="utf-8")


# ── MANUAL OVERRIDES / EXCLUSIONS ─────────────────────────────────────
OVERRIDES_FILE = DATA_DIR / "overrides.json"
EXCLUSIONS_FILE = DATA_DIR / "exclusions.json"


def _load_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {path}: {e}") from e


def load_overrides():
    """Load manual field overrides for dynamically fetched camps.

    File format:
      {
        "camp-id": {"phone": "...", "website": "...", "isVerified": true},
        ...
      }
    """
    data = _load_json_file(OVERRIDES_FILE, {})
    if not isinstance(data, dict):
        raise RuntimeError(f"{OVERRIDES_FILE} must contain a top-level JSON object")
    for camp_id, patch in data.items():
        if not isinstance(camp_id, str) or not camp_id.strip():
            raise RuntimeError(f"{OVERRIDES_FILE} contains an invalid camp id key: {camp_id!r}")
        if not isinstance(patch, dict):
            raise RuntimeError(f"Override for {camp_id!r} in {OVERRIDES_FILE} must be a JSON object")
    return data


def load_exclusions():
    """Load list of camp IDs to exclude from the generated output."""
    data = _load_json_file(EXCLUSIONS_FILE, [])
    if not isinstance(data, list):
        raise RuntimeError(f"{EXCLUSIONS_FILE} must contain a top-level JSON array")
    cleaned = []
    for camp_id in data:
        if not isinstance(camp_id, str) or not camp_id.strip():
            raise RuntimeError(f"{EXCLUSIONS_FILE} contains an invalid camp id entry: {camp_id!r}")
        cleaned.append(camp_id.strip())
    return cleaned


def apply_exclusions(camps_dict):
    """Remove any camp IDs listed in data/exclusions.json."""
    excluded_ids = load_exclusions()
    removed = 0
    for camp_id in excluded_ids:
        if camp_id in camps_dict:
            del camps_dict[camp_id]
            removed += 1
    print(f"  Exclusions applied: {removed} removed")
    return removed


def apply_overrides(camps_dict):
    """Apply partial field patches from data/overrides.json."""
    overrides = load_overrides()
    applied = 0
    missing_ids = []

    numeric_float_fields = {"pricePerNight", "horseFeePerNight", "latitude", "longitude"}
    numeric_int_fields = {"maxRigLength", "stallCount", "paddockCount", "seasonStart", "seasonEnd"}
    bool_fields = {"isVerified", "hasWashRack", "hasDumpStation", "hasWifi", "hasBathhouse", "pullThroughAvailable"}
    list_fields = {"hookups", "accommodations", "imageColors", "photoURLs"}

    for camp_id, patch in overrides.items():
        camp = camps_dict.get(camp_id)
        if camp is None:
            missing_ids.append(camp_id)
            continue

        for key, value in patch.items():
            if key in numeric_float_fields:
                try:
                    camp[key] = float(value)
                except (TypeError, ValueError):
                    raise RuntimeError(f"Override {camp_id!r}.{key} must be a number")
            elif key in numeric_int_fields:
                try:
                    camp[key] = int(value)
                except (TypeError, ValueError):
                    raise RuntimeError(f"Override {camp_id!r}.{key} must be an integer")
            elif key in bool_fields:
                if not isinstance(value, bool):
                    raise RuntimeError(f"Override {camp_id!r}.{key} must be true or false")
                camp[key] = value
            elif key in list_fields:
                if not isinstance(value, list):
                    raise RuntimeError(f"Override {camp_id!r}.{key} must be a JSON array")
                camp[key] = value
            else:
                camp[key] = value

        if "isVerified" not in patch:
            camp["isVerified"] = True
        camps_dict[camp_id] = camp
        applied += 1

    if missing_ids:
        print(f"  Overrides skipped (missing ids): {len(missing_ids)}")
    print(f"  Overrides applied: {applied} updated")
    return applied

# ── RIDB HELPERS ──────────────────────────────────────────────────────
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

def parse_season(facility):
    """Extract real open/close months from FACILITYSEASON array.
    Returns (0, 0) if no reliable data found — app shows no season status."""
    seasons = facility.get("FACILITYSEASON") or []

    for season in seasons:
        start_str = season.get("StartDate", "") or ""
        end_str   = season.get("EndDate", "")   or ""
        try:
            if "-" in start_str and "-" in end_str:
                start_month = int(start_str.split("-")[1])
                end_month   = int(end_str.split("-")[1])
                if 1 <= start_month <= 12 and 1 <= end_month <= 12:
                    if not (start_month == 1 and end_month == 12):
                        return start_month, end_month
        except:
            pass

    return 0, 0  # Unknown — no data is better than wrong data
def parse_rig_length(facility):
    """Extract max rig length from PERMITTEDEQUIPMENT on campsites."""
    campsites = facility.get("CAMPSITE") or []
    max_len = 0
    for site in campsites:
        for eq in (site.get("PERMITTEDEQUIPMENT") or []):
            eq_name = (eq.get("EquipmentName") or "").lower()
            # Only care about trailer/RV type equipment
            if any(k in eq_name for k in ["trailer", "rv", "motorhome", "camper", "horse"]):
                try:
                    length = int(eq.get("MaxLength") or 0)
                    if length > max_len:
                        max_len = length
                except:
                    pass
    return max_len if max_len > 0 else 0  # 0 = unknown, app shows nothing

def parse_stall_count(facility):
    """Extract actual stall count from amenities."""
    amenities = facility.get("FACILITYAMENITY") or []
    for a in amenities:
        name = (a.get("AmenityName") or "").lower()
        if "stall" in name:
            try:
                qty = int(a.get("AmenityValue") or a.get("Quantity") or 0)
                if qty > 0:
                    return qty
            except:
                pass
    return 0

def parse_paddock_count(facility):
    """Extract actual corral/paddock count from amenities."""
    amenities = facility.get("FACILITYAMENITY") or []
    for a in amenities:
        name = (a.get("AmenityName") or "").lower()
        if "corral" in name or "paddock" in name:
            try:
                qty = int(a.get("AmenityValue") or a.get("Quantity") or 0)
                if qty > 0:
                    return qty
            except:
                pass
    return 0

def parse_ridb_fee(facility):
    """Extract nightly fee from RIDB facility data.
    Checks FACILITYFEE first, then falls back to CAMPSITE fees.
    Returns 0.0 if no fee data found (app shows 'See site for pricing')."""
    # Try facility-level fee first
    for fee in (facility.get("FACILITYFEE") or []):
        fee_type = (fee.get("FeeType") or "").lower()
        if "overnight" in fee_type or "nightly" in fee_type or "camping" in fee_type or fee_type == "":
            try:
                amount = float(fee.get("FeeAmount") or 0)
                if amount > 0:
                    return amount
            except:
                pass

    # Fall back to campsite-level fees
    campsites = facility.get("CAMPSITE") or []
    fees_found = []
    for site in campsites:
        for fee in (site.get("CAMPSITE_FEE") or []):
            fee_type = (fee.get("FeeType") or "").lower()
            # Skip reservation/one-time fees, only want nightly/use fees
            if "reservation" in fee_type or "cancellation" in fee_type:
                continue
            try:
                amount = float(fee.get("FeeAmount") or 0)
                if amount > 0:
                    fees_found.append(amount)
            except:
                pass

    # Return the median fee if multiple campsites to avoid outliers
    if fees_found:
        fees_found.sort()
        return fees_found[len(fees_found) // 2]

    return 0.0

# ── RIDB HELPERS ──────────────────────────────────────────────────────
def parse_ridb_photos(facility):
    """Extract all photo URLs from MEDIA array, primary first."""
    media = facility.get("MEDIA") or []
    images = [m for m in media if m.get("MediaType") == "Image" and m.get("URL")]
    if not images:
        return []
    # Primary first, then gallery images, then rest
    primary = [m for m in images if m.get("IsPrimary")]
    gallery = [m for m in images if not m.get("IsPrimary") and m.get("IsGallery")]
    rest    = [m for m in images if not m.get("IsPrimary") and not m.get("IsGallery")]
    ordered = primary + gallery + rest
    return [m["URL"] for m in ordered[:6]]  # cap at 6 photos

# ── RIDB ───────────────────────────────────────────────────────────────
def _ridb_total_count(data):
    """Return RIDB's reported total result count when present."""
    metadata = data.get("METADATA") if isinstance(data, dict) else None
    results = metadata.get("RESULTS") if isinstance(metadata, dict) else None
    if not isinstance(results, dict):
        return None

    for key in ("TOTAL_COUNT", "TotalCount", "totalCount", "total_count"):
        value = results.get(key)
        try:
            count = int(value)
            return count if count >= 0 else None
        except (TypeError, ValueError):
            continue
    return None


def fetch_ridb_state(state):
    camps = {}
    headers = {"apikey": RIDB_KEY}
    state_started = time.monotonic()
    search_terms = [
        ("activity", "9"),           # activity 9 = Horseback Riding
        ("query", "horse corral"),
        ("query", "equestrian"),
        ("query", "horse camp"),
        ("query", "horse stall"),
    ]


    for param_key, param_val in search_terms:
        offset = 0
        page_number = 0
        seen_term_ids = set()
        seen_page_signatures = set()
        reported_total = None

        while True:
            elapsed = time.monotonic() - state_started
            if elapsed > RIDB_STATE_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f"RIDB {state} exceeded {RIDB_STATE_TIMEOUT_SECONDS}s while fetching "
                    f"{param_key}={param_val!r}; refusing to publish a partial state result"
                )

            if page_number >= RIDB_MAX_PAGES_PER_SEARCH:
                raise RuntimeError(
                    f"RIDB {state} exceeded {RIDB_MAX_PAGES_PER_SEARCH} pages while fetching "
                    f"{param_key}={param_val!r}; possible pagination loop"
                )

            params = {
                param_key: param_val,
                "state":   state,
                "limit":   RIDB_PAGE_SIZE,
                "offset":  offset,
                "full":    "true",
            }
            data = safe_get(f"{RIDB_BASE}/facilities", headers=headers, params=params)
            if not data:
                break

            facilities = data.get("RECDATA", [])
            if not facilities:
                break


            page_number += 1
            if reported_total is None:
                reported_total = _ridb_total_count(data)

            page_ids = [str(f.get("FacilityID", "")).strip() for f in facilities]
            page_id_set = {fid for fid in page_ids if fid}
            page_signature = frozenset(page_id_set)

            # A repeated page means RIDB ignored or reset the offset. Stop this
            # search term instead of requesting the same records forever.
            if page_signature and page_signature in seen_page_signatures:
                print(
                    f"\n  WARNING: RIDB {state} repeated page {page_number} for "
                    f"{param_key}={param_val!r} at offset {offset}; stopping that query",
                    flush=True,
                )
                break

            # Also stop if a differently ordered/partially overlapping page adds
            # no new facility IDs within this search term.
            new_term_ids = page_id_set - seen_term_ids
            if page_number > 1 and page_id_set and not new_term_ids:
                print(
                    f"\n  WARNING: RIDB {state} page {page_number} added no new IDs for "
                    f"{param_key}={param_val!r} at offset {offset}; stopping that query",
                    flush=True,
                )
                break

            if page_signature:
                seen_page_signatures.add(page_signature)
            seen_term_ids.update(page_id_set)

            if (
                RIDB_PROGRESS_EVERY_PAGES > 0
                and page_number % RIDB_PROGRESS_EVERY_PAGES == 0
            ):
                total_text = f"/{reported_total}" if reported_total is not None else ""
                print(
                    f"\n  RIDB {state} {param_key}={param_val!r}: page {page_number}, "
                    f"offset {offset}, {len(seen_term_ids)}{total_text} unique raw facilities",
                    flush=True,
                )

            for f in facilities:
                fid = str(f.get("FacilityID", ""))
                if not fid or fid in camps:
                    continue

                lat = float(f.get("FacilityLatitude", 0) or 0)
                lng = float(f.get("FacilityLongitude", 0) or 0)
                if abs(lat) < 0.1 or abs(lng) < 0.1:
                    continue

                amenities  = [a.get("AmenityName", "") for a in (f.get("FACILITYAMENITY") or [])]
                activities = [a.get("ActivityName", "") for a in (f.get("ACTIVITY") or [])]
                desc       = strip_html(f.get("FacilityDescription", ""))
                blob       = " ".join(amenities + activities + [desc])

                # RIDB activity=9 means Horseback Riding may be nearby; it does
                # not prove the campground supports overnight horse camping.
                # Require a stronger onsite horse-camping signal from the facility
                # name, amenities, or description for every RIDB match.
                if not has_ridb_horse_camping_signal(f.get("FacilityName", ""), amenities, desc):
                    continue

                addr  = (f.get("FACILITYADDRESS") or [{}])[0]
                city  = addr.get("City", "")
                fstate = addr.get("AddressStateCode", state)

                blob_lower = blob.lower()

                hookups = []
                if "50 amp" in blob_lower or "50-amp" in blob_lower: hookups.append("50A")
                if "30 amp" in blob_lower or "30-amp" in blob_lower: hookups.append("30A")
                if "water hookup" in blob_lower:                       hookups.append("Water")

                accommodations = []
                if "stall"    in blob_lower: accommodations.append("Stalls")
                if "corral"   in blob_lower: accommodations.append("Corrals")
                if "highline" in blob_lower or "high line" in blob_lower or "tie rail" in blob_lower:
                    accommodations.append("Highlines")
                if "paddock"  in blob_lower: accommodations.append("Paddocks")
                if "trail" in blob_lower or "hiking" in blob_lower: accommodations.append("Trails")
                if "cabin" in blob_lower: accommodations.append("Cabins")

                season_start, season_end = parse_season(f)
                camps[fid] = {
                    "id":                  f"ridb-{fid}",
                    "name":                f.get("FacilityName", "Unknown Camp"),
                    "location":            f"{city}, {fstate}".strip(", "),
                    "state":               fstate,
                    "latitude":            lat,
                    "longitude":           lng,
                    "pricePerNight":       parse_ridb_fee(f),
                    "horseFeePerNight":    0.0,
                    "hookups":             list(dict.fromkeys(hookups)),
                    "accommodations":      list(dict.fromkeys(accommodations)),
                    "maxRigLength":        parse_rig_length(f),
                    "stallCount":          parse_stall_count(f),
                    "paddockCount":        parse_paddock_count(f),
                    "phone":               f.get("FacilityPhone", ""),
                    "website":             f.get("FacilityReservationURL", "") or f"https://www.recreation.gov/camping/campgrounds/{fid}",
                    "description":         desc[:2000],
                    "isVerified":          False,
                    "seasonStart":         season_start,
                    "seasonEnd":           season_end,
                    "hasWashRack":         "wash rack" in blob_lower,
                    "hasDumpStation":      "dump" in blob_lower,
                    "hasWifi":             "wifi" in blob_lower or "internet" in blob_lower,
                    "hasBathhouse":        "shower" in blob_lower or "bathhouse" in blob_lower,
                    "pullThroughAvailable": "pull-through" in blob_lower or "pull through" in blob_lower,
                    "imageColors":         ["5C7A4E", "D4A853"],
                    "photoURLs":           parse_ridb_photos(f),
                    "source":              "RIDB",
                }

            received_count = len(facilities)
            next_offset = offset + received_count

            # Prefer RIDB's own total count when available. This handles a final
            # page containing exactly RIDB_PAGE_SIZE records without requesting
            # an unnecessary extra page.
            if reported_total is not None and next_offset >= reported_total:
                break
            if received_count < RIDB_PAGE_SIZE:
                break

            offset = next_offset
            time.sleep(0.5)

        time.sleep(0.3)

    return list(camps.values())




# ── U.S. FOREST SERVICE ───────────────────────────────────────────────
# A Horse Camping marker is authoritative. For normal Campground/RV/Group
# Camping markers, require a separate onsite horse/stock-camping signal. This
# includes sites such as Monture Creek that identify as a campground while their
# details explicitly allow stock and provide horse facilities.
USFS_HORSE_CAMPING_PATTERNS = [
    re.compile(r"\bhorse\s+(?:camp(?:ground|ing)?|camps?|site|sites|area|areas)\b", re.I),
    re.compile(r"\bequestrian\s+(?:camp(?:ground|ing)?|camps?|site|sites|area|areas)\b", re.I),
    re.compile(r"\b(?:public\s+)?stock\s+(?:facilit(?:y|ies)|camp(?:ground|ing)?|camps?|site|sites|area|areas|corral|corrals|pen|pens)\b", re.I),
    re.compile(r"\bhorse\s*/\s*pack\s+animals?\s+(?:are\s+)?allowed\b", re.I),
    re.compile(r"\b(?:corrals?|stalls?|paddocks?|high\s*lines?|hitching\s+rails?|tie\s+rails?)\b", re.I),
    re.compile(r"\bpack\s+stock\b", re.I),
]

# Query only the fields actually consumed below. It reduces nightly transfer
# volume while still keeping the source, amenities, restrictions, and stable ID.
USFS_OUT_FIELDS = ",".join([
    "RECAREANAME",
    "LONGITUDE",
    "LATITUDE",
    "RECAREAURL",
    "OPEN_SEASON_START",
    "OPEN_SEASON_END",
    "FORESTNAME",
    "RECAREAID",
    "MARKERACTIVITY",
    "RECAREADESCRIPTION",
    "FEEDESCRIPTION",
    "RESTRICTIONS",
    "INFRA_CN",
    "OBJECTID",
])

# Accept every explicit Horse Camping marker (including label variants that
# contain both Horse and Camp), plus camping markers with an explicit horse,
# equestrian, stock, corral, or pack-animal detail. Final acceptance is checked
# again locally by _usfs_has_horse_camping_signal().
USFS_CANDIDATE_WHERE = (
    "((MARKERACTIVITY LIKE '%Horse%' AND MARKERACTIVITY LIKE '%Camp%') "
    "OR (MARKERACTIVITY LIKE '%Camp%' AND ("
    "RECAREADESCRIPTION LIKE '%horse%' OR RECAREADESCRIPTION LIKE '%equestrian%' "
    "OR RECAREADESCRIPTION LIKE '%stock%' OR RECAREADESCRIPTION LIKE '%corral%' "
    "OR RECAREADESCRIPTION LIKE '%pack animal%' OR FEEDESCRIPTION LIKE '%horse%' "
    "OR RESTRICTIONS LIKE '%horse%')))"
)


def _usfs_value(attrs, *names):
    """Return the first non-empty USFS ArcGIS field (service field case varies)."""
    lowered = {str(k).lower(): v for k, v in (attrs or {}).items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _usfs_month(value):
    """Best-effort month extraction from a USFS open-season text field."""
    text = str(value or "").strip().lower()
    if not text:
        return 0
    for token, month in MONTH_MAP.items():
        if re.search(r"\b" + re.escape(token) + r"\b", text):
            return month
    match = re.search(r"\b(1[0-2]|[1-9])\b", text)
    return int(match.group(1)) if match else 0


def _usfs_camp_id(attrs):
    """Create a stable ID from the Forest Service recreation-area identifier."""
    raw = _usfs_value(attrs, "recareaid", "objectid", "infra_cn")
    return "usfs-" + re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


def _usfs_state_cache_key(lat, lon):
    """Stable namespace within the existing persistent geocode cache."""
    return f"usfs-state:{lat:.5f},{lon:.5f}"


def _usfs_cached_state(lat, lon):
    state = _geocode_cache.get(_usfs_state_cache_key(lat, lon))
    if isinstance(state, str) and state in STATE_FIPS_TO_ABBR.values():
        _usfs_state_stats["hits"] += 1
        return state
    return ""


def _usfs_state_from_coordinates(lat, lon):
    """Resolve a USFS coordinate to a postal abbreviation using Census GeoLookup.

    Values are cached in data/geocode_cache.json, so a given Forest Service site
    normally requires this official lookup only once. An unresolved state is
    intentionally returned as blank; the caller aborts rather than publishing
    a misleading generic ``US`` state value.
    """
    cached = _usfs_cached_state(lat, lon)
    if cached:
        return cached

    _usfs_state_stats["misses"] += 1
    payload = safe_get(
        US_CENSUS_COORDINATES_URL,
        params={
            "x": f"{lon:.6f}",
            "y": f"{lat:.6f}",
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "States",
            "format": "json",
        },
        retries=3,
    )
    # Keep a conservative request pace only for newly seen sites. Cached sites
    # make later nightly runs avoid this API altogether.
    time.sleep(0.2)

    geographies = ((payload or {}).get("result") or {}).get("geographies") or {}
    state_rows = geographies.get("States") or []
    if not state_rows:
        # Be resilient to a future display-label variation from the Census API.
        for label, rows in geographies.items():
            if "state" in str(label).lower() and isinstance(rows, list):
                state_rows = rows
                break

    for row in state_rows:
        fips = str((row or {}).get("STATE", "")).zfill(2)
        state = STATE_FIPS_TO_ABBR.get(fips, "")
        if state:
            _geocode_cache[_usfs_state_cache_key(lat, lon)] = state
            _usfs_state_stats["stored"] += 1
            return state

    _usfs_state_stats["unresolved"] += 1
    return ""


def _usfs_has_horse_camping_signal(name, marker_activity, description, restrictions, fee_description):
    """Return True for an explicit or strongly evidenced USFS horse camp."""
    marker = str(marker_activity or "").lower()
    if "horse" in marker and "camp" in marker:
        return True
    if "camp" not in marker:
        return False

    blob = " ".join([
        str(name or ""),
        str(description or ""),
        str(restrictions or ""),
        str(fee_description or ""),
    ])
    return any(pattern.search(blob) for pattern in USFS_HORSE_CAMPING_PATTERNS)


def fetch_usfs_recreation_sites():
    """Fetch official Forest Service campgrounds that support overnight horses.

    The primary source is the official Forest Service ArcGIS recreation layer.
    The newer fs.usda.gov site can expose additional official campground pages
    that are not present, or not fully horse-tagged, in that layer. After the
    ArcGIS pass, a conservative website supplement crawls the official
    Horse Riding and Camping opportunity pages for forests already represented
    by the ArcGIS source and imports only detail pages with coordinates and
    explicit overnight horse-camping evidence.

    The USFS source has no state field. Each accepted coordinate is therefore
    resolved through the official Census geographic lookup and cached. This
    prevents camps from being incorrectly grouped under a fake ``US`` state.
    """
    params = {
        "where": USFS_CANDIDATE_WHERE,
        "outFields": USFS_OUT_FIELDS,
        "returnGeometry": "true",
        "f": "json",
        "resultRecordCount": 2000,
        "resultOffset": 0,
        "outSR": 4326,
    }
    camps = []
    offset = 0
    seen_ids = set()
    unresolved = []

    while True:
        params["resultOffset"] = offset
        payload = safe_get(USFS_RECREATION_URL, params=params)
        if payload is None:
            raise RuntimeError(
                "Unable to retrieve U.S. Forest Service recreation candidates; "
                "refusing to publish an incomplete USFS feed."
            )
        if payload.get("error"):
            raise RuntimeError(f"U.S. Forest Service service error: {payload['error']}")
        features = payload.get("features") or []
        if not features:
            break

        for feature in features:
            attrs = feature.get("attributes") or {}
            geometry = feature.get("geometry") or {}
            name = _usfs_value(attrs, "recareaname")
            activity = _usfs_value(attrs, "markeractivity")
            description = _usfs_value(attrs, "recareadescription")
            restrictions = _usfs_value(attrs, "restrictions")
            fee_description = _usfs_value(attrs, "feedescription")
            if not name or not _usfs_has_horse_camping_signal(
                name, activity, description, restrictions, fee_description
            ):
                continue

            try:
                lat = float(geometry.get("y") if geometry.get("y") is not None else _usfs_value(attrs, "latitude"))
                lon = float(geometry.get("x") if geometry.get("x") is not None else _usfs_value(attrs, "longitude"))
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            camp_id = _usfs_camp_id(attrs)
            if not camp_id or camp_id == "usfs-" or camp_id in seen_ids:
                continue
            seen_ids.add(camp_id)

            state = _usfs_state_from_coordinates(lat, lon)
            if not state:
                unresolved.append(f"{name} ({camp_id}; {lat:.5f}, {lon:.5f})")
                continue

            forest = _usfs_value(attrs, "forestname")
            description_parts = [description, restrictions, fee_description]
            final_description = " ".join(part for part in description_parts if part)
            website = _usfs_value(attrs, "recareaurl")
            accommodations = ["Trails"]
            marker_activity = activity.lower()
            if "rv camping" in marker_activity:
                accommodations.append("Big Rig")

            camps.append({
                "id": camp_id,
                "name": name,
                "location": forest or f"U.S. Forest Service, {state}",
                "state": state,
                "latitude": lat,
                "longitude": lon,
                "pricePerNight": 0.0,
                "horseFeePerNight": 0.0,
                "hookups": [],
                "accommodations": accommodations,
                "maxRigLength": 0,
                "stallCount": 0,
                "paddockCount": 0,
                "phone": "",
                "website": website,
                "description": final_description or "Official U.S. Forest Service horse-camping site.",
                "isVerified": True,
                "seasonStart": _usfs_month(_usfs_value(attrs, "open_season_start")),
                "seasonEnd": _usfs_month(_usfs_value(attrs, "open_season_end")),
                "hasWashRack": False,
                "hasDumpStation": False,
                "hasWifi": False,
                "hasBathhouse": False,
                "pullThroughAvailable": False,
                "imageColors": ["6A1B9A", "CE93D8"],
                "photoURLs": [],
                "source": "U.S. Forest Service",
            })

        if len(features) < int(params["resultRecordCount"]):
            break
        offset += len(features)
        time.sleep(0.25)

    if unresolved:
        sample = "; ".join(unresolved[:3])
        suffix = "" if len(unresolved) <= 3 else f"; +{len(unresolved) - 3} more"
        raise RuntimeError(
            "Unable to resolve a state for one or more U.S. Forest Service camps. "
            "Refusing to publish generic US state values: " + sample + suffix
        )

    supplemental_camps = fetch_usfs_official_website_supplement(camps)
    camps.extend(supplemental_camps)

    print(f"  U.S. Forest Service: {len(camps)} official horse-camping listings")
    if supplemental_camps:
        print(f"  U.S. Forest Service website supplement: {len(supplemental_camps)} additional official pages")
    return camps


USFS_WEBSITE_BASE = "https://www.fs.usda.gov"
USFS_WEBSITE_USER_AGENT = "HorseCampDataFetcher/1.0 (+https://horsecampfinder.com)"
USFS_HORSE_ACTIVITY_PATH = "/recreation/opportunities/horse-riding-and-camping"
USFS_WEBSITE_MAX_ACTIVITY_PAGES = 8
USFS_WEBSITE_MAX_DETAIL_PAGES_PER_FOREST = 80

# The official Forest Service website rate-limits bursts. Keep this crawler
# deliberately slower than the API-backed sources and use adaptive backoff
# instead of repeatedly hammering the site after throttling begins.
USFS_WEBSITE_REQUEST_DELAY_SECONDS = 2.0
USFS_WEBSITE_REQUEST_JITTER_SECONDS = 0.5
USFS_WEBSITE_MAX_429_RETRY_SECONDS = 60
USFS_WEBSITE_403_RETRY_SECONDS = 60
USFS_WEBSITE_MAX_CONSECUTIVE_403_FAILURES = 5
USFS_WEBSITE_SUPPLEMENT_MIN_RETAIN_RATIO = 0.80
USFS_WEBSITE_BOOTSTRAP_GOOD_FEED_COMMIT = "2b38b08d5b8281228b664627c0b75edc6d3da6f6"
_last_usfs_website_request_at = 0.0
_usfs_website_consecutive_403_failures = 0
_usfs_website_throttle_events = 0


class USFSWebsiteBlocked(RuntimeError):
    """Raised after sustained HTTP 403 blocking from fs.usda.gov."""

USFS_DETAIL_LINK_INCLUDE_RE = re.compile(
    r"\b(?:camp|campground|campsite|camping|horse\s+camp|equestrian\s+camp|stock\s+camp|cow\s+camp)\b",
    re.I,
)
USFS_DETAIL_LINK_STRONG_CAMP_RE = re.compile(
    r"\b(?:camp|campground|campsite|camping|horse\s+camp|equestrian\s+camp|stock\s+camp|cow\s+camp)\b",
    re.I,
)
USFS_DETAIL_LINK_EXCLUDE_RE = re.compile(
    r"\b(?:trail|trails|trailhead|picnic|day[- ]use|overlook|interpretive|ohv|scenic|winter|snowmobile)\b",
    re.I,
)

USFS_PAGE_STRONG_HORSE_CAMPING_PATTERNS = [
    re.compile(r"\b\d+\s+(?:sites?|campsites?)\s+for\s+horse\s+campers?\b", re.I),
    re.compile(r"\bhorse\s+campers?\b", re.I),
    re.compile(r"\bhorse\s+camp(?:ground|ing)?\b", re.I),
    re.compile(r"\bequestrian\s+camp(?:ground|ing)?\b", re.I),
    re.compile(r"\bstock\s+camp(?:ground|ing)?\b", re.I),
    re.compile(r"\bhorse\s*/\s*pack\s+animals?\s+(?:are\s+)?allowed\b", re.I),
    re.compile(r"\b(?:corrals?|stalls?|mangers?|high\s*lines?|hitching\s+rails?|tie\s+rails?)\b", re.I),
]

USFS_PAGE_OVERNIGHT_CAMPING_PATTERNS = [
    re.compile(r"\bcampground\b", re.I),
    re.compile(r"\bcampsites?\b", re.I),
    re.compile(r"\bcamping\s+units?\b", re.I),
    re.compile(r"\bsingle\s+(?:camping\s+)?units?\b", re.I),
    re.compile(r"\bgroup\s+(?:camping\s+)?sites?\b", re.I),
    re.compile(r"\bfirst[- ]come,?\s+first[- ]serve", re.I),
    re.compile(r"\bnon[- ]reservation\s+campground\b", re.I),
]

USFS_PAGE_NON_OVERNIGHT_PATTERNS = [
    re.compile(r"\bday\s+use\s+only\b", re.I),
    re.compile(r"\bno\s+overnight\s+camp(?:ing)?\b", re.I),
    re.compile(r"\bcamping\s+is\s+not\s+allowed\b", re.I),
]


def _usfs_polite_website_pause():
    global _last_usfs_website_request_at
    target_delay = USFS_WEBSITE_REQUEST_DELAY_SECONDS + random.uniform(0.0, USFS_WEBSITE_REQUEST_JITTER_SECONDS)
    now = time.monotonic()
    wait = target_delay - (now - _last_usfs_website_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_usfs_website_request_at = time.monotonic()


def _usfs_retry_after_seconds(response, attempt):
    retry_after = str(response.headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        return min(max(int(retry_after), 5), USFS_WEBSITE_MAX_429_RETRY_SECONDS)
    backoff = (15, 30, 60)
    return min(backoff[min(attempt, len(backoff) - 1)], USFS_WEBSITE_MAX_429_RETRY_SECONDS)


def _usfs_fetch_html(url, retries=3):
    """Fetch one Forest Service page with adaptive 429/403 protection."""
    global _usfs_website_consecutive_403_failures, _usfs_website_throttle_events

    # Existing callers commonly request two attempts. Allow enough attempts for
    # the intended 15s -> 30s -> 60s 429 backoff while keeping the request bounded.
    max_attempts = max(4, retries)
    retried_403 = False

    for attempt in range(max_attempts):
        try:
            _usfs_polite_website_pause()
            response = requests.get(
                url,
                timeout=20,
                headers={"User-Agent": USFS_WEBSITE_USER_AGENT},
                allow_redirects=True,
            )
            if response.status_code == 200 and response.text:
                _usfs_website_consecutive_403_failures = 0
                return response.url, response.text

            if response.status_code == 429:
                _usfs_website_throttle_events += 1
                if attempt < max_attempts - 1:
                    wait = _usfs_retry_after_seconds(response, attempt)
                    print(f"  USFS website rate limited for {url}; waiting {wait}s before retry")
                    time.sleep(wait)
                    continue
                print(f"  USFS website HTTP 429 for {url} after all retries")
                return "", ""

            if response.status_code == 403:
                _usfs_website_throttle_events += 1
                if not retried_403:
                    retried_403 = True
                    print(
                        f"  USFS website HTTP 403 for {url}; "
                        f"waiting {USFS_WEBSITE_403_RETRY_SECONDS}s before one retry"
                    )
                    time.sleep(USFS_WEBSITE_403_RETRY_SECONDS)
                    continue

                _usfs_website_consecutive_403_failures += 1
                print(
                    f"  USFS website HTTP 403 for {url} after retry "
                    f"({_usfs_website_consecutive_403_failures}/"
                    f"{USFS_WEBSITE_MAX_CONSECUTIVE_403_FAILURES} consecutive blocked requests)"
                )
                if _usfs_website_consecutive_403_failures >= USFS_WEBSITE_MAX_CONSECUTIVE_403_FAILURES:
                    raise USFSWebsiteBlocked(
                        "Forest Service website returned repeated HTTP 403 responses"
                    )
                return "", ""

            print(f"  USFS website HTTP {response.status_code} for {url}")
            return "", ""
        except USFSWebsiteBlocked:
            raise
        except Exception as e:
            print(f"  USFS website request error (attempt {attempt + 1}/{max_attempts}) for {url}: {e}")
            if attempt + 1 < max_attempts:
                time.sleep(3 * (attempt + 1))
    return "", ""

def _usfs_html_to_text(raw_html):
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw_html or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<title[\s\S]*?</title>", " ", text, flags=re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|li|h[1-6]|section|article|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _usfs_extract_h1(raw_html):
    match = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", raw_html or "", flags=re.I)
    if not match:
        return ""
    return re.sub(r"\s+", " ", _usfs_html_to_text(match.group(1))).strip()


def _usfs_extract_title_parts(raw_html):
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", raw_html or "", flags=re.I)
    if not match:
        return []
    title = _usfs_html_to_text(match.group(1))
    return [part.strip() for part in title.split("|") if part.strip()]


def _usfs_extract_forest_name(raw_html):
    parts = _usfs_extract_title_parts(raw_html)
    if parts and parts[-1].lower() == "forest service":
        parts = parts[:-1]
    if len(parts) >= 2 and "national forest" in parts[0].lower():
        return parts[0]
    text = _usfs_html_to_text(raw_html)
    match = re.search(r"\b([A-Z][A-Za-z' -]+ National Forest)\b", text)
    return match.group(1).strip() if match else "U.S. Forest Service"


def _usfs_extract_page_coordinates(text):
    lat_match = re.search(r"\bLatitude\s*:?\s*(-?\d+(?:\.\d+)?)\b", text, flags=re.I)
    lon_match = re.search(r"\bLongitude\s*:?\s*(-?\d+(?:\.\d+)?)\b", text, flags=re.I)
    if not lat_match or not lon_match:
        return None, None
    try:
        lat = float(lat_match.group(1))
        lon = float(lon_match.group(1))
    except ValueError:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def _usfs_extract_official_detail_links(raw_html, base_url):
    links = []
    seen = set()
    anchor_re = re.compile(r"<a\b[^>]*href=[\"']([^\"'#?]+)(?:[?#][^\"']*)?[\"'][^>]*>([\s\S]*?)</a>", re.I)
    for match in anchor_re.finditer(raw_html or ""):
        href = html.unescape(match.group(1)).strip()
        label = _usfs_html_to_text(match.group(2))
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != "www.fs.usda.gov":
            continue
        if not re.match(r"^/r\d{2}/[^/]+/recreation/[^/]+", parsed.path):
            continue
        if "/recreation/opportunities" in parsed.path:
            continue
        if "/recreation/areas/" in parsed.path:
            # Area pages can contain broad recreation summaries but are not
            # individual overnight camp listings. Detail pages remain eligible.
            continue
        if re.match(r"^/r\d{2}/[^/]+/recreation/(?:camping-cabins|epic-adventures|trails)/?$", parsed.path):
            continue

        blob = " ".join([
            parsed.path.replace("-", " ").replace("/", " "),
            label,
        ])
        if not USFS_DETAIL_LINK_INCLUDE_RE.search(blob):
            continue
        if USFS_DETAIL_LINK_EXCLUDE_RE.search(blob) and not USFS_DETAIL_LINK_STRONG_CAMP_RE.search(blob):
            continue

        normalized = f"https://www.fs.usda.gov{parsed.path}"
        if normalized not in seen:
            seen.add(normalized)
            links.append(normalized)
    return links

def _usfs_resolve_forest_base_url(url):
    parsed = urlparse(str(url or ""))
    match = re.match(r"^/(r\d{2})/([^/]+)(?:/|$)", parsed.path)
    if parsed.netloc.lower() == "www.fs.usda.gov" and match:
        return f"https://www.fs.usda.gov/{match.group(1)}/{match.group(2)}"

    # Legacy recarea URLs expose the forest slug but not the new /rNN/ prefix.
    # Follow one representative site URL per forest and derive the modern base
    # from the redirect target or links in the returned official page.
    final_url, raw_html = _usfs_fetch_html(str(url or ""), retries=2)
    if final_url:
        match = re.match(r"^/(r\d{2})/([^/]+)(?:/|$)", urlparse(final_url).path)
        if match:
            return f"https://www.fs.usda.gov/{match.group(1)}/{match.group(2)}"
    if raw_html:
        href_re = re.compile(r"href=[\"'](/r\d{2}/[^/]+)/(?:recreation|home|about|visit)?", re.I)
        match = href_re.search(raw_html)
        if match:
            return urljoin(USFS_WEBSITE_BASE, match.group(1))
    return ""


def _usfs_forest_bases_from_primary_camps(camps):
    samples_by_slug = {}
    direct_bases = set()
    for camp in camps:
        website = str(camp.get("website") or "")
        parsed = urlparse(website)
        direct = re.match(r"^/(r\d{2})/([^/]+)(?:/|$)", parsed.path)
        if parsed.netloc.lower() == "www.fs.usda.gov" and direct:
            direct_bases.add(f"https://www.fs.usda.gov/{direct.group(1)}/{direct.group(2)}")
            continue
        legacy = re.match(r"^/recarea/([^/]+)(?:/|$)", parsed.path)
        if parsed.netloc.lower() == "www.fs.usda.gov" and legacy:
            samples_by_slug.setdefault(legacy.group(1), website)

    bases = set(direct_bases)
    for slug, sample_url in sorted(samples_by_slug.items()):
        base = _usfs_resolve_forest_base_url(sample_url)
        if base:
            bases.add(base)
    return sorted(bases)


def _usfs_page_has_overnight_horse_camping_signal(text):
    if any(pattern.search(text) for pattern in USFS_PAGE_NON_OVERNIGHT_PATTERNS):
        return False
    has_horse_signal = any(pattern.search(text) for pattern in USFS_PAGE_STRONG_HORSE_CAMPING_PATTERNS)
    has_overnight_signal = any(pattern.search(text) for pattern in USFS_PAGE_OVERNIGHT_CAMPING_PATTERNS)
    return has_horse_signal and has_overnight_signal


def _usfs_page_accommodations(text):
    accommodations = ["Trails"]
    if re.search(r"\b(?:corrals?|pens?|paddocks?|mangers?|hitching\s+rails?|tie\s+rails?|high\s*lines?)\b", text, flags=re.I):
        accommodations.append("Corrals")
    if re.search(r"\b(?:rv|trailer|motor\s*home|motorhome)\b", text, flags=re.I):
        accommodations.append("Big Rig")
    return list(dict.fromkeys(accommodations))


def _usfs_page_hookups(text):
    hookups = []
    if re.search(r"\bpotable\s+water\s+is\s+available\b|\bdrinking\s+water\b", text, flags=re.I):
        hookups.append("Water")
    if re.search(r"\belectric(?:al)?\s+hookups?\b|\b30\s*amp\b|\b30a\b", text, flags=re.I):
        hookups.append("30A")
    if re.search(r"\b50\s*amp\b|\b50a\b", text, flags=re.I):
        hookups.append("50A")
    if re.search(r"\bsewer\s+hookups?\b", text, flags=re.I):
        hookups.append("Sewer")
    return list(dict.fromkeys(hookups))


def _usfs_month_after_label(text, label):
    match = re.search(label + r".{0,80}?\b(" + "|".join(re.escape(k) for k in MONTH_MAP.keys()) + r")\b", text, flags=re.I | re.S)
    return MONTH_MAP.get(match.group(1).lower(), 0) if match else 0


def _usfs_site_id_from_page_url(url):
    parsed = urlparse(url)
    raw = re.sub(r"^/", "", parsed.path.lower())
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return f"usfs-page-{raw}"


def _usfs_description_from_page(text, name):
    desc = text
    title_match = re.search(r"^" + re.escape(name) + r"$", desc, flags=re.M)
    if title_match:
        desc = desc[title_match.end():]
    stop_labels = [
        "Reservations", "General Information", "Getting There",
        "Facility and Amenity Information", "Recreation Opportunities",
        "Recreation Groups", "Last updated",
    ]
    for label in stop_labels:
        idx = desc.find(label)
        if 0 < idx < 1800:
            desc = desc[:idx]
            break
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:2000]


def _usfs_build_camp_from_official_page(url, raw_html):
    text = _usfs_html_to_text(raw_html)
    name = _usfs_extract_h1(raw_html)
    if not name or name.lower() in {"recreation", "horse riding and camping"}:
        return None
    if not _usfs_page_has_overnight_horse_camping_signal(text):
        return None
    lat, lon = _usfs_extract_page_coordinates(text)
    if lat is None or lon is None:
        return None
    state = _usfs_state_from_coordinates(lat, lon)
    if not state:
        raise RuntimeError(
            "Unable to resolve a state for official Forest Service website page: "
            f"{name} ({url}; {lat:.5f}, {lon:.5f})"
        )

    forest = _usfs_extract_forest_name(raw_html)
    description = _usfs_description_from_page(text, name)
    if not description:
        description = "Official U.S. Forest Service horse-camping site."

    return {
        "id": _usfs_site_id_from_page_url(url),
        "name": name,
        "location": forest or f"U.S. Forest Service, {state}",
        "state": state,
        "latitude": lat,
        "longitude": lon,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": _usfs_page_hookups(text),
        "accommodations": _usfs_page_accommodations(text),
        "maxRigLength": 0,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": url,
        "description": description,
        "isVerified": True,
        "seasonStart": _usfs_month_after_label(text, r"(?:Seasons?\s+of\s+Use|Open\s+Season)"),
        "seasonEnd": _usfs_month_after_label(text, r"(?:closes?|through|until|to)"),
        "hasWashRack": bool(re.search(r"\bwash\s+rack\b", text, flags=re.I)),
        "hasDumpStation": bool(re.search(r"\bdump\s+station\b", text, flags=re.I)),
        "hasWifi": bool(re.search(r"\b(?:wi[- ]?fi|internet)\b", text, flags=re.I)),
        "hasBathhouse": bool(re.search(r"\b(?:shower|bathhouse|flush\s+toilet)\b", text, flags=re.I)),
        "pullThroughAvailable": bool(re.search(r"\bpull[- ]through\b", text, flags=re.I)),
        "imageColors": ["6A1B9A", "CE93D8"],
        "photoURLs": [],
        "source": "U.S. Forest Service",
    }


def _usfs_existing_page_urls(camps):
    urls = set()
    for camp in camps:
        website = str(camp.get("website") or "")
        parsed = urlparse(website)
        if parsed.netloc.lower() == "www.fs.usda.gov":
            urls.add(f"https://www.fs.usda.gov{parsed.path}".rstrip("/"))
    return urls


def _fetch_usfs_official_website_supplement_live(primary_camps):
    """Crawl official Forest Service activity pages for missed horse camp pages.

    This is not a manual allowlist. It discovers official fs.usda.gov forest
    Horse Riding and Camping pages from forests already represented in the USFS
    ArcGIS result, follows their listed official recreation detail pages, and
    imports only pages with explicit overnight horse-camping evidence plus
    coordinates.
    """
    forest_bases = _usfs_forest_bases_from_primary_camps(primary_camps)
    if not forest_bases:
        print("  U.S. Forest Service website supplement: no forest bases discovered")
        return []

    existing_urls = _usfs_existing_page_urls(primary_camps)
    discovered_detail_urls = []
    seen_detail_urls = set(existing_urls)
    per_forest_limit = USFS_WEBSITE_MAX_DETAIL_PAGES_PER_FOREST

    for base in forest_bases:
        forest_detail_count = 0
        for page_index in range(USFS_WEBSITE_MAX_ACTIVITY_PAGES):
            activity_url = f"{base}{USFS_HORSE_ACTIVITY_PATH}"
            if page_index:
                activity_url += f"?page=%2C{page_index}"
            final_url, raw_html = _usfs_fetch_html(activity_url, retries=2)
            if not raw_html:
                break
            links = _usfs_extract_official_detail_links(raw_html, final_url or activity_url)
            for link in links:
                normalized = link.rstrip("/")
                if normalized in seen_detail_urls:
                    continue
                seen_detail_urls.add(normalized)
                discovered_detail_urls.append(link)
                forest_detail_count += 1
                if forest_detail_count >= per_forest_limit:
                    break

            text = _usfs_html_to_text(raw_html)
            showing = re.search(r"Showing:\s*(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+results", text, flags=re.I)
            if forest_detail_count >= per_forest_limit:
                break
            if not showing or int(showing.group(2)) >= int(showing.group(3)):
                break

    supplemental = []
    seen_ids = set()
    for detail_url in discovered_detail_urls:
        final_url, raw_html = _usfs_fetch_html(detail_url, retries=2)
        if not raw_html:
            continue
        final_url = (final_url or detail_url).split("?", 1)[0].rstrip("/")
        camp = _usfs_build_camp_from_official_page(final_url, raw_html)
        if not camp:
            continue
        if camp["id"] in seen_ids:
            continue
        seen_ids.add(camp["id"])
        supplemental.append(camp)

    print(
        "  U.S. Forest Service website supplement: "
        f"{len(forest_bases)} forests, {len(discovered_detail_urls)} official detail pages checked, "
        f"{len(supplemental)} accepted"
    )
    return supplemental


def _extract_usfs_website_supplement(feed):
    camps = feed.get("camps", []) if isinstance(feed, dict) else []
    return [
        dict(camp)
        for camp in camps
        if isinstance(camp, dict)
        and camp.get("source") == "U.S. Forest Service"
        and str(camp.get("id") or "").startswith("usfs-page-")
    ]


def _load_usfs_website_supplement_cache():
    try:
        cached = json.loads(USFS_WEBSITE_SUPPLEMENT_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(cached, list) and cached:
            print(f"  Cached U.S. Forest Service website supplement: {len(cached)} listings")
            return [dict(camp) for camp in cached if isinstance(camp, dict)]
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  WARNING: Could not read USFS website supplement cache: {e}")

    # First guarded run only: seed from the last known-good feed before the
    # Aug. 13 Forest Service blocking event. Checkout uses full history so this
    # remains available without another network call.
    try:
        raw = subprocess.check_output(
            ["git", "show", f"{USFS_WEBSITE_BOOTSTRAP_GOOD_FEED_COMMIT}:camps.json"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.STDOUT,
        )
        previous = _extract_usfs_website_supplement(json.loads(raw))
        if previous:
            print(
                "  Bootstrapped U.S. Forest Service website supplement from "
                f"last known-good feed: {len(previous)} listings"
            )
            return previous
    except Exception as e:
        print(f"  WARNING: Could not bootstrap prior USFS supplement from git history: {e}")

    return []


def _write_usfs_website_supplement_cache(camps):
    try:
        USFS_WEBSITE_SUPPLEMENT_CACHE_FILE.write_text(
            json.dumps(camps, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  U.S. Forest Service website supplement cache saved: {len(camps)} listings")
    except Exception as e:
        print(f"  WARNING: Could not write USFS website supplement cache: {e}")


def fetch_usfs_official_website_supplement(primary_camps):
    """Fetch the USFS website supplement without publishing a throttled collapse."""
    global _usfs_website_consecutive_403_failures, _usfs_website_throttle_events
    _usfs_website_consecutive_403_failures = 0
    _usfs_website_throttle_events = 0
    previous = _load_usfs_website_supplement_cache()

    try:
        current = _fetch_usfs_official_website_supplement_live(primary_camps)
    except USFSWebsiteBlocked as e:
        if not previous:
            raise RuntimeError(
                "USFS website crawl was blocked and no previous accepted supplement is available; "
                "refusing to publish an incomplete USFS feed."
            ) from e
        print(
            "  WARNING: USFS website crawl stopped after sustained 403 blocking; "
            f"retaining {len(previous)} listings from the previous accepted supplement."
        )
        _write_usfs_website_supplement_cache(previous)
        return previous

    if previous and _usfs_website_throttle_events:
        minimum_expected = math.ceil(len(previous) * USFS_WEBSITE_SUPPLEMENT_MIN_RETAIN_RATIO)
        if len(current) < minimum_expected:
            print(
                "  WARNING: USFS website supplement was throttled and fell from "
                f"{len(previous)} previous listings to {len(current)} current listings; "
                f"retaining the previous supplement (minimum accepted after throttling: {minimum_expected})."
            )
            _write_usfs_website_supplement_cache(previous)
            return previous

    _write_usfs_website_supplement_cache(current)
    return current

# ── NPS ────────────────────────────────────────────────────────────────
def fetch_nps_state(state):
    camps = []
    headers = {"X-Api-Key": NPS_KEY}
    params  = {"stateCode": state, "limit": 100, "start": 0, "fields": "images"}

    data = safe_get(f"{NPS_BASE}/campgrounds", headers=headers, params=params)
    if not data:
        return camps

    for c in data.get("data", []):
        desc       = c.get("description", "")
        amenities  = c.get("amenities", {})
        blob       = " ".join([
            desc,
            amenities.get("horseTrailsOnsite", ""),
            amenities.get("corralOrPaddockOnsite", ""),
            amenities.get("stableNearby", ""),
        ])

        if not is_equestrian(blob):
            continue

        try:
            lat = float(c.get("latitude", 0))
            lng = float(c.get("longitude", 0))
        except:
            continue
        if abs(lat) < 0.1 or abs(lng) < 0.1:
            continue

        addr    = (c.get("addresses") or [{}])[0]
        city    = addr.get("city", "")
        fee     = 0.0
        fees    = c.get("fees") or []
        if fees:
            try: fee = float(fees[0].get("cost", 0))
            except: pass

        # Hookups — NPS values are "Yes - seasonal", "Yes - year round", or "No"
        def nps_yes(val): return str(val or "").startswith("Yes")
        hookups = []
        if nps_yes(amenities.get("electricalHookups")): hookups.append("30A")
        if nps_yes(amenities.get("waterHookups")):      hookups.append("Water")
        if nps_yes(amenities.get("sewerHookups")):      hookups.append("Sewer")
        # potableWater — only add if starts with "Yes" (not "No water" or "Water, but not potable")
        potable = " ".join(amenities.get("potableWater") or [])
        if potable.startswith("Yes"):                   hookups.append("Water")
        # Deduplicate in case both waterHookups and potableWater say yes
        hookups = list(dict.fromkeys(hookups))
        if not hookups: hookups.append("No Hookups")

        accommodations = []
        if nps_yes(amenities.get("corralOrPaddockOnsite")): accommodations.append("Corrals")
        if nps_yes(amenities.get("stableNearby")):          accommodations.append("Stalls")
        if nps_yes(amenities.get("horseTrailsOnsite")):     accommodations.append("Trails")

        contacts = c.get("contacts", {})
        phones   = contacts.get("phoneNumbers", [])
        phone    = phones[0].get("phoneNumber", "") if phones else ""

        # Season — NPS API doesn't provide reliable open/close months
        # operatingHours contains daily schedule dates, not seasonal months
        season_start, season_end = 0, 0

        camps.append({
            "id":                  f"nps-{c['id']}",
            "name":                c.get("name", "NPS Camp"),
            "location":            f"{city}, {state}".strip(", "),
            "state":               state,
            "latitude":            lat,
            "longitude":           lng,
            "pricePerNight":       fee,
            "horseFeePerNight":    0.0,
            "hookups":             hookups,
            "accommodations":      list(dict.fromkeys(accommodations)),
            "maxRigLength":        0,
            "stallCount":          0,
            "paddockCount":        0,
            "phone":               phone,
            "website":             c.get("url", f"https://www.nps.gov/{c.get('parkCode', '')}/"),
            "description":         desc[:2000],
            "isVerified":          False,
            "seasonStart":         season_start,
            "seasonEnd":           season_end,
            "hasWashRack":         False,
            "hasDumpStation":      nps_yes(amenities.get("dumpStation")),
            "hasWifi":             nps_yes(amenities.get("internetConnectivity")),
            "hasBathhouse":        (any("flush" in t.lower() for t in (amenities.get("toilets") or [])) or any(str(s).strip().lower() not in ("none", "") for s in (amenities.get("showers") or []) if s)),
            "pullThroughAvailable": nps_yes(amenities.get("pullThroughCampsites")),
            "imageColors":         ["4A7FA5", "5C7A4E"],
            "photoURLs":           [img["url"] for img in (c.get("images") or []) if img.get("url")][:6],
            "source":              "NPS",
        })

    return camps



# ── CALIFORNIA STATE PARKS ─────────────────────────────────────────────
CA_STATE_PARKS_BASE = "https://services2.arcgis.com/AhxrK3F6WM8ECvDi/arcgis/rest/services/Campgrounds/FeatureServer/0/query"
CA_STATE_PARKS_KEYWORDS = [
    "horse", "equestrian", "bridle", "bridle trail", "stock",
    "corral", "stall", "tie rail", "highline", "paddock", "equine", "mule"
]

def _is_ca_state_park_equestrian(attrs):
    text_blob = " ".join(str(attrs.get(k, "") or "") for k in [
        "Campground", "TYPE", "SUBTYPE", "DETAIL", "UNITNAME"
    ]).lower()
    return any(k in text_blob for k in CA_STATE_PARKS_KEYWORDS)

def _ca_state_park_accommodations(attrs):
    text_blob = " ".join(str(attrs.get(k, "") or "") for k in [
        "Campground", "TYPE", "SUBTYPE", "DETAIL", "UNITNAME"
    ]).lower()

    accommodations = []
    if "stall" in text_blob:
        accommodations.append("Stalls")
    if any(k in text_blob for k in ["corral", "paddock", "tie rail", "highline"]):
        accommodations.append("Corrals")
    if any(k in text_blob for k in ["trail", "bridle", "horse", "equestrian"]):
        accommodations.append("Trails")

    return list(dict.fromkeys(accommodations)) or ["Trails"]

def fetch_ca_state_parks():
    """Fetch California State Parks campgrounds from the official ArcGIS layer.

    The public dataset covers all state park campgrounds, so this importer keeps
    only equestrian-relevant rows using campground/unit/type/detail keyword
    matching. That makes the first pass conservative while staying fully on
    official machine-readable data.
    """
    camps = []
    seen_ids = set()
    offset = 0
    page_size = 2000

    while True:
        params = {
            "where": "1=1",
            "outFields": "FID,Campground,GISID,TYPE,SUBTYPE,DETAIL,UNITNAME,WHAT3WORD_ADDRESS",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        }
        data = safe_get(CA_STATE_PARKS_BASE, params=params)
        if not data:
            break

        features = data.get("features", [])
        if not features:
            break

        for feature in features:
            attrs = feature.get("attributes") or {}
            geom = feature.get("geometry") or {}
            lat = geom.get("y")
            lng = geom.get("x")

            try:
                lat = float(lat)
                lng = float(lng)
            except (TypeError, ValueError):
                continue

            if abs(lat) < 0.1 or abs(lng) < 0.1:
                continue
            if not _is_ca_state_park_equestrian(attrs):
                continue

            gisid = str(attrs.get("GISID") or attrs.get("FID") or "").strip()
            campground = str(attrs.get("Campground") or "").strip()
            unit_name = str(attrs.get("UNITNAME") or "").strip()
            type_name = str(attrs.get("TYPE") or "").strip()
            subtype = str(attrs.get("SUBTYPE") or "").strip()
            detail = str(attrs.get("DETAIL") or "").strip()

            cid = f"ca-sp-{gisid or f'{lat:.5f},{lng:.5f}'}"
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            name = campground or unit_name or "California State Park Campground"
            location = f"{unit_name}, CA" if unit_name else "CA"

            detail_parts = [p for p in [type_name, subtype, detail] if p]
            detail_text = " • ".join(detail_parts)
            desc = f"California State Parks campground in {unit_name}." if unit_name else "California State Parks campground."
            if detail_text:
                desc += f" {detail_text}."
            desc += " Imported from the official California State Parks Campgrounds layer; verify horse amenities before arrival."

            camps.append({
                "id": cid,
                "name": name,
                "location": location,
                "state": "CA",
                "latitude": lat,
                "longitude": lng,
                "pricePerNight": 0.0,
                "horseFeePerNight": 0.0,
                "hookups": [],
                "accommodations": _ca_state_park_accommodations(attrs),
                "maxRigLength": 0,
                "stallCount": 0,
                "paddockCount": 0,
                "phone": "",
                "website": "",
                "description": desc[:2000],
                "isVerified": False,
                "seasonStart": 0,
                "seasonEnd": 0,
                "hasWashRack": False,
                "hasDumpStation": False,
                "hasWifi": False,
                "hasBathhouse": False,
                "pullThroughAvailable": False,
                "imageColors": ["5C7A4E", "D4A853"],
                "photoURLs": [],
                "source": "State Parks",
            })

        if len(features) < page_size:
            break
        offset += page_size
        time.sleep(0.3)

    print(f"  CA State Parks: {len(camps)} equestrian candidates")
    return camps



IL_HORSEBACK_URL = "https://dnr.illinois.gov/recreation/horsebackriding.html"


def _strip_html_basic(text):
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _il_slug_candidates(name):
    base = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    candidates = [base]
    replacements = {
        "statepark": "",
        "staterecreationarea": "",
        "statefishwildlifearea": "",
        "stateforest": "",
        "statenaturalarea": "",
        "county": "county",
        "co": "",
        "donnellystatefishwildlifearea": "donnelly",
        "andleaqua": "leaquana",
    }
    for old, new in replacements.items():
        if old in base:
            candidates.append(base.replace(old, new))
    manual = {
        "chainolakesstatepark": ["chainolakes"],
        "desplainesstatefishwildlifearea": ["desplaines"],
        "jimedgarpanthercreekstatefishwildlifearea": ["jimedgarpanthercreek"],
        "jubileecollegestatepark": ["jubileecollege"],
        "lakeleaquanastaterecreationarea": ["lakeleaquana"],
        "morrisonrockwoodstatepark": ["morrisonrockwood"],
        "putnamcountycodonneIIystatefishwildlifearea": ["putnamcounty", "putnam"],
        "putnamcountycodonneIIystatefishwildlifearea": ["putnamcounty", "putnam"],
        "putnamcountycodonneIIy": ["putnamcounty", "putnam"],
        "putnamcountycodonneIIystat": ["putnamcounty", "putnam"],
        "putnamcountycodonneIIystatefishwildlifearea": ["putnamcounty", "putnam"],
        "putnamcountycodonneIlystatefishwildlifearea": ["putnamcounty", "putnam"],
        "pyramidstaterecreationarea": ["pyramid"],
        "ramseylakestaterecreationarea": ["ramseylake"],
        "randolphcountystaterecreationarea": ["randolphcounty"],
        "salinecountystatefishwildlifearea": ["salinecounty"],
        "sangchrislakestaterecreationarea": ["sangchris"],
        "stephanaforbesstaterecreationarea": ["stephanaforbes"],
        "weinbergkingstatefishwildlifearea": ["weinbergking"],
        "wolfcreekstatepark": ["wolfcreek"],
        "middleforkstatefishwildlifearea": ["middlefork"],
        "greenriverstatewildlifearea": ["greenriver"],
        "bigriverstateforest": ["bigriver"],
        "franklincreekstatenaturalarea": ["franklincreek"],
        "kankakeeriverstatepark": ["kankakeeriver"],
        "matthiessenstatepark": ["matthiessen"],
        "moraineviewstaterecreationarea": ["moraineview"],
        "argylelakestatepark": ["argylelake"],
        "ferneclyffestatepark": ["ferneclyffe"],
        "giantcitystatepark": ["giantcity"],
        "redhillsstatepark": ["redhills"],
        "rockcutstatepark": ["rockcut"],
        "sandridgestateforest": ["sandridge"],
        "siloamspringsstatepark": ["siloamsprings"],
        "hennepincanalstatetrail": ["hennepincanal"],
    }
    for k, vals in manual.items():
        if base == k:
            candidates = vals + candidates
            break
    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fetch_text(url):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "HorseCamp/1.0"})
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


def _il_extract_phone_coords(page_text):
    text = _strip_html_basic(page_text)
    phone = ""
    m = re.search(r"(?:Daily\s+Phone:|Phone:)\s*([0-9\-\(\) ]{7,})", text, flags=re.I)
    if m:
        phone = m.group(1).strip()

    lat = lng = None

    # Prefer explicitly labeled coordinates. Some IL DNR pages present longitude
    # as a positive number with a trailing W, which must be negated.
    mlat = re.search(r"(?:Park\s+)?Latitude[:\s]*([0-9]+(?:\.[0-9]+)?)\s*([NS])?", text, flags=re.I)
    mlng = re.search(r"(?:Park\s+)?Longitude[:\s]*(-?[0-9]+(?:\.[0-9]+)?)\s*([EW])?", text, flags=re.I)
    if mlat and mlng:
        lat = float(mlat.group(1))
        lng = float(mlng.group(1))
        lat_dir = (mlat.group(2) or "N").upper()
        lng_dir = (mlng.group(2) or "W").upper()
        if lat_dir == "S":
            lat = -abs(lat)
        else:
            lat = abs(lat)
        if lng_dir == "W":
            lng = -abs(lng)
        else:
            lng = abs(lng)
    else:
        # Fallback for pages that expose signed decimal coordinates directly.
        coords = re.findall(r"\b(-?\d{1,3}\.\d{3,})\b", text)
        if len(coords) >= 2:
            vals = [float(x) for x in coords[-2:]]
            if -90 <= vals[0] <= 90 and -180 <= vals[1] <= 180:
                lat, lng = vals[0], vals[1]

    # Illinois park pages usually refer to west longitudes; if we parsed a positive
    # longitude in the normal Illinois range, flip it to west as a safety net.
    if lat is not None and lng is not None and 36 <= lat <= 43 and 87 <= lng <= 92:
        lng = -lng

    return phone, lat, lng, text


def _il_extract_price(text):
    m = re.search(r"cost per night is \$(\d+(?:\.\d+)?)", text, flags=re.I)
    if not m:
        m = re.search(r"\$(\d+(?:\.\d+)?)\s*/?\s*night", text, flags=re.I)
    return float(m.group(1)) if m else 0.0


def _il_hookups(text):
    low = text.lower()
    hookups = []

    # Be conservative for Illinois. Generic mentions of electricity on a park page
    # do not reliably mean the equestrian campground has 30A hookups.
    power_terms = [
        "30 amp", "30-amp", "30a",
        "electrical hookup", "electrical hookups",
        "electric hookup", "electric hookups",
        "rv hookups", "hookups with electricity",
        "water and electricity", "water & electricity",
        "electric campsites", "electric sites",
    ]
    if any(term in low for term in power_terms):
        hookups.append("30A")

    water_terms = [
        "water hookup", "water hookups", "hydrant", "hydrants",
        "water available", "potable water", "drinking water",
        "water spigot", "water spigots", "water at campground",
        "water at the campground", "water in campground",
        "water in the campground",
    ]
    if any(term in low for term in water_terms):
        hookups.append("Water")

    return hookups


def _il_accommodations(text):
    low = text.lower()
    acc = ["Trails"]
    if "hitching" in low or "tie line" in low or "tie lines" in low:
        acc.append("Highlines")
    if "corral" in low:
        acc.append("Corrals")
    if "stall" in low:
        acc.append("Stalls")
    return list(dict.fromkeys(acc))


def fetch_il_state_parks():
    """Fetch Illinois official equestrian-camping sites from IDNR.

    The statewide IDNR horseback-riding page is not a clean HTML table, so this
    parser uses the official park links plus the nearby Yes/No text that follows
    each site name on the page.
    """
    html = _fetch_text(IL_HORSEBACK_URL)
    if not html:
        print("  Illinois State Parks: statewide page unavailable")
        return []

    yes_sites = []
    anchor_re = re.compile(r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>([^<]+)</a>", flags=re.I)
    for m in anchor_re.finditer(html):
        href, site_name = m.group(1), _strip_html_basic(m.group(2)).strip()
        low_name = site_name.lower()
        if not site_name or low_name in ("horseback riding", "contact us", "illinois.gov"):
            continue
        if not any(k in low_name for k in ["state park", "state forest", "state trail", "state recreation area", "state fish", "wildlife area", "state natural area"]):
            continue
        tail = _strip_html_basic(html[m.end():m.end()+220])
        tail = re.sub(r'\s+', ' ', tail).strip().lower()
        if not tail.startswith('yes'):
            continue
        full_href = href if href.startswith('http') else ('https://dnr.illinois.gov' + href)
        yes_sites.append((site_name, full_href))

    # Deduplicate while preserving order.
    seen = set()
    deduped_sites = []
    for site_name, full_href in yes_sites:
        key = site_name.lower()
        if key not in seen:
            seen.add(key)
            deduped_sites.append((site_name, full_href))

    camps = []
    for site_name, main_url in deduped_sites:
        main_text = _fetch_text(main_url)
        if not main_text:
            continue

        phone, lat, lng, main_plain = _il_extract_phone_coords(main_text)

        slug = ""
        mslug = re.search(r'/park(?:s/(?:about|activity|camp))?/park\.([a-z0-9\-]+)\.html', main_url, flags=re.I)
        if mslug:
            slug = mslug.group(1)
        else:
            candidates = _il_slug_candidates(site_name)
            slug = candidates[0] if candidates else ""

        about_url = act_url = camp_url = ""
        about_text = act_text = camp_text = ""
        if slug:
            about_url = f"https://dnr.illinois.gov/parks/about/park.{slug}.html"
            act_url = f"https://dnr.illinois.gov/parks/activity/park.{slug}.html"
            camp_url = f"https://dnr.illinois.gov/parks/camp/park.{slug}.html"
            about_text = _fetch_text(about_url)
            act_text = _fetch_text(act_url)
            camp_text = _fetch_text(camp_url)

            # Some direct links already point to the activity/camp page; fill the
            # missing main page using the standard park path when possible.
            if (lat is None or lng is None) and slug:
                fallback_main = _fetch_text(f"https://dnr.illinois.gov/parks/park.{slug}.html")
                if fallback_main:
                    main_text = fallback_main
                    phone, lat, lng, main_plain = _il_extract_phone_coords(fallback_main)
                    main_url = f"https://dnr.illinois.gov/parks/park.{slug}.html"

        if lat is None or lng is None:
            continue

        combined_text = " ".join([_strip_html_basic(x) for x in [main_text, about_text, act_text, camp_text] if x])
        lower = combined_text.lower()
        site_type = "Illinois State Park"
        if "state fish" in site_name.lower() or "wildlife" in site_name.lower():
            site_type = "Illinois State Fish & Wildlife Area"
        elif "state forest" in site_name.lower():
            site_type = "Illinois State Forest"
        elif "state trail" in site_name.lower():
            site_type = "Illinois State Trail"
        elif "recreation area" in site_name.lower():
            site_type = "Illinois State Recreation Area"
        elif "state natural area" in site_name.lower():
            site_type = "Illinois State Natural Area"

        season_start, season_end = 0, 0
        if "may 1" in lower and ("october 31" in lower or "november" in lower):
            season_start = 5
            season_end = 10 if "october 31" in lower else 11
        elif "april 1" in lower and "october 31" in lower:
            season_start = 4
            season_end = 10

        camps.append({
            "id": f"il-sp-{re.sub(r'[^a-z0-9]+', '-', site_name.lower()).strip('-')}",
            "name": site_name,
            "location": f"{site_name}, IL",
            "state": "IL",
            "latitude": lat,
            "longitude": lng,
            "pricePerNight": _il_extract_price(combined_text),
            "horseFeePerNight": 0.0,
            "hookups": _il_hookups(combined_text),
            "accommodations": _il_accommodations(combined_text),
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": phone,
            "website": camp_url or act_url or about_url or main_url or IL_HORSEBACK_URL,
            "description": (f"Official Illinois DNR equestrian-camping site. {site_type}. " + combined_text)[:2000],
            "isVerified": False,
            "seasonStart": season_start,
            "seasonEnd": season_end,
            "hasWashRack": "wash rack" in lower,
            "hasDumpStation": "dump station" in lower or "sanitary dump" in lower,
            "hasWifi": "wifi" in lower or "wi-fi" in lower,
            "hasBathhouse": "shower" in lower or "flush toilets" in lower or "restrooms" in lower,
            "pullThroughAvailable": "pull through" in lower or "pull-through" in lower,
            "imageColors": ["B5543A", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
        })

    print(f"  Illinois State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_tn_state_parks():
    """Load manual TN state-park listings from data/state_parks/tn.json."""
    return load_manual_state_parks("TN")

def fetch_ar_state_parks():
    """Load manual AR state-park listings from data/state_parks/ar.json."""
    return load_manual_state_parks("AR")

def fetch_va_state_parks():
    """Load manual VA state-park listings from data/state_parks/va.json."""
    return load_manual_state_parks("VA")

def fetch_ga_state_parks():
    """Load manual GA state-park listings from data/state_parks/ga.json."""
    return load_manual_state_parks("GA")

def fetch_nc_state_parks():
    """Load manual NC state-park listings from data/state_parks/nc.json."""
    return load_manual_state_parks("NC")

def fetch_az_state_parks():
    """Load manual AZ state-park listings from data/state_parks/az.json."""
    return load_manual_state_parks("AZ")

def fetch_ny_state_parks():
    """Load manual NY state-park listings from data/state_parks/ny.json."""
    return load_manual_state_parks("NY")

def fetch_mn_state_parks():
    """Load manual MN state-park listings from data/state_parks/mn.json."""
    return load_manual_state_parks("MN")

def fetch_co_state_parks():
    """Load manual CO state-park listings from data/state_parks/co.json."""
    return load_manual_state_parks("CO")

def fetch_ct_state_parks():
    """Load manual CT state-park listings from data/state_parks/ct.json."""
    return load_manual_state_parks("CT")

def fetch_id_state_parks():
    """Load manual ID state-park listings from data/state_parks/id.json."""
    return load_manual_state_parks("ID")

def fetch_wa_state_parks():
    """Load manual WA state-park listings from data/state_parks/wa.json."""
    return load_manual_state_parks("WA")

def fetch_nm_state_parks():
    """Load manual NM state-park listings from data/state_parks/nm.json."""
    return load_manual_state_parks("NM")

def fetch_ut_state_parks():
    """Load manual UT state-park listings from data/state_parks/ut.json."""
    return load_manual_state_parks("UT")

def fetch_sc_state_parks():
    """Load manual SC state-park listings from data/state_parks/sc.json."""
    return load_manual_state_parks("SC")

# ── ALABAMA STATE PARKS ────────────────────────────────────────────────
# Alabama is discovered from the official Alabama State Parks site on every run.
# No park names, campground names, campground URLs, or amenities are hardcoded.
# Once this importer has completed successfully at least once, the previous
# verified Alabama records in camps.json are used as last-known-good protection
# if the official site is temporarily unavailable or the live result collapses.
AL_STATE_PARKS_INDEX = "https://www.alapark.com/parks"
AL_STATE_PARKS_USER_AGENT = "HorseCampDataFetcher/1.0 (+https://horsecampfinder.com/)"

AL_HORSE_CAMP_POSITIVE_PATTERNS = [
    re.compile(r"\bequestrian\s+camp(?:ground|ing|sites?|area)\b", re.I),
    re.compile(r"\bhorse\s+camp(?:ground|ing|sites?|area)\b", re.I),
    re.compile(r"\bequine\s+camp(?:ground|ing|sites?|area)\b", re.I),
    re.compile(r"\bover\s*night\s+horseback\s+camping\b", re.I),
    re.compile(r"\bovernight\s+(?:horse|equestrian|equine)\s+camping\b", re.I),
    re.compile(r"\bcamping\s+in\s+the\s+equestrian\s+area\b", re.I),
    re.compile(r"\b(?:camping|campsites?)\b.{0,120}\brestricted\s+to\s+equestrian\b", re.I | re.S),
    re.compile(r"\brestricted\s+to\s+equestrian\s+camp(?:ground|ing|sites?|area)?\b", re.I),
]

AL_HORSE_CAMP_DETAIL_PATTERNS = [
    re.compile(r"\bover\s*night\b|\bovernight\b", re.I),
    re.compile(r"\b\d+\s+(?:equestrian\s+|horse\s+|equine\s+)?(?:camp)?sites?\b", re.I),
    re.compile(r"\b(?:electric(?:al)?|water|sewer)\b.{0,80}\b(?:hook\s*ups?|service|spigots?|hydrants?)\b", re.I | re.S),
    re.compile(r"\b(?:first[- ]come|walk[- ]up|reservations?|camping\s+rate|campground\s+amenities)\b", re.I),
    re.compile(r"\bcamping\s+in\s+the\s+equestrian\s+area\b", re.I),
]

# Do not publish an area that the official site itself describes as still being
# developed into a horse campground. This keeps trail/day-use or future projects
# from being presented as established HorseCamp destinations.
AL_HORSE_CAMP_DEVELOPMENT_PATTERNS = [
    re.compile(r"\b(?:working|work)\b.{0,100}\bdevelop(?:ing|ment)?\b.{0,100}\bhorse\s+camp", re.I | re.S),
    re.compile(r"\bdevelop(?:ing|ment)?\b.{0,100}\bhorse\s+camp\s+area\b", re.I | re.S),
    re.compile(r"\bhorse\s+camp\s+area\b.{0,120}\b(?:still\s+primitive|under\s+development|being\s+developed)\b", re.I | re.S),
]

AL_HORSE_CAMP_NON_OVERNIGHT_PATTERNS = [
    re.compile(r"\bday\s+use\s+only\b", re.I),
    re.compile(r"\bno\s+overnight\s+camp(?:ing)?\b", re.I),
    re.compile(r"\bovernight\s+camping\s+is\s+not\s+allowed\b", re.I),
]


def _fetch_state_park_html(url, label="State Parks", retries=3, user_agent=None):
    """Fetch one official state-park HTML page with bounded retries."""
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=(8, 20),
                headers={"User-Agent": user_agent or AL_STATE_PARKS_USER_AGENT},
                allow_redirects=True,
            )
            if response.status_code == 200 and response.text:
                return response.url, response.text
            if response.status_code == 429 and attempt < retries:
                retry_after = str(response.headers.get("Retry-After") or "").strip()
                wait = int(retry_after) if retry_after.isdigit() else 10 * attempt
                wait = max(2, min(wait, 60))
                print(f"  {label}: HTTP 429; waiting {wait}s before retry", flush=True)
                time.sleep(wait)
                continue
            if response.status_code >= 500 and attempt < retries:
                time.sleep(3 * attempt)
                continue
            print(f"  {label}: HTTP {response.status_code} for {url}", flush=True)
            return "", ""
        except requests.RequestException as error:
            if attempt >= retries:
                print(f"  {label}: request failed for {url}: {error}", flush=True)
                return "", ""
            time.sleep(3 * attempt)
    return "", ""


def _load_previous_state_park_records(state_code, verified_only=False):
    """Read a state's previously published State Parks records from camps.json."""
    path = REPO_ROOT / "camps.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        print(f"  WARNING: Could not read previous camps.json for {state_code}: {error}")
        return []

    rows = payload.get("camps", []) if isinstance(payload, dict) else []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("source") != "State Parks" or row.get("state") != state_code:
            continue
        if verified_only and row.get("isVerified") is not True:
            continue
        result.append(dict(row))
    return result


def _guard_dynamic_state_park_result(state_code, current):
    """Protect a dynamic state source from a temporary or suspicious collapse.

    Manual state JSON can establish the first migration's rough expected count,
    but it is never used as fallback data. After a successful dynamic run, the
    prior verified records already present in camps.json become the last-known-good
    fallback. A moderate natural change is allowed; a large count drop or major ID
    churn is treated as a scraper/source failure that needs review.
    """
    previous_all = _load_previous_state_park_records(state_code, verified_only=False)
    previous_verified = _load_previous_state_park_records(state_code, verified_only=True)
    baseline = previous_verified or previous_all
    minimum_count = max(1, math.ceil(len(baseline) * 0.70)) if baseline else 1

    suspicious_identity_churn = False
    if previous_verified and current:
        previous_ids = {str(row.get("id") or "") for row in previous_verified}
        current_ids = {str(row.get("id") or "") for row in current}
        minimum_overlap = max(1, math.ceil(len(previous_ids) * 0.60))
        suspicious_identity_churn = len(previous_ids & current_ids) < minimum_overlap

    if len(current) >= minimum_count and not suspicious_identity_churn:
        return current

    reason = (
        "major campground-ID churn"
        if suspicious_identity_churn
        else f"only {len(current)} records (minimum safe count {minimum_count})"
    )
    if previous_verified:
        print(
            f"  WARNING: {state_code} live State Parks result is suspicious ({reason}); "
            f"retaining {len(previous_verified)} last-known-good verified records from camps.json."
        )
        return previous_verified

    raise RuntimeError(
        f"{state_code} live State Parks result is suspicious ({reason}) and no verified "
        "dynamic fallback exists. Refusing to publish stale/manual state data."
    )


def _al_root_park_links(index_html):
    """Discover official Alabama park root pages from the statewide park index."""
    links = []
    seen = set()
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>([\s\S]*?)</a>",
        re.I,
    )
    for match in anchor_re.finditer(index_html or ""):
        href = html.unescape(match.group(1)).strip()
        label = _strip_html_basic(match.group(2)).strip()
        absolute = urljoin(AL_STATE_PARKS_INDEX, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() not in {"alapark.com", "www.alapark.com"}:
            continue
        park_path = parsed.path.rstrip("/")
        if not re.fullmatch(r"/parks/[^/]+", park_path):
            continue
        if not label or "park" not in label.lower():
            continue
        normalized = f"https://www.alapark.com{park_path}"
        if normalized not in seen:
            seen.add(normalized)
            links.append(normalized)
    return links


def _al_same_park_candidate_links(park_url, raw_html):
    """Find official pages that could contain horse-camping information."""
    parsed_park = urlparse(park_url)
    base_path = parsed_park.path.rstrip("/")
    candidates = []
    seen = {park_url.rstrip("/")}
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>([\s\S]*?)</a>",
        re.I,
    )
    for match in anchor_re.finditer(raw_html or ""):
        href = html.unescape(match.group(1)).strip()
        label = _strip_html_basic(match.group(2)).strip()
        absolute = urljoin(park_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() not in {"alapark.com", "www.alapark.com"}:
            continue
        if not parsed.path.startswith(base_path + "/"):
            continue
        blob = f"{parsed.path} {label}".lower()
        if not any(term in blob for term in ("horse", "equestrian", "equine", "camp")):
            continue
        normalized = f"https://www.alapark.com{parsed.path.rstrip('/')}"
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    def priority(url):
        low = url.lower()
        if any(term in low for term in ("horse", "equestrian", "equine")):
            return 0
        return 1

    candidates.sort(key=priority)
    strong = [url for url in candidates if priority(url) == 0]
    generic = [url for url in candidates if priority(url) == 1][:4]
    return (strong + generic)[:10]


def _al_extract_h1(raw_html):
    match = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", raw_html or "", flags=re.I)
    return _strip_html_basic(match.group(1)).strip() if match else ""


def _al_heading_rows(raw_html):
    rows = []
    heading_re = re.compile(r"<h([1-6])\b[^>]*>([\s\S]*?)</h\1>", re.I)
    matches = list(heading_re.finditer(raw_html or ""))
    for index, match in enumerate(matches):
        level = int(match.group(1))
        heading = _strip_html_basic(match.group(2)).strip()
        end = len(raw_html or "")
        for later in matches[index + 1:]:
            if int(later.group(1)) <= level:
                end = later.start()
                break
        body = _usfs_html_to_text((raw_html or "")[match.end():end])
        rows.append((level, heading, body))
    return rows


def _al_is_established_horse_camp(text):
    text = str(text or "")
    if any(pattern.search(text) for pattern in AL_HORSE_CAMP_NON_OVERNIGHT_PATTERNS):
        return False
    if any(pattern.search(text) for pattern in AL_HORSE_CAMP_DEVELOPMENT_PATTERNS):
        return False
    has_horse_camp = any(pattern.search(text) for pattern in AL_HORSE_CAMP_POSITIVE_PATTERNS)
    has_camping_detail = any(pattern.search(text) for pattern in AL_HORSE_CAMP_DETAIL_PATTERNS)
    return has_horse_camp and has_camping_detail


def _al_local_horse_windows(text, radius=420):
    """Return tightly scoped text around explicit horse-camping statements.

    Keep unrelated campground amenities from leaking into the horse-camp record.
    A preceding sentence is included only when it clearly applies to all campsites
    (for example, "all with 30-amp service"). The window always stops at the end
    of the horse-camping sentence, so a following premium/RV-site sentence such as
    "50/30/20-amp service" cannot be mistaken for an equestrian amenity.
    """
    text = str(text or "")
    windows = []
    starts = []
    for pattern in AL_HORSE_CAMP_POSITIVE_PATTERNS:
        starts.extend(match.start() for match in pattern.finditer(text))

    sentence_boundary_re = re.compile(r"(?:[.!?](?:\s+|$)|\n+)")

    for start_pos in sorted(set(starts)):
        # Find the sentence containing the horse-camping signal.
        prior_boundaries = list(sentence_boundary_re.finditer(text, 0, start_pos))
        sentence_start = prior_boundaries[-1].end() if prior_boundaries else 0
        next_boundary = sentence_boundary_re.search(text, start_pos)
        sentence_end = next_boundary.end() if next_boundary else min(len(text), start_pos + radius)
        current_sentence = text[sentence_start:sentence_end].strip()

        # A park page may state a campground-wide utility in the immediately
        # preceding sentence, then identify the equestrian area in the next one.
        # Import that prior sentence only when it explicitly applies to all sites;
        # never pull in premium/special-loop amenities.
        previous_sentence = ""
        if prior_boundaries:
            previous_end = prior_boundaries[-1].start()
            earlier = list(sentence_boundary_re.finditer(text, 0, previous_end))
            previous_start = earlier[-1].end() if earlier else 0
            candidate = text[previous_start:sentence_start].strip()
            candidate_low = candidate.lower()
            applies_to_all_sites = bool(
                re.search(r"\ball\b.{0,100}\b(?:camp)?sites?\b", candidate_low, flags=re.S)
                or re.search(r"\b(?:camp)?sites?\b.{0,120}\ball\b", candidate_low, flags=re.S)
            )
            has_utility_detail = bool(
                re.search(r"\b(?:20|30|50)\s*[-‐‑‒–—]?\s*amp\b", candidate_low)
                or re.search(r"\b(?:water|sewer)\b.{0,80}\b(?:hook\s*ups?|service)\b", candidate_low, flags=re.S)
            )
            if applies_to_all_sites and has_utility_detail and "premium" not in candidate_low:
                previous_sentence = candidate

        window = " ".join(part for part in (previous_sentence, current_sentence) if part).strip()
        if _al_is_established_horse_camp(window):
            windows.append(window)
    return windows


def _al_horse_context(raw_html):
    """Extract horse-specific text without inheriting ordinary RV-loop amenities."""
    sections = []
    for level, heading, body in _al_heading_rows(raw_html):
        if level == 1:
            continue
        heading_low = heading.lower()
        combined = f"{heading}\n{body}".strip()
        heading_is_horse_camp = (
            any(term in heading_low for term in ("horse", "equestrian", "equine"))
            and "camp" in heading_low
        )
        if heading_is_horse_camp and _al_is_established_horse_camp(combined):
            # Dedicated horse-camping heading: the whole section is relevant.
            sections.append(combined)
        elif _al_is_established_horse_camp(combined):
            # Generic headings such as "Accommodations" can mix regular RV sites
            # with an equestrian subsection. Keep only the local horse statement.
            sections.extend(_al_local_horse_windows(body))

    if not sections:
        sections.extend(_al_local_horse_windows(_usfs_html_to_text(raw_html)))

    deduped = []
    seen = set()
    for section in sections:
        normalized = re.sub(r"\s+", " ", section).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            deduped.append(normalized)
    return deduped


def _al_horse_page_score(url, contexts):
    text = " ".join(contexts).lower()
    score = len(contexts) * 10
    if "horse" in url.lower() or "equestrian" in url.lower() or "equine" in url.lower():
        score += 20
    if "equestrian campground" in text or "horse campground" in text:
        score += 20
    if "horse camping area" in text or "equestrian camping area" in text:
        score += 15
    return score


def _al_camp_label(raw_html, contexts):
    candidates = []
    for _, heading, _ in _al_heading_rows(raw_html):
        lower = heading.lower()
        if not any(term in lower for term in ("horse", "equestrian", "equine")):
            continue
        if "camp" not in lower:
            continue
        cleaned = re.sub(r"\s*/\s*day\s+riding.*$", "", heading, flags=re.I).strip(" -/")
        cleaned = re.sub(r"\bday\s+riding\b.*$", "", cleaned, flags=re.I).strip(" -/")
        score = 0
        low = cleaned.lower()
        if "horse camping area" in low:
            score = 40
        elif "equestrian campground" in low or "horse campground" in low:
            score = 35
        elif "equestrian camping" in low or "equine camping" in low:
            score = 30
        else:
            score = 20
        candidates.append((score, cleaned))

    if candidates:
        candidates.sort(reverse=True)
        label = candidates[0][1]
        if label.lower() == "equestrian camping":
            return "Equestrian Campground"
        if label.lower() == "equine camping":
            return "Equestrian Camping Area"
        return label

    combined = " ".join(contexts).lower()
    if "equestrian campground" in combined:
        return "Equestrian Campground"
    if "horse camping area" in combined:
        return "Horse Camping Area"
    return "Equestrian Camping Area"


def _al_hookups(text):
    low = str(text or "").lower()
    hookups = []
    dash = r"[\s\-\u2010-\u2015]*"
    for amps in (20, 30, 50):
        if re.search(rf"\b{amps}{dash}amp\b|\b{amps}a\b", low):
            hookups.append(f"{amps}A")

    if re.search(
        r"\bwater\b.{0,80}\b(?:hook\s*ups?|service|spigots?|hydrants?)\b"
        r"|\b(?:water\s+spigots?|water\s+hydrants?|hydrants?)\b"
        r"|\b(?:service|hook\s*ups?)\b.{0,80}\bwater\b",
        low,
        flags=re.S,
    ):
        hookups.append("Water")
    if re.search(r"\bsewer\b.{0,60}\bhook\s*ups?\b|\bfull\s+hook\s*ups?\b", low, flags=re.S):
        hookups.append("Sewer")
    if re.search(r"\bno\s+(?:utility\s+)?hook\s*ups?\b", low):
        return ["No Hookups"]
    return list(dict.fromkeys(hookups))


def _al_accommodations(horse_text, support_text):
    low = str(horse_text or "").lower()
    support = str(support_text or "").lower()
    accommodations = []
    if re.search(r"\b(?:horse|equestrian|equine)\s+trails?\b|\bhorseback\s+riding\b", support):
        accommodations.append("Trails")
    if re.search(r"\bstalls?\b", low):
        accommodations.append("Stalls")
    if re.search(r"\bcorrals?\b", low):
        accommodations.append("Corrals")
    if re.search(r"\bpaddocks?\b", low):
        accommodations.append("Paddocks")
    if re.search(r"\bhigh\s*lines?\b|\btie\s+rails?\b|\bhitching\s+rails?\b", low):
        accommodations.append("Highlines")
    return list(dict.fromkeys(accommodations)) or ["Trails"]


def _al_stall_count(text):
    match = re.search(r"\b(\d+)\s+(?:covered\s+)?stalls?\b", str(text or ""), flags=re.I)
    return int(match.group(1)) if match else 0


def _al_paddock_count(text):
    match = re.search(r"\b(\d+)\s+paddocks?\b", str(text or ""), flags=re.I)
    return int(match.group(1)) if match else 0


def _al_phone(raw_html):
    text = _usfs_html_to_text(raw_html)
    directory = re.search(
        r"(?:Park\s+Directory|Phone\s+Numbers?|Park\s+Office)\s*:?\s*#?\s*"
        r"(?:\+?1[-. ]*)?(\d{3})[-. )]+(\d{3})[-. ]+(\d{4})",
        text,
        flags=re.I,
    )
    if directory:
        return f"{directory.group(1)}-{directory.group(2)}-{directory.group(3)}"
    match = re.search(r"\b(?:\+?1[-. ]*)?(\d{3})[-. )]+(\d{3})[-. ]+(\d{4})\b", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def _al_city(raw_html):
    text = _usfs_html_to_text(raw_html)
    location_pos = text.lower().find("location")
    search_text = text[location_pos:location_pos + 700] if location_pos >= 0 else text

    # Prefer an address-shaped line and strip the street portion. Alabama park
    # pages commonly render: "200 Terrace Drive Pelham, AL 35124" or
    # "4325 Alabama Highway 128 Alexander City, AL 35010".
    address_match = re.search(
        r"(?:^|\n)([^\n]{0,140}?,\s*AL\s+\d{5}(?:-\d{4})?)(?:$|\n)",
        search_text,
        flags=re.M,
    )
    candidate = address_match.group(1).strip() if address_match else search_text
    road_match = re.search(
        r"\b(?:Street|St\.?|Road|Rd\.?|Drive|Dr\.?|Highway|Hwy\.?|Avenue|Ave\.?|"
        r"Lane|Ln\.?|Parkway|Pkwy\.?|Boulevard|Blvd\.?|Route|County\s+Road|Co\.?\s*Rd\.?)"
        r"\s+(?:\d+[A-Za-z-]*\s+)?([A-Z][A-Za-z .'-]+),\s*AL\s+\d{5}(?:-\d{4})?\b",
        candidate,
    )
    if road_match:
        return road_match.group(1).strip()

    match = re.search(
        r"\b([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2}),\s*AL\s+\d{5}(?:-\d{4})?\b",
        candidate,
    )
    return match.group(1).strip() if match else ""


def _al_slug_id(park_url):
    slug = urlparse(park_url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-state-park$", "", slug, flags=re.I)
    return "al-stateparks-" + re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")


def _al_build_camp(park_url, park_html, horse_pages):
    park_name = _al_extract_h1(park_html)
    if not park_name:
        return None

    scored_pages = []
    all_contexts = []
    support_texts = [_usfs_html_to_text(park_html)]
    for page_url, raw_html, contexts in horse_pages:
        all_contexts.extend(contexts)
        support_texts.append(_usfs_html_to_text(raw_html))
        scored_pages.append((_al_horse_page_score(page_url, contexts), page_url, raw_html, contexts))
    if not scored_pages or not all_contexts:
        return None

    scored_pages.sort(key=lambda row: row[0], reverse=True)
    _, best_url, best_html, best_contexts = scored_pages[0]
    horse_text = "\n".join(all_contexts)
    support_text = "\n".join(support_texts)
    label = _al_camp_label(best_html, best_contexts)
    name = label if park_name.lower() in label.lower() else f"{park_name} {label}"

    lat, lon = _geocode_place_nominatim(f"{park_name}, Alabama")
    time.sleep(1.0)
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        previous = {
            row.get("id"): row
            for row in _load_previous_state_park_records("AL", verified_only=False)
        }.get(_al_slug_id(park_url))
        if previous:
            try:
                lat = float(previous.get("latitude") or 0)
                lon = float(previous.get("longitude") or 0)
            except (TypeError, ValueError):
                lat = lon = 0.0
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        print(f"  Alabama State Parks: could not resolve coordinates for {park_name}")
        return None

    low = horse_text.lower()
    year_round = bool(re.search(r"\byear[- ]round\b", horse_text, flags=re.I))
    city = _al_city(park_html)
    description = re.sub(r"\s+", " ", " ".join(best_contexts)).strip()

    return {
        "id": _al_slug_id(park_url),
        "name": name,
        "location": f"{city}, AL" if city else f"{park_name}, AL",
        "state": "AL",
        "latitude": lat,
        "longitude": lon,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": _al_hookups(horse_text),
        "accommodations": _al_accommodations(horse_text, support_text),
        "maxRigLength": 0,
        "stallCount": _al_stall_count(horse_text),
        "paddockCount": _al_paddock_count(horse_text),
        "phone": _al_phone(park_html),
        "website": best_url,
        "description": description[:2000] or f"Official Alabama State Parks horse-camping location at {park_name}.",
        "isVerified": True,
        "seasonStart": 1 if year_round else 0,
        "seasonEnd": 12 if year_round else 0,
        "hasWashRack": bool(re.search(r"\b(?:wash\s+rack|horse\s+wash)\b", low)),
        "hasDumpStation": bool(
            re.search(r"\b(?:rv\s+dump\s+station|sanitary\s+dump)\b", low)
            or (
                re.search(r"\bdump\s+station\b", low)
                and not re.search(r"\bdump\s+station\s+for\s+horse\s+waste\b", low)
            )
        ),
        "hasWifi": bool(re.search(r"\b(?:wi[- ]?fi|internet)\b", low)),
        "hasBathhouse": bool(re.search(r"\b(?:bathhouse|shower\s+house|shower\s+building|flush\s+toilets?)\b", low)),
        "pullThroughAvailable": bool(re.search(r"\bpull[- ]through\b", low)),
        "imageColors": ["C0392B", "F1948A"],
        "photoURLs": [],
        "source": "State Parks",
    }


def fetch_al_state_parks():
    """Dynamically discover established Alabama horse campgrounds and amenities.

    Discovery starts from the official Alabama State Parks index, follows each
    official park root page and its horse/equestrian/camping subpages, and only
    publishes parks with explicit established overnight horse-camping evidence.
    No Alabama campground names or amenity values are hardcoded here.
    """
    index_url, index_html = _fetch_state_park_html(AL_STATE_PARKS_INDEX, "Alabama State Parks index")
    if not index_html:
        return _guard_dynamic_state_park_result("AL", [])

    park_urls = _al_root_park_links(index_html)
    if not park_urls:
        return _guard_dynamic_state_park_result("AL", [])
    print(f"  Alabama State Parks: discovered {len(park_urls)} official park pages from statewide index")

    camps = []
    for park_url in park_urls:
        _, park_html = _fetch_state_park_html(park_url, "Alabama State Parks")
        if not park_html:
            continue

        horse_pages = []
        root_contexts = _al_horse_context(park_html)
        if root_contexts:
            horse_pages.append((park_url, park_html, root_contexts))

        for candidate_url in _al_same_park_candidate_links(park_url, park_html):
            final_url, candidate_html = _fetch_state_park_html(candidate_url, "Alabama State Parks")
            if not candidate_html:
                continue
            contexts = _al_horse_context(candidate_html)
            if contexts:
                horse_pages.append((final_url or candidate_url, candidate_html, contexts))
            time.sleep(0.15)

        if not horse_pages:
            continue

        camp = _al_build_camp(park_url, park_html, horse_pages)
        if camp:
            camps.append(camp)

    # Stable ordering and duplicate protection if the site index repeats a park.
    deduped = {}
    for camp in camps:
        deduped[camp["id"]] = camp
    camps = sorted(deduped.values(), key=lambda row: row["name"])

    camps = _guard_dynamic_state_park_result("AL", camps)
    print(f"  Alabama State Parks: {len(camps)} dynamic official horse-camping listings")
    return camps

def fetch_wy_state_parks():
    """Load manual WY state-park listings from data/state_parks/wy.json."""
    return load_manual_state_parks("WY")

def fetch_mt_state_parks():
    """Load manual MT state-park listings from data/state_parks/mt.json."""
    return load_manual_state_parks("MT")

def fetch_de_state_parks():
    """Load manual DE state-park listings from data/state_parks/de.json."""
    return load_manual_state_parks("DE")

def fetch_ms_state_parks():
    """Load manual MS state-park listings from data/state_parks/ms.json."""
    return load_manual_state_parks("MS")

def fetch_ak_state_parks():
    """Load manual AK state-park listings from data/state_parks/ak.json."""
    return load_manual_state_parks("AK")

def fetch_ia_state_parks():
    """Load manual IA state-park listings from data/state_parks/ia.json."""
    return load_manual_state_parks("IA")

def fetch_hi_state_parks():
    """Load manual HI state-park listings from data/state_parks/hi.json."""
    return load_manual_state_parks("HI")

def fetch_nj_state_parks():
    """Load manual NJ state-park listings from data/state_parks/nj.json."""
    return load_manual_state_parks("NJ")

def fetch_ri_state_parks():
    """Load manual RI state-park listings from data/state_parks/ri.json."""
    return load_manual_state_parks("RI")

def fetch_nh_state_parks():
    """Load manual NH state-park listings from data/state_parks/nh.json."""
    return load_manual_state_parks("NH")

def fetch_me_state_parks():
    """Load manual ME state-park listings from data/state_parks/me.json."""
    return load_manual_state_parks("ME")

def fetch_ma_state_parks():
    """Load manual MA state-park listings from data/state_parks/ma.json."""
    return load_manual_state_parks("MA")

def fetch_nd_state_parks():
    """Load manual ND state-park listings from data/state_parks/nd.json."""
    return load_manual_state_parks("ND")

# ── HORSEMOTEL.COM PARTNER LISTINGS ───────────────────────────────────
# Authorized partner data from HorseMotel.com. HorseMotel.com remains the
# source of truth. HorseCamp consumes the already-generated standalone
# HorseMotel repo feed and transforms it into the HorseCamp camp schema.
def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_list(value):
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "y", "1"):
            return True
        if low in ("false", "no", "n", "0"):
            return False
    return default


def _horsemotel_location(listing):
    location = _clean_text(listing.get("location"))
    if location:
        return location

    city = _clean_text(listing.get("city"))
    state = _clean_text(listing.get("state"))
    country = _clean_text(listing.get("country"))
    parts = [part for part in (city, state or country) if part]
    return ", ".join(parts)


def _transform_horsemotel_listing(listing, index):
    """Map one standalone HorseMotel app record into HorseCamp's camp schema."""
    name = _clean_text(listing.get("name"))
    lat = _as_float(listing.get("latitude"))
    lng = _as_float(listing.get("longitude"))

    if not name or (lat == 0 and lng == 0):
        return None

    raw_id = _clean_text(listing.get("id"))
    if not raw_id:
        raw_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"listing-{index}"
    camp_id = raw_id if raw_id.startswith("horsemotel-") else f"horsemotel-{raw_id}"

    accommodations = _clean_list(listing.get("accommodations"))
    if "Horse Motel" not in accommodations:
        accommodations.append("Horse Motel")

    status_notice = _clean_text(listing.get("statusNotice"))
    description = _clean_text(listing.get("description"))
    description_parts = []
    if status_notice:
        description_parts.append(f"Notice: {status_notice}")
    if description:
        description_parts.append(description)
    final_description = " ".join(description_parts).strip()

    state = _clean_text(listing.get("state")).upper()

    camp = {
        "id": camp_id,
        "name": name,
        "location": _horsemotel_location(listing),
        "state": state,
        "latitude": lat,
        "longitude": lng,
        "pricePerNight": _as_float(listing.get("pricePerNight")),
        "horseFeePerNight": _as_float(listing.get("horseFeePerNight")),
        "hookups": _clean_list(listing.get("hookups")),
        "accommodations": list(dict.fromkeys(accommodations)),
        "maxRigLength": _as_int(listing.get("maxRigLength")),
        "stallCount": _as_int(listing.get("stallCount")),
        "paddockCount": _as_int(listing.get("paddockCount")),
        "phone": _clean_text(listing.get("phone")),
        "website": _clean_text(listing.get("website")),
        "description": final_description[:2000],
        "isVerified": _as_bool(listing.get("isVerified"), True),
        "seasonStart": _as_int(listing.get("seasonStart"), 1),
        "seasonEnd": _as_int(listing.get("seasonEnd"), 12),
        "hasWashRack": _as_bool(listing.get("hasWashRack")),
        "hasDumpStation": _as_bool(listing.get("hasDumpStation")),
        "hasWifi": _as_bool(listing.get("hasWifi")),
        "hasBathhouse": _as_bool(listing.get("hasBathhouse")),
        "pullThroughAvailable": _as_bool(listing.get("pullThroughAvailable")),
        "imageColors": _clean_list(listing.get("imageColors")),
        "photoURLs": _clean_list(listing.get("photoURLs")),
        "source": "HorseMotel.com",
    }

    email = _clean_text(listing.get("email"))
    if email:
        camp["email"] = email

    source_url = _clean_text(listing.get("sourceUrl"))
    if source_url:
        camp["sourceUrl"] = source_url

    address = _clean_text(listing.get("address"))
    if address:
        camp["address"] = address

    map_search_address = _clean_text(listing.get("mapSearchAddress"))
    if map_search_address:
        camp["mapSearchAddress"] = map_search_address


    return camp


def fetch_horsemotel_listings():
    print(f"  Fetching HorseMotel.com partner feed from {HORSEMOTEL_FEED_URL}")
    try:
        response = requests.get(
            HORSEMOTEL_FEED_URL,
            timeout=60,
            headers={"User-Agent": "HorseCamp feed sync (+https://horsecampfinder.com/)"},
        )
        response.raise_for_status()
        listings = response.json()
    except Exception as e:
        raise RuntimeError(f"Unable to fetch HorseMotel.com feed from {HORSEMOTEL_FEED_URL}: {e}") from e

    if not isinstance(listings, list):
        raise ValueError("HorseMotel.com feed must contain a top-level JSON array of listings")

    transformed = []
    skipped = 0
    for i, listing in enumerate(listings, start=1):
        if not isinstance(listing, dict):
            skipped += 1
            continue
        camp = _transform_horsemotel_listing(listing, i)
        if camp is None:
            skipped += 1
            continue

        required_fields = ("id", "name", "location", "state", "latitude", "longitude", "source")
        for field in required_fields:
            if field not in camp:
                raise ValueError(f"HorseMotel.com listing #{i} is missing required field after transform: {field}")
        transformed.append(camp)

    if len(transformed) < HORSEMOTEL_MIN_LISTINGS:
        raise RuntimeError(
            f"Refusing to continue: HorseMotel.com feed only produced {len(transformed)} usable listings "
            f"(minimum required: {HORSEMOTEL_MIN_LISTINGS})"
        )

    print(f"  HorseMotel.com transformed listings: {len(transformed)}")
    if skipped:
        print(f"  HorseMotel.com skipped rows: {skipped}")
    return transformed


def fetch_private_camps():
    if not PRIVATE_CAMPS_FILE.exists():
        raise FileNotFoundError(
            f"Missing private camps file: {PRIVATE_CAMPS_FILE}. "
            "Create data/private_camps.json before running the fetch."
        )

    with PRIVATE_CAMPS_FILE.open("r", encoding="utf-8") as f:
        private_camps = json.load(f)

    if not isinstance(private_camps, list):
        raise ValueError("data/private_camps.json must contain a JSON array of private camp listings")

    for i, camp in enumerate(private_camps, start=1):
        if not isinstance(camp, dict):
            raise ValueError(f"Private camp #{i} in data/private_camps.json is not a JSON object")
        for field in ("id", "name", "location", "state", "latitude", "longitude", "source"):
            if field not in camp:
                raise ValueError(f"Private camp #{i} is missing required field: {field}")

    return private_camps


def fetch_nv_state_parks():
    """Load manual NV state-park listings from data/state_parks/nv.json."""
    return load_manual_state_parks("NV")

def fetch_ok_state_parks():
    """Load manual OK state-park listings from data/state_parks/ok.json."""
    return load_manual_state_parks("OK")

# ── KANSAS STATE PARKS ─────────────────────────────────────────────────
# Kansas is discovered dynamically from official KDWP sources. The legacy/current
# HTML equestrian indexes are preferred; if KDWP's main website blocks GitHub
# Actions, official KDWP publications are used as a discovery fallback. No Kansas
# park names, individual park URLs, or amenity values are hardcoded.
KS_STATE_PARKS_HTML_SOURCES = [
    "https://ksoutdoors.com/Services/Outdoor-Activities/Equestrian-Trails-Campgrounds",
    "https://www.ksoutdoors.gov/outdoor-activities/other-outdoor-recreation-in-kansas",
]
KS_STATE_PARKS_BASE = "https://www.ksoutdoors.gov/about-kdwp/where-we-work/state-parks"
KS_STATE_PARKS_PUBLICATION_SOURCES = [
    "https://ksoutdoors.gov/content/download/52060/526467/version/3/file/Kansas%2BHorse%2BTrails%2B2020.pdf",
    "https://history.ksoutdoors.com/content/download/7798/33835/file/2022%20-79-4%20-%20JA%20Kansas%20Wildlife%20%26%20Parks%20Magazine.pdf",
]
KS_STATE_PARKS_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
KS_STATE_PARKS_DELAY_SECONDS = (2.5, 3.5)

KS_HORSE_TERMS_RE = re.compile(
    r"\b(?:horse|horses|horseback|equestrian|equine|corrals?|horse\s+pens?|"
    r"paddocks?|stalls?|high\s*lines?|hitching\s+posts?|tie\s+rails?)\b",
    re.I,
)


def _ks_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": KS_STATE_PARKS_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


_KS_HTTP = _ks_session()


def _ks_request(url, *, referrer="", binary=False, attempts=2):
    """Fetch one KDWP resource with short bounded retries.

    KDWP has returned immediate 403 responses to GitHub-hosted runners. A 403 gets
    one short retry and then control moves to the next official source instead of
    waiting minutes and repeating a request that is being blocked by the WAF.
    """
    headers = {"Referer": referrer} if referrer else {}
    for attempt in range(1, attempts + 1):
        try:
            response = _KS_HTTP.get(
                url,
                timeout=(10, 35),
                headers=headers,
                allow_redirects=True,
            )
        except requests.RequestException as error:
            if attempt >= attempts:
                print(f"  Kansas State Parks: request failed for {url}: {error}")
                return "", b"" if binary else ""
            wait = 4 + random.uniform(0, 2)
            print(f"  Kansas State Parks: request error; retrying in {wait:.1f}s")
            time.sleep(wait)
            continue

        if response.status_code == 200 and response.content:
            return response.url, response.content if binary else response.text

        if response.status_code == 403:
            if attempt < attempts:
                wait = 5 + random.uniform(0, 3)
                print(f"  Kansas State Parks: HTTP 403 for {url}; one retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            print(f"  Kansas State Parks: HTTP 403 for {url}; trying alternate official source")
            return "", b"" if binary else ""

        if response.status_code == 429 and attempt < attempts:
            retry_after = str(response.headers.get("Retry-After") or "").strip()
            wait = int(retry_after) if retry_after.isdigit() else 10
            wait = max(3, min(wait, 30))
            print(f"  Kansas State Parks: HTTP 429; retrying in {wait}s")
            time.sleep(wait)
            continue

        if response.status_code >= 500 and attempt < attempts:
            time.sleep(5 + random.uniform(0, 2))
            continue

        print(f"  Kansas State Parks: HTTP {response.status_code} for {url}")
        return "", b"" if binary else ""

    return "", b"" if binary else ""


def _ks_fetch_html(url, referrer=""):
    final_url, body = _ks_request(url, referrer=referrer, binary=False)
    return final_url, body


def _ks_official_url(href, base=KS_STATE_PARKS_HTML_SOURCES[0]):
    absolute = urljoin(base, html.unescape(str(href or "")).strip())
    parsed = urlparse(absolute)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"ksoutdoors.gov", "www.ksoutdoors.gov", "ksoutdoors.com", "www.ksoutdoors.com"}:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return f"https://www.ksoutdoors.gov{path.rstrip('/')}"


def _ks_equestrian_park_links(index_html, source_url):
    """Discover park names/links only from KDWP's equestrian-campground section."""
    raw = str(index_html or "")
    low = raw.lower()
    marker = "state parks with equestrian campgrounds"
    section_start = low.find(marker)
    if section_start < 0:
        return []

    section_end = low.find("equestrian trails", section_start + len(marker))
    segment = raw[section_start:section_end if section_end > section_start else min(len(raw), section_start + 30000)]
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"'#]+)(?:[?#][^\"']*)?[\"'][^>]*>([\s\S]*?)</a>",
        re.I,
    )

    rows = []
    seen = set()
    for match in anchor_re.finditer(segment):
        label = re.sub(r"\s+", " ", _strip_html_basic(match.group(2))).strip()
        if "state park" not in label.lower():
            continue
        url = _ks_official_url(match.group(1), source_url)
        key = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        if not url or key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": label,
            "park_url": url,
            "source_url": source_url,
            "phone": "",
            "campsites": 0,
            "trail_miles": 0.0,
            "source_kind": "html-index",
        })
    return rows


def _ks_pdf_to_text(pdf_bytes):
    """Extract text from an official KDWP PDF.

    The workflow installs pypdf for this fallback. pdftotext remains a secondary
    option so the importer is still usable in local environments without pypdf.
    """
    if not pdf_bytes:
        return ""

    # Preferred pure-Python path used by GitHub Actions.
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        chunks = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text:
                chunks.append(text)
        extracted = "\n".join(chunks).strip()
        if extracted:
            return extracted
    except ImportError:
        pass
    except Exception as error:
        print(f"  Kansas State Parks: pypdf could not read KDWP publication: {error}")

    # Local fallback when pypdf is not installed but poppler is available.
    token = f"{os.getpid()}_{random.randint(100000, 999999)}"
    pdf_path = Path("/tmp") / f"horsecamp_ks_{token}.pdf"
    txt_path = Path("/tmp") / f"horsecamp_ks_{token}.txt"
    try:
        pdf_path.write_bytes(pdf_bytes)
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", str(pdf_path), str(txt_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=False,
            )
        except FileNotFoundError:
            print("  Kansas State Parks: no PDF text extractor is available")
            return ""
        except subprocess.TimeoutExpired:
            print("  Kansas State Parks: pdftotext timed out on KDWP publication")
            return ""
        if proc.returncode != 0 or not txt_path.exists():
            detail = re.sub(r"\s+", " ", proc.stderr or "").strip()[:180]
            print(f"  Kansas State Parks: could not extract KDWP publication text{': ' + detail if detail else ''}")
            return ""
        return txt_path.read_text(encoding="utf-8", errors="replace")
    finally:
        for path in (pdf_path, txt_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _ks_normalize_publication_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip(" ,.-")
    name = re.sub(r"\s+SP$", " State Park", name, flags=re.I)
    return name


def _ks_declared_publication_count(text):
    """Return an official publication's stated equestrian-campground count when present."""
    low = re.sub(r"\s+", " ", str(text or "")).lower()
    number_words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
        "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
        "twenty": 20,
    }
    match = re.search(r"\b(\d+|" + "|".join(number_words) + r")\s+equestrian\s+campgrounds?\b", low)
    if not match:
        return 0
    token = match.group(1)
    return int(token) if token.isdigit() else number_words.get(token, 0)


def _ks_publication_rows(text, source_url):
    """Parse horse-camp park names from official KDWP publication text.

    Supports both the KDWP Horse Trails contact table and the later magazine
    poster format. Parks with an explicit campsite count of zero are excluded.
    """
    clean = str(text or "").replace("\r", "")
    rows = []
    seen = set()

    # Horse Trails brochure: park name, phone, email, campsite count, trail miles.
    contact_re = re.compile(
        r"(?P<name>[A-Z][A-Za-z0-9 &'’.-]{2,80}?(?:State\s+Park|\bSP))\s+"
        r"(?P<phone>\(?\d{3}\)?[ -]\d{3}-\d{4})\s+"
        r"[^\s@]+@[^\s]+\s+"
        r"(?P<camps>\d+)\+?\s+"
        r"(?P<trails>\d+(?:\.\d+)?)\+?",
        re.I,
    )
    for match in contact_re.finditer(clean):
        campsites = int(match.group("camps"))
        if campsites <= 0:
            continue
        name = _ks_normalize_publication_name(match.group("name"))
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "park_url": "",
            "source_url": source_url,
            "phone": re.sub(r"[()]", "", match.group("phone")).replace(" ", "-"),
            "campsites": campsites,
            "trail_miles": float(match.group("trails")),
            "source_kind": "horse-trails-publication",
        })

    # Magazine poster: "Park Name, City + Horse Campsites". Only explicit horse-
    # campsite lines qualify, so trail-only facilities are not introduced.
    poster_re = re.compile(
        r"(?P<name>[A-Z][A-Za-z0-9 &'’.-]{2,80}?(?:State\s+Park|\bSP))\s*,[^\n+]{1,60}\+\s*Horse\s+Campsites\b",
        re.I,
    )
    for match in poster_re.finditer(clean):
        name = _ks_normalize_publication_name(match.group("name"))
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "park_url": "",
            "source_url": source_url,
            "phone": "",
            "campsites": 0,
            "trail_miles": 0.0,
            "source_kind": "magazine-publication",
        })
    return rows


def _ks_slug_from_name(park_name):
    base = re.sub(r"\bstate\s+park\b", " ", str(park_name or ""), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def _ks_park_url_from_name(park_name):
    slug = _ks_slug_from_name(park_name)
    return f"{KS_STATE_PARKS_BASE}/{slug}" if slug else ""


def _ks_discover_official_rows():
    # Prefer the official equestrian HTML lists because they provide direct park
    # links. A 403 on one source immediately moves to the next source.
    for source_url in KS_STATE_PARKS_HTML_SOURCES:
        _, index_html = _ks_fetch_html(source_url)
        if not index_html:
            continue
        rows = _ks_equestrian_park_links(index_html, source_url)
        if rows:
            print(f"  Kansas State Parks: discovered {len(rows)} equestrian-campground parks from official HTML")
            return rows
        print(f"  Kansas State Parks: official HTML source had no campground links: {source_url}")

    # Publication fallback. The PDFs are official KDWP publications, and park
    # names are parsed from the downloaded content rather than hardcoded here.
    for source_url in KS_STATE_PARKS_PUBLICATION_SOURCES:
        _, pdf_bytes = _ks_request(source_url, binary=True)
        if not pdf_bytes:
            continue
        text = _ks_pdf_to_text(pdf_bytes)
        rows = _ks_publication_rows(text, source_url)
        declared = _ks_declared_publication_count(text)
        if rows and declared:
            minimum_parse = max(1, math.ceil(declared * 0.80))
            if len(rows) < minimum_parse:
                print(
                    f"  Kansas State Parks: publication says {declared} equestrian campgrounds "
                    f"but parser found only {len(rows)}; trying alternate official publication"
                )
                continue
        if rows:
            print(f"  Kansas State Parks: discovered {len(rows)} horse-camping parks from official KDWP publication")
            return rows
        print(f"  Kansas State Parks: KDWP publication produced no horse-camp rows: {source_url}")

    return []

def _ks_slug_id(park_name):
    base = re.sub(r"\bstate\s+park\b", " ", str(park_name or ""), flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return f"ks-stateparks-{slug}"


def _ks_phone(raw_html):
    text = _usfs_html_to_text(raw_html)
    office = re.search(
        r"Park\s+Office.{0,100}?(?:\+?1[-. ]*)?\(?([2-9]\d{2})\)?[-. /]+(\d{3})[-. ]+(\d{4})",
        text,
        flags=re.I | re.S,
    )
    if office:
        return f"{office.group(1)}-{office.group(2)}-{office.group(3)}"
    match = re.search(r"\b(?:\+?1[-. ]*)?\(?([2-9]\d{2})\)?[-. /]+(\d{3})[-. ]+(\d{4})\b", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def _ks_city(raw_html):
    text = _usfs_html_to_text(raw_html)
    location_pos = text.lower().find("location")
    search_text = text[location_pos:location_pos + 900] if location_pos >= 0 else text

    road_match = re.search(
        r"\b(?:Road|Rd\.?|Street|St\.?|Drive|Dr\.?|Avenue|Ave\.?|Highway|Hwy\.?|"
        r"Lane|Ln\.?|Parkway|Pkwy\.?|Boulevard|Blvd\.?|Route)\s+"
        r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3}),\s*KS\s+\d{5}(?:-\d{4})?\b",
        search_text,
    )
    if road_match:
        return road_match.group(1).strip()

    matches = list(re.finditer(
        r"\b([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2}),\s*KS\s+\d{5}(?:-\d{4})?\b",
        search_text,
    ))
    return matches[-1].group(1).strip() if matches else ""


def _ks_name_tokens(value):
    noise = {
        "area", "camp", "campground", "campgrounds", "camping", "equestrian",
        "horse", "horses", "park", "state", "trail", "trails", "the",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) >= 4 and token not in noise
    }


def _ks_horse_contexts(raw_html):
    """Keep local horse/equestrian statements instead of generic park amenities."""
    text = _usfs_html_to_text(raw_html)
    contexts = []
    for match in KS_HORSE_TERMS_RE.finditer(text):
        left = max(0, match.start() - 260)
        right = min(len(text), match.end() + 420)
        window = text[left:right]
        # Trim to nearby sentence/line boundaries so a generic campground utility
        # paragraph does not bleed into the equestrian details.
        rel = match.start() - left
        before = max(window.rfind("\n", 0, rel), window.rfind(". ", 0, rel))
        if before >= 0:
            window = window[before + 1:]
            rel -= before + 1
        after_candidates = [
            pos for pos in (window.find("\n", rel), window.find(". ", rel)) if pos >= 0
        ]
        if after_candidates:
            window = window[:min(after_candidates) + 1]
        cleaned = re.sub(r"\s+", " ", window).strip()
        if cleaned and KS_HORSE_TERMS_RE.search(cleaned):
            contexts.append(cleaned)

    out = []
    seen = set()
    for context in contexts:
        key = context.lower()
        if key not in seen:
            seen.add(key)
            out.append(context)
    return out


def _ks_area_sections(raw_html):
    areas = []
    for level, heading, body in _al_heading_rows(raw_html):
        if level < 3:
            continue
        if not re.search(r"\bCamping\s+Available\s*:\s*Yes\b", body, flags=re.I):
            continue
        areas.append((heading.strip(), re.sub(r"\s+", " ", body).strip()))
    return areas


def _ks_horse_trail_names(raw_html):
    names = []
    for level, heading, body in _al_heading_rows(raw_html):
        if level < 3:
            continue
        if re.search(r"\bHorse\s+Riding\b", body, flags=re.I) or re.search(
            r"\b(?:horse|equestrian)\b", heading, flags=re.I
        ):
            names.append(heading.strip())
    return names


def _ks_select_horse_area(raw_html, horse_contexts):
    areas = _ks_area_sections(raw_html)
    if not areas:
        return None

    context_text = " ".join(horse_contexts).lower()
    trail_names = _ks_horse_trail_names(raw_html)
    trail_tokens = [_ks_name_tokens(name) for name in trail_names]
    best = None

    for name, body in areas:
        tokens = _ks_name_tokens(name)
        low = f"{name} {body}".lower()
        score = 0
        if KS_HORSE_TERMS_RE.search(low):
            score += 120

        # If an official horse statement names the area (Randolph, Saddle Ridge,
        # etc.), treat that as strong evidence even when the campground table
        # itself contains only generic utility fields.
        significant_mentions = [token for token in tokens if token in context_text]
        if significant_mentions:
            score += 50 + 10 * min(len(significant_mentions), 3)

        for horse_tokens in trail_tokens:
            overlap = tokens & horse_tokens
            if overlap:
                score += 35 + 10 * min(len(overlap), 2)

        if best is None or score > best[0]:
            best = (score, name, body)

    if not best or best[0] < 45:
        return None
    return best[1], best[2]


def _ks_find_area_detail_link(park_url, raw_html, area_name):
    if not area_name:
        return ""
    wanted = re.sub(r"\s+", " ", area_name).strip().lower()
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"'#]+)(?:[?#][^\"']*)?[\"'][^>]*>([\s\S]*?)</a>",
        re.I,
    )
    best = ""
    best_score = 0
    wanted_tokens = _ks_name_tokens(area_name)
    for match in anchor_re.finditer(raw_html or ""):
        label = _strip_html_basic(match.group(2)).strip()
        label_low = re.sub(r"\s+", " ", label).lower()
        if not label_low:
            continue
        score = 0
        if label_low == wanted:
            score = 100
        else:
            overlap = wanted_tokens & _ks_name_tokens(label)
            if overlap:
                score = 20 + 10 * len(overlap)
        if score <= best_score:
            continue
        url = _ks_official_url(match.group(1), park_url)
        if not url:
            continue
        best = url
        best_score = score
    return best


def _ks_hookups(text):
    low = str(text or "").lower()
    hookups = []
    dash = r"[\s\-\u2010-\u2015]*"
    for amps in (20, 30, 50):
        if re.search(rf"\b{amps}{dash}amp\b|\b{amps}a\b", low):
            hookups.append(f"{amps}A")

    # KDWP area tables commonly use phrases such as "Water, Electric" and
    # "Water, Sewer, Electric" without specifying amperage. Preserve what the
    # source actually says; do not guess an amp rating from a site count.
    if re.search(
        r"\bwater\b.{0,80}\b(?:electric|hook\s*ups?|hydrants?|service)\b"
        r"|\b(?:electric|hook\s*ups?|hydrants?|service)\b.{0,80}\bwater\b",
        low,
        flags=re.S,
    ):
        hookups.append("Water")
    if re.search(r"\bsewer\b.{0,80}\b(?:electric|water|hook\s*ups?)\b|\bfull\s+hook\s*ups?\b", low, flags=re.S):
        hookups.append("Sewer")
    return list(dict.fromkeys(hookups))


def _ks_accommodations(horse_text, page_text):
    low = str(horse_text or "").lower()
    page_low = str(page_text or "").lower()
    accommodations = []
    if re.search(r"\b(?:horse|equestrian)\s+trail\b|\bhorse\s+riding\b|\bhorseback\s+riding\b", page_low):
        accommodations.append("Trails")
    if re.search(r"\b(?:corrals?|horse\s+pens?)\b", low):
        accommodations.append("Corrals")
    if re.search(r"\bstalls?\b", low):
        accommodations.append("Stalls")
    if re.search(r"\bpaddocks?\b", low):
        accommodations.append("Paddocks")
    if re.search(r"\bhigh\s*lines?\b|\btie\s+rails?\b|\bhitching\s+(?:rails?|posts?)\b", low):
        accommodations.append("Highlines")
    return list(dict.fromkeys(accommodations)) or ["Trails"]


KS_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}


def _ks_parse_count_token(value):
    token = str(value or "").strip().lower()
    if token.isdigit():
        return int(token)
    return KS_NUMBER_WORDS.get(token, 0)


def _ks_paddock_count(text):
    # Only count when KDWP explicitly ties a numbered group of equestrian sites
    # to individual corrals. A generic campsite count must not become a corral count.
    number = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    match = re.search(
        rf"\b({number})\s+equestrian\s+campsites?\b.{{0,160}}\bindividual\s+corrals?\b",
        str(text or ""),
        flags=re.I | re.S,
    )
    return _ks_parse_count_token(match.group(1)) if match else 0


def _ks_camp_name(park_name, selected_area):
    if not selected_area:
        return f"{park_name} Equestrian Campground"
    if selected_area.lower() in park_name.lower():
        return park_name
    if "camp" in selected_area.lower():
        return f"{park_name} {selected_area}"
    return f"{park_name} {selected_area} Equestrian Campground"


def _ks_build_camp(park_url, park_html):
    park_name = _al_extract_h1(park_html)
    if not park_name:
        return None

    page_text = _usfs_html_to_text(park_html)
    horse_contexts = _ks_horse_contexts(park_html)
    selected = _ks_select_horse_area(park_html, horse_contexts)
    area_name, area_body = selected if selected else ("", "")

    detail_url = _ks_find_area_detail_link(park_url, park_html, area_name)
    detail_text = ""
    detail_horse_contexts = []
    if detail_url and detail_url.rstrip("/") != park_url.rstrip("/"):
        final_url, detail_html = _ks_fetch_html(detail_url, referrer=park_url)
        if detail_html:
            detail_url = final_url or detail_url
            detail_text = _usfs_html_to_text(detail_html)
            detail_horse_contexts = _ks_horse_contexts(detail_html)

    horse_text = " ".join(horse_contexts + detail_horse_contexts + ([area_body] if area_body else []))
    if not horse_text:
        # The statewide KDWP page is already authoritative that this park has an
        # equestrian campground; keep unknown amenities blank rather than inventing them.
        horse_text = "Official KDWP equestrian campground."

    camp_id = _ks_slug_id(park_name)
    lat, lon = _geocode_place_nominatim(f"{park_name}, Kansas")
    time.sleep(1.0)
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        previous = {
            row.get("id"): row
            for row in _load_previous_state_park_records("KS", verified_only=True)
        }.get(camp_id)
        if previous:
            try:
                lat = float(previous.get("latitude") or 0)
                lon = float(previous.get("longitude") or 0)
            except (TypeError, ValueError):
                lat = lon = 0.0
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        print(f"  Kansas State Parks: could not resolve coordinates for {park_name}")
        return None

    description_parts = horse_contexts[:4] + detail_horse_contexts[:3]
    if area_name and area_body:
        description_parts.append(f"{area_name}: {area_body}")
    description = re.sub(r"\s+", " ", " ".join(description_parts)).strip()
    if not description:
        description = f"Official Kansas Department of Wildlife and Parks equestrian campground at {park_name}."

    city = _ks_city(park_html)
    year_round = bool(re.search(
        r"\b(?:year[- ]around|year[- ]round|open\s+year\s+round)\b",
        " ".join(horse_contexts),
        flags=re.I,
    ))

    amenity_text = " ".join([horse_text, detail_text if detail_horse_contexts else ""])
    return {
        "id": camp_id,
        "name": _ks_camp_name(park_name, area_name),
        "location": f"{city}, KS" if city else f"{park_name}, KS",
        "state": "KS",
        "latitude": lat,
        "longitude": lon,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": _ks_hookups(amenity_text),
        "accommodations": _ks_accommodations(horse_text, page_text),
        "maxRigLength": 0,
        "stallCount": 0,
        "paddockCount": _ks_paddock_count(horse_text),
        "phone": _ks_phone(park_html),
        "website": detail_url or park_url,
        "description": description[:2000],
        "isVerified": True,
        "seasonStart": 1 if year_round else 0,
        "seasonEnd": 12 if year_round else 0,
        "hasWashRack": bool(re.search(r"\bwash\s+(?:rack|station)\b", amenity_text, flags=re.I)),
        "hasDumpStation": bool(re.search(r"\bdump\s+station\b", amenity_text, flags=re.I)),
        "hasWifi": bool(re.search(r"\bwi[- ]?fi\b|\binternet\b", amenity_text, flags=re.I)),
        "hasBathhouse": bool(re.search(r"\b(?:shower\s+(?:house|building)|bathhouse|modern\s+restroom)\b", amenity_text, flags=re.I)),
        "pullThroughAvailable": bool(re.search(r"\bpull[- ]through\b", amenity_text, flags=re.I)),
        "imageColors": ["C0392B", "F1948A"],
        "photoURLs": [],
        "source": "State Parks",
    }




def _ks_build_discovery_camp(row):
    """Build a conservative record when KDWP confirms horse camping but blocks details."""
    park_name = str(row.get("name") or "").strip()
    if not park_name:
        return None
    camp_id = _ks_slug_id(park_name)
    lat, lon = _geocode_place_nominatim(f"{park_name}, Kansas")
    time.sleep(1.0)
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        previous = {
            item.get("id"): item
            for item in _load_previous_state_park_records("KS", verified_only=True)
        }.get(camp_id)
        if previous:
            try:
                lat = float(previous.get("latitude") or 0)
                lon = float(previous.get("longitude") or 0)
            except (TypeError, ValueError):
                lat = lon = 0.0
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        print(f"  Kansas State Parks: could not resolve coordinates for {park_name}")
        return None

    campsites = int(row.get("campsites") or 0)
    trail_miles = float(row.get("trail_miles") or 0.0)
    details = ["Official Kansas Department of Wildlife and Parks source confirms an equestrian campground."]
    if campsites:
        details.append(f"The official KDWP publication lists {campsites} horse campsites.")
    if trail_miles:
        details.append(f"The same publication lists approximately {trail_miles:g} miles of trail.")

    park_url = row.get("park_url") or _ks_park_url_from_name(park_name)
    accommodations = ["Trails"] if trail_miles > 0 else []
    return {
        "id": camp_id,
        "name": f"{park_name} Equestrian Campground",
        "location": f"{park_name}, KS",
        "state": "KS",
        "latitude": lat,
        "longitude": lon,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [],
        "accommodations": accommodations,
        "maxRigLength": 0,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": str(row.get("phone") or ""),
        "website": park_url or str(row.get("source_url") or ""),
        "description": " ".join(details)[:2000],
        "isVerified": True,
        "seasonStart": 0,
        "seasonEnd": 0,
        "hasWashRack": False,
        "hasDumpStation": False,
        "hasWifi": False,
        "hasBathhouse": False,
        "pullThroughAvailable": False,
        "imageColors": ["C0392B", "F1948A"],
        "photoURLs": [],
        "source": "State Parks",
    }


def fetch_ks_state_parks():
    """Dynamically fetch Kansas horse campgrounds from official KDWP sources."""
    rows = _ks_discover_official_rows()
    if not rows:
        print("  Kansas State Parks: all official discovery sources unavailable")
        return _guard_dynamic_state_park_result("KS", [])

    camps = []
    for row in rows:
        park_name = str(row.get("name") or "").strip()
        park_url = str(row.get("park_url") or "") or _ks_park_url_from_name(park_name)
        final_url = ""
        park_html = ""
        if park_url:
            final_url, park_html = _ks_fetch_html(park_url, referrer=str(row.get("source_url") or ""))

        camp = None
        if park_html:
            camp = _ks_build_camp(final_url or park_url, park_html)
            if camp and not camp.get("phone") and row.get("phone"):
                camp["phone"] = str(row.get("phone"))
        if camp is None:
            # The discovery source itself is authoritative that horse camping is
            # present. Publish only fields it supports; do not copy manual JSON.
            camp = _ks_build_discovery_camp(row)

        if camp:
            camps.append(camp)
        time.sleep(random.uniform(*KS_STATE_PARKS_DELAY_SECONDS))

    deduped = {camp["id"]: camp for camp in camps}
    camps = sorted(deduped.values(), key=lambda item: item["name"])
    camps = _guard_dynamic_state_park_result("KS", camps)
    print(f"  Kansas State Parks: {len(camps)} dynamic official equestrian-camping listings")
    return camps

def fetch_md_state_parks():
    """Load manual MD state-park listings from data/state_parks/md.json."""
    return load_manual_state_parks("MD")

def fetch_vt_state_parks():
    """Load manual VT state-park listings from data/state_parks/vt.json."""
    return load_manual_state_parks("VT")

def fetch_wv_state_parks():
    """Load manual WV state-park listings from data/state_parks/wv.json."""
    return load_manual_state_parks("WV")

def fetch_la_state_parks():
    """Fetch conservative Louisiana State Parks equestrian camping locations.

    Louisiana State Parks officially surfaces horseback riding at a small set of
    parks, and those park pages also advertise overnight camping. This first pass
    stays conservative and only includes parks with clear official horseback-riding
    plus camping signals.
    """
    parks = [
        {
            "id": "la-stateparks-bogue-chitto",
            "name": "Bogue Chitto State Park Equestrian Area Campground",
            "location": "Franklinton, LA",
            "state": "LA",
            "latitude": 30.7907,
            "longitude": -89.8834,
            "pricePerNight": 33.0,
            "horseFeePerNight": 3.0,
            "hookups": ["30A", "Water", "Sewer"],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "985-839-5707",
            "website": "https://www.lastateparks.com/parks-preserves/bogue-chitto-state-park",
            "description": "Official Louisiana State Parks page lists an Equestrian Area Campground with seven premium sites that include sewer, water, and electrical hookups, along with equestrian trail riding in the park.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
        },
        {
            "id": "la-stateparks-lake-bistineau",
            "name": "Lake Bistineau State Park Horse Trail Camping",
            "location": "Doyline, LA",
            "state": "LA",
            "latitude": 32.6430,
            "longitude": -93.4177,
            "pricePerNight": 22.0,
            "horseFeePerNight": 3.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "318-745-3503",
            "website": "https://www.lastateparks.com/parks-preserves/lake-bistineau-state-park",
            "description": "Official Louisiana State Parks page says Lake Bistineau has an equestrian trail and notes that overnight campsites can be rented at parks offering equestrian trails.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
        },
    ]
    print(f"  Louisiana State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks

# ── MAIN ───────────────────────────────────────────────────────────────




def _geocode_place_nominatim(query):
    """Geocode a place name using Nominatim. Returns (lat, lon) or (0,0).
    Results are cached in data/geocode_cache.json — network only hit on a miss.
    """
    cached = _cache_lookup(query)
    if cached is not None:
        return cached

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {"User-Agent": "HorseCamp/1.0 (state parks importer)"}
    data = safe_get(url, headers=headers, params=params, retries=2)
    if isinstance(data, list) and data:
        try:
            lat = float(data[0].get("lat", 0) or 0)
            lon = float(data[0].get("lon", 0) or 0)
            _cache_store(query, lat, lon)
            return lat, lon
        except Exception:
            _cache_store(query, 0.0, 0.0)
            return 0.0, 0.0
    _cache_store(query, 0.0, 0.0)
    return 0.0, 0.0


# Backward-compatible alias used by later state importers
geocode_nominatim = _geocode_place_nominatim


def fetch_fl_state_parks():
    """Fetch Florida State Parks equestrian camping parks from the official Florida State Parks page.
    Park names come from the official Equestrian Camping page; coordinates are geocoded conservatively.
    """
    park_names = [
        "Alafia River State Park",
        "Buckman Lock - St. Johns Loop North and South",
        "Colt Creek State Park",
        "Florida Caverns State Park",
        "Highlands Hammock State Park",
        "Jonathan Dickinson State Park",
        "Kissimmee Prairie Preserve State Park",
        "Lake Kissimmee State Park",
        "Lake Louisa State Park",
        "Little Manatee River State Park",
        "Lower Wekiva River Preserve State Park",
        "Paynes Prairie Preserve State Park",
        "River Rise Preserve State Park",
        "Rock Springs Run State Reserve",
        "Ross Prairie Trailhead and Campground",
        "Shangri-La Trailhead and Campground",
        "St. Sebastian River Preserve State Park",
        "Wekiwa Springs State Park",
    ]

    # Park-specific amenity hints based on official park pages where known.
    amenity_overrides = {
        "Alafia River State Park": {"hookups": ["30A", "Water"], "accommodations": ["Stalls", "Paddocks", "Trails"], "hasBathhouse": True, "stallCount": 12, "paddockCount": 6},
        "Colt Creek State Park": {"hookups": ["Water"], "accommodations": ["Paddocks", "Trails"], "paddockCount": 0},
        "Kissimmee Prairie Preserve State Park": {"hookups": ["50A", "Water"], "accommodations": ["Paddocks", "Trails"], "paddockCount": 10, "hasBathhouse": True},
        "Lower Wekiva River Preserve State Park": {"hookups": [], "accommodations": ["Stalls", "Corrals", "Trails"], "hasBathhouse": True},
        "River Rise Preserve State Park": {"hookups": [], "accommodations": ["Stalls", "Trails"], "stallCount": 20, "hasBathhouse": True},
        "St. Sebastian River Preserve State Park": {"hookups": [], "accommodations": ["Paddocks", "Trails"], "hasBathhouse": False},
    }

    overview_url = "https://www.floridastateparks.org/equestrian-camping"
    camps = []
    for idx, name in enumerate(park_names, start=1):
        lat, lon = _geocode_place_nominatim(f"{name}, Florida")
        time.sleep(1.0)
        if abs(lat) < 0.1 or abs(lon) < 0.1:
            continue
        slug = name.lower().replace("&", "and")
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        website = f"https://www.floridastateparks.org/parks-and-trails/{slug}"
        overrides = amenity_overrides.get(name, {})
        hooks = overrides.get("hookups", [])
        acc = overrides.get("accommodations", ["Trails"])
        city_state = "Florida"
        camps.append({
            "id": f"flsp-{slug}",
            "name": name,
            "location": city_state,
            "state": "FL",
            "latitude": lat,
            "longitude": lon,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": hooks,
            "accommodations": acc,
            "maxRigLength": 0,
            "stallCount": overrides.get("stallCount", 0),
            "paddockCount": overrides.get("paddockCount", 0),
            "phone": "",
            "website": website,
            "description": "Official Florida State Parks equestrian camping location. Verify campsite type, amenities, and reservations with the park.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": overrides.get("hasBathhouse", False),
            "pullThroughAvailable": False,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
        })
    print(f"  Florida State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_ky_state_parks():
    """Load manual KY state-park listings from data/state_parks/ky.json."""
    return load_manual_state_parks("KY")

def fetch_pa_state_parks():
    """Load manual PA state-park listings from data/state_parks/pa.json."""
    return load_manual_state_parks("PA")

def fetch_mi_state_parks():
    """Fetch official Michigan equestrian campgrounds from the official Michigan DNR list.
    Uses the official equestrian-campgrounds page as the allowlist and geocodes each named
    campground/park conservatively.
    """
    mi_sites = [
        "4 Mile Trail Camp",
        "Big Oaks State Forest Campground",
        "Black Lake Trail Camp",
        "Brighton Recreation Area Equestrian Campground",
        "Cedar River North State Forest Campground",
        "Elk Hill Group Equestrian Campground",
        "Elk Hill Equestrian River Trail Campground",
        "Fort Custer Recreation Area Equestrian Campground",
        "Garey Lake Trail Camp",
        "Garey Lake State Forest Campground",
        "Goose Creek Trail Camp",
        "Headquarters Lake State Forest Campground",
        "Highland Recreation Area Rustic and Equestrian Campground",
        "Hopkins Creek Equestrian State Forest Campground and Trail Camp",
        "Ionia Recreation Area Equestrian Campground",
        "Johnsons Crossing Trail Camp",
        "Lake Dubonnet Trail Camp",
        "Ortonville-Equestrian",
        "Pontiac Lake Recreation Area Equestrian Campground",
        "Rapid River Trail Camp",
        "Scheck's Place Trail Camp",
        "Stoney Creek Trail Camp",
        "Walsh Road Equestrian State Forest Campground and Trail Camp",
        "Waterloo Recreation Area Equestrian Campground",
        "Yankee Springs Recreation Area Equestrian Campground",
    ]

    def geocode(name):
        queries = [
            f"{name}, Michigan",
            f"{name} campground, Michigan",
            f"{name} equestrian campground, Michigan",
        ]
        for q in queries:
            cached = _cache_lookup(q)
            if cached is not None:
                return cached
            try:
                r = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": q, "format": "jsonv2", "limit": 1},
                    headers={"User-Agent": "HorseCamp/1.0 (horsecampfinder.com)"},
                    timeout=20,
                )
                if r.status_code == 200:
                    arr = r.json()
                    if arr:
                        lat, lon = float(arr[0]["lat"]), float(arr[0]["lon"])
                        _cache_store(q, lat, lon)
                        return lat, lon
            except Exception:
                pass
            _cache_store(q, 0.0, 0.0)
            time.sleep(1.0)
        return 0.0, 0.0

    camps = []
    for name in mi_sites:
        lat, lng = geocode(name)
        if abs(lat) < 0.1 or abs(lng) < 0.1:
            continue
        desc = "Official Michigan DNR equestrian campground or trail camp. Verify campground type, reservations, trailer access, and horse amenities with Michigan DNR before arrival."
        lower = name.lower()
        accommodations = ["Trails"]
        if "equestrian" in lower or "trail camp" in lower:
            accommodations.append("Corrals")
        camps.append({
            "id": f"mi-statepark-{re.sub(r'[^a-z0-9]+','-', name.lower()).strip('-')}",
            "name": name,
            "location": "Michigan",
            "state": "MI",
            "latitude": lat,
            "longitude": lng,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": list(dict.fromkeys(accommodations)),
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "",
            "website": "https://www.michigan.gov/dnr/things-to-do/camping-and-lodging/equestrian-campgrounds",
            "description": desc,
            "isVerified": False,
            "seasonStart": 0,
            "seasonEnd": 0,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "imageColors": ["C0392B", "E59866"],
            "photoURLs": [],
            "source": "State Parks",
        })
    print(f"  Michigan State Parks: {len(camps)} official equestrian-camping listings")
    return camps


def fetch_wi_state_parks():
    """Load manual WI state-park listings from data/state_parks/wi.json."""
    return load_manual_state_parks("WI")

def fetch_mo_state_parks():
    """Fetch Missouri State Parks equestrian campgrounds conservatively.

    Uses the official Missouri State Parks guide to campsites as an allowlist of four
    parks with separate equestrian campgrounds.
    """
    properties = [
        {
            "name": "Sam A. Baker State Park Equestrian Campground",
            "query": "Sam A. Baker State Park equestrian campground Missouri",
            "location": "Patterson, MO",
            "website": "https://mostateparks.com/activity/camping/guide-campsites",
            "description": "Official Missouri State Parks equestrian campground associated with Sam A. Baker State Park's horse trails.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "Cuivre River State Park Equestrian Campground",
            "query": "Cuivre River State Park equestrian campground Missouri",
            "location": "Troy, MO",
            "website": "https://mostateparks.com/activity/camping/guide-campsites",
            "description": "Official Missouri State Parks equestrian campground at Cuivre River State Park; use is limited to campers with horses.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "Johnson's Shut-Ins State Park Equestrian Campground",
            "query": "Johnson's Shut-Ins State Park equestrian campground Missouri",
            "location": "Middle Brook, MO",
            "website": "https://mostateparks.com/activity/camping/guide-campsites",
            "description": "Official Missouri State Parks equestrian campground at Johnson's Shut-Ins State Park associated with the park's horse trails.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "St. Joe State Park Equestrian Campground",
            "query": "St. Joe State Park equestrian campground Missouri",
            "location": "Park Hills, MO",
            "website": "https://mostateparks.com/activity/camping/guide-campsites",
            "description": "Official Missouri State Parks equestrian campground at St. Joe State Park associated with the park's equestrian trail system.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
    ]

    camps = []
    for p in properties:
        lat, lng = geocode_nominatim(p["query"])
        if not lat or not lng:
            lat, lng = geocode_nominatim(f'{p["name"]}, {p["location"]}')
        camps.append({
            "id": "mo-stateparks-" + re.sub(r'[^a-z0-9]+', '-', p["name"].lower()).strip('-'),
            "name": p["name"],
            "location": p["location"],
            "state": "MO",
            "latitude": lat,
            "longitude": lng,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "1-877-422-6766",
            "website": p["website"],
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 3,
            "seasonEnd": 11,
            "hasWashRack": False,
            "hasDumpStation": p["hasDumpStation"],
            "hasWifi": False,
            "hasBathhouse": p["hasBathhouse"],
            "pullThroughAvailable": False,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
        })
    print(f"  Missouri State Parks: {len(camps)} official equestrian-camping listings")
    return camps



def fetch_in_state_parks():
    """Load manual IN state-park listings from data/state_parks/in.json."""
    return load_manual_state_parks("IN")

def fetch_tx_state_parks():
    """Load manual TX state-park listings from data/state_parks/tx.json."""
    return load_manual_state_parks("TX")

def fetch_oh_state_parks():
    """Load manual OH state-park listings from data/state_parks/oh.json."""
    return load_manual_state_parks("OH")

def fetch_or_state_parks():
    """Load manual OR state-park listings from data/state_parks/or.json."""
    return load_manual_state_parks("OR")

def fetch_ne_state_parks():
    """Load manual NE state-park listings from data/state_parks/ne.json."""
    return load_manual_state_parks("NE")

def fetch_sd_state_parks():
    """Load manual SD state-park listings from data/state_parks/sd.json."""
    return load_manual_state_parks("SD")

def main():
    print(f"HorseCamp data fetch starting — {datetime.now(timezone.utc).isoformat()}")
    print(f"RIDB key present: {'Yes' if RIDB_KEY else 'NO — set RIDB_API_KEY secret'}")
    print(f"NPS key present:  {'Yes' if NPS_KEY  else 'NO — set NPS_API_KEY secret'}")
    load_geocode_cache()

    all_camps = {}
    total_ridb = 0
    total_nps = 0
    total_usfs = 0

    for i, state in enumerate(STATES):
        state_started = time.time()
        # Use a complete line so GitHub Actions displays the state immediately,
        # even when the following network request stalls before producing output.
        print(f"[{i+1}/{len(STATES)}] {state}...", flush=True)
        ridb_camps = fetch_ridb_state(state) if RIDB_KEY else []
        print(f"  RIDB {state} complete: {len(ridb_camps)} records", flush=True)

        if NPS_KEY:
            print(f"  NPS {state} starting...", flush=True)
            nps_camps = fetch_nps_state(state)
            print(f"  NPS {state} complete: {len(nps_camps)} records", flush=True)
        else:
            nps_camps = []
        state_new = 0
        for camp in ridb_camps + nps_camps:
            cid = camp["id"]
            if cid not in all_camps:
                all_camps[cid] = camp
                state_new += 1
        total_ridb += len(ridb_camps)
        total_nps += len(nps_camps)
        elapsed = time.time() - state_started
        print(f"  {len(ridb_camps)} RIDB + {len(nps_camps)} NPS = {state_new} new [{elapsed:.1f}s]")
        time.sleep(0.5)

    print_section("U.S. Forest Service")
    print("Fetching official Horse Camping recreation sites...")
    usfs_camps = fetch_usfs_recreation_sites()
    usfs_new, usfs_duplicate_ids, usfs_duplicate_nearby, usfs_duplicate_nearby_details = merge_camps_by_id_and_proximity(
        usfs_camps, all_camps,
    )
    total_usfs = len(usfs_camps)
    print_metric("U.S. Forest Service added", usfs_new)
    print_metric("U.S. Forest Service duplicate IDs", usfs_duplicate_ids)
    print_metric("U.S. Forest Service nearby duplicates", usfs_duplicate_nearby)
    ridb_usfs_duplicate_details = remove_ridb_usfs_name_location_duplicates(all_camps)
    print_metric("RIDB ↔ USFS name/location duplicates", len(ridb_usfs_duplicate_details))
    print_ridb_usfs_duplicate_details(ridb_usfs_duplicate_details)
    print_metric("USFS state cache hits", _usfs_state_stats["hits"])
    print_metric("USFS state lookups", _usfs_state_stats["misses"])

    def merge_state(camps):
        new_count = 0
        for camp in camps:
            cid = camp["id"]
            if cid not in all_camps:
                all_camps[cid] = camp
                new_count += 1
        return new_count

    state_park_jobs = [
        ("AK", "Alaska", fetch_ak_state_parks, "Alaska State Parks Equestrian Camping"),
        ("AL", "Alabama", fetch_al_state_parks, "Alabama State Parks Equestrian Camping"),
        ("AR", "Arkansas", fetch_ar_state_parks, "Arkansas State Parks Horse Camping"),
        ("AZ", "Arizona", fetch_az_state_parks, "Arizona State Parks Equestrian Camping"),
        ("CA", "California", fetch_ca_state_parks, "California State Parks Open Data"),
        ("CO", "Colorado", fetch_co_state_parks, "Colorado State Parks Equestrian Camping"),
        ("CT", "Connecticut", fetch_ct_state_parks, "Connecticut State Parks Equestrian Camping"),
        ("DE", "Delaware", fetch_de_state_parks, "Delaware State Parks Equestrian Camping"),
        ("FL", "Florida", fetch_fl_state_parks, "Florida State Parks Equestrian Camping"),
        ("GA", "Georgia", fetch_ga_state_parks, "Georgia State Parks Equestrian Camping"),
        ("HI", "Hawaii", fetch_hi_state_parks, "Hawaii State Parks Equestrian Camping"),
        ("IA", "Iowa", fetch_ia_state_parks, "Iowa State Parks Equestrian Camping"),
        ("ID", "Idaho", fetch_id_state_parks, "Idaho State Parks Equestrian Camping"),
        ("IL", "Illinois", fetch_il_state_parks, "Illinois DNR Equestrian Camping"),
        ("IN", "Indiana", fetch_in_state_parks, "Indiana DNR Horse Camping"),
        ("KS", "Kansas", fetch_ks_state_parks, "Kansas State Parks Equestrian Camping"),
        ("KY", "Kentucky", fetch_ky_state_parks, "Kentucky State Parks Horse Camping"),
        ("LA", "Louisiana", fetch_la_state_parks, "Louisiana State Parks Equestrian Camping"),
        ("MA", "Massachusetts", fetch_ma_state_parks, "Massachusetts State Parks Equestrian Camping"),
        ("MD", "Maryland", fetch_md_state_parks, "Maryland State Parks Equestrian Camping"),
        ("ME", "Maine", fetch_me_state_parks, "Maine State Parks Equestrian Camping"),
        ("MI", "Michigan", fetch_mi_state_parks, "Michigan DNR Equestrian Campgrounds"),
        ("MN", "Minnesota", fetch_mn_state_parks, "Minnesota DNR Horse Campgrounds"),
        ("MO", "Missouri", fetch_mo_state_parks, "Missouri State Parks Equestrian Campgrounds"),
        ("MS", "Mississippi", fetch_ms_state_parks, "Mississippi State Parks Equestrian Camping"),
        ("MT", "Montana", fetch_mt_state_parks, "Montana State Parks Equestrian Camping"),
        ("NC", "North Carolina", fetch_nc_state_parks, "North Carolina State Parks Equestrian Camping"),
        ("ND", "North Dakota", fetch_nd_state_parks, "North Dakota State Parks Equestrian Camping"),
        ("NE", "Nebraska", fetch_ne_state_parks, "Nebraska State Parks Equestrian Camping"),
        ("NH", "New Hampshire", fetch_nh_state_parks, "New Hampshire State Parks Equestrian Camping"),
        ("NJ", "New Jersey", fetch_nj_state_parks, "New Jersey State Parks Equestrian Camping"),
        ("NM", "New Mexico", fetch_nm_state_parks, "New Mexico State Parks Equestrian Camping"),
        ("NV", "Nevada", fetch_nv_state_parks, "Nevada State Parks Equestrian Camping"),
        ("NY", "New York", fetch_ny_state_parks, "New York State Parks Equestrian Camping"),
        ("OH", "Ohio", fetch_oh_state_parks, "Ohio State Parks Bridle Camps"),
        ("OK", "Oklahoma", fetch_ok_state_parks, "Oklahoma State Parks Equestrian Camping"),
        ("OR", "Oregon", fetch_or_state_parks, "Oregon State Parks Equestrian Camping"),
        ("PA", "Pennsylvania", fetch_pa_state_parks, "Pennsylvania State Parks Horse Camping"),
        ("RI", "Rhode Island", fetch_ri_state_parks, "Rhode Island State Parks Equestrian Camping"),
        ("SC", "South Carolina", fetch_sc_state_parks, "South Carolina State Parks Equestrian Camping"),
        ("SD", "South Dakota", fetch_sd_state_parks, "South Dakota State Parks Equestrian Camping"),
        ("TN", "Tennessee", fetch_tn_state_parks, "Tennessee State Parks Horse Camping"),
        ("TX", "Texas", fetch_tx_state_parks, "Texas Parks & Wildlife Equestrian Camping"),
        ("UT", "Utah", fetch_ut_state_parks, "Utah State Parks Equestrian Camping"),
        ("VA", "Virginia", fetch_va_state_parks, "Virginia State Parks Horse Camping"),
        ("VT", "Vermont", fetch_vt_state_parks, "Vermont State Parks Equestrian Camping"),
        ("WA", "Washington", fetch_wa_state_parks, "Washington State Parks Equestrian Camping"),
        ("WI", "Wisconsin", fetch_wi_state_parks, "Wisconsin DNR Equestrian Campsites"),
        ("WV", "West Virginia", fetch_wv_state_parks, "West Virginia State Parks Equestrian Camping"),
        ("WY", "Wyoming", fetch_wy_state_parks, "Wyoming State Parks Equestrian Camping"),
    ]

    state_park_totals = {}
    state_park_sources = []
    for abbr, state_name, fetcher, source_label in state_park_jobs:
        print(f"\nFetching {state_name} State Parks...")
        started = time.time()
        state_camps = fetcher()
        state_park_totals[abbr] = len(state_camps)
        state_park_sources.append(source_label)
        merged = merge_state(state_camps)
        elapsed = time.time() - started
        print(f"  {abbr} State Parks: {merged} new listings added [{elapsed:.1f}s]")

    print_section("Partner Sources")
    print("Merging HorseMotel.com partner listings...")
    horsemotel_new, horsemotel_duplicate_ids, horsemotel_duplicate_nearby, horsemotel_duplicate_nearby_details = merge_camps_by_id_and_proximity(
        fetch_horsemotel_listings(),
        all_camps,
    )
    print_metric("HorseMotel.com added", horsemotel_new)
    print_metric("HorseMotel.com duplicate IDs", horsemotel_duplicate_ids)
    print_metric("HorseMotel.com nearby duplicates", horsemotel_duplicate_nearby)

    print_section("Private / Curated Sources")
    print("Merging private camp listings...")
    private_camp_new, private_camp_duplicate_ids, private_camp_duplicate_nearby, private_camp_duplicate_nearby_details = merge_camps_by_id_and_proximity(
        fetch_private_camps(),
        all_camps,
    )
    print_metric("Private Camps added", private_camp_new)
    print_metric("Private Camps duplicate IDs", private_camp_duplicate_ids)
    print_metric("Private Camps nearby duplicates", private_camp_duplicate_nearby)

    print_section("Cleanup / Data Quality")
    print("Applying manual exclusions...")
    excluded_count = apply_exclusions(all_camps)

    print("\nRemoving invalid/non-horse listings...")
    invalid_count = remove_invalid_equestrian_listings(all_camps)

    print("\nApplying manual overrides...")
    override_count = apply_overrides(all_camps)

    camps_list = sorted(all_camps.values(), key=lambda c: (c["state"], c["name"]))
    retired_field_count = strip_public_feed_fields(camps_list)
    descriptions_before = [str(camp.get("description") or "") for camp in camps_list]
    camps_list = normalize_description_fields(camps_list)
    sanitized_description_count = sum(
        before != str(camp.get("description") or "")
        for before, camp in zip(descriptions_before, camps_list)
    )
    print_metric("Descriptions sanitized", sanitized_description_count)
    validate_public_feed(camps_list)
    output = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(camps_list),
        "sources": ["Recreation.gov RIDB", "NPS API", "U.S. Forest Service"] + state_park_sources + ["HorseMotel.com", "Private Camps"],
        "camps": camps_list,
    }
    output_path = REPO_ROOT / "camps.json"
    write_camps_json(output_path, output)

    source_counts = {}
    for camp in camps_list:
        source = camp.get("source") or "Unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
    horsemotel_count = source_counts.get("HorseMotel.com", 0)
    private_camp_count = source_counts.get("Private Camps", 0)
    state_parks_count = source_counts.get("State Parks", 0)
    ridb_count = source_counts.get("RIDB", 0)
    nps_count = source_counts.get("NPS", 0)
    usfs_count = source_counts.get("U.S. Forest Service", 0)
    verified_count = sum(1 for c in camps_list if c.get("isVerified"))

    print_section("Final Totals")
    print(f"Done. {len(camps_list)} total camps written to {output_path.relative_to(REPO_ROOT)}")

    print("\nFederal fetch totals:")
    print_metric("RIDB fetched before cleanup", total_ridb)
    print_metric("NPS fetched before cleanup", total_nps)
    print_metric("U.S. Forest Service fetched before dedupe", total_usfs)

    print("\nState park source totals:")
    for abbr in sorted(state_park_totals):
        print_metric(f"{abbr} State Parks", state_park_totals[abbr])

    print("\nPartner / curated merge:")
    print_metric("HorseMotel.com added", horsemotel_new)
    print_metric("HorseMotel.com duplicate IDs", horsemotel_duplicate_ids)
    print_metric("HorseMotel.com nearby duplicates", horsemotel_duplicate_nearby)
    print_nearby_duplicate_details("HorseMotel.com", horsemotel_duplicate_nearby_details)
    print_metric("Private Camps added", private_camp_new)
    print_metric("Private Camps duplicate IDs", private_camp_duplicate_ids)
    print_metric("Private Camps nearby duplicates", private_camp_duplicate_nearby)
    print_nearby_duplicate_details("Private Camps", private_camp_duplicate_nearby_details)

    print("\nFinal feed by source:")
    print_metric("RIDB", ridb_count)
    print_metric("NPS", nps_count)
    print_metric("U.S. Forest Service", usfs_count)
    print_metric("State Parks", state_parks_count)
    print_metric("HorseMotel.com", horsemotel_count)
    print_metric("Private Camps", private_camp_count)

    print("\nData quality adjustments:")
    print_metric("Exclusions applied", excluded_count)
    print_metric("Invalid/non-horse removed", invalid_count)
    print_metric("Overrides applied", override_count)
    print_metric("Omitted/default fields stripped", retired_field_count)

    print("\nOverall:")
    print_metric("Verified listings", verified_count)
    print_metric("Unique total", len(camps_list))

    print("\nLocation cache:")
    print_metric("Geocode cache hits", _geocode_stats["hits"])
    print_metric("Geocode cache misses (new)", _geocode_stats["misses"])
    print_metric("USFS state cache hits", _usfs_state_stats["hits"])
    print_metric("USFS state lookups (new)", _usfs_state_stats["misses"])
    print_metric("USFS unresolved states", _usfs_state_stats["unresolved"])
    write_geocode_cache()


if __name__ == "__main__":
    main()
