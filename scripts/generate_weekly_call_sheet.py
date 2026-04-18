from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
LAYOVERS_PATH = DATA_DIR / "layovers.json"
STATE_PARKS_DIR = DATA_DIR / "state_parks"
PROGRESS_PATH = DATA_DIR / "call_sheet_progress.json"
OUTPUT_DIR = REPO_ROOT / "out" / "weekly_call_sheet"

DEFAULT_BATCH_SIZE = 30


@dataclass
class Listing:
    state: str
    name: str
    location: str
    phone: str
    website: str
    source: str
    source_detail: str
    accommodations: str
    hookups: str
    notes: str


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_manual_records() -> dict[str, list[Listing]]:
    grouped: dict[str, list[Listing]] = defaultdict(list)

    def add_records(items: list[dict[str, Any]]) -> None:
        for item in items:
            state = (item.get("state") or "").strip().upper()
            if not state:
                continue
            grouped[state].append(
                Listing(
                    state=state,
                    name=(item.get("name") or "").strip(),
                    location=(item.get("location") or "").strip(),
                    phone=(item.get("phone") or "").strip(),
                    website=(item.get("website") or "").strip(),
                    source=(item.get("source") or "").strip(),
                    source_detail=(item.get("sourceDetail") or item.get("source") or "").strip(),
                    accommodations=", ".join(item.get("accommodations") or []),
                    hookups=", ".join(item.get("hookups") or []),
                    notes=(item.get("description") or "").strip(),
                )
            )

    add_records(load_json(LAYOVERS_PATH))

    for path in sorted(STATE_PARKS_DIR.glob("*.json")):
        add_records(load_json(path))

    for listings in grouped.values():
        listings.sort(key=lambda x: (x.name.lower(), x.location.lower()))

    return dict(sorted(grouped.items()))


def load_progress(states: list[str]) -> dict[str, Any]:
    default = {
        "current_state_index": 0,
        "offset_by_state": {state: 0 for state in states},
        "last_generated_at": None,
        "last_state": None,
        "last_batch_start": None,
        "last_batch_end": None,
    }
    if not PROGRESS_PATH.exists():
        return default

    data = load_json(PROGRESS_PATH)
    if not isinstance(data, dict):
        return default

    offset_by_state = data.get("offset_by_state") or {}
    normalized_offsets = {state: int(offset_by_state.get(state, 0) or 0) for state in states}

    return {
        "current_state_index": int(data.get("current_state_index", 0) or 0),
        "offset_by_state": normalized_offsets,
        "last_generated_at": data.get("last_generated_at"),
        "last_state": data.get("last_state"),
        "last_batch_start": data.get("last_batch_start"),
        "last_batch_end": data.get("last_batch_end"),
    }


def choose_batch(grouped: dict[str, list[Listing]], batch_size: int) -> tuple[str, list[Listing], dict[str, Any]]:
    states = list(grouped.keys())
    if not states:
        raise RuntimeError("No manual records found in data/layovers.json or data/state_parks/*.json")

    progress = load_progress(states)
    state_index = progress["current_state_index"] % len(states)
    offset_by_state = progress["offset_by_state"]

    for _ in range(len(states)):
        state = states[state_index]
        listings = grouped[state]
        if not listings:
            state_index = (state_index + 1) % len(states)
            continue

        offset = max(0, min(offset_by_state.get(state, 0), len(listings)))
        if offset >= len(listings):
            offset = 0
            offset_by_state[state] = 0
            state_index = (state_index + 1) % len(states)
            continue

        batch = listings[offset : offset + batch_size]
        if not batch:
            state_index = (state_index + 1) % len(states)
            continue

        new_offset = offset + len(batch)
        if new_offset >= len(listings):
            offset_by_state[state] = 0
            next_state_index = (state_index + 1) % len(states)
        else:
            offset_by_state[state] = new_offset
            next_state_index = state_index

        progress.update(
            {
                "current_state_index": next_state_index,
                "offset_by_state": offset_by_state,
                "last_generated_at": datetime.now(timezone.utc).isoformat(),
                "last_state": state,
                "last_batch_start": offset + 1,
                "last_batch_end": offset + len(batch),
            }
        )
        return state, batch, progress

    raise RuntimeError("No listings available to generate a call sheet.")


def write_progress(progress: dict[str, Any]) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, records: list[Listing]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "State",
                "Name",
                "Location",
                "Phone",
                "Website",
                "Type",
                "Accommodations",
                "Hookups",
                "Reached?",
                "Open?",
                "Horse camping?",
                "Corrections needed",
                "Date checked",
                "Initials",
                "Notes",
            ]
        )
        for r in records:
            writer.writerow(
                [
                    r.state,
                    r.name,
                    r.location,
                    r.phone,
                    r.website,
                    r.source_detail or r.source,
                    r.accommodations,
                    r.hookups,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    r.notes,
                ]
            )


def ellipsize(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def write_pdf(path: Path, records: list[Listing], state: str, progress: dict[str, Any], batch_size: int) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(letter),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.35 * inch,
        bottomMargin=0.35 * inch,
    )

    part = math.ceil((progress["last_batch_end"] or len(records)) / batch_size)
    generated_on = datetime.now().astimezone().strftime("%Y-%m-%d")
    story = [
        Paragraph(f"HorseCamp weekly verification call sheet — {state} (part {part})", styles["Title"]),
        Paragraph(
            f"Generated {generated_on}. Call batch {progress['last_batch_start']}–{progress['last_batch_end']} for {state}.",
            styles["Normal"],
        ),
        Spacer(1, 0.18 * inch),
    ]

    rows = [["Name / Location", "Phone / Website", "Type / Amenities", "Call Notes"]]
    for r in records:
        rows.append(
            [
                f"{r.name}\n{r.location}",
                f"{r.phone or '—'}\n{ellipsize(r.website or '—', 48)}",
                f"{ellipsize(r.source_detail or r.source, 24)}\n{ellipsize(r.accommodations or '—', 42)}\nHookups: {ellipsize(r.hookups or '—', 30)}",
                "Reached: ________   Open: ________\nHorse camping: ________   Price: ________\nCorrections: _______________________________________\n_______________________________________________",
            ]
        )

    table = Table(rows, colWidths=[2.4 * inch, 2.1 * inch, 2.2 * inch, 3.9 * inch], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E6E6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                ("LEADING", (0, 1), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#9A9A9A")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    doc.build(story)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a weekly verification call sheet from manual HorseCamp data.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    batch_size = max(1, args.batch_size)
    grouped = load_manual_records()
    state, records, progress = choose_batch(grouped, batch_size=batch_size)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / "weekly_call_sheet.csv"
    pdf_path = OUTPUT_DIR / "weekly_call_sheet.pdf"

    write_csv(csv_path, records)
    write_pdf(pdf_path, records, state=state, progress=progress, batch_size=batch_size)
    write_progress(progress)

    manifest = {
        "state": state,
        "count": len(records),
        "batch_start": progress["last_batch_start"],
        "batch_end": progress["last_batch_end"],
        "csv_path": str(csv_path.relative_to(REPO_ROOT)),
        "pdf_path": str(pdf_path.relative_to(REPO_ROOT)),
    }
    (OUTPUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
