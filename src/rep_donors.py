"""Who funds each federal rep, from the FEC API (api.open.fec.gov). FEC-reported only.

Everything stored here is "FEC-reported": itemized money as filed by the committees.
Employer text is donor-supplied and sector labels are keyword heuristics, so nothing
in this module is ever "verified" the way a quote matched at its source is.
"""
import argparse, re, sqlite3, sys, time
from datetime import datetime, timezone
import requests
from src.config import get_env, load_representatives
from src.db import get_connection
from src.http_cache import cached_get
from src.rep_record import _dicts, _now, iter_reps, update_progress

FEC = "https://api.open.fec.gov/v1"
PACE_SECONDS = 0.6
WEEK_SECONDS = 7 * 86400
DEFAULT_SINCE_CYCLE = 2014
TOP_N = 25
SOURCE = "donors:fec"
_sleep = time.sleep
NOT_REAL_EMPLOYERS = {"NOT EMPLOYED", "SELF EMPLOYED", "SELF-EMPLOYED", "RETIRED", "NONE", "N/A", "NA",
                      "INFORMATION REQUESTED", "REQUESTED", "HOMEMAKER", "UNEMPLOYED", "NULL", ""}
SIZE_LABELS = {0: "$0-199", 200: "$200-499", 500: "$500-999", 1000: "$1000-1999", 2000: "$2000+"}
SECTOR_KEYWORDS = {
    "pharma/health": ["pharma", "pfizer", "merck", "amgen", "abbvie", "johnson & johnson", "health", "medical",
                      "hospital", "biotech", "unitedhealth", "anthem", "cigna"],
    "securities/finance": ["capital", "securities", "investment", "bank", "blackstone", "goldman", "jpmorgan",
                           "citadel", "private equity", "hedge", "asset management"],
    "tech": ["google", "alphabet", "amazon", "meta", "facebook", "microsoft", "apple", "nvidia", "oracle",
             "software", "technology", "openai", "coinbase", "multicoin", "crypto", "web3"],
    "energy": ["oil", "gas", "petroleum", "exxon", "chevron", "energy", "utility", "power", "coal"],
    "defense": ["lockheed", "boeing", "raytheon", "northrop", "general dynamics", "defense", "aerospace"],
    "labor": ["union", "workers", "afl", "seiu", "teamsters", "ibew", "nea ", "afscme"],
    "israel-related PACs": ["aipac", "united democracy project", "democratic majority for israel", "israel"],
}
JURISDICTION = {"finance": ["pharma/health", "securities/finance", "tech"], "banking": ["securities/finance"],
                "energy": ["energy"], "health": ["pharma/health"], "armed services": ["defense"],
                "commerce": ["tech"], "intelligence": ["tech", "defense"]}
# Money moving between the candidate's own committees, joint fundraisers and ActBlue's conduit is not a donor.
CONDUIT_WORDS = ("victory fund", "actblue", "winred")  # the rep's own committee is excluded by name in _rows_pac
CONFLICT_KINDS = ("employer", "pac", "outside_spend")  # outside_spend counts only when it SUPPORTS the rep; leadership_pac is the rep's own money, not a donor


def ensure_schema(conn):
    for sql in """
        CREATE TABLE IF NOT EXISTS rep_donors (
          id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT NOT NULL, cycle INTEGER NOT NULL, kind TEXT NOT NULL,
          donor TEXT NOT NULL, amount REAL NOT NULL, n INTEGER, note TEXT NOT NULL DEFAULT '', url TEXT,
          fetched_at TEXT NOT NULL, UNIQUE(rep, cycle, kind, donor, note));
        CREATE TABLE IF NOT EXISTS rep_finance_totals (
          rep TEXT NOT NULL, cycle INTEGER NOT NULL, receipts REAL, individual_itemized REAL,
          individual_unitemized REAL, pac_contributions REAL, coverage_end TEXT, url TEXT,
          fetched_at TEXT NOT NULL, PRIMARY KEY(rep, cycle));
        CREATE INDEX IF NOT EXISTS idx_rep_donors_rep_cycle ON rep_donors(rep, cycle)
    """.split(";"):
        if sql.strip(): conn.execute(sql)


