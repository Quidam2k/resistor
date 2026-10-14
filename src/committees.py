"""Committee assignments + upcoming hearings/markups for Todd's reps.

Floor votes are the worst moment to write (positions are locked). Leverage is
before a markup or hearing in a committee the rep sits on. This module answers
"which of my reps' committees meet soon, and on what?" for session openers.

Sources (all read-only, cached via src.http_cache):
- Committee membership: unitedstates/congress-legislators (CC0), no key.
- House meetings: Congress.gov committee-meeting API (CONGRESS_API_KEY).
- Senate meetings: senate.gov committee_schedules/hearings.xml, no key.

Committee codes are normalized to thomas_id form: "HSII" for a full committee,
"HSII13" for a subcommittee. Congress.gov systemCodes ("hsii13", "hlzs00") and
senate.gov cmte_codes ("SSFI00") map onto these by upper-casing and dropping
a trailing "00".
"""

import argparse
import xml.etree.ElementTree as ET
from datetime import date, datetime

import yaml

from src.config import get_env
from src.congress_api import CONGRESS_API_BASE, MEMBER_IDS
from src.http_cache import cached_get

LEGISLATORS_BASE = "https://unitedstates.github.io/congress-legislators"
SENATE_HEARINGS_URL = "https://www.senate.gov/general/committee_schedules/hearings.xml"

ROSTER_TTL = 7 * 24 * 3600
SCHEDULE_TTL = 6 * 3600
CURRENT_CONGRESS = 119


def normalize_code(code: str) -> str:
    code = (code or "").strip().upper()
    if len(code) == 6 and code.endswith("00"):
        code = code[:4]
    return code


def _load_yaml(name: str):
    text = cached_get(f"{LEGISLATORS_BASE}/{name}", ttl_seconds=ROSTER_TTL, as_text=True)
    return yaml.safe_load(text)


def committee_names() -> dict[str, str]:
    """thomas_id -> display name, subcommittees as 'Committee: Subcommittee'."""
    names = {}
    for c in _load_yaml("committees-current.yaml"):
        names[c["thomas_id"]] = c["name"]
        for s in c.get("subcommittees", []):
            names[c["thomas_id"] + s["thomas_id"]] = f"{c['name']}: {s['name']}"
    return names


def rep_committees(members: dict = None) -> dict[str, list[dict]]:
    """Rep name -> [{code, name, title}] from the CC0 membership roster."""
    members = members or MEMBER_IDS
    by_bioguide = {v["bioguide"]: name for name, v in members.items()}
    names = committee_names()
    result = {name: [] for name in members}
    for code, roster in _load_yaml("committee-membership-current.yaml").items():
        for m in roster:
            rep = by_bioguide.get(m.get("bioguide"))
            if rep:
                result[rep].append({"code": code, "name": names.get(code, code),
                                    "title": m.get("title")})
    return result


def house_meetings(today: date = None, max_details: int = 40) -> list[dict]:
    """Upcoming House committee meetings (hearings and markups).

    The list endpoint returns only ids sorted by last update, so each meeting
    costs one detail call; max_details caps that, and details are cached.
    """
    today = today or date.today()
    api_key = get_env("CONGRESS_API_KEY")
    if not api_key:
        raise RuntimeError("CONGRESS_API_KEY not set in .env")
    params = {"api_key": api_key, "format": "json", "limit": max_details}
    listing = cached_get(f"{CONGRESS_API_BASE}/committee-meeting/{CURRENT_CONGRESS}/house",
                         params, ttl_seconds=SCHEDULE_TTL)
    meetings = []
    for item in listing.get("committeeMeetings", [])[:max_details]:
        detail = cached_get(item["url"].split("?")[0], {"api_key": api_key, "format": "json"},
                            ttl_seconds=SCHEDULE_TTL).get("committeeMeeting", {})
        when = detail.get("date")
        if not when or datetime.fromisoformat(when.replace("Z", "+00:00")).date() < today:
            continue
        meetings.append({
            "chamber": "House",
            "date": when[:10],
            "time": when[11:16],
            "type": detail.get("type"),
            "status": detail.get("meetingStatus"),
            "title": detail.get("title"),
            "codes": [normalize_code(c.get("systemCode")) for c in detail.get("committees", [])],
            "url": f"https://www.congress.gov/event/{CURRENT_CONGRESS}th-Congress/house-event/{detail.get('eventId')}",
        })
    return meetings


def senate_meetings(today: date = None) -> list[dict]:
    """Upcoming Senate committee meetings from senate.gov's schedule XML."""
    today = today or date.today()
    root = ET.fromstring(cached_get(SENATE_HEARINGS_URL, ttl_seconds=SCHEDULE_TTL,
                                    as_text=True).encode("utf-8"))
    meetings = []
    for m in root.iter("meeting"):
        code = normalize_code(m.findtext("cmte_code"))
        when = m.findtext("date_iso_8601")
        if not code or not when or date.fromisoformat(when) < today:
            continue  # the feed emits a placeholder row when nothing is scheduled
        meetings.append({
            "chamber": "Senate",
            "date": when,
            "time": (m.findtext("time") or "")[:5],
            "type": m.findtext("type"),
            "status": None,
            "title": (m.findtext("matter") or "").strip(),
            "codes": [code],
            "committee": m.findtext("committee"),
            "url": "https://www.senate.gov/committees/hearings_meetings.htm",
        })
    return meetings


def leverage_windows(today: date = None, house_max_details: int = 40) -> list[dict]:
    """Upcoming meetings of committees (or their parents) a rep sits on, soonest first."""
    today = today or date.today()
    assignments = rep_committees()
    names = committee_names()
    meetings = senate_meetings(today) + house_meetings(today, house_max_details)

    windows = []
    for meeting in meetings:
        for rep, committees in assignments.items():
            chamber = MEMBER_IDS[rep]["chamber"]
            if meeting["chamber"].lower() != chamber:
                continue
            seats = {c["code"]: c for c in committees}
            for code in meeting["codes"]:
                seat = seats.get(code) or seats.get(code[:4])
                if seat:
                    windows.append({**meeting, "rep": rep, "committee_code": code,
                                    "committee": names.get(code, code),
                                    "rep_title": seat.get("title")})
                    break
    windows.sort(key=lambda w: (w["date"], w["time"] or ""))
    return windows


def format_window(w: dict) -> str:
    role = f" ({w['rep_title']})" if w.get("rep_title") else ""
    kind = (w.get("type") or "meeting").lower()
    return (f"{w['date']} {w['time'] or ''}  {w['rep']}{role} sits on {w['committee']}: "
            f"{kind} - {w['title']}").replace("  ", " ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Committee leverage windows")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("assignments")
    sub.add_parser("upcoming")
    args = parser.parse_args()

    if args.cmd == "assignments":
        for rep, committees in rep_committees().items():
            print(rep)
            for c in committees:
                print(f"  {c['code']:<7} {c['name']}" + (f" ({c['title']})" if c["title"] else ""))
    else:
        windows = leverage_windows()
        if not windows:
            print("No upcoming meetings found for your reps' committees.")
        for w in windows:
            print(format_window(w).encode("ascii", "replace").decode())
