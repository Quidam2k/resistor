"""FARA (Foreign Agents Registration Act) filings that mention a federal rep.

Pulls the DOJ efile API (registrants, foreign principals, filed documents),
scans the filed PDFs for mentions of each rep, and ranks countries by
registrations. Filed facts only. Registrants and principals are organizations.
Registered agents only; unregistered influence is invisible.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml

from src.config import DATA_DIR, load_representatives
from src.db import get_connection
from src import rep_record

API = "https://efile.fara.gov/api/v1"
HEADERS = {"User-Agent": "resistor-civic-research/1.0"}
MIN_GAP = 2.5
CACHE_TTL = 12 * 3600
DOC_TYPES = {"Supplemental Statement", "Informational Materials"}
FLAGS_PATH = Path(__file__).resolve().parent.parent / "config" / "fara_flags.yaml"
FOOTER = ("Registered agents only; unregistered influence isn't visible. A mention is a "
          "name match in a filed document, not proof of a meeting; read the excerpt.")
SPEND_NA = "n/a: not in the FARA API"
_last_net = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS fara_registrants(
  reg_number INTEGER PRIMARY KEY, name TEXT, reg_date TEXT, term_date TEXT,
  status TEXT, principals_at TEXT, docs_at TEXT);
CREATE TABLE IF NOT EXISTS fara_principals(
  reg_number INTEGER, fp_name TEXT, country TEXT, country_norm TEXT, status TEXT,
  UNIQUE(reg_number, fp_name, status));
CREATE TABLE IF NOT EXISTS fara_docs(
  url TEXT PRIMARY KEY, reg_number INTEGER, doc_type TEXT, doc_date TEXT,
  fp_name TEXT, fp_country TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS fara_hits(
  id INTEGER PRIMARY KEY, rep TEXT, reg_number INTEGER, registrant TEXT,
  doc_type TEXT, doc_date TEXT, kind TEXT, amount REAL, excerpt TEXT, url TEXT,
  UNIQUE(rep, url, kind, excerpt));
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pdf_name(url):
    """Cache file name for a PDF url. FARA file names embed registration number, date and sequence (unique)."""
    return os.path.basename(urlparse(url).path) or hashlib.sha1(url.encode()).hexdigest() + ".pdf"


def _dir(sub):
    p = Path(DATA_DIR) / "fara" / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- config ----------

def load_flags(path=None):
    p = Path(path) if path else FLAGS_PATH
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    d["values_flags"] = d.get("values_flags") or []
    d["country_aliases"] = {str(k).strip().upper(): v for k, v in (d.get("country_aliases") or {}).items()}
    d["icc"] = d.get("icc") or []
    d["iran_note"] = d.get("iran_note") or ""
    return d


def norm_country(name, flags):
    if not name or not str(name).strip():
        return ""
    s = str(name).strip()
    return (flags.get("country_aliases") or {}).get(s.upper()) or s.title()


# ---------- network (stub these in tests) ----------

def _pace():
    wait = MIN_GAP - (time.monotonic() - _last_net)
    if wait > 0:
        time.sleep(wait)


def _ok_url(url):
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname == "efile.fara.gov"


def _get(url, timeout):
    """Paced GET that backs off on 429/5xx (the API sits behind a rate limiter); raises after 4 tries.
    Redirects are followed by hand and each hop is host-checked BEFORE it is requested."""
    global _last_net
    for attempt in range(4):
        target = url
        for _hop in range(6):
            if not _ok_url(target):
                raise ValueError(f"refusing to fetch off efile.fara.gov: {target}")
            _pace()
            try:
                r = requests.get(target, headers=HEADERS, timeout=timeout, allow_redirects=False)
            finally:
                _last_net = time.monotonic()
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                target = urljoin(target, r.headers["Location"])
                continue
            break
        else:
            raise ValueError("too many redirects")
        if r.status_code not in (429, 500, 502, 503, 504):
            r.raise_for_status()
            return r
        if attempt == 3:
            r.raise_for_status()
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            wait = 0
        time.sleep(min(max(wait, 30 * 2 ** attempt), 300))


def fetch_json(url, ttl=CACHE_TTL):
    """GET url as JSON, disk-cached by sha1(url). Raises on any failure."""
    path = _dir("json") / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    r = _get(url, 60)
    data = r.json()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def fetch_pdf(url, dest):
    """Download a PDF to dest unless it already exists. Raises on failure."""
    dest = Path(dest)
    if dest.exists():
        return dest
    if not _ok_url(url):
        raise ValueError(f"refusing to fetch a non-FARA url: {url}")
    body = _get(url, 120).content
    if b"%PDF" not in body[:1024]:
        raise ValueError("response is not a PDF")
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, dest)
    return dest


def rows(payload):
    """Normalise {"ROWSET": ""} / one dict / list of dicts to a list of dicts."""
    if not isinstance(payload, dict):
        return []
    inner = None
    for key in ("ROWSET", "REGISTRANTS_ACTIVE", "REGISTRANTS_TERMINATED"):
        if key in payload:
            inner = payload[key]
            break
    if not isinstance(inner, dict):
        return []
    row = inner.get("ROW")
    if isinstance(row, dict):
        return [row]
    if isinstance(row, list):
        return [x for x in row if isinstance(x, dict)]
    return []


# ---------- scanning ----------

_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
_AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_CTX = {"Senate": re.compile(r"Senate|Senator|Oregon|D-OR", re.I),
        "House": re.compile(r"(?<!White )House|Representative|Congress(?:wo)?man|Oregon|D-OR", re.I)}
_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def _rep_patterns(rep):
    toks = [t for t in rep["name"].replace(",", " ").split() if t.rstrip(".").lower() not in _SUFFIX]
    last = re.escape(toks[-1])
    full = re.compile(r"\b" + r"\s+".join(re.escape(t) for t in toks) + r"\b", re.I)
    if rep["chamber"] == "Senate":
        titles = [r"\b(?:Senator|Sen\.|Senators)\s+(?:\w+\.?\s+){0,2}" + last + r"\b",
                  r"\bOffice of (?:Senator|Sen\.)\s+(?:\w+\s+){0,2}" + last + r"\b"]
    else:
        titles = [r"\b(?:Representative|Rep\.|Congresswoman|Congressman)\s+(?:\w+\.?\s+){0,2}" + last + r"\b",
                  r"\bOffice of (?:Representative|Rep\.|Congresswoman)\s+(?:\w+\s+){0,2}" + last + r"\b"]
    return full, [re.compile(t, re.I) for t in titles], re.compile(r"\b" + last + r"\b", re.I)


_SEC_START = re.compile(r"POLITICAL CONTRIBUTIONS|Response to Item 15\s*\(c\)")
_SEC_END = re.compile(r"V\s*-\s*INFORMATIONAL MATERIALS|Response to Item (?!15\s*\(c\))")


def _contrib_sections(text):
    """Spans of the political-contributions section (page footers repeat, so end only at the next form section)."""
    spans = []
    for m in _SEC_START.finditer(text):
        e = _SEC_END.search(text, m.end())
        spans.append((m.start(), e.start() if e else len(text)))
    return spans


def _excerpt(text, s, e):
    pad = max(0, (294 - (e - s)) // 2)
    pad = min(150, pad)
    a, b = max(0, s - pad), min(len(text), e + pad)
    return ("..." if a > 0 else "") + text[a:b] + ("..." if b < len(text) else "")


def scan_text(text, reps):
    """Return [{rep, kind, amount, excerpt}] for name matches in text."""
    text = re.sub(r"\s+", " ", text or "")
    sections = _contrib_sections(text)
    out, seen = [], set()
    for rep in reps:
        full, titles, bare = _rep_patterns(rep)
        spans = [m.span() for m in full.finditer(text)]
        for t in titles:
            spans += [m.span() for m in t.finditer(text)]
        for m in bare.finditer(text):
            ctx = text[max(0, m.start() - 60):m.start()] + " " + text[m.end():m.end() + 60]
            if _CTX[rep["chamber"]].search(ctx):
                spans.append(m.span())
        merged = []
        for s, e in sorted(spans):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            ex = _excerpt(text, s, e)
            kind, amount = "mention", None
            if any(a <= s < b for a, b in sections):
                kind = "contribution"  # an entry in the registrant's own political-contributions section
                am = _AMOUNT.search(_DATE.split(text[e:e + 120])[0])  # an amount past the next row's date is not this row's
                if am:
                    try:
                        amount = float(am.group(1).replace(",", ""))
                    except ValueError:
                        pass
            if (rep["name"], ex, kind) in seen:
                continue
            seen.add((rep["name"], ex, kind))
            out.append({"rep": rep["name"], "kind": kind, "amount": amount, "excerpt": ex})
    return out


def _fed_reps():
    out = []
    for r in rep_record.iter_reps(load_representatives()):
        if r.get("chamber_hint") in ("Senate", "House"):
            out.append({"name": r["name"], "chamber": r["chamber_hint"]})
    return out


def is_federal(name):
    return any(r["name"].lower() == name.strip().lower() for r in _fed_reps())


def _canon(name, reps=None):
    for r in (reps if reps is not None else _fed_reps()):
        if r["name"].lower() == name.strip().lower():
            return r["name"]
    return name.strip()


# ---------- refresh ----------

def _iso(us):
    try:
        return datetime.strptime(str(us).strip()[:10], "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _scope_sql(flags, all_regs):
    sql = "FROM fara_registrants r WHERE r.docs_at IS NULL"
    params = []
    if not all_regs:
        vf = flags["values_flags"]
        sql += (" AND EXISTS (SELECT 1 FROM fara_principals p WHERE p.reg_number=r.reg_number "
                "AND p.country_norm IN (%s))" % ",".join("?" * len(vf)))
        params = list(vf)
    return sql, params


def remaining(conn, flags, all_regs=False):
    sql, params = _scope_sql(flags, all_regs)
    return {
        "principals": conn.execute("SELECT COUNT(*) FROM fara_registrants WHERE principals_at IS NULL").fetchone()[0],
        "docs_lists": conn.execute("SELECT COUNT(*) " + sql, params).fetchone()[0],
        "pdfs": conn.execute("SELECT COUNT(*) FROM fara_docs WHERE state='pending'").fetchone()[0],
    }


def refresh(conn, max_minutes=120, since="2023-01-01", all_registrants=False, flags=None, reps=None, retry_errors=False,
            stale_days=7):
    ensure_schema(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # a registrant's lists are re-read after stale_days so newer filings and principals are picked up
    conn.execute("UPDATE fara_registrants SET docs_at=NULL WHERE docs_at < ?", (cutoff,))
    conn.execute("UPDATE fara_registrants SET principals_at=NULL WHERE principals_at < ?", (cutoff,))
    conn.commit()
    if retry_errors:
        conn.execute("UPDATE fara_docs SET state='pending' WHERE state='error'")
    flags = flags or load_flags()
    reps = _fed_reps() if reps is None else reps
    since_year = int(since[:4])
    deadline = time.monotonic() + max_minutes * 60
    late = lambda: time.monotonic() >= deadline
    errors, stopped = 0, False

    # phase 1: registrants
    for status, ep in (("active", "Active"), ("terminated", "Terminated")):
        if late():
            stopped = True
            break
        try:
            payload = fetch_json(f"{API}/Registrants/json/{ep}")
        except Exception as ex:
            errors += 1
            print(f"registrants {ep}: {ex}", file=sys.stderr)
            continue
        for row in rows(payload):
            try:
                num = int(row["Registration_Number"])
            except (KeyError, ValueError, TypeError):
                continue
            term = _iso(row.get("Termination_Date") or "")
            if status == "terminated" and (not term or int(term[:4]) < since_year):
                continue
            conn.execute(
                "INSERT INTO fara_registrants(reg_number,name,reg_date,term_date,status) VALUES(?,?,?,?,?) "
                "ON CONFLICT(reg_number) DO UPDATE SET name=excluded.name, reg_date=excluded.reg_date, "
                "term_date=excluded.term_date, "
                "principals_at=CASE WHEN status!=excluded.status THEN NULL ELSE principals_at END, "
                "status=excluded.status",
                (num, (row.get("Name") or "").strip(), _iso(row.get("Registration_Date") or ""), term or None, status))
        conn.commit()

    # phase 2: principals
    if not stopped:
        todo = conn.execute("SELECT reg_number, status FROM fara_registrants WHERE principals_at IS NULL "
                            "ORDER BY status, reg_number DESC").fetchall()
        for num, status in todo:
            if late():
                stopped = True
                break
            ep = "Active" if status == "active" else "Terminated"
            try:
                payload = fetch_json(f"{API}/ForeignPrincipals/json/{ep}/{num}")
            except Exception:
                errors += 1
                continue
            conn.execute("DELETE FROM fara_principals WHERE reg_number=?", (num,))
            for row in rows(payload):
                fp = (row.get("FP_NAME") or "").strip()
                if not fp:
                    continue
                c = (row.get("COUNTRY_NAME") or "").strip()
                conn.execute("INSERT OR IGNORE INTO fara_principals VALUES(?,?,?,?,?)",
                             (num, fp, c, norm_country(c, flags), status))
            conn.execute("UPDATE fara_registrants SET principals_at=? WHERE reg_number=?", (_now(), num))
            conn.commit()

    # phase 3: document lists for in-scope registrants
    if not stopped:
        sql, params = _scope_sql(flags, all_registrants)
        for (num,) in conn.execute("SELECT r.reg_number " + sql + " ORDER BY r.reg_number DESC", params).fetchall():
            if late():
                stopped = True
                break
            try:
                payload = fetch_json(f"{API}/RegDocs/json/{num}")
            except Exception:
                errors += 1
                continue
            for row in rows(payload):
                url = (row.get("URL") or "").strip()
                date = (row.get("DATE_STAMPED") or "")[:10]
                if not url or row.get("DOCUMENT_TYPE") not in DOC_TYPES or date < since:
                    continue
                conn.execute("INSERT OR IGNORE INTO fara_docs VALUES(?,?,?,?,?,?,'pending')",
                             (url, num, row["DOCUMENT_TYPE"], date, row.get("FOREIGN_PRINCIPAL_NAME"),
                              row.get("FOREIGN_PRINCIPAL_COUNTRY")))
            conn.execute("UPDATE fara_registrants SET docs_at=? WHERE reg_number=?", (_now(), num))
            conn.commit()

    # phase 4: download, convert, scan pending PDFs
    if not stopped:
        pending = conn.execute(
            "SELECT d.url, d.reg_number, d.doc_type, d.doc_date, COALESCE(r.name,'') FROM fara_docs d "
            "LEFT JOIN fara_registrants r ON r.reg_number=d.reg_number "
            "WHERE d.state='pending' ORDER BY d.doc_date DESC").fetchall()
        hits_n = 0
        for i, (url, num, dtype, ddate, rname) in enumerate(pending, 1):
            if late():
                stopped = True
                break
            state = "error"
            try:
                base = pdf_name(url)
                pdf = _dir("pdf") / base
                txt = _dir("pdf") / (base + ".txt")
                fetch_pdf(url, pdf)
                if not txt.exists():
                    try:
                        subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), str(txt)],
                                       timeout=60, check=True, capture_output=True)
                    except Exception:
                        txt.unlink(missing_ok=True)
                        raise
                text = txt.read_text(encoding="utf-8", errors="replace")
                if not text.strip():
                    state = "notext"
                else:
                    for h in scan_text(text, reps):
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO fara_hits(rep,reg_number,registrant,doc_type,doc_date,kind,"
                            "amount,excerpt,url) VALUES(?,?,?,?,?,?,?,?,?)",
                            (h["rep"], num, rname, dtype, ddate, h["kind"], h["amount"], h["excerpt"], url))
                        hits_n += cur.rowcount
                    state = "ok"
            except Exception:
                errors += 1
            conn.execute("UPDATE fara_docs SET state=? WHERE url=?", (state, url))
            conn.commit()
            if i % 25 == 0:
                print(f"  docs {i}/{len(pending)} processed, {hits_n} hits stored")

    if stopped:
        print(f"time cap of {max_minutes} min reached; stopping cleanly (re-run to resume)")
    print(f"errors this run: {errors} (list lookups retry next run; failed PDFs need --retry-errors)")
    print_status(conn, flags=flags, all_regs=all_registrants)
    return 0


def rescan(conn, reps=None):
    """Re-run the scanner over cached PDF text (no network). A doc's hits are replaced only when its text is cached."""
    ensure_schema(conn)
    reps = _fed_reps() if reps is None else reps
    n = skipped = 0
    for url, num, dtype, ddate, rname in conn.execute(
            "SELECT d.url, d.reg_number, d.doc_type, d.doc_date, COALESCE(r.name,'') FROM fara_docs d "
            "LEFT JOIN fara_registrants r ON r.reg_number=d.reg_number WHERE d.state='ok'").fetchall():
        txt = _dir("pdf") / (pdf_name(url) + ".txt")
        if not txt.exists():
            skipped += 1
            continue
        conn.execute("DELETE FROM fara_hits WHERE url=?", (url,))
        for h in scan_text(txt.read_text(encoding="utf-8", errors="replace"), reps):
            n += conn.execute(
                "INSERT OR IGNORE INTO fara_hits(rep,reg_number,registrant,doc_type,doc_date,kind,amount,excerpt,url) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (h["rep"], num, rname, dtype, ddate, h["kind"], h["amount"], h["excerpt"], url)).rowcount
    conn.commit()
    print(f"rescan: {n} hits stored; {skipped} docs skipped (no cached text, existing hits kept)")
    return 0


def print_status(conn, flags=None, all_regs=False):
    ensure_schema(conn)
    flags = flags or load_flags()
    q = lambda s, p=(): conn.execute(s, p).fetchone()[0]
    print("FARA data status")
    print(f"  registrants: {q('SELECT COUNT(*) FROM fara_registrants')} "
          f"(active {q('SELECT COUNT(*) FROM fara_registrants WHERE status=?', ('active',))}, "
          f"terminated {q('SELECT COUNT(*) FROM fara_registrants WHERE status=?', ('terminated',))})")
    print(f"  principals stored: {q('SELECT COUNT(*) FROM fara_principals')}")
    rem = remaining(conn, flags, all_regs)
    print(f"  left: principals lookups {rem['principals']} | document lists {rem['docs_lists']} | PDFs {rem['pdfs']}")
    states = dict(conn.execute("SELECT state, COUNT(*) FROM fara_docs GROUP BY state").fetchall())
    print("  docs: " + ", ".join(f"{k} {states.get(k, 0)}" for k in ("pending", "ok", "notext", "error")))
    print(f"  hits stored: {q('SELECT COUNT(*) FROM fara_hits')}")
    return 0


# ---------- output ----------

def show(conn, rep_name, limit=10):
    ensure_schema(conn)
    flags = load_flags()
    vf = set(flags["values_flags"])
    rep = _canon(rep_name)
    print(f"-- FARA: registered foreign agents' filings that mention {rep} (python -m src.fara show) --")
    q = lambda s, p=(): conn.execute(s, p).fetchone()[0]
    ok = q("SELECT COUNT(*) FROM fara_docs WHERE state='ok'")
    total = q("SELECT COUNT(*) FROM fara_docs")
    flagged_in = ",".join("?" * len(vf))
    nreg = q("SELECT COUNT(*) FROM fara_registrants WHERE docs_at IS NOT NULL OR reg_number IN "
             f"(SELECT reg_number FROM fara_principals WHERE country_norm IN ({flagged_in}))", list(vf))
    old, new = conn.execute("SELECT MIN(docs_at), MAX(docs_at) FROM fara_registrants WHERE docs_at IS NOT NULL").fetchone()
    last = f"document lists fetched {old} to {new}" if old else "never"
    print(f"Coverage: {ok} of {total} documents in scope scanned ok; {nreg} registrants in scope; "
          f"{last}")
    hits = conn.execute(
        "SELECT reg_number, registrant, doc_type, doc_date, kind, amount, excerpt, url FROM fara_hits "
        "WHERE lower(rep)=lower(?) ORDER BY doc_date DESC, id", (rep,)).fetchall()
    cache = {}

    def info(num):
        if num not in cache:
            pr = conn.execute("SELECT DISTINCT fp_name, country_norm FROM fara_principals WHERE reg_number=?",
                              (num,)).fetchall()
            pr.sort(key=lambda x: (x[1] not in vf, x[0]))
            fl = []
            for _, c in pr:
                if c in vf and c not in fl:
                    fl.append(c)
            cache[num] = (["%s (%s)" % (n, c) for n, c in pr[:3]], fl)
        return cache[num]

    items = [(h, *info(h[0])) for h in hits]
    ordered = [x for x in items if x[2]] + [x for x in items if not x[2]]
    shown = ordered[:limit] if limit else ordered
    flagged = [x for x in shown if x[2]]
    others = [x for x in shown if not x[2]]

    def line(x):
        (_, reg, dtype, ddate, kind, amount, ex, url), prs, fl = x
        what = ("mention found" if kind != "contribution" else "CONTRIBUTION entry reported by the registrant, " +
                (f"${amount:,.2f}" if amount is not None else "amount: see filing"))
        mark = "".join(f"[values flag: principal in {c}] " for c in fl)
        print(f"{mark}{ddate} | {reg} | principals: {'; '.join(prs)} | {dtype} | {what} | {ex} | {url}")

    if not hits:
        print(f"no mention found in the {ok} documents scanned so far (registered agents only)")
    if flagged:
        print("Registrants with a principal in a country on Todd's values-flag list (config/fara_flags.yaml; a list, not a finding):")
        countries = []
        for x in flagged:
            countries += [c for c in x[2] if c not in countries]
        for e in flags["icc"]:
            if e.get("country") in countries:
                print(f"ICC arrest warrant issued for {e.get('person')}"
                      f" ({e.get('warrant_issued', 'date n/a')}; {e.get('source_url', 'icc-cpi.int')}, checked {e.get('checked')})")
        if "Iran" in countries and flags["iran_note"]:
            print(flags["iran_note"])
        for x in flagged:
            line(x)
    if others:
        print("Other registrants:")
        for x in others:
            line(x)
    if len(shown) < len(items):
        print(f"(showing {len(shown)} of {len(items)} mentions; use --limit for more)")
    print(FOOTER)
    return 0




def ranking(conn, top=None):
    ensure_schema(conn)
    vf = set(load_flags()["values_flags"])
    data = conn.execute(
        "SELECT country_norm, COUNT(DISTINCT reg_number), COUNT(DISTINCT reg_number || '|' || fp_name) AS n "
        "FROM fara_principals WHERE country_norm != '' GROUP BY country_norm ORDER BY n DESC, country_norm").fetchall()
    if top:
        data = data[:top]
    tbl = [("rank", "country", "values flag", "registrants", "principals", "spending")]
    for i, (c, nr, npr) in enumerate(data, 1):
        tbl.append((str(i), c, "VALUES FLAG" if c in vf else "", str(nr), str(npr), SPEND_NA))
    w = [max(len(r[j]) for r in tbl) for j in range(6)]
    for r in tbl:
        print(" | ".join(v.ljust(w[j]) for j, v in enumerate(r)).rstrip())
    done = conn.execute("SELECT COUNT(*) FROM fara_registrants WHERE principals_at IS NOT NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM fara_registrants").fetchone()[0]
    print(f"Counts cover principals fetched so far for {done} of {total} registrants (active registrants' current principals; "
          "terminated registrants since the --since year).")
    print("Counts are filed registrations (registered agents only; unregistered influence isn't visible).")
    return 0


# ---------- cli ----------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m src.fara", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="pull FARA data (resumable)")
    r.add_argument("--max-minutes", type=float, default=120)
    r.add_argument("--since", default="2023-01-01")
    r.add_argument("--all-registrants", action="store_true")
    r.add_argument("--retry-errors", action="store_true")
    s = sub.add_parser("show", help="filings that mention a rep")
    s.add_argument("rep", nargs="?")
    s.add_argument("--limit", type=int, default=10)
    k = sub.add_parser("ranking", help="countries ranked by registrations")
    k.add_argument("--top", type=int)
    sub.add_parser("status", help="counts per phase and what remains")
    sub.add_parser("rescan", help="re-scan cached PDF text with the current matcher (no network)")
    args = ap.parse_args(argv)

    conn = get_connection()
    ensure_schema(conn)
    if args.cmd == "refresh":
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print("--since must be YYYY-MM-DD", file=sys.stderr)
            return 2
        return refresh(conn, args.max_minutes, args.since, args.all_registrants, retry_errors=args.retry_errors)
    if args.cmd == "show":
        names = [args.rep] if args.rep else [r_["name"] for r_ in _fed_reps()]
        for n in names:
            show(conn, n, args.limit)
        return 0
    if args.cmd == "ranking":
        return ranking(conn, args.top)
    if args.cmd == "rescan":
        return rescan(conn)
    return print_status(conn)


if __name__ == "__main__":
    sys.exit(main())