def _get(path, **params):
    _sleep(PACE_SECONDS)
    params.setdefault("per_page", 100)
    key = get_env("CONGRESS_API_KEY", "")
    if not key: raise RuntimeError("CONGRESS_API_KEY (an api.data.gov key) is not set; FEC rejects empty keys")
    return cached_get(f"{FEC}/{path}", {**params, "api_key": key}, ttl_seconds=24 * 3600)


def _safe(exc):
    """Exception text with the api key blanked: requests puts the full URL, key included, in HTTP errors."""
    return re.sub(r"api_key=[^&\s]+", "api_key=***", str(exc))


def _num(value): return float(value) if value else 0.0


def resolve_candidate_id(rep):
    if rep.get("fec_candidate_id"): return rep["fec_candidate_id"]
    last = rep["name"].replace(" Jr.", "").split()[-1]
    office = "S" if rep["chamber_hint"] == "Senate" else "H"
    year = datetime.now().year
    for hit in _get("candidates/search/", q=last, office=office, state="OR").get("results", []):
        if last.lower() in (hit.get("name") or "").lower() and any(y >= year - 1 for y in hit.get("election_years") or []):
            return hit["candidate_id"]
    raise RuntimeError(f"no FEC candidate id for {rep['name']}; set fec_candidate_id in config")


def _rows_employer(committee, cycle):
    rows = _get("schedules/schedule_a/by_employer/", committee_id=committee, cycle=cycle, sort="-total",
                per_page=TOP_N * 2).get("results", [])
    keep = [r for r in rows if (r.get("employer") or "").strip().upper() not in NOT_REAL_EMPLOYERS]
    return [("employer", r["employer"].strip(), _num(r.get("total")), r.get("count"), "") for r in keep[:TOP_N]]


def _rows_size(committee, cycle):
    rows = _get("schedules/schedule_a/by_size/", committee_id=committee, cycle=cycle).get("results", [])
    return [("size", SIZE_LABELS[r["size"]], _num(r.get("total")), r.get("count"), "")
            for r in rows if r.get("size") in SIZE_LABELS and r.get("total")]


def _rows_pac(committee, cycle, own_name=""):
    sums, counts, params = {}, {}, {}
    for _ in range(3):
        page = _get("schedules/schedule_a/", committee_id=committee, two_year_transaction_period=cycle,
                    is_individual="false", sort="-contribution_receipt_amount", **params)
        rows = page.get("results", [])
        if not rows: break
        for r in rows:
            name = (r.get("contributor_name") or "").strip().upper()
            amount = _num(r.get("contribution_receipt_amount"))
            own = own_name and own_name.upper() in name
            conduit = any(w in name.lower() for w in CONDUIT_WORDS)
            if name and amount > 0 and not own and not conduit and r.get("entity_type") in {"PAC", "COM", "PTY"}:  # ORG rows are refunds/conduits (corporations cannot give)
                sums[name] = sums.get(name, 0.0) + amount
                counts[name] = counts.get(name, 0) + 1
        last = (page.get("pagination") or {}).get("last_indexes") or {}
        if not last.get("last_index"): break
        params = {"last_index": last["last_index"],
                  "last_contribution_receipt_amount": last.get("last_contribution_receipt_amount")}
    top = sorted(sums.items(), key=lambda kv: -kv[1])[:TOP_N]
    return [("pac", name, amount, counts[name], "") for name, amount in top]


def _rows_leadership(candidate, cycle):
    rows = []
    for pac in _get("committees/", sponsor_candidate_id=candidate, per_page=20).get("results", []):
        try: totals = _get(f"committee/{pac['committee_id']}/totals/", cycle=cycle).get("results") or [{}]
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404: continue  # the PAC had no filings that cycle
            raise
        if _num(totals[0].get("receipts")):
            rows.append(("leadership_pac", pac["name"], _num(totals[0]["receipts"]), None, "", pac["committee_id"]))
    return rows


