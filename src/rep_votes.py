"""Bill and roll-call collectors for representative records (ticket #5863)."""

import csv
import io
import json
import re
from contextlib import contextmanager

import requests

from src.congress_api import MEMBER_IDS
from src.import_votes import _get_with_backoff, house_vote_rows
from src.config import load_representatives
from src.rep_record import ensure_schema, fetch_text, iter_reps, odata, update_progress

VOTE_COLUMNS = (
    "representative", "vote_date", "bill_id", "bill_title", "vote",
    "chamber", "session", "source_url", "roll_url", "question",
)
BILL_TYPES = dict(zip(
    ("S", "HR", "SRES", "HRES", "SJRES", "HJRES", "SCONRES", "HCONRES"),
    ("senate-bill", "house-bill", "senate-resolution", "house-resolution",
     "senate-joint-resolution", "house-joint-resolution",
     "senate-concurrent-resolution", "house-concurrent-resolution"),
))


def bill_url(congress: int, bill_type: str, number):
    kind = BILL_TYPES.get(bill_type.upper())
    if kind is None:
        return None
    suffix = "th" if 11 <= congress % 100 <= 13 else {
        1: "st", 2: "nd", 3: "rd"}.get(congress % 10, "th")
    return f"https://www.congress.gov/bill/{congress}{suffix}-congress/{kind}/{number}"


def or_measure_url(session, prefix, number):
    return f"https://olis.oregonlegislature.gov/liz/{session}/Measures/Overview/{prefix}{number}"


@contextmanager
def _source(conn, rep, source, result=None):
    """Return per-source errors to the caller's run summary; never retry."""
    ensure_schema(conn)
    if result is None:
        result = {"inserted": 0, "errors": []}
    before = result["inserted"]
    try:
        with conn:
            yield result
    except requests.HTTPError as exc:
        result["inserted"] = before
        result["errors"].append({"rep": rep, "source": source, "error": str(exc)})
        with conn:
            update_progress(conn, rep, source, [], note=f"error: {exc}")


def _bill(conn, rep, session, bill_id, role, title, date, url):
    return conn.execute(
        "INSERT OR IGNORE INTO rep_bills "
        "(rep, session, bill_id, role, title, date, url, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (rep, str(session), bill_id, role, title, date, url),
    ).rowcount


def _vote(conn, row, keys=("representative", "roll_url")):
    where = " AND ".join(f"{key} IS ?" for key in keys)
    if conn.execute(f"SELECT 1 FROM voting_records WHERE {where} LIMIT 1",
                    tuple(row.get(key) for key in keys)).fetchone():
        return 0
    conn.execute(
        f"INSERT INTO voting_records ({', '.join(VOTE_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in VOTE_COLUMNS)})",
        tuple(row.get(key) for key in VOTE_COLUMNS),
    )
    return 1


def _vote_exists(conn, rep_name, url):
    return conn.execute(
        "SELECT 1 FROM voting_records WHERE representative=? AND (roll_url=? OR source_url=?) LIMIT 1",
        (rep_name, url, url)).fetchone() is not None


def collect_federal_bills(conn, rep, since, max_pages=8):
    """Sponsored + cosponsored bills; a career backfill passes a large max_pages."""
    result = {"inserted": 0, "errors": []}
    member = MEMBER_IDS.get(rep["name"])
    if not member:
        return result
    for role, endpoint, key in (
        ("sponsor", "sponsored-legislation", "sponsoredLegislation"),
        ("cosponsor", "cosponsored-legislation", "cosponsoredLegislation"),
    ):
        source, dates, offset = f"bills:congress-{role}", [], 0
        with _source(conn, rep["name"], source, result):
            for _ in range(max_pages):
                page = _get_with_backoff(
                    f"member/{member['bioguide']}/{endpoint}", {"limit": 250, "offset": offset})
                items = page.get(key, [])
                for item in items:
                    date = item.get("introducedDate") or ""
                    if not item.get("type") or date < str(since):
                        continue
                    title = item.get("title") or item.get("latestTitle")  # field name varies by endpoint
                    if isinstance(title, dict):
                        title = title.get("title")
                    added = _bill(
                        conn, rep["name"], item["congress"],
                        f"{item['type']}.{item['number']}", role, title, date,
                        bill_url(int(item["congress"]), item["type"], item["number"]))
                    result["inserted"] += added
                    if added:
                        dates.append(date)
                offset += len(items)
                if (not items or not page.get("pagination", {}).get("next")
                        or all((x.get("introducedDate") or "") < str(since) for x in items)):
                    break
            update_progress(conn, rep["name"], source, dates, cursor=str(offset))
    return result


def _literal(value):
    return str(value).replace("'", "''")


