#!/usr/bin/env python3
"""Validate the generated HorseCamp public camps.json feed.

The schema source of truth is data/example.json. Any camp field that is not
present in Example.json is treated as a schema creep error and fails validation.

This validator intentionally keeps rating/reviewCount allowed while the public
App Store build 1.03 still requires them. Remove them from data/example.json and
from the public feed after the tolerant 1.04 app build is released.
"""
from __future__ import annotations

import json
import sys
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CAMPS_PATH = REPO_ROOT / "camps.json"
EXAMPLE_PATH = REPO_ROOT / "data" / "example.json"
MIN_EXPECTED_CAMPS = 1000

RETIRED_FIELDS = {
    "address",
    "addressPreferredForMaps",
    "attribution",
    "category",
    "city",
    "coordinateSource",
    "lastSynced",
    "locationConfidence",
    "mapSearchAddress",
    "partner",
    "sourceDetail",
    "sourceUrl",
    "submittedIssueNumber",
}

REQUIRED_FIELDS = {
    "id",
    "name",
    "location",
    "state",
    "latitude",
    "longitude",
    "source",
}

BLOCKED_SOURCES = {"OSM", "OpenStreetMap"}

HORSEMOTEL_STATE_PAGE_RE = re.compile(r"^https?://(?:www\.)?horsemotel\.com/[A-Za-z-]+\.html$", re.I)


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:  # pragma: no cover - CLI validation path
        raise SystemExit(f"ERROR: Could not parse {path.relative_to(REPO_ROOT)}: {exc}") from exc


def load_allowed_fields() -> set[str]:
    example = load_json(EXAMPLE_PATH)
    if not isinstance(example, list) or not example or not isinstance(example[0], dict):
        raise SystemExit("ERROR: data/example.json must be a non-empty array with one example object.")
    return set(example[0].keys())


def load_camps_feed() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    feed = load_json(CAMPS_PATH)
    if not isinstance(feed, dict):
        raise SystemExit("ERROR: camps.json must be a wrapper object with a camps array, not a raw array.")
    camps = feed.get("camps")
    if not isinstance(camps, list):
        raise SystemExit("ERROR: camps.json wrapper must contain a camps array.")
    if feed.get("count") != len(camps):
        raise SystemExit(f"ERROR: camps.json count={feed.get('count')} does not match camps length={len(camps)}.")
    return feed, camps


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate() -> int:
    allowed_fields = load_allowed_fields()
    missing_from_example = REQUIRED_FIELDS - allowed_fields
    if missing_from_example:
        print("ERROR: Required fields are missing from data/example.json:")
        for field in sorted(missing_from_example):
            print(f"  - {field}")
        return 1

    feed, camps = load_camps_feed()
    errors: list[str] = []
    warnings: list[str] = []

    if len(camps) < MIN_EXPECTED_CAMPS:
        errors.append(f"camps.json has only {len(camps)} camps; expected at least {MIN_EXPECTED_CAMPS}.")

    sources = feed.get("sources")
    if isinstance(sources, list):
        blocked_feed_sources = [source for source in sources if any(blocked in str(source) for blocked in BLOCKED_SOURCES)]
        if blocked_feed_sources:
            errors.append(f"Blocked source names appear in feed sources list: {blocked_feed_sources}")
    else:
        warnings.append("camps.json sources is missing or is not an array.")

    unexpected_counts: Counter[str] = Counter()
    retired_counts: Counter[str] = Counter()
    missing_required: dict[str, list[str]] = defaultdict(list)
    blocked_source_records: list[str] = []
    horsemotel_state_page_websites: list[str] = []
    duplicate_ids: list[str] = []
    seen_ids: set[str] = set()

    for index, camp in enumerate(camps):
        if not isinstance(camp, dict):
            errors.append(f"camps[{index}] is not an object.")
            continue

        camp_id = str(camp.get("id") or f"index-{index}")
        if camp_id in seen_ids:
            duplicate_ids.append(camp_id)
        seen_ids.add(camp_id)

        extra_fields = set(camp.keys()) - allowed_fields
        for field in extra_fields:
            unexpected_counts[field] += 1
        for field in RETIRED_FIELDS & set(camp.keys()):
            retired_counts[field] += 1
        for field in REQUIRED_FIELDS:
            if field not in camp or is_blank(camp.get(field)):
                missing_required[field].append(camp_id)

        source = str(camp.get("source") or "")
        if source in BLOCKED_SOURCES or camp_id.startswith("osm-"):
            blocked_source_records.append(camp_id)

        website = str(camp.get("website") or "").strip()
        if source == "HorseMotel.com" and website and HORSEMOTEL_STATE_PAGE_RE.match(website):
            horsemotel_state_page_websites.append(camp_id)

    if unexpected_counts:
        errors.append("Unexpected fields outside data/example.json: " + ", ".join(f"{k}={v}" for k, v in sorted(unexpected_counts.items())))
    if retired_counts:
        errors.append("Retired fields are still present: " + ", ".join(f"{k}={v}" for k, v in sorted(retired_counts.items())))
    if missing_required:
        details = []
        for field, ids in sorted(missing_required.items()):
            preview = ", ".join(ids[:5])
            suffix = "..." if len(ids) > 5 else ""
            details.append(f"{field} missing/blank on {len(ids)} records ({preview}{suffix})")
        errors.append("Required field failures: " + "; ".join(details))
    if duplicate_ids:
        preview = ", ".join(duplicate_ids[:10])
        suffix = "..." if len(duplicate_ids) > 10 else ""
        errors.append(f"Duplicate camp ids found: {preview}{suffix}")
    if blocked_source_records:
        preview = ", ".join(blocked_source_records[:10])
        suffix = "..." if len(blocked_source_records) > 10 else ""
        errors.append(f"Blocked OSM/OpenStreetMap records found: {preview}{suffix}")
    if horsemotel_state_page_websites:
        preview = ", ".join(horsemotel_state_page_websites[:10])
        suffix = "..." if len(horsemotel_state_page_websites) > 10 else ""
        errors.append(f"HorseMotel.com generic state-page URLs found in website field: {preview}{suffix}")

    if errors:
        print("HorseCamp feed validation failed.")
        for error in errors:
            print(f"ERROR: {error}")
        for warning in warnings:
            print(f"WARNING: {warning}")
        return 1

    print("HorseCamp feed validation passed.")
    print(f"Camps: {len(camps)}")
    print(f"Allowed schema fields: {len(allowed_fields)}")
    if warnings:
        for warning in warnings:
            print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(validate())