def _rows_outside(candidate, cycle):
    rows = _get("schedules/schedule_e/by_candidate/", candidate_id=candidate, cycle=cycle).get("results", [])
    rows = sorted((r for r in rows if _num(r.get("total")) > 0), key=lambda r: -_num(r["total"]))[:TOP_N]
    return [("outside_spend", r["committee_name"], _num(r["total"]), r.get("count"),
             "support" if r.get("support_oppose_indicator") == "S" else "oppose") for r in rows if r.get("committee_name")]


def _url(kind, candidate, committee, cycle, extra=None):
    if kind == "leadership_pac": return f"https://www.fec.gov/data/committee/{extra}/?cycle={cycle}"
    if kind == "outside_spend": return f"https://www.fec.gov/data/independent-expenditures/?candidate_id={candidate}&cycle={cycle}"
    if kind == "pac": return f"https://www.fec.gov/data/receipts/?committee_id={committee}&two_year_transaction_period={cycle}&is_individual=false"
    return f"https://www.fec.gov/data/receipts/individual-contributions/?committee_id={committee}&two_year_transaction_period={cycle}"


def fetch_cycle(candidate, committees, cycle, last_name=""):
    """All donor rows for one cycle: (kind, donor, amount, n, note, url)."""
    out = []
    for committee in committees:
        for rows in (_rows_employer(committee, cycle), _rows_size(committee, cycle), _rows_pac(committee, cycle, last_name)):
            out += [(*r[:5], _url(r[0], candidate, committee, cycle)) for r in rows]
    out += [(*r[:5], _url(r[0], candidate, None, cycle, r[5])) for r in _rows_leadership(candidate, cycle)]
    out += [(*r[:5], _url(r[0], candidate, None, cycle)) for r in _rows_outside(candidate, cycle)]
    return out


