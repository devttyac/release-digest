#!/usr/bin/env python3
"""Email the rendered digest with posters embedded, so no client can block them.

Usage:
    .venv/bin/python send_digest.py [JSON_PATH] [--to ADDRESS] [--dry-run]

Why this exists instead of sending through the Gmail MCP tool:

Remote images (image.tmdb.org, media.rawg.io) are blocked by default in most
clients — they leak "this person opened the email" to a third party, so clients
require an explicit opt-in. Embedding each poster as an inline attachment
referenced by Content-ID removes the external fetch entirely, so there is
nothing left to block.

Doing that through the MCP tool would mean pushing ~1.5MB of base64 through the
model's context every single month, which is not viable for an unattended job.
Sending over SMTP lets the images go straight from disk to the wire.

Configuration comes from the project's gitignored .env (never CLI args, never
logged). See .env.example:
    GMAIL_ADDRESS         the sending Gmail account
    GMAIL_APP_PASSWORD    a Google App Password, NOT the account password
    DIGEST_RECIPIENT      where to send the digest
"""

import argparse
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

import render_digest

SCRIPT_DIR = Path(__file__).resolve().parent
REQUEST_TIMEOUT = 20
MAX_IMAGE_BYTES = 2 * 1024 * 1024   # a poster thumbnail is tens of KB; anything larger is wrong
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # stay well under the 25MB message ceiling
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


ALLOWED_IMAGE_HOSTS = frozenset({"image.tmdb.org", "media.rawg.io"})


