#!/usr/bin/env python3
"""
Import authorized HorseMotel.com partner listings into HorseCamp.

HorseMotel.com remains the source of truth. This script normalizes an approved
partner export into data/horsemotel_listings.json so the existing HorseCamp
nightly seed can merge it into camps.json.

Supported first-phase inputs:
  - CSV file exported/provided by HorseMotel.com
  - JSON file/export URL provided by HorseMotel.com
  - CSV export URL provided by HorseMotel.com

This intentionally avoids Supabase and Cloudflare Workers for phase 1.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "data" / "imports" / "horsemotel_listings.csv"
DEFAULT_JSON = REPO_ROOT / "data" / "horsemotel_listings.json"
DEFAULT_REPORT = REPO_ROOT / "data" / "imports" / "horsemotel_import_report.md"
PARTNER_NAME = "HorseMotel.com"
ATTRIBUTION = "Listing provided by HorseMotel.com"

FIELD_ALIASES = {
    "name": ["name", "listing_name", "business_name", "title", "facility", "facility_name"],
    "location": ["location", "address", "street_address", "full_address"],
    "city": ["city", "town"],
    "state": ["state", "state_code", "province", "region"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "lng", "lon", "long"],
    "phone": ["phone", "phone_number", "telephone"],
    "website": ["website", "url", "listing_url", "link", "horse_motel_url"],
    "description": ["description", "notes", "details", "summary"],
    "email": ["email", "email_address"],
    "pricePerNight": ["price_per_night", "price", "nightly_rate"],
    "horseFeePerNight": ["horse_fee_per_night", "horse_fee"],
    "stallCount": ["stall_count", "stalls"],
    "paddockCount": ["paddock_count", "paddocks", "corrals"],
    "maxRigLength": ["max_rig_length", "rig_length", "max_length"],
    "photoURLs": ["photo_urls", "photos", "image_urls", "images"],
    "accommodations": ["accommodations", "amenities", "features"],
    "sourceUrl": ["source_url", "source", "horse_motel_listing_url"],
}

BOOL_FIELDS = {
    "hasWashRack": ["has_wash_rack", "wash_rack"],
    "hasDumpStation": ["has_dump_station", "dump_station"],
    "hasWifi": ["has_wifi", "wifi"],
    "hasBathhouse": ["has_bathhouse", "bathhouse", "bathrooms", "showers"],
    "pullThroughAvailable": ["pull_through_available", "pull_through", "pullthrough"],
}


def compact_json_dump(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    rendered = _compact_selected_array_fields(rendered, {"hookups", "accommodations", "imageColors", "photoURLs"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered + "\n", encoding="utf-8")


def _compact_selected_array_fields(json_text: str, field_names: set[str]) -> str:
    field_pattern = "|".join(re.escape(name) for name in field_names)
    pattern = re.compile(
        rf'(?P<indent>^[ \t]*)"(?P<field>{field_pattern})": \[\n'
        rf'(?P<body>(?:^[ \t]+.*\n)*?)'
        rf'(?P=indent)\]',
        flags=re.MULTILINE,
    )

    def repl(match: re.Match[str]) -> str:
        array_text = "[\n" + match.group("body") + match.group("indent") + "]"
        values = json.loads(array_text)
        return f'{match.group("indent")}"{match.group("field")}": {json.dumps(values, ensure_ascii=False)}'

    return pattern.sub(repl, json_text)


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def first_value(row: Dict[str, Any], aliases: Iterable[str]) -> str:
    normalized = {norm_key(k): v for k, v in row.items()}
    for alias in aliases:
        value = normalized.get(norm_key(alias))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_float(value: str, default: float = 0.0) -> float:
    if not value:
        return default
    cleaned = re.sub(r"[^0-9.\-]", "", value)
    try:
        return float(cleaned) if cleaned else default
    except ValueError:
        return default


def parse_int(value: str, default: int = 0) -> int:
    if not value:
        return default
    cleaned = re.sub(r"[^0-9\-]", "", value)
    try:
        return int(cleaned) if cleaned else default
    except ValueError:
        return default


def parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "available", "included", "x"}


def parse_list(value: str) -> list[str]:
    if not value:
        return []
    if value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[|;,]", value)
    return [p.strip() for p in parts if p.strip()]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "listing"


def build_id(name: str, state: str, location: str, source_url: str = "") -> str:
    stable = source_url or f"{name}|{state}|{location}"
    digest = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:8]
    return f"horsemotel-{slugify(name)}-{state.lower()}-{digest}"


def normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = first_value(row, FIELD_ALIASES["name"])
    if not name:
        return None

    city = first_value(row, FIELD_ALIASES["city"])
    state = first_value(row, FIELD_ALIASES["state"]).upper()
    location = first_value(row, FIELD_ALIASES["location"])
    if not location:
        location = ", ".join(v for v in [city, state] if v)

    source_url = first_value(row, FIELD_ALIASES["sourceUrl"])
    website = first_value(row, FIELD_ALIASES["website"]) or source_url
    lat = parse_float(first_value(row, FIELD_ALIASES["latitude"]), default=0.0)
    lng = parse_float(first_value(row, FIELD_ALIASES["longitude"]), default=0.0)

    accommodations = parse_list(first_value(row, FIELD_ALIASES["accommodations"]))
    for required in ["HorseMotel.com", "Layover", "Horse Camping"]:
        if required not in accommodations:
            accommodations.append(required)

    photo_urls = parse_list(first_value(row, FIELD_ALIASES["photoURLs"]))
    description = first_value(row, FIELD_ALIASES["description"]) or "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival."

    listing = {
        "id": build_id(name, state, location, source_url),
        "name": name,
        "location": location,
        "city": city,
        "state": state,
        "latitude": lat,
        "longitude": lng,
        "pricePerNight": parse_float(first_value(row, FIELD_ALIASES["pricePerNight"]), 0.0),
        "horseFeePerNight": parse_float(first_value(row, FIELD_ALIASES["horseFeePerNight"]), 0.0),
        "hookups": [],
        "accommodations": accommodations,
        "maxRigLength": parse_int(first_value(row, FIELD_ALIASES["maxRigLength"]), 0),
        "stallCount": parse_int(first_value(row, FIELD_ALIASES["stallCount"]), 0),
        "paddockCount": parse_int(first_value(row, FIELD_ALIASES["paddockCount"]), 0),
        "phone": first_value(row, FIELD_ALIASES["phone"]),
        "email": first_value(row, FIELD_ALIASES["email"]),
        "website": website,
        "sourceUrl": source_url or website,
        "description": description,
        "isVerified": True,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": False,
        "hasDumpStation": False,
        "hasWifi": False,
        "hasBathhouse": False,
        "pullThroughAvailable": False,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": ["6D4C41", "BCAAA4"],
        "photoURLs": photo_urls,
        "source": PARTNER_NAME,
        "sourceDetail": PARTNER_NAME,
        "category": PARTNER_NAME,
        "partner": PARTNER_NAME,
        "attribution": ATTRIBUTION,
        "lastSynced": datetime.now(timezone.utc).date().isoformat(),
    }

    for output_field, aliases in BOOL_FIELDS.items():
        listing[output_field] = parse_bool(first_value(row, aliases), False)

    return listing


def read_csv(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def read_json(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("listings") or data.get("data") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array or an object with listings/data/items")
    return [item for item in data if isinstance(item, dict)]


def read_url(url: str) -> list[Dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "HorseCamp authorized HorseMotel.com sync"})
    with urlopen(request, timeout=30) as response:
        content_type = response.headers.get("content-type", "").lower()
        body = response.read().decode("utf-8-sig")
    if "json" in content_type or url.lower().endswith(".json"):
        data = json.loads(body)
        if isinstance(data, dict):
            data = data.get("listings") or data.get("data") or data.get("items") or []
        if not isinstance(data, list):
            raise ValueError("Source URL JSON must contain an array or listings/data/items")
        return [item for item in data if isinstance(item, dict)]
    return list(csv.DictReader(body.splitlines()))


def merge_unique(listings: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    by_id: dict[str, Dict[str, Any]] = {}
    skipped_missing_geo = 0
    for row in listings:
        normalized = normalize_row(row)
        if not normalized:
            continue
        # The app map requires coordinates. Keep a report trail, but do not ship unmappable rows.
        if not normalized.get("latitude") or not normalized.get("longitude"):
            skipped_missing_geo += 1
            continue
        by_id[normalized["id"]] = normalized
    if skipped_missing_geo:
        print(f"Skipped {skipped_missing_geo} HorseMotel.com rows missing latitude/longitude")
    return sorted(by_id.values(), key=lambda item: (item.get("state", ""), item.get("name", "")))


def write_report(path: Path, count: int, inputs: list[str]) -> None:
    lines = [
        "# HorseMotel.com Import Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Listings written: {count}",
        "",
        "## Inputs",
    ]
    lines.extend(f"- {item}" for item in inputs if item)
    lines.extend([
        "",
        "## Notes",
        f"- Partner/source: {PARTNER_NAME}",
        f"- Attribution: {ATTRIBUTION}",
        "- HorseMotel.com remains the source of truth.",
        "- Rows without coordinates are skipped until latitude/longitude are provided.",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import authorized HorseMotel.com listings into HorseCamp JSON")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV export/input path")
    parser.add_argument("--json", type=Path, help="Optional JSON export/input path")
    parser.add_argument("--source-url", help="Optional authorized CSV/JSON export URL")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON, help="Output JSON path")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Import report path")
    parser.add_argument("--allow-empty", action="store_true", help="Write [] when no input rows are available")
    args = parser.parse_args()

    rows: list[Dict[str, Any]] = []
    inputs: list[str] = []

    csv_rows = read_csv(args.csv)
    if csv_rows:
        rows.extend(csv_rows)
        inputs.append(str(args.csv.relative_to(REPO_ROOT) if args.csv.is_relative_to(REPO_ROOT) else args.csv))

    if args.json:
        json_rows = read_json(args.json)
        rows.extend(json_rows)
        inputs.append(str(args.json.relative_to(REPO_ROOT) if args.json.is_relative_to(REPO_ROOT) else args.json))

    if args.source_url:
        url_rows = read_url(args.source_url)
        rows.extend(url_rows)
        inputs.append(args.source_url)

    listings = merge_unique(rows)
    if not listings and not args.allow_empty:
        print("No HorseMotel.com listings found. Provide CSV/JSON input or pass --allow-empty.", file=sys.stderr)
        return 2

    compact_json_dump(args.output, listings)
    write_report(args.report, len(listings), inputs or ["No input rows; initialized empty partner JSON"])
    print(f"Wrote {len(listings)} listings to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
