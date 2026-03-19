"""Import downloaded voting records into the SQLite database."""

import json
from pathlib import Path

from src.config import DATA_DIR
from src.db import get_connection, init_db


VOTES_DIR = DATA_DIR / "voting_records"


def import_congress_gov_votes(filepath: Path):
    """Import Congress.gov format voting records into the database."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    member = data["member"]
    congress = data.get("congress", "unknown")
    votes = data.get("votes", [])

    conn = get_connection()
    imported = 0

    for vote in votes:
        # Congress.gov vote structure varies — extract what we can
        bill_info = vote.get("bill", {})
        bill_id = None
        bill_title = None

        if bill_info:
            bill_type = bill_info.get("type", "").upper()
            bill_num = bill_info.get("number", "")
            if bill_type and bill_num:
                bill_id = f"{bill_type}.{bill_num}"
            bill_title = bill_info.get("title")

        vote_date = vote.get("date", "")
        position = vote.get("position", vote.get("vote", ""))
        chamber = vote.get("chamber", "")
        question = vote.get("question", "")
        description = vote.get("description", question)
        url = vote.get("url", "")

        if not bill_title and description:
            bill_title = description

        # Check if already imported (avoid duplicates)
        existing = conn.execute(
            "SELECT id FROM voting_records WHERE representative=? AND vote_date=? AND bill_id=?",
            (member, vote_date, bill_id)
        ).fetchone()

        if existing:
            continue

        conn.execute(
            "INSERT INTO voting_records "
            "(representative, vote_date, bill_id, bill_title, vote, chamber, session, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (member, vote_date, bill_id, bill_title, position, chamber,
             f"{congress}th Congress", url)
        )
        imported += 1

    conn.commit()
    conn.close()
    print(f"  Imported {imported} new votes for {member} (skipped {len(votes) - imported} duplicates)")


def import_govtrack_votes(filepath: Path):
    """Import GovTrack format voting records into the database."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    member = data["member"]
    congress = data.get("congress", "unknown")
    votes = data.get("votes", [])

    conn = get_connection()
    imported = 0

    for entry in votes:
        vote_info = entry.get("vote", {})
        option = entry.get("option", {})

        vote_date = vote_info.get("created", "")
        position = option.get("value", "")
        chamber = vote_info.get("chamber_label", "")
        question = vote_info.get("question", "")
        category = vote_info.get("category_label", "")

        # Extract bill info if present
        related = vote_info.get("related_bill")
        bill_id = None
        bill_title = None
        if related and isinstance(related, dict):
            bill_type = related.get("bill_type_label", "")
            bill_num = related.get("number", "")
            if bill_type and bill_num:
                bill_id = f"{bill_type} {bill_num}"
            bill_title = related.get("title")

        if not bill_title:
            bill_title = question or category

        url = vote_info.get("link", "")

        existing = conn.execute(
            "SELECT id FROM voting_records WHERE representative=? AND vote_date=? AND bill_id=?",
            (member, vote_date, bill_id)
        ).fetchone()

        if existing:
            continue

        conn.execute(
            "INSERT INTO voting_records "
            "(representative, vote_date, bill_id, bill_title, vote, chamber, session, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (member, vote_date, bill_id, bill_title, position, chamber,
             f"{congress}th Congress", url)
        )
        imported += 1

    conn.commit()
    conn.close()
    print(f"  Imported {imported} new votes for {member}")


def import_all():
    """Import all downloaded voting record files."""
    init_db()

    if not VOTES_DIR.exists():
        print(f"No voting records directory at {VOTES_DIR}")
        print("Run 'python -m src.congress_api votes' first to download records.")
        return

    json_files = list(VOTES_DIR.glob("*.json"))
    if not json_files:
        print(f"No JSON files in {VOTES_DIR}")
        return

    for filepath in json_files:
        print(f"\nImporting {filepath.name}...")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        source = data.get("source", "unknown")
        if source == "govtrack":
            import_govtrack_votes(filepath)
        else:
            import_congress_gov_votes(filepath)


if __name__ == "__main__":
    import_all()
