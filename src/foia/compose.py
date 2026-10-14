"""Compose narrow FOIA requests and appeals as unsent drafts only."""

import re
from datetime import date, datetime

from src.config import load_user
from src.db import get_connection
from src.foia import tracker

ALLOWED_CITES = (
    "552(a)(3)(A)",
    "552(a)(3)(B)",
    "552(a)(4)(A)(ii)",
    "552(a)(4)(A)(iii)",
    "552(a)(6)(A)(i)",
    "552(a)(6)(A)(ii)",
    "552(a)(6)(B)(i)",
    "552(a)(6)(C)(i)",
    "552(a)(6)(E)",
    "552(b)",
    "552(b)(6)",
    "552(a)(6)",
)
MAX_DRAFTS_PER_DAY = 3
DRAFT_HEADER = (
    "DRAFT - NOT SENT. Review, verify every fact and date, "
    "and send only through Todd's approval."
)

_CITATION_PATTERN = re.compile(r"552(?:\([0-9A-Za-z]+\))+")
_CASE_PATTERN = re.compile(r"\b[A-Z][A-Za-z.&']+ v\. [A-Z]")
_REPORTER_PATTERN = re.compile(
    r"\b\d+ (?:F\.(?:2d|3d|4th)|F\. Supp\.|U\.S\.|S\. Ct\.) \d+"
)
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def check_citations(text: str) -> list[str]:
    """Return disallowed citation tokens and case-law smells in text order."""
    offenders = [
        (match.start(), match.group())
        for match in _CITATION_PATTERN.finditer(text)
        if match.group() not in ALLOWED_CITES
    ]
    for pattern in (_CASE_PATTERN, _REPORTER_PATTERN):
        offenders.extend(
            (match.start(), match.group()) for match in pattern.finditer(text)
        )
    return [value for _, value in sorted(offenders, key=lambda item: item[0])]


def ensure_drafts_table():
    """Create the FOIA tables and the unsent drafts table."""
    tracker.ensure_tables()
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS foia_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER REFERENCES foia_requests(id),
                kind TEXT NOT NULL,
                agency TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft_unsent',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _parse_date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("date must be an ISO YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must be an ISO YYYY-MM-DD string") from None
    if parsed.isoformat() != value:
        raise ValueError("date must be an ISO YYYY-MM-DD string")
    return parsed


def _today(value: date | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, str):
        return _parse_date(value)
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("today must be a date or an ISO YYYY-MM-DD string")
    return value


def _letter_header(requester: dict, today: date, officer: str,
                   agency: str, subject: str) -> str:
    lines = [
        DRAFT_HEADER,
        str(requester["name"]),
        str(requester["address"]),
        f"{requester['city']}, {requester['state']} {requester['zip']}",
    ]
    if requester.get("email"):
        lines.append(str(requester["email"]))
    lines.extend([
        f"{_MONTHS[today.month - 1]} {today.day}, {today.year}",
        "",
        officer,
        agency,
        "",
        subject,
    ])
    return "\n".join(lines)


def _validate_body(body: str):
    offenders = check_citations(body)
    if offenders:
        raise ValueError(f"disallowed citations or case-law references: {offenders}")
    assert "\u2014" not in body
    if not body.isascii():
        raise ValueError("draft body must contain plain ASCII text only")


def _save_draft(request_id: int | None, kind: str, agency: str,
                body: str) -> int:
    ensure_drafts_table()
    conn = get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "request":
                count = conn.execute(
                    "SELECT COUNT(*) FROM foia_drafts "
                    "WHERE kind='request' AND date(created_at)=date('now')"
                ).fetchone()[0]
                if count >= MAX_DRAFTS_PER_DAY:
                    raise ValueError(
                        f"maximum of {MAX_DRAFTS_PER_DAY} request drafts per day"
                    )
            cursor = conn.execute(
                "INSERT INTO foia_drafts "
                "(request_id, kind, agency, body, status) "
                "VALUES (?, ?, ?, ?, 'draft_unsent')",
                (request_id, kind, agency, body)
            )
            draft_id = cursor.lastrowid
        return draft_id
    finally:
        conn.close()


