"""Stock-trade filings (STOCK Act periodic transaction reports) per federal rep. An INDEX, not per-trade detail.

House: the Clerk's yearly financial-disclosure index (disclosures-clerk.house.gov, FilingType "P" = periodic
transaction report), matched to our House member by last name + state/district. Stored: filing date, document id,
link to the filed PDF. The PDFs are not parsed here (per-trade rows are a follow-up).
Senate: efdsearch.senate.gov (periodic transaction reports, report type 11). The site makes the visitor accept a
use agreement (5 U.S.C. app. 105(c)) before searching; Todd authorised the scripted acceptance (msgs 67415, 67451).
It runs ONLY through scripts/senate_efd_refresh.py (fixed host, config senators only) and the verbatim agreement text +
acceptance date are kept in docs/senate_efd_agreement.md, stamped only after the acceptance succeeds. If the agreement
text no longer matches the 105(c) wording we stop and write nothing. Same filings-only index as the House.
State officials (Oregon) file no trade lists, only annual Statements of Economic Interest, so there is nothing to fetch.

USE LIMIT: federal law (5 U.S.C. 13107(c) House; 5 U.S.C. app. 105(c) Senate, same text) makes it unlawful to use these
reports for any unlawful purpose, any commercial purpose other than news media, credit rating, or to solicit money.
Resistor uses them only to inform constituent letters; never for fundraising or sale.
"""
import argparse, html as html_lib, io, re, sqlite3, sys, time, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
from src.config import load_representatives
from src.db import get_connection
from src.rep_record import _dicts, _now, iter_reps, update_progress

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SOURCE = "trades:house"
SENATE_SOURCE = "trades:senate"
EFD_HOST = "https://efdsearch.senate.gov"
EFD_PTR_REPORT_TYPE = 11
EFD_PAGE = 100
AGREEMENT_DOC = "docs/senate_efd_agreement.md"
AGREEMENT_MUST_CONTAIN = ("5 U.S.C. app.", "105(c)", "for any commercial purpose", "solicitation of money")
DEFAULT_SINCE_YEAR = 2020
WEEK_SECONDS = 7 * 86400
PACE_SECONDS = 1.0
USER_AGENT = "Resistor civic research (read-only, public filings)"
USE_NOTE = ("Use: constituent letters only. Federal law bars using these reports for commercial purposes or to solicit money "
            "(5 U.S.C. 13107(c)).")
SENATE_USE_NOTE = ("Use: constituent letters only. Federal law bars using these reports for commercial purposes or to solicit "
                   "money (5 U.S.C. app. 105(c)). Never read these rows for fundraising/donation asks or for Todd's businesses.")
# The use flag lives on every row; read_rows() is the ONLY reader and refuses any purpose but a constituent letter.
USE_FLAG = "constituent_letters_only"
ALLOWED_PURPOSES = {"constituent_letter"}
_sleep = time.sleep


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS rep_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT NOT NULL, source TEXT NOT NULL, filing_id TEXT NOT NULL,
        year INTEGER NOT NULL, filing_date TEXT, url TEXT NOT NULL, fetched_at TEXT NOT NULL,
        UNIQUE(rep, source, filing_id))""")
    if "use_flag" not in {r[1] for r in conn.execute("PRAGMA table_info(rep_trades)")}:
        conn.execute(f"ALTER TABLE rep_trades ADD COLUMN use_flag TEXT NOT NULL DEFAULT '{USE_FLAG}'")


def read_rows(conn, rep, source, purpose):
    """The only way to read trade rows. Rows are flagged constituent-letters-only (federal law bars commercial use and
    soliciting money); any other purpose (fundraising, donation asks, Todd's shop or Third Wish) is refused."""
    if purpose not in ALLOWED_PURPOSES:
        raise PermissionError(f"rep_trades rows may only be read for {sorted(ALLOWED_PURPOSES)}, not {purpose!r}")
    return _dicts(conn, "SELECT * FROM rep_trades WHERE rep=? AND source=? AND use_flag=? ORDER BY filing_date DESC, id DESC",
                  (rep, source, USE_FLAG))


def _district_code(rep):
    """'OR-4' -> 'OR04' (the House Clerk's StateDst)."""
    m = re.match(r"([A-Za-z]{2})-?(\d+)", str(rep.get("district") or ""))
    return f"{m.group(1).upper()}{int(m.group(2)):02d}" if m else None


def _last_name(rep):
    return re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV)$", "", rep["name"]).split()[-1].casefold()


