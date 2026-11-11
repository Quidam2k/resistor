"""Import saved official YouTube captions without network or model calls."""
import argparse
import os
from pathlib import Path
import re
import sys

from src.config import load_representatives
from src.db import get_connection
from src.rep_record import (
    ensure_schema, _save_statement, update_progress, iter_reps, normalize,
)


def _surname(name):
    tokens = normalize(name).split()
    while tokens and tokens[-1].rstrip(".,").casefold() in {"jr", "sr", "ii", "iii"}:
        tokens.pop()
    return tokens[-1].rstrip(".,") if tokens else ""


def parse_transcript(path) -> dict | None:
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    meta = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[key.strip()] = value
    private = meta.get("private", "").casefold() == "true"
    filtered = meta.get("filtered", "").casefold() == "true"
    if private or filtered or not meta.get("url") or not meta.get("published"):
        return None
    body = lines[end + 1:]
    while body and not body[0].strip():
        body.pop(0)
    heading = body.pop(0).strip()[2:] if body and body[0].strip().startswith("# ") else ""
    return dict(
        title=normalize(meta.get("title") or heading),
        url=meta["url"], published=meta["published"], source=meta.get("source", ""),
        private=private, filtered=filtered, text="\n".join(body).strip(),
    )


def segments(text, words=150, max_segments=40) -> list[str]:
    if words <= 0:
        raise ValueError("words must be positive")
    tokens = text.split()
    result = []
    for start in range(0, min(len(tokens), words * max_segments), words):
        chunk = tokens[start:start + words]
        if len(chunk) == words or len(chunk) >= 40:
            result.append(" ".join(chunk))
    return result


def collect_youtube(conn, rep, root) -> dict:
    counts = dict(inserted=0, verified=0, videos=0)
    slugs = rep.get("youtube_slugs") or []
    if not slugs:
        return counts
    name = rep["name"]
    surname = _surname(name).casefold()
    for slug in slugs:
        dates_saved = []
        for path in sorted((Path(root) / slug).glob("*.md")):
            if not path.is_file():
                continue
            video = parse_transcript(path)
            if video is None:
                continue
            counts["videos"] += 1
            verified = int(bool(surname) and bool(re.search(
                r"(?<!\w)" + re.escape(surname) + r"(?!\w)", video["title"].casefold(),
            )))
            for quote in segments(video["text"]):
                saved = _save_statement(conn, dict(
                    rep=name, date=video["published"], title=video["title"],
                    venue="YouTube (official channel)", quote=quote, quote_kind="video",
                    url=video["url"], source_kind="video", verified=verified,
                ))
                if saved is not None:
                    counts["inserted"] += 1
                    counts["verified"] += verified
                    dates_saved.append(video["published"])
        update_progress(conn, name, "statements:youtube", dates_saved,
                        note="captions have no timestamps")
        conn.commit()
    return counts


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rep")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else os.environ.get("RESISTOR_YOUTUBE_ROOT")
    if not root:
        parser.error("--root or RESISTOR_YOUTUBE_ROOT is required")
    reps = iter_reps(load_representatives())
    conn = get_connection()
    try:
        ensure_schema(conn)
        for rep in reps:
            if not rep.get("youtube_slugs"):
                continue
            if args.rep is not None and rep["name"].casefold() != args.rep.casefold():
                continue
            result = collect_youtube(conn, rep, root)
            print(f"{rep['name']} youtube: {result['inserted']} new "
                  f"({result['verified']} verified) from {result['videos']} videos")
        conn.commit()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
