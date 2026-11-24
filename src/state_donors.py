"""Who funds each Oregon state rep, from ORESTAR (secure.sos.state.or.us/orestar). ORESTAR-reported only.

Everything stored here is "ORESTAR-reported": contributions as filed by the committee with the Oregon
Secretary of State. Names are filer-supplied, the list view has no employer field, and nothing here is
ever "verified". Rows go into the same rep_donors / rep_finance_totals tables as the FEC data
(src.rep_donors), so `python -m src.rep_donors show/conflicts "<rep>"` works for state reps too.

The totals row is a LOWER BOUND: only organizational gifts (business, committee, party, labor, other) and
individual gifts of $1,000 or more are fetched; small individual gifts number in the tens of thousands.
"""
import argparse, html, re, sqlite3, sys, time
from datetime import datetime, timezone
import requests
from src.config import load_representatives
from src.db import get_connection
from src.rep_donors import ensure_schema
from src.rep_record import _now, iter_reps, update_progress

BASE = "https://secure.sos.state.or.us/orestar"
SOURCE = "donors:orestar"
PACE_SECONDS = 1.0
WEEK_SECONDS = 7 * 86400
DEFAULT_SINCE_YEAR = 2018
MAX_PAGES = 40  # 50 rows per page; a capped group is reported as a warning, never silently
PAGE_ROWS = 50
ROWS_PER_KIND = 25
STATE_HINTS = {"OR_Senate", "OR_House", "Governor"}
URL_PREFIX = "https://secure.sos.state.or.us/%"
USER_AGENT = "Resistor civic research (read-only, public filings)"
GROUPS = (  # (kind, ORESTAR contributor types, min individual amount)
    ("employer", ("B",), None),
    ("pac", ("C", "P", "L", "O"), None),
    ("individual_large", ("I",), "1000"),
)
_sleep = time.sleep


def _safe(exc):
    """Exception text with the CSRF token blanked (it rides in the query string of HTTP errors)."""
    return re.sub(r"OWASP_CSRFTOKEN=[^&\s'\"]+", "OWASP_CSRFTOKEN=***", str(exc))


def _text(cell):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def _amount(raw):
    raw = raw.strip()
    negative = raw.startswith("(") or raw.startswith("-") or raw.startswith("$-") or raw.startswith("-$")
    digits = re.sub(r"[^\d.]", "", raw)
    if not digits or digits == ".": raise ValueError(f"unparseable amount {raw!r}")
    return -float(digits) if negative else float(digits)


def parse_results(markup):
    """-> (total records found, [dict(tran_id, date, status, filer, contributor, subtype, amount)])."""
    found = re.search(r"([\d,]+)\s+records? found", markup)
    total = int(found.group(1).replace(",", "")) if found else 0
    rows = []
    for tr in re.findall(r'<tr valign="middle"[^>]*>(.*?)</tr>', markup, re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cells) < 7 or not cells[0].isdigit(): continue
        if cells[2] not in {"Original", "Amended"}: continue
        try: amount = _amount(cells[6])
        except ValueError: continue
        rows.append(dict(tran_id=cells[0], date=cells[1], status=cells[2], filer=cells[3],
                         contributor=cells[4], subtype=cells[5], amount=amount))
    return total, rows


def normalize_name(name):
    name = re.sub(r"\s*\*+\s*$", "", name or "")  # out-of-state marker
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)  # trailing committee id
    return " ".join(name.split())


def aggregate(rows):
    """-> {normalized name: (sum, count)}."""
    sums, counts = {}, {}
    for r in rows:
        name = normalize_name(r["contributor"])
        if not name: continue
        sums[name] = sums.get(name, 0.0) + r["amount"]
        counts[name] = counts.get(name, 0) + 1
    return {n: (sums[n], counts[n]) for n in sums}


class OrestarClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.started = False

    def _http(self, method, path, **kw):
        _sleep(PACE_SECONDS)
        resp = self.session.request(method, BASE + path, timeout=120, **kw)
        resp.raise_for_status()
        return resp

    def get_token(self):
        if not self.started:
            self._http("GET", "/gotoPublicTransactionSearch.do"); self.started = True
        body = self._http("POST", "/JavaScriptServlet", headers={"FETCH-CSRF-TOKEN": "1"}).text.strip()
        name, sep, value = body.partition(":")
        if not sep or not name or not value: raise RuntimeError("ORESTAR gave no CSRF token (site layout changed?)")
        return name, value

    def search(self, committee_id, start, end, contributor_type, amount_from=None):
        """-> (records found, rows fetched, truncated). Dates are MM/DD/YYYY. Pages until a short page."""
        rows, seen, truncated, total = [], set(), False, 0
        for page in range(MAX_PAGES):
            name, value = self.get_token()
            params = {name: value, "cneSearchButtonName": "search" if page == 0 else "next", "cneSearchPageIdx": str(page),
                      "cneSearchFilerCommitteeId": str(committee_id), "cneSearchTranStartDate": start,
                      "cneSearchTranEndDate": end, "cneSearchFilerCommitteeTxtSearchType": "C",
                      "cneSearchContributorTxtSearchType": "C", "cneSearchTranType": "C",
                      "cneSearchContributorType": contributor_type}
            if amount_from: params["cneSearchTranAmountFrom"] = amount_from
            total, batch = parse_results(self._http("GET", "/gotoPublicTransactionSearchResults.do", params=params).text)
            fresh = [r for r in batch if r["tran_id"] not in seen]
            seen.update(r["tran_id"] for r in fresh)
            rows += fresh
            if len(batch) < PAGE_ROWS or not fresh: return total, rows, False
        return total, rows, True


