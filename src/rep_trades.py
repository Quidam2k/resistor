"""Stock-trade filings (STOCK Act periodic transaction reports) per federal rep. An INDEX, not per-trade detail.

House: the Clerk's yearly financial-disclosure index (disclosures-clerk.house.gov, FilingType "P" = periodic
transaction report), matched to our House member by last name + state/district. Stored: filing date, document id,
link to the filed PDF. The PDFs are not parsed here (per-trade rows are a follow-up).
Senate: NOT fetched yet. efdsearch.senate.gov makes the visitor accept a use agreement before searching;
accepting it is Todd's call, so `show` says "not checked" for senators rather than "none on file".
State officials (Oregon) file no trade lists, only annual Statements of Economic Interest, so there is nothing to fetch.

USE LIMIT: federal law (5 U.S.C. 13107(c); the Senate agreement quotes the same text) makes it unlawful to use these
reports for any unlawful purpose, any commercial purpose other than news media, credit rating, or to solicit money.
Resistor uses them only to inform constituent letters; never for fundraising or sale.
"""
import argparse, io, re, sqlite3, sys, time, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
from src.config import load_representatives
from src.db import get_connection
from src.rep_record import _dicts, _now, iter_reps, update_progress

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SOURCE = "trades:house"
DEFAULT_SINCE_YEAR = 2020
WEEK_SECONDS = 7 * 86400
PACE_SECONDS = 1.0
USER_AGENT = "Resistor civic research (read-only, public filings)"
USE_NOTE = ("Use: constituent letters only. Federal law bars using these reports for commercial purposes or to solicit money "
            "(5 U.S.C. 13107(c)).")
_sleep = time.sleep


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS rep_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT NOT NULL, source TEXT NOT NULL, filing_id TEXT NOT NULL,
        year INTEGER NOT NULL, filing_date TEXT, url TEXT NOT NULL, fetched_at TEXT NOT NULL,
        UNIQUE(rep, source, filing_id))""")


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


def _recent(conn, rep):
    row = conn.execute("SELECT updated_at FROM backfill_progress WHERE rep=? AND source=?", (rep, SOURCE)).fetchone()
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


def _chamber(rep):
    return next((r["chamber_hint"] for r in iter_reps(load_representatives()) if r["name"].casefold() == rep.casefold()), None)


def show(conn, rep, limit=5, chamber=None):
    """Honest trade-filing lines for one rep: what was checked and when, never 'clean'."""
    ensure_schema(conn)
    chamber = chamber or _chamber(rep)
    if chamber == "Senate":
        print("Stock-trade filings: Senate eFD not checked (it needs a use-agreement acceptance); nothing is known either way.")
        return
    if chamber in {"OR_Senate", "OR_House", "Governor"}:
        print("Stock-trade filings: Oregon officials file no trade lists, only annual Statements of Economic Interest (Oregon Government Ethics Commission); not fetched.")
        return
    row = conn.execute("SELECT updated_at, note FROM backfill_progress WHERE rep=? AND source=?", (rep, SOURCE)).fetchone()
    if not row:
        print("Stock-trade filings: not checked yet (House PTRs come from the Clerk's index; Senate eFD is not fetched).")
        return
    rows = _dicts(conn, "SELECT * FROM rep_trades WHERE rep=? AND source=? ORDER BY filing_date DESC, id DESC", (rep, SOURCE))
    checked = row[0][:10]
    if not rows:
        print(f"Stock-trade filings: no periodic transaction reports on file with the House Clerk ({row[1]}; checked {checked}). "
              "That means none filed, not that the member holds no stocks.")
    else:
        print(f"Stock-trade filings: {len(rows)} periodic transaction report(s) on file with the House Clerk (checked {checked}); "
              "the PDFs list the trades, this index does not.")
        for r in rows[:limit]: print(f"  filed {r['filing_date'] or r['year']} | {r['url']}")
    print(USE_NOTE)


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
        if not reps: parser.error("no matching House representatives (Senate eFD is not fetched; see module docstring)")
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
