"""Fetch voting records and bill text from Congress.gov API and GovTrack."""

import json
import time
from datetime import datetime
from pathlib import Path

import requests

from src.config import get_env, DATA_DIR


CONGRESS_API_BASE = "https://api.congress.gov/v3"
GOVTRACK_API_BASE = "https://www.govtrack.us/api/v2"

# Congress.gov bioguide IDs for Todd's representatives
MEMBER_IDS = {
    "Ron Wyden": {"bioguide": "W000779", "chamber": "senate"},
    "Jeff Merkley": {"bioguide": "M001176", "chamber": "senate"},
    "Val Hoyle": {"bioguide": "H001096", "chamber": "house"},
}

# GovTrack person IDs
GOVTRACK_IDS = {
    "Ron Wyden": 300100,
    "Jeff Merkley": 412325,
    "Val Hoyle": 456858,
}


def _congress_get(endpoint: str, params: dict = None) -> dict:
    """Make an authenticated request to the Congress.gov API."""
    api_key = get_env("CONGRESS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "CONGRESS_API_KEY not set. Get a free key at api.congress.gov/sign-up"
        )

    if params is None:
        params = {}
    params["api_key"] = api_key
    params.setdefault("format", "json")
    params.setdefault("limit", 250)

    url = f"{CONGRESS_API_BASE}/{endpoint}"
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def _govtrack_get(endpoint: str, params: dict = None) -> dict:
    """Make a request to the GovTrack API (no key needed)."""
    if params is None:
        params = {}

    url = f"{GOVTRACK_API_BASE}/{endpoint}"
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Member voting records via Congress.gov
# ---------------------------------------------------------------------------

def fetch_member_votes(member_name: str, congress: int = 119,
                       offset: int = 0) -> list[dict]:
    """Fetch roll call votes for a member from Congress.gov.

    Args:
        member_name: e.g. "Ron Wyden"
        congress: Congress number (119 = 2025-2027)
        offset: Pagination offset

    Returns:
        List of vote records
    """
    info = MEMBER_IDS.get(member_name)
    if not info:
        raise ValueError(f"Unknown member: {member_name}. Known: {list(MEMBER_IDS.keys())}")

    bioguide = info["bioguide"]
    endpoint = f"member/{bioguide}/votes"
    data = _congress_get(endpoint, {"offset": offset})
    return data.get("votes", [])


def fetch_all_member_votes(member_name: str, congress: int = 119) -> list[dict]:
    """Fetch ALL votes for a member, paginating through results."""
    all_votes = []
    offset = 0

    while True:
        votes = fetch_member_votes(member_name, congress, offset)
        if not votes:
            break
        all_votes.extend(votes)
        print(f"  Fetched {len(all_votes)} votes for {member_name}...")
        offset += 250
        time.sleep(0.5)  # Be nice to the API

    return all_votes


# ---------------------------------------------------------------------------
# Member voting records via GovTrack (backup / richer data)
# ---------------------------------------------------------------------------

def fetch_govtrack_votes(member_name: str, congress: int = 119,
                         since: str = None) -> list[dict]:
    """Fetch votes from GovTrack API (no API key needed).

    GovTrack provides richer context including vote descriptions and bill info.
    Filter by congress year range since GovTrack doesn't support congress param.
    """
    person_id = GOVTRACK_IDS.get(member_name)
    if not person_id:
        raise ValueError(f"Unknown member: {member_name}")

    # Map congress number to date range
    # 119th Congress: Jan 3, 2025 - Jan 3, 2027
    congress_start_years = {
        119: "2025-01-03",
        118: "2023-01-03",
        117: "2021-01-03",
    }
    start_date = since or congress_start_years.get(congress, "2025-01-03")

    all_votes = []
    offset = 0

    while True:
        data = _govtrack_get("vote_voter", {
            "person": person_id,
            "created__gte": start_date,
            "limit": 100,
            "offset": offset,
            "order_by": "-created",
        })

        objects = data.get("objects", [])
        if not objects:
            break

        all_votes.extend(objects)
        print(f"  Fetched {len(all_votes)} GovTrack votes for {member_name}...")

        if offset + 100 >= data.get("meta", {}).get("total_count", 0):
            break
        offset += 100
        time.sleep(0.5)

    return all_votes


# ---------------------------------------------------------------------------
# Bill text
# ---------------------------------------------------------------------------

def fetch_bill_info(bill_type: str, bill_number: int,
                    congress: int = 119) -> dict:
    """Fetch bill information from Congress.gov.

    Args:
        bill_type: "s" (Senate), "hr" (House), "sjres", "hjres", etc.
        bill_number: The bill number
        congress: Congress number

    Returns:
        Bill metadata dict
    """
    endpoint = f"bill/{congress}/{bill_type}/{bill_number}"
    return _congress_get(endpoint)


