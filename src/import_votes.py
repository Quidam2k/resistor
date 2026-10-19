"""Import downloaded voting records into the SQLite database."""

import argparse
import json
import time
from pathlib import Path

import requests

from src.config import DATA_DIR
from src.congress_api import MEMBER_IDS, _congress_get
from src.db import get_connection, init_db

PACE_SECONDS = 1.0  # ~3,600 calls/hr, under the 5k/hr Congress.gov key limit


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


HOUSE_CACHE_DIR = DATA_DIR / "cache"  # not VOTES_DIR: import_all() globs that


def _get_with_backoff(endpoint: str, params: dict = None, tries: int = 5) -> dict:
    """_congress_get paced for the 5k/hr key limit, backing off on 429/5xx."""
    for attempt in range(tries):
        try:
            time.sleep(PACE_SECONDS)
            return _congress_get(endpoint, dict(params or {}))
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", 0)
            if (code != 429 and code < 500) or attempt == tries - 1:
                raise
            time.sleep(30 * 2 ** attempt)


def fetch_house_votes(rep: str, congress: int = 119) -> list[dict]:
    """Every House roll call for `rep` in `congress`, shaped for voting_records.

    Uses Congress.gov /house-vote (GovTrack is 403). Cached to HOUSE_CACHE_DIR
    so a later import makes no API calls. Roll calls she missed are skipped.
    """
    cache = HOUSE_CACHE_DIR / f"{rep.lower().replace(' ', '_')}_house_{congress}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    bioguide = MEMBER_IDS[rep]["bioguide"]
    rows = []
    for session in (1, 2):
        offset = 0
        while True:
            page = _get_with_backoff(f"house-vote/{congress}/{session}",
                                     {"offset": offset, "limit": 250})
            votes = page.get("houseRollCallVotes", [])
            for v in votes:
                members = _get_with_backoff(
                    f"house-vote/{congress}/{session}/{v['rollCallNumber']}/members",
                    {"limit": 500})["houseRollCallVoteMemberVotes"]
                rows.extend(house_vote_rows(rep, bioguide, members, session))
            offset += len(votes)
            if not votes or not page.get("pagination", {}).get("next"):
                break
    HOUSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def house_vote_rows(rep: str, bioguide: str, vote: dict, session: int) -> list[dict]:
    """voting_records row(s) for `bioguide` from one /members payload."""
    bill = None
    if vote.get("legislationType") and vote.get("legislationNumber"):
        bill = f"{vote['legislationType']}.{vote['legislationNumber']}"
    return [{"representative": rep, "vote_date": vote.get("startDate", ""),
             "bill_id": bill, "bill_title": vote.get("voteQuestion"),
             "vote": m.get("voteCast", ""), "chamber": "House",
             "session": str(session), "source_url": vote.get("sourceDataURL")}
            for m in vote.get("results", []) if m.get("bioguideID") == bioguide]


def replace_with_house_votes(rep: str, rows: list[dict], apply: bool = False) -> dict:
    """Replace ALL of `rep`'s rows (the misfiled Senate ones) with House rows, in ONE
    transaction. Without apply=True it only counts (nothing is written)."""
    conn = get_connection()
    try:
        # All of rep's rows go, so a re-apply replaces instead of duplicating.
        doomed = conn.execute(
            "SELECT COUNT(*) FROM voting_records WHERE representative=?",
            (rep,)).fetchone()[0]
        result = {"delete": doomed, "insert": len(rows), "applied": False}
        if apply and not rows:
            raise ValueError(f"refusing to delete {rep}'s rows with nothing to import")
        if apply:
            with conn:  # commits both or neither
                conn.execute("DELETE FROM voting_records WHERE representative=?", (rep,))
                conn.executemany(
                    "INSERT INTO voting_records (representative, vote_date, bill_id, "
                    "bill_title, vote, chamber, session, source_url) VALUES "
                    "(:representative, :vote_date, :bill_id, :bill_title, :vote, "
                    ":chamber, :session, :source_url)", rows)
            result["applied"] = True
        return result
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Import voting records.")
    sub = p.add_subparsers(dest="cmd")
    h = sub.add_parser("house-dryrun", help="count/sample a House re-import (no writes)")
    h.add_argument("rep")
    h.add_argument("--apply", action="store_true",
                   help="ACTUALLY delete the Senate rows and import (needs Todd's yes)")
    args = p.parse_args(argv)
    if args.cmd != "house-dryrun":
        return import_all()
    rows = fetch_house_votes(args.rep)
    conn = get_connection()
    old = [dict(r) for r in conn.execute(
        "SELECT vote_date, bill_id, vote, chamber FROM voting_records "
        "WHERE representative=? AND chamber='Senate' ORDER BY vote_date LIMIT 3",
        (args.rep,))]
    conn.close()
    result = replace_with_house_votes(args.rep, rows, apply=args.apply)
    print(f"{'APPLIED' if result['applied'] else 'DRY RUN'}: delete {result['delete']} "
          f"existing rows, insert {result['insert']} House rows for {args.rep}")
    print("  would delete, e.g.:", *old, sep="\n    ")
    print("  would insert, e.g.:", *rows[:3], sep="\n    ")
    return result


if __name__ == "__main__":
    main()
