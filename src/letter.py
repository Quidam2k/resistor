"""Letter composition, formatting, and storage."""

import os
import re
from datetime import datetime
from pathlib import Path

from src.config import DATA_DIR, load_user
from src.db import save_letter, get_prior_correspondence


LETTERS_DIR = DATA_DIR / "letters"


def _unique_letter_path(stem: str) -> Path:
    """First free LETTERS_DIR/<stem>.md, then <stem>-2.md, -3.md...

    Redrafts share a date, recipient and topic prefix, so without the suffix
    a v3 overwrote v2's file and rejecting v2 stamped the v3 text 'rejected'.
    """
    os.makedirs(LETTERS_DIR, exist_ok=True)
    filepath, n = LETTERS_DIR / f"{stem}.md", 2
    while filepath.exists():
        filepath, n = LETTERS_DIR / f"{stem}-{n}.md", n + 1
    return filepath


# Windows refuses these in filenames; a ':' silently writes the letter into an
# NTFS alternate data stream and leaves a 0-byte file (#59-61, 2026-10-23; #9387).
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


def _letter_stem(date_str: str, recipient_name: str, topic: str) -> str:
    safe_topic = topic.lower().replace(" ", "-")[:40]
    safe_recipient = recipient_name.lower().replace(" ", "-").replace(".", "")
    return _UNSAFE_FILENAME_CHARS.sub("", f"{date_str}-{safe_recipient}-{safe_topic}")


def format_letter(recipient_name: str, recipient_title: str, topic: str,
                  body: str, include_address: bool = True) -> str:
    """Format a complete letter with constituent header.

    Args:
        recipient_name: e.g. "Ron Wyden"
        recipient_title: e.g. "US Senator"
        topic: One-line topic description
        body: The letter content (without header/footer)
        include_address: Whether to include Todd's address at top
    """
    user = load_user()
    date_str = datetime.now().strftime("%B %d, %Y")

    parts = []

    if include_address:
        parts.append(
            f"{user['name']}\n"
            f"{user['address']}\n"
            f"{user['city']}, {user['state']} {user['zip']}"
        )

    parts.append(date_str)
    parts.append(f"Dear {recipient_title} {recipient_name},")
    parts.append(body)
    parts.append(
        f"Sincerely,\n"
        f"{user['name']}\n"
        f"{user['city']}, {user['state']} {user['zip']}"
    )

    return "\n\n".join(parts)


def save_letter_markdown(recipient_name: str, topic: str, body: str,
                         channel: str, session_date: str = None) -> Path:
    """Save a letter as markdown and record in the database.

    Returns the path to the saved markdown file.
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    filepath = _unique_letter_path(_letter_stem(date_str, recipient_name, topic))

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# Letter to {recipient_name}\n\n")
        f.write(f"**Topic:** {topic}\n")
        f.write(f"**Date:** {date_str}\n")
        f.write(f"**Channel:** {channel}\n")
        f.write(f"**Status:** draft\n\n")
        f.write("---\n\n")
        f.write(body)

    # Also save to database
    letter_id = save_letter(
        recipient=recipient_name,
        topic=topic,
        body=body,
        channel=channel,
        session_date=session_date or date_str,
    )

    return filepath


def write_letter_markdown(recipient_name: str, topic: str, body: str,
                          channel: str, status: str = "draft") -> Path:
    """Write a letter markdown file WITHOUT touching the database.

    Same file conventions as save_letter_markdown; used by the outbox,
    which manages its own DB row.
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    filepath = _unique_letter_path(_letter_stem(date_str, recipient_name, topic))

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# Letter to {recipient_name}\n\n")
        f.write(f"**Topic:** {topic}\n")
        f.write(f"**Date:** {date_str}\n")
        f.write(f"**Channel:** {channel}\n")
        f.write(f"**Status:** {status}\n\n")
        f.write("---\n\n")
        f.write(body)

    return filepath


def update_markdown_status(md_path, status: str):
    """Rewrite the **Status:** header line in a letter markdown file.

    Silently does nothing if the file is missing or has no Status line —
    the DB is the source of truth; the md header is a courtesy mirror.
    """
    path = Path(md_path)
    if not path.is_absolute():
        path = LETTERS_DIR.parent.parent / path  # relative to project root
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("**Status:**"):
            lines[i] = f"**Status:** {status}"
            path.write_text("\n".join(lines), encoding="utf-8")
            return


def parse_letter_markdown(md_path) -> dict:
    """Parse a letter md file into headers + body (text below the first ---)."""
    path = Path(md_path)
    text = path.read_text(encoding="utf-8")
    header_part, sep, body = text.partition("\n---\n")
    if not sep:
        raise ValueError(f"No '---' separator in {md_path}")
    headers = {}
    for line in header_part.split("\n"):
        if line.startswith("**") and ":**" in line:
            key, _, value = line[2:].partition(":**")
            headers[key.strip().lower()] = value.strip()
    return {"headers": headers, "body": body.strip()}


def get_context_for_letter(recipient_name: str, topic: str = None) -> str:
    """Build context string from prior correspondence for composing a follow-up.

    Returns a summary of what we've written before and any responses received.
    """
    history = get_prior_correspondence(recipient_name, topic)

    if not history:
        return f"No prior correspondence with {recipient_name}" + (
            f" on {topic}" if topic else ""
        ) + "."

    lines = [f"Prior correspondence with {recipient_name}:"]
    for entry in history:
        sent = entry.get("sent_at", entry.get("created_at", "unknown date"))
        lines.append(f"\n--- Letter sent {sent} ---")
        lines.append(f"Topic: {entry['topic']}")
        # Include first ~300 chars of body for context
        body_preview = entry["body"][:300]
        if len(entry["body"]) > 300:
            body_preview += "..."
        lines.append(body_preview)

        if entry.get("response_body"):
            lines.append(f"\n  Response received {entry.get('response_date', 'unknown')}:")
            resp_preview = entry["response_body"][:300]
            if len(entry["response_body"]) > 300:
                resp_preview += "..."
            lines.append(f"  {resp_preview}")

    return "\n".join(lines)
