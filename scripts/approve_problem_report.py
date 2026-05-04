#!/usr/bin/env python3
"""
Approve a HorseCamp problem-report issue as a data fix.

For approved "No horse camping" reports, this adds the reported campId to
`data/exclusions.json` so the next Seed Camp Data run removes it from camps.json.
The GitHub Action opens a PR for final review before the exclusion is merged.
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
REPORT_LOG_DIR = Path("data/problem_reports")

HIDDEN_JSON_RE = re.compile(
    r"<!--\s*HORSECAMP_PROBLEM_REPORT_JSON\s*(?P<json>\{.*?\})\s*HORSECAMP_PROBLEM_REPORT_JSON\s*-->",
    re.DOTALL | re.IGNORECASE,
)
FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(?P<json>\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


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


def load_exclusions(path: Path = EXCLUSIONS_PATH) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
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
    path.write_text(json.dumps(unique_sorted, ensure_ascii=False) + "\n", encoding="utf-8")


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


def extract_problem_report(body: str) -> dict[str, Any]:
    for regex in (HIDDEN_JSON_RE, FENCED_JSON_RE):
        match = regex.search(body)
        if match:
            return decode_json(match.group("json"))

    parsed = extract_first_balanced_json(body)
    if parsed is not None:
        return parsed

    raise ValueError("Could not find problem report JSON in issue body")


def validate_report(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    camp_id = clean_text(payload.get("campId") or payload.get("camp_id") or payload.get("id"))
    if not camp_id:
        raise ValueError("Problem report is missing campId")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", camp_id):
        raise ValueError(f"Problem report campId looks invalid: {camp_id!r}")

    category = clean_text(payload.get("category") or payload.get("problemCategory") or payload.get("problem_category"))
    category_key = category.lower().replace("-", " ").replace("_", " ")
    if category_key != "no horse camping":
        raise ValueError(
            "This automation only excludes listings for problem reports with category 'No horse camping'. "
            f"Found category: {category or '(blank)'}"
        )

    camp_name = clean_text(payload.get("campName") or payload.get("camp_name") or payload.get("name")) or camp_id
    notes = clean_text(payload.get("notes") or payload.get("userNotes") or payload.get("user_notes"))
    return camp_id, camp_name, category, notes


def write_problem_report_log(issue_number: int, payload: dict[str, Any], camp_id: str, already_excluded: bool) -> Path:
    REPORT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_LOG_DIR / f"approved_issue_{issue_number}.json"
    path.write_text(
        compact_json(
            {
                "issueNumber": issue_number,
                "action": "exclude_generated_listing",
                "campId": camp_id,
                "alreadyExcluded": already_excluded,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-body", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    body = Path(args.issue_body).read_text(encoding="utf-8")
    payload = extract_problem_report(body)
    camp_id, camp_name, category, notes = validate_report(payload)

    exclusions = load_exclusions()
    already_excluded = camp_id in exclusions
    if not already_excluded:
        exclusions.append(camp_id)
        write_exclusions(exclusions)

    log_path = write_problem_report_log(args.issue_number, payload, camp_id, already_excluded)

    pr_title = f"Exclude non-horse-camping listing: {camp_name}"
    pr_body = (
        f"Closes #{args.issue_number}\n\n"
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
    print(summary)
    write_github_outputs(args.github_output, pr_title=pr_title, pr_body=pr_body, summary=summary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