def _iso(date_text):
    try: return datetime.strptime(date_text.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError: return ""


def parse_house_index(xml_bytes, rep):
    """PTR filings ('P') in one year's Clerk index that belong to this House member -> [(doc_id, year, 'YYYY-MM-DD')]."""
    dst, last = _district_code(rep), _last_name(rep)
    out = []
    for member in ET.fromstring(xml_bytes.lstrip(b"\xef\xbb\xbf")).iter("Member"):
        g = lambda tag: (member.findtext(tag) or "").strip()
        if g("FilingType") != "P" or not g("DocID").isdigit() or not g("Year").isdigit(): continue
        if g("StateDst").upper() == dst and g("Last").casefold() == last:
            out.append((g("DocID"), int(g("Year")), _iso(g("FilingDate"))))
    return out


def fetch_house_year(year):
    _sleep(PACE_SECONDS)
    resp = requests.get(HOUSE_ZIP.format(year=year), headers={"User-Agent": USER_AGENT}, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        return z.read(f"{year}FD.xml")


def _recent(conn, rep, source=SOURCE):
    row = conn.execute("SELECT updated_at FROM backfill_progress WHERE rep=? AND source=?", (rep, source)).fetchone()
    if not row or not row[0]: return False
    try: return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < WEEK_SECONDS
    except ValueError: return False


def refresh_rep(conn, rep, since_year=DEFAULT_SINCE_YEAR, force=False, fetch=fetch_house_year):
    name = rep["name"]
    if _recent(conn, name) and not force: return {"skipped": True}
    if not _district_code(rep): raise RuntimeError(f"no district for {name}; set district like 'OR-4' in config")
    found, errors, years = [], [], []
    for year in range(since_year, datetime.now().year + 1):
        try:
            found += parse_house_index(fetch(year), rep); years.append(year)
        except (requests.RequestException, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
            errors.append(f"{year}: {exc}")
    if errors:  # never write a partial picture: "no PTRs on file" must mean every year was read
        return {"errors": errors}
    stamp = _now()
    for doc, year, filed in found:
        conn.execute("INSERT OR REPLACE INTO rep_trades (rep, source, filing_id, year, filing_date, url, fetched_at) VALUES (?,?,?,?,?,?,?)",
                     (name, SOURCE, doc, year, filed, HOUSE_PDF.format(year=year, doc=doc), stamp))
    update_progress(conn, name, SOURCE, [f"{y}-12-31" for y in years], note=f"{len(found)} PTRs, {since_year}-{years[-1]}")
    conn.commit()
    return {"filings": len(found), "years": len(years), "errors": []}


# ---------------------------------------------------------------- Senate eFD (periodic transaction reports)
class AgreementChanged(RuntimeError):
    """The eFD use agreement no longer reads like 5 U.S.C. app. 105(c): stop, accept nothing, tell Todd."""


def _csrf_token(markup):
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', markup)
    if not m: raise RuntimeError("eFD page has no csrf token (site layout changed)")
    return m.group(1)


def agreement_text(markup):
    """The verbatim use-agreement paragraphs from the eFD home page (tags stripped, whitespace folded)."""
    start = markup.find("Title 1 of the Ethics")
    end = markup.find("<form", start + 1) if start >= 0 else -1
    if start < 0 or end < 0: raise AgreementChanged("could not find the agreement text on the eFD home page")
    text = " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", markup[start:end])).split())
    if any(phrase not in text for phrase in AGREEMENT_MUST_CONTAIN):
        raise AgreementChanged(f"agreement no longer matches 105(c) wording; read it by hand: {text[:300]}")
    return text


def efd_accept_agreement(session):
    """GET home (csrf) -> check agreement text -> POST prohibition_agreement=1. Returns the agreement text only on success."""
    home = EFD_HOST + "/search/home/"
    resp = session.get(home, timeout=60); resp.raise_for_status()
    text = agreement_text(resp.text)
    _sleep(PACE_SECONDS)
    posted = session.post(home, data={"prohibition_agreement": "1", "csrfmiddlewaretoken": _csrf_token(resp.text)},
                          headers={"Referer": home}, timeout=60)
    posted.raise_for_status()
    if "/search/home" in posted.url: raise RuntimeError("eFD did not accept the agreement (still on the home page)")
    return text


def _efd_state(rep):
    if not rep.get("state"): raise ValueError(f"no state for {rep['name']}; set state like 'OR' in config")
    return rep["state"]