def draft_request(agency: str, records_description: str,
                  date_range: tuple[str, str], fee_category: str = "other",
                  fee_waiver_reason: str = None,
                  format_pref: str = "electronic", requester: dict = None,
                  request_id: int = None, save: bool = True,
                  allow_long_range: bool = False,
                  today: date | str | None = None) -> dict:
    """Compose one narrow request and optionally save it as draft_unsent."""
    if not isinstance(agency, str):
        raise ValueError("one agency per request")
    # Real agency names contain "and" and commas ("Department of Health and
    # Human Services"), so only explicit list separators count as multiple.
    if any(sep in agency for sep in (";", "\n", " / ")):
        raise ValueError("one agency per request")
    if not agency.strip():
        raise ValueError("agency must not be empty")

    if (not isinstance(records_description, str)
            or len(records_description.split()) < 12
            or re.search(
                r"\b(?:any\s+and\s+all|all\s+records)\b",
                records_description, re.IGNORECASE
            )):
        raise ValueError(
            "name specific records, offices, custodians and a date range; "
            "use at least 12 words and avoid 'any and all' or 'all records'"
        )

    if not isinstance(date_range, (tuple, list)) or len(date_range) != 2:
        raise ValueError("date_range requires start and end ISO YYYY-MM-DD strings")
    start, end = (_parse_date(value) for value in date_range)
    if end < start:
        raise ValueError("date_range end must be on or after start")
    if (end - start).days > 3 * 365 + 1 and not allow_long_range:
        raise ValueError(
            "date_range must span at most 3*365+1 days "
            "unless allow_long_range=True"
        )

    if fee_category not in ("other", "news_media", "educational", "commercial"):
        raise ValueError(f"invalid fee category: {fee_category}")

    today = _today(today)
    if requester is None:
        requester = load_user()
    requested_format = "paper" if format_pref == "paper" else "electronic"

    paragraphs = [
        _letter_header(
            requester, today, "FOIA Officer", agency,
            "Re: Freedom of Information Act request"
        ),
        "Under the Freedom of Information Act, 5 U.S.C. 552(a)(3)(A), "
        "I request copies of the following records:",
        records_description,
        f"Date range: {start.isoformat()} through {end.isoformat()}.",
        f"I request the records in {requested_format} format, as provided "
        "by 5 U.S.C. 552(a)(3)(B).",
        f"My fee category is {fee_category} under 5 U.S.C. 552(a)(4)(A)(ii). "
        "Please contact me before incurring fees above $25.",
    ]
    if fee_waiver_reason is not None:
        paragraphs.append(
            "I request a public-interest fee waiver under "
            "5 U.S.C. 552(a)(4)(A)(iii) for the following reason: "
            f'"{fee_waiver_reason}"\n'
            "[Explain how disclosure contributes significantly to public "
            "understanding of government operations - fill in before sending]"
        )
    paragraphs.extend([
        "If any portion of these records is withheld, please release all "
        "reasonably segregable portions and cite the specific exemption "
        "under 5 U.S.C. 552(b) for each withholding.",
        "I am happy to discuss narrowing this request. Please contact me "
        "if anything is unclear.",
        "I look forward to a determination within 20 business days, as "
        "provided by 5 U.S.C. 552(a)(6)(A)(i).",
        f"Sincerely,\n{requester['name']}",
    ])
    body = "\n\n".join(paragraphs)
    _validate_body(body)
    draft_id = _save_draft(request_id, "request", agency, body) if save else None
    return {"draft_id": draft_id, "body": body}


def draft_appeal(request_id: int, grounds: str,
                 today: date | str | None = None, requester: dict = None,
                 save: bool = True) -> dict:
    """Compose an appeal using logged facts and explicit review placeholders."""
    if grounds not in (
        "no_determination", "denial", "partial", "no_records", "fee_denial"
    ):
        raise ValueError(f"invalid appeal grounds: {grounds}")

    today = _today(today)
    request = tracker.get_request(request_id)
    deadlines = tracker.compute_deadlines(request_id, record=True)
    reference = request.get("tracking_number") or f"#{request['id']}"
    determination = next(
        (event for event in reversed(request["events"])
         if event["event_type"] == "determination"),
        None
    )

    if grounds == "no_determination":
        due = deadlines["initial_determination"]["due"]
        if due is None:
            raise ValueError("no calculated deadline (not received or clock paused)")
        if today <= due:
            raise ValueError(f"not overdue yet: due {due}")
        if determination is not None:
            raise ValueError(
                "determination already recorded; choose determination appeal grounds"
            )
        appeal_paragraphs = [
            f"My request {reference} was received {request['received_date']}. "
            f"The calculated statutory deadline was {due} under "
            "5 U.S.C. 552(a)(6)(A)(i). "
            f"No determination has been received as of {today.isoformat()}.",
            "Under 5 U.S.C. 552(a)(6)(C)(i), I am deemed to have exhausted "
            "administrative remedies. I ask for a determination without "
            "further delay.",
        ]
    else:
        if determination is None:
            raise ValueError("a determination event is required for these grounds")
        determination_label = {
            "granted": "grant determination",
            "denied": "denial",
            "partial": "partial determination",
            "no_records": "no-records determination",
            "fee_denial": "fee denial",
        }.get(determination["details"], "determination")
        appeal_paragraphs = [
            f"I appeal the {determination_label} dated "
            f"{determination['event_date']} on request {reference}.",
            "I request de novo review of each withholding and release of "
            "all reasonably segregable portions under 5 U.S.C. 552(b).",
        ]
        if grounds == "no_records":
            appeal_paragraphs.append(
                "I ask the agency to conduct a search reasonably calculated "
                "to uncover all responsive records."
            )
        appeal_paragraphs.append(
            "[STATE SPECIFIC REASONS THE WITHHOLDING OR SEARCH WAS WRONG "
            "- do not file until filled in]"
        )
        if "appeal_window" in deadlines:
            due = deadlines["appeal_window"]["due"]
            if due is not None:
                appeal_paragraphs.append(
                    f"Calculated appeal window closes {due}."
                )

    if requester is None:
        requester = load_user()
    paragraphs = [
        _letter_header(
            requester, today, "FOIA Appeals Officer", request["agency"],
            f"Re: Freedom of Information Act appeal - request {reference}"
        ),
        *appeal_paragraphs,
        f"Sincerely,\n{requester['name']}",
        tracker.DISCLAIMER,
    ]
    body = "\n\n".join(paragraphs)
    _validate_body(body)
    draft_id = (
        _save_draft(request_id, "appeal", request["agency"], body)
        if save else None
    )
    return {"draft_id": draft_id, "body": body}


def list_drafts() -> list[dict]:
    """Return saved unsent drafts, newest first."""
    ensure_drafts_table()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM foia_drafts ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
