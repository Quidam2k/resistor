"""Read-only regulations.gov v4 client + comment-draft scaffolding.

A substantive comment on a proposed rule obliges the agency to consider it and
respond in the final rule (5 U.S.C. 553(c)), which is more legal weight than a
constituent letter. This module only READS dockets/documents and saves comment
DRAFTS. There is deliberately no submission code: posting a comment goes
through Todd's approval and is done by hand on regulations.gov.

Uses CONGRESS_API_KEY (an api.data.gov key, which regulations.gov accepts).
regulations.gov allows 1,000 requests/hour on that shared key, so every read
is cached via src.http_cache.
"""

import argparse
from datetime import date, datetime, timezone

from src.config import get_env
from src.db import get_connection
from src.http_cache import cached_get

REGS_API_BASE = "https://api.regulations.gov/v4"

SEARCH_TTL = 6 * 3600       # open-comment searches change daily at most
DETAIL_TTL = 24 * 3600      # docket/document metadata rarely changes

DRAFT_HEADER = ("DRAFT - NOT SUBMITTED. Fill in every bracketed section, verify "
                "every fact, and submit by hand on regulations.gov only after "
                "Todd approves the full text.")


def _regs_get(endpoint: str, params: dict = None, ttl: int = DETAIL_TTL) -> dict:
    api_key = get_env("CONGRESS_API_KEY")
    if not api_key:
        raise RuntimeError("CONGRESS_API_KEY (api.data.gov key) not set in .env")
    params = dict(params or {})
    params["api_key"] = api_key
    return cached_get(f"{REGS_API_BASE}/{endpoint}", params, ttl_seconds=ttl)


def _summarize(doc: dict) -> dict:
    a = doc.get("attributes", {})
    return {
        "id": doc.get("id"),
        "docket_id": a.get("docketId"),
        "agency": a.get("agencyId"),
        "title": a.get("title"),
        "document_type": a.get("documentType"),
        "fr_doc_num": a.get("frDocNum"),
        "comment_start": a.get("commentStartDate"),
        "comment_end": a.get("commentEndDate"),
        "open_for_comment": a.get("openForComment"),
        "url": f"https://www.regulations.gov/document/{doc.get('id')}",
    }


def get_docket(docket_id: str) -> dict:
    """Docket metadata (title, RIN, abstract)."""
    d = _regs_get(f"dockets/{docket_id}")["data"]
    a = d.get("attributes", {})
    return {
        "id": d.get("id"),
        "agency": a.get("agencyId"),
        "title": a.get("title"),
        "short_title": a.get("shortTitle"),
        "rin": a.get("rin"),
        "abstract": a.get("dkAbstract"),
        "docket_type": a.get("docketType"),
        "url": f"https://www.regulations.gov/docket/{d.get('id')}",
    }


def get_document(document_id: str) -> dict:
    """Full document metadata, including Federal Register citation."""
    d = _regs_get(f"documents/{document_id}")["data"]
    summary = _summarize(d)
    a = d.get("attributes", {})
    summary.update({
        "fr_volume": a.get("frVolNum"),
        "fr_pages": a.get("startEndPage"),
        "cfr_part": a.get("cfrPart"),
        "abstract": a.get("docAbstract"),
    })
    return summary


def find_open_comment_periods(search_term: str = None, agency: str = None,
                              docket_id: str = None, today: date = None,
                              page_size: int = 25) -> list[dict]:
    """Proposed rules whose comment period has not closed, soonest-closing first."""
    today = today or date.today()
    params = {
        "filter[documentType]": "Proposed Rule",
        "filter[commentEndDate][ge]": today.isoformat(),
        "sort": "commentEndDate",
        "page[size]": page_size,
    }
    if search_term:
        params["filter[searchTerm]"] = search_term
    if agency:
        params["filter[agencyId]"] = agency
    if docket_id:
        params["filter[docketId]"] = docket_id
    data = _regs_get("documents", params, ttl=SEARCH_TTL).get("data", [])
    return [_summarize(d) for d in data]


