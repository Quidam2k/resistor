"""Scoreboard: auto-check whether reps acted on our asks, and surface replies.

Two loops:
  1. Outcome auto-check - for each open ask tied to a bill, poll Congress.gov and
     mark it `landed` the moment the rep sponsors OR cosponsors.
  2. Response capture - Claude runs the Gmail connector against the sender filters
     from response_search_queries() and logs genuine replies via db.record_response().

CLI: `python -m src.tracker` prints the scoreboard.
"""

from datetime import datetime

from src import db
from src.congress_api import MEMBER_IDS, _congress_get, fetch_bill_info

# Bioguide IDs for the reps we track asks against. Derived from congress_api's
# MEMBER_IDS so there is a single source of truth (and no drift, e.g. Hoyle's ID).
# FUTURE: fold these into config/representatives.yaml so non-federal reps and new
# members don't require a code change.
REP_BIOGUIDE = {name: info["bioguide"] for name, info in MEMBER_IDS.items()}

# Sender domains for pulling replies via the Gmail connector. State/local reps
# with direct email can be added here as we start corresponding with them.
RESPONSE_SENDERS = [
    "wyden.senate.gov",
    "merkley.senate.gov",
    "hoyle.house.gov",
]

# Subject-line fingerprints of GENUINE casework replies, learned from the
# archive (2023-2024). Sender alone does NOT separate replies from newsletters:
# Merkley sends both his mass newsletters AND his substantive topic-specific
# replies from the SAME address (Senator_Merkley@merkley.senate.gov). The real
# signal is the subject:
#   - Merkley substantive: "In response to your message about <topic>",
#     "Responding to your message ...", "Regarding your message about <topic>".
#   - Wyden: a generic form receipt titled "Thank You for Contacting Me"
#     ("your individualized response may be delayed") -- proof of receipt, NOT
#     a position. Log it as acknowledged, don't quote it as his view.
# Everything else from these domains ("Jeff Around Oregon", fundraising, event
# RSVPs, breaking-news blasts) is a newsletter -- skip it.
RESPONSE_SUBJECT_HINTS = [
    "in response to your",
    "responding to your message",
    "regarding your message",
    "thank you for contacting",
]


# ---------------------------------------------------------------------------
# A. Outcome auto-check
# ---------------------------------------------------------------------------

def fetch_bill_supporters(congress: int, bill_type: str,
                          number: int) -> dict[str, str]:
    """Return {bioguideId: fullName} for a bill's sponsor(s) and cosponsors.

    One call for the bill (sponsors) + one for cosponsors. bioguideId is the
    robust match key - a rep counts as having acted whether they sponsored or
    cosponsored.
    """
    supporters: dict[str, str] = {}

    info = fetch_bill_info(bill_type, number, congress)
    for sponsor in info.get("bill", {}).get("sponsors", []):
        bioguide = sponsor.get("bioguideId")
        if bioguide:
            supporters[bioguide] = sponsor.get("fullName", "")

    data = _congress_get(f"bill/{congress}/{bill_type}/{number}/cosponsors")
    for cosponsor in data.get("cosponsors", []):
        bioguide = cosponsor.get("bioguideId")
        if bioguide:
            supporters[bioguide] = cosponsor.get("fullName", "")

    return supporters


def check_open_asks(update: bool = True) -> list[dict]:
    """Check every open ask against the live bill data.

    For each ask, stamp last_checked/last_status_note; if the rep's bioguide
    appears among the bill's supporters, flip status to 'landed' with a
    landed_date. Returns report rows (dicts) describing each ask's outcome.
    """
    now = datetime.now().isoformat()
    today = now[:10]
    report = []

    # Cache supporters per bill so multiple asks on the same bill hit the API once.
    supporters_cache: dict[tuple, dict] = {}

    for ask in db.get_open_asks():
        bill_label = f"{ask['bill_type'].upper()}.{ask['bill_number']}"
        bioguide = REP_BIOGUIDE.get(ask["rep"])

        if not bioguide:
            note = f"No bioguide known for {ask['rep']}; cannot auto-check."
            if update:
                db.update_ask_status(ask["id"], last_checked=now,
                                     last_status_note=note)
            report.append({**ask, "landed": False, "note": note})
            continue

        key = (ask["congress"], ask["bill_type"], ask["bill_number"])
        try:
            if key not in supporters_cache:
                supporters_cache[key] = fetch_bill_supporters(*key)
            supporters = supporters_cache[key]
        except Exception as e:
            note = f"API error checking {bill_label}: {e}"
            if update:
                db.update_ask_status(ask["id"], last_checked=now,
                                     last_status_note=note)
            report.append({**ask, "landed": False, "note": note})
            continue

        landed = bioguide in supporters
        if landed:
            note = f"{ask['rep']} is on {bill_label} as of {today}."
            if update:
                db.update_ask_status(ask["id"], status="landed",
                                     landed_date=today, last_checked=now,
                                     last_status_note=note)
        else:
            note = f"{ask['rep']} NOT yet on {bill_label} (checked {today})."
            if update:
                db.update_ask_status(ask["id"], last_checked=now,
                                     last_status_note=note)

        report.append({**ask, "landed": landed, "note": note})

    return report


# ---------------------------------------------------------------------------
# B. Response capture (Claude-driven via the Gmail connector)
# ---------------------------------------------------------------------------

