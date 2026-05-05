#!/usr/bin/env python3
"""
Approve a HorseCamp problem-report issue as a data fix.

Supported approved report types:
- "No horse camping" adds the reported campId to data/exclusions.json.
- "Wrong location" adds/updates latitude and longitude in data/overrides.json.

The GitHub Action opens a PR for final review before the data fix is merged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

EXCLUSIONS_PATH = Path("data/exclusions.json")
OVERRIDES_PATH = Path("data/overrides.json")
REPORT_LOG_DIR = Path("data/problem_reports")

HIDDEN_JSON_RE = re.compile(
    r"<!--\s*HORSECAMP_PROBLEM_REPORT_JSON\s*(?P<json>\{.*?\})\s*HORSECAMP_PROBLEM_REPORT_JSON\s*-->",
    re.DOTALL | re.IGNORECASE,
)
FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(?P<json>\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def category_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().replace("-", " ").replace("_", " ").strip())


def compact_json(value: Any) -> str:
    """Pretty JSON with simple scalar arrays kept on one line for easier GitHub review."""
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


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_exclusions(path: Path = EXCLUSIONS_PATH) -> list[str]:
    data = load_json(path, [])
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    exclusions: list[str] = []
    for item in data:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path} contains an invalid camp id entry: {item!r}")
        exclusions.append(item.strip())
    return exclusions


def write_exclusions(exclusions: list[str], path: Path = EXCLUSIONS_PATH) -> None:
    unique_sorted = sorted(dict.fromkeys(exclusions))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep exclusions one item per line so GitHub diffs/reviews are readable.
    path.write_text(pretty_json(unique_sorted), encoding="utf-8")


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, dict[str, Any]]:
    data = load_json(path, {})
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    for camp_id, patch in data.items():
        if not isinstance(camp_id, str) or not camp_id.strip():
            raise ValueError(f"{path} contains an invalid camp id key: {camp_id!r}")
        if not isinstance(patch, dict):
            raise ValueError(f"Override for {camp_id!r} in {path} must be a JSON object")
    return data


def write_overrides(overrides: dict[str, dict[str, Any]], path: Path = OVERRIDES_PATH) -> None:
    sorted_overrides = {key: overrides[key] for key in sorted(overrides)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(sorted_overrides), encoding="utf-8")


def decode_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Problem report JSON is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Problem report JSON must be an object")
    return parsed


def extract_first_balanced_json(text: str) -> dict[str, Any] | None:
    """Fallback for issue bodies where GitHub details/fences were edited away."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        return decode_json(candidate)
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def extract_markdown_table_fields(body: str) -> dict[str, str]:
    """Extract values from the visible issue table.

    Maintainers sometimes correct the visible GitHub issue table. Prefer these
    table values over the hidden/raw JSON so approved data fixes use the reviewed
    values shown in the issue.
    """
    aliases = {
        "problemcategory": "category",
        "camptype": "category",
        "campname": "campName",
        "campid": "campId",
        "coordinates": "coordinates",
        "state": "state",
        "location": "location",
        "source": "source",
        "website": "website",
        "phone": "phone",
        "usernotes": "notes",
    }
    fields: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if set(cells[0]) <= {"-", ":", " "}:
            continue
        key = aliases.get(normalize_field_name(cells[0]))
        if key:
            value = re.sub(r"<[^>]+>", "", cells[1]).strip()
            if value and value != "—":
                fields[key] = value
    return fields


def extract_problem_report(body: str) -> dict[str, Any]:
    payload: dict[str, Any] | None = None
    for regex in (HIDDEN_JSON_RE, FENCED_JSON_RE):
        match = regex.search(body)
        if match:
            payload = decode_json(match.group("json"))
            break

    if payload is None:
        payload = extract_first_balanced_json(body)

    if payload is None:
        payload = {}

    # Visible table edits are the maintainer-reviewed values, so let them win.
    payload.update(extract_markdown_table_fields(body))

    if not payload:
        raise ValueError("Could not find problem report JSON or table fields in issue body")
    return payload


def get_camp_id(payload: dict[str, Any]) -> str:
    camp_id = clean_text(payload.get("campId") or payload.get("camp_id") or payload.get("id"))
    if not camp_id:
        raise ValueError("Problem report is missing campId")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", camp_id):
        raise ValueError(f"Problem report campId looks invalid: {camp_id!r}")
    return camp_id


def get_category(payload: dict[str, Any]) -> str:
    category = clean_text(payload.get("category") or payload.get("problemCategory") or payload.get("problem_category"))
    if not category:
        raise ValueError("Problem report is missing category/problemCategory")
    return category


def parse_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    return parsed


def parse_coordinates(payload: dict[str, Any]) -> tuple[float, float]:
    # Prefer the visible/maintainer-edited Coordinates table value when present.
    coordinates = clean_text(payload.get("coordinates") or payload.get("coordinate") or payload.get("coords"))
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", coordinates)
    if match:
        lat_value, lon_value = match.group(1), match.group(2)
    else:
        lat_value = payload.get("latitude") or payload.get("lat")
        lon_value = payload.get("longitude") or payload.get("lon") or payload.get("lng")

    if (lat_value in (None, "")) or (lon_value in (None, "")):
        raise ValueError("Wrong location reports must include latitude/longitude or a 'Coordinates' value")

    latitude = parse_float(lat_value, field="latitude")
    longitude = parse_float(lon_value, field="longitude")

    if not (-90 <= latitude <= 90):
        raise ValueError("latitude must be between -90 and 90")
    if not (-180 <= longitude <= 180):
        raise ValueError("longitude must be between -180 and 180")
    if latitude == 0 and longitude == 0:
        raise ValueError("latitude/longitude cannot both be zero")

    return latitude, longitude


