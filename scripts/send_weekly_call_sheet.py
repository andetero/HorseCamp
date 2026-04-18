from __future__ import annotations

import json
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "out" / "weekly_call_sheet" / "manifest.json"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def attach_file(message: EmailMessage, path: Path) -> None:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        maintype, subtype = mime_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"
    with path.open("rb") as f:
        message.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=path.name)


def main() -> None:
    smtp_host = required_env("SMTP_HOST")
    smtp_port = int(required_env("SMTP_PORT"))
    smtp_username = required_env("SMTP_USERNAME")
    smtp_password = required_env("SMTP_PASSWORD")
    from_email = required_env("FROM_EMAIL")
    to_email = required_env("TO_EMAIL")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pdf_path = REPO_ROOT / manifest["pdf_path"]
    csv_path = REPO_ROOT / manifest["csv_path"]

    message = EmailMessage()
    message["Subject"] = f"HorseCamp weekly verification call sheet — {manifest['state']}"
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(
        f"Weekly HorseCamp verification call sheet for {manifest['state']} "
        f"({manifest['count']} listings, batch {manifest['batch_start']}-{manifest['batch_end']}).\n"
        f"PDF and CSV attached."
    )

    attach_file(message, pdf_path)
    attach_file(message, csv_path)

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.login(smtp_username, smtp_password)
        server.send_message(message)

    print(f"Sent weekly call sheet email to {to_email} for state {manifest['state']}")


if __name__ == "__main__":
    main()