def is_allowed_image_url(url: str) -> bool:
    """Only fetch posters from the two CDNs we actually source from.

    The URLs come out of third-party API responses, which are untrusted input:
    a crafted entry could otherwise point this fetch at an internal address
    (localhost, link-local metadata endpoints) and turn the sender into an SSRF
    vector. An explicit host allowlist over https is the cheap, precise fix.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_IMAGE_HOSTS


def sniff_image_subtype(blob: bytes) -> str | None:
    """Identify an image by magic bytes.

    Needed because TMDB sometimes responds without a Content-Type header, and
    trusting the header alone silently drops perfectly good posters. Sniffing
    also means a non-image response can't be attached as though it were one.
    """
    if blob.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def collect_image_urls(data: dict) -> list[str]:
    seen: list[str] = []
    for category in ("games", "movies", "tv"):
        for item in data.get(category, []):
            url = item.get("image")
            if url and url not in seen:
                seen.append(url)
    return seen


def download_images(urls: list[str]) -> tuple[dict[str, str], dict[str, tuple[bytes, str]], list[str]]:
    """Fetch each poster. Returns (url->cid, cid->(bytes, subtype), errors).

    A failed download is not fatal: that item simply keeps its remote URL and
    degrades to the old blocked-image behaviour rather than losing the card.
    """
    cid_map: dict[str, str] = {}
    payloads: dict[str, tuple[bytes, str]] = {}
    errors: list[str] = []
    total = 0

    for url in urls:
        if not is_allowed_image_url(url):
            errors.append(f"{url}: host not in the image CDN allowlist")
            continue
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue
        if not resp.ok:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue

        subtype = sniff_image_subtype(resp.content)
        if subtype is None:
            content_type = resp.headers.get("Content-Type", "") or "no content-type"
            errors.append(f"{url}: not a recognised image ({content_type})")
            continue
        if len(resp.content) > MAX_IMAGE_BYTES:
            errors.append(f"{url}: {len(resp.content)} bytes exceeds per-image cap")
            continue
        if total + len(resp.content) > MAX_TOTAL_BYTES:
            errors.append(f"{url}: skipped, total attachment budget reached")
            continue

        cid = make_msgid(domain="release-digest")[1:-1]  # strip the angle brackets for use in src="cid:..."
        cid_map[url] = cid
        payloads[cid] = (resp.content, subtype)
        total += len(resp.content)

    return cid_map, payloads, errors


def plain_text(data: dict, month_label: str) -> str:
    lines = [f"Release Digest — {month_label}", ""]
    for key, label in (("games", "GAMES"), ("movies", "MOVIES"), ("tv", "TV")):
        items = data.get(key, [])
        if not items:
            continue
        lines.append(label)
        for item in items:
            mark = " [TOP PICK]" if item.get("top_pick") else ""
            lines.append(f"  {render_digest.fmt_date(item['release_date'])} — {item['title']}{mark}")
            if item.get("overview"):
                lines.append(f"      {item['overview']}")
        lines.append("")
    lines.append("Sent automatically by release-digest.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", nargs="?", default="/tmp/release-digest-data.json")
    parser.add_argument("--to", default=None, help="Recipient (default: DIGEST_RECIPIENT in .env)")
    parser.add_argument("--subject-suffix", default="", help="Appended to the subject, e.g. ' (test)'")
    parser.add_argument("--dry-run", action="store_true", help="Build the message and report, but do not send")
    parser.add_argument("--archive-dir", default=None,
                        help="Also write the rendered HTML and raw JSON here, named by month")
    args = parser.parse_args()

    load_dotenv(SCRIPT_DIR / ".env")
    sender = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    # Google displays app passwords as "abcd efgh ijkl mnop"; the spaces are
    # presentational and must not be sent as part of the credential.
    app_password = "".join((os.environ.get("GMAIL_APP_PASSWORD") or "").split())
    recipient = args.to or os.environ.get("DIGEST_RECIPIENT") or ""

    missing = [
        name for name, val in (
            ("GMAIL_ADDRESS", sender),
            ("GMAIL_APP_PASSWORD", app_password),
            ("DIGEST_RECIPIENT", recipient),
        )
        if not val or val.startswith("your_")
    ]
    if missing and not args.dry_run:
        print(f"Missing/unset: {', '.join(missing)} — edit .env in {SCRIPT_DIR}", file=sys.stderr)
        return 2

    data = json.loads(Path(args.json_path).read_text())
    year, month = data["month"].split("-")
    month_label = f"{render_digest.MONTHS[int(month) - 1]} {year}"

    cid_map, payloads, image_errors = download_images(collect_image_urls(data))
    print(f"Embedded {len(payloads)} images ({sum(len(b) for b, _ in payloads.values())} bytes)", file=sys.stderr)
    for err in image_errors:
        print(f"  image skipped — {err}", file=sys.stderr)

    if args.archive_dir:
        archive = Path(args.archive_dir)
        archive.mkdir(parents=True, exist_ok=True)
        # Archive the remote-URL version, not the cid: one — cid references only
        # resolve inside the email that carries the attachments, so an archived
        # copy using them would render with broken images.
        (archive / f"release-digest-{data['month']}.html").write_text(render_digest.render(data))
        (archive / f"release-digest-{data['month']}.json").write_text(json.dumps(data, indent=2))
        print(f"Archived to {archive}", file=sys.stderr)

    msg = EmailMessage()
    msg["Subject"] = f"Release Digest — {month_label}{args.subject_suffix}"
    msg["From"] = sender or "unset@example.com"
    msg["To"] = recipient or "unset@example.com"
    msg.set_content(plain_text(data, month_label))
    msg.add_alternative(render_digest.render(data, cid_map), subtype="html")

    # Attach each poster to the HTML part so it resolves as a related resource.
    html_part = msg.get_payload()[-1]
    for cid, (blob, subtype) in payloads.items():
        html_part.add_related(blob, maintype="image", subtype=subtype, cid=f"<{cid}>")

    if args.dry_run:
        print(f"DRY RUN — would send to {recipient}, {len(msg.as_bytes())} bytes total", file=sys.stderr)
        return 0

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(sender, app_password)
        smtp.send_message(msg)

    print(f"Sent to {recipient} — {len(msg.as_bytes())} bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
