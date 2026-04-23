#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATE_PARKS_DIR = DATA_DIR / "state_parks"
LAYOVERS_PATH = DATA_DIR / "layovers.json"
PRIVATE_CAMPS_PATH = DATA_DIR / "private_camps.json"
PROGRESS_PATH = DATA_DIR / "call_sheet_progress.json"
OUTPUT_DIR = ROOT / "generated"
OUTPUT_PDF = OUTPUT_DIR / "weekly_call_sheet.pdf"
OUTPUT_MANIFEST = OUTPUT_DIR / "weekly_call_sheet_manifest.json"

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))
DEFAULT_STATE = os.getenv("DEFAULT_START_STATE", "")
CARDS_PER_PAGE = int(os.getenv("CALL_SHEET_CARDS_PER_PAGE", "3"))

SOURCE_OPTIONS = ["Layover", "RIDB", "State Parks", "OSM", "NPS", "Private Camps"]
HOOKUP_OPTIONS = ["20A", "30A", "50A", "Water", "Sewer"]
ACCOMMODATION_OPTIONS = [
    "Trails",
    "Stalls",
    "Corrals",
    "Paddocks",
    "Highlines",
    "Cabins",
    "Group Camping",
    "Primitive Camping",
    "Horse Camping",
]
BOOLEAN_FIELDS: list[tuple[str, str]] = [
    ("is_verified", "Verified"),
    ("has_wash_rack", "Wash Rack"),
    ("has_dump_station", "Dump Station"),
    ("has_wifi", "Wifi"),
    ("has_bathhouse", "Bathhouse"),
    ("pull_through_available", "Pull Through"),
]


@dataclass
class Listing:
    source_type: str
    source: str
    source_detail: str
    state: str
    name: str
    location: str
    phone: str
    website: str
    listing_id: str
    notes: str = ""
    hookups: list[str] = field(default_factory=list)
    accommodations: list[str] = field(default_factory=list)
    price_per_night: float | int | str = ""
    horse_fee_per_night: float | int | str = ""
    max_rig_length: float | int | str = ""
    stall_count: float | int | str = ""
    paddock_count: float | int | str = ""
    season_start: float | int | str = ""
    season_end: float | int | str = ""
    is_verified: bool | None = None
    has_wash_rack: bool | None = None
    has_dump_station: bool | None = None
    has_wifi: bool | None = None
    has_bathhouse: bool | None = None
    pull_through_available: bool | None = None


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


def normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def normalize_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def listing_from_record(record: dict[str, Any], source_type: str) -> Listing:
    state = str(record.get("state", "")).strip().upper()
    return Listing(
        source_type=source_type,
        source=str(record.get("source", source_type)).strip() or source_type,
        source_detail=str(record.get("sourceDetail", "")).strip(),
        state=state,
        name=str(record.get("name", "")).strip(),
        location=str(record.get("location", "")).strip(),
        phone=normalize_phone(record.get("phone")),
        website=str(record.get("website", "")).strip(),
        listing_id=str(record.get("id", "")).strip(),
        notes=str(record.get("description", "")).strip(),
        hookups=normalize_list(record.get("hookups")),
        accommodations=normalize_list(record.get("accommodations")),
        price_per_night=record.get("pricePerNight", ""),
        horse_fee_per_night=record.get("horseFeePerNight", ""),
        max_rig_length=record.get("maxRigLength", ""),
        stall_count=record.get("stallCount", ""),
        paddock_count=record.get("paddockCount", ""),
        season_start=record.get("seasonStart", ""),
        season_end=record.get("seasonEnd", ""),
        is_verified=normalize_bool(record.get("isVerified")),
        has_wash_rack=normalize_bool(record.get("hasWashRack")),
        has_dump_station=normalize_bool(record.get("hasDumpStation")),
        has_wifi=normalize_bool(record.get("hasWifi")),
        has_bathhouse=normalize_bool(record.get("hasBathhouse")),
        pull_through_available=normalize_bool(record.get("pullThroughAvailable")),
    )


def load_manual_listings() -> list[Listing]:
    listings: list[Listing] = []

    for record in load_json(LAYOVERS_PATH, []):
        listings.append(listing_from_record(record, "Layover"))

    for record in load_json(PRIVATE_CAMPS_PATH, []):
        listings.append(listing_from_record(record, "Private Camp"))

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
        raise RuntimeError("No manual listings were found in data/layovers.json, data/private_camps.json, or data/state_parks/*.json")

    progress.setdefault("states", {})

    current_state = progress.get("current_state") or DEFAULT_STATE
    if current_state not in grouped or not grouped[current_state]:
        current_state = states[0]

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

    progress["last_run_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    progress["last_batch"] = {
        "state": current_state,
        "count": len(batch),
        "offset_started": offset,
        "offset_ended": next_offset,
    }
    return current_state, batch, progress


