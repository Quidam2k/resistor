"""Letter composition, formatting, and storage."""

import os
from datetime import datetime
from pathlib import Path

from src.config import DATA_DIR, load_user
from src.db import save_letter, get_prior_correspondence


LETTERS_DIR = DATA_DIR / "letters"


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
    safe_topic = topic.lower().replace(" ", "-")[:40]
    safe_recipient = recipient_name.lower().replace(" ", "-").replace(".", "")
    filename = f"{date_str}-{safe_recipient}-{safe_topic}.md"

    filepath = LETTERS_DIR / filename
    os.makedirs(LETTERS_DIR, exist_ok=True)

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
