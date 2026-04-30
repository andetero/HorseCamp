#!/usr/bin/env python3
"""
Approve a HorseCamp user submission from a GitHub Issue body and update JSON data files.

Expected issue body contains this hidden block, created by the Cloudflare Worker:

<!-- HORSECAMP_SUBMISSION_JSON
{ ... }
HORSECAMP_SUBMISSION_JSON -->

This script intentionally does not approve anything directly. It edits JSON files on the
current Git branch. The GitHub Action then opens a PR for final review/merge.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

PRIVATE_CAMPS_PATH = Path("data/private_camps.json")
LAYOVERS_PATH = Path("data/layovers.json")
CAMPS_PATH = Path("camps.json")
SUBMISSION_LOG_DIR = Path("data/submissions")

DEFAULT_IMAGE_COLORS = ["6D4C41", "BCAAA4"]

PRIVATE_DEFAULT_DESCRIPTION = (
    "Private camp. Details may change; please confirm horse access, availability, "
    "amenities, fees, and rules before travel."
)

LAYOVER_DEFAULT_DESCRIPTION = (
    "Horse layover. Details may change; please confirm overnight horse access, "
    "availability, amenities, fees, and rules before travel."
)

JSON_BLOCK_RE = re.compile(
    r"<!--\s*HORSECAMP_SUBMISSION_JSON\s*(?P<json>\{.*?\})\s*HORSECAMP_SUBMISSION_JSON\s*-->",
    re.DOTALL,
)
FENCED_JSON_RE = re.compile(r"```json\s*(?P<json>\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

VALID_HOOKUPS = {"30A", "50A", "Water", "Sewer", "Generator", "None"}
VALID_ACCOMMODATIONS = {
    "Horse Camping",
    "Layover",
    "Stalls",
    "Corrals",
    "Paddocks",
    "Trails",
    "Cabins",
    "Primitive Camping",
    "Group Camping",
}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "unknown"


def normalize_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    return parsed


def parse_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_money(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def format_phone(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    digits = re.sub(r"\D+", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]}-{digits[4:7]}-{digits[7:11]}"
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    return raw


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "available"}


def coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,;|]", str(value))
    cleaned = [clean_text(item) for item in items if clean_text(item)]
    return list(dict.fromkeys(cleaned))


def normalize_hookups(value: Any) -> list[str]:
    aliases = {
        "30": "30A",
        "30a": "30A",
        "30 amp": "30A",
        "30 amps": "30A",
        "50": "50A",
        "50a": "50A",
        "50 amp": "50A",
        "50 amps": "50A",
        "water": "Water",
        "sewer": "Sewer",
        "generator": "Generator",
        "none": "None",
    }
    out: list[str] = []
    for item in coerce_string_list(value):
        normalized = aliases.get(item.lower(), item)
        if normalized in VALID_HOOKUPS and normalized not in out:
            out.append(normalized)
    if "None" in out and len(out) > 1:
        out = [item for item in out if item != "None"]
    return out


def normalize_accommodations(value: Any, *, kind: str) -> list[str]:
    out: list[str] = []
    for item in coerce_string_list(value):
        match = next((valid for valid in VALID_ACCOMMODATIONS if valid.lower() == item.lower()), item)
        if match in VALID_ACCOMMODATIONS and match not in out:
            out.append(match)

    if kind == "layover":
        if "Layover" not in out:
            out.insert(0, "Layover")
        if "Horse Camping" not in out:
            out.append("Horse Camping")
    else:
        if "Horse Camping" not in out:
            out.append("Horse Camping")
    return out


def validate_lat_lon(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90):
        raise ValueError("latitude must be between -90 and 90")
    if not (-180 <= lon <= 180):
        raise ValueError("longitude must be between -180 and 180")
    if lat == 0 and lon == 0:
        raise ValueError("latitude/longitude cannot both be zero")


def load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("camps"), list):
        return data["camps"]
    raise ValueError(f"{path} must contain a JSON array or an object with a camps array")


def write_json_array(path: Path, data: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def extract_submission(body: str) -> dict[str, Any]:
    match = JSON_BLOCK_RE.search(body)
    if not match:
        match = FENCED_JSON_RE.search(body)
    if not match:
        raise ValueError("Could not find HORSECAMP_SUBMISSION_JSON block in issue body")
    try:
        payload = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Submission JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Submission JSON must be an object")
    return payload


def normalize_kind(value: Any) -> str:
    raw = clean_text(value).lower().replace("-", "_").replace(" ", "_")
    if raw in {"layover", "horse_layover", "horse_layovers"}:
        return "layover"
    if raw in {"private_camp", "private", "camp", "horse_camp", "horse_camping"}:
        return "private_camp"
    raise ValueError("type must be layover or private_camp")


def make_id(prefix: str, name: str, state: str, existing_ids: set[str]) -> str:
    base = f"{prefix}{slugify(name)}"
    if state:
        base = f"{base}-{state.lower()}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    existing_ids.add(candidate)
    return candidate


def haversine_meters(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371000.0
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_duplicate(record: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    name_key = normalize_for_match(record.get("name", ""))
    lat = float(record.get("latitude") or 0.0)
    lon = float(record.get("longitude") or 0.0)
    for existing in candidates:
        existing_name = clean_text(existing.get("name"))
        existing_name_key = normalize_for_match(existing_name)
        if name_key and existing_name_key and name_key == existing_name_key:
            return f"same name as existing record: {existing_name}"
        ex_lat = float(existing.get("latitude") or 0.0)
        ex_lon = float(existing.get("longitude") or 0.0)
        if -90 <= ex_lat <= 90 and -180 <= ex_lon <= 180 and ex_lat and ex_lon:
            if haversine_meters(lat, lon, ex_lat, ex_lon) < 100:
                return f"within 100m of existing record: {existing_name}"
    return ""


def build_record(payload: dict[str, Any], issue_number: int, existing_ids: set[str]) -> tuple[str, Path, dict[str, Any]]:
    kind = normalize_kind(payload.get("type"))
    name = clean_text(payload.get("name"))
    if len(name) < 3:
        raise ValueError("name is required")

    state = clean_text(payload.get("state")).upper()
    if not re.fullmatch(r"[A-Z]{2}", state):
        raise ValueError("state must be a two-letter abbreviation")

    lat = parse_float(payload.get("latitude"), field="latitude")
    lon = parse_float(payload.get("longitude"), field="longitude")
    validate_lat_lon(lat, lon)

    location = clean_text(payload.get("location") or payload.get("address"))
    if not location:
        location = f"{state} ({lat:.6f}, {lon:.6f})"

    website = clean_text(payload.get("website"))
    if website and not re.match(r"(?i)^https?://", website):
        website = "https://" + website

    description = clean_text(payload.get("description"))
    if not description:
        description = LAYOVER_DEFAULT_DESCRIPTION if kind == "layover" else PRIVATE_DEFAULT_DESCRIPTION

    if kind == "layover":
        source = "Layover"
        target_path = LAYOVERS_PATH
        id_prefix = "layover-submitted-"
        accommodations = normalize_accommodations(payload.get("accommodations"), kind="layover")
    else:
        source = "Private Camps"
        target_path = PRIVATE_CAMPS_PATH
        id_prefix = "private-submitted-"
        accommodations = normalize_accommodations(payload.get("accommodations"), kind="private_camp")

    record = {
        "id": make_id(id_prefix, name, state, existing_ids),
        "name": name,
        "location": location,
        "state": state,
        "latitude": lat,
        "longitude": lon,
        "pricePerNight": parse_money(payload.get("pricePerNight")),
        "horseFeePerNight": parse_money(payload.get("horseFeePerNight")),
        "hookups": normalize_hookups(payload.get("hookups")),
        "accommodations": accommodations,
        "maxRigLength": parse_int(payload.get("maxRigLength")),
        "stallCount": parse_int(payload.get("stallCount")),
        "paddockCount": parse_int(payload.get("paddockCount")),
        "phone": format_phone(payload.get("phone")),
        "website": website,
        "description": description,
        "isVerified": False,
        "seasonStart": parse_int(payload.get("seasonStart"), 1),
        "seasonEnd": parse_int(payload.get("seasonEnd"), 12),
        "hasWashRack": bool_value(payload.get("hasWashRack")),
        "hasDumpStation": bool_value(payload.get("hasDumpStation")),
        "hasWifi": bool_value(payload.get("hasWifi")),
        "hasBathhouse": bool_value(payload.get("hasBathhouse")),
        "pullThroughAvailable": bool_value(payload.get("pullThroughAvailable")),
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": DEFAULT_IMAGE_COLORS,
        "photoURLs": [],
        "source": source,
        "submittedIssueNumber": issue_number,
    }
    return kind, target_path, record


def append_record(target_path: Path, record: dict[str, Any]) -> None:
    records = load_json_array(target_path)
    records.append(record)
    records.sort(key=lambda item: ((item.get("state") or "ZZ"), (item.get("name") or "").lower()))
    write_json_array(target_path, records)


def write_submission_log(issue_number: int, kind: str, record: dict[str, Any], original_payload: dict[str, Any]) -> Path:
    SUBMISSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = SUBMISSION_LOG_DIR / f"approved_issue_{issue_number}.json"
    path.write_text(
        json.dumps(
            {
                "issueNumber": issue_number,
                "type": kind,
                "recordId": record["id"],
                "name": record["name"],
                "targetSource": record["source"],
                "record": record,
                "originalPayload": original_payload,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_github_outputs(output_path: str | None, *, pr_title: str, pr_body: str, summary: str) -> None:
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"pr_title={pr_title}\n")
        f.write("pr_body<<EOF\n")
        f.write(pr_body)
        f.write("\nEOF\n")
        f.write("summary<<EOF\n")
        f.write(summary)
        f.write("\nEOF\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-body", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    body = Path(args.issue_body).read_text(encoding="utf-8")
    payload = extract_submission(body)

    all_existing = []
    target_existing_private = load_json_array(PRIVATE_CAMPS_PATH)
    target_existing_layovers = load_json_array(LAYOVERS_PATH)
    all_existing.extend(target_existing_private)
    all_existing.extend(target_existing_layovers)
    if CAMPS_PATH.exists():
        all_existing.extend(load_json_array(CAMPS_PATH))

    existing_ids = {str(item.get("id")) for item in all_existing if item.get("id")}
    kind, target_path, record = build_record(payload, args.issue_number, existing_ids)

    duplicate = find_duplicate(record, all_existing)
    if duplicate:
        raise ValueError(f"Submission appears to be a duplicate: {duplicate}")

    append_record(target_path, record)
    log_path = write_submission_log(args.issue_number, kind, record, payload)

    human_kind = "layover" if kind == "layover" else "private camp"
    pr_title = f"Add {human_kind}: {record['name']}"
    pr_body = (
        f"Adds approved HorseCamp user submission from issue #{args.issue_number}.\n\n"
        f"Closes #{args.issue_number}\n\n"
        f"- Type: {human_kind}\n"
        f"- Name: {record['name']}\n"
        f"- State: {record['state']}\n"
        f"- Coordinates: {record['latitude']}, {record['longitude']}\n"
        f"- Target: `{target_path}`\n\n"
        "Please review the generated JSON diff before merging. "
        "Merging this PR will automatically close the linked submission issue."
    )
    summary = f"Prepared {human_kind} submission '{record['name']}' for {target_path}; log: {log_path}"
    print(summary)
    write_github_outputs(args.github_output, pr_title=pr_title, pr_body=pr_body, summary=summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
