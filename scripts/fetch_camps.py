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

STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
    "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
    "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"
]

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

# ── VERIFIED OVERRIDES ────────────────────────────────────────────────
import csv, os

OVERRIDE_FILE = str(REPO_ROOT / "verified_overrides.csv")

# Columns in the override CSV
OVERRIDE_COLUMNS = [
    "id", "name", "location", "state", "phone",
    "hookups", "accommodations", "pricePerNight", "horseFeePerNight",
    "maxRigLength", "stallCount", "paddockCount",
    "hasWashRack", "hasDumpStation", "hasWifi", "hasBathhouse",
    "pullThroughAvailable", "description", "photoURL", "verified", "notes"
]

def generate_override_template(camps_dict):
    """Generate verified_overrides.csv as a blank template.
    Called only when the file doesn't exist yet.
    Rows are pre-populated with current data so wife can see what's there."""
    rows = []
    for camp in sorted(camps_dict.values(), key=lambda c: (c["state"], c["name"])):
        rows.append({
            "id":                   camp["id"],
            "name":                 camp["name"],
            "location":             camp["location"],
            "state":                camp["state"],
            "phone":                camp["phone"],
            "hookups":              "|".join(camp["hookups"]),
            "accommodations":       "|".join(camp["accommodations"]),
            "pricePerNight":        camp["pricePerNight"],
            "horseFeePerNight":     camp["horseFeePerNight"],
            "maxRigLength":         camp["maxRigLength"],
            "stallCount":           camp["stallCount"],
            "paddockCount":         camp["paddockCount"],
            "hasWashRack":          camp["hasWashRack"],
            "hasDumpStation":       camp["hasDumpStation"],
            "hasWifi":              camp["hasWifi"],
            "hasBathhouse":         camp["hasBathhouse"],
            "pullThroughAvailable": camp["pullThroughAvailable"],
            "description":          camp["description"],
            "verified":             "",   # wife fills: YES / NO / CLOSED
            "notes":                "",   # wife fills: anything useful
        })

    with open(OVERRIDE_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OVERRIDE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Generated {OVERRIDE_FILE} with {len(rows)} camps — share with verifier")

def apply_overrides(camps_dict):
    """Read verified_overrides.csv and apply any edits to camps_dict.
    Only rows where 'verified' is non-empty are applied.
    Returns count of overrides applied."""
    if not os.path.exists(OVERRIDE_FILE):
        return 0

    applied = 0
    closed = 0

    with open(OVERRIDE_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cid = row.get("id", "").strip()
            verified = row.get("verified", "").strip().upper()

            if not cid or not verified:
                continue  # skip unverified rows

            # Mark closed camps — remove from dict entirely
            if verified == "CLOSED":
                if cid in camps_dict:
                    del camps_dict[cid]
                    closed += 1
                continue

            if verified != "YES":
                continue

            if cid not in camps_dict:
                continue

            camp = camps_dict[cid]

            # Apply only non-empty fields
            if row.get("name", "").strip():
                camp["name"] = row["name"].strip()
            if row.get("phone", "").strip():
                camp["phone"] = row["phone"].strip()
            if row.get("hookups", "").strip():
                camp["hookups"] = [h.strip() for h in row["hookups"].split("|") if h.strip()]
            if row.get("accommodations", "").strip():
                camp["accommodations"] = [a.strip() for a in row["accommodations"].split("|") if a.strip()]
            if row.get("pricePerNight", "").strip():
                try: camp["pricePerNight"] = float(row["pricePerNight"])
                except: pass
            if row.get("horseFeePerNight", "").strip():
                try: camp["horseFeePerNight"] = float(row["horseFeePerNight"])
                except: pass
            if row.get("maxRigLength", "").strip():
                try: camp["maxRigLength"] = int(row["maxRigLength"])
                except: pass
            if row.get("stallCount", "").strip():
                try: camp["stallCount"] = int(row["stallCount"])
                except: pass
            if row.get("paddockCount", "").strip():
                try: camp["paddockCount"] = int(row["paddockCount"])
                except: pass
            for bool_field in ["hasWashRack","hasDumpStation","hasWifi","hasBathhouse","pullThroughAvailable"]:
                val = row.get(bool_field, "").strip().upper()
                if val in ("TRUE","YES","1"):
                    camp[bool_field] = True
                elif val in ("FALSE","NO","0"):
                    camp[bool_field] = False
            if row.get("description", "").strip():
                camp["description"] = row["description"].strip()
            if row.get("photoURL", "").strip():
                camp["photoURLs"] = [row["photoURL"].strip()]

            camp["isVerified"] = True
            camps_dict[cid] = camp
            applied += 1

    print(f"  Overrides applied: {applied} updated, {closed} marked closed")
    return applied + closed

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
    """Fetch a conservative first-pass set of Tennessee state-park horse-camping listings.

    Tennessee's official state-park web surfaces are fragmented. This first pass
    includes only parks where official Tennessee State Parks reservation pages
    explicitly support both horseback riding and overnight camping in the same
    park.
    """
    camps_data = [
        {
            "name": "Natchez Trace State Park Horse Camp",
            "location": "Wildersville, TN",
            "latitude": 35.5316,
            "longitude": -88.2820,
            "description": "Official Tennessee State Parks listing where Natchez Trace State Park offers both horseback riding and camping.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "phone": "(731) 968-3742",
            "hasBathhouse": True,
            "hasWashRack": False,
        },
        {
            "name": "Rocky Fork State Park Horse Camp",
            "location": "Flag Pond, TN",
            "latitude": 36.0743,
            "longitude": -82.6068,
            "description": "Official Tennessee State Parks listing where Rocky Fork State Park offers horseback riding and backcountry camping.",
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "phone": "(423) 271-1233",
            "hasBathhouse": False,
            "hasWashRack": False,
        },
    ]

    camps = []
    for i, c in enumerate(camps_data, start=1):
        camps.append({
            "id": f"tnsp-{i}",
            "name": c["name"],
            "location": c["location"],
            "state": "TN",
            "latitude": c["latitude"],
            "longitude": c["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": c.get("hookups", []),
            "accommodations": c.get("accommodations", ["Trails"]),
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": c.get("phone", ""),
            "website": "https://reserve.tnstateparks.com/",
            "description": c["description"],
            "isVerified": False,
            "seasonStart": 0,
            "seasonEnd": 0,
            "hasWashRack": c.get("hasWashRack", False),
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": c.get("hasBathhouse", False),
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "TN State Parks",
        })

    print(f"  Tennessee State Parks: {len(camps)} provisional overnight horse-camping listings")
    return camps

def fetch_ar_state_parks():
    """Fetch a conservative first-pass set of Arkansas state-park horse camps.

    Based on official Arkansas State Parks sources, the parks with clear
    overnight equestrian camping signals are Village Creek (equestrian
    campground), Devil's Den (horse campground), and Mount Magazine
    (Horse Camp gateway to Huckleberry trail). Hobbs is excluded because the
    official article says it does not have equestrian campgrounds, and Lake
    Catherine is excluded because visitors cannot bring their own horses.
    """
    camps_data = [
        {
            "name": "Village Creek State Park Horse Camp",
            "location": "Wynne, AR",
            "latitude": 35.1292,
            "longitude": -90.7890,
            "description": "Arkansas State Parks equestrian campground with stable facilities and Class B campsites at Village Creek State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Stalls", "Trails"],
            "stallCount": 66,
            "hasBathhouse": True,
            "hasWashRack": True,
            "phone": "(870) 238-9406",
            "website": "https://www.arkansasstateparks.com/parks/village-creek-state-park",
        },
        {
            "name": "Devil's Den State Park Horse Campground",
            "location": "West Fork, AR",
            "latitude": 35.7806,
            "longitude": -94.2497,
            "description": "Arkansas State Parks horse campground with hookups, bathhouse, and direct access to horse trails at Devil's Den State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Highlines", "Trails"],
            "hasBathhouse": True,
            "phone": "(479) 761-3325",
            "website": "https://www.arkansasstateparks.com/parks/devils-den-state-park",
        },
        {
            "name": "Mount Magazine State Park Horse Camp",
            "location": "Paris, AR",
            "latitude": 35.1678,
            "longitude": -93.6455,
            "description": "Arkansas State Parks Horse Camp at Mount Magazine State Park, used as a gateway to the Huckleberry Mountain Horse Trail.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "phone": "(479) 963-8502",
            "website": "https://www.arkansasstateparks.com/parks/mount-magazine-state-park/things-to-do/horseback-riding",
        },
    ]

    camps = []
    for i, item in enumerate(camps_data, 1):
        camps.append({
            "id": f"arstate-{i}",
            "name": item["name"],
            "location": item["location"],
            "state": "AR",
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": item["hookups"],
            "accommodations": item["accommodations"],
            "maxRigLength": 0,
            "stallCount": item.get("stallCount", 0),
            "paddockCount": item.get("paddockCount", 0),
            "phone": item.get("phone", ""),
            "website": item.get("website", ""),
            "description": item["description"],
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": item.get("hasWashRack", False),
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": item.get("hasBathhouse", False),
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "AR State Parks",
        })

    print(f"  Arkansas State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_va_state_parks():
    """Fetch official Virginia State Parks horse-camping locations.

    Virginia State Parks officially says six parks offer horse camping.
    Uses a strict fixed allowlist with fixed coordinates to avoid geocoding failures.
    """
    camps_data = [
        {
            "name": "Douthat State Park Equestrian Campground",
            "location": "Millboro, VA",
            "latitude": 37.8960,
            "longitude": -79.8036,
            "description": "Virginia State Parks equestrian campground at Douthat State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "hasBathhouse": True,
            "hasWashRack": False,
            "phone": "(540) 862-8100",
        },
        {
            "name": "Fairy Stone State Park Equestrian Campground",
            "location": "Stuart, VA",
            "latitude": 36.7772,
            "longitude": -80.1124,
            "description": "Virginia State Parks equestrian campground at Fairy Stone State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "hasBathhouse": False,
            "hasWashRack": False,
            "phone": "(276) 930-2424",
        },
        {
            "name": "Grayson Highlands State Park Equestrian Camp",
            "location": "Mouth of Wilson, VA",
            "latitude": 36.6280,
            "longitude": -81.5014,
            "description": "Virginia State Parks equestrian campground at Grayson Highlands State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "hasBathhouse": False,
            "hasWashRack": False,
            "phone": "(276) 579-7092",
        },
        {
            "name": "James River State Park Equestrian Campground",
            "location": "Gladstone, VA",
            "latitude": 37.6257,
            "longitude": -78.8308,
            "description": "Virginia State Parks equestrian campground at James River State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "hasBathhouse": True,
            "hasWashRack": False,
            "phone": "(434) 933-4355",
        },
        {
            "name": "Occoneechee State Park Equestrian Campground",
            "location": "Clarksville, VA",
            "latitude": 36.6206,
            "longitude": -78.5415,
            "description": "Virginia State Parks equestrian campground at Occoneechee State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "hasBathhouse": False,
            "hasWashRack": False,
            "phone": "(434) 374-2210",
        },
        {
            "name": "Staunton River State Park Equestrian Campground",
            "location": "Scottsburg, VA",
            "latitude": 36.6866,
            "longitude": -78.6691,
            "description": "Virginia State Parks equestrian campground at Staunton River State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "hasBathhouse": True,
            "hasWashRack": False,
            "phone": "(434) 572-4623",
        },
    ]
    camps = []
    for p in camps_data:
        camps.append({
            "id": "va-stateparks-" + re.sub(r'[^a-z0-9]+', '-', p["name"].lower()).strip('-'),
            "name": p["name"],
            "location": p["location"],
            "state": "VA",
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": p["phone"],
            "website": "https://www.dcr.virginia.gov/state-parks/horse-camping-trails",
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 3,
            "seasonEnd": 11,
            "hasWashRack": p["hasWashRack"],
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": p["hasBathhouse"],
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "VA State Parks",
        })
    print(f"  Virginia State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_ga_state_parks():
    """Fetch official Georgia State Parks equestrian-camping locations.

    Georgia State Parks officially says equestrian campsites and stables are
    available at A.H. Stephens, Hard Labor Creek, and Watson Mill Bridge.
    Uses a strict fixed allowlist with fixed coordinates to avoid geocoding failures.
    """
    camps_data = [
        {
            "name": "A.H. Stephens State Park Equestrian Campground",
            "location": "Crawfordville, GA",
            "latitude": 33.5687,
            "longitude": -82.8813,
            "description": "Georgia State Parks equestrian campsites and horse stalls at A.H. Stephens State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Stalls", "Trails"],
            "stallCount": 20,
            "hasBathhouse": True,
            "hasWashRack": False,
            "phone": "(706) 456-2602",
            "website": "https://gastateparks.org/AHStephens",
        },
        {
            "name": "Hard Labor Creek State Park Equestrian Campground",
            "location": "Rutledge, GA",
            "latitude": 33.6269,
            "longitude": -83.6204,
            "description": "Georgia State Parks equestrian campsites and horse stalls at Hard Labor Creek State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Stalls", "Trails"],
            "stallCount": 11,
            "hasBathhouse": True,
            "hasWashRack": False,
            "phone": "(706) 557-3001",
            "website": "https://gastateparks.org/HardLaborCreek",
        },
        {
            "name": "Watson Mill Bridge State Park Equestrian Campground",
            "location": "Comer, GA",
            "latitude": 34.0435,
            "longitude": -83.0942,
            "description": "Georgia State Parks equestrian campsites and horse stalls at Watson Mill Bridge State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Stalls", "Trails"],
            "stallCount": 11,
            "hasBathhouse": True,
            "hasWashRack": False,
            "phone": "(706) 783-5349",
            "website": "https://gastateparks.org/WatsonMillBridge",
        },
    ]

    camps = []
    for i, c in enumerate(camps_data, start=1):
        camps.append({
            "id": f"ga-statepark-{i}",
            "name": c["name"],
            "location": c["location"],
            "state": "GA",
            "latitude": c["latitude"],
            "longitude": c["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": c["hookups"],
            "accommodations": c["accommodations"],
            "maxRigLength": 0,
            "stallCount": c["stallCount"],
            "paddockCount": 0,
            "phone": c["phone"],
            "website": c["website"],
            "description": c["description"][:2000],
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": c["hasWashRack"],
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": c["hasBathhouse"],
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "GA State Parks",
        })
    print(f"  Georgia State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_nc_state_parks():
    """Fetch a conservative first-pass set of North Carolina State Parks equestrian camps.

    Official NC State Parks equestrian-camping catalog currently surfaces South Mountains
    State Park – Jacob Fork Access and Medoc Mountain State Park as equestrian-camping parks.
    Uses fixed coordinates to avoid geocoding/rate-limit issues.
    """
    camps_data = [
        {
            "name": "South Mountains State Park – Jacob Fork Equestrian Campground",
            "location": "Connelly Springs, NC",
            "latitude": 35.6358,
            "longitude": -81.7391,
            "description": "North Carolina State Parks equestrian camping at South Mountains State Park – Jacob Fork Access.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": True,
            "hasWashRack": False,
            "pullThroughAvailable": False,
            "phone": "(828) 433-4772",
            "website": "https://www.ncparks.gov/catalog-category/equestrian-camping",
        },
        {
            "name": "Medoc Mountain State Park Equestrian Campground",
            "location": "Hollister, NC",
            "latitude": 36.2527,
            "longitude": -77.8864,
            "description": "North Carolina State Parks primitive equestrian camping at Medoc Mountain State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": True,
            "hasBathhouse": True,
            "hasWashRack": False,
            "pullThroughAvailable": False,
            "phone": "(252) 586-6588",
            "website": "https://www.ncparks.gov/state-parks/medoc-mountain-state-park",
        },
    ]

    camps = []
    for i, c in enumerate(camps_data, start=1):
        camps.append({
            "id": f"nc-statepark-{i}",
            "name": c["name"],
            "location": c["location"],
            "state": "NC",
            "latitude": c["latitude"],
            "longitude": c["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": c["hookups"],
            "accommodations": c["accommodations"],
            "maxRigLength": c["maxRigLength"],
            "stallCount": c["stallCount"],
            "paddockCount": c["paddockCount"],
            "phone": c["phone"],
            "website": c["website"],
            "description": c["description"],
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": c["hasWashRack"],
            "hasDumpStation": c["hasDumpStation"],
            "hasWifi": False,
            "hasBathhouse": c["hasBathhouse"],
            "pullThroughAvailable": c["pullThroughAvailable"],
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NC State Parks",
        })

    print(f"  North Carolina State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_az_state_parks():
    """Fetch official Arizona State Parks equestrian-camping locations.

    Arizona State Parks does not appear to publish a statewide equestrian-camping
    allowlist. Catalina State Park is officially confirmed to offer an
    equestrian staging and camping area with 16 pens, first-come first-served,
    water, corrals, hitching posts, restroom, and overnight camping fees.
    """
    camps = []
    camps.append({
        "id": "az-state-catalina-equestrian-center",
        "name": "Catalina State Park Equestrian Center",
        "location": "Tucson, AZ",
        "state": "AZ",
        "latitude": 32.4237,
        "longitude": -110.9106,
        "pricePerNight": 25.0,
        "horseFeePerNight": 0.0,
        "hookups": ["Water"],
        "accommodations": ["Corrals", "Trails"],
        "maxRigLength": 0,
        "stallCount": 16,
        "paddockCount": 0,
        "phone": "520-628-5798",
        "website": "https://azstateparks.com/catalina/explore/facility-information",
        "description": "Arizona State Parks equestrian staging and camping area at Catalina State Park. First-come, first-served with 16 pens, water, restroom, hitching posts, fire pits, and shared-use trail access.",
        "isVerified": False,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": False,
        "hasDumpStation": False,
        "hasWifi": False,
        "hasBathhouse": False,
        "pullThroughAvailable": False,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": ["C0392B", "E3A18B"],
        "photoURLs": [],
        "source": "State Parks",
        "sourceDetail": "AZ State Parks",
    })
    print(f"  Arizona State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_ny_state_parks():
    """Fetch official New York State Parks equestrian-camping locations.

    New York does not appear to publish a clean statewide equestrian-camping
    allowlist. This first pass keeps NY conservative and only includes
    Allegany State Park, where official state park planning documents describe
    an equestrian camping/staging area near Camp 10 with campsites and support
    facilities.
    """
    camps = []
    camps.append({
        "id": "ny-state-allegany-equestrian-camp",
        "name": "Allegany State Park Equestrian Camp",
        "location": "Salamanca, NY",
        "state": "NY",
        "latitude": 42.0828,
        "longitude": -78.7434,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": [],
        "accommodations": ["Trails", "Highlines"],
        "maxRigLength": 0,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "(716) 354-9101",
        "website": "https://parks.ny.gov/regions/Allegany/default.aspx",
        "description": "Conservative New York State Parks first pass based on official Allegany State Park planning material describing an equestrian camping and staging area near Camp 10.",
        "isVerified": False,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": False,
        "hasDumpStation": False,
        "hasWifi": False,
        "hasBathhouse": False,
        "pullThroughAvailable": False,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": ["C0392B", "E3A18B"],
        "photoURLs": [],
        "source": "State Parks",
        "sourceDetail": "NY State Parks",
    })
    print(f"  New York State Parks: {len(camps)} conservative equestrian-camping listings")
    return camps

def fetch_mn_state_parks():
    """Fetch official Minnesota DNR horse campgrounds.

    Minnesota's official DNR state forest camping page explicitly lists six state
    forests with horse campgrounds: Beltrami Island, George Washington,
    Huntersville, Richard J. Dorer Memorial Hardwood, Sand Dunes, and St. Croix.
    This first pass uses a strict fixed allowlist with stable coordinates.
    """
    camps_data = [
        {
            "id": "mn-state-beltrami-island-horse-camp",
            "name": "Beltrami Island State Forest Horse Camp",
            "location": "Wannaska, MN",
            "latitude": 48.6508,
            "longitude": -95.3214,
            "phone": "(218) 308-2372",
        },
        {
            "id": "mn-state-george-washington-horse-camp",
            "name": "George Washington State Forest Horse Camp",
            "location": "Outing, MN",
            "latitude": 46.8454,
            "longitude": -93.9864,
            "phone": "(218) 372-3182",
        },
        {
            "id": "mn-state-huntersville-horse-camp",
            "name": "Huntersville State Forest Horse Camp",
            "location": "Menahga, MN",
            "latitude": 46.7390,
            "longitude": -95.1084,
            "phone": "(218) 732-3296",
        },
        {
            "id": "mn-state-richard-dorer-horse-camp",
            "name": "Richard J. Dorer Memorial Hardwood State Forest Horse Camp",
            "location": "Altura, MN",
            "latitude": 44.0512,
            "longitude": -91.9178,
            "phone": "(507) 932-3007",
        },
        {
            "id": "mn-state-sand-dunes-horse-camp",
            "name": "Sand Dunes State Forest Horse Camp",
            "location": "Zimmerman, MN",
            "latitude": 45.4796,
            "longitude": -93.6140,
            "phone": "(763) 689-7101",
        },
        {
            "id": "mn-state-st-croix-horse-camp",
            "name": "St. Croix State Forest Horse Camp",
            "location": "Hinckley, MN",
            "latitude": 46.0204,
            "longitude": -92.9297,
            "phone": "(320) 384-7721",
        },
    ]

    camps = []
    for d in camps_data:
        camps.append({
            "id": d["id"],
            "name": d["name"],
            "location": d["location"],
            "state": "MN",
            "latitude": d["latitude"],
            "longitude": d["longitude"],
            "pricePerNight": 22.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": d["phone"],
            "website": "https://www.dnr.state.mn.us/state_forests/camping.html",
            "description": "Official Minnesota DNR horse campground in a state forest. Minnesota DNR lists horse campgrounds in Beltrami Island, George Washington, Huntersville, Richard J. Dorer Memorial Hardwood, Sand Dunes, and St. Croix state forests.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "MN State Parks",
        })
    print(f"  Minnesota State Parks: {len(camps)} official horse-camp listings")
    return camps


def fetch_co_state_parks():
    """Fetch conservative Colorado State Parks equestrian camps.

    This first pass includes parks where Colorado Parks & Wildlife explicitly
    identifies equestrian campsites on official camping pages.
    """
    parks = [
        {
            "id": "co-stateparks-golden-gate-canyon",
            "name": "Golden Gate Canyon State Park Equestrian Campsites",
            "location": "Golden, CO",
            "state": "CO",
            "latitude": 39.8351,
            "longitude": -105.4047,
            "pricePerNight": 41.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "303-582-3707",
            "website": "https://cpw.state.co.us/state-parks/golden-gate-canyon-state-park/golden-gate-canyon-state-park-camping-lodging",
            "description": "Colorado Parks & Wildlife says campsites 15 and 16 at Golden Gate Canyon State Park are equestrian campsites and can accommodate horses.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "CO State Parks",
        },
        {
            "id": "co-stateparks-mueller",
            "name": "Mueller State Park Equestrian Campsites",
            "location": "Divide, CO",
            "state": "CO",
            "latitude": 38.8766,
            "longitude": -105.1681,
            "pricePerNight": 36.0,
            "horseFeePerNight": 10.0,
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "719-687-2366",
            "website": "https://cpw.state.co.us/state-parks/mueller-state-park/mueller-state-park-camping-lodging",
            "description": "Colorado Parks & Wildlife says campsites 133 and 134 at Mueller State Park are equestrian sites and gives an equestrian campsite fee of $36 plus $10 per animal, per night.",
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
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "CO State Parks",
        },
    ]
    print(f"  Colorado State Parks: {len(parks)} official equestrian-camping listings")
    return parks








def fetch_ct_state_parks():
    """Fetch conservative Connecticut state-park horse-camping locations.

    Connecticut has limited dedicated overnight horse-camping inventory, so this
    first pass stays strict and only includes official CT DEEP forests where
    horseback riding and overnight camping are both clearly supported.
    """
    parks = [
        {
            "id": "ct-stateparks-pachaug-green-falls",
            "name": "Pachaug State Forest Green Falls Horse Camp",
            "location": "Voluntown, CT",
            "state": "CT",
            "latitude": 41.5908,
            "longitude": -71.8396,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "860-424-3200",
            "website": "https://portal.ct.gov/deep/state-parks/forests/pachaug-state-forest",
            "description": "Official CT DEEP Pachaug State Forest page lists Green Falls Campground for camping and the forest is a major horseback-riding destination with designated horse trails and camp access to ride-in areas.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "CT State Parks",
        },
        {
            "id": "ct-stateparks-natchaug-horse-camp",
            "name": "Natchaug State Forest Horse Camp Area",
            "location": "Eastford, CT",
            "state": "CT",
            "latitude": 41.8945,
            "longitude": -72.1246,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "860-424-3200",
            "website": "https://portal.ct.gov/deep/state-parks/forests/natchaug-state-forest",
            "description": "Conservative Connecticut first pass. Official CT DEEP Natchaug State Forest information supports horseback riding and dispersed/primitive camping use in the forest, making it a reasonable horse-camping entry to verify before arrival.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "CT State Parks",
        },
    ]
    print(f"  Connecticut State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


# ── MAIN ───────────────────────────────────────────────────────────────


def fetch_id_state_parks():
    """Fetch official Idaho state-park equestrian camping locations.

    Idaho Parks and Recreation's official horseback-riding guidance and park pages
    support a conservative fixed allowlist for parks with clear overnight equestrian
    camping details. This first pass includes Farragut, Bruneau Dunes, and Heyburn.
    Uses fixed coordinates to avoid geocoding/rate-limit issues.
    """
    parks = [
        {
            "id": "id-stateparks-farragut",
            "name": "Farragut State Park Equestrian Campsites",
            "location": "Athol, ID",
            "state": "ID",
            "latitude": 47.95139,
            "longitude": -116.60222,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water", "30A"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(208) 262-4444",
            "website": "https://parksandrecreation.idaho.gov/state-park/farragut-state-park/",
            "description": "Official Idaho State Parks equestrian camping at Farragut State Park. The park page lists 6 equestrian sites and equestrian campsites among amenities.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "ID State Parks",
        },
        {
            "id": "id-stateparks-bruneau-dunes",
            "name": "Bruneau Dunes State Park Equestrian Campground",
            "location": "Mountain Home, ID",
            "state": "ID",
            "latitude": 42.9100,
            "longitude": -115.70972,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(208) 366-7919",
            "website": "https://parksandrecreation.idaho.gov/state-park/bruneau-dunes-state-park/",
            "description": "Official Idaho State Parks equestrian campground at Bruneau Dunes State Park. The park page says the equestrian area has corrals, water spigots, a vault toilet, a shelter, and 19 non-reservable campsites.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "ID State Parks",
        },
        {
            "id": "id-stateparks-heyburn",
            "name": "Heyburn State Park Equestrian Trailhead Campsites",
            "location": "Plummer, ID",
            "state": "ID",
            "latitude": 47.35333,
            "longitude": -116.77194,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Paddocks"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(208) 686-1308",
            "website": "https://parksandrecreation.idaho.gov/state-park/heyburn-state-park/",
            "description": "Official Idaho State Parks equestrian camping at Heyburn State Park. The park page says campsites are available at the South Side and North Side trailheads with paddocks for livestock.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "ID State Parks",
        },
    ]
    print(f"  Idaho State Parks: {len(parks)} official equestrian-camping listings")
    return parks


def fetch_wa_state_parks():
    """Fetch official Washington State Parks equestrian camping locations.

    Uses a strict fixed allowlist from official Washington State Parks pages
    that explicitly mention primitive equestrian sites, equestrian campsites,
    or an equestrian campground with overnight camping.
    """
    parks = [
        {
            "id": "wa-stateparks-battle-ground-lake",
            "name": "Battle Ground Lake State Park Equestrian Camp",
            "location": "Battle Ground, WA",
            "state": "WA",
            "latitude": 45.7786,
            "longitude": -122.4660,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 35,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(360) 687-4621",
            "website": "https://parks.wa.gov/find-parks/state-parks/battle-ground-lake-state-park",
            "description": "Official Washington State Parks page says Battle Ground Lake offers primitive equestrian sites along with camping and horseback riding trails.",
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
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WA State Parks",
        },
        {
            "id": "wa-stateparks-riverside",
            "name": "Riverside State Park Equestrian Campground",
            "location": "Nine Mile Falls, WA",
            "state": "WA",
            "latitude": 47.7640,
            "longitude": -117.5604,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(509) 465-5064",
            "website": "https://parks.wa.gov/find-parks/state-parks/riverside-state-park",
            "description": "Official Washington State Parks page says Riverside's Equestrian Area has standard and partial-hookup campsites, each campsite has a corral, and the equestrian campground is open seasonally.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WA State Parks",
        },
        {
            "id": "wa-stateparks-steamboat-rock",
            "name": "Steamboat Rock State Park Equestrian Campground",
            "location": "Electric City, WA",
            "state": "WA",
            "latitude": 47.9057,
            "longitude": -119.0253,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 50,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(509) 633-1304",
            "website": "https://parks.wa.gov/find-parks/state-parks/steamboat-rock-state-park",
            "description": "Official Washington State Parks page says an equestrian site at Northrup Canyon requires a reservation and the park offers camping and miles of trails for horses and hikers.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WA State Parks",
        },
        {
            "id": "wa-stateparks-lewis-clark",
            "name": "Lewis and Clark State Park Equestrian Campsites",
            "location": "Winlock, WA",
            "state": "WA",
            "latitude": 46.5043,
            "longitude": -122.7953,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(360) 864-2643",
            "website": "https://parks.wa.gov/find-parks/state-parks/lewis-clark-state-park",
            "description": "Official Washington State Parks page says Lewis and Clark offers five primitive equestrian campsites on a first-come, first-served basis.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 9,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WA State Parks",
        },
        {
            "id": "wa-stateparks-beacon-rock",
            "name": "Beacon Rock State Park Equestrian Camp Area",
            "location": "North Bonneville, WA",
            "state": "WA",
            "latitude": 45.6311,
            "longitude": -122.0221,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 40,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(509) 427-8265",
            "website": "https://parks.wa.gov/474/Beacon-Rock",
            "description": "Official Washington State Parks page says the Equestrian Camp Area at Beacon Rock offers two primitive sites available to campers with horses.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WA State Parks",
        },
        {
            "id": "wa-stateparks-rainbow-falls",
            "name": "Rainbow Falls State Park Equestrian Sites",
            "location": "Chehalis, WA",
            "state": "WA",
            "latitude": 46.6287,
            "longitude": -123.2309,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 60,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "(360) 291-3767",
            "website": "https://parks.wa.gov/find-parks/state-parks/rainbow-falls-state-park",
            "description": "Official Washington State Parks page says Rainbow Falls has two equestrian sites and is popular with horseback riders tackling the Willapa Hills Trail.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 11,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WA State Parks",
        },
    ]
    print(f"  Washington State Parks: {len(parks)} official equestrian-camping listings")
    return parks


def fetch_nm_state_parks():
    """Fetch a conservative first-pass set of New Mexico State Parks horse-camping locations.

    Based on official New Mexico State Parks horse/facilities pages and park pages,
    this pass includes only parks with clear overnight equestrian-camping language.
    Uses fixed coordinates to avoid geocoding/rate-limit issues.
    """
    parks = [
        {
            "id": "nm-stateparks-bluewater-lake",
            "name": "Bluewater Lake State Park Horse Camping Area",
            "location": "Prewitt, NM",
            "state": "NM",
            "latitude": 35.2728,
            "longitude": -108.1192,
            "phone": "505-876-2391",
            "website": "https://www.emnrd.nm.gov/spd/horsebackriding/",
            "description": "Official New Mexico State Parks horse-facilities page says horseback riding and camping are allowed in Section 4 of the NW quadrant on the Prewitt side of Bluewater Lake.",
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "hasBathhouse": False,
        },
        {
            "id": "nm-stateparks-caballo-lake",
            "name": "Caballo Lake State Park Horse Corrals Camping Area",
            "location": "Caballo, NM",
            "state": "NM",
            "latitude": 32.9037,
            "longitude": -107.2925,
            "phone": "575-743-3942",
            "website": "https://www.emnrd.nm.gov/spd/horsebackriding/",
            "description": "Official New Mexico State Parks horse-facilities page says horses are allowed at the horse corrals with camping allowed near the horse corrals at Caballo Lake State Park.",
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "hasBathhouse": False,
        },
        {
            "id": "nm-stateparks-el-vado-lake",
            "name": "El Vado Lake State Park Equestrian Camping Area",
            "location": "Tierra Amarilla, NM",
            "state": "NM",
            "latitude": 36.5936,
            "longitude": -106.7076,
            "phone": "",
            "website": "https://www.emnrd.nm.gov/spd/find-a-park/el-vado-lake-state-park/",
            "description": "Official New Mexico State Parks horse-facilities page says overnight stays are allowed in designated areas at El Vado Lake State Park, and the official park page includes both Equestrian and Camping as park features.",
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "hasBathhouse": False,
        },
        {
            "id": "nm-stateparks-fenton-lake",
            "name": "Fenton Lake State Park Equestrian Camping Area",
            "location": "Jemez Springs, NM",
            "state": "NM",
            "latitude": 35.8799,
            "longitude": -106.7194,
            "phone": "575-829-3630",
            "website": "https://www.emnrd.nm.gov/spd/fentonlakestatepark.html",
            "description": "Official Fenton Lake State Park page lists Equestrian with both Trails and Camping.",
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "hasBathhouse": False,
        },
        {
            "id": "nm-stateparks-oasis",
            "name": "Oasis State Park Horse Camping Sites",
            "location": "Portales, NM",
            "state": "NM",
            "latitude": 34.1707,
            "longitude": -103.3346,
            "phone": "575-356-5331",
            "website": "https://www.emnrd.nm.gov/spd/horsebackriding/",
            "description": "Official New Mexico State Parks horse-facilities page says overnight stays are allowed in designated areas at Oasis State Park, with horse trailer parking and water hydrants at designated campsites.",
            "hookups": ["Water"],
            "accommodations": ["Trails", "Horse Trailer Parking"],
            "hasBathhouse": False,
        },
        {
            "id": "nm-stateparks-sugarite-canyon",
            "name": "Sugarite Canyon State Park Soda Pocket Equestrian Campground",
            "location": "Raton, NM",
            "state": "NM",
            "latitude": 36.9846,
            "longitude": -104.4105,
            "phone": "575-445-5607",
            "website": "https://www.emnrd.nm.gov/spd/horsebackriding/",
            "description": "Official New Mexico State Parks horse-facilities page says horse camping at Sugarite Canyon is allowed only at the Soda Pocket campground, where four corrals are available.",
            "hookups": [],
            "accommodations": ["Corrals", "Trails"],
            "paddockCount": 4,
            "hasBathhouse": False,
        },
        {
            "id": "nm-stateparks-villanueva",
            "name": "Villanueva State Park Horse Camping Area",
            "location": "Villanueva, NM",
            "state": "NM",
            "latitude": 35.2624,
            "longitude": -105.3575,
            "phone": "575-421-2957",
            "website": "https://www.emnrd.nm.gov/spd/horsebackriding/",
            "description": "Official New Mexico State Parks horse-facilities page says camping with horses is allowed at the primitive camping area at site 3A in Villanueva State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "hasBathhouse": False,
        },
    ]

    camps = []
    for p in parks:
        camps.append({
            "id": p["id"],
            "name": p["name"],
            "location": p["location"],
            "state": p["state"],
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p.get("hookups", []),
            "accommodations": p.get("accommodations", ["Trails"]),
            "maxRigLength": 0,
            "stallCount": p.get("stallCount", 0),
            "paddockCount": p.get("paddockCount", 0),
            "phone": p.get("phone", ""),
            "website": p["website"],
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": p.get("hasBathhouse", False),
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NM State Parks",
        })
    print(f"  New Mexico State Parks: {len(camps)} official equestrian-camping listings")
    return camps


def fetch_ut_state_parks():
    """Utah State Parks entries with official equestrian camping evidence.
    Conservative fixed allowlist with fixed coordinates; no live geocoding.
    Official evidence includes:
    - Antelope Island State Park: official camping page says two equestrian sites are available.
    - Goblin Valley State Park: official primitive camping page says Site 5 is great for equestrian camping and Crack Canyon is designated for equestrian camping.
    """
    parks = [
        {
            "id": "ut-stateparks-antelope-island-equestrian",
            "name": "Antelope Island State Park Equestrian Sites",
            "location": "Syracuse, UT",
            "state": "UT",
            "latitude": 41.0582,
            "longitude": -112.2216,
            "pricePerNight": 40.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "1-800-322-3770",
            "website": "https://stateparks.utah.gov/parks/antelope-island/camping-opportunities/",
            "description": "Official Utah State Parks camping page says Antelope Island has two equestrian sites available in White Rock Bay. No water or electricity at the sites.",
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
            "imageColors": ["C0392B", "E57373"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "UT State Parks",
        },
        {
            "id": "ut-stateparks-goblin-valley-equestrian",
            "name": "Goblin Valley State Park Equestrian Camping",
            "location": "Green River, UT",
            "state": "UT",
            "latitude": 38.5744,
            "longitude": -110.7073,
            "pricePerNight": 15.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "435-259-3710",
            "website": "https://stateparks.utah.gov/parks/goblin-valley/dispersed-camping/",
            "description": "Official Utah State Parks primitive camping page says Site 5 at Behind the Butte East is great for equestrian camping and Crack Canyon Access is designated for equestrian camping.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E57373"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "UT State Parks",
        },
    ]
    print(f"  Utah State Parks: {len(parks)} official equestrian-camping listings")
    return parks



def fetch_sc_state_parks():
    """Fetch South Carolina State Parks equestrian camping from an official allowlist."""
    parks = [
        {"id": "sc-stateparks-croft", "name": "Croft State Park Equestrian Campground", "location": "Spartanburg, SC", "state": "SC", "latitude": 34.9492, "longitude": -81.9136, "website": "https://southcarolinaparks.com/camping-and-lodging/camping/equestrian", "phone": "864-585-1283", "description": "Official South Carolina State Parks equestrian camping location at Croft State Park.", "hookups": [], "accommodations": ["Trails", "Corrals"], "hasBathhouse": True, "seasonStart": 1, "seasonEnd": 12},
        {"id": "sc-stateparks-h-cooper-black", "name": "H. Cooper Black Jr. Memorial Field Trial Area Equestrian Campground", "location": "Cheraw, SC", "state": "SC", "latitude": 34.6815, "longitude": -80.0186, "website": "https://southcarolinaparks.com/camping-and-lodging/camping/equestrian", "phone": "843-378-1555", "description": "Official South Carolina State Parks equestrian camping location at H. Cooper Black Jr. Memorial Field Trial Area.", "hookups": ["Water"], "accommodations": ["Trails", "Corrals", "Stalls"], "hasBathhouse": True, "seasonStart": 1, "seasonEnd": 12},
        {"id": "sc-stateparks-kings-mountain", "name": "Kings Mountain State Park Equestrian Campground", "location": "Blacksburg, SC", "state": "SC", "latitude": 35.1390, "longitude": -81.3902, "website": "https://southcarolinaparks.com/camping-and-lodging/camping/equestrian", "phone": "864-936-7921", "description": "Official South Carolina State Parks equestrian camping location at Kings Mountain State Park.", "hookups": ["Water"], "accommodations": ["Trails", "Corrals"], "hasBathhouse": True, "seasonStart": 1, "seasonEnd": 12},
        {"id": "sc-stateparks-lee", "name": "Lee State Park Equestrian Campground", "location": "Bishopville, SC", "state": "SC", "latitude": 34.1703, "longitude": -80.2484, "website": "https://southcarolinaparks.com/camping-and-lodging/camping/equestrian", "phone": "803-428-5307", "description": "Official South Carolina State Parks equestrian camping location at Lee State Park.", "hookups": ["Water"], "accommodations": ["Trails", "Corrals"], "hasBathhouse": True, "seasonStart": 1, "seasonEnd": 12},
    ]
    camps = []
    for p in parks:
        camps.append({
            "id": p["id"], "name": p["name"], "location": p["location"], "state": p["state"],
            "latitude": p["latitude"], "longitude": p["longitude"], "pricePerNight": 0.0, "horseFeePerNight": 0.0,
            "hookups": p["hookups"], "accommodations": p["accommodations"], "maxRigLength": 0, "stallCount": 0,
            "paddockCount": 0, "phone": p["phone"], "website": p["website"], "description": p["description"],
            "isVerified": False, "seasonStart": p["seasonStart"], "seasonEnd": p["seasonEnd"], "hasWashRack": False,
            "hasDumpStation": False, "hasWifi": False, "hasBathhouse": p["hasBathhouse"], "pullThroughAvailable": False,
            "rating": 0.0, "reviewCount": 0, "imageColors": ["C0392B", "F1948A"], "photoURLs": [],
            "source": "State Parks", "sourceDetail": "SC State Parks",
        })
    print(f"  South Carolina State Parks: {len(camps)} official equestrian-camping listings")
    return camps


def fetch_al_state_parks():
    """Fetch Alabama State Parks equestrian camping from a conservative allowlist."""
    parks = [
        {"id": "al-stateparks-oak-mountain", "name": "Oak Mountain State Park Equestrian Campground", "location": "Pelham, AL", "state": "AL", "latitude": 33.3301, "longitude": -86.7562, "website": "https://www.alapark.com/parks/oak-mountain-state-park", "phone": "205-620-2520", "description": "Official Alabama State Parks equestrian camping location at Oak Mountain State Park.", "hookups": ["Water", "30A"], "accommodations": ["Trails", "Corrals", "Stalls"], "hasBathhouse": True, "seasonStart": 1, "seasonEnd": 12},
        {"id": "al-stateparks-cheaha", "name": "Cheaha State Park Equestrian Camp Area", "location": "Delta, AL", "state": "AL", "latitude": 33.4334, "longitude": -85.8083, "website": "https://www.alapark.com/parks/cheaha-state-park", "phone": "256-488-5111", "description": "Official Alabama State Parks equestrian camping location at Cheaha State Park.", "hookups": [], "accommodations": ["Trails", "Corrals"], "hasBathhouse": True, "seasonStart": 1, "seasonEnd": 12},
    ]
    camps = []
    for p in parks:
        camps.append({
            "id": p["id"], "name": p["name"], "location": p["location"], "state": p["state"],
            "latitude": p["latitude"], "longitude": p["longitude"], "pricePerNight": 0.0, "horseFeePerNight": 0.0,
            "hookups": p["hookups"], "accommodations": p["accommodations"], "maxRigLength": 0, "stallCount": 0,
            "paddockCount": 0, "phone": p["phone"], "website": p["website"], "description": p["description"],
            "isVerified": False, "seasonStart": p["seasonStart"], "seasonEnd": p["seasonEnd"], "hasWashRack": False,
            "hasDumpStation": False, "hasWifi": False, "hasBathhouse": p["hasBathhouse"], "pullThroughAvailable": False,
            "rating": 0.0, "reviewCount": 0, "imageColors": ["C0392B", "F1948A"], "photoURLs": [],
            "source": "State Parks", "sourceDetail": "AL State Parks",
        })
    print(f"  Alabama State Parks: {len(camps)} official equestrian-camping listings")
    return camps


def fetch_wy_state_parks():
    """Fetch conservative Wyoming state-park horse camping locations.

    Uses a strict fixed allowlist from official Wyoming State Parks pages with
    explicit horse corrals/facilities adjacent to overnight camping.
    This first pass stays conservative and includes only parks/sites with clear
    official horse-camping signals.
    """
    parks = [
        {
            "id": "wy-stateparks-curt-gowdy-aspen-grove",
            "name": "Curt Gowdy State Park Aspen Grove Horse Camp Area",
            "location": "Cheyenne, WY",
            "state": "WY",
            "latitude": 41.1738,
            "longitude": -105.2310,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals", "Pasture"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "307-632-7946",
            "website": "https://wyoparks.wyo.gov/index.php/places-to-go/curt-gowdy",
            "description": "Official Wyoming State Parks page says Aspen Grove Campground is next to a free public horse corral, and the horseback-riding page says a horse corral for public use and a large fenced pasture area are located near Aspen Grove Campground.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 9,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WY State Parks",
        },
        {
            "id": "wy-stateparks-keyhole-homestead-horse-sites",
            "name": "Keyhole State Park Homestead Horse Corral Sites",
            "location": "Moorcroft, WY",
            "state": "WY",
            "latitude": 44.3346,
            "longitude": -104.7800,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 3,
            "phone": "307-756-3596",
            "website": "https://wyoparks.wyo.gov/index.php/activities-amenities-keyhole/horse-facility-keyhole",
            "description": "Official Wyoming State Parks horse-facility page says Keyhole State Park has three reservable horse corral sites at Homestead sites 44, 45, and 46, with attached water containers and riding access just behind the corrals.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 9,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WY State Parks",
        },
        {
            "id": "wy-stateparks-medicine-lodge-group-campsites",
            "name": "Medicine Lodge Archaeological Site Horse Corrals and Group Campsites",
            "location": "Hyattville, WY",
            "state": "WY",
            "latitude": 44.2744,
            "longitude": -107.6097,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Corrals", "Group Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 5,
            "phone": "307-469-2234",
            "website": "https://wyoparks.wyo.gov/index.php/activities-amenities-medicine-lodge/horse-facility-medicine-lodge",
            "description": "Official Wyoming State Parks Medicine Lodge horse-facility page says public corrals are available next to the main parking area, and the fees page lists a group shelter with campsites, making this a conservative overnight horse-camping entry.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WY State Parks",
        },
    ]
    print(f"  Wyoming State Parks: {len(parks)} official equestrian-camping listings")
    return parks


def fetch_mt_state_parks():
    """Fetch a conservative first-pass set of Montana state-park horse camping locations.

    Montana FWP does not appear to publish a statewide equestrian-camping allowlist.
    This pass stays strict and only includes parks where official FWP pages clearly
    indicate both camping and horseback riding at the same park.
    """
    parks = [
        {
            "id": "mt-stateparks-fish-creek",
            "name": "Fish Creek State Park Horse Camping Area",
            "location": "Alberton, MT",
            "state": "MT",
            "latitude": 47.0009,
            "longitude": -114.8368,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "406-542-5500",
            "website": "https://fwp.mt.gov/stateparks/fish-creek",
            "description": "Official Montana FWP page says Fish Creek State Park is open year round and includes both camping and horseback riding among park activities.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "MT State Parks",
        },
    ]
    print(f"  Montana State Parks: {len(parks)} official equestrian-camping listings")
    return parks


def fetch_de_state_parks():
    """Fetch a conservative first-pass set of Delaware State Parks equestrian camping.

    Delaware appears to have limited dedicated overnight horse-camping inventory.
    This pass stays conservative and only includes parks where the official park
    pages clearly surface both horseback/equestrian use and overnight camping.
    """
    parks = [
        {
            "id": "de-stateparks-lums-pond",
            "name": "Lums Pond State Park Equestrian Camping Area",
            "location": "Bear, DE",
            "state": "DE",
            "latitude": 39.5794,
            "longitude": -75.6948,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "302-368-6989",
            "website": "https://www.destateparks.com/park/lums-pond/",
            "description": "Conservative Delaware State Parks first pass. Official Lums Pond State Park information surfaces both equestrian trails and camping at the park, so it is included as a likely horse-friendly overnight option to verify before arrival.",
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
            "sourceDetail": "DE State Parks",
        },
        {
            "id": "de-stateparks-trap-pond",
            "name": "Trap Pond State Park Equestrian Camping Area",
            "location": "Laurel, DE",
            "state": "DE",
            "latitude": 38.5202,
            "longitude": -75.4713,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "302-875-5153",
            "website": "https://www.destateparks.com/park/trap-pond/",
            "description": "Conservative Delaware State Parks first pass. Official Trap Pond State Park information surfaces equestrian use and family camping at the park, so it is included as a likely horse-friendly overnight option to verify before arrival.",
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
            "sourceDetail": "DE State Parks",
        },
    ]
    print(f"  Delaware State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_ms_state_parks():
    """Fetch a conservative first-pass set of Mississippi State Parks equestrian camping.

    This pass stays conservative and only includes Mississippi state parks where
    official MDWFP pages clearly surface both equestrian use and overnight
    camping/accommodations.
    """
    parks = [
        {
            "id": "ms-stateparks-trace",
            "name": "Trace State Park Equestrian Camping Area",
            "location": "Belden, MS",
            "state": "MS",
            "latitude": 34.2418,
            "longitude": -88.9264,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "50A", "Water", "Sewer"],
            "accommodations": ["Trails", "Stalls", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "662-489-2958",
            "website": "https://www.mdwfp.com/parks-destinations/park/trace-state-park",
            "description": "Official Mississippi State Parks page says Trace State Park offers 35 miles of combined equestrian trail use and a horse barn for overnight accommodations; the park also offers developed RV camping.",
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
            "sourceDetail": "MS State Parks",
        },
        {
            "id": "ms-stateparks-lake-lowndes",
            "name": "Lake Lowndes State Park Equestrian Camping Area",
            "location": "Columbus, MS",
            "state": "MS",
            "latitude": 33.4343,
            "longitude": -88.3881,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "50A", "Water", "Sewer"],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "662-328-2110",
            "website": "https://www.mdwfp.com/parks-destinations/park/lake-lowndes-state-park",
            "description": "Conservative Mississippi State Parks first pass. Official MDWFP pages surface a 7-mile equestrian trail at Lake Lowndes State Park and also list RV and tent camping at the park, so it is included as a likely horse-friendly overnight option to verify before arrival.",
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
            "sourceDetail": "MS State Parks",
        },
    ]
    print(f"  Mississippi State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks



def fetch_ak_state_parks():
    """Fetch conservative Alaska State Parks horse-friendly camping locations.

    Alaska has limited centralized equestrian-camping data, so this first pass
    stays strict and only includes official Alaska State Parks units whose pages
    clearly list both camping and horseback riding among supported activities.
    """
    parks = [
        {
            "id": "ak-stateparks-eklutna-lake",
            "name": "Eklutna Lake Campground Horse Access",
            "location": "Chugiak, AK",
            "state": "AK",
            "latitude": 61.4148,
            "longitude": -149.1457,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "907-345-5014",
            "website": "https://dnr.alaska.gov/parks/aspunits/chugach/eklutnalkcamp.htm",
            "description": "Official Alaska State Parks Eklutna Lake Campground page lists both camping and horseback riding among park activities.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 9,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "AK State Parks",
        },
        {
            "id": "ak-stateparks-matanuska-lakes",
            "name": "Matanuska Lakes State Recreation Area Horse Access Camping",
            "location": "Palmer, AK",
            "state": "AK",
            "latitude": 61.5998,
            "longitude": -149.2586,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "907-745-3975",
            "website": "https://dnr.alaska.gov/parks/aspunits/matsu/keplerbradlksra.htm",
            "description": "Official Alaska State Parks Matanuska Lakes State Recreation Area page says camping and horseback riding are dominant activities in the park.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 9,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "AK State Parks",
        },
    ]
    print(f"  Alaska State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_ia_state_parks():
    """Fetch official Iowa DNR equestrian camping locations.

    Iowa DNR's overnight-camping guidance explicitly lists designated equestrian
    campgrounds. This first pass uses a conservative fixed allowlist built from
    the statewide Iowa DNR list plus park pages with clear equestrian-camping
    amenities.
    """
    parks = [
        {
            "id": "ia-stateparks-brushy-creek",
            "name": "Brushy Creek State Recreation Area Equestrian Campgrounds",
            "location": "Lehigh, IA",
            "state": "IA",
            "latitude": 42.3919,
            "longitude": -93.9847,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "515-543-8298",
            "website": "https://www.iowadnr.gov/places-go/state-parks/all-parks/brushy-creek-state-recreation-area",
            "description": "Official Iowa DNR page says Brushy Creek has north and south equestrian campgrounds with electric and non-electric sites, a horse-wash area, hitch rails, and modern shower/restroom facilities.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": True,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "IA State Parks",
        },
        {
            "id": "ia-stateparks-elk-rock",
            "name": "Elk Rock State Park Equestrian Campground",
            "location": "Knoxville, IA",
            "state": "IA",
            "latitude": 41.3783,
            "longitude": -93.2629,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Stalls", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "641-842-6008",
            "website": "https://www.iowadnr.gov/places-go/state-parks/all-parks/elk-rock-state-park",
            "description": "Official Iowa DNR page says Elk Rock's equestrian campground has electric and non-electric sites, a shower building, horse stalls, hitching rails, and a riding arena.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
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
            "sourceDetail": "IA State Parks",
        },
        {
            "id": "ia-stateparks-lake-of-three-fires",
            "name": "Lake of Three Fires State Park Equestrian Campground",
            "location": "Bedford, IA",
            "state": "IA",
            "latitude": 40.7116,
            "longitude": -94.6902,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Corrals", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "712-523-2700",
            "website": "https://www.iowadnr.gov/places-go/state-parks/all-parks/lake-three-fires-state-park",
            "description": "Official Iowa DNR page says the equestrian campground has electric and non-electric sites, restrooms, corrals, hitching posts, and holding pens.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
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
            "sourceDetail": "IA State Parks",
        },
        {
            "id": "ia-stateparks-waubonsie",
            "name": "Waubonsie State Park Equestrian Campground",
            "location": "Hamburg, IA",
            "state": "IA",
            "latitude": 40.6614,
            "longitude": -95.6869,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "712-382-2786",
            "website": "https://www.iowadnr.gov/places-go/state-parks/all-parks/waubonsie-state-park",
            "description": "Official Iowa DNR page says Waubonsie's primitive equestrian campground contains non-electric sites with hitching rails, pens, and pit vault toilets.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "IA State Parks",
        },
        {
            "id": "ia-stateparks-nine-eagles",
            "name": "Nine Eagles State Park Equestrian Campsites",
            "location": "Davis City, IA",
            "state": "IA",
            "latitude": 40.5984,
            "longitude": -93.7701,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "641-442-2855",
            "website": "https://www.iowadnr.gov/places-go/state-parks/all-parks/nine-eagles-state-park",
            "description": "Official Iowa DNR page says primitive equestrian campsites contain fire rings, pit toilets, water, and hitching rails.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "IA State Parks",
        },
        {
            "id": "ia-stateparks-volga-river",
            "name": "Volga River State Recreation Area Albany Equestrian Campground",
            "location": "Fayette, IA",
            "state": "IA",
            "latitude": 42.8989,
            "longitude": -91.7711,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "563-425-4161",
            "website": "https://www.iowadnr.gov/places-go/state-parks/all-parks/volga-river-state-recreation-area",
            "description": "Official Iowa DNR page says the Albany Campground has equestrian sites with electricity.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "IA State Parks",
        },
        {
            "id": "ia-stateparks-shimek",
            "name": "Shimek State Forest Lick Creek Equestrian Campgrounds",
            "location": "Farmington, IA",
            "state": "IA",
            "latitude": 40.6425,
            "longitude": -91.7118,
            "pricePerNight": 0.0,
            "horseFeePerNight": 4.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Stalls", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "319-878-3811",
            "website": "https://www.iowadnr.gov/places-go/state-forests/shimek-state-forest",
            "description": "Official Iowa DNR page says the Lower and Upper Campgrounds in the Lick Creek Unit are designed for equestrian use, with open-air stalls for overnight stabling, hitching posts, and a water hydrant.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "IA State Parks",
        },
    ]
    print(f"  Iowa State Parks: {len(parks)} official equestrian-camping listings")
    return parks


def fetch_hi_state_parks():
    """Fetch a conservative first-pass set of Hawaiʻi state-park horse camping locations.

    Hawaiʻi has limited official overnight equestrian inventory, so this pass stays
    conservative and only includes areas where official DLNR park/recreation pages
    clearly surface both camping and horseback riding access.
    """
    parks = [
        {
            "id": "hi-stateparks-kokee-nualolo",
            "name": "Kōkeʻe State Park Nualolo Horse Camping Access",
            "location": "Waimea, HI",
            "state": "HI",
            "latitude": 22.1412,
            "longitude": -159.6606,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "808-274-3444",
            "website": "https://dlnr.hawaii.gov/dsp/parks/kauai/kokee-state-park/",
            "description": "Conservative Hawaiʻi DLNR first pass. Kōkeʻe State Park is an official camping area with trail access into a region used for horseback riding; verify current equestrian rules before arrival.",
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
            "sourceDetail": "HI State Parks",
        },
        {
            "id": "hi-stateparks-polipoli-spring",
            "name": "Polipoli Spring State Recreation Area Horse Camping Access",
            "location": "Kula, HI",
            "state": "HI",
            "latitude": 20.6550,
            "longitude": -156.3070,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "808-984-8109",
            "website": "https://dlnr.hawaii.gov/dsp/parks/maui/polipoli-spring-state-recreation-area/",
            "description": "Conservative Hawaiʻi DLNR first pass. Polipoli Spring State Recreation Area is an official camping area in a region with horseback riding access; verify current equestrian rules before arrival.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "HI State Parks",
        },
    ]
    print(f"  Hawaii State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_nj_state_parks():
    """Fetch a conservative first-pass set of New Jersey State Parks horse-camping locations.

    New Jersey does not appear to publish a clean statewide equestrian-camping allowlist.
    This pass stays conservative and only includes parks/forests where official NJDEP pages
    clearly support both overnight camping and horseback riding in the same park/forest.
    """
    parks = [
        {
            "id": "nj-stateparks-wharton-atsion-horse-camp",
            "name": "Wharton State Forest Atsion Horse Camping Access",
            "location": "Shamong, NJ",
            "state": "NJ",
            "latitude": 39.7427,
            "longitude": -74.7269,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "609-561-0024",
            "website": "https://dep.nj.gov/parksandforests/state-park/wharton-state-forest/",
            "description": "Conservative New Jersey first pass. Official NJDEP pages for Wharton State Forest and Atsion Recreation Area surface both camping and horseback riding in the same forest/recreation area, so this is included as a horse-friendly overnight option to verify before arrival.",
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
            "sourceDetail": "NJ State Parks",
        },
    ]
    print(f"  New Jersey State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_ri_state_parks():
    """Fetch a conservative first-pass set of Rhode Island State Parks equestrian camping.

    Rhode Island DEM appears to separate campground inventory from equestrian-use parks.
    This pass stays strict and currently returns no listings until an official RI state
    page clearly surfaces both overnight camping and horseback riding at the same park.
    """
    parks = []
    print(f"  Rhode Island State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_nh_state_parks():
    """Fetch a conservative first-pass set of New Hampshire State Parks equestrian camping.

    New Hampshire does not appear to publish a dedicated statewide horse-camping allowlist.
    This pass stays conservative and only includes parks where official NH State Parks pages
    clearly surface both camping and equestrian trail use within the same park.
    """
    parks = [
        {
            "id": "nh-stateparks-bear-brook",
            "name": "Bear Brook State Park Horse Camping Access",
            "location": "Allenstown, NH",
            "state": "NH",
            "latitude": 43.1440,
            "longitude": -71.3570,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "603-485-9869",
            "website": "https://www.nhstateparks.org/find-parks-trails/bear-brook-state-park",
            "description": "Conservative New Hampshire first pass. Official NH State Parks information for Bear Brook surfaces campground lodging and over 40 miles of trails for hikers, mountain bikers and equestrians in the same park, so it is included as a horse-friendly overnight option to verify before arrival.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
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
            "sourceDetail": "NH State Parks",
        },
    ]
    print(f"  New Hampshire State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_me_state_parks():
    """Fetch a conservative first-pass set of Maine State Parks equestrian camping.

    Maine does not appear to publish a statewide horse-camping allowlist. This pass stays
    conservative and only includes parks where official Maine DACF materials clearly show
    both overnight camping and horseback riding or trail riding at the same park.
    """
    parks = [
        {
            "id": "me-stateparks-mount-blue",
            "name": "Mount Blue State Park Horse Camping Access",
            "location": "Weld, ME",
            "state": "ME",
            "latitude": 44.681737,
            "longitude": -70.449439,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "207-585-2347",
            "website": "https://www.maine.gov/mountblue",
            "description": "Conservative Maine first pass. Official Maine DACF guide material says Mount Blue has a 136-site campground at Webb Beach and 18 miles of multi-use trails for mountain bikers and equestrians, so it is included as a horse-friendly overnight option to verify before arrival.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "ME State Parks",
        },
    ]
    print(f"  Maine State Parks: {len(parks)} conservative equestrian-camping listings")
    return parks


def fetch_ma_state_parks():
    """Fetch official Massachusetts State Parks equestrian camping locations.

    Massachusetts DCR directly identifies horse camping at Charge Pond within Myles Standish
    State Forest, so this pass uses a small fixed allowlist built from the official park page,
    trail map, and campground metadata.
    """
    parks = [
        {
            "id": "ma-stateparks-myles-standish-charge-pond",
            "name": "Myles Standish State Forest Charge Pond Horse Camp",
            "location": "Carver, MA",
            "state": "MA",
            "latitude": 41.8176,
            "longitude": -70.6756,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "508-866-2526",
            "website": "https://www.mass.gov/locations/myles-standish-state-forest",
            "description": "Official Massachusetts DCR information says Charge Pond at Myles Standish State Forest has an area set aside for horse camping, and the forest also offers extensive equestrian trails.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
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
            "sourceDetail": "MA State Parks",
        },
    ]
    print(f"  Massachusetts State Parks: {len(parks)} official equestrian-camping listings")
    return parks


def fetch_nd_state_parks():
    """Fetch official North Dakota horse-park camping locations.

    North Dakota Parks and Recreation publishes both a statewide horseback-riding overview
    and detailed park pages for designated horse parks. This pass uses a conservative fixed
    allowlist built from those official horse-park pages.
    """
    parks = [
        {
            "id": "nd-stateparks-fort-ransom-horse-camp",
            "name": "Fort Ransom State Park Horse Campgrounds",
            "location": "Fort Ransom, ND",
            "state": "ND",
            "latitude": 46.54556,
            "longitude": -97.92972,
            "pricePerNight": 0.0,
            "horseFeePerNight": 6.0,
            "hookups": ["Electric", "Water"],
            "accommodations": ["Trails", "Corrals", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 68,
            "phone": "701-973-4331",
            "website": "https://www.parkrec.nd.gov/fort-ransom-state-park",
            "description": "Official North Dakota Parks and Recreation information says Fort Ransom State Park is a designated horse park with 24 modern horse campsites, 9 primitive horse campsites, 68 corrals, showers, a dump station, and extensive horseback-riding trails.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
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
            "sourceDetail": "ND State Parks",
        },
        {
            "id": "nd-stateparks-fort-abraham-lincoln-horse-camp",
            "name": "Fort Abraham Lincoln State Park Horse Campground",
            "location": "Mandan, ND",
            "state": "ND",
            "latitude": 46.7594,
            "longitude": -100.8443,
            "pricePerNight": 0.0,
            "horseFeePerNight": 6.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Corrals", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 8,
            "phone": "701-667-6340",
            "website": "https://www.parkrec.nd.gov/fort-abraham-lincoln-state-park",
            "description": "Official North Dakota Parks and Recreation information says Fort Abraham Lincoln State Park offers 4 primitive horse campsites, 8 corrals, showers, a sewage dump station, and nearly 20 miles of trails open to horseback riding.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
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
            "sourceDetail": "ND State Parks",
        },
        {
            "id": "nd-stateparks-rough-rider-horse-camp",
            "name": "Rough Rider State Park Horse Campgrounds",
            "location": "Medora, ND",
            "state": "ND",
            "latitude": 46.892634,
            "longitude": -103.538254,
            "pricePerNight": 0.0,
            "horseFeePerNight": 6.0,
            "hookups": ["Electric", "Water"],
            "accommodations": ["Trails", "Corrals", "Horse Camping", "Group Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 66,
            "phone": "701-623-2024",
            "website": "https://www.parkrec.nd.gov/rough-rider-state-park",
            "description": "Official North Dakota Parks and Recreation information says Rough Rider State Park is a designated horse park with standard and group horse campsites, 66 corrals, a round pen, a dump station, shower house, and direct access to the Maah Daah Hey Trail.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
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
            "sourceDetail": "ND State Parks",
        },
        {
            "id": "nd-stateparks-little-missouri-horse-camp",
            "name": "Little Missouri State Park Horse Campgrounds",
            "location": "Killdeer, ND",
            "state": "ND",
            "latitude": 47.5075,
            "longitude": -102.50083,
            "pricePerNight": 0.0,
            "horseFeePerNight": 6.0,
            "hookups": ["Electric"],
            "accommodations": ["Trails", "Corrals", "Horse Camping", "Group Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 81,
            "phone": "701-764-5256",
            "website": "https://www.parkrec.nd.gov/little-missouri-state-park-0",
            "description": "Official North Dakota Parks and Recreation information says Little Missouri State Park is a designated horse park with modern and primitive campsites, 81 corrals, a round pen, a pay shower house, RV dump station, and over 40 miles of trails open to horseback riding.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
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
            "sourceDetail": "ND State Parks",
        },
    ]
    print(f"  North Dakota State Parks: {len(parks)} official equestrian-camping listings")
    return parks

# ── OPENSTREETMAP ────────────────────────────────────────────
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
    """Fetch a conservative first-pass set of Nevada State Parks horse-camping locations.

    Uses a strict fixed allowlist from official Nevada State Parks pages with
    explicit overnight equestrian camping language.
    """
    parks = [
        {
            "id": "nv-stateparks-washoe-lake-equestrian",
            "name": "Washoe Lake State Park Equestrian Campground",
            "location": "Carson City, NV",
            "state": "NV",
            "latitude": 39.2167,
            "longitude": -119.7592,
            "pricePerNight": 15.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 45,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "775-687-4319",
            "website": "https://parks.nv.gov/parks/washoe-lake/",
            "description": "Official Nevada State Parks page says the main equestrian facility at Washoe Lake includes an arena, corrals, horse washing station, water and lighting, and that camping is permitted for both tents and RVs with large parking areas for horse trailer access.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": True,
            "hasDumpStation": True,
            "hasWifi": True,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NV State Parks",
        },
        {
            "id": "nv-stateparks-fort-churchill-scout-camp",
            "name": "Fort Churchill State Historic Park Scout Camp",
            "location": "Silver Springs, NV",
            "state": "NV",
            "latitude": 39.2796,
            "longitude": -119.2927,
            "pricePerNight": 15.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "775-577-2345",
            "website": "https://parks.nv.gov/parks/fort-churchill-state-historic-park/",
            "description": "Official Nevada State Parks page says Scout Camp at Fort Churchill is a dispersed camping or day-use area adjacent to the Carson River that includes multiple horse corrals and a designated manure dump area.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NV State Parks",
        },
    ]
    print(f"  Nevada State Parks: {len(parks)} official equestrian-camping listings")
    return parks

def fetch_ok_state_parks():
    """Fetch a conservative first-pass set of Oklahoma state-park horse camps.

    Uses official Oklahoma state park pages with explicit overnight equestrian-camping
    language. This first pass stays strict and only includes parks where the official
    page clearly describes an equestrian camp/campground for overnight use.
    """
    camps_data = [
        {
            "id": "ok-stateparks-foss-equestrian-camp",
            "name": "Foss State Park Equestrian Camp",
            "location": "Foss, OK",
            "state": "OK",
            "latitude": 35.5667,
            "longitude": -99.2198,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "50A", "Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "580-592-4433",
            "website": "https://www.travelok.com/state-parks/foss-state-park",
            "description": "Official Oklahoma State Parks page says Foss State Park offers an equestrian camp featuring a multi-purpose trail for horseback riding, hiking and mountain biking.",
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
            "sourceDetail": "OK State Parks",
        },
        {
            "id": "ok-stateparks-salt-plains-equestrian-camp",
            "name": "Salt Plains State Park Equestrian Campgrounds",
            "location": "Jet, OK",
            "state": "OK",
            "latitude": 36.7431,
            "longitude": -98.1323,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Corrals", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "580-626-4731",
            "website": "https://www.travelok.com/state-parks/salt-plains-state-park",
            "description": "Official Oklahoma State Parks page says Salt Plains State Park has equestrian campgrounds available, including Nathan Boone Equestrian Camp and more primitive sites at George Sibley Equestrian Area.",
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
            "sourceDetail": "OK State Parks",
        },
    ]
    print(f"  Oklahoma State Parks: {len(camps_data)} official equestrian-camping listings")
    return camps_data



def fetch_ks_state_parks():
    """Fetch Kansas State Parks equestrian-camping locations.

    Uses Kansas Department of Wildlife & Parks' official statewide equestrian
    campgrounds page as the allowlist, with fixed coordinates for stable nightly runs.
    This pass keeps Kansas conservative and focused on overnight horse-camping parks.
    """
    parks = [
        {
            "id": "ks-stateparks-eisenhower",
            "name": "Eisenhower State Park Equestrian Campground",
            "location": "Melvern, KS",
            "state": "KS",
            "latitude": 38.5140,
            "longitude": -95.9303,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 15,
            "phone": "785-528-4102",
            "website": "https://ksoutdoors.gov/state-parks/locations/eisenhower",
            "description": "Official Kansas State Park equestrian campground at Eisenhower State Park. Kansas says the park has equestrian campsites and the park page describes 15 equestrian campsites with electric/water and individual corrals.",
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
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-el-dorado",
            "name": "El Dorado State Park Equestrian Campground",
            "location": "El Dorado, KS",
            "state": "KS",
            "latitude": 37.8433,
            "longitude": -96.8088,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "316-321-7180",
            "website": "https://ksoutdoors.gov/State-Parks/Locations/El-Dorado",
            "description": "Official Kansas State Park equestrian campground at El Dorado State Park based on KDWP's statewide equestrian campgrounds page.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-hillsdale",
            "name": "Hillsdale State Park Saddle Ridge Equestrian Area",
            "location": "Paola, KS",
            "state": "KS",
            "latitude": 38.6550,
            "longitude": -94.9000,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "913-594-3600",
            "website": "https://ksoutdoors.gov/State-Parks/Locations/Hillsdale",
            "description": "Official Kansas State Park equestrian camping area at Hillsdale State Park based on KDWP's statewide equestrian campgrounds page and the Saddle Ridge equestrian area.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-historic-lake-scott",
            "name": "Historic Lake Scott State Park Equestrian Campground",
            "location": "Scott City, KS",
            "state": "KS",
            "latitude": 38.6833,
            "longitude": -100.9189,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "620-872-2061",
            "website": "https://ksoutdoors.gov/State-Parks/Locations/Historic-Lake-Scott",
            "description": "Official Kansas State Park equestrian campground at Historic Lake Scott State Park based on KDWP's statewide equestrian campgrounds page.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-milford",
            "name": "Milford State Park Eagle Ridge Equestrian Campground",
            "location": "Junction City, KS",
            "state": "KS",
            "latitude": 39.1008,
            "longitude": -96.9094,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["50A", "Water", "Sewer"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 5,
            "phone": "785-238-3014",
            "website": "https://ksoutdoors.gov/State-Parks/Locations/Milford/Areas/Eagle-Ridge-Campground",
            "description": "Official Kansas State Park equestrian campground at Milford State Park. Eagle Ridge is an equestrian campground with full-hookup sites and corrals near select sites.",
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
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-perry",
            "name": "Perry State Park Equestrian Camping Area",
            "location": "Perry, KS",
            "state": "KS",
            "latitude": 39.1506,
            "longitude": -95.4953,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "785-246-3449",
            "website": "https://ksoutdoors.gov/State-Parks/Locations/Perry",
            "description": "Official Kansas State Park equestrian camping area at Perry State Park based on KDWP's statewide equestrian campgrounds page and the equestrian trail area at Jefferson Point.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-sand-hills",
            "name": "Sand Hills State Park Equestrian Campground",
            "location": "Hutchinson, KS",
            "state": "KS",
            "latitude": 38.1083,
            "longitude": -97.8550,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "Water", "Sewer"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "316-542-3664",
            "website": "https://ksoutdoors.gov/State-Parks/Locations/Sand-Hills",
            "description": "Official Kansas State Park equestrian campground at Sand Hills State Park based on KDWP's statewide equestrian campgrounds page and the Sand Hills camping page.",
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
            "sourceDetail": "KS State Parks",
        },
        {
            "id": "ks-stateparks-tuttle-creek",
            "name": "Tuttle Creek State Park South Randolph Equestrian Campground",
            "location": "Manhattan, KS",
            "state": "KS",
            "latitude": 39.4308,
            "longitude": -96.7078,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 20,
            "phone": "785-539-7941",
            "website": "https://ksoutdoors.gov/state-parks/locations/tuttle-creek/areas/south-randolph",
            "description": "Official Kansas State Park equestrian campground at Tuttle Creek State Park. South Randolph has 20 electric sites, community water hydrants, horse pens, wash racks, dump station, shower building, and restroom.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": True,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "KS State Parks",
        },
    ]
    print(f"  Kansas State Parks: {len(parks)} official equestrian-camping listings")
    return parks





def fetch_md_state_parks():
    """Fetch conservative Maryland equestrian camping locations from official DNR sources.

    This first pass includes only Maryland public lands where official DNR pages
    clearly indicate both equestrian access and overnight camping with horses.
    """
    parks = [
        {
            "id": "md-stateparks-fair-hill-race-barn",
            "name": "Fair Hill NRMA Race Barn Equestrian Camping Facility",
            "location": "Elkton, MD",
            "state": "MD",
            "latitude": 39.7108,
            "longitude": -75.8656,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "410-398-1246",
            "website": "https://dnr.maryland.gov/publiclands/pages/central/fairhill/camping.aspx",
            "description": "Official Maryland DNR equestrian camping at Fair Hill Natural Resources Management Area. The camping page says equestrian camping facilities are available by reservation only at the Race Barn facility.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "MD State Parks",
        },
        {
            "id": "md-stateparks-savage-river-margraff",
            "name": "Savage River State Forest Margraff Plantation Horse Camp",
            "location": "Grantsville, MD",
            "state": "MD",
            "latitude": 39.6437,
            "longitude": -79.1484,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "301-895-5759",
            "website": "https://dnr.maryland.gov/forests/documents/srsf-horseback-riding-brochure.pdf",
            "description": "Official Maryland DNR Savage River State Forest horse brochure says visitors camping with horses should use the campsites located at the Margraff Plantation, making it a conservative overnight horse-camping entry.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "MD State Parks",
        },
    ]
    print(f"  Maryland State Parks: {len(parks)} official equestrian-camping listings")
    return parks

def fetch_vt_state_parks():
    """Fetch a conservative first-pass set of Vermont horse-camping locations.

    Vermont has limited state-managed horse-camping inventory. This pass stays
    conservative and only includes locations with a clear public horse-camping
    signal associated with state park / state forest recreation areas.
    """
    parks = [
        {
            "id": "vt-stateparks-new-discovery",
            "name": "New Discovery State Park Horse Camp",
            "location": "Groton, VT",
            "state": "VT",
            "latitude": 44.2893,
            "longitude": -72.2420,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 8,
            "phone": "802-584-3822",
            "website": "https://vtstateparks.com/newdiscovery.html",
            "description": "Conservative Vermont horse-camping entry for New Discovery State Park in Groton State Forest, a known equestrian camping area with horse campsites/corrals and trail access.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "VT State Parks",
        },
        {
            "id": "vt-stateparks-camp-plymouth",
            "name": "Camp Plymouth State Park Horse Camp",
            "location": "Plymouth, VT",
            "state": "VT",
            "latitude": 43.4797,
            "longitude": -72.7403,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Horse Camping", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "802-672-3612",
            "website": "https://vtstateparks.com/plymouth.html",
            "description": "Conservative Vermont horse-camping entry for Camp Plymouth State Park, a state park commonly described with group and horse camping facilities.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "VT State Parks",
        },
    ]
    print(f"  Vermont State Parks: {len(parks)} conservative horse-camping listings")
    return parks

def fetch_wv_state_parks():
    """Fetch official West Virginia State Parks equestrian camping locations.

    Conservative fixed allowlist using official West Virginia State Parks pages
    with explicit equestrian-camping language.
    """
    parks = [
        {
            "id": "wv-stateparks-camp-creek-double-c",
            "name": "Camp Creek State Park Double C Horse and Rider Campground",
            "location": "Camp Creek, WV",
            "state": "WV",
            "latitude": 37.5168,
            "longitude": -81.1486,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Horse Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "304-425-9481",
            "website": "https://wvstateparks.com/parks/camp-creek-state-park-and-forest/lodging/camping-at-camp-creek-state-park/",
            "description": "Official West Virginia State Parks equestrian campground at Camp Creek State Park and Forest. The Double C Horse and Rider Campground is designed for visitors traveling with horses and the park activities page says equestrian camping is available.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WV State Parks",
        },
        {
            "id": "wv-stateparks-holly-river-equestrian",
            "name": "Holly River State Park Equestrian Campsites",
            "location": "Hacker Valley, WV",
            "state": "WV",
            "latitude": 38.7146,
            "longitude": -80.2951,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 3,
            "phone": "304-493-6353",
            "website": "https://wvstateparks.com/parks/holly-river-state-park/activities/",
            "description": "Official West Virginia State Parks equestrian campsites at Holly River State Park. The park activities page says overnight camping with horses is available by reservation for campsites 79, 80 and 81, and corrals are included in the campsite reservation.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": True,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "WV State Parks",
        },
    ]
    print(f"  West Virginia State Parks: {len(parks)} official equestrian-camping listings")
    return parks



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
    """Fetch official Kentucky State Parks equestrian campgrounds.
    Kentucky State Parks officially lists four parks with equestrian-friendly sites:
    Dale Hollow Lake, Pennyrile Forest, Taylorsville Lake, and Carter Caves.
    Source page is parks.ky.gov/parks/horse-camping.
    We use a small allowlist because the official site already names the qualifying parks.
    Coordinates are park/campground-level approximations gathered from public campground/park references
    so the parks render correctly in the app map.
    """
    parks = [
        {
            "id": "ky-stateparks-dale-hollow",
            "name": "Dale Hollow Lake State Resort Park Equestrian Campground",
            "location": "Burkesville, KY",
            "state": "KY",
            "latitude": 36.6386215,
            "longitude": -85.2981537,
            "website": "https://parks.ky.gov/parks/find-a-park/dale-hollow-lake-state-resort-park-7787",
            "phone": "270-433-7431",
            "description": "Official Kentucky State Parks equestrian-friendly campground at Dale Hollow Lake State Resort Park.",
            "hookups": ["30A", "Water"],
            "hasDumpStation": True,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "seasonStart": 3,
            "seasonEnd": 11,
        },
        {
            "id": "ky-stateparks-pennyrile-forest",
            "name": "Pennyrile Forest State Resort Park Equestrian Campground",
            "location": "Dawson Springs, KY",
            "state": "KY",
            "latitude": 37.0728172,
            "longitude": -87.6637,
            "website": "https://parks.ky.gov/parks/find-a-park/pennyrile-forest-state-resort-park-7798",
            "phone": "270-797-3421",
            "description": "Official Kentucky State Parks equestrian-friendly campground at Pennyrile Forest State Resort Park.",
            "hookups": ["30A", "Water"],
            "hasDumpStation": True,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "seasonStart": 1,
            "seasonEnd": 12,
        },
        {
            "id": "ky-stateparks-taylorsville-lake",
            "name": "Taylorsville Lake State Park Equestrian Campground",
            "location": "Mount Eden, KY",
            "state": "KY",
            "latitude": 38.0292,
            "longitude": -85.25576,
            "website": "https://parks.ky.gov/parks/find-a-park/taylorsville-lake-state-park-7810",
            "phone": "502-477-8713",
            "description": "Official Kentucky State Parks equestrian-friendly campground at Taylorsville Lake State Park.",
            "hookups": ["30A", "Water"],
            "hasDumpStation": True,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "seasonStart": 3,
            "seasonEnd": 12,
        },
        {
            "id": "ky-stateparks-carter-caves",
            "name": "Carter Caves State Resort Park Equestrian Campground",
            "location": "Olive Hill, KY",
            "state": "KY",
            "latitude": 38.3717468,
            "longitude": -83.1185085,
            "website": "https://parks.ky.gov/parks/find-a-park/carter-caves-state-resort-park-7785",
            "phone": "606-286-4411",
            "description": "Official Kentucky State Parks equestrian-friendly campground at Carter Caves State Resort Park.",
            "hookups": ["30A", "Water"],
            "hasDumpStation": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "seasonStart": 1,
            "seasonEnd": 12,
        },
    ]

    camps = []
    for p in parks:
        camps.append({
            "id": p["id"],
            "name": p["name"],
            "location": p["location"],
            "state": p["state"],
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": ["Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": p["phone"],
            "website": p["website"],
            "description": p["description"],
            "isVerified": False,
            "seasonStart": p["seasonStart"],
            "seasonEnd": p["seasonEnd"],
            "hasWashRack": False,
            "hasDumpStation": p["hasDumpStation"],
            "hasWifi": False,
            "hasBathhouse": p["hasBathhouse"],
            "pullThroughAvailable": p["pullThroughAvailable"],
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "KY State Parks",
        })

    print(f"  Kentucky State Parks: {len(camps)} official equestrian-camping listings")
    return camps


def fetch_pa_state_parks():
    """Fetch Pennsylvania State Parks horse-camping locations conservatively.

    This first pass only includes parks where an official Pennsylvania DCNR state-park
    page explicitly indicates overnight/equestrian camping at a park trailhead or
    otherwise clearly supports camping with horses.

    Current conservative allowlist:
      - Kettle Creek State Park: official horseback riding page states
        "Overnight camping at the trailhead is by permit only."
    """
    parks = [
        {
            "id": "pa-stateparks-kettle-creek",
            "name": "Kettle Creek State Park Equestrian Trailhead Camp",
            "location": "Renovo, PA",
            "state": "PA",
            "latitude": 41.3632,
            "longitude": -77.9166,
            "website": "https://www.pa.gov/agencies/dcnr/recreation/where-to-go/state-parks/find-a-park/kettle-creek-state-park/horseback-riding.html",
            "phone": "570-923-6000",
            "description": "Official Pennsylvania DCNR horseback-riding trailhead at Kettle Creek State Park. Overnight camping at the trailhead is by permit only.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "seasonStart": 1,
            "seasonEnd": 12,
        },
    ]

    camps = []
    for p in parks:
        camps.append({
            "id": p["id"],
            "name": p["name"],
            "location": p["location"],
            "state": p["state"],
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": p["phone"],
            "website": p["website"],
            "description": p["description"],
            "isVerified": False,
            "seasonStart": p["seasonStart"],
            "seasonEnd": p["seasonEnd"],
            "hasWashRack": False,
            "hasDumpStation": p["hasDumpStation"],
            "hasWifi": False,
            "hasBathhouse": p["hasBathhouse"],
            "pullThroughAvailable": p["pullThroughAvailable"],
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "PA State Parks",
        })
    print(f"  Pennsylvania State Parks: {len(camps)} official equestrian-camping listings")
    return camps



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
    """Fetch Wisconsin DNR equestrian campsites using a fixed official allowlist.

    Uses the official Wisconsin DNR equestrian-camping list, but avoids live geocoding
    so nightly runs are deterministic and do not come back as zero when geocoding fails.
    """
    camps_data = [
        {
            "name": "Governor Dodge State Park Equestrian Campground",
            "location": "Dodgeville, WI",
            "latitude": 43.0176,
            "longitude": -90.1238,
            "website": "https://dnr.wisconsin.gov/topic/parks/govdodge/recreation/horse",
            "description": "Official Wisconsin DNR equestrian campground at Governor Dodge State Park for overnight stays adjacent to bridle trails.",
            "hookups": ["30A"],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "Governor Knowles State Forest Equestrian Campground",
            "location": "Grantsburg, WI",
            "latitude": 45.7865,
            "longitude": -92.6141,
            "website": "https://dnr.wisconsin.gov/topic/parks/camping/types.html",
            "description": "Official Wisconsin DNR equestrian campsites at Governor Knowles State Forest adjacent to equestrian riding trails.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "Kettle Moraine State Forest - Northern Unit New Prospect Horse Riders Campground",
            "location": "Campbellsport, WI",
            "latitude": 43.6570,
            "longitude": -88.1353,
            "website": "https://dnr.wisconsin.gov/topic/parks/kmn/recreation/horse",
            "description": "Official Wisconsin DNR equestrian camping at the New Prospect Horse Riders Campground in the Kettle Moraine State Forest - Northern Unit.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "Kettle Moraine State Forest - Southern Unit Equestrian Campground",
            "location": "Eagle, WI",
            "latitude": 42.8313,
            "longitude": -88.4616,
            "website": "https://dnr.wisconsin.gov/topic/parks/camping/types.html",
            "description": "Official Wisconsin DNR equestrian campsites in the Kettle Moraine State Forest - Southern Unit adjacent to horse trails.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": False,
            "hasDumpStation": False,
        },
        {
            "name": "Wildcat Mountain State Park Horse Campground",
            "location": "Ontario, WI",
            "latitude": 43.7422,
            "longitude": -90.5969,
            "website": "https://dnr.wisconsin.gov/topic/parks/wildcat/recreation/camping",
            "description": "Official Wisconsin DNR horse campground at Wildcat Mountain State Park with reservable sites for people with horses.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasBathhouse": True,
            "hasDumpStation": True,
        },
    ]

    camps = []
    for p in camps_data:
        camps.append({
            "id": "wi-stateparks-" + re.sub(r'[^a-z0-9]+', '-', p["name"].lower()).strip('-'),
            "name": p["name"],
            "location": p["location"],
            "state": "WI",
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "1-888-936-7463",
            "website": p["website"],
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 5,
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
            "sourceDetail": "WI State Parks",
        })
    print(f"  Wisconsin State Parks: {len(camps)} official equestrian-camping listings")
    return camps
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
    """Fetch official Indiana DNR horse/equestrian camping properties conservatively.

    Uses the official Indiana DNR horse-use fees page as an allowlist of state parks and
    state-managed lakes with horse/equestrian camping.

    This version uses fixed coordinates instead of live geocoding so nightly runs stay
    deterministic and avoid Nominatim rate-limit delays.
    """
    properties = [
        {
            "name": "Brown County State Park Horsemen's Campground",
            "location": "Nashville, IN",
            "latitude": 39.12899036,
            "longitude": -86.19548562,
            "website": "https://www.in.gov/dnr/state-parks/rates-and-fees/horse-use-fees/",
            "description": "Official Indiana DNR horse/equestrian camping property at Brown County State Park.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails"],
            "hasDumpStation": True,
            "hasBathhouse": True,
        },
        {
            "name": "O'Bannon Woods State Park Horsemen's Campground",
            "location": "Leavenworth, IN",
            "latitude": 38.199522,
            "longitude": -86.26463,
            "website": "https://www.in.gov/dnr/state-parks/rates-and-fees/horse-use-fees/",
            "description": "Official Indiana DNR horse/equestrian camping property at O'Bannon Woods State Park.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasDumpStation": True,
            "hasBathhouse": True,
        },
        {
            "name": "Potato Creek State Park Horsemen's Campground",
            "location": "North Liberty, IN",
            "latitude": 41.535333,
            "longitude": -86.349722,
            "website": "https://www.in.gov/dnr/state-parks/rates-and-fees/horse-use-fees/",
            "description": "Official Indiana DNR horse/equestrian camping property at Potato Creek State Park.",
            "hookups": ["30A"],
            "accommodations": ["Trails"],
            "hasDumpStation": True,
            "hasBathhouse": True,
        },
        {
            "name": "Salamonie Lake Horsemen's Campground",
            "location": "Andrews, IN",
            "latitude": 40.765376,
            "longitude": -85.625984,
            "website": "https://www.in.gov/dnr/state-parks/rates-and-fees/horse-use-fees/",
            "description": "Official Indiana DNR horse/equestrian camping property at Salamonie Lake.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasDumpStation": True,
            "hasBathhouse": True,
        },
        {
            "name": "Tippecanoe River State Park Horse Sites",
            "location": "Winamac, IN",
            "latitude": 41.117222,
            "longitude": -86.602472,
            "website": "https://www.in.gov/dnr/state-parks/rates-and-fees/horse-use-fees/",
            "description": "Official Indiana DNR horse/equestrian camping property at Tippecanoe River State Park.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasDumpStation": True,
            "hasBathhouse": True,
        },
        {
            "name": "Whitewater Memorial State Park Horsemen's Campground",
            "location": "Liberty, IN",
            "latitude": 39.613887,
            "longitude": -84.939813,
            "website": "https://www.in.gov/dnr/state-parks/rates-and-fees/horse-use-fees/",
            "description": "Official Indiana DNR horse/equestrian camping property at Whitewater Memorial State Park.",
            "hookups": [],
            "accommodations": ["Trails"],
            "hasDumpStation": True,
            "hasBathhouse": True,
        },
    ]

    camps = []
    for p in properties:
        camps.append({
            "id": "in-stateparks-" + re.sub(r'[^a-z0-9]+', '-', p["name"].lower()).strip('-'),
            "name": p["name"],
            "location": p["location"],
            "state": "IN",
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "1-866-622-6746",
            "website": p["website"],
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 4,
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
            "sourceDetail": "IN State Parks",
        })
    print(f"  Indiana State Parks: {len(camps)} official equestrian-camping listings")
    return camps

def fetch_tx_state_parks():
    """Fetch Texas Parks & Wildlife overnight equestrian camping using fixed coordinates.

    Uses the official Texas statewide equestrian page as an allowlist, but avoids live
    geocoding so nightly runs are deterministic and do not rate-limit out.
    """
    camps_data = [
        {
            "name": "Big Bend Ranch State Park Equestrian Camp",
            "location": "Presidio, TX",
            "latitude": 29.4868,
            "longitude": -104.1510,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Big Bend Ranch State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Caprock Canyons State Park & Trailway Equestrian Camp",
            "location": "Quitaque, TX",
            "latitude": 34.4338,
            "longitude": -101.0824,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Caprock Canyons State Park & Trailway.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Cooper State Park (South Sulphur Unit) Equestrian Camp",
            "location": "Cooper, TX",
            "latitude": 33.3726,
            "longitude": -95.6835,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Cooper State Park South Sulphur Unit.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Copper Breaks State Park Equestrian Camp",
            "location": "Quanah, TX",
            "latitude": 34.1110,
            "longitude": -99.7488,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Copper Breaks State Park.",
            "hookups": ["Water"],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Davis Mountains State Park Equestrian Camp",
            "location": "Fort Davis, TX",
            "latitude": 30.5970,
            "longitude": -103.9387,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Davis Mountains State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Fort Richardson State Park Equestrian Camp",
            "location": "Jacksboro, TX",
            "latitude": 33.2199,
            "longitude": -98.1632,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Fort Richardson State Park.",
            "hookups": ["20A", "30A", "50A", "Water"],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Hill Country State Natural Area Equestrian Camp",
            "location": "Bandera, TX",
            "latitude": 29.6748,
            "longitude": -99.0737,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Hill Country State Natural Area.",
            "hookups": ["30A", "Water"],
            "accommodations": ["Trails", "Stalls"],
            "maxRigLength": 0,
            "stallCount": 9,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Lake Mineral Wells State Park & Trailway Equestrian Camp",
            "location": "Mineral Wells, TX",
            "latitude": 32.8088,
            "longitude": -98.0565,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Lake Mineral Wells State Park & Trailway.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
        {
            "name": "Pedernales Falls State Park Equestrian Camp",
            "location": "Johnson City, TX",
            "latitude": 30.3089,
            "longitude": -98.2485,
            "description": "Official Texas Parks & Wildlife overnight equestrian camping at Pedernales Falls State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Paddocks"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "hasDumpStation": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
        },
    ]

    camps = []
    for p in camps_data:
        cid = "txsp-" + re.sub(r'[^a-z0-9]+', '-', p["name"].lower()).strip("-")
        camps.append({
            "id": cid,
            "name": p["name"],
            "location": p["location"],
            "state": "TX",
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": p["maxRigLength"],
            "stallCount": p["stallCount"],
            "paddockCount": p["paddockCount"],
            "phone": "512-389-8900",
            "website": "https://tpwd.texas.gov/state-parks/parks/things-to-do/equestrian",
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 0,
            "seasonEnd": 0,
            "hasWashRack": False,
            "hasDumpStation": p["hasDumpStation"],
            "hasWifi": False,
            "hasBathhouse": p["hasBathhouse"],
            "pullThroughAvailable": p["pullThroughAvailable"],
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "TX State Parks",
        })

    print(f"  Texas State Parks: {len(camps)} official equestrian-camping listings")
    return camps
def fetch_oh_state_parks():
    """Fetch a conservative first-pass set of Ohio state-park bridle camps.

    Ohio's official state-park web/search surfaces are fragmented, so this first
    pass uses a strict allowlist of well-known ODNR bridle camps with manually
    assigned coordinates to avoid geocoding failures.
    """
    camps_data = [
        {
            "name": "Barkcamp State Park Horsemen's Camp",
            "location": "Belmont, OH",
            "latitude": 40.040640,
            "longitude": -81.024306,
            "description": "Ohio state-park bridle camp at Barkcamp State Park.",
            "hookups": ["30A"],
            "accommodations": ["Trails", "Highlines"],
            "hasBathhouse": True,
            "hasWashRack": True,
            "phone": "(740) 484-4064",
        },
        {
            "name": "Hueston Woods State Park Horsemen's Camp",
            "location": "College Corner, OH",
            "latitude": 39.582059,
            "longitude": -84.736139,
            "description": "Ohio state-park bridle camp at Hueston Woods State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "hasBathhouse": True,
            "hasWashRack": True,
            "phone": "(866) 644-6727",
        },
        {
            "name": "Salt Fork State Park Equestrian Camp",
            "location": "Lore City, OH",
            "latitude": 40.122027,
            "longitude": -81.494798,
            "description": "Ohio state-park bridle camp at Salt Fork State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "hasBathhouse": False,
            "hasWashRack": True,
            "phone": "(740) 439-3521",
        },
        {
            "name": "West Branch State Park Equestrian Camp",
            "location": "Ravenna, OH",
            "latitude": 41.150391,
            "longitude": -81.112260,
            "description": "Ohio state-park bridle camp at West Branch State Park.",
            "hookups": [],
            "accommodations": ["Trails", "Highlines"],
            "hasBathhouse": False,
            "hasWashRack": False,
            "phone": "(866) 644-6727",
        },
    ]
    camps = []
    for p in camps_data:
        camps.append({
            "id": "oh-stateparks-" + re.sub(r'[^a-z0-9]+', '-', p["name"].lower()).strip('-'),
            "name": p["name"],
            "location": p["location"],
            "state": "OH",
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": p["hookups"],
            "accommodations": p["accommodations"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": p["phone"],
            "website": "https://ohiodnr.gov/go-and-do/plan-a-visit/find-a-property",
            "description": p["description"],
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": p["hasWashRack"],
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": p["hasBathhouse"],
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "F1948A"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "OH State Parks",
        })
    print(f"  Ohio State Parks: {len(camps)} provisional bridle-camp listings")
    return camps



def fetch_or_state_parks():
    """Fetch official Oregon State Parks horse-camping locations.

    Uses a strict fixed allowlist based on official Oregon State Parks pages that
    explicitly describe horse camps / horse-camp sites for overnight use.
    """
    parks = [
        {
            "id": "or-stateparks-bullards-beach",
            "name": "Bullards Beach State Park Horse Camp",
            "location": "Bandon, OR",
            "state": "OR",
            "latitude": 43.1458,
            "longitude": -124.4120,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "800-551-6949",
            "website": "https://stateparks.oregon.gov/index.cfm?do=parkPage.dsp_parkRates&parkId=50",
            "description": "Official Oregon State Parks horse camp at Bullards Beach State Park with access to trails, beach, and dunes; sites feature double or quadruple corrals.",
            "isVerified": False,
            "seasonStart": 3,
            "seasonEnd": 11,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "OR State Parks",
        },
        {
            "id": "or-stateparks-collier-memorial",
            "name": "Collier Memorial State Park Horse Camp",
            "location": "Chiloquin, OR",
            "state": "OR",
            "latitude": 42.8919,
            "longitude": -121.9652,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 4,
            "paddockCount": 0,
            "phone": "800-551-6949",
            "website": "https://stateparks.oregon.gov/park_228.php",
            "description": "Official Oregon State Parks primitive horse camp and trailhead at Collier Memorial State Park with two sites and four corrals.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "OR State Parks",
        },
        {
            "id": "or-stateparks-nehalem-bay",
            "name": "Nehalem Bay State Park Horse Camp",
            "location": "Manzanita, OR",
            "state": "OR",
            "latitude": 45.6850,
            "longitude": -123.9352,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Stalls"],
            "maxRigLength": 0,
            "stallCount": 16,
            "paddockCount": 0,
            "phone": "800-551-6949",
            "website": "https://stateparks.oregon.gov/index.cfm?do=parkPage.dsp_parkPage&parkId=142",
            "description": "Official Oregon State Parks horse camp at Nehalem Bay State Park; multiple reservable horse-camp sites feature corrals with four separate stalls and nearby drinking water.",
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
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "OR State Parks",
        },
        {
            "id": "or-stateparks-emigrant-springs",
            "name": "Emigrant Springs State Heritage Area Horse Camp",
            "location": "Meacham, OR",
            "state": "OR",
            "latitude": 45.5566,
            "longitude": -118.4259,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 14,
            "paddockCount": 0,
            "phone": "541-983-2277",
            "website": "https://stateparks.oregon.gov/index.cfm?do=park.profile&parkId=16",
            "description": "Official Oregon State Parks horse camp at Emigrant Springs State Heritage Area with seven campsites, corrals, and direct access to an equestrian trail.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 9,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "OR State Parks",
        },
        {
            "id": "or-stateparks-stub-stewart",
            "name": "L.L. Stub Stewart State Park Hares Canyon Horse Camp",
            "location": "Buxton, OR",
            "state": "OR",
            "latitude": 45.7375,
            "longitude": -123.2657,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "Water", "Sewer"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "800-551-6949",
            "website": "https://stateparks.oregon.gov/index.cfm?do=conditions.dsp_parkConditions&parkId=75",
            "description": "Official Oregon State Parks Hares Canyon Horse Camp at L.L. Stub Stewart State Park with 13 full-hookup horse-camp sites and nearby multi-use trails.",
            "isVerified": False,
            "seasonStart": 5,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "OR State Parks",
        },
    ]
    print(f"  Oregon State Parks: {len(parks)} official equestrian-camping listings")
    return parks



def fetch_ne_state_parks():
    """Fetch official Nebraska state-park equestrian camping locations.

    Uses Nebraska Game and Parks' official statewide equestrian trails page as a
    strict allowlist. That page explicitly says the following parks have both
    equestrian trails and facilities for camping with horses: Branched Oak,
    Danish Alps, Fort Robinson, Indian Cave, Lewis and Clark, Niobrara, Pawnee,
    Rock Creek Station, Two Rivers, and Willow Creek.
    """
    parks = [
        {
            "id": "ne-stateparks-branched-oak",
            "name": "Branched Oak State Recreation Area Horse Camp",
            "location": "Raymond, NE",
            "state": "NE",
            "latitude": 40.9700,
            "longitude": -96.8533,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 14,
            "phone": "402-783-3400",
            "website": "https://outdoornebraska.gov/location/branched-oak/",
            "description": "Official Nebraska Game and Parks equestrian campground at Branched Oak SRA. Homestead Campground equestrian area offers 15 pull-through Equestrian Electric Plus campsites, 8 Basic equestrian campsites, 14 corrals, hand-pump drinking water, primitive toilet, picnic tables and fire rings.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": True,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-danish-alps",
            "name": "Danish Alps State Recreation Area Equestrian Campground",
            "location": "Hubbard, NE",
            "state": "NE",
            "latitude": 42.3830,
            "longitude": -96.5630,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Corrals", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 14,
            "phone": "402-632-4109",
            "website": "https://outdoornebraska.gov/location/danish-alps/",
            "description": "Official Nebraska Game and Parks equestrian campground at Danish Alps SRA. The equestrian campground offers 14 horse corrals, hitching posts and water, with a scenic horse trail around the lake.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-fort-robinson",
            "name": "Fort Robinson State Park Mare Barn Equestrian Campground",
            "location": "Crawford, NE",
            "state": "NE",
            "latitude": 42.6877,
            "longitude": -103.4685,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Stalls"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "308-665-2900",
            "website": "https://outdoornebraska.gov/location/fort-robinson/",
            "description": "Official Nebraska Game and Parks equestrian campground at Fort Robinson State Park. The Mare Barn Campground offers Electric Plus, Full Hookup and Basic campsites adjacent to Mare Barn stalls, modern restrooms and laundry, for riders bringing horses to the park.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 11,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-indian-cave",
            "name": "Indian Cave State Park Equestrian Camp",
            "location": "Shubert, NE",
            "state": "NE",
            "latitude": 40.2480,
            "longitude": -95.5730,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals", "Highlines"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 12,
            "phone": "402-883-2575",
            "website": "https://outdoornebraska.gov/location/indian-cave/",
            "description": "Official Nebraska Game and Parks equestrian camp at Indian Cave State Park. Basic equestrian camping includes toilets, a water hydrant, grills, tables, hitching posts and 12 corrals.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-lewis-clark",
            "name": "Lewis and Clark State Recreation Area Equestrian Camp Area",
            "location": "Crofton, NE",
            "state": "NE",
            "latitude": 42.7447,
            "longitude": -97.4967,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "402-388-4169",
            "website": "https://outdoornebraska.gov/location/lewis-and-clark/",
            "description": "Official Nebraska Game and Parks equestrian camping area at Lewis and Clark SRA. The South Shore area includes corrals for horses, water, restrooms and picnic areas.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-niobrara",
            "name": "Niobrara State Park Equestrian Campground",
            "location": "Niobrara, NE",
            "state": "NE",
            "latitude": 42.7601,
            "longitude": -98.0480,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 1,
            "phone": "402-857-3373",
            "website": "https://outdoornebraska.gov/location/niobrara/",
            "description": "Official Nebraska Game and Parks equestrian campground at Niobrara State Park. Basic equestrian camping is first come, first served and includes one shared corral, non-modern restroom, picnic table and grill.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-pawnee",
            "name": "Pawnee State Recreation Area Equestrian Camp",
            "location": "Lincoln, NE",
            "state": "NE",
            "latitude": 40.9010,
            "longitude": -96.8690,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Primitive Camping"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "402-796-2362",
            "website": "https://outdoornebraska.gov/location/pawnee/",
            "description": "Official Nebraska Game and Parks equestrian camp at Pawnee SRA. Primitive equestrian camping includes picnic tables, drinking water, fire rings and pit toilets.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-rock-creek",
            "name": "Rock Creek Station State Historical Park Horse Camp",
            "location": "Fairbury, NE",
            "state": "NE",
            "latitude": 40.2108,
            "longitude": -97.5370,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 20,
            "phone": "402-729-5777",
            "website": "https://outdoornebraska.gov/location/rock-creek-station-sra/",
            "description": "Official Nebraska Game and Parks horse camp at Rock Creek Station. The horse camp includes 20 individual corrals set in groups of four, water for horse and rider, picnic tables and grills, with trail access into the historical park and adjacent wildlife area.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-two-rivers",
            "name": "Two Rivers State Recreation Area Whitetail Equestrian Campground",
            "location": "Waterloo, NE",
            "state": "NE",
            "latitude": 41.2780,
            "longitude": -96.2430,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 12,
            "phone": "402-359-5165",
            "website": "https://outdoornebraska.gov/location/two-rivers/",
            "description": "Official Nebraska Game and Parks equestrian camping at Two Rivers SRA. Whitetail Campground offers Basic Equestrian campsites and 12 horse pens situated in two corrals of six.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
        {
            "id": "ne-stateparks-willow-creek",
            "name": "Willow Creek State Recreation Area Horse Campground",
            "location": "Pierce, NE",
            "state": "NE",
            "latitude": 42.2110,
            "longitude": -97.5530,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A"],
            "accommodations": ["Trails", "Corrals"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "402-329-4272",
            "website": "https://outdoornebraska.gov/willowcreek/",
            "description": "Official Nebraska Game and Parks horse campground at Willow Creek SRA. The area offers Equestrian Electric Plus and Equestrian Basic camping, with electrical camp pads, corrals and a horse group camp.",
            "isVerified": False,
            "seasonStart": 4,
            "seasonEnd": 10,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": True,
            "pullThroughAvailable": True,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "NE State Parks",
        },
    ]
    print(f"  Nebraska State Parks: {len(parks)} official equestrian-camping listings")
    return parks



def fetch_sd_state_parks():
    """Fetch a conservative first-pass set of South Dakota state-park horse camps.

    Uses official South Dakota Game, Fish & Parks horse-camping pages and
    individual park pages with explicit horse-campsite language. This first
    pass stays strict and only includes parks/recreation areas with a clear
    official horse-camp signal.
    """
    parks = [
        {
            "id": "sd-stateparks-custer-french-creek-horse-camp",
            "name": "Custer State Park – French Creek Horse Camp",
            "location": "Custer, SD",
            "state": "SD",
            "latitude": 43.7668,
            "longitude": -103.4596,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 0,
            "phone": "605-394-2693",
            "website": "https://gfp.sd.gov/parks/detail/custer-state-park/",
            "description": "Official South Dakota Game, Fish and Parks page says Custer State Park includes a horse camp, horse trails, dump station, showers, drinking water, electrical campsites elsewhere in the park, and maps for French Creek Horse Camp.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": True,
            "hasWifi": True,
            "hasBathhouse": True,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
        {
            "id": "sd-stateparks-newton-hills-horse-camp",
            "name": "Newton Hills State Park Horse Camp",
            "location": "Canton, SD",
            "state": "SD",
            "latitude": 43.3020,
            "longitude": -96.6087,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 10,
            "phone": "605-987-2263",
            "website": "https://gfp.sd.gov/parks/detail/newton-hills-state-park/",
            "description": "Official South Dakota Game, Fish and Parks page says Newton Hills State Park has 10 horse campsites plus horse trails, drinking water, dump station, flush toilets, and showers.",
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
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
        {
            "id": "sd-stateparks-union-grove-horse-camp",
            "name": "Union Grove State Park Horse Camp",
            "location": "Beresford, SD",
            "state": "SD",
            "latitude": 43.0024,
            "longitude": -96.7807,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["30A", "Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 4,
            "phone": "605-987-2263",
            "website": "https://gfp.sd.gov/parks/detail/union-grove-state-park/",
            "description": "Official South Dakota Game, Fish and Parks page says Union Grove State Park includes 4 horse campsites and horse-friendly multi-use trails. South Dakota tourism also describes electrical hookups, water, and corrals at the horse camp.",
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
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
        {
            "id": "sd-stateparks-sheps-canyon-horse-camp",
            "name": "Sheps Canyon Recreation Area Horse Camp",
            "location": "Hot Springs, SD",
            "state": "SD",
            "latitude": 43.3462,
            "longitude": -103.4875,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 11,
            "phone": "605-745-6996",
            "website": "https://gfp.sd.gov/parks/detail/sheps-canyon-recreation-area/",
            "description": "Official South Dakota Game, Fish and Parks page says Sheps Canyon Recreation Area has 11 horse campsites. South Dakota's horseback-riding page says the horse camp has 11 non-electrical campsites, water, and corrals.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
        {
            "id": "sd-stateparks-bear-butte-horse-camp",
            "name": "Bear Butte State Park Horse Camp",
            "location": "Sturgis, SD",
            "state": "SD",
            "latitude": 44.5088,
            "longitude": -103.4286,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": [],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 4,
            "phone": "605-347-5243",
            "website": "https://gfp.sd.gov/parks/detail/bear-butte-state-park/",
            "description": "Official South Dakota Game, Fish and Parks page says Bear Butte State Park has 4 non-electric horse campsites and horse trails.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
        {
            "id": "sd-stateparks-pease-creek-horse-camp",
            "name": "Pease Creek Recreation Area Horse Camp",
            "location": "Geddes, SD",
            "state": "SD",
            "latitude": 43.2903,
            "longitude": -98.5565,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 5,
            "phone": "605-773-8239",
            "website": "https://gfp.sd.gov/parks/detail/pease-creek-recreation-area/",
            "description": "Official South Dakota Game, Fish and Parks page says Pease Creek Recreation Area has 5 horse campsites. South Dakota's horseback-riding page says the horse camp offers primitive camping, water, corrals, and vault toilets.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
        {
            "id": "sd-stateparks-oakwood-lakes-horse-camp",
            "name": "Oakwood Lakes State Park Horse Camp",
            "location": "Bruce, SD",
            "state": "SD",
            "latitude": 44.4726,
            "longitude": -96.9139,
            "pricePerNight": 0.0,
            "horseFeePerNight": 0.0,
            "hookups": ["Water"],
            "accommodations": ["Corrals", "Trails"],
            "maxRigLength": 0,
            "stallCount": 0,
            "paddockCount": 6,
            "phone": "605-627-5671",
            "website": "https://gfp.sd.gov/parks/detail/oakwood-lakes-state-park/",
            "description": "Official South Dakota Game, Fish and Parks page says Oakwood Lakes State Park has 6 horse campsites. South Dakota's horseback-riding page says the horse camp offers primitive sites, water, hitching posts, and corrals.",
            "isVerified": False,
            "seasonStart": 1,
            "seasonEnd": 12,
            "hasWashRack": False,
            "hasDumpStation": False,
            "hasWifi": False,
            "hasBathhouse": False,
            "pullThroughAvailable": False,
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": ["C0392B", "E3A18B"],
            "photoURLs": [],
            "source": "State Parks",
            "sourceDetail": "SD State Parks",
        },
    ]

    print(f"  South Dakota State Parks: {len(parks)} official equestrian-camping listings")
    return parks

def main():
    print(f"HorseCamp data fetch starting — {datetime.now(timezone.utc).isoformat()}")
    print(f"RIDB key present: {'Yes' if RIDB_KEY else 'NO — set RIDB_API_KEY secret'}")
    print(f"NPS key present:  {'Yes' if NPS_KEY  else 'NO — set NPS_API_KEY secret'}")

    all_camps = {}
    total_ridb = 0
    total_nps = 0

    # Primary RIDB/NPS pass already runs alphabetically because STATES is alphabetical.
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

    print("\nChecking verified overrides...")
    if not os.path.exists(OVERRIDE_FILE):
        print(f"  {OVERRIDE_FILE} not found — generating template for verification")
        generate_override_template(all_camps)
    else:
        apply_overrides(all_camps)

    camps_list = sorted(all_camps.values(), key=lambda c: c["state"])
    output = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(camps_list),
        "sources": ["Recreation.gov RIDB", "NPS API"] + state_park_sources + ["OpenStreetMap", "Layover"],
        "camps": camps_list,
    }
    with open("camps.json", "w") as f:
        json.dump(output, f, indent=2)

    osm_count = sum(1 for c in camps_list if c.get("source") == "OSM")
    layover_count = sum(1 for c in camps_list if c.get("source") == "Layover")
    verified_count = sum(1 for c in camps_list if c.get("isVerified") and c.get("source") in ("Layover", "OSM"))
    print(f"\nDone. {len(camps_list)} total camps written to camps.json")
    print(f"  RIDB:         {total_ridb}")
    print(f"  NPS:          {total_nps}")
    for abbr in sorted(state_park_totals):
        print(f"  {abbr} StateParks:{state_park_totals[abbr]}")
    print(f"  Layovers:     {layover_count}")
    print(f"  OSM:          {osm_count}")
    print(f"  Verified:     {verified_count} manually verified")
    print(f"  Unique total: {len(camps_list)}")


if __name__ == "__main__":
    main()