def fetch_bill_text_url(bill_type: str, bill_number: int,
                        congress: int = 119) -> str | None:
    """Get the URL to the bill text from Congress.gov.

    Returns URL to the text version, or None if not available.
    """
    endpoint = f"bill/{congress}/{bill_type}/{bill_number}/text"
    data = _congress_get(endpoint)

    text_versions = data.get("textVersions", [])
    if not text_versions:
        return None

    # Get the most recent version
    latest = text_versions[0]
    formats = latest.get("formats", [])

    # Prefer plain text, then HTML, then PDF
    for fmt in formats:
        if fmt.get("type") == "Formatted Text":
            return fmt.get("url")
    for fmt in formats:
        if fmt.get("type") == "HTML":
            return fmt.get("url")
    for fmt in formats:
        if fmt.get("type") == "PDF":
            return fmt.get("url")

    return None


def fetch_bill_text(bill_type: str, bill_number: int,
                    congress: int = 119) -> str | None:
    """Download the actual text of a bill.

    Returns the bill text as a string, or None if unavailable.
    """
    url = fetch_bill_text_url(bill_type, bill_number, congress)
    if not url:
        return None

    api_key = get_env("CONGRESS_API_KEY")
    response = requests.get(url, params={"api_key": api_key})
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# Bulk download and storage
# ---------------------------------------------------------------------------

def download_all_voting_records(congress: int = 119):
    """Download voting records for all of Todd's federal representatives.

    Saves to data/voting_records/ as JSON files.
    """
    output_dir = DATA_DIR / "voting_records"
    output_dir.mkdir(exist_ok=True)

    for member_name in MEMBER_IDS:
        print(f"\nFetching votes for {member_name}...")

        # Try Congress.gov first
        try:
            votes = fetch_all_member_votes(member_name, congress)
            source = "congress_gov"
        except Exception as e:
            print(f"  Congress.gov failed ({e}), trying GovTrack...")
            try:
                votes = fetch_govtrack_votes(member_name, congress)
                source = "govtrack"
            except Exception as e2:
                print(f"  GovTrack also failed: {e2}")
                continue

        # Save to file
        safe_name = member_name.lower().replace(" ", "_").replace(".", "")
        filename = f"{safe_name}_votes_{congress}th.json"
        filepath = output_dir / filename

        record = {
            "member": member_name,
            "congress": congress,
            "source": source,
            "fetched_at": datetime.now().isoformat(),
            "total_votes": len(votes),
            "votes": votes,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, default=str)

        print(f"  Saved {len(votes)} votes to {filepath}")

    print("\nDone!")


def download_bill(bill_type: str, bill_number: int, congress: int = 119):
    """Download bill info and text, save locally.

    Example: download_bill("s", 2845) for S. 2845
    """
    output_dir = DATA_DIR / "bills"
    output_dir.mkdir(exist_ok=True)

    print(f"Fetching bill {bill_type.upper()}.{bill_number}...")

    info = fetch_bill_info(bill_type, bill_number, congress)
    text = fetch_bill_text(bill_type, bill_number, congress)

    filename = f"{bill_type}{bill_number}_{congress}th"

    # Save metadata
    with open(output_dir / f"{filename}_info.json", "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, default=str)

    # Save text if available
    if text:
        with open(output_dir / f"{filename}_text.txt", "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  Saved bill text ({len(text)} chars)")
    else:
        print("  Bill text not available")

    print(f"  Saved to {output_dir / filename}_*")
    return info


# ---------------------------------------------------------------------------
# Convenience: download key bills for Todd's advocacy topics
# ---------------------------------------------------------------------------

KEY_BILLS = [
    # Billionaire taxation
    ("s", 2845, 119, "Billionaires Income Tax Act (Wyden)"),
    ("hr", 5427, 119, "Billionaires Income Tax Act (House companion)"),
    # PRO Act
    ("hr", 20, 119, "PRO Act (House)"),
    ("s", 852, 119, "PRO Act (Senate)"),
    # Ranked Choice Voting
    ("hr", 6589, 119, "Ranked Choice Voting Act (House)"),
    ("s", 3425, 119, "Ranked Choice Voting Act (Senate)"),
    # Antitrust
    # (add Wyden's antitrust bill number when identified)
]


def download_key_bills():
    """Download all bills relevant to Todd's advocacy priorities."""
    for bill_type, number, congress, description in KEY_BILLS:
        print(f"\n--- {description} ---")
        try:
            download_bill(bill_type, number, congress)
        except Exception as e:
            print(f"  Error: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "bills":
        download_key_bills()
    elif len(sys.argv) > 1 and sys.argv[1] == "votes":
        download_all_voting_records()
    else:
        print("Usage:")
        print("  python -m src.congress_api votes   -- Download voting records")
        print("  python -m src.congress_api bills   -- Download key bill texts")
        print()
        print("Requires CONGRESS_API_KEY in .env (free at api.congress.gov/sign-up)")
