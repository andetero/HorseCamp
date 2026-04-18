#!/usr/bin/env python3
"""
HorseCamp Data Fetcher
Runs nightly via GitHub Actions.
Calls Recreation.gov (RIDB) and NPS APIs, writes results to camps.json
which is served at horsecampfinder.com/camps.json for the iOS app.

Required GitHub Secrets:
  RIDB_API_KEY  — from ridb.recreation.gov/profile
  NPS_API_KEY   — from developer.nps.gov/signup
"""

import os, json, time, re, requests
from datetime import datetime, timezone
from pathlib import Path

RIDB_KEY   = os.environ.get("RIDB_API_KEY", "")
NPS_KEY    = os.environ.get("NPS_API_KEY", "")

RIDB_BASE = "https://ridb.recreation.gov/api/v1"
NPS_BASE  = "https://developer.nps.gov/api/v1"

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
LAYOVERS_FILE = DATA_DIR / "layovers.json"
STATE_PARKS_DIR = DATA_DIR / "state_parks"

STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
    "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
    "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]


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

def strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()

def is_equestrian(text_blob):
    low = text_blob.lower()
    return any(k in low for k in EQUESTRIAN_KEYWORDS)

def safe_get(url, headers=None, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                print(f"  Rate limited — waiting 10s...")
                time.sleep(10)
            else:
                print(f"  HTTP {r.status_code} for {url}")
                return None
        except Exception as e:
            print(f"  Request error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return None

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

    numeric_float_fields = {"pricePerNight", "horseFeePerNight", "rating", "latitude", "longitude"}
    numeric_int_fields = {"maxRigLength", "stallCount", "paddockCount", "reviewCount", "seasonStart", "seasonEnd"}
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
def fetch_ridb_state(state):
    camps = {}
    headers = {"apikey": RIDB_KEY}
    search_terms = [
        ("activity", "9"),           # activity 9 = Horseback Riding
        ("query", "horse corral"),
        ("query", "equestrian"),
        ("query", "horse camp"),
        ("query", "horse stall"),
    ]

    for param_key, param_val in search_terms:
        offset = 0
        while True:
            params = {
                param_key: param_val,
                "state":   state,
                "limit":   50,
                "offset":  offset,
                "full":    "true",
            }
            data = safe_get(f"{RIDB_BASE}/facilities", headers=headers, params=params)
            if not data:
                break
            facilities = data.get("RECDATA", [])
            if not facilities:
                break

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

                # For activity=9 (Horseback Riding) searches, require equestrian
                # keywords in description/amenities too — not just the activity name.
                # This prevents generic multi-use areas (OHV parks etc.) from matching
                # simply because they list horseback riding as one of many activities.
                if param_key == "activity" and param_val == "9":
                    desc_amenity_blob = " ".join(amenities + [desc])
                    if not is_equestrian(desc_amenity_blob):
                        continue
                elif not is_equestrian(blob):
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
                    "rating":              0.0,
                    "reviewCount":         0,
                    "imageColors":         ["5C7A4E", "D4A853"],
                    "photoURLs":           parse_ridb_photos(f),
                    "source":              "RIDB",
                }

            offset += 50
            if len(facilities) < 50:
                break
            time.sleep(0.5)

        time.sleep(0.3)

    return list(camps.values())


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
            "rating":              0.0,
            "reviewCount":         0,
            "imageColors":         ["4A7FA5", "5C7A4E"],
            "photoURLs":           [img["url"] for img in (c.get("images") or []) if img.get("url")][:6],
            "source":              "NPS",
        })

    return camps



