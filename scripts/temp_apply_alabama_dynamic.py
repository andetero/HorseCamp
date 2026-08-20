#!/usr/bin/env python3
from pathlib import Path
import importlib.util
import json
import re

ROOT = Path(__file__).resolve().parent.parent
FETCH = ROOT / "scripts" / "fetch_camps.py"
STATIC = ROOT / "data" / "state_parks" / "al.json"
SELF = Path(__file__).resolve()

ALABAMA_CODE = r'''AL_STATE_PARK_USER_AGENT = "HorseCampDataFetcher/1.0 (+https://horsecampfinder.com/)"
AL_STATE_PARK_SOURCES = [
    {
        "id": "al-stateparks-oak-mountain",
        "name": "Oak Mountain State Park Equestrian Campground",
        "park": "Oak Mountain State Park",
        "pages": [
            {
                "url": "https://www.alapark.com/parks/oak-mountain-state-park/camping",
                "start": "Equestrian Camping",
                "end": "SECURITY",
            }
        ],
    },
    {
        "id": "al-stateparks-wind-creek",
        "name": "Wind Creek State Park Equestrian Camping Area",
        "park": "Wind Creek State Park",
        "pages": [
            {
                "url": "https://www.alapark.com/parks/wind-creek-state-park",
                "start": "Our campsites are designed",
                "end": "Nature",
            },
            {
                "url": "https://www.alapark.com/parks/wind-creek-state-park/horse-camping-areaday-riding",
                "start": "Horse Camping Area/Day Riding",
                "end": "Horse Trails",
            },
        ],
    },
]
AL_STATE_PARK_REQUIRED_IDS = {row["id"] for row in AL_STATE_PARK_SOURCES}


def _fetch_official_state_park_page(url, label, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=(8, 20),
                headers={"User-Agent": AL_STATE_PARK_USER_AGENT},
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


def _load_previous_dynamic_state_park_records(state_code):
    path = REPO_ROOT / "camps.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        print(f"  WARNING: could not read prior camps.json for {state_code}: {error}")
        return []
    rows = payload.get("camps", []) if isinstance(payload, dict) else []
    return [
        dict(row)
        for row in rows
        if isinstance(row, dict)
        and row.get("source") == "State Parks"
        and row.get("state") == state_code
        and row.get("isVerified") is True
    ]


def _guard_dynamic_state_park_result(state_code, camps, required_ids):
    required_ids = set(required_ids)
    current_ids = {str(row.get("id") or "") for row in camps}
    if required_ids.issubset(current_ids):
        return camps
    previous = _load_previous_dynamic_state_park_records(state_code)
    previous_ids = {str(row.get("id") or "") for row in previous}
    if required_ids.issubset(previous_ids):
        print(
            f"  WARNING: {state_code} State Parks live fetch incomplete; "
            f"retaining {len(previous)} last-published dynamic records"
        )
        return previous
    missing = sorted(required_ids - current_ids)
    raise RuntimeError(
        f"{state_code} State Parks live fetch incomplete; missing {', '.join(missing)} "
        "and no validated dynamic fallback is available"
    )


def _alabama_section_text(raw_html, start_label, end_label):
    text = _strip_html_basic(raw_html)
    low = text.lower()
    start = low.find(start_label.lower())
    if start < 0:
        return ""
    end = low.find(end_label.lower(), start + len(start_label)) if end_label else -1
    if end < 0:
        end = min(len(text), start + 6000)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _alabama_phone(raw_html):
    text = _strip_html_basic(raw_html)
    match = re.search(r"\b(?:\+?1[-. ]*)?(\d{3})[-. )]+(\d{3})[-. ]+(\d{4})\b", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def _alabama_hookups(section):
    low = section.lower()
    hookups = []
    dash = r"[\s\-\u2010-\u2015]*"
    if re.search(rf"\b50{dash}amp\b|\b50a\b", low):
        hookups.append("50A")
    if re.search(rf"\b30{dash}amp\b|\b30a\b", low):
        hookups.append("30A")
    if "water" in low and re.search(r"\b(?:hook\s*ups?|service|spigots?|hydrants?)\b", low):
        hookups.append("Water")
    if re.search(r"\bsewer\s+hook\s*ups?\b|\bwater,?\s+and\s+sewer\s+hook\s*ups?\b", low):
        hookups.append("Sewer")
    return list(dict.fromkeys(hookups))


def _alabama_accommodations(section):
    low = section.lower()
    accommodations = ["Trails"]
    if re.search(r"\bstalls?\b", low):
        accommodations.append("Stalls")
    if re.search(r"\bcorrals?\b", low):
        accommodations.append("Corrals")
    if re.search(r"\bhighlines?\b|\bhigh lines?\b|\btie rails?\b", low):
        accommodations.append("Highlines")
    return list(dict.fromkeys(accommodations))


def _alabama_stall_count(section):
    match = re.search(r"\b(\d+)\s+(?:covered\s+)?stalls?\b", section, flags=re.I)
    return int(match.group(1)) if match else 0


def _build_alabama_state_park(source):
    sections = []
    final_url = ""
    raw_pages = []
    for page in source["pages"]:
        fetched_url, raw_html = _fetch_official_state_park_page(
            page["url"], f"Alabama State Parks {source['park']}"
        )
        if not raw_html:
            continue
        raw_pages.append(raw_html)
        final_url = fetched_url or final_url
        section = _alabama_section_text(raw_html, page["start"], page["end"])
        if section:
            sections.append(section)
        time.sleep(0.25)
    if not sections:
        print(f"  Alabama State Parks: expected horse-camping section missing for {source['park']}")
        return None
    section = " ".join(sections)
    lat, lon = _geocode_place_nominatim(f"{source['park']}, Alabama")
    time.sleep(1.0)
    if abs(lat) < 0.1 or abs(lon) < 0.1:
        print(f"  Alabama State Parks: could not geocode {source['park']}")
        return None
    lower = section.lower()
    raw_combined = " ".join(raw_pages)
    return {
        "id": source["id"],
        "name": source["name"],
        "location": f"{source['park']}, AL",
        "state": "AL",
        "latitude": lat,
        "longitude": lon,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": _alabama_hookups(section),
        "accommodations": _alabama_accommodations(section),
        "maxRigLength": 0,
        "stallCount": _alabama_stall_count(section),
        "paddockCount": 0,
        "phone": _alabama_phone(raw_combined),
        "website": final_url or source["pages"][0]["url"],
        "description": section[:2000],
        "isVerified": True,
        "seasonStart": 1 if "year-round" in lower or "year round" in lower else 0,
        "seasonEnd": 12 if "year-round" in lower or "year round" in lower else 0,
        "hasWashRack": bool(re.search(r"\b(?:wash rack|horse wash)\b", lower)),
        "hasDumpStation": bool(re.search(r"\b(?:rv dump station|sanitary dump)\b", lower)),
        "hasWifi": bool(re.search(r"\b(?:wi-fi|wifi|internet)\b", lower)),
        "hasBathhouse": bool(re.search(r"\b(?:bathhouse|shower house|shower building)\b", lower)),
        "pullThroughAvailable": bool(re.search(r"\bpull[- ]through\b", lower)),
        "imageColors": ["C0392B", "F1948A"],
        "photoURLs": [],
        "source": "State Parks",
    }


def fetch_al_state_parks():
    """Fetch Alabama's established equestrian campgrounds from official Alapark pages."""
    camps = []
    for source in AL_STATE_PARK_SOURCES:
        camp = _build_alabama_state_park(source)
        if camp:
            camps.append(camp)
    camps.sort(key=lambda row: row["name"])
    camps = _guard_dynamic_state_park_result("AL", camps, AL_STATE_PARK_REQUIRED_IDS)
    print(f"  Alabama State Parks: {len(camps)} dynamic official equestrian-camping listings")
    return camps
'''


