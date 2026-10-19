"""Enqueue letters for approval in the web UI.

This is the ONLY module Claude should use to move a letter toward sending.
Nothing here ever sends anything — letters land in status pending_approval
and wait for a human click in the web UI (src/webui.py).
"""

import math
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT, get_env
from src.db import (get_connection, get_letter_by_id, init_db,
                    update_letter_status)
from src.delivery.router import get_delivery_info
from src.letter import parse_letter_markdown, update_markdown_status, \
    write_letter_markdown

# Rough per-page character capacity for a faxed letter (12pt, 1in margins)
FAX_CHARS_PER_PAGE = 2800
FAX_CENTS_PER_PAGE = 3
MAIL_CENTS_PER_LETTER = 83


def estimate_cost_cents(channel: str, body: str) -> int:
    if channel == "fax":
        pages = max(1, math.ceil(len(body) / FAX_CHARS_PER_PAGE))
        return pages * FAX_CENTS_PER_PAGE
    if channel == "mail":
        return MAIL_CENTS_PER_LETTER
    return 0  # email, web_form


def _relative_md_path(path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _lob_test_render(recipient: str, topic: str, body: str) -> str | None:
    """Create a free Lob test letter so the UI can show the actual printed
    page. Returns a thumbnail URL, or None if no test key / render failed."""
    if not get_env("LOB_TEST_API_KEY"):
        return None
    from src.delivery.mail_sender import send_letter as lob_send
    info = get_delivery_info(recipient)
    mail_address = info.get("mail_address")
    if not mail_address:
        return None
    result = lob_send(mail_address, body, subject=topic,
                      recipient_name=recipient, dry_run=False, use_live=False)
    thumbnails = result.get("thumbnails") or []
    if thumbnails:
        return thumbnails[0].get("large") or thumbnails[0].get("medium")
    return None


def queue_letter(recipient: str, topic: str, body: str,
                 channel: str = None, md_path: str = None) -> int:
    """Queue a letter for human approval. Returns the letter id.

    Args:
        recipient: Representative name (must match router DELIVERY_DEFAULTS)
        topic: One-line topic
        body: Full letter text as it will be sent
        channel: Delivery channel; defaults to the rep's router default
        md_path: Existing markdown file for this letter. If omitted, a new
            md file is written using the standard conventions.
    """
    init_db()
    channel = channel or get_delivery_info(recipient).get("channel", "unknown")

    if md_path:
        update_markdown_status(md_path, "pending_approval")
    else:
        md_path = write_letter_markdown(recipient, topic, body, channel,
                                        status="pending_approval")

    thumbnail = None
    if channel == "mail":
        thumbnail = _lob_test_render(recipient, topic, body)

    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO letters (recipient, topic, body, channel, status, "
        "session_date, estimated_cost_cents, lob_thumbnail_url, md_path) "
        "VALUES (?, ?, ?, ?, 'pending_approval', ?, ?, ?, ?)",
        (recipient, topic, body, channel,
         datetime.now().strftime("%Y-%m-%d"),
         estimate_cost_cents(channel, body), thumbnail,
         _relative_md_path(md_path))
    )
    letter_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return letter_id


def queue_letter_from_markdown(md_path: str, recipient: str,
                               channel: str = None) -> int:
    """Queue an already-written letter md file (body = text below ---)."""
    parsed = parse_letter_markdown(md_path)
    topic = parsed["headers"].get("topic", "Untitled")
    channel = channel or parsed["headers"].get("channel", "").split(" ")[0] \
        or None
    return queue_letter(recipient, topic, parsed["body"],
                        channel=channel, md_path=md_path)


def reject_letter(letter_id: int, note: str):
    """Mark a queued letter rejected with a note explaining why.

    Claude should read the note and queue a redrafted letter as a NEW row —
    rejected letters are never resurrected.
    """
    letter = get_letter_by_id(letter_id)
    if not letter:
        raise ValueError(f"No letter with id {letter_id}")
    update_letter_status(letter_id, "rejected", {
        "rejected_at": datetime.now().isoformat(),
        "rejection_note": note,
    })