def _parse_osm_fee(tags):
    """Parse fee from OSM charge/fee tags. Returns 0.0 if unknown."""
    charge = tags.get("charge", "") or tags.get("fee:amount", "") or ""
    if charge:
        # Extract numeric value e.g. "5 USD", "10", "$5"
        import re
        match = re.search(r"[0-9]+(?:\.[0-9]+)?", charge.replace(",", "."))
        if match:
            try:
                return float(match.group())
            except:
                pass
    return 0.0


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
                "rating": 0.0,
                "reviewCount": 0,
                "imageColors": ["5C7A4E", "D4A853"],
                "photoURLs": [],
                "source": "State Parks",
                "sourceDetail": "CA State Parks",
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
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["B5543A", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "IL State Parks",
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

def fetch_al_state_parks():
    """Load manual AL state-park listings from data/state_parks/al.json."""
    return load_manual_state_parks("AL")

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

def fetch_osm(existing_camps):
    """
    Fetches horse-friendly campsites from OpenStreetMap via Overpass API.
    Uses the horse=yes tag which is explicitly set by OSM contributors.
    Deduplicates against existing RIDB/NPS/Layover camps by proximity (500m).
    Free, no API key required.
    """
    import math, urllib.request, urllib.parse

    def haversine_meters(lat1, lon1, lat2, lon2):
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    def is_duplicate(lat, lng, threshold_m=500):
        for camp in existing_camps.values():
            if haversine_meters(lat, lng, camp["latitude"], camp["longitude"]) < threshold_m:
                return True
        return False

    # Overpass query — all camp_sites with horse=yes in US bounding box
    # Bounding box: south=24, west=-127, north=50, east=-65
    query = """
[out:json][timeout:60];
(
  node["tourism"="camp_site"]["horse"="yes"](24,-127,50,-65);
  way["tourism"="camp_site"]["horse"="yes"](24,-127,50,-65);
  node["leisure"="horse_riding"]["access"="yes"](24,-127,50,-65);
);
out center;
"""
    # Try multiple Overpass API mirrors in case one is down/slow
    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]
    data = None
    for url in mirrors:
        try:
            encoded = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(url, data=encoded, method="POST")
            req.add_header("User-Agent", "HorseCamp/1.0 (horsecampfinder.com)")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            print(f"  OSM: connected via {url.split('/')[2]}")
            break
        except Exception as e:
            print(f"  OSM mirror failed ({url.split('/')[2]}): {e}")
            time.sleep(5)

    if not data:
        print("  OSM: all mirrors failed — skipping")
        return []

    STATES_BY_BBOX = {
        "AL":(30.1,84.9,35.0,88.5),"AK":(54.0,130.0,72.0,172.0),
        "AZ":(31.3,109.1,37.0,114.8),"AR":(33.0,89.6,36.5,94.6),
        "CA":(32.5,114.1,42.0,124.5),"CO":(36.9,102.0,41.0,109.1),
        "CT":(40.9,71.8,42.1,73.7),"DE":(38.4,75.0,39.8,75.8),
        "FL":(24.4,79.9,31.0,87.6),"GA":(30.4,80.8,35.0,85.6),
        "HI":(18.9,154.8,22.2,160.2),"ID":(41.9,111.0,49.0,117.2),
        "IL":(36.9,87.5,42.5,91.5),"IN":(37.8,84.8,41.8,88.1),
        "IA":(40.4,90.1,43.5,96.6),"KS":(36.9,94.6,40.0,102.1),
        "KY":(36.5,81.9,39.1,89.6),"LA":(28.9,88.8,33.0,94.1),
        "ME":(43.0,66.9,47.5,71.1),"MD":(37.9,74.9,39.7,79.5),
        "MA":(41.2,69.9,42.9,73.5),"MI":(41.7,82.1,48.3,90.4),
        "MN":(43.5,89.5,49.4,97.2),"MS":(30.1,88.1,35.0,91.7),
        "MO":(35.9,89.1,40.6,95.8),"MT":(44.4,104.0,49.0,116.1),
        "NE":(40.0,95.3,43.0,104.1),"NV":(35.0,114.0,42.0,120.0),
        "NH":(42.7,70.7,45.3,72.6),"NJ":(38.9,73.9,41.4,75.6),
        "NM":(31.3,103.0,37.0,109.1),"NY":(40.5,71.8,45.0,79.8),
        "NC":(33.8,75.5,36.6,84.3),"ND":(45.9,96.6,49.0,104.1),
        "OH":(38.4,80.5,42.3,84.8),"OK":(33.6,94.4,37.0,103.0),
        "OR":(41.9,116.5,46.3,124.7),"PA":(39.7,74.7,42.3,80.5),
        "RI":(41.1,71.2,42.0,71.9),"SC":(32.0,78.5,35.2,83.4),
        "SD":(42.5,96.4,45.9,104.1),"TN":(34.9,81.6,36.7,90.3),
        "TX":(25.8,93.5,36.5,106.7),"UT":(36.9,109.0,42.0,114.1),
        "VT":(42.7,71.5,45.0,73.4),"VA":(36.5,75.2,39.5,83.7),
        "WA":(45.5,116.9,49.0,124.8),"WV":(37.2,77.7,40.6,82.6),
        "WI":(42.5,86.8,47.1,92.9),"WY":(40.9,104.0,45.0,111.1),
    }

    def guess_state(lat, lng):
        for state, (slat, slng, nlat, nlng) in STATES_BY_BBOX.items():
            if slat <= lat <= nlat and slng <= abs(lng) <= nlng:
                return state
        return ""

    camps = {}
    skipped_dup = 0

    for element in data.get("elements", []):
        # Get coordinates
        if element["type"] == "node":
            lat = element.get("lat", 0)
            lng = element.get("lon", 0)
        else:
            center = element.get("center", {})
            lat = center.get("lat", 0)
            lng = center.get("lon", 0)

        if not lat or not lng:
            continue

        if is_duplicate(lat, lng):
            skipped_dup += 1
            continue

        tags = element.get("tags", {})
        name = tags.get("name", "")
        if not name:
            continue

        phone = tags.get("phone", tags.get("contact:phone", ""))
        website = tags.get("website", tags.get("contact:website", ""))
        state = guess_state(lat, lng)

        # Build hookups from OSM tags
        hookups = []
        if tags.get("electric_hookup") == "yes" or tags.get("power_supply") == "yes":
            hookups.append("30A")
        if tags.get("water_point") == "yes" or tags.get("drinking_water") == "yes":
            hookups.append("Water")

        # Build accommodations from actual OSM tags
        accommodations = []
        if tags.get("horse_stables") == "yes" or tags.get("stables") == "yes":
            accommodations.append("Stalls")
        if tags.get("horse_riding") == "yes" or tags.get("paddock") == "yes":
            accommodations.append("Corrals")
        if tags.get("horse_trail") == "yes" or tags.get("hiking") == "yes":
            accommodations.append("Trails")
        if tags.get("cabin") == "yes" or tags.get("tourism") == "cabin":
            accommodations.append("Cabins")

        eid = f"osm-{element['id']}"
        camps[eid] = {
            "id":                  eid,
            "name":                name,
            "location":            f"{state}".strip(", "),
            "state":               state,
            "latitude":            lat,
            "longitude":           lng,
            "pricePerNight":       _parse_osm_fee(tags),
            "horseFeePerNight":    0.0,
            "hookups":             hookups,
            "accommodations":      list(dict.fromkeys(accommodations)),
            "maxRigLength":        0,
            "stallCount":          0,
            "paddockCount":        0,
            "phone":               phone,
            "website":             website,
            "description":         f"Horse-friendly campsite. Verify amenities before arrival.",
            "isVerified":          False,
            "seasonStart":         0,
            "seasonEnd":           0,
            "hasWashRack":         False,
            "hasDumpStation":      tags.get("sanitary_dump_station") == "yes",
            "hasWifi":             tags.get("internet_access") == "yes",
            "hasBathhouse":        tags.get("shower") == "yes",
            "pullThroughAvailable": False,
            "rating":              0.0,
            "reviewCount":         0,
            "imageColors":         ["8B5E3C", "D4A853"],
            "photoURLs":           [],
            "source":              "OSM",
        }

    result = list(camps.values())
    print(f"  OSM: {len(result)} new camps ({skipped_dup} duplicates skipped)")
    return result