def apply_patch():
    text = FETCH.read_text(encoding="utf-8")
    old = '''def fetch_al_state_parks():
    """Load manual AL state-park listings from data/state_parks/al.json."""
    return load_manual_state_parks("AL")
'''
    if old not in text:
        raise RuntimeError("Expected manual Alabama importer not found")
    FETCH.write_text(text.replace(old, ALABAMA_CODE, 1), encoding="utf-8")


def validate():
    old = json.loads(STATIC.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("fetch_camps", FETCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_geocode_cache()
    new = module.fetch_al_state_parks()
    old_by = {c["id"]: c for c in old}
    new_by = {c["id"]: c for c in new}
    print("\n=== Alabama static -> dynamic comparison ===")
    print("Old IDs:", sorted(old_by))
    print("New IDs:", sorted(new_by))
    print("Added:", sorted(set(new_by) - set(old_by)))
    print("Removed:", sorted(set(old_by) - set(new_by)))
    print(json.dumps(new, indent=2, ensure_ascii=False))

    expected = {"al-stateparks-oak-mountain", "al-stateparks-wind-creek"}
    if set(new_by) != expected:
        raise RuntimeError(f"Expected exactly Oak Mountain + Wind Creek; got {sorted(new_by)}")
    oak = new_by["al-stateparks-oak-mountain"]
    for value in ("50A", "Water", "Sewer"):
        if value not in oak.get("hookups", []):
            raise RuntimeError(f"Oak Mountain missing {value}")
    if "Stalls" not in oak.get("accommodations", []):
        raise RuntimeError("Oak Mountain missing stalls")
    wind = new_by["al-stateparks-wind-creek"]
    for value in ("30A", "Water"):
        if value not in wind.get("hookups", []):
            raise RuntimeError(f"Wind Creek missing {value}")


if __name__ == "__main__":
    apply_patch()
    validate()
    STATIC.unlink()
    SELF.unlink()
    print("Alabama dynamic conversion validated; removed al.json and temporary patch script.")