def collect_or_bills(conn, rep, session_key):
    if not rep.get("or_code"):
        return {"inserted": 0, "errors": []}
    source, dates = "bills:oregon", []
    with _source(conn, rep["name"], source) as result:
        sponsors = odata("MeasureSponsors", f"SessionKey eq '{_literal(session_key)}' "
                         f"and LegislatoreCode eq '{_literal(rep['or_code'])}'")
        for sponsor in sponsors:
            if sponsor.get("SponsorLevel") != "Chief":
                continue
            s, p, n = (sponsor[k] for k in ("SessionKey", "MeasurePrefix", "MeasureNumber"))
            bill_id = f"{p} {n}"
            if conn.execute("SELECT 1 FROM rep_bills WHERE rep=? AND session=? AND bill_id=?",
                            (rep["name"], str(s), bill_id)).fetchone():
                continue
            measures = odata("Measures", f"SessionKey eq '{_literal(s)}' "
                             f"and MeasurePrefix eq '{_literal(p)}' and MeasureNumber eq {int(n)}")
            measure = measures[0] if measures else {}
            date = measure.get("IntroducedDate") or measure.get("CreatedDate")
            added = _bill(
                conn, rep["name"], s, bill_id, "chief",
                measure.get("CatchLine") or measure.get("RelatingTo"), date,
                or_measure_url(s, p, n))
            result["inserted"] += added
            if added and date:
                dates.append(date)
        update_progress(conn, rep["name"], source, dates, cursor=str(session_key))
    return result


def collect_or_votes(conn, rep, session_key):
    if not rep.get("or_code"):
        return {"inserted": 0, "errors": []}
    source, dates = "votes:oregon", []
    with _source(conn, rep["name"], source) as result:
        votes = odata("MeasureVotes", f"SessionKey eq '{_literal(session_key)}' "
                      f"and VoteName eq '{_literal(rep['or_code'])}'")
        for v in votes:
            s = v.get("SessionKey", session_key)
            row = dict(zip(VOTE_COLUMNS, (
                rep["name"], v["ActionDate"], f"{v['MeasurePrefix']} {v['MeasureNumber']}",
                None, v["Vote"], "OR_Senate" if v["Chamber"] == "S" else "OR_House",
                str(s), or_measure_url(s, v["MeasurePrefix"], v["MeasureNumber"]),
                None, v.get("ActionText"))))
            if _vote(conn, row, ("representative", "bill_id", "vote_date", "question")):
                result["inserted"] += 1
                dates.append(v["ActionDate"])
        update_progress(conn, rep["name"], source, dates, cursor=str(session_key))
    return result


def collect_house_votes(conn, rep, congress=119):
    member = MEMBER_IDS.get(rep["name"], {})
    if member.get("chamber") != "house":
        return {"inserted": 0, "errors": []}
    source, dates = "votes:congress-house", []
    with _source(conn, rep["name"], source) as result:
        existing = {r[0] for r in conn.execute(
            "SELECT source_url FROM voting_records WHERE representative=?", (rep["name"],))}
        for session in (1, 2):
            offset = 0
            while True:
                endpoint = f"house-vote/{congress}/{session}"
                page = _get_with_backoff(endpoint, {"limit": 250, "offset": offset})
                votes = page.get("houseRollCallVotes", [])
                for v in votes:
                    url = v["sourceDataURL"]
                    if url in existing:
                        continue
                    members = _get_with_backoff(
                        f"{endpoint}/{v['rollCallNumber']}/members", {"limit": 500})
                    rows = house_vote_rows(
                        rep["name"], member["bioguide"], members["houseRollCallVoteMemberVotes"], session)
                    for row in rows:
                        row.update(source_url=url, roll_url=url, question=v.get("voteQuestion"))
                        if _vote(conn, row):
                            result["inserted"] += 1
                            dates.append(row["vote_date"])
                    if rows:
                        existing.add(url)
                offset += len(votes)
                if not votes or not page.get("pagination", {}).get("next"):
                    break
        update_progress(conn, rep["name"], source, dates, cursor=str(congress))
    return result