def _efd_rows(session, rep, since_year):
    """All Periodic Transaction Report rows for one senator, paged. Raises on any HTTP/JSON problem."""
    search = EFD_HOST + "/search/"
    page = session.get(search, timeout=60); page.raise_for_status()
    _sleep(PACE_SECONDS)
    rows, start, total = [], 0, None
    while total is None or start < total:
        resp = session.post(EFD_HOST + "/search/report/data/", timeout=60, headers={
            "Referer": search, "X-CSRFToken": session.cookies.get("csrftoken") or _csrf_token(page.text)},
            data={"draw": "1", "start": str(start), "length": str(EFD_PAGE), "report_types": f"[{EFD_PTR_REPORT_TYPE}]",
                  "filer_types": "[]", "submitted_start_date": f"01/01/{since_year} 00:00:00", "submitted_end_date": "",
                  "candidate_state": "", "senator_state": _efd_state(rep), "office_id": "",
                  "first_name": "", "last_name": _last_name(rep)})
        resp.raise_for_status()
        body = resp.json()
        total = int(body["recordsFiltered"])
        rows += body["data"]
        if not body["data"]: break
        start += len(body["data"])  # advance by what came back, never skip rows on a short page
        _sleep(PACE_SECONDS)
    return rows


def parse_efd_rows(rows, rep):
    """eFD result rows [first, last, 'Last, First (Senator)', '<a href=/search/view/ptr|paper/UUID/>title</a>', 'MM/DD/YYYY']
    for this senator -> [(filing_id, year, 'YYYY-MM-DD', url)]. Paper filings keep their /paper/ url so show() can say so."""
    last, out = _last_name(rep), []
    for row in rows:
        if len(row) < 5 or html_lib.unescape(str(row[1])).strip().casefold() != last: continue
        link = re.search(r'href="(/search/view/(?:ptr|paper)/([0-9A-Za-z-]+)/)"', str(row[3]))
        filed = _iso(str(row[4]))
        if not link or not filed: raise ValueError(f"unreadable eFD row for {rep['name']}: {row!r}")
        out.append((link.group(2), int(filed[:4]), filed, EFD_HOST + link.group(1)))
    return out


def refresh_senate_rep(conn, rep, session, since_year=DEFAULT_SINCE_YEAR, force=False):
    name = rep["name"]
    if _recent(conn, name, SENATE_SOURCE) and not force: return {"skipped": True}
    try: found = parse_efd_rows(_efd_rows(session, rep, since_year), rep)
    except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:  # never write a partial picture
        return {"errors": [str(exc)]}
    stamp, this_year = _now(), datetime.now().year
    for fid, year, filed, url in found:
        conn.execute("INSERT OR REPLACE INTO rep_trades (rep, source, filing_id, year, filing_date, url, fetched_at, use_flag) "
                     "VALUES (?,?,?,?,?,?,?,?)", (name, SENATE_SOURCE, fid, year, filed, url, stamp, USE_FLAG))
    update_progress(conn, name, SENATE_SOURCE, [f"{y}-12-31" for y in range(since_year, this_year + 1)],
                    note=f"{len(found)} PTRs, {since_year}-{this_year}")
    conn.commit()
    return {"filings": len(found), "errors": []}


def write_agreement_doc(text, root=None):
    """Keep the verbatim agreement + acceptance date in the repo. Called only AFTER the acceptance POST succeeded.
    The first acceptance date is never overwritten; a changed (but still 105(c)) text is appended with its own date."""
    from pathlib import Path
    path = Path(root or Path(__file__).resolve().parent.parent) / AGREEMENT_DOC
    stamp = (f"accepted: {datetime.now().strftime('%Y-%m-%d')} by script (scripts/senate_efd_refresh.py), "
             f"at the owner's direction (msgs 67415, 67451).\n\n> {text}\n")
    if path.exists():
        body = path.read_text(encoding="utf-8")
        if text in body: return path
        path.write_text(body + f"\n## Agreement text changed (still passes the 105(c) check)\nRe-{stamp}", encoding="utf-8")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Senate eFD use agreement\n\n"
        f"Source: {EFD_HOST}/search/home/ (verbatim, fetched at acceptance)\n"
        f"First {stamp}\n"
        "## Scope of use here\n"
        "Constituent letters and `rep_record show` only. Rows carry use_flag=constituent_letters_only and are read only through "
        "`rep_trades.read_rows(..., purpose='constituent_letter')`, which refuses any other purpose. Never for fundraising or "
        "donation asks, credit rating, or anything for the owner's shop or Third Wish.\n"
        "Stop rule: if the agreement text stops matching 105(c), or the scripted acceptance is blocked, stop and report; no workaround.\n",
        encoding="utf-8")
    return path


