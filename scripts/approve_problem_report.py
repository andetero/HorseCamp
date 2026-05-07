#!/usr/bin/env python3
"""
Approve a HorseCamp problem-report issue as a data fix.

Supported approved report types:
- Wrong location
- Closed / no longer available
- No horse camping
- Duplicate listing
- Missing phone
- Missing website
- Missing description/details
- Missing accommodations
- Incorrect amenities/accommodations
- Bad phone / website
- Other

The GitHub Action opens a PR for final review before the data fix is merged.
The issue body may contain plain JSON, fenced JSON, or the legacy hidden JSON block.
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

EXCLUSION_CATEGORIES = {
    "closed no longer available",
    "closed / no longer available",
    "closed",
    "no horse camping",
    "duplicate listing",
    "duplicate",
}

CATEGORY_ALIASES = {
    "wronglocation": "Wrong location",
    "closednolongeravailable": "Closed / no longer available",
    "closed": "Closed / no longer available",
    "nohorsecamping": "No horse camping",
    "duplicatelisting": "Duplicate listing",
    "duplicate": "Duplicate listing",
    "missingphone": "Missing phone",
    "missingwebsite": "Missing website",
    "missingdescriptiondetails": "Missing description/details",
    "missingdescription": "Missing description/details",
    "missingdetails": "Missing description/details",
    "missingaccommodations": "Missing accommodations",
    "incorrectamenitiesaccommodations": "Incorrect amenities/accommodations",
    "incorrectaccommodationsamenities": "Incorrect amenities/accommodations",
    "incorrectamenities": "Incorrect amenities/accommodations",
    "incorrectaccommodations": "Incorrect amenities/accommodations",
    "badphonewebsite": "Bad phone / website",
    "badphone": "Bad phone / website",
    "badwebsite": "Bad phone / website",
    "other": "Other",
}

OVERRIDE_ALLOWED_FIELDS = {
    "name",
    "location",
    "address",
    "city",
    "state",
    "latitude",
    "longitude",
    "phone",
    "website",
    "description",
    "hookups",
    "accommodations",
    "maxRigLength",
    "stallCount",
    "paddockCount",
    "seasonStart",
    "seasonEnd",
    "hasWashRack",
    "hasDumpStation",
    "hasWifi",
    "hasBathhouse",
    "pullThroughAvailable",
}

BOOLEAN_FIELDS = {"hasWashRack", "hasDumpStation", "hasWifi", "hasBathhouse", "pullThroughAvailable"}
INTEGER_FIELDS = {"maxRigLength", "stallCount", "paddockCount", "seasonStart", "seasonEnd"}
FLOAT_FIELDS = {"latitude", "longitude"}
LIST_FIELDS = {"hookups", "accommodations"}


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
    camp_id = clean_text(payload.get("campId") or payload.get("camp_id") or payload.get("listingId") or payload.get("listing_id") or payload.get("id"))
    if not camp_id:
        raise ValueError("Problem report is missing campId")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", camp_id):
        raise ValueError(f"Problem report campId looks invalid: {camp_id!r}")
    return camp_id


def get_category(payload: dict[str, Any]) -> str:
    category = clean_text(payload.get("category") or payload.get("problemCategory") or payload.get("problem_category") or payload.get("problemType") or payload.get("problem_type"))
    if not category:
        raise ValueError("Problem report is missing category/problemCategory")
    return category


def parse_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    return parsed


def parse_int(value: Any, *, field: str) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = clean_text(value).lower()
    if text in {"1", "true", "yes", "y", "on", "available"}:
        return True
    if text in {"0", "false", "no", "n", "off", "unavailable", ""}:
        return False
    raise ValueError(f"Expected a boolean value, got: {value!r}")


def coerce_string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,;|]", str(value))
    out: list[str] = []
    for item in items:
        cleaned = clean_text(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def canonical_category(category: str) -> str:
    compact = normalize_field_name(category)
    return CATEGORY_ALIASES.get(compact, category.strip())


def get_proposed_updates(payload: dict[str, Any], category: str) -> dict[str, Any]:
    updates = payload.get("proposedUpdates") or payload.get("proposed_updates") or payload.get("updates")
    if updates is not None:
        if not isinstance(updates, dict):
            raise ValueError("proposedUpdates must be a JSON object")
        return dict(updates)

    # Backward-compatible support for older issue bodies where values were top-level fields.
    direct: dict[str, Any] = {}
    for field in OVERRIDE_ALLOWED_FIELDS:
        if field in payload and payload[field] not in (None, ""):
            direct[field] = payload[field]

    normalized = normalize_field_name(category)
    if normalized == "missingphone" and payload.get("phone"):
        direct["phone"] = payload["phone"]
    if normalized == "missingwebsite" and payload.get("website"):
        direct["website"] = payload["website"]
    if normalized in {"missingdescriptiondetails", "missingdescription", "missingdetails"}:
        description = payload.get("description") or payload.get("details")
        if description:
            direct["description"] = description
    return direct


def normalize_override_updates(updates: dict[str, Any], *, category: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    unknown_fields = sorted(set(updates) - OVERRIDE_ALLOWED_FIELDS - {"exclude", "excludeReason", "duplicateOf", "duplicate_of"})
    if unknown_fields:
        raise ValueError(f"Unsupported proposedUpdates field(s): {', '.join(unknown_fields)}")

    for field, value in updates.items():
        if field not in OVERRIDE_ALLOWED_FIELDS:
            continue
        if value is None or value == "":
            continue
        if field in FLOAT_FIELDS:
            parsed = parse_float(value, field=field)
            if field == "latitude" and not (-90 <= parsed <= 90):
                raise ValueError("latitude must be between -90 and 90")
            if field == "longitude" and not (-180 <= parsed <= 180):
                raise ValueError("longitude must be between -180 and 180")
            normalized[field] = parsed
        elif field in INTEGER_FIELDS:
            normalized[field] = parse_int(value, field=field)
        elif field in BOOLEAN_FIELDS:
            normalized[field] = parse_bool(value)
        elif field in LIST_FIELDS:
            normalized[field] = coerce_string_list(value)
        else:
            normalized[field] = clean_text(value)

    if ("latitude" in normalized) ^ ("longitude" in normalized):
        raise ValueError("latitude and longitude must be approved together")
    if normalized.get("latitude") == 0 and normalized.get("longitude") == 0:
        raise ValueError("latitude/longitude cannot both be zero")

    normalized_category = normalize_field_name(category)
    if normalized_category == "missingphone" and "phone" not in normalized:
        raise ValueError("Missing phone reports must include proposedUpdates.phone")
    if normalized_category == "missingwebsite" and "website" not in normalized:
        raise ValueError("Missing website reports must include proposedUpdates.website")
    if normalized_category in {"missingdescriptiondetails", "missingdescription", "missingdetails"} and "description" not in normalized:
        raise ValueError("Missing description/details reports must include proposedUpdates.description")
    if normalized_category in {"missingaccommodations", "incorrectamenitiesaccommodations", "incorrectaccommodationsamenities", "incorrectamenities", "incorrectaccommodations"}:
        if not any(key in normalized for key in ("accommodations", "hookups", "hasWashRack", "hasDumpStation", "hasWifi", "hasBathhouse", "pullThroughAvailable", "stallCount", "paddockCount")):
            raise ValueError("Accommodation/amenity reports must include at least one accommodation, hookup, or amenity field")
    return normalized


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


def build_pr_body(
    *,
    issue_number: int,
    target: str,
    category: str,
    camp_id: str,
    notes: str,
    payload: dict[str, Any],
    action_summary: str,
) -> str:
    body = (
        f"Closes #{issue_number}\n\n"
        f"Target: `{target}`\n\n"
        f"Approved problem report category: `{category}`\n\n"
        f"Listing ID: `{camp_id}`\n\n"
        f"{action_summary}\n\n"
        "Important: editing this PR description/body does not change app data. "
        "To change what gets merged into the app, edit the JSON file in the Files changed tab.\n\n"
    )
    if notes:
        body += f"User notes: {notes}\n\n"
    body += (
        "Problem report payload for review:\n\n"
        "```json\n"
        f"{compact_json(payload)}\n"
        "```\n\n"
        "Review the JSON diff below, then merge when ready. Merging this PR will automatically close the linked issue."
    )
    return body


def approve_exclusion_report(issue_number: int, payload: dict[str, Any], camp_id: str, camp_name: str, category: str, notes: str) -> tuple[str, str, str]:
    updates = get_proposed_updates(payload, category)
    duplicate_of = clean_text(updates.get("duplicateOf") or updates.get("duplicate_of") or payload.get("duplicateOf") or payload.get("duplicate_of"))
    exclude_reason = clean_text(
        updates.get("excludeReason")
        or updates.get("exclude_reason")
        or payload.get("excludeReason")
        or payload.get("exclude_reason")
        or category
    )

    exclusions = load_exclusions()
    already_excluded = camp_id in exclusions
    if not already_excluded:
        exclusions.append(camp_id)
        write_exclusions(exclusions)

    result = {"alreadyExcluded": already_excluded, "target": str(EXCLUSIONS_PATH), "excludeReason": exclude_reason}
    if duplicate_of:
        result["duplicateOf"] = duplicate_of
    log_path = write_problem_report_log(issue_number, payload, "exclude_listing", camp_id, result)

    action_summary = (
        f"This PR adds generated listing ID `{camp_id}` to `data/exclusions.json`. "
        "The next Seed Camp Data run will remove this listing from `camps.json`."
    )
    if duplicate_of:
        action_summary += f"\n\nDuplicate of: `{duplicate_of}`"
    if exclude_reason:
        action_summary += f"\n\nReason: {exclude_reason}"

    if canonical_category(category) == "Duplicate listing":
        pr_title = f"Exclude duplicate listing: {camp_name}"
    elif canonical_category(category) == "Closed / no longer available":
        pr_title = f"Exclude closed listing: {camp_name}"
    else:
        pr_title = f"Exclude listing: {camp_name}"

    pr_body = build_pr_body(
        issue_number=issue_number,
        target=str(EXCLUSIONS_PATH),
        category=category,
        camp_id=camp_id,
        notes=notes,
        payload=payload,
        action_summary=action_summary,
    )
    summary = f"Added {camp_id} to {EXCLUSIONS_PATH}; wrote review log: {log_path}" if not already_excluded else f"{camp_id} was already present in {EXCLUSIONS_PATH}; wrote review log: {log_path}"
    return pr_title, pr_body, summary


def approve_override_report(issue_number: int, payload: dict[str, Any], camp_id: str, camp_name: str, category: str, notes: str) -> tuple[str, str, str]:
    updates = get_proposed_updates(payload, category)

    # Coordinate reports can still use the older top-level coordinates field.
    if canonical_category(category) == "Wrong location" and not {"latitude", "longitude"}.issubset(updates):
        latitude, longitude = parse_coordinates(payload)
        updates["latitude"] = latitude
        updates["longitude"] = longitude

    normalized_updates = normalize_override_updates(updates, category=category)
    if not normalized_updates:
        # All reports still create a PR path. For truly generic/Other reports, keep a review log only.
        log_path = write_problem_report_log(
            issue_number,
            payload,
            "review_only",
            camp_id,
            {"target": str(REPORT_LOG_DIR), "message": "No source JSON override was provided; maintainer review required."},
        )
        pr_title = f"Review HorseCamp problem report: {camp_name}"
        pr_body = build_pr_body(
            issue_number=issue_number,
            target=str(log_path),
            category=category,
            camp_id=camp_id,
            notes=notes,
            payload=payload,
            action_summary="This PR records the approved problem report for review. Add a concrete override or exclusion in the Files changed tab before merging if app data should change.",
        )
        return pr_title, pr_body, f"Wrote review-only problem report log: {log_path}"

    overrides = load_overrides()
    existing_patch = dict(overrides.get(camp_id, {}))
    previous_values = {key: existing_patch.get(key) for key in normalized_updates}
    existing_patch.update(normalized_updates)
    overrides[camp_id] = existing_patch
    write_overrides(overrides)

    log_path = write_problem_report_log(
        issue_number,
        payload,
        "override_listing_fields",
        camp_id,
        {
            "target": str(OVERRIDES_PATH),
            "updates": normalized_updates,
            "previousValues": previous_values,
        },
    )

    category_title = canonical_category(category)
    if category_title == "Wrong location":
        pr_title = f"Correct listing location: {camp_name}"
    elif category_title in {"Missing phone", "Missing website", "Missing description/details", "Missing accommodations"}:
        pr_title = f"Add missing listing info: {camp_name}"
    elif category_title in {"Incorrect amenities/accommodations", "Bad phone / website"}:
        pr_title = f"Correct listing details: {camp_name}"
    else:
        pr_title = f"Update listing details: {camp_name}"

    action_summary = (
        f"This PR adds or updates an override for generated listing ID `{camp_id}` in `data/overrides.json`. "
        "The next Seed Camp Data run will apply these corrected fields to `camps.json`.\n\n"
        "Proposed updates:\n\n"
        "```json\n"
        f"{compact_json(normalized_updates)}\n"
        "```"
    )
    pr_body = build_pr_body(
        issue_number=issue_number,
        target=str(OVERRIDES_PATH),
        category=category,
        camp_id=camp_id,
        notes=notes,
        payload=payload,
        action_summary=action_summary,
    )
    summary = f"Updated override for {camp_id} in {OVERRIDES_PATH}; wrote review log: {log_path}"
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
    camp_name = clean_text(payload.get("campName") or payload.get("camp_name") or payload.get("listingName") or payload.get("listing_name") or payload.get("name")) or camp_id
    notes = clean_text(payload.get("notes") or payload.get("userNotes") or payload.get("user_notes"))

    category = canonical_category(category)
    normalized_category = category_key(category)
    if normalized_category in EXCLUSION_CATEGORIES:
        pr_title, pr_body, summary = approve_exclusion_report(args.issue_number, payload, camp_id, camp_name, category, notes)
    else:
        pr_title, pr_body, summary = approve_override_report(args.issue_number, payload, camp_id, camp_name, category, notes)

    print(summary)
    write_github_outputs(args.github_output, pr_title=pr_title, pr_body=pr_body, summary=summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
