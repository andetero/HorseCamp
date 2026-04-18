#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Spacer, Paragraph, Table, TableStyle, PageBreak

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATE_PARKS_DIR = DATA_DIR / "state_parks"
LAYOVERS_PATH = DATA_DIR / "layovers.json"
PROGRESS_PATH = DATA_DIR / "call_sheet_progress.json"
OUTPUT_DIR = ROOT / "generated"
OUTPUT_PDF = OUTPUT_DIR / "weekly_call_sheet.pdf"
OUTPUT_MANIFEST = OUTPUT_DIR / "weekly_call_sheet_manifest.json"

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))
DEFAULT_STATE = os.getenv("DEFAULT_START_STATE", "")


@dataclass
class Listing:
    source_type: str
    state: str
    name: str
    location: str
    phone: str
    website: str
    listing_id: str
    notes: str = ""


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")


def normalize_phone(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def listing_from_record(record: dict[str, Any], source_type: str) -> Listing:
    state = str(record.get("state", "")).strip().upper()
    return Listing(
        source_type=source_type,
        state=state,
        name=str(record.get("name", "")).strip(),
        location=str(record.get("location", "")).strip(),
        phone=normalize_phone(record.get("phone")),
        website=str(record.get("website", "")).strip(),
        listing_id=str(record.get("id", "")).strip(),
        notes=str(record.get("description", "")).strip(),
    )


def load_manual_listings() -> list[Listing]:
    listings: list[Listing] = []

    for record in load_json(LAYOVERS_PATH, []):
        listings.append(listing_from_record(record, "Layover"))

    if STATE_PARKS_DIR.exists():
        for path in sorted(STATE_PARKS_DIR.glob("*.json")):
            for record in load_json(path, []):
                listings.append(listing_from_record(record, "State Park"))

    listings = [x for x in listings if x.name and x.state]
    listings.sort(key=lambda x: (x.state, x.name.lower(), x.location.lower()))
    return listings


def build_state_groups(listings: Iterable[Listing]) -> dict[str, list[Listing]]:
    grouped: dict[str, list[Listing]] = defaultdict(list)
    for item in listings:
        grouped[item.state].append(item)
    return dict(sorted(grouped.items(), key=lambda kv: kv[0]))


def pick_batch(grouped: dict[str, list[Listing]], progress: dict[str, Any], batch_size: int) -> tuple[str, list[Listing], dict[str, Any]]:
    states = [s for s, rows in grouped.items() if rows]
    if not states:
        raise RuntimeError("No manual listings were found in data/layovers.json or data/state_parks/*.json")

    progress.setdefault("states", {})

    current_state = progress.get("current_state") or DEFAULT_STATE
    if current_state not in grouped or not grouped[current_state]:
        current_state = states[0]

    # Find the next state with remaining rows.
    checked = 0
    while checked < len(states):
        state_rows = grouped[current_state]
        offset = int(progress["states"].get(current_state, 0))
        if offset < len(state_rows):
            break
        idx = states.index(current_state)
        current_state = states[(idx + 1) % len(states)]
        checked += 1
    else:
        progress = {"current_state": states[0], "states": {}}
        current_state = states[0]

    state_rows = grouped[current_state]
    offset = int(progress["states"].get(current_state, 0))
    batch = state_rows[offset : offset + batch_size]
    next_offset = offset + len(batch)

    progress["states"][current_state] = next_offset
    if next_offset >= len(state_rows):
        idx = states.index(current_state)
        progress["current_state"] = states[(idx + 1) % len(states)]
    else:
        progress["current_state"] = current_state

    progress["last_run_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    progress["last_batch"] = {
        "state": current_state,
        "count": len(batch),
        "offset_started": offset,
        "offset_ended": next_offset,
    }
    return current_state, batch, progress


def paragraph(text: str, style_name: str = "BodyText") -> Paragraph:
    styles = getSampleStyleSheet()
    safe = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )
    return Paragraph(safe, styles[style_name])


def make_pdf(state: str, batch: list[Listing], generated_at: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=landscape(letter),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch,
    )

    elements: list[Any] = []
    styles = getSampleStyleSheet()
    title = f"HorseCamp weekly verification sheet — {state}"
    subtitle = f"Generated {generated_at} • {len(batch)} listings"
    instructions = (
        "For each listing: mark OK, FIX, NO ANSWER, CLOSED, or CALL BACK. "
        "Add any corrections for phone, website, horse camping availability, amenities, or pricing."
    )
    elements.extend([
        Paragraph(title, styles["Title"]),
        Spacer(1, 0.08 * inch),
        Paragraph(subtitle, styles["Heading3"]),
        Spacer(1, 0.08 * inch),
        Paragraph(instructions, styles["BodyText"]),
        Spacer(1, 0.18 * inch),
    ])

    rows: list[list[Any]] = [[
        paragraph("#", "Heading5"),
        paragraph("Type", "Heading5"),
        paragraph("Name", "Heading5"),
        paragraph("Location", "Heading5"),
        paragraph("Phone", "Heading5"),
        paragraph("Website", "Heading5"),
        paragraph("Status / notes", "Heading5"),
    ]]

    for idx, item in enumerate(batch, start=1):
        rows.append([
            paragraph(str(idx)),
            paragraph(item.source_type),
            paragraph(item.name),
            paragraph(item.location),
            paragraph(item.phone or "—"),
            paragraph(item.website or "—"),
            paragraph("OK / FIX / NO ANSWER / CLOSED / CALL BACK\n\n______________________________"),
        ])

    table = Table(
        rows,
        colWidths=[0.35 * inch, 0.8 * inch, 2.15 * inch, 1.6 * inch, 1.2 * inch, 2.2 * inch, 2.5 * inch],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEAEA")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F8F8")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ])
    )

    elements.append(table)
    doc.build(elements)


def main() -> None:
    listings = load_manual_listings()
    grouped = build_state_groups(listings)
    progress = load_json(PROGRESS_PATH, {})
    state, batch, updated_progress = pick_batch(grouped, progress, BATCH_SIZE)
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    make_pdf(state, batch, generated_at)
    save_json(PROGRESS_PATH, updated_progress)

    manifest = {
        "state": state,
        "count": len(batch),
        "generated_at": generated_at,
        "pdf": str(OUTPUT_PDF.relative_to(ROOT)),
    }
    save_json(OUTPUT_MANIFEST, manifest)

    print(f"Generated {OUTPUT_PDF} with {len(batch)} listings for {state}")


if __name__ == "__main__":
    main()