def write_problem_report_log(issue_number: int, payload: dict[str, Any], action: str, camp_id: str, result: dict[str, Any]) -> Path:
    REPORT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_LOG_DIR / f"approved_issue_{issue_number}.json"
    path.write_text(
        compact_json(
            {
                "issueNumber": issue_number,
                "action": action,
                "campId": camp_id,
                "result": result,
                "originalPayload": payload,
            }
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


def approve_no_horse_camping(issue_number: int, payload: dict[str, Any], camp_id: str, camp_name: str, category: str, notes: str) -> tuple[str, str, str]:
    exclusions = load_exclusions()
    already_excluded = camp_id in exclusions
    if not already_excluded:
        exclusions.append(camp_id)
        write_exclusions(exclusions)

    log_path = write_problem_report_log(
        issue_number,
        payload,
        "exclude_generated_listing",
        camp_id,
        {"alreadyExcluded": already_excluded, "target": str(EXCLUSIONS_PATH)},
    )

    pr_title = f"Exclude non-horse-camping listing: {camp_name}"
    pr_body = (
        f"Closes #{issue_number}\n\n"
        f"Target: `{EXCLUSIONS_PATH}`\n\n"
        f"Approved problem report category: `{category}`\n\n"
        f"This PR adds the generated listing ID `{camp_id}` to `data/exclusions.json`. "
        "The next Seed Camp Data run will remove this listing from `camps.json`.\n\n"
        "Important: editing this PR description/body does not change app data. "
        "To change what gets merged into the app, edit `data/exclusions.json` in the Files changed tab.\n\n"
    )
    if notes:
        pr_body += f"User notes: {notes}\n\n"
    pr_body += (
        "Problem report payload for review:\n\n"
        "```json\n"
        f"{compact_json(payload)}\n"
        "```\n\n"
        "Review the JSON diff below, then merge when ready. Merging this PR will automatically close the linked issue."
    )

    if already_excluded:
        summary = f"{camp_id} was already present in {EXCLUSIONS_PATH}; wrote review log: {log_path}"
    else:
        summary = f"Added {camp_id} to {EXCLUSIONS_PATH}; wrote review log: {log_path}"
    return pr_title, pr_body, summary


def approve_wrong_location(issue_number: int, payload: dict[str, Any], camp_id: str, camp_name: str, category: str, notes: str) -> tuple[str, str, str]:
    latitude, longitude = parse_coordinates(payload)
    overrides = load_overrides()
    existing_patch = dict(overrides.get(camp_id, {}))
    previous_latitude = existing_patch.get("latitude")
    previous_longitude = existing_patch.get("longitude")

    existing_patch["latitude"] = latitude
    existing_patch["longitude"] = longitude
    overrides[camp_id] = existing_patch
    write_overrides(overrides)

    log_path = write_problem_report_log(
        issue_number,
        payload,
        "coordinate_override",
        camp_id,
        {
            "target": str(OVERRIDES_PATH),
            "latitude": latitude,
            "longitude": longitude,
            "previousLatitude": previous_latitude,
            "previousLongitude": previous_longitude,
        },
    )

    pr_title = f"Correct listing location: {camp_name}"
    pr_body = (
        f"Closes #{issue_number}\n\n"
        f"Target: `{OVERRIDES_PATH}`\n\n"
        f"Approved problem report category: `{category}`\n\n"
        f"This PR adds or updates a coordinate override for generated listing ID `{camp_id}`. "
        "The next Seed Camp Data run will apply these coordinates to `camps.json`.\n\n"
        f"Corrected coordinates: `{latitude}, {longitude}`\n\n"
        "Important: editing this PR description/body does not change app data. "
        "To change what gets merged into the app, edit `data/overrides.json` in the Files changed tab.\n\n"
    )
    if notes:
        pr_body += f"User notes: {notes}\n\n"
    pr_body += (
        "Problem report payload for review:\n\n"
        "```json\n"
        f"{compact_json(payload)}\n"
        "```\n\n"
        "Review the JSON diff below, then merge when ready. Merging this PR will automatically close the linked issue."
    )

    summary = f"Set coordinate override for {camp_id} to {latitude}, {longitude} in {OVERRIDES_PATH}; wrote review log: {log_path}"
    return pr_title, pr_body, summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-body", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    body = Path(args.issue_body).read_text(encoding="utf-8")
    payload = extract_problem_report(body)
    camp_id = get_camp_id(payload)
    category = get_category(payload)
    camp_name = clean_text(payload.get("campName") or payload.get("camp_name") or payload.get("name")) or camp_id
    notes = clean_text(payload.get("notes") or payload.get("userNotes") or payload.get("user_notes"))

    normalized_category = category_key(category)
    if normalized_category == "no horse camping":
        pr_title, pr_body, summary = approve_no_horse_camping(args.issue_number, payload, camp_id, camp_name, category, notes)
    elif normalized_category == "wrong location":
        pr_title, pr_body, summary = approve_wrong_location(args.issue_number, payload, camp_id, camp_name, category, notes)
    else:
        raise ValueError(
            "This automation supports problem reports with category 'No horse camping' or 'Wrong location'. "
            f"Found category: {category or '(blank)'}"
        )

    print(summary)
    write_github_outputs(args.github_output, pr_title=pr_title, pr_body=pr_body, summary=summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
