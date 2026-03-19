"""Fetch Oregon state legislative data — votes, bills, and member info.

Oregon's Legislative Information System (OLIS) provides data at
olis.oregonlegislature.gov. They have an API but it's not well-documented.
This module uses web scraping as a fallback and the OLIS search API where possible.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from src.config import DATA_DIR


OLIS_BASE = "https://olis.oregonlegislature.gov"
OLIS_API_BASE = f"{OLIS_BASE}/liz"

# Oregon legislative sessions
CURRENT_SESSION = "2025R1"  # 2025 Regular Session

# Todd's state reps
OREGON_MEMBERS = {
    "James Manning Jr.": {
        "chamber": "S",  # Senate
        "district": 7,
        "page": "manning",
    },
    "Julie Fahey": {
        "chamber": "H",  # House
        "district": 14,
        "page": "fahey",
    },
}


def fetch_oregon_bill_info(bill_id: str, session: str = CURRENT_SESSION) -> dict:
    """Fetch bill information from OLIS.

    Args:
        bill_id: e.g. "HB3391", "SB1507"
        session: Legislative session, e.g. "2025R1"

    Returns:
        Dict with bill metadata
    """
    url = f"{OLIS_API_BASE}/{session}/Measures/Overview/{bill_id}"
    response = requests.get(url)
    response.raise_for_status()

    # OLIS returns HTML, so we'll extract what we can
    return {
        "bill_id": bill_id,
        "session": session,
        "url": url,
        "status": "fetched",
        "html_length": len(response.text),
    }


def fetch_oregon_bill_text_url(bill_id: str,
                                session: str = CURRENT_SESSION) -> str:
    """Get the URL to download bill text from OLIS.

    Returns URL to the measure document PDF.
    """
    return (
        f"{OLIS_API_BASE}/{session}/Downloads/MeasureDocument/{bill_id}"
    )


def fetch_oregon_member_votes(member_name: str,
                               session: str = CURRENT_SESSION) -> list[dict]:
    """Fetch vote records for an Oregon legislator.

    OLIS doesn't have a clean votes-per-member API, so we fetch from
    the member's page and the session's vote records.
    """
    info = OREGON_MEMBERS.get(member_name)
    if not info:
        raise ValueError(f"Unknown Oregon member: {member_name}")

    # OLIS provides committee votes and floor votes
    # Floor votes are at: /liz/{session}/Committees/SJUD/FloorVotes
    # But the easiest approach is the measures list with vote data
    chamber = "Senate" if info["chamber"] == "S" else "House"

    # Try to get floor session data
    url = f"{OLIS_API_BASE}/{session}/FloorSessions/{chamber}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return [{
            "member": member_name,
            "session": session,
            "chamber": chamber,
            "source_url": url,
            "note": "Raw HTML — needs parsing for vote extraction",
            "html_length": len(response.text),
        }]
    except requests.RequestException as e:
        return [{"error": str(e), "member": member_name}]


def download_oregon_bill(bill_id: str, session: str = CURRENT_SESSION):
    """Download an Oregon bill's document.

    Saves to data/bills/oregon/
    """
    output_dir = DATA_DIR / "bills" / "oregon"
    output_dir.mkdir(parents=True, exist_ok=True)

    url = fetch_oregon_bill_text_url(bill_id, session)
    print(f"Downloading {bill_id} from {url}...")

    response = requests.get(url)
    response.raise_for_status()

    # OLIS serves PDFs for measure documents
    content_type = response.headers.get("Content-Type", "")
    if "pdf" in content_type:
        ext = "pdf"
    elif "html" in content_type:
        ext = "html"
    else:
        ext = "pdf"  # Default assumption

    filepath = output_dir / f"{bill_id}_{session}.{ext}"
    with open(filepath, "wb") as f:
        f.write(response.content)

    print(f"  Saved to {filepath} ({len(response.content)} bytes)")
    return filepath


# Key Oregon bills for Todd's priorities
OREGON_KEY_BILLS = [
    ("HB3391", "2025R1", "RCV Study Bill"),
    ("SB1507", "2025R1", "Tax Code Disconnect"),
    ("SB1511", "2025R1", "Estate Tax Increase"),
]


def download_oregon_key_bills():
    """Download all Oregon bills relevant to Todd's advocacy."""
    for bill_id, session, description in OREGON_KEY_BILLS:
        print(f"\n--- {description} ({bill_id}) ---")
        try:
            download_oregon_bill(bill_id, session)
        except Exception as e:
            print(f"  Error: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "bills":
        download_oregon_key_bills()
    else:
        print("Usage:")
        print("  python -m src.oregon_api bills   -- Download key Oregon bill texts")