def response_search_queries(since: str = None) -> list[str]:
    """Gmail search queries Claude should run at session start.

    Returns two queries, both narrowed by the subject fingerprints of real
    casework replies (RESPONSE_SUBJECT_HINTS) so newsletters are excluded up
    front -- a plain `from:<domain>` sweep drowns real replies in ~200
    mailing-list blasts (learned the hard way 2026-08-18):
      [0] the tight query: replies matching a known subject fingerprint.
      [1] a wider safety net: everything from the domains, minus obvious
          newsletter noise, in case a rep uses a new subject format.

    Pass `since` (YYYY/MM/DD, e.g. the last session date) to scope both to
    new mail. For each genuine reply, call db.record_response(letter_id,
    representative, received_date, body) against the matching sent letter
    (match by rep + the nearest prior topic). Wyden's "Thank You for
    Contacting Me" is only a receipt -- log it, but don't quote it as a view.
    """
    senders = " OR ".join(RESPONSE_SENDERS)
    subjects = " OR ".join(f'subject:"{hint}"' for hint in RESPONSE_SUBJECT_HINTS)
    date = f" after:{since}" if since else ""
    tight = f"from:({senders}) AND ({subjects}){date}"
    wide = (f"from:({senders}) -subject:newsletter -subject:town "
            f"-subject:RSVP{date}")
    return [tight, wide]


# ---------------------------------------------------------------------------
# Seed - today's asks (idempotent)
# ---------------------------------------------------------------------------

# (letter_id, rep, congress, bill_type, bill_number, ask_summary, asked_date)
INITIAL_ASKS = [
    (18, "Ron Wyden", 119, "s", 3425, "cosponsor the Ranked Choice Voting Act", "2026-08-18"),
    (19, "Jeff Merkley", 119, "s", 3425, "cosponsor the Ranked Choice Voting Act", "2026-08-18"),
    (20, "Val Hoyle", 119, "hr", 6589, "cosponsor the Ranked Choice Voting Act", "2026-08-18"),
]


def seed_initial_asks() -> int:
    """Seed the three RCV cosponsorship asks. Skips any whose letter already
    has an ask, so re-running is safe. Returns the count newly added."""
    added = 0
    for letter_id, rep, congress, bill_type, number, summary, asked in INITIAL_ASKS:
        if db.ask_exists_for_letter(letter_id):
            print(f"  ask for letter #{letter_id} ({rep}) already exists, skipping")
            continue
        db.add_ask(letter_id, rep, congress, bill_type, number, summary, asked)
        added += 1
        print(f"  added ask: {rep} -> {bill_type.upper()}.{number} (letter #{letter_id})")
    return added


# ---------------------------------------------------------------------------
# C. Scoreboard
# ---------------------------------------------------------------------------

def status_report() -> str:
    """Build the human-readable scoreboard string."""
    lines = []
    today = datetime.now().isoformat()[:10]

    lines.append("=" * 64)
    lines.append(f"RESISTOR SCOREBOARD  ({today})")
    lines.append("=" * 64)

    # 1. Open asks with live status.
    lines.append("")
    lines.append("OPEN ASKS")
    lines.append("-" * 64)
    open_asks = db.get_open_asks()
    if not open_asks:
        lines.append("  (none open)")
    for ask in open_asks:
        bill_label = f"{ask['bill_type'].upper()}.{ask['bill_number']}"
        note = ask.get("last_status_note") or "not yet checked"
        lines.append(f"  {ask['rep']} - {bill_label} - {ask['ask_summary']}")
        lines.append(f"      status: {note}")

    # Landed asks (wins worth showing).
    landed = [a for a in db.get_all_asks() if a["status"] == "landed"]
    if landed:
        lines.append("")
        lines.append("LANDED (rep acted)")
        lines.append("-" * 64)
        for ask in landed:
            bill_label = f"{ask['bill_type'].upper()}.{ask['bill_number']}"
            lines.append(f"  {ask['rep']} - {bill_label} - landed {ask['landed_date']}")

    # 2. Logged responses.
    lines.append("")
    lines.append("RESPONSES LOGGED")
    lines.append("-" * 64)
    responses = db.get_all_responses()
    if not responses:
        lines.append("  (none yet)")
    for r in responses:
        snippet = " ".join((r["body"] or "").split())[:70]
        lines.append(f"  {r['received_date']} - {r['representative']}: {snippet}")

    # 3. Sent letters awaiting a response (possible follow-ups).
    lines.append("")
    lines.append("SENT - NO RESPONSE YET (follow-up candidates)")
    lines.append("-" * 64)
    waiting = db.get_sent_letters_without_response()
    if not waiting:
        lines.append("  (none)")
    for letter in waiting:
        sent = (letter.get("sent_at") or "")[:10]
        lines.append(f"  #{letter['id']} {letter['recipient']} - {letter['topic']} (sent {sent})")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == "seed":
        print("Seeding initial asks...")
        n = seed_initial_asks()
        print(f"Done: {n} added.\n")
    elif cmd == "check":
        # Live API pass, then print the scoreboard.
        print("Checking open asks against Congress.gov...")
        for row in check_open_asks(update=True):
            print(f"  {row['note']}")
        print()

    print(status_report())
