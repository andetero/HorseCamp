#!/usr/bin/env python3
"""
One-time Google Places staging geocoder for Horse Camp private camps.

This script reads a Google Maps Saved List CSV exported from Google Takeout,
looks up each camp by title using Google Places Text Search (New), and writes:

  data/private_camps_google_geocoded_STAGING.json
  data/private_camps_geocode_report.md

It intentionally does NOT overwrite data/private_camps.json.

Important:
- Google Maps Platform terms place restrictions on caching/storing and use of
  Places/Geocoding content, including latitude/longitude. Treat this as a
  staging/review tool unless you have confirmed your intended production use
  complies with your Google Maps Platform agreement.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests


TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

DEFAULT_IMAGE_COLORS = ["6D4C41", "BCAAA4"]
US_STATE_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
CANADA_PROVINCE_RE = re.compile(r",\s*([A-Z]{2})\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", re.I)

FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.googleMapsUri",
        "places.types",
    ]
)


def norm_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "unknown-private-camp"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def title_similarity(a: str, b: str) -> float:
    a_n = norm_name(a)
    b_n = norm_name(b)
    if not a_n or not b_n:
        return 0.0
    ratio = SequenceMatcher(None, a_n, b_n).ratio()

    a_tokens = set(re.findall(r"[a-z0-9]+", a_n))
    b_tokens = set(re.findall(r"[a-z0-9]+", b_n))
    token_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))

    containment = 0.0
    if a_n in b_n or b_n in a_n:
        containment = 0.25

    return min(1.0, (ratio * 0.55) + (token_score * 0.35) + containment)


def extract_state(formatted_address: str) -> str:
    if not formatted_address:
        return ""
    m = US_STATE_RE.search(formatted_address)
    if m:
        return m.group(1).upper()
    m = CANADA_PROVINCE_RE.search(formatted_address)
    if m:
        return m.group(1).upper()
    return ""


DECIMAL_COORD_RE = re.compile(r"(?<![\d.-])(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)(?![\d.-])")
DMS_PAIR_RE = re.compile(
    r"""(?ix)
    (\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([NS])
    \s+
    (\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([EW])
    """
)


def looks_like_coordinate_title(value: str) -> bool:
    value = value or ""
    return bool(DECIMAL_COORD_RE.search(value) or DMS_PAIR_RE.search(value) or ("°" in value and (("N" in value.upper()) or ("S" in value.upper()))))


def dms_to_decimal(degrees: str, minutes: str, seconds: str, hemisphere: str) -> float:
    decimal = float(degrees) + (float(minutes) / 60.0) + (float(seconds) / 3600.0)
    if hemisphere.upper() in {"S", "W"}:
        decimal *= -1
    return decimal


def parse_coordinates_from_text(*values: str) -> tuple[float, float] | None:
    combined = " ".join(v or "" for v in values)

    # Google Maps search URLs often include decimal coordinates:
    # https://www.google.com/maps/search/39.05803,-104.79338
    m = DECIMAL_COORD_RE.search(combined)
    if m:
        lat = float(m.group(1))
        lon = float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    # Saved list titles can also be DMS, e.g. 39°03'28.9"N 104°47'36.2"W
    m = DMS_PAIR_RE.search(combined)
    if m:
        lat = dms_to_decimal(m.group(1), m.group(2), m.group(3), m.group(4))
        lon = dms_to_decimal(m.group(5), m.group(6), m.group(7), m.group(8))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    return None


def infer_hookups(note: str) -> list[str]:
    text = (note or "").lower()
    hookups: list[str] = []
    if re.search(r"\b30\s*a\b|30amp|30 amp", text):
        hookups.append("30A")
    if re.search(r"\b50\s*a\b|50amp|50 amp", text):
        hookups.append("50A")
    if "water" in text:
        hookups.append("Water")
    if "sewer" in text:
        hookups.append("Sewer")
    return list(dict.fromkeys(hookups))


def infer_accommodations(title: str, note: str) -> list[str]:
    text = f"{title} {note}".lower()
    acc = ["Horse Camping"]
    if any(word in text for word in ["trail", "trails", "ride", "riding"]):
        acc.append("Trails")
    if any(word in text for word in ["corral", "corrals", "pen", "pens"]):
        acc.append("Corrals")
    if "stall" in text or "stalls" in text:
        acc.append("Stalls")
    if "paddock" in text or "paddocks" in text:
        acc.append("Paddocks")
    if "cabin" in text or "cabins" in text:
        acc.append("Cabins")
    if "primitive" in text:
        acc.append("Primitive Camping")
    if any(word in text for word in ["horse motel", "horse hotel", "overnight", "layover"]):
        acc.append("Layover")
    return list(dict.fromkeys(acc))


def make_description(note: str) -> str:
    base = (
        "Imported from the Horse Camps Google Maps saved list. "
        "Needs coordinate, amenity, reservation, and horse-access verification before publishing."
    )
    note = clean_text(note)
    if note:
        return f"{base} Saved-list note: {note}"
    return base


def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("camps"), list):
        return data["camps"]
    if isinstance(data, list):
        return data
    raise ValueError(f"{path} must be a JSON array or an object with a camps array")


def read_saved_list_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if any((v or "").strip() for v in r.values())]


def google_text_search(api_key: str, query: str, timeout: int = 30) -> list[dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "languageCode": "en",
        "regionCode": "US",
        "maxResultCount": 5,
    }
    resp = requests.post(TEXT_SEARCH_URL, headers=headers, json=body, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"Google Places Text Search failed {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()
    return payload.get("places", []) or []


def choose_best_place(title: str, places: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    best = None
    best_score = 0.0
    for place in places:
        display = ((place.get("displayName") or {}).get("text") or "").strip()
        address = place.get("formattedAddress") or ""
        score = title_similarity(title, display)

        # Small boost if search result address is in US/Canada-style format.
        if extract_state(address):
            score = min(1.0, score + 0.05)

        if score > best_score:
            best = place
            best_score = score
    return best, best_score


def make_private_camp_record(
    title: str,
    note: str,
    original_url: str,
    place: dict[str, Any] | None,
    score: float,
    existing_ids: set[str],
    min_score: float,
    direct_coordinates: tuple[float, float] | None = None,
    direct_coordinate_source: str = "",
) -> dict[str, Any]:
    base_id = "private-" + slugify(title)
    camp_id = base_id
    suffix = 2
    while camp_id in existing_ids:
        camp_id = f"{base_id}-{suffix}"
        suffix += 1
    existing_ids.add(camp_id)

    formatted_address = ""
    state = ""
    latitude = 0.0
    longitude = 0.0
    google_place_id = ""
    google_maps_uri = original_url
    matched_name = ""

    if direct_coordinates:
        latitude, longitude = direct_coordinates
        matched_name = title
        google_maps_uri = original_url
        score = 1.0
        geocode_provider = direct_coordinate_source or "Saved List Direct Coordinate"
    else:
        geocode_provider = "Google Places Text Search"

    is_confident = bool(place and score >= min_score)

    if place and is_confident and not direct_coordinates:
        matched_name = ((place.get("displayName") or {}).get("text") or "").strip()
        formatted_address = place.get("formattedAddress") or ""
        state = extract_state(formatted_address)
        loc = place.get("location") or {}
        latitude = float(loc.get("latitude") or 0.0)
        longitude = float(loc.get("longitude") or 0.0)
        google_place_id = place.get("id") or ""
        google_maps_uri = place.get("googleMapsUri") or original_url

    return {
        "id": camp_id,
        "name": title,
        "location": formatted_address,
        "state": state,
        "latitude": latitude,
        "longitude": longitude,
        "pricePerNight": 0.0,
        "horseFeePerNight": 0.0,
        "hookups": infer_hookups(note),
        "accommodations": infer_accommodations(title, note),
        "maxRigLength": 0,
        "stallCount": 0,
        "paddockCount": 0,
        "phone": "",
        "website": "",
        "description": make_description(note),
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
        "imageColors": DEFAULT_IMAGE_COLORS,
        "photoURLs": [],
        "source": "Private Camps",

        # Staging/review metadata. Remove these before final production if desired.
        "importSource": "Google Maps saved list: Horse Camps",
        "savedListURL": original_url,
        "googleMapsURL": google_maps_uri,
        "googlePlaceId": google_place_id,
        "googleMatchedName": matched_name,
        "geocodeProvider": geocode_provider,
        "geocodeConfidence": round(score, 3),
        "needsCoordinates": not (latitude and longitude),
        "needsVerification": True,
    }


def write_report(
    path: Path,
    *,
    existing_count: int,
    csv_count: int,
    imported_records: list[dict[str, Any]],
    skipped_existing: list[str],
    skipped_duplicate_csv: list[str],
    unresolved: list[dict[str, Any]],
    low_confidence: list[dict[str, Any]],
    min_score: float,
) -> None:
    resolved = [r for r in imported_records if r.get("latitude") and r.get("longitude")]
    states = {}
    for r in resolved:
        st = r.get("state") or "UNKNOWN"
        states[st] = states.get(st, 0) + 1

    lines = [
        "# Private Camps Google Places Geocode Report",
        "",
        "## Summary",
        "",
        f"- Existing private camps before import: **{existing_count}**",
        f"- Nonblank CSV rows: **{csv_count}**",
        f"- Imported staging records: **{len(imported_records)}**",
        f"- Imported with coordinates: **{len(resolved)}**",
        f"- Unresolved/no coordinates: **{len(unresolved)}**",
        f"- Low confidence threshold: **{min_score}**",
        f"- Skipped because already existed: **{len(skipped_existing)}**",
        f"- Skipped duplicate names inside CSV: **{len(skipped_duplicate_csv)}**",
        "",
        "## Important compliance note",
        "",
        "This workflow uses Google Places as a staging/review geocoder. Review your Google Maps Platform terms before using Google-derived latitude/longitude values as permanent production data, especially if displayed on non-Google maps.",
        "",
        "## Resolved by state",
        "",
    ]

    for st, count in sorted(states.items()):
        lines.append(f"- {st}: {count}")

    lines += [
        "",
        "## Skipped existing",
        "",
    ]
    lines += [f"- {name}" for name in skipped_existing] or ["- none"]

    lines += [
        "",
        "## Skipped duplicate CSV names",
        "",
    ]
    lines += [f"- {name}" for name in skipped_duplicate_csv] or ["- none"]

    lines += [
        "",
        "## Unresolved / no confident coordinate",
        "",
    ]
    if unresolved:
        for item in unresolved:
            lines.append(f"- **{item['title']}** — best match: {item.get('best_match') or 'none'}; score: {item.get('score', 0)}")
    else:
        lines.append("- none")

    lines += [
        "",
        "## Low confidence candidates",
        "",
    ]
    if low_confidence:
        for item in low_confidence[:100]:
            lines.append(f"- **{item['title']}** → {item.get('best_match') or 'none'}; score: {item.get('score', 0)}")
        if len(low_confidence) > 100:
            lines.append(f"- ...and {len(low_confidence) - 100} more")
    else:
        lines.append("- none")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="data/imports/horse_camps_google_maps.csv", help="Google Maps saved-list CSV")
    parser.add_argument("--existing", default="data/private_camps.json", help="Existing private_camps.json")
    parser.add_argument("--output", default="data/private_camps_google_geocoded_STAGING.json", help="Staging output JSON")
    parser.add_argument("--report", default="data/private_camps_geocode_report.md", help="Markdown report path")
    parser.add_argument("--api-key-env", default="GOOGLE_PLACES_KEY", help="Environment variable containing API key")
    parser.add_argument("--limit", type=int, default=0, help="Max new rows to process; 0 means all")
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between API calls")
    parser.add_argument("--min-score", type=float, default=0.55, help="Minimum confidence score to accept coordinates")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        print(f"ERROR: missing API key env var {args.api_key_env}", file=sys.stderr)
        return 2

    csv_path = Path(args.csv)
    existing_path = Path(args.existing)
    output_path = Path(args.output)
    report_path = Path(args.report)

    existing = load_json_array(existing_path)
    rows = read_saved_list_csv(csv_path)

    existing_names = {norm_name(c.get("name", "")) for c in existing}
    existing_ids = {c.get("id", "") for c in existing}

    merged = list(existing)
    imported: list[dict[str, Any]] = []
    skipped_existing: list[str] = []
    skipped_duplicate_csv: list[str] = []
    unresolved: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    seen_import_names: set[str] = set()

    processed = 0

    for row in rows:
        title = clean_text(row.get("Title") or "")
        if not title:
            continue

        n = norm_name(title)
        if n in existing_names:
            skipped_existing.append(title)
            continue
        if n in seen_import_names:
            skipped_duplicate_csv.append(title)
            continue

        if args.limit and processed >= args.limit:
            break

        note = clean_text(row.get("Note") or "")
        url = clean_text(row.get("URL") or "")

        direct_coordinates = parse_coordinates_from_text(title, url)
        record_title = title
        if direct_coordinates and looks_like_coordinate_title(title) and note:
            # Use the user's note as the display name when Google saved a raw coordinate as the title.
            record_title = clean_text(note)

        query_variants = [
            title,
            f"{title} horse camp",
            f"{title} equestrian campground",
        ]

        best_place = None
        best_score = 0.0
        best_match = ""
        api_errors: list[str] = []

        if not direct_coordinates:
            for query in query_variants:
                try:
                    places = google_text_search(api_key, query)
                except RuntimeError as exc:
                    err = str(exc)
                    api_errors.append(err)
                    # Bad text queries should not kill the entire batch. Key/quota/auth errors should.
                    if any(token in err for token in ["API_KEY", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "429", "403"]):
                        raise
                    print(f"WARN: skipping query for {title!r}: {err[:220]}")
                    continue

                candidate, score = choose_best_place(title, places)
                candidate_name = ""
                if candidate:
                    candidate_name = ((candidate.get("displayName") or {}).get("text") or "").strip()
                if score > best_score:
                    best_place = candidate
                    best_score = score
                    best_match = candidate_name
                if best_score >= 0.85:
                    break
                time.sleep(args.sleep)
        else:
            best_match = record_title
            best_score = 1.0

        if best_score < args.min_score:
            low_confidence.append({"title": record_title, "best_match": best_match or "; ".join(api_errors)[:180], "score": round(best_score, 3)})

        record = make_private_camp_record(
            title=record_title,
            note=note,
            original_url=url,
            place=best_place,
            score=best_score,
            existing_ids=existing_ids,
            min_score=args.min_score,
            direct_coordinates=direct_coordinates,
            direct_coordinate_source="Saved List URL/Title Coordinates" if direct_coordinates else "",
        )

        if not (record.get("latitude") and record.get("longitude")):
            unresolved.append({"title": title, "best_match": best_match, "score": round(best_score, 3)})

        merged.append(record)
        imported.append(record)
        seen_import_names.add(n)
        processed += 1
        print(f"[{processed}] {title} -> {best_match or 'NO MATCH'} score={best_score:.3f} coords={record['latitude']},{record['longitude']}")
        time.sleep(args.sleep)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_report(
        report_path,
        existing_count=len(existing),
        csv_count=len(rows),
        imported_records=imported,
        skipped_existing=skipped_existing,
        skipped_duplicate_csv=skipped_duplicate_csv,
        unresolved=unresolved,
        low_confidence=low_confidence,
        min_score=args.min_score,
    )

    print("")
    print(f"Wrote staging JSON: {output_path}")
    print(f"Wrote report: {report_path}")
    print(f"Existing: {len(existing)}")
    print(f"CSV nonblank rows: {len(rows)}")
    print(f"Imported staging records: {len(imported)}")
    print(f"Unresolved: {len(unresolved)}")
    print("")
    print("Review the staging JSON/report before replacing data/private_camps.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