def _store_cycle(conn, rep, cycle, total, rows):
    stamp = _now()
    conn.execute("DELETE FROM rep_donors WHERE rep=? AND cycle=?", (rep, cycle))
    for kind, donor, amount, n, note, url in rows:
        conn.execute("INSERT OR REPLACE INTO rep_donors (rep, cycle, kind, donor, amount, n, note, url, fetched_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (rep, cycle, kind, donor, amount, n, note, url, stamp))
    if total: conn.execute("INSERT OR REPLACE INTO rep_finance_totals VALUES (?,?,?,?,?,?,?,?,?)", (rep, cycle, *total, stamp))
    conn.commit()


def _recent(conn, rep):
    row = conn.execute("SELECT updated_at FROM backfill_progress WHERE rep=? AND source=?", (rep, SOURCE)).fetchone()
    if not row or not row[0]: return False
    try: return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < WEEK_SECONDS
    except ValueError: return False


def refresh_rep(conn, rep, since_cycle=DEFAULT_SINCE_CYCLE, force=False):
    if _recent(conn, rep["name"]) and not force: return {"skipped": True}
    candidate = resolve_candidate_id(rep)
    cycles = sorted(c for c in _get(f"candidate/{candidate}/").get("results", [{}])[0].get("cycles", [])
                    if c >= since_cycle and c % 2 == 0)
    totals = {t["cycle"]: t for t in _get(f"candidate/{candidate}/totals/", election_full="false", per_page=30).get("results", [])
              if t.get("cycle")}
    principal = _get(f"candidate/{candidate}/committees/", designation="P", per_page=20).get("results", [])
    have = {r[0] for r in conn.execute("SELECT cycle FROM rep_finance_totals WHERE rep=? UNION SELECT cycle FROM rep_donors WHERE rep=?",
                                       (rep["name"], rep["name"]))}
    done, rows_total, errors = [], 0, []
    for cycle in cycles:
        if cycle in have and cycle != cycles[-1] and not force: continue
        try:
            committees = [c["committee_id"] for c in principal if cycle in (c.get("cycles") or [])]
            rows = fetch_cycle(candidate, committees, cycle, rep["name"].replace(" Jr.", "").split()[-1])
            t = totals.get(cycle)
            total = (_num(t.get("receipts")), _num(t.get("individual_itemized_contributions")),
                     _num(t.get("individual_unitemized_contributions")), _num(t.get("other_political_committee_contributions")),
                     (t.get("coverage_end_date") or "")[:10],
                     f"https://www.fec.gov/data/candidate/{candidate}/?cycle={cycle}") if t else None
            _store_cycle(conn, rep["name"], cycle, total, rows)
            done.append(f"{cycle}-12-31"); rows_total += len(rows)
        except (requests.RequestException, KeyError, ValueError) as exc:
            conn.rollback(); errors.append(f"{cycle}: {_safe(exc)}")
    # A weekly stamp only counts when nothing failed; otherwise the next run retries.
    if not errors: update_progress(conn, rep["name"], SOURCE, done, note=f"{rows_total} rows")
    conn.commit()
    return {"cycles": len(done), "rows": rows_total, "errors": errors}


def _money(value): return f"${value:,.0f}"


def _source_for(conn, rep):
    """(label, footer) by where the rep's rows came from: state reps are ORESTAR, federal reps are FEC."""
    if conn.execute("SELECT 1 FROM rep_donors WHERE rep=? AND url LIKE 'https://secure.sos.state.or.us/%' LIMIT 1", (rep,)).fetchone():
        return "ORESTAR-reported", ("Source: ORESTAR (Oregon Secretary of State). Names are filer-supplied; totals are lower bounds "
                                    "(organizational and $1,000+ individual gifts only); treat as reported, not verified.")
    return "FEC-reported", "Source: FEC (api.open.fec.gov). Employer/industry labels come from donor-supplied text; treat as reported, not verified."


def show(conn, rep, limit=8):
    label, footer = _source_for(conn, rep)
    print(f"{label} finances for {rep}")
    for t in _dicts(conn, "SELECT * FROM rep_finance_totals WHERE rep=? ORDER BY cycle DESC", (rep,)):
        print(f"{t['cycle']}: receipts {_money(t['receipts'])}, individual itemized {_money(t['individual_itemized'])}, "
              f"unitemized {_money(t['individual_unitemized'])}, PAC {_money(t['pac_contributions'])}, "
              f"coverage to {t['coverage_end'] or '-'} | {t['url']}")
        for kind in ("employer", "pac", "leadership_pac", "outside_spend"):
            for d in _dicts(conn, "SELECT * FROM rep_donors WHERE rep=? AND cycle=? AND kind=? ORDER BY amount DESC LIMIT ?",
                            (rep, t["cycle"], kind, limit)):
                print(f"  {'pac (largest itemized receipts)' if kind == 'pac' else kind} | {d['donor']}{' (' + d['note'] + ')' if d['note'] else ''} | {_money(d['amount'])} | n={d['n']}")
    print(footer)


def _has_word(low, word):
    """Short keywords ('meta', 'oil', 'gas') must be whole words; longer ones may be prefixes ('health' in 'healthcare')."""
    word = word.strip()
    return re.search(r"\b" + re.escape(word) + (r"\b" if len(word) <= 4 else ""), low) is not None


def sector_for(name):
    low = (name or "").lower()
    return [s for s, words in SECTOR_KEYWORDS.items() if any(_has_word(low, w) for w in words)]


def conflicts(conn, rep):
    try:
        from src.committees import rep_committees
        committees = [c["name"] for c in rep_committees().get(rep, [])]
    except Exception as exc:  # roster is a network fetch; the sector view is still useful without it
        committees = []; print(f"(committee roster unavailable: {_safe(exc)})")
    cycles = [r[0] for r in conn.execute("SELECT DISTINCT cycle FROM rep_donors WHERE rep=? ORDER BY cycle DESC LIMIT 3", (rep,))]
    sectors = {}
    for d in _dicts(conn, f"SELECT * FROM rep_donors WHERE rep=? AND cycle IN ({','.join('?' * len(cycles))}) "
                          f"AND kind IN ({','.join('?' * len(CONFLICT_KINDS))}) AND note != 'oppose'", (rep, *cycles, *CONFLICT_KINDS)) if cycles else []:
        for sector in sector_for(d["donor"]):
            bucket = sectors.setdefault(sector, {})
            bucket[d["donor"]] = bucket.get(d["donor"], 0.0) + d["amount"]
    label, _footer = _source_for(conn, rep)
    print(f"{label} donor sectors for {rep} (cycles {', '.join(map(str, cycles)) or 'none'}); sector labels are keyword heuristics, not findings.")
    for sector, donors in sorted(sectors.items(), key=lambda kv: -sum(kv[1].values())):
        top = sorted(donors.items(), key=lambda kv: -kv[1])[:3]
        hooks = sorted({c for c in committees if ":" not in c
                        for key, secs in JURISDICTION.items() if key in c.lower() and sector in secs})
        print(f"{sector}: {_money(sum(donors.values()))} | " + "; ".join(f"{n} {_money(a)}" for n, a in top)
              + "".join(f" <- sits on {h}" for h in hooks))
    print(_footer.split(". ")[0] + "; reported, not verified.")


TOPIC_STOPWORDS = {"oregon", "national", "action", "committee", "political", "america", "american", "united", "states",
                   "letter", "support", "oppose", "federal", "senate", "house", "congress", "bill", "vote"}
KIND_PHRASE = {"employer": "donors employed by {d} gave {a}",
               "pac": "{d} (PAC) gave {a} in itemized contributions (largest receipts only)",
               "leadership_pac": "the rep's leadership PAC {d} raised {a}",
               "outside_spend": "{d} spent {a} on independent expenditures filed as '{note}' of the rep"}


def donor_context(conn, rep, topic, limit=2):
    """record_context-shaped items: this rep's FEC-reported donors whose name contains a topic word."""
    words = [w for w in re.findall(r"[a-z0-9.]{4,}", (topic or "").lower()) if w not in TOPIC_STOPWORDS]
    if not words: return []
    where = " OR ".join("lower(donor) LIKE ?" for _ in words)
    try:
        rows = _dicts(conn, f"SELECT * FROM rep_donors WHERE rep=? AND kind != 'size' AND ({where}) "
                            "ORDER BY cycle DESC, amount DESC LIMIT ?", (rep, *[f"%{w}%" for w in words], limit))
    except sqlite3.OperationalError: return []
    return [dict(kind="donor", date=f"{r['cycle']}-12-31", label="FEC-reported",
                 text=KIND_PHRASE[r["kind"]].format(d=r["donor"], a=_money(r["amount"]), note=r["note"]) + f" in the {r['cycle']} cycle",
                 url=r["url"]) for r in rows]


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh"); refresh.add_argument("--rep"); refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--since-cycle", type=int, default=DEFAULT_SINCE_CYCLE)
    for name in ("show", "conflicts"): sub.add_parser(name).add_argument("rep")
    sub.choices["show"].add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)
    conn = get_connection()
    try:
        ensure_schema(conn); conn.commit()
        if args.command == "show": show(conn, args.rep, args.limit)
        elif args.command == "conflicts": conflicts(conn, args.rep)
        else:
            reps = [r for r in iter_reps(load_representatives()) if r["chamber_hint"] in {"Senate", "House"}
                    and (not args.rep or r["name"].casefold() == args.rep.casefold())]
            if not reps: parser.error("no matching federal representatives")
            failed = False
            for rep in reps:
                try: result = refresh_rep(conn, rep, args.since_cycle, args.force)
                except (requests.RequestException, RuntimeError, sqlite3.Error, ValueError, IndexError, KeyError) as exc:
                    conn.rollback(); result = {"errors": [_safe(exc)]}
                if result.get("skipped"): print(f"{rep['name']}: skipped (pulled <7 days ago)")
                else: print(f"{rep['name']}: {result.get('cycles', 0)} cycles, {result.get('rows', 0)} rows")
                for error in result.get("errors", []): print(f"  ERROR: {error}"); failed = True
            return int(failed)
        return 0
    finally: conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
