#!/usr/bin/env python3
"""
Import curated Google Maps Saved List CSV exports into HorseCamp JSON files.

This script is intentionally NOT a Google Places crawler/geocoder.

Expected workflow:
  1. Curate places in Google Maps Saved Lists.
  2. Put coordinates in each saved item's Note field.
  3. Export the list CSV with Google Takeout.
  4. Place CSVs at:
       data/imports/horse_layovers.csv
       data/imports/horse_camps.csv
  5. Run this script/workflow.

Supported note formats:

Simple 3-line format:
  Better Display Name
  37.123456, -109.123456
  https://example.com/

Labeled format:
  name: Better Display Name
  coords: 37.123456, -109.123456
  website: https://example.com/

Optional labeled note fields:
  state: UT
  location: 11175 Paria Valley Rd, Kanab, UT 84741
  phone: +14357034112
  website: https://example.com
  description: Short custom description
  hookups: 30A, 50A, Water
  accommodations: Stalls, Corrals, Trails
  rig: 60
  stalls: 12
  paddocks: 4
  wash: yes
  dump: yes
  wifi: no
  bathhouse: yes
  pullthrough: yes
  price: 55
  horsefee: 20

Generated IDs use these prefixes:
  layover-gmaps-
  private-gmaps-

On each run, previous records with those prefixes are removed and rebuilt
from the current CSV. Existing manual records are preserved.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


LAYOVER_ID_PREFIX = "layover-gmaps-"
PRIVATE_ID_PREFIX = "private-gmaps-"

DEFAULT_IMAGE_COLORS = ["6D4C41", "BCAAA4"]

PRIVATE_DEFAULT_DESCRIPTION = (
    "Private camp. Details may change; please confirm horse access, availability, "
    "amenities, fees, and rules before travel."
)

LAYOVER_DEFAULT_DESCRIPTION = (
    "Horse layover. Details may change; please confirm overnight horse access, "
    "availability, amenities, fees, and rules before travel."
)

COORDS_LABEL_RE = re.compile(
    r"(?im)^\s*(?:coords?|coordinates?|lat(?:itude)?\s*/?\s*lon(?:gitude)?|lat\s*,\s*lng)\s*:\s*"
    r"(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$"
)
DECIMAL_PAIR_RE = re.compile(r"(?<![\d.-])(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)(?![\d.-])")
DMS_PAIR_RE = re.compile(
    r"""(?ix)
    (\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([NS])
    \s+
    (\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([EW])
    """
)


STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}

# Rough bounding boxes only, used as a fallback when the note does not include state.
# For border cases, add "state: XX" to the note.
STATE_BOUNDS = [
    ("AK", 51.0, 72.0, -180.0, -129.0), ("HI", 18.5, 22.5, -161.0, -154.0),
    ("AL", 30.1, 35.1, -88.6, -84.8), ("AR", 33.0, 36.6, -94.7, -89.6),
    ("AZ", 31.2, 37.1, -114.9, -109.0), ("CA", 32.4, 42.1, -124.6, -114.0),
    ("CO", 36.8, 41.1, -109.2, -101.9), ("CT", 40.9, 42.1, -73.8, -71.7),
    ("DE", 38.4, 39.9, -75.9, -75.0), ("FL", 24.3, 31.1, -87.8, -80.0),
    ("GA", 30.3, 35.1, -85.7, -80.7), ("IA", 40.2, 43.6, -96.7, -90.0),
    ("ID", 42.0, 49.1, -117.3, -111.0), ("IL", 36.8, 42.6, -91.6, -87.0),
    ("IN", 37.7, 41.9, -88.2, -84.7), ("KS", 36.9, 40.1, -102.2, -94.4),
    ("KY", 36.4, 39.2, -89.7, -81.9), ("LA", 28.8, 33.2, -94.2, -88.8),
    ("MA", 41.1, 42.9, -73.6, -69.8), ("MD", 37.8, 39.8, -79.6, -75.0),
    ("ME", 42.9, 47.6, -71.2, -66.8), ("MI", 41.6, 48.4, -90.5, -82.1),
    ("MN", 43.4, 49.4, -97.4, -89.4), ("MO", 35.9, 40.7, -95.8, -89.0),
    ("MS", 30.1, 35.1, -91.7, -88.0), ("MT", 44.2, 49.1, -116.2, -104.0),
    ("NC", 33.7, 36.7, -84.4, -75.3), ("ND", 45.8, 49.1, -104.2, -96.4),
    ("NE", 39.9, 43.1, -104.2, -95.2), ("NH", 42.6, 45.4, -72.6, -70.5),
    ("NJ", 38.8, 41.4, -75.6, -73.8), ("NM", 31.2, 37.1, -109.2, -103.0),
    ("NV", 35.0, 42.1, -120.1, -114.0), ("NY", 40.4, 45.1, -79.9, -71.8),
    ("OH", 38.3, 42.4, -84.9, -80.5), ("OK", 33.5, 37.1, -103.1, -94.3),
    ("OR", 41.8, 46.4, -124.7, -116.4), ("PA", 39.6, 42.4, -80.7, -74.6),
    ("RI", 41.0, 42.1, -71.9, -71.0), ("SC", 32.0, 35.3, -83.5, -78.4),
    ("SD", 42.4, 45.95, -104.2, -96.4), ("TN", 34.8, 36.8, -90.4, -81.5),
    ("TX", 25.8, 36.6, -106.7, -93.4), ("UT", 36.8, 42.1, -114.2, -109.0),
    ("VA", 36.5, 39.6, -83.7, -75.0), ("VT", 42.7, 45.1, -73.5, -71.4),
    ("WA", 45.4, 49.1, -124.8, -116.8), ("WI", 42.4, 47.4, -92.9, -86.7),
    ("WV", 37.1, 40.7, -82.7, -77.7), ("WY", 40.8, 45.1, -111.2, -104.0),
    # Canadian provinces likely to appear in horse travel lists.
    ("AB", 48.9, 60.1, -120.1, -110.0), ("BC", 48.2, 60.1, -139.2, -114.0),
    ("SK", 48.9, 60.1, -110.1, -101.3), ("MB", 48.8, 60.1, -102.1, -88.8),
    ("ON", 41.6, 56.9, -95.2, -74.0), ("QC", 44.8, 62.7, -79.8, -57.0),
]


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "unknown"


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    value = str(value).strip().lower()
    return value in {"1", "true", "yes", "y", "on", "available"}


def parse_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else default


def parse_int(value: str | None, default: int = 0) -> int:
    return int(parse_float(value, default))


def format_phone(value: str | None) -> str:
    """Normalize Google-pasted phone values into a nicer display string.

    Examples:
      +14357034112 -> +1 435-703-4112
      14357034112  -> +1 435-703-4112
      4357034112   -> 435-703-4112
    Non-US/Canada or already-formatted values are preserved as clean text.
    """
    raw = clean_text(value)
    if not raw:
        return ""

    digits = re.sub(r"\D+", "", raw)

    # North American number with country code
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]}-{digits[4:7]}-{digits[7:11]}"

    # North American number without country code
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"

    return raw


def dms_to_decimal(degrees: str, minutes: str, seconds: str, hemisphere: str) -> float:
    decimal = float(degrees) + (float(minutes) / 60.0) + (float(seconds) / 3600.0)
    if hemisphere.upper() in {"S", "W"}:
        decimal *= -1
    return decimal


def parse_coordinates(note: str) -> tuple[float, float] | None:
    m = COORDS_LABEL_RE.search(note or "")
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if valid_lat_lon(lat, lon):
            return lat, lon

    # Fallback: allow a plain decimal pair anywhere in the note.
    m = DECIMAL_PAIR_RE.search(note or "")
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if valid_lat_lon(lat, lon):
            return lat, lon

    m = DMS_PAIR_RE.search(note or "")
    if m:
        lat = dms_to_decimal(m.group(1), m.group(2), m.group(3), m.group(4))
        lon = dms_to_decimal(m.group(5), m.group(6), m.group(7), m.group(8))
        if valid_lat_lon(lat, lon):
            return lat, lon

    return None


def valid_lat_lon(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def parse_note_fields(note: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in (note or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "")
        value = value.strip()
        if key and value:
            fields[key] = value
    return fields


def is_coordinate_line(line: str) -> bool:
    return bool(DECIMAL_PAIR_RE.search(line or "") or DMS_PAIR_RE.search(line or ""))


def is_url_line(line: str) -> bool:
    return bool(re.match(r"(?i)^https?://", (line or "").strip()))


def is_phone_line(line: str) -> bool:
    value = clean_text(line)
    if not value or is_url_line(value) or is_coordinate_line(value):
        return False
    digits = re.sub(r"\D+", "", value)
    return len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))


def parse_simple_note_fields(note: str) -> dict[str, str]:
    """Support easy 3-line notes:
       Better Name
       37.123456, -109.123456
       https://example.com/
    """
    fields: dict[str, str] = {}
    lines = [line.strip() for line in (note or "").splitlines() if line.strip()]

    for line in lines:
        if is_url_line(line) and "website" not in fields:
            fields["website"] = line
            continue

        if is_phone_line(line) and "phone" not in fields:
            fields["phone"] = line
            continue

        # If the line has no label and is not coordinates/URL/phone, treat the first one as a name.
        if ":" not in line and not is_coordinate_line(line) and not is_phone_line(line) and "name" not in fields:
            fields["name"] = clean_text(line)

    return fields


def merge_note_fields(labeled: dict[str, str], simple: dict[str, str]) -> dict[str, str]:
    merged = dict(simple)
    merged.update(labeled)  # explicit labeled fields win
    return merged


def infer_state_from_location(location: str) -> str:
    loc = location or ""
    m = re.search(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,|$|\s)", loc)
    if m:
        return m.group(1).upper()
    for name, abbr in STATE_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", loc, re.I):
            return abbr
    return ""


def infer_state_from_coords(lat: float, lon: float) -> str:
    for abbr, minlat, maxlat, minlon, maxlon in STATE_BOUNDS:
        if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
            return abbr
    return ""


def parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    items = re.split(r"[,;|]", value)
    return [clean_text(item) for item in items if clean_text(item)]


def normalize_hookups(items: list[str]) -> list[str]:
    aliases = {
        "30": "30A", "30a": "30A", "30 amp": "30A", "30 amps": "30A",
        "50": "50A", "50a": "50A", "50 amp": "50A", "50 amps": "50A",
        "water": "Water", "sewer": "Sewer",
    }
    out: list[str] = []
    for item in items:
        key = item.lower()
        out.append(aliases.get(key, item))
    return list(dict.fromkeys(out))


def infer_hookups(note: str, explicit: str | None = None) -> list[str]:
    if explicit:
        return normalize_hookups(parse_csv_list(explicit))
    text = (note or "").lower()
    hookups = []
    if re.search(r"\b30\s*a\b|30amp|30 amp", text):
        hookups.append("30A")
    if re.search(r"\b50\s*a\b|50amp|50 amp", text):
        hookups.append("50A")
    if re.search(r"\bwater\b", text):
        hookups.append("Water")
    if re.search(r"\bsewer\b", text):
        hookups.append("Sewer")
    return hookups


def infer_private_accommodations(note: str, explicit: str | None = None) -> list[str]:
    if explicit:
        values = parse_csv_list(explicit)
    else:
        values = ["Horse Camping"]
        text = (note or "").lower()
        if "trail" in text:
            values.append("Trails")
        if "stall" in text:
            values.append("Stalls")
        if "corral" in text or "pen" in text:
            values.append("Corrals")
        if "paddock" in text:
            values.append("Paddocks")
        if "cabin" in text:
            values.append("Cabins")
        if "primitive" in text:
            values.append("Primitive Camping")
        if "group" in text:
            values.append("Group Camping")
        if "layover" in text or "overnight" in text or "horse motel" in text:
            values.append("Layover")
    if "Horse Camping" not in values:
        values.append("Horse Camping")
    return list(dict.fromkeys(values))


def infer_layover_accommodations(note: str, explicit: str | None = None) -> list[str]:
    if explicit:
        values = parse_csv_list(explicit)
    else:
        values = ["Layover", "Horse Camping"]
        text = (note or "").lower()
        if "stall" in text:
            values.append("Stalls")
        if "corral" in text or "pen" in text:
            values.append("Corrals")
        if "paddock" in text:
            values.append("Paddocks")
        if "cabin" in text:
            values.append("Cabins")
    if "Layover" not in values:
        values.insert(0, "Layover")
    if "Horse Camping" not in values:
        values.append("Horse Camping")
    return list(dict.fromkeys(values))


def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a top-level JSON array")
    return data


def compact_json(value: Any) -> str:
    """Pretty JSON with simple scalar arrays kept on one line for easier review."""
    text = json.dumps(value, indent=2, ensure_ascii=False)

    def inline_array(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, list) and all(not isinstance(item, (dict, list)) for item in parsed):
            return json.dumps(parsed, ensure_ascii=False)
        return raw

    return re.sub(r"\[\n(?:\s+[^\[\]{}]+,?\n)+\s*\]", inline_array, text)


def write_json_array(path: Path, data: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(compact_json(data) + "\n", encoding="utf-8")


def haversine_meters(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371000.0
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def existing_manual_duplicate(record: dict[str, Any], manual_records: list[dict[str, Any]]) -> str:
    name_key = norm(record.get("name", ""))
    lat = float(record.get("latitude") or 0.0)
    lon = float(record.get("longitude") or 0.0)
    for existing in manual_records:
        existing_name_key = norm(existing.get("name", ""))
        if name_key and existing_name_key and name_key == existing_name_key:
            return f"same name as existing manual record: {existing.get('name')}"
        ex_lat = float(existing.get("latitude") or 0.0)
        ex_lon = float(existing.get("longitude") or 0.0)
        if valid_lat_lon(ex_lat, ex_lon) and haversine_meters(lat, lon, ex_lat, ex_lon) < 100:
            return f"within 100m of existing manual record: {existing.get('name')}"
    return ""


def make_id(prefix: str, name: str, state: str, seen_ids: set[str]) -> str:
    base = f"{prefix}{slugify(name)}"
    if state:
        base = f"{base}-{state.lower()}"
    candidate = base
    suffix = 2
    while candidate in seen_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    seen_ids.add(candidate)
    return candidate


def read_google_maps_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError(f"{path} has no CSV header")
        missing = {"Title", "Note"} - set(reader.fieldnames)
        if missing:
            raise RuntimeError(f"{path} is missing expected column(s): {', '.join(sorted(missing))}")
        return [row for row in reader if any((value or "").strip() for value in row.values())]


def convert_rows(
    rows: list[dict[str, str]],
    *,
    kind: str,
    existing_manual_records: list[dict[str, Any]],
    seen_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    generated: list[dict[str, Any]] = []
    report: list[str] = []
    seen_source_keys: set[str] = set()

    for index, row in enumerate(rows, start=1):
        title = clean_text(row.get("Title"))
        note = row.get("Note") or ""
        if not title:
            report.append(f"- row {index}: skipped blank title")
            continue

        coords = parse_coordinates(note)
        if not coords:
            report.append(f"- {title}: skipped, no coords found in Note")
            continue

        lat, lon = coords
        fields = merge_note_fields(parse_note_fields(note), parse_simple_note_fields(note))

        name = clean_text(fields.get("name") or title)
        location = clean_text(fields.get("location") or fields.get("address") or "")
        state = clean_text(fields.get("state") or "")
        state = state.upper() if state else infer_state_from_location(location) or infer_state_from_coords(lat, lon)
        if not location:
            location = f"{state} ({lat:.6f}, {lon:.6f})" if state else f"{lat:.6f}, {lon:.6f}"

        source_key = f"{norm(name)}:{round(lat, 6)}:{round(lon, 6)}"
        if source_key in seen_source_keys:
            report.append(f"- {name}: skipped duplicate row in CSV")
            continue
        seen_source_keys.add(source_key)

        if kind == "layover":
            prefix = LAYOVER_ID_PREFIX
            source = "Layover"
            description = clean_text(fields.get("description") or LAYOVER_DEFAULT_DESCRIPTION)
            accommodations = infer_layover_accommodations(note, fields.get("accommodations"))
        elif kind == "private":
            prefix = PRIVATE_ID_PREFIX
            source = "Private Camps"
            description = clean_text(fields.get("description") or PRIVATE_DEFAULT_DESCRIPTION)
            accommodations = infer_private_accommodations(note, fields.get("accommodations"))
        else:
            raise ValueError(f"Unsupported kind: {kind}")

        record = {
            "id": make_id(prefix, name, state, seen_ids),
            "name": name,
            "location": location,
            "state": state,
            "latitude": lat,
            "longitude": lon,
            "pricePerNight": parse_float(fields.get("price"), 0.0),
            "horseFeePerNight": parse_float(fields.get("horsefee") or fields.get("horsefeepernight"), 0.0),
            "hookups": infer_hookups(note, fields.get("hookups")),
            "accommodations": accommodations,
            "maxRigLength": parse_int(fields.get("rig") or fields.get("maxrig") or fields.get("maxriglength"), 0),
            "stallCount": parse_int(fields.get("stalls") or fields.get("stallcount"), 0),
            "paddockCount": parse_int(fields.get("paddocks") or fields.get("paddockcount"), 0),
            "phone": format_phone(fields.get("phone") or ""),
            "website": clean_text(fields.get("website") or fields.get("url") or ""),
            "description": description,
            "isVerified": parse_bool(fields.get("verified"), False),
            "seasonStart": parse_int(fields.get("seasonstart"), 1),
            "seasonEnd": parse_int(fields.get("seasonend"), 12),
            "hasWashRack": parse_bool(fields.get("wash") or fields.get("washrack"), False),
            "hasDumpStation": parse_bool(fields.get("dump") or fields.get("dumpstation"), False),
            "hasWifi": parse_bool(fields.get("wifi"), False),
            "hasBathhouse": parse_bool(fields.get("bathhouse"), False),
            "pullThroughAvailable": parse_bool(fields.get("pullthrough") or fields.get("pullthroughavailable"), False),
            "rating": 0.0,
            "reviewCount": 0,
            "imageColors": DEFAULT_IMAGE_COLORS,
            "photoURLs": [],
            "source": source,
        }

        dup_reason = existing_manual_duplicate(record, existing_manual_records)
        if dup_reason:
            report.append(f"- {name}: skipped, {dup_reason}")
            continue

        generated.append(record)

    return generated, report


def update_json_from_csv(
    *,
    csv_path: Path,
    json_path: Path,
    kind: str,
    id_prefix: str,
) -> tuple[int, int, list[str]]:
    existing = load_json_array(json_path)
    manual = [item for item in existing if not str(item.get("id", "")).startswith(id_prefix)]
    seen_ids = {str(item.get("id", "")) for item in manual if item.get("id")}

    rows = read_google_maps_csv(csv_path)
    generated, report = convert_rows(rows, kind=kind, existing_manual_records=manual, seen_ids=seen_ids)

    combined = manual + generated
    combined.sort(key=lambda item: ((item.get("state") or "ZZ"), (item.get("name") or "").lower()))
    write_json_array(json_path, combined)

    return len(rows), len(generated), report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layovers-csv", default="data/imports/horse_layovers.csv")
    parser.add_argument("--private-camps-csv", default="data/imports/horse_camps.csv")
    parser.add_argument("--layovers-json", default="data/layovers.json")
    parser.add_argument("--private-camps-json", default="data/private_camps.json")
    parser.add_argument("--report", default="data/imports/google_maps_list_import_report.md")
    parser.add_argument("--fail-on-missing-csv", action="store_true")
    args = parser.parse_args()

    jobs = [
        ("Horse Layovers", Path(args.layovers_csv), Path(args.layovers_json), "layover", LAYOVER_ID_PREFIX),
        ("Horse Camps", Path(args.private_camps_csv), Path(args.private_camps_json), "private", PRIVATE_ID_PREFIX),
    ]

    report_lines = [
        "# Google Maps Saved List Import Report",
        "",
        "This importer only trusts coordinates entered in the CSV `Note` field.",
        "It does not call Google Places and does not fuzzy-match names.",
        "",
    ]

    had_error = False

    for label, csv_path, json_path, kind, id_prefix in jobs:
        report_lines += [f"## {label}", ""]
        if not csv_path.exists():
            msg = f"Missing CSV: `{csv_path}`"
            report_lines.append(f"- {msg}")
            report_lines.append("")
            print(f"WARNING: {msg}")
            if args.fail_on_missing_csv:
                had_error = True
            continue

        try:
            row_count, generated_count, row_report = update_json_from_csv(
                csv_path=csv_path,
                json_path=json_path,
                kind=kind,
                id_prefix=id_prefix,
            )
        except Exception as exc:
            had_error = True
            report_lines.append(f"- ERROR: {exc}")
            report_lines.append("")
            print(f"ERROR importing {label}: {exc}", file=sys.stderr)
            continue

        skipped_count = len([line for line in row_report if "skipped" in line])
        report_lines += [
            f"- CSV rows: **{row_count}**",
            f"- Generated records: **{generated_count}**",
            f"- Skipped rows: **{skipped_count}**",
            f"- Output: `{json_path}`",
            "",
        ]
        if row_report:
            report_lines.append("### Row notes")
            report_lines.append("")
            report_lines.extend(row_report)
            report_lines.append("")

        print(f"{label}: {generated_count} generated from {row_count} CSV rows -> {json_path}")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"Wrote report: {report_path}")

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