def _recent(conn, rep):
    row = conn.execute("SELECT updated_at FROM backfill_progress WHERE rep=? AND source=?", (rep, SOURCE)).fetchone()
    if not row or not row[0]: return False
    try: return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < WEEK_SECONDS
    except ValueError: return False


def _top(agg):
    return sorted(((n, s, c) for n, (s, c) in agg.items() if s > 0), key=lambda t: -t[1])[:ROWS_PER_KIND]


def fetch_year(client, committee_id, year):
    """-> (donor rows [(kind, donor, amount, n)], latest tran date 'YYYY-MM-DD' or '', groups that hit the page cap)."""
    start, end = f"01/01/{year}", f"12/31/{year}"
    out, latest, capped = [], "", []
    for kind, types, amount_from in GROUPS:
        merged = []
        for ctype in types:
            _total, rows, truncated = client.search(committee_id, start, end, ctype, amount_from)
            merged += rows
            if truncated: capped.append(f"{year} {kind}/{ctype}")
        for r in merged:
            m, d, y = r["date"].split("/")
            latest = max(latest, f"{y}-{m}-{d}")
        out += [(kind, name, s, c) for name, s, c in _top(aggregate(merged))]
    return out, latest, capped


def _committee_url(committee_id):
    return f"{BASE}/sooDetail.do?cneCommitteeId={committee_id}"


def _store_year(conn, rep, year, committee_id, rows, latest):
    stamp, url = _now(), _committee_url(committee_id)
    conn.execute("DELETE FROM rep_donors WHERE rep=? AND cycle=? AND url LIKE ?", (rep, year, URL_PREFIX))
    for kind, donor, amount, n in rows:
        conn.execute("INSERT OR REPLACE INTO rep_donors (rep, cycle, kind, donor, amount, n, note, url, fetched_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (rep, year, kind, donor, amount, n, "", url, stamp))
    by_kind = lambda k: sum(a for kind, _d, a, _n in rows if kind == k)
    conn.execute("INSERT OR REPLACE INTO rep_finance_totals VALUES (?,?,?,?,?,?,?,?,?)",
                 (rep, year, sum(a for _k, _d, a, _n in rows), by_kind("individual_large"), 0.0,
                  by_kind("pac"), latest, url, stamp))
    conn.commit()


def refresh_rep(conn, rep, since_year=DEFAULT_SINCE_YEAR, force=False, client=None):
    name = rep["name"]
    if _recent(conn, name) and not force: return {"skipped": True}
    committee_id = rep.get("orestar_committee_id")
    if not committee_id: raise RuntimeError(f"no orestar_committee_id for {name}; set it in config/representatives.yaml")
    client = client or OrestarClient()
    this_year = datetime.now().year
    have = {r[0] for r in conn.execute("SELECT DISTINCT cycle FROM rep_donors WHERE rep=? AND url LIKE ?", (name, URL_PREFIX))}
    done, rows_total, errors, warnings = [], 0, [], []
    for year in range(since_year, this_year + 1):
        if year in have and year != this_year and not force: continue
        try:
            rows, latest, capped = fetch_year(client, committee_id, year)
            warnings += [f"{c} hit the {MAX_PAGES * PAGE_ROWS}-row cap; sums for it may be low" for c in capped]
            _store_year(conn, name, year, committee_id, rows, latest)
            done.append(f"{year}-12-31"); rows_total += len(rows)
        except (requests.RequestException, RuntimeError, ValueError, AttributeError, sqlite3.Error) as exc:
            conn.rollback(); errors.append(f"{year}: {_safe(exc)}")
    # A weekly stamp only counts when nothing failed; otherwise the next run retries.
    if not errors: update_progress(conn, name, SOURCE, done, note=f"{rows_total} rows")
    conn.commit()
    return {"cycles": len(done), "rows": rows_total, "errors": errors, "warnings": warnings}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh"); refresh.add_argument("--rep"); refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--since-year", type=int, default=DEFAULT_SINCE_YEAR)
    args = parser.parse_args(argv)
    reps = [r for r in iter_reps(load_representatives()) if r["chamber_hint"] in STATE_HINTS
            and (not args.rep or r["name"].casefold() == args.rep.casefold())]
    if not reps: parser.error("no matching Oregon state representatives")
    conn = get_connection()
    failed = False
    try:
        ensure_schema(conn); conn.commit()
        client = OrestarClient()
        for rep in reps:
            try: result = refresh_rep(conn, rep, args.since_year, args.force, client)
            except (requests.RequestException, RuntimeError, sqlite3.Error, ValueError, AttributeError) as exc:
                conn.rollback(); result = {"errors": [_safe(exc)]}
            if result.get("skipped"): print(f"{rep['name']}: skipped (pulled <7 days ago)")
            else: print(f"{rep['name']}: {result.get('cycles', 0)} cycles, {result.get('rows', 0)} rows")
            for warning in result.get("warnings", []): print(f"  WARNING: {warning}")
            for error in result.get("errors", []): print(f"  ERROR: {error}"); failed = True
        return int(failed)
    finally: conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
