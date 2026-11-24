from __future__ import annotations

import json
import re
from typing import Any, Callable

from src import three_opinion as t
from src.config import load_user
from src.interpreter import split_footnotes

_BARE_RE = re.compile(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}/\S*")
_COUNT_KEYS = ("SUPPORTED", "NOT_IN_SOURCES", "CONTRADICTED", "ASK_OR_OPINION")
_FOOTER = (
    "Advisory only. A pass is not proof a letter is safe to send: it checks the text "
    "against the sources listed, not whether the sources are right. Your read and "
    "your Approve click are unchanged."
)


# ---------- Part A: claims table ----------

def normalize_url(url: str) -> str:
    """Strip, add https:// when no scheme, then clean."""
    url = (url or "").strip()
    if not url:
        return ""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    return t._clean_url(url)


def load_claims(path: str) -> list[dict]:
    """Load and validate a claims JSON file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    msg = "claims file must be a JSON list of objects with a claim"
    if not isinstance(data, list):
        raise ValueError(msg)
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(msg)
        c = item.get("claim")
        if not isinstance(c, str) or not c.strip():
            raise ValueError(msg)
    return data


def check_claims(claims: list[dict], fetch: Callable = t.fetch_sources) -> list[dict]:
    """Return copies of claims with status and why added."""
    urls: list[str] = []
    for c in claims:
        u = normalize_url(str(c.get("url") or ""))
        if u and u not in urls:
            urls.append(u)
    by_url: dict[str, dict] = {}
    if urls:
        results = list(fetch(urls))
        by_url = {r.get("url"): r for r in results}  # match by url only, never by position
    out: list[dict] = []
    for c in claims:
        item = dict(c)
        u = normalize_url(str(c.get("url") or ""))
        quote = t._norm(str(c.get("quote") or ""))
        if not u:
            item["status"], item["why"] = "UNSOURCED", "no source given"
        else:
            rec = by_url.get(u)
            if not rec or not rec.get("ok"):
                why = (rec or {}).get("error") or "no result"
                item["status"], item["why"] = "SOURCE_UNREACHABLE", str(why)
            elif len(quote) < t.MIN_QUOTE_CHARS or quote not in t._norm(rec.get("text") or ""):
                item["status"] = "QUOTE_NOT_FOUND"
                item["why"] = "quote not found in the source"
            else:
                item["status"], item["why"] = "CONFIRMED", "quote found in source"
        out.append(item)
    return out


def unconfirmed(checked: list[dict]) -> list[dict]:
    """Items whose status is not CONFIRMED."""
    return [c for c in checked if c.get("status") != "CONFIRMED"]


def _cell(text: Any) -> str:
    return re.sub(r"[|\r\n]", " ", str(text))


def render_claims_section(checked: list[dict]) -> str:
    """Markdown section for the claims check."""
    if not checked:
        return ""
    lines = ["## Claims and sources (checked before drafting)", ""]
    ok = [c for c in checked if c.get("status") == "CONFIRMED"]
    if ok:
        lines += ["| Claim | Kind | Source |", "| --- | --- | --- |"]
        for c in ok:
            lines.append(
                f"| {_cell(c.get('claim'))} | {_cell(c.get('kind', ''))} | "
                f"{_cell(normalize_url(str(c.get('url') or '')))} |"
            )
    else:
        lines.append("(none confirmed)")
    bad = unconfirmed(checked)
    if bad:
        lines += [
            "",
            "## Couldn't confirm, where did you hear this?",
            "",
            "These stay out of the letters unless you tell me where you heard them. "
            "If you remember the source, say so; if not, they remain out.",
            "",
        ]
        for c in bad:
            lines.append(f"- {_san(c.get('claim'))} ({c.get('status')}: {_san(c.get('why'))})")
    return "\n".join(lines)


# ---------- Part B: fact check of letters ----------

def _find_url(text: str) -> str:
    m = t.URL_RE.search(text)
    if m:
        return m.group(0)
    m = _BARE_RE.search(text)
    return m.group(0) if m else ""


def footnote_urls(body: str) -> list[str]:
    """URLs found in the footnotes of a letter body, de-duplicated in order."""
    out: list[str] = []
    for _n, note in split_footnotes(body)[1]:
        found = ""
        if ": " in note:
            found = _find_url(note.split(": ", 1)[1])
        if not found:
            found = _find_url(note)
        if found:
            u = normalize_url(found)
            if u and u not in out:
                out.append(u)
    return out


def _paragraphs(body: str, user: Any) -> list[str]:
    prose = split_footnotes(body)[0]
    prose = re.sub(r"\s*\[\d{1,2}\]", "", prose)
    prose = t.strip_constituent(prose, user)
    paras = [p.strip() for p in re.split(r"\n\s*\n", prose)]
    return [p for p in paras if p and not p.startswith(("Dear ", "Sincerely"))]


def review_text(letters: list[dict], user: Any) -> str:
    """One text for the batch: shared paragraphs once, then each letter's own."""
    per = [_paragraphs(str(L.get("body", "")), user) for L in letters]
    if not per:
        return ""
    shared = [p for p in dict.fromkeys(per[0]) if all(p in other for other in per)]
    out = list(shared)
    seen = set(out)
    for paras in per:
        for p in paras:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return "\n\n".join(out)