# ── LAYOVER LISTINGS ───────────────────────────────────────────────────
# Curated horse layover facilities — private barns and farms that
# welcome overnight horse travelers. Call to verify before arrival.
# Source data now lives in data/layovers.json so new layovers can be
# added without editing Python code.
def fetch_layovers():
    if not LAYOVERS_FILE.exists():
        raise FileNotFoundError(
            f"Missing layovers file: {LAYOVERS_FILE}. "
            "Create data/layovers.json before running the fetch."
        )

    with LAYOVERS_FILE.open("r", encoding="utf-8") as f:
        layovers = json.load(f)

    if not isinstance(layovers, list):
        raise ValueError("data/layovers.json must contain a JSON array of layover listings")

    for i, layover in enumerate(layovers, start=1):
        if not isinstance(layover, dict):
            raise ValueError(f"Layover #{i} in data/layovers.json is not a JSON object")
        for field in ("id", "name", "location", "state", "latitude", "longitude", "source"):
            if field not in layover:
                raise ValueError(f"Layover #{i} is missing required field: {field}")

    return layovers



def fetch_nv_state_parks():
    """Load manual NV state-park listings from data/state_parks/nv.json."""
    return load_manual_state_parks("NV")

def fetch_ok_state_parks():
    """Load manual OK state-park listings from data/state_parks/ok.json."""
    return load_manual_state_parks("OK")

