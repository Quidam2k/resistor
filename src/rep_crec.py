"""Collect Congressional Record speaker turns from GovInfo."""

import html
import re
import time

import requests

from src.config import get_env
from src.rep_record import (
    PACE_SECONDS,
    _save_statement,
    normalize,
    update_progress,
    verify_quote,
)

_SOURCE = "statements:crec"
_SPEAKER = re.compile(
    r"\n {2}((?:Mr|Ms|Mrs|Miss)\. [^\n.]+"
    r"|The [A-Z][A-Z ]*(?: pro tempore)?)\.[ \t]*"
)
_PAGE = re.compile(r"\[\[Page [^\]]*\]\]")
_VENUES = {"PgS": "Senate floor", "PgH": "House floor", "PgE": "Extensions of Remarks"}


def clean_text(raw: str) -> str:
    """Granule html as plain text: tags stripped, entities unescaped, page markers dropped."""
    return "\n" + _PAGE.sub(" ", html.unescape(re.sub(r"<[^>]+>", "", raw)))


def speaker_segments(text: str, surname: str) -> list[str]:
    """Return this member's turns, excluding speaker labels and page markers."""
    text = clean_text(text)
    target = re.compile(
        r"(?:Mr|Ms|Mrs|Miss)\. " + re.escape(surname.upper())
        + r"(?: of [A-Za-z]+(?: [A-Za-z]+)*)?"
    )
    markers = [
        match for match in _SPEAKER.finditer(text)
        if match[1].startswith("The ")
        or match[1].split(". ", 1)[1].split(" of ", 1)[0].isupper()
    ]
    segments = []
    for index, marker in enumerate(markers):
        if not target.fullmatch(marker[1]):
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segment = normalize(text[marker.end():end])
        if segment:
            segments.append(segment)
    return segments


def search_year(surname, year, *, post=requests.post) -> list[dict]:
    """Fetch all search pages for one surname and publication year."""
    key = get_env("CONGRESS_API_KEY")
    if not key:
        raise RuntimeError("CONGRESS_API_KEY is required")
    results, offset = [], "*"
    while True:
        response = post(
            f"https://api.govinfo.gov/search?api_key={key}",
            json={
                "query": (
                    f"collection:CREC AND member:{surname} AND "
                    f"publishdate:range({year}-01-01,{year}-12-31)"
                ),
                "pageSize": 100,
                "offsetMark": offset,
                "sorts": [{"field": "publishdate", "sortOrder": "DESC"}],
            },
            timeout=60,
        )
        response.raise_for_status()
        page = response.json()
        items = page.get("results", [])
        results.extend(items)
        next_offset = page.get("offsetMark")
        if not items or not next_offset or next_offset == offset:
            return results
        offset = next_offset
        time.sleep(PACE_SECONDS)


def fetch_granule(url: str) -> str:
    """Granule text, uncached (a career is thousands of pages) and paced."""
    try:
        response = requests.get(url, timeout=60, headers={"User-Agent": "resistor-civic-research/1.0"})
        response.raise_for_status()
        return response.text
    finally:
        time.sleep(PACE_SECONDS)


def resume_cursor(conn, rep_name: str, source: str, year: int):
    """str(year) when it is older than the stored cursor (or none is stored), else None, so
    re-visiting the current year on a resume never loses how far back the backfill got."""
    row = conn.execute("SELECT cursor FROM backfill_progress WHERE rep=? AND source=?",
                       (rep_name, source)).fetchone()
    stored = row[0] if row else None
    return str(year) if not stored or year < int(stored) else None


def collect_crec(
    conn, rep: dict, years: list[int], *,
    search=search_year, fetch=fetch_granule, min_words=40, oldest=None,
) -> dict:
    """Save qualifying turns and commit progress after each completed year."""
    result = {"inserted": 0, "errors": [], "years_done": []}
    if not years:
        return result
    tokens = rep["name"].split()
    while tokens and tokens[-1].rstrip(".").lower() in {"jr", "sr", "iii"}:
        tokens.pop()
    surname = tokens[-1]
    for year in sorted(set(years), reverse=True):
        saved_dates = []
        try:
            items = search(surname, year)
        except requests.HTTPError as exc:
            result["errors"].append(
                {"rep": rep["name"], "source": _SOURCE, "error": str(exc)}
            )
            return result
        for item in items:
            granule = item["granuleId"]
            url = (
                f"https://www.govinfo.gov/content/pkg/{item['packageId']}"
                f"/html/{granule}.htm"
            )
            try:
                text = fetch(url)
            except requests.HTTPError as exc:
                result["errors"].append(
                    {"rep": rep["name"], "source": _SOURCE, "error": str(exc)}
                )
                continue
            venue = next((label for marker, label in _VENUES.items() if marker in granule),
                         "Congressional Record")
            cleaned = clean_text(text)
            for segment in speaker_segments(text, surname):
                if len(segment.split()) < min_words:
                    continue
                row = _save_statement(conn, {
                    "rep": rep["name"], "date": item["dateIssued"],
                    "title": item.get("title"), "venue": venue, "quote": segment,
                    "quote_kind": "floor", "url": url, "source_kind": "floor",
                    "verified": int(verify_quote(segment, cleaned)),
                })
                if row is not None:
                    result["inserted"] += 1
                    saved_dates.append(item["dateIssued"])
        update_progress(
            conn, rep["name"], _SOURCE, saved_dates,
            cursor=resume_cursor(conn, rep["name"], _SOURCE, year),
            exhausted=int(year == (oldest or min(years))) or None,
        )
        conn.commit()
        result["years_done"].append(year)
    return result


def years_to_collect(
    conn, rep: dict, this_year: int, floor: int = 1994, source: str = _SOURCE,
) -> list[int]:
    """Choose years for an initial backfill, a resume, or a daily refresh."""
    first = max(floor, int(rep["in_office_since"]))
    row = conn.execute(
        "SELECT cursor, exhausted FROM backfill_progress WHERE rep=? AND source=?",
        (rep["name"], source),
    ).fetchone()
    if row and row[1] == 1:
        return [this_year]
    if row and row[0] is not None:
        return [this_year] + list(range(int(row[0]) - 1, first - 1, -1))
    return list(range(this_year, first - 1, -1))