def senate_main():
    """Fixed entry point for scripts/senate_efd_refresh.py: no arguments, host hard-coded, config senators only."""
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    reps = [r for r in iter_reps(load_representatives()) if r["chamber_hint"] == "Senate"]
    conn = get_connection()
    try:
        ensure_schema(conn); conn.commit()
        due = [r for r in reps if not _recent(conn, r["name"], SENATE_SOURCE)]
        if not due: print("Senate eFD: all senators checked <7 days ago; nothing to do"); return 0
        session = requests.Session(); session.headers["User-Agent"] = USER_AGENT
        try: text = efd_accept_agreement(session)
        except AgreementChanged as exc: print(f"STOP (agreement changed, nothing accepted): {exc}"); return 2
        except (requests.RequestException, RuntimeError) as exc: print(f"STOP (acceptance failed): {exc}"); return 1
        print(f"agreement recorded in {write_agreement_doc(text)}")
        failed = False
        for rep in due:
            _sleep(PACE_SECONDS)
            result = refresh_senate_rep(conn, rep, session)
            for error in result.get("errors", []): print(f"{rep['name']}  ERROR: {error}"); failed = True
            if not result.get("errors"): print(f"{rep['name']}: {result['filings']} PTRs")
        return int(failed)
    finally: conn.close()


def _chamber(rep):
    return next((r["chamber_hint"] for r in iter_reps(load_representatives()) if r["name"].casefold() == rep.casefold()), None)


def show(conn, rep, limit=5, chamber=None):
    """Honest trade-filing lines for one rep: what was checked and when, never 'clean'."""
    ensure_schema(conn)
    chamber = chamber or _chamber(rep)
    if chamber in {"OR_Senate", "OR_House", "Governor"}:
        print("Stock-trade filings: Oregon officials file no trade lists, only annual Statements of Economic Interest (Oregon Government Ethics Commission); not fetched.")
        return
    senate = chamber == "Senate"
    source, office, note = (SENATE_SOURCE, "the Senate eFD", SENATE_USE_NOTE) if senate else (SOURCE, "the House Clerk", USE_NOTE)
    row = conn.execute("SELECT updated_at, note FROM backfill_progress WHERE rep=? AND source=?", (rep, source)).fetchone()
    if not row:
        print("Stock-trade filings: not checked yet (Senate eFD: python scripts/senate_efd_refresh.py)." if senate else
              "Stock-trade filings: not checked yet (House PTRs come from the Clerk's index).")
        return
    rows = read_rows(conn, rep, source, "constituent_letter")
    checked = row[0][:10]
    if not rows:
        print(f"Stock-trade filings: no periodic transaction reports on file with {office} ({row[1]}; checked {checked}). "
              "That means none filed, not that the member holds no stocks.")
    else:
        paper = sum("/view/paper/" in r["url"] for r in rows)
        print(f"Stock-trade filings: {len(rows)} periodic transaction report(s) on file with {office} (checked {checked}); "
              "the filings list the trades, this index does not." + (f" {paper} filed on paper (scanned, no electronic copy)." if paper else ""))
        for r in rows[:limit]: print(f"  filed {r['filing_date'] or r['year']} | {r['url']}" + (" (paper)" if "/view/paper/" in r["url"] else ""))
    print(note)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh"); refresh.add_argument("--rep"); refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--since-year", type=int, default=DEFAULT_SINCE_YEAR)
    sub.add_parser("show").add_argument("rep")
    args = parser.parse_args(argv)
    conn = get_connection()
    try:
        ensure_schema(conn); conn.commit()
        if args.command == "show": show(conn, args.rep); return 0
        reps = [r for r in iter_reps(load_representatives()) if r["chamber_hint"] == "House"
                and (not args.rep or r["name"].casefold() == args.rep.casefold())]
        if not reps: parser.error("no matching House representatives (Senate: python scripts/senate_efd_refresh.py)")
        failed = False
        for rep in reps:
            try: result = refresh_rep(conn, rep, args.since_year, args.force)
            except (requests.RequestException, RuntimeError, sqlite3.Error, ValueError) as exc:
                conn.rollback(); result = {"errors": [str(exc)]}
            if result.get("skipped"): print(f"{rep['name']}: skipped (checked <7 days ago)")
            elif not result.get("errors"): print(f"{rep['name']}: {result['filings']} PTRs across {result['years']} years")
            for error in result.get("errors", []): print(f"{rep['name']}  ERROR: {error}"); failed = True
        return int(failed)
    finally: conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