def fetch_ks_state_parks():
    """Load manual KS state-park listings from data/state_parks/ks.json."""
    return load_manual_state_parks("KS")

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
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "LA State Parks",
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
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "LA State Parks",
        },
    ]
    print(f"  Louisiana State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks

# ── MAIN ───────────────────────────────────────────────────────────────




def _geocode_place_nominatim(query):
    """Geocode a place name using Nominatim. Returns (lat, lon) or (0,0)."""
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
            return float(data[0].get("lat", 0) or 0), float(data[0].get("lon", 0) or 0)
        except Exception:
            return 0.0, 0.0
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
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "FL State Parks",
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
                        return float(arr[0]["lat"]), float(arr[0]["lon"])
            except Exception:
                pass
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
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E59866"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "MI State Parks",
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
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "MO State Parks",
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

    all_camps = {}
    total_ridb = 0
    total_nps = 0

    for i, state in enumerate(STATES):
        state_started = time.time()
        print(f"[{i+1}/{len(STATES)}] {state}...", end=" ", flush=True)
        ridb_camps = fetch_ridb_state(state) if RIDB_KEY else []
        nps_camps = fetch_nps_state(state) if NPS_KEY else []
        state_new = 0
        for camp in ridb_camps + nps_camps:
            cid = camp["id"]
            if cid not in all_camps:
                all_camps[cid] = camp
                state_new += 1
        total_ridb += len(ridb_camps)
        total_nps += len(nps_camps)
        elapsed = time.time() - state_started
        print(f"{len(ridb_camps)} RIDB + {len(nps_camps)} NPS = {state_new} new [{elapsed:.1f}s]")
        time.sleep(0.5)

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

    print("\nMerging layover listings...")
    import math as _math
    layover_new = 0
    for camp in fetch_layovers():
        cid = camp["id"]
        if cid not in all_camps:
            lat, lng = camp["latitude"], camp["longitude"]
            dup = False
            for ex in all_camps.values():
                dlat = _math.radians(lat - ex["latitude"])
                dlng = _math.radians(lng - ex["longitude"])
                a = _math.sin(dlat/2)**2 + _math.cos(_math.radians(lat))*_math.cos(_math.radians(ex["latitude"]))*_math.sin(dlng/2)**2
                if 6371000 * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1-a)) < 500:
                    dup = True
                    break
            if not dup:
                all_camps[cid] = camp
                layover_new += 1
    print(f"  Layovers: {layover_new} new listings added")

    print("\nFetching from OpenStreetMap...")
    osm_camps = fetch_osm(all_camps)
    for camp in osm_camps:
        cid = camp["id"]
        if cid not in all_camps:
            all_camps[cid] = camp

    print("\nApplying manual exclusions...")
    excluded_count = apply_exclusions(all_camps)

    print("\nApplying manual overrides...")
    override_count = apply_overrides(all_camps)

    camps_list = sorted(all_camps.values(), key=lambda c: (c["state"], c["name"]))
    output = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(camps_list),
        "sources": ["Recreation.gov RIDB", "NPS API"] + state_park_sources + ["OpenStreetMap", "Layover"],
        "camps": camps_list,
    }
    output_path = REPO_ROOT / "camps.json"
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    osm_count = sum(1 for c in camps_list if c.get("source") == "OSM")
    layover_count = sum(1 for c in camps_list if c.get("source") == "Layover")
    verified_count = sum(1 for c in camps_list if c.get("isVerified"))
    print(f"\nDone. {len(camps_list)} total camps written to {output_path.relative_to(REPO_ROOT)}")
    print(f"  RIDB:         {total_ridb}")
    print(f"  NPS:          {total_nps}")
    for abbr in sorted(state_park_totals):
        print(f"  {abbr} StateParks:{state_park_totals[abbr]}")
    print(f"  Layovers:     {layover_count}")
    print(f"  OSM:          {osm_count}")
    print(f"  Excluded:     {excluded_count}")
    print(f"  Overrides:    {override_count}")
    print(f"  Verified:     {verified_count}")
    print(f"  Unique total: {len(camps_list)}")


if __name__ == "__main__":
    main()