def import_senate_votes(conn, path):
    """Validate the entire offline file before making any database changes."""
    with open(path, encoding="utf-8") as handle:
        entries = json.load(handle)
    senators = {r["name"] for r in iter_reps(load_representatives())
                if MEMBER_IDS.get(r["name"], {}).get("chamber") == "senate"}
    if not isinstance(entries, list):
        raise ValueError("Senate import must be a list")
    rows = []
    for v in entries:
        if (not isinstance(v, dict) or not isinstance(v.get("rep"), str)
                or v["rep"] not in senators
                or v.get("vote") not in ("Yea", "Nay", "Not Voting", "Present")
                or not isinstance(v.get("url"), str)
                or not v["url"].startswith("https://www.senate.gov/legislative/LIS/roll_call_votes/")):
            raise ValueError("Invalid Senate representative, vote, or official URL")
        match = re.search(r"/vote(\d+)([12])/", v["url"])
        if not match or not v.get("date"):
            raise ValueError("Missing Senate session or vote date")
        rows.append(dict(zip(VOTE_COLUMNS, (
            v["rep"], v["date"], v.get("measure"), v.get("title"), v["vote"],
            "Senate", f"{match[1]}-{match[2]}", v["url"], v["url"], v.get("question")))))
    ensure_schema(conn)
    with conn:
        return {"inserted": sum(_vote(conn, row) for row in rows), "errors": []}


def _n(value):
    """Voteview CSVs write integer ids as floats ("10713.0")."""
    return int(float(value))


def _session(congress, roll):
    """Voteview leaves session blank before the 101st; derive it from the year.
    Approximate: right for the modern two-session Congress we import (1981 on); a
    Jan 1-3 lame-duck vote stays in the second session. Pre-1940s third sessions are not handled."""
    if roll.get("session"):
        return _n(roll["session"])
    return min(int(roll["date"][:4]) - (1787 + 2 * congress) + 1, 2)


def _official_roll(chamber, congress, roll):
    """Official roll-call page, or None where none exists online
    (senate.gov LIS starts with the 101st Congress, the Clerk's pages with 1990)."""
    number = roll.get("clerk_rollnumber")
    if not number:
        return None
    session = _session(congress, roll)
    if (chamber == "S" and congress < 101) or (chamber == "H" and roll["date"][:4] < "1990"):
        return None
    if chamber == "S":
        return ("https://www.senate.gov/legislative/LIS/roll_call_votes/"
                f"vote{congress}{session}/vote_{congress}_{session}_{_n(number):05d}.htm")
    return f"https://clerk.house.gov/Votes/{roll['date'][:4]}{_n(number)}"


def collect_voteview(conn, rep, chamber, congresses, icpsr):
    letter = {"S": "S", "SENATE": "S", "H": "H", "HOUSE": "H"}.get(chamber.upper())
    if letter is None:
        raise ValueError("chamber must be Senate/S or House/H")
    chamber = "Senate" if letter == "S" else "House"
    source = f"votes:voteview-{chamber.lower()}"
    congresses = sorted({int(n) for n in congresses}, reverse=True)
    icpsr = _n(icpsr)
    with _source(conn, rep["name"], source) as result:
        for congress in congresses:
            dates = []
            base = "https://voteview.com/static/data/out"
            tag = f"{letter}{congress:03d}"
            rolls = {_n(r["rollnumber"]): r for r in csv.DictReader(io.StringIO(
                fetch_text(f"{base}/rollcalls/{tag}_rollcalls.csv")))}
            votes = csv.DictReader(io.StringIO(
                fetch_text(f"{base}/votes/{tag}_votes.csv")))
            for v in votes:
                if _n(v["icpsr"]) != icpsr:
                    continue
                cast = _n(v["cast_code"])
                if cast == 0:
                    continue
                # Voteview cast codes: 1-3 yea, 4-6 nay, 7-8 present, 9 not voting.
                vote = {1: "Yea", 2: "Yea", 3: "Yea", 4: "Nay", 5: "Nay",
                        6: "Nay", 7: "Present", 8: "Present", 9: "Not Voting"}.get(cast)
                if vote is None:
                    continue
                number = _n(v["rollnumber"])
                roll = rolls[number]
                url = f"https://voteview.com/rollcall/R{tag}{number:04d}"
                row = dict(zip(VOTE_COLUMNS, (
                    rep["name"], roll["date"], roll.get("bill_number") or None,
                    roll.get("vote_desc"), vote, chamber, f"{congress}-{_session(congress, roll)}",
                    url, _official_roll(letter, congress, roll), roll.get("vote_question"))))
                if row["roll_url"] and _vote_exists(conn, rep["name"], row["roll_url"]):
                    continue  # already stored from senate.gov or the Clerk
                if _vote(conn, row, ("representative", "source_url")):
                    result["inserted"] += 1
                    dates.append(roll["date"])
            update_progress(conn, rep["name"], source, dates, cursor=str(congress),
                            exhausted=int(congress == congresses[-1]), note="")  # clears a stale error
    return result


def congress_for_year(year):
    return (int(year) - 1789) // 2 + 1


def collect_senate_voteview(conn, rep, congresses):
    return collect_voteview(conn, rep, "Senate", congresses, rep["voteview_icpsr"])