def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": base["Title"],
        "subtitle": base["Heading3"],
        "body": ParagraphStyle("CallSheetBody", parent=base["BodyText"], fontName="Helvetica", fontSize=8, leading=10),
        "small": ParagraphStyle("CallSheetSmall", parent=base["BodyText"], fontName="Helvetica", fontSize=7.4, leading=9),
        "header": ParagraphStyle("CallSheetHeader", parent=base["Heading4"], fontName="Helvetica-Bold", fontSize=10, leading=12),
    }


def escape_text(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape_text(text).replace("\n", "<br/>"), style)


def format_scalar(value: Any) -> str:
    if value is None or value == "":
        return "____"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def format_current(values: list[str]) -> str:
    return ", ".join(values) if values else "none listed"


def format_choice_line(options: list[str], selected: list[str]) -> str:
    selected_set = {x.strip() for x in selected}
    return "   ".join(f"[{'X' if option in selected_set else ' '}] {option}" for option in options)


def format_boolean_line(value: bool | None) -> str:
    if value is True:
        return "[X] TRUE   [ ] FALSE"
    if value is False:
        return "[ ] TRUE   [X] FALSE"
    return "[ ] TRUE   [ ] FALSE"


def build_listing_card(index: int, item: Listing, style_map: dict[str, ParagraphStyle]) -> KeepTogether:
    source_line = format_choice_line(SOURCE_OPTIONS, [item.source])
    hookup_line = format_choice_line(HOOKUP_OPTIONS, item.hookups)
    accommodation_line = format_choice_line(ACCOMMODATION_OPTIONS, item.accommodations)

    current_source = item.source
    if item.source_detail:
        current_source += f" ({item.source_detail})"

    detail_lines = [
        f"Phone: {item.phone or '____'}    Website: {item.website or '____'}",
        f"Location: {item.location or '____'}",
        f"ID: {item.listing_id or '____'}    Current source: {current_source or '____'}",
        f"Price/night: {format_scalar(item.price_per_night)}    Horse fee/night: {format_scalar(item.horse_fee_per_night)}    Max rig length: {format_scalar(item.max_rig_length)}",
        f"Stalls: {format_scalar(item.stall_count)}    Paddocks: {format_scalar(item.paddock_count)}    Season: {format_scalar(item.season_start)} to {format_scalar(item.season_end)}",
        f"Source (circle one): {source_line}",
        f"Hookups (circle all): {hookup_line}",
        f"Current hookups: {format_current(item.hookups)}",
        f"Accommodations (circle all): {accommodation_line}",
        f"Current accommodations: {format_current(item.accommodations)}",
    ]

    bool_lines = [
        f"{label}: {format_boolean_line(getattr(item, attr))}"
        for attr, label in BOOLEAN_FIELDS
    ]
    detail_lines.extend(bool_lines)
    detail_lines.extend([
        "Corrections / notes:",
        "____________________________________________________________",
        "____________________________________________________________",
    ])

    rows = [[paragraph(f"{index}. {item.name} ({item.state}) — {item.source_type}", style_map["header"])]]
    for line in detail_lines:
        rows.append([paragraph(line, style_map["small"])])

    card = Table(rows, colWidths=[10.15 * inch])
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#666666")),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#DDDDDD")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return KeepTogether([card, Spacer(1, 0.12 * inch)])


def make_pdf(state: str, batch: list[Listing], generated_at: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT_PDF),
        pagesize=landscape(letter),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.35 * inch,
        bottomMargin=0.35 * inch,
    )

    style_map = styles()
    elements: list[Any] = []
    title = f"HorseCamp weekly verification sheet — {state}"
    subtitle = f"Generated {generated_at} • {len(batch)} listings"
    instructions = (
        "Circle the correct options, mark TRUE or FALSE for amenities, and write any corrections for phone, website, pricing, season, or notes. "
        "Use Source options: Layover, RIDB, State Parks, OSM, NPS, or Private Camps."
    )
    elements.extend([
        Paragraph(title, style_map["title"]),
        Spacer(1, 0.06 * inch),
        Paragraph(subtitle, style_map["subtitle"]),
        Spacer(1, 0.06 * inch),
        Paragraph(instructions, style_map["body"]),
        Spacer(1, 0.16 * inch),
    ])

    for idx, item in enumerate(batch, start=1):
        if idx > 1 and (idx - 1) % CARDS_PER_PAGE == 0:
            elements.append(PageBreak())
        elements.append(build_listing_card(idx, item, style_map))

    doc.build(elements)


def main() -> None:
    listings = load_manual_listings()
    grouped = build_state_groups(listings)
    progress = load_json(PROGRESS_PATH, {})
    state, batch, updated_progress = pick_batch(grouped, progress, BATCH_SIZE)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

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