def _sev(v: str) -> int:
    return t.SEVERITY.get(v, -1)


def _verdicts(row: dict) -> list[str]:
    return [str(v) for v in (row.get("verdicts") or {}).values() if v and v != t.NO_CLAIM]


def _counts(rows: list[dict]) -> dict:
    counts = {k: 0 for k in _COUNT_KEYS}
    for row in rows:
        vs = _verdicts(row)
        if not vs:
            continue
        worst = max(vs, key=_sev)
        if worst in counts:
            counts[worst] += 1
    return counts


def check_letters(letters: list[dict], legs: tuple = ("sonnet", "opus", "codex"),
                  runner: Callable = t.run_leg, fetch: Callable = t.fetch_sources,
                  user: Any = None) -> dict:
    """Run the three-opinion check over a batch. Never raises."""
    try:
        if user is None:
            user = load_user()
        urls: list[str] = []
        for L in letters:
            for u in footnote_urls(str(L.get("body", ""))):
                if u not in urls:
                    urls.append(u)
        if not urls:
            return {"status": "NO_SOURCES"}
        text = review_text(letters, user)
        sources = fetch(urls)
        legs_out = t.run_review(text, sources, legs=legs, runner=runner)
        cons = t.consensus(legs_out, t.split_sentences(text))
        rows = cons.get("rows") or []
        return {
            "status": cons["status"],
            "ran": cons.get("ran"),
            "not_ran": cons.get("not_ran"),
            "sources": [{"url": s.get("url"), "ok": bool(s.get("ok")), "error": s.get("error")} for s in sources],
            "counts": _counts(rows),
            "flags": cons.get("flags") or [],
            "contradicted": any("CONTRADICTED" in _verdicts(r) for r in rows),
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


def title_prefix(result: dict) -> str:
    """Title prefix when any claim is contradicted."""
    return "CHECK: " if result.get("contradicted") else ""


def _san(text: Any) -> str:
    s = re.sub(r"\s+", " ", str(text)).strip()
    s = s.replace("|", "/").replace("\u2014", "-").replace("\u2013", "-")
    return re.sub(r"safe to send", "[phrase removed]", s, flags=re.IGNORECASE)


def render_fact_check_section(result: dict) -> str:
    """Markdown fact check section, advisory only."""
    lines = ["## Fact check (advisory)", ""]
    status = result.get("status")
    if status == "ERROR":
        lines.append(f"The automatic check did not run ({_san(result.get('error', ''))}). "
                     "Check the facts by hand.")
    elif status == "NO_SOURCES":
        lines.append("No source links in the letters, so nothing could be checked.")
    else:
        ran = [_san(x) for x in (result.get("ran") or [])]
        lines.append("Legs that ran: " + (", ".join(ran) if ran else "none"))
        for item in result.get("not_ran") or []:
            label, reason = (list(item) + ["unknown"])[:2] if isinstance(item, (list, tuple)) else (item, "unknown")
            lines.append(f"Did not run: {_san(label)} ({_san(reason)})")
        srcs = result.get("sources") or []
        opened = sum(1 for s in srcs if s.get("ok"))
        lines.append(f"Sources opened: {opened} of {len(srcs)}")
        for s_ in (s_ for s_ in srcs if not s_.get("ok")):
            lines.append(f"Could not open: {_san(s_.get('url'))} ({_san(s_.get('error') or 'unknown')[:60]})")
        if opened < len(srcs):
            lines.append("A claim whose source could not be opened shows as not in sources; that is "
                         "the checker's limit, not a finding.")
        c = result.get("counts") or {}
        lines.append(
            "Claims by sentence: "
            f"{c.get('SUPPORTED', 0)} supported, {c.get('NOT_IN_SOURCES', 0)} not in sources, "
            f"{c.get('CONTRADICTED', 0)} contradicted, {c.get('ASK_OR_OPINION', 0)} asks/opinions"
        )
        if status == "INCOMPLETE":
            lines.append("Fewer than 3 legs ran, so treat this as weaker than usual.")
        flags = result.get("flags") or []
        if flags:
            lines += ["", "Flagged:"]
            for f in (f for f in flags if isinstance(f, dict)):
                sentence = _san(f.get("claim") or "")
                vs = f.get("verdicts") or {}
                detail = ", ".join(f"{_san(k)}={_san(v)}" for k, v in vs.items())
                prefix = "CONTRADICTED: " if "CONTRADICTED" in [str(v) for v in vs.values()] else ""
                lines.append(f"- {prefix}{sentence} ({detail})")
    lines += ["", _FOOTER]
    return "\n".join(lines)
