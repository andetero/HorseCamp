#!/usr/bin/env python3
from __future__ import annotations

import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = ROOT / "generated" / "weekly_call_sheet.pdf"

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
FROM_EMAIL = os.environ["FROM_EMAIL"]
TO_EMAIL = os.environ["PRINTER_EMAIL"]


def attach_file(msg: EmailMessage, path: Path) -> None:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        maintype, subtype = mime_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"

    with path.open("rb") as f:
        msg.add_attachment(
            f.read(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )


def main() -> None:
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"PDF not found: {PDF_PATH}")

    msg = EmailMessage()
    msg["Subject"] = "HorseCamp weekly verification sheet"
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    msg.set_content("Attached is the weekly HorseCamp verification sheet in PDF format.")

    attach_file(msg, PDF_PATH)

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

    print(f"Sent {PDF_PATH.name} to {TO_EMAIL}")


if __name__ == "__main__":
    main()
