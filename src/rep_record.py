"""Representative statements, shared schema and record CLI for ticket #5863.
rep_votes collectors take (conn, rep, since[, or_session]); importer (conn, file).
"""
import argparse, hashlib, html, re, sqlite3, sys, time
import xml.etree.ElementTree as ET
from datetime import date as Date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import requests
from src.config import load_representatives
from src.db import get_connection
from src.http_cache import cached_get
PACE_SECONDS = 1.0
MAX_PAGES_PER_FEED = 25
ODATA = "https://api.oregonlegislature.gov/odata/odataservice.svc"

def ensure_schema(conn):
    for sql in """
        CREATE TABLE IF NOT EXISTS rep_statements (
          id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT NOT NULL, date TEXT, title TEXT, venue TEXT,
          quote TEXT, quote_kind TEXT, url TEXT NOT NULL, source_kind TEXT NOT NULL DEFAULT 'official',
          verified INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS rep_bills (
          id INTEGER PRIMARY KEY AUTOINCREMENT, rep TEXT NOT NULL, session TEXT NOT NULL, bill_id TEXT NOT NULL,
          role TEXT NOT NULL, title TEXT, date TEXT, url TEXT, fetched_at TEXT NOT NULL,
          UNIQUE(rep, session, bill_id, role));
        CREATE TABLE IF NOT EXISTS voting_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, representative TEXT NOT NULL, vote_date TEXT NOT NULL,
          bill_id TEXT, bill_title TEXT, vote TEXT NOT NULL, chamber TEXT, session TEXT, source_url TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS backfill_progress (
          rep TEXT NOT NULL, source TEXT NOT NULL, cursor TEXT, oldest_date TEXT, newest_date TEXT,
          items INTEGER NOT NULL DEFAULT 0, exhausted INTEGER NOT NULL DEFAULT 0, note TEXT,
          updated_at TEXT NOT NULL, PRIMARY KEY (rep, source));
        CREATE INDEX IF NOT EXISTS idx_rep_statements_rep_date ON rep_statements(rep, date);
        CREATE INDEX IF NOT EXISTS idx_rep_bills_rep ON rep_bills(rep)
    """.split(";"):
        if sql.strip(): conn.execute(sql)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(voting_records)")}
    for column in ("roll_url", "question"):
        if column not in columns: conn.execute(f"ALTER TABLE voting_records ADD COLUMN {column} TEXT")
def update_progress(conn, rep, source, dates: list[str], cursor=None, exhausted=None, note=None):
    valid = [d for d in dates if d]
    conn.execute("""
        INSERT INTO backfill_progress VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rep, source) DO UPDATE SET
          oldest_date=min(coalesce(backfill_progress.oldest_date, excluded.oldest_date), coalesce(excluded.oldest_date, backfill_progress.oldest_date)),
          newest_date=max(coalesce(backfill_progress.newest_date, excluded.newest_date), coalesce(excluded.newest_date, backfill_progress.newest_date)),
          items=backfill_progress.items+excluded.items, cursor=coalesce(excluded.cursor, backfill_progress.cursor),
          exhausted=coalesce(?, backfill_progress.exhausted),
          note=coalesce(excluded.note, backfill_progress.note), updated_at=excluded.updated_at
    """, (rep, source, cursor, min(valid, default=None), max(valid, default=None),
          len(dates), int(exhausted or 0), note, _now(), exhausted))
def coverage_pct(oldest_date, since_year, today):
    if not oldest_date or not since_year: return None
    try:
        today = Date.fromisoformat(today) if isinstance(today, str) else today
        span = (today - Date(int(since_year), 1, 1)).days
        return max(0, min(100, round(100 * (today - Date.fromisoformat(oldest_date[:10])).days / span))) if span > 0 else None
    except (ValueError, TypeError): return None

def iter_reps(cfg) -> list[dict]:
    reps = []
    for group, section, hint in (("federal", "senators", "Senate"), ("federal", "house", "House"),
                                 ("state", "senate", "OR_Senate"), ("state", "house", "OR_House"),
                                 ("state", "governor", "Governor")):
        parent = cfg.get(group, {})
        entries = parent.get(section, []) if isinstance(parent, dict) else []
        for rep in entries if isinstance(entries, list) else []:
            if isinstance(rep, dict) and rep.get("name"):
                reps.append({**rep, "title": rep.get("title", ""), "feeds": rep.get("feeds") or [],
                             "or_code": rep.get("or_legislator_code"), "chamber_hint": hint})
    return reps