def days_left(comment_end: str, now: datetime = None) -> int | None:
    """Whole days until the comment period closes (negative once closed)."""
    if not comment_end:
        return None
    end = datetime.fromisoformat(comment_end.replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    return (end - now).days


# ---------------------------------------------------------------------------
# Comment drafts (stored only; never submitted)
# ---------------------------------------------------------------------------

def ensure_tables():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regs_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,        -- regulations.gov document commented on
            docket_id TEXT,
            agency TEXT,
            title TEXT,
            comment_end TEXT,                 -- ISO timestamp the period closes
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft_unsent',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            notes TEXT
        )
    """)
    conn.commit()
    conn.close()


def draft_comment(document_id: str, position: str, defects: list[str] = None,
                  requester: dict = None, save: bool = True) -> dict:
    """Scaffold a substantive comment on a proposed rule.

    Agencies must respond to substantive points (specific factual, legal or
    analytic defects), not to statements of preference, so the scaffold leaves
    one bracketed section per defect for Todd to fill with sourced specifics.
    Nothing here invents arguments.
    """
    doc = get_document(document_id)
    if requester is None:
        from src.config import load_user
        requester = load_user()

    fr_cite = ""
    if doc.get("fr_volume") and doc.get("fr_pages"):
        first_page = str(doc["fr_pages"]).split("-")[0].strip()
        fr_cite = f"{doc['fr_volume']} FR {first_page}"

    defects = defects or ["[DEFECT 1 - name the specific factual, legal or "
                          "analytic flaw, with a source]"]
    sections = "\n\n".join(
        f"{i}. {d}\n   [Evidence and citation for this point. Do not submit "
        f"until filled in and verified.]" for i, d in enumerate(defects, 1))

    name = requester.get("name", "[NAME]")
    city = requester.get("city", "")
    state = requester.get("state", "")
    location = ", ".join(p for p in (city, state) if p)

    lines = [
        DRAFT_HEADER,
        "",
        f"Docket: {doc.get('docket_id') or '[docket]'}",
        f"Document: {doc['id']}" + (f" ({fr_cite})" if fr_cite else ""),
        f"Rule: {doc.get('title')}",
        f"Agency: {doc.get('agency')}",
        f"Comment period closes: {doc.get('comment_end')}",
        "",
        f"I am {name}" + (f", of {location}" if location else "")
        + f". I write to comment on the proposed rule above. {position}",
        "",
        "I ask the agency to consider and respond to each of the following "
        "specific points in the final rule, as 5 U.S.C. 553(c) requires it to "
        "consider relevant matter presented in comments.",
        "",
        sections,
        "",
        "[Specific requested change to the rule text, if any.]",
        "",
        "Thank you for considering these comments.",
        "",
        name,
    ]
    body = "\n".join(lines)

    draft_id = None
    if save:
        ensure_tables()
        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO regs_comments (document_id, docket_id, agency, title, "
            "comment_end, body) VALUES (?, ?, ?, ?, ?, ?)",
            (doc["id"], doc.get("docket_id"), doc.get("agency"), doc.get("title"),
             doc.get("comment_end"), body))
        conn.commit()
        draft_id = cur.lastrowid
        conn.close()
    return {"draft_id": draft_id, "body": body, "document": doc}


def list_comment_drafts() -> list[dict]:
    ensure_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM regs_comments ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="regulations.gov (read-only)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("open", help="open comment periods on proposed rules")
    s.add_argument("search", nargs="?")
    s.add_argument("--agency")
    s.add_argument("--docket")
    d = sub.add_parser("docket")
    d.add_argument("docket_id")
    args = parser.parse_args()

    if args.cmd == "open":
        for r in find_open_comment_periods(args.search, args.agency, args.docket):
            left = days_left(r["comment_end"])
            print(f"{r['comment_end'][:10]} ({left}d)  {r['agency']:<6} {r['id']}")
            print(f"    {r['title']}")
    elif args.cmd == "docket":
        for k, v in get_docket(args.docket_id).items():
            print(f"{k}: {v}")
