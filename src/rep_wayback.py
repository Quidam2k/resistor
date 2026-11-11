"""Collect public campaign statements from politely paced Wayback snapshots."""
import posixpath
import time
from urllib.parse import unquote, urlsplit

import requests

from src.rep_crec import resume_cursor
from src.rep_record import (
    _save_statement, _title, normalize, page_text, update_progress,
)

_CDX = "https://web.archive.org/cdx/search/cdx"
_WEB = "https://web.archive.org/web/"
_AGENT = "resistor-civic-research/1.0 (+public civic project)"
_WANTED = (
    "issue", "priorit", "about", "record", "plan", "position", "values",
    "vision", "news", "press", "statement", "accomplish", "agenda", "on-the",
)
_BLOCKED = (
    "wp-content", "wp-json", "donate", "contribute", "volunteer", "login",
    "cart", "shop", "privacy", "tag/", "category/", "feed",
)


def polite_get(url, params=None, *, get=requests.get, sleep=time.sleep,
               pace=5.0, retries=4) -> requests.Response:
    """Try at most retries requests, pausing even after the final refusal."""
    for attempt in range(retries):
        sleep(pace)
        response = None
        try:
            response = get(url, params=params, headers={"User-Agent": _AGENT}, timeout=60)
        except requests.ConnectionError:
            pass
        else:
            if response.status_code not in (429, 503):
                response.raise_for_status()
                return response
        if attempt == retries - 1:
            break
        delay = 60.0 * 2 ** attempt
        if response is not None:
            try:
                delay = max(delay, float(response.headers.get("Retry-After", "")))
            except (TypeError, ValueError):
                pass
        sleep(delay)
    raise RuntimeError("wayback rate limited")


def wanted_path(original_url) -> bool:
    try:
        parsed = urlsplit(original_url)
        path = unquote(parsed.path).lower()
    except ValueError:
        return False
    if "?" in original_url.split("#", 1)[0] or any(s in path for s in _BLOCKED):
        return False
    extension = posixpath.splitext(path.rstrip("/"))[1]
    if extension not in ("", ".html", ".htm", ".php", ".asp", ".aspx"):
        return False
    return path in ("", "/") or any(s in path for s in _WANTED)


def snapshots(domain, year, *, get=polite_get) -> list[tuple[str, str]]:
    params = [
        ("url", f"{domain.rstrip('/')}/*"), ("output", "json"),
        ("fl", "timestamp,original"), ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"), ("collapse", "urlkey"),
        ("from", str(year)), ("to", str(year)),
    ]
    response = get(_CDX, params=params)
    if not response.text.strip():
        return []
    return [(stamp, url) for stamp, url in response.json()[1:] if wanted_path(url)][:40]


def collect_wayback(conn, rep, years, *, get=polite_get, max_chars=5000, oldest=None) -> dict:
    result = {"inserted": 0, "errors": [], "years_done": []}
    domains = rep.get("campaign_domains") or []
    if not domains:
        result["note"] = "no campaign_domains configured"
        return result
    years = sorted(set(years), reverse=True)
    for year in years:
        dates = []
        try:
            for domain in domains:
                for stamp, original in snapshots(domain, year, get=get):
                    try:
                        raw = get(f"{_WEB}{stamp}id_/{original}").text
                    except requests.HTTPError:
                        continue  # a snapshot that 404s or errors is skipped, not fatal
                    text = normalize(page_text(raw))
                    if len(text.split()) < 80:
                        continue
                    quote = text[:max_chars]
                    if conn.execute(
                        "SELECT 1 FROM rep_statements "
                        "WHERE rep=? AND source_kind='campaign' AND quote=? LIMIT 1",
                        (rep["name"], quote),
                    ).fetchone():
                        continue
                    date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
                    saved = _save_statement(conn, dict(
                        rep=rep["name"], date=date,
                        title=_title(raw) or urlsplit(original).path,
                        venue=f"campaign site {domain} (archived)", quote=quote,
                        quote_kind="campaign", url=f"{_WEB}{stamp}/{original}",
                        source_kind="campaign", verified=1,
                    ))
                    if saved:
                        result["inserted"] += 1
                        dates.append(date)
        except RuntimeError as exc:
            result["errors"].append(str(exc))
            update_progress(conn, rep["name"], "statements:wayback", dates,
                            note="rate limited, resume later")
            conn.commit()
            return result
        update_progress(conn, rep["name"], "statements:wayback", dates,
                        cursor=resume_cursor(conn, rep["name"], "statements:wayback", year),
                        exhausted=int(year == (oldest or years[-1])) or None, note="")
        conn.commit()
        result["years_done"].append(year)
    return result