def fetch_text(url):
    try: return cached_get(url, as_text=True, ttl_seconds=6 * 3600)
    finally: time.sleep(PACE_SECONDS)
def odata(entity, filter_str):
    try: return cached_get(f"{ODATA}/{entity}", {"$filter": filter_str, "$format": "json"})["value"]
    finally: time.sleep(PACE_SECONDS)

def normalize(s):
    return " ".join(html.unescape(s).translate(str.maketrans("\u2018\u2019\u201c\u201d\u00a0\u2013\u2014", "''\"\" --")).split())
def page_text(markup):
    markup = re.sub(r"<(script|style|nav|header|footer)\b[^>]*>.*?</\1\s*>", " ", markup, flags=re.I | re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())

def parse_rss(xml_text):
    items = []
    for node in ET.fromstring(xml_text).findall("./channel/item"):
        try:
            date = parsedate_to_datetime(node.findtext("pubDate") or "").date().isoformat()
        except (ValueError, TypeError, OverflowError, IndexError): date = None
        items.append(dict(title=normalize(node.findtext("title") or ""),
                          url=re.sub(r"^http://", "https://", (node.findtext("link") or "").strip(), flags=re.I),
                          date=date, body_html=node.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")))
    return items
class _Links(HTMLParser):
    def __init__(self, markup):
        super().__init__(); self.links = []; self.feed(markup)
    def handle_starttag(self, tag, attrs):
        if tag == "a": self.links.extend(v for k, v in attrs if k == "href" and v)
def parse_listing(markup, base_url, link_prefix):
    urls = []
    for href in _Links(markup).links:
        url = urljoin(base_url, href)
        path = urlsplit(url).path
        if path.startswith(link_prefix) and path != link_prefix and "/table/" not in url and "?" not in url:
            urls.append(url)
    return list(dict.fromkeys(urls))
_MONTHS = "January February March April May June July August September October November December".split()
def find_date(text):
    match = re.search(r"\b(" + "|".join(_MONTHS) + r") (\d{1,2}), (\d{4})\b", text)
    try:
        return Date(int(match[3]), _MONTHS.index(match[1]) + 1, int(match[2])).isoformat() if match else None
    except ValueError: return None
def find_venue(text):
    match = re.search(r"(WASHINGTON, D\.?C\.?|Washington, D\.?C\.?|WASHINGTON\b|"
                      r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)?, (?:OR\b|Ore\.(?!\w)|Oregon\b))(?=.{0,3}[-\u2013\u2014])", text[:1500])
    return ("Washington, D.C." if match[0].startswith(("WASHINGTON", "Washington,")) else normalize(match[0])) if match else "not stated"

_QUOTE = re.compile(r'["\u201c]([^"\u201c\u201d]{40,900})["\u201d]')
_SPEECH = re.compile(r"\b(said|says|stated|added|continued|noted)\b", re.I)
_JOINT = re.compile(r"\b(wrote|letter|lawmakers|members|signed)\b", re.I)

_ABBREV = {"rep", "sen", "gov", "dr", "mr", "ms", "mrs", "jr", "st", "u.s", "d.c", "ore"}

def _sentence_ends(fragment):
    """Offsets of '. ' sentence breaks, skipping titles like 'Rep. ' and 'D-Ore.'."""
    for match in re.finditer(r"\.\s", fragment):
        word = re.split(r"[\s(\-]", fragment[:match.start()])[-1].lower()
        if word not in _ABBREV:
            yield match.start()

def _clause_after(text, end):
    """Attribution right after a quote: to the next quote mark or sentence end, max 80 chars."""
    clause = re.split(r'["\u201c\u201d]', text[end:end + 80], maxsplit=1)[0]
    return clause[:next(_sentence_ends(clause), len(clause))]

def _clause_before(text, start):
    """Lead-in sentence right before a quote ('Hoyle said. "...'), back to the previous quote mark."""
    clause = re.split(r'["\u201c\u201d]', text[max(0, start - 80):start])[-1].rstrip()
    breaks = [i for i in _sentence_ends(clause + " ") if i < len(clause) - 1]
    return clause[breaks[-1] + 1:] if breaks else clause

def extract_quotes(text, last_name):
    """Quotes attributable to this rep. Only the clause touching each quote counts, so a
    co-signer's quote ('..." said Rep. Zinke.') is never credited to the rep."""
    name = re.compile(r"\b" + re.escape(last_name) + r"\b", re.I)
    found = {}
    for match in _QUOTE.finditer(text):
        after, before = _clause_after(text, match.end()), _clause_before(text, match.start())
        if name.search(after) and _SPEECH.search(after):
            kind = "personal"
        elif name.search(before) and _SPEECH.search(before) and not _SPEECH.search(after):
            kind = "personal"  # continuation: 'Hoyle said. "Second quote."'
        elif _JOINT.search(after) and not _SPEECH.search(after):
            kind = "joint_letter"
        else:
            continue  # someone else's words, or unattributable
        quote = normalize(match[1]).rstrip(",").rstrip()
        if found.get(quote) != "personal":
            found[quote] = kind
    rows = [{"quote": q, "kind": k} for q, k in found.items()]
    return sorted(rows, key=lambda row: row["kind"] != "personal")[:3]
def verify_quote(quote, source_text): return normalize(quote) in normalize(source_text)
def content_hash(rep, url, quote): return hashlib.sha256(f"{rep}|{url}|{quote or ''}".encode("utf-8")).hexdigest()
def _now(): return datetime.now(timezone.utc).isoformat()
def _dicts(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    return [dict(zip([c[0] for c in cursor.description], row)) for row in cursor]
def _save_statement(conn, row):
    row = {**row, "fetched_at": _now(), "content_hash": content_hash(row["rep"], row["url"], row["quote"])}
    cursor = conn.execute(f"INSERT OR IGNORE INTO rep_statements ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    return {**row, "id": cursor.lastrowid} if cursor.rowcount else None
def _attempt(rep, source, function, *args):
    try: return function(*args)
    except (requests.RequestException, ValueError, ET.ParseError, OSError, RuntimeError) as exc:
        code = getattr(getattr(exc, "response", None), "status_code", "")
        rep.setdefault("_errors", []).append(f"{source}: {code} {exc}")
        return []
def _listing_item(url, generic_title=""):
    """One release page. A site-wide title (same as the listing page's) falls back to the URL slug."""
    raw = fetch_text(url)
    title = _title(raw)
    if not title or title == generic_title:
        title = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()
    return dict(url=url, title=title, date=find_date(page_text(raw)), body_html=raw)

def _title(raw):
    match = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', raw, re.I)
             or re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", raw, re.I | re.S)
             or re.search(r"<title\b[^>]*>(.*?)</title\s*>", raw, re.I | re.S))
    return page_text(match[1]).split(" | ", 1)[0] if match else ""

def collect_statements(conn, rep, since):
    rows = []
    tokens = rep["name"].split()
    while tokens and tokens[-1].rstrip(".").lower() in {"jr", "sr", "iii"}: tokens.pop()
    for feed in rep.get("feeds", []):
        kind, url = feed.get("kind"), feed.get("url")
        if kind not in {"rss", "listing"} or not url: continue
        start = len(rows)
        raw = _attempt(rep, url, fetch_text, url)
        items = _attempt(rep, url, parse_rss, raw) if kind == "rss" and raw else []
        if kind == "listing" and raw:
            urls = parse_listing(raw, url, feed.get("link_prefix", "/"))[:MAX_PAGES_PER_FEED]
            items = (_attempt(rep, link, _listing_item, link, _title(raw)) for link in urls)
        for item in items:
            if not item or not item.get("date") or item["date"] < since or not item.get("url"): continue
            text = page_text(item.get("body_html") or "")
            quotes = extract_quotes(text, tokens[-1]) if tokens else []
            for quote in quotes or [{"quote": None, "kind": None}]:
                row = _save_statement(conn, dict(rep=rep["name"], date=item["date"], title=item["title"],
                    venue=find_venue(text), quote=quote["quote"], quote_kind=quote["kind"], url=item["url"],
                    source_kind="official", verified=int(verify_quote(quote["quote"], text)) if quote["quote"] else 0))
                if row: rows.append(row)
        update_progress(conn, rep["name"], f"statements:{kind}", [r["date"] for r in rows[start:]])
    return rows

def add_media_statement(conn, rep, date, url, quote, title=None, venue=None):
    name = rep["name"] if isinstance(rep, dict) else rep
    official = conn.execute("SELECT quote FROM rep_statements WHERE rep=? AND source_kind='official' AND quote IS NOT NULL", (name,))
    verified = bool(normalize(quote)) and any(verify_quote(quote, r[0]) for r in official if r[0])  # media text must sit inside an official quote
    row = _save_statement(conn, dict(rep=name, date=date, url=url, quote=quote, title=title,
                                    venue=venue or "not stated", quote_kind="personal", source_kind="media", verified=int(verified)))
    return row or _dicts(conn, "SELECT * FROM rep_statements WHERE content_hash=?", (content_hash(name, url, quote),))[0]

def _vote_bill_jobs(rep, args):
    """(category, source, zero-arg job) for the bills/votes collectors that fit this rep."""
    from src import rep_votes as rv
    conn, hint = args.conn, rep["chamber_hint"]
    jobs = []
    if hint in {"Senate", "House"}:
        pages = 8 if args.since >= (Date.today() - timedelta(days=366)).isoformat() else 1000
        jobs.append(("bills", "bills:congress", lambda: rv.collect_federal_bills(conn, rep, args.since, pages)))
        if hint == "Senate" and rep.get("voteview_icpsr") and not args.skip_senate_votes:
            congress = rv.congress_for_year(Date.today().year)
            jobs.append(("votes", "votes:voteview-senate", lambda: rv.collect_senate_voteview(conn, rep, [congress])))
        elif hint == "House" and not args.skip_house_votes:
            jobs.append(("votes", "votes:congress-house", lambda: rv.collect_house_votes(conn, rep)))
    elif rep["or_code"]:
        jobs.append(("bills", "bills:oregon", lambda: rv.collect_or_bills(conn, rep, args.or_session)))
        jobs.append(("votes", "votes:oregon", lambda: rv.collect_or_votes(conn, rep, args.or_session)))
    return jobs

def _backfill_votes(conn, reps):
    """Whole-career Voteview roll calls for every federal rep: both chambers, every congress
    since in_office_since (a chamber the member was not in simply yields no rows)."""
    from src import rep_votes as rv
    last = rv.congress_for_year(Date.today().year)
    for rep in reps:
        rep["_errors"] = []
        if not rep.get("voteview_icpsr") or not rep.get("in_office_since"):
            continue
        first = rv.congress_for_year(rep["in_office_since"])
        for chamber in ("House", "Senate"):
            result = _attempt(rep, f"voteview-{chamber}", rv.collect_voteview, conn, rep, chamber,
                              range(first, last + 1), rep["voteview_icpsr"]) or {}
            conn.commit()
            print(f"{rep['name']} {chamber}: {result.get('inserted', 0)} new votes")
            for error in result.get("errors", []):
                print(f"  ERROR: {error['source']}: {error['error']}")
        for error in rep["_errors"]: print(f"  ERROR: {error}")

def _below_normal_priority():
    """Batch work must never compete with the person at the keyboard."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)
        except Exception: pass

def _refresh(conn, args, reps):
    failed = False
    for rep in reps:
        rep["_errors"] = []
        statements = collect_statements(conn, rep, args.since)
        conn.commit()  # a failed vote/bill source then rolls back only its own rows
        counts = {"bills": 0, "votes": 0}
        if not args.statements_only:
            for category, source, job in _vote_bill_jobs(rep, args):
                result = _attempt(rep, source, job) or {}
                counts[category] += result.get("inserted", 0)
                rep["_errors"].extend(f"{e['source']}: {e['error']}" for e in result.get("errors", []))
        conn.commit()
        print(f"{rep['name']}: {len(statements)} new statements, {counts['bills']} new bills, {counts['votes']} new votes")
        for row in sorted(statements, key=lambda r: r["date"] or "", reverse=True)[:5]:
            print(f"  {row['date']} | {row['title']} | {row['url']}")
        for error in rep["_errors"]: print(f"  ERROR: {error}")
        failed |= bool(rep["_errors"])
    return int(failed)

def _show(conn, rep, limit):
    for row in _dicts(conn, "SELECT * FROM rep_statements WHERE rep=? ORDER BY date DESC, id DESC LIMIT ?", (rep, limit)):
        tags = ("[no quote yet]" if not row["quote"] else "[verified]" if row["verified"]
                else "[reported, unverified]") + (" [joint letter]" if row["quote_kind"] == "joint_letter" else "")
        print(f"{row['date']} | {row['title']} {tags} | {row['url']}")
        if row["quote"]: print(f"  {row['quote']}")
    for row in _dicts(conn, "SELECT * FROM rep_bills WHERE rep=? ORDER BY date DESC, id DESC LIMIT ?", (rep, limit)):
        print(f"{row['date']} | {row['bill_id']} ({row['role']}) | {row['title']} | {row['url']}")
    for row in _dicts(conn, "SELECT * FROM voting_records WHERE representative=? ORDER BY vote_date DESC, id DESC LIMIT ?", (rep, limit)):
        print(f"{row['vote_date']} | {row['bill_id'] or ''} | {row['vote']} | {row['question'] or row['bill_title'] or ''} | {row['roll_url'] or row['source_url'] or ''}")

def _progress(conn, reps):
    for rep in reps:
        rows = _dicts(conn, "SELECT * FROM backfill_progress WHERE rep=? ORDER BY source", (rep["name"],))
        sources = {"statements:" + f["kind"] for f in rep["feeds"] if f.get("kind") in {"rss", "listing"}}
        hint = rep["chamber_hint"]
        if hint in {"Senate", "House"}:
            sources.update(("bills:congress-sponsor", "bills:congress-cosponsor",
                            "votes:voteview-senate" if hint == "Senate" else "votes:congress-house"))
        elif rep["or_code"]: sources.update(("bills:oregon", "votes:oregon"))
        known = {r["source"] for r in rows}
        rows += [dict(source=s, oldest_date=None, items=0, exhausted=0, note="") for s in sources - known]
        year = rep.get("in_office_since")
        for row in sorted(rows, key=lambda r: r["source"]):
            pct = coverage_pct(row["oldest_date"], year, Date.today())
            print(f"{rep['name']} | {row['source']} | {row['oldest_date'] or '-'} | {row['items']} | {str(pct) + '%' if pct is not None else '-'} | {row['exhausted']} | {row['note'] or ''}")
        oldest = min((r["oldest_date"] for r in rows if r["oldest_date"]), default=None)
        pct = coverage_pct(oldest, year, Date.today())
        print(f"{rep['name']} | all sources | {oldest or '-'} | {str(pct) + '%' if pct is not None else '-'}")

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True); refresh = sub.add_parser("refresh")
    refresh.add_argument("--since", type=lambda s: Date.fromisoformat(s).isoformat(), default=(Date.today() - timedelta(days=30)).isoformat())
    refresh.add_argument("--rep"); refresh.add_argument("--or-session", default="2026R1")
    refresh.add_argument("--skip-house-votes", action="store_true"); refresh.add_argument("--skip-senate-votes", action="store_true"); refresh.add_argument("--statements-only", action="store_true")
    sub.add_parser("import-senate").add_argument("file")
    media = sub.add_parser("add-media")
    for flag in ("rep", "date", "url", "quote", "title", "venue"):
        media.add_argument("--" + flag, required=flag in {"rep", "date", "url", "quote"},
                           type=(lambda s: Date.fromisoformat(s).isoformat()) if flag == "date" else str)
    show = sub.add_parser("show"); show.add_argument("rep"); show.add_argument("--limit", type=int, default=10)
    sub.add_parser("progress")
    backfill = sub.add_parser("backfill-votes", help="whole-career Voteview roll calls (slow, polite)")
    backfill.add_argument("--rep")
    args = parser.parse_args(argv)
    conn = get_connection()
    try:
        ensure_schema(conn); conn.commit()
        if args.command in {"refresh", "backfill-votes"}:
            reps = [r for r in iter_reps(load_representatives()) if not args.rep or r["name"].casefold() == args.rep.casefold()]
            if not reps: parser.error("no matching representatives")
            _below_normal_priority()
            if args.command == "backfill-votes":
                _backfill_votes(conn, reps); return 0
            args.conn = conn
            return _refresh(conn, args, reps)
        if args.command == "import-senate":
            from src.rep_votes import import_senate_votes
            print(import_senate_votes(conn, args.file))
        elif args.command == "add-media":
            row = add_media_statement(conn, args.rep, args.date, args.url, args.quote, args.title, args.venue)
            print(f"{row['id']} | {'[verified]' if row['verified'] else '[unverified]'} | {row['url']}")
        elif args.command == "show":
            if args.limit < 1: parser.error("--limit must be positive")
            _show(conn, args.rep, args.limit)
        elif args.command == "progress": _progress(conn, iter_reps(load_representatives()))
        conn.commit(); return 0
    except (requests.RequestException, sqlite3.Error, OSError, ValueError, ImportError, RuntimeError) as exc:
        conn.rollback(); print(f"ERROR: {exc}"); return 1
    finally: conn.close()
if __name__ == "__main__":
    raise SystemExit(main())
