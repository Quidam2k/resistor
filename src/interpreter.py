"""Voice-interpreter loop: the steps Jarvis drives during Todd's Resistor block.

    python -m src.interpreter actions [--refresh]   numbered menu of things worth a letter
    python -m src.interpreter pick N                drafting brief for menu item N
    python -m src.interpreter draft N --body-file F write + lint the letter markdown
    python -m src.interpreter queue MD              queue for approval (pending_approval)
    python -m src.interpreter status ID             where a queued letter stands
    python -m src.interpreter log                   record the block, print the habit goal id

There is deliberately NO approve or send step here. A queued letter goes out
only when Todd clicks Approve in the web UI (src/webui.py). This module must
never import deliver_letter, the job runner, or any sender; the only thing it
takes from src.delivery is the read-only get_delivery_info lookup.
"""

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from src import db
from src.config import DATA_DIR, load_user
from src.congress_api import MEMBER_IDS
from src.delivery.router import get_delivery_info
from src.letter import (get_context_for_letter, parse_letter_markdown,
                        write_letter_markdown)
from src.outbox import estimate_cost_cents, queue_letter_from_markdown

MENU_PATH = DATA_DIR / "runtime" / "interpreter_menu.json"
SESSIONS_DIR = DATA_DIR / "sessions"
PANTHEON_DB = Path(os.environ.get("PANTHEON_DB", "Q:/Pantheon/data/pantheon.db"))

PER_GROUP = 3            # keep the spoken menu short
REDRAFT_WINDOW_DAYS = 60
BILL_RE = re.compile(r"\b(S|H\.?\s?R|S\.?\s?J\.?\s?Res|H\.?\s?J\.?\s?Res)\.?\s?(\d+)\b", re.I)


def _query(sql: str, params=()) -> list[dict]:
    conn = db.get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _bill_key(text: str) -> str:
    """'H.R. 6589', 'HR.6589', 'hr 6589' -> 'HR6589'."""
    return re.sub(r"[.\s]", "", text).upper()


def _approval_url(letter_id: int) -> str:
    port = os.environ.get("WEBUI_PORT", "5317")
    return f"http://127.0.0.1:{port}/letter/{letter_id}"


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def build_actions() -> list[dict]:
    """Collect candidate actions from existing data. Read-only."""
    items = []

    for ask in db.get_open_asks()[:PER_GROUP]:
        bill = f"{ask['bill_type'].upper()}.{ask['bill_number']}"
        items.append({
            "kind": "open_ask", "recipient": ask["rep"],
            "topic": ask["ask_summary"],
            "say": f"Push {ask['rep']} again on {bill}: {ask['ask_summary']} "
                   f"({ask.get('last_status_note') or 'not yet checked'})",
            "sources": [f"asks#{ask['id']}"], "bills": [bill],
        })

    for letter in db.get_sent_letters_without_response()[:PER_GROUP]:
        sent = (letter.get("sent_at") or "")[:10]
        items.append({
            "kind": "follow_up", "recipient": letter["recipient"],
            "topic": letter["topic"],
            "say": f"Follow up with {letter['recipient']} on '{letter['topic']}' "
                   f"(sent {sent}, no reply logged)",
            "sources": [f"letters#{letter['id']}"], "bills": [],
        })

    # Rejected letters with no later (or already-sent) letter to the same rep
    # on the same topic.
    rejected = _query(
        "SELECT l.* FROM letters l WHERE l.status='rejected' "
        "AND l.rejected_at >= date('now', ?) "
        "AND NOT EXISTS (SELECT 1 FROM letters n WHERE n.recipient=l.recipient "
        "AND n.topic=l.topic AND n.id != l.id AND (n.id > l.id OR n.status='sent')) "
        "ORDER BY l.rejected_at DESC LIMIT ?",
        (f"-{REDRAFT_WINDOW_DAYS} days", PER_GROUP))
    for letter in rejected:
        note = (letter.get("rejection_note") or "no note").strip()
        items.append({
            "kind": "redraft", "recipient": letter["recipient"],
            "topic": letter["topic"],
            "say": f"Redraft to {letter['recipient']} on '{letter['topic']}'; "
                   f"you rejected it: {note}",
            "sources": [f"letters#{letter['id']}"], "bills": [],
        })

    # Most recent distinct vote questions, with each rep's vote. Only rows
    # whose chamber matches the member's (the GovTrack import once filed
    # Senate votes under Hoyle, a House member).
    member_ok = " OR ".join(
        "(representative=? AND lower(chamber)=?)" for _ in MEMBER_IDS)
    member_params = [v for name, info in MEMBER_IDS.items()
                     for v in (name, info["chamber"])]
    questions = _query(
        f"SELECT bill_title, MAX(vote_date) AS vote_date FROM voting_records "
        f"WHERE {member_ok} GROUP BY bill_title ORDER BY vote_date DESC LIMIT ?",
        (*member_params, PER_GROUP))
    for q in questions:
        votes = _query(
            f"SELECT id, representative, vote, source_url FROM voting_records "
            f"WHERE bill_title=? AND vote_date=? AND ({member_ok})",
            (q["bill_title"], q["vote_date"], *member_params))
        how = ", ".join(f"{v['representative']} {v['vote']}" for v in votes)
        bills = [m.group(0) for m in BILL_RE.finditer(q["bill_title"] or "")]
        items.append({
            "kind": "vote", "recipient": None, "topic": q["bill_title"],
            "say": f"Vote {q['vote_date'][:10]}: {q['bill_title']} ({how})",
            "sources": [f"voting_records#{v['id']}" for v in votes],
            "source_urls": sorted({v["source_url"] for v in votes if v["source_url"]}),
            "bills": bills,
        })

    for letter in db.get_pending_approval_letters():
        items.append({
            "kind": "awaiting_click", "recipient": letter["recipient"],
            "topic": letter["topic"], "letter_id": letter["id"],
            "say": f"Already queued: #{letter['id']} to {letter['recipient']} "
                   f"on '{letter['topic']}', waiting for your click",
            "sources": [f"letters#{letter['id']}"], "bills": [],
        })
    return items


def cmd_actions(args) -> list[dict]:
    if args.refresh:
        from src.tracker import check_open_asks
        for row in check_open_asks(update=True):
            print(f"  refreshed: {row['note']}")
    items = build_actions()
    MENU_PATH.parent.mkdir(parents=True, exist_ok=True)
    MENU_PATH.write_text(json.dumps(
        {"built_at": datetime.now().isoformat(), "items": items}, indent=2),
        encoding="utf-8")
    newest_vote = _query("SELECT MAX(vote_date) AS d FROM voting_records")[0]["d"]
    print(f"RESISTOR ACTIONS ({len(items)})  votes data through {(newest_vote or 'n/a')[:10]}")
    for n, item in enumerate(items, 1):
        print(f"  {n}. [{item['kind']}] {item['say']}")
    return items


def _menu_item(n: int) -> dict:
    if not MENU_PATH.exists():
        raise SystemExit("No menu yet. Run: python -m src.interpreter actions")
    items = json.loads(MENU_PATH.read_text(encoding="utf-8"))["items"]
    if not 1 <= n <= len(items):
        raise SystemExit(f"Pick 1..{len(items)}")
    return items[n - 1]


# ---------------------------------------------------------------------------
# pick
# ---------------------------------------------------------------------------

def cmd_pick(args):
    item = _menu_item(args.n)
    recipient = args.recipient or item["recipient"]
    print(f"PICKED #{args.n} [{item['kind']}] {item['topic']}")
    print(f"Sources (cite these, not memory): {', '.join(item['sources'])}")
    for url in item.get("source_urls", []):
        print(f"  {url}")
    if item["kind"] == "awaiting_click":
        print(f"Already queued. Todd approves or rejects at {_approval_url(item['letter_id'])}")
        return
    if not recipient:
        print("Needs a recipient: re-run with --recipient \"Ron Wyden\" (or Merkley / Hoyle).")
        return
    if item["kind"] == "redraft":
        row = db.get_letter_by_id(int(item["sources"][0].split("#")[1]))
        print(f"Rejection note: {row.get('rejection_note') or '(none)'}")
        print(f"Rejected text (for reference, never resurrect the row):\n{row['body']}\n")
    info = get_delivery_info(recipient)
    channel = info.get("channel", "unknown")
    print(f"To: {recipient} via {channel}; est. cost "
          f"${estimate_cost_cents(channel, 'x' * 2500) / 100:.2f}")
    if channel != "mail" and info.get("mail_address"):
        print("  (Lob mail also possible: draft with --channel mail)")
    print(get_context_for_letter(recipient, None))
    print("\nRules: one topic; no em dashes; fact-check against the web before "
          "queueing; invite a reply with their reasoning; cite sources above for "
          "any vote or bill claim.")


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------

def lint_letter(body: str, item: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block the draft."""
    errors, warnings = [], []
    if "\u2014" in body or "\u2013" in body:
        errors.append("contains an em/en dash (letters must not read as AI-written)")
    user = load_user()
    if user["name"] not in body:
        errors.append(f"constituent name '{user['name']}' missing")
    if str(user["zip"]) not in body:
        errors.append("constituent address/zip missing")
    known = {_bill_key(b) for b in item.get("bills", [])}
    for m in BILL_RE.finditer(body):
        if _bill_key(m.group(0)) not in known:
            warnings.append(f"{m.group(0)} is not in this action's sources; "
                            "confirm it against a web source before queueing")
    return errors, warnings


def cmd_draft(args) -> Path | None:
    item = _menu_item(args.n)
    recipient = args.recipient or item["recipient"]
    if not recipient:
        raise SystemExit("Needs --recipient for this action")
    body = Path(args.body_file).read_text(encoding="utf-8").strip()
    topic = args.topic or item["topic"]
    errors, warnings = lint_letter(body, item)
    for w in warnings:
        print(f"  warn: {w}")
    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
        print("Draft NOT written.")
        return None
    channel = args.channel or get_delivery_info(recipient).get("channel", "unknown")
    path = write_letter_markdown(recipient, topic, body, channel, status="draft")
    text = path.read_text(encoding="utf-8").replace(
        "**Status:** draft\n",
        f"**Status:** draft\n**Sources:** {', '.join(item['sources'])}\n", 1)
    path.write_text(text, encoding="utf-8")
    print(f"DRAFT written: {path}")
    print(f"Read it to Todd. When he's happy: python -m src.interpreter queue \"{path}\"")
    return path


# ---------------------------------------------------------------------------
# queue / status
# ---------------------------------------------------------------------------

def cmd_queue(args) -> int:
    path = Path(args.md)
    first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    recipient = first.removeprefix("# Letter to ").strip()
    body = parse_letter_markdown(path)["body"]
    errors, _ = lint_letter(body, {})
    if errors:
        raise SystemExit(f"Refusing to queue: {'; '.join(errors)}")
    letter_id = queue_letter_from_markdown(str(path), recipient)
    letter = db.get_letter_by_id(letter_id)
    cost = letter.get("estimated_cost_cents") or 0
    print(f"QUEUED #{letter_id} to {recipient} via {letter['channel']} "
          f"(est. ${cost / 100:.2f}); status pending_approval")
    if letter.get("lob_thumbnail_url"):
        print("  Lob TEST render attached (free, never mailed); Todd sees the printed page in the UI")
    print(f"  Todd approves here, by click only: {_approval_url(letter_id)}")
    print("  (UI not running? python -m src.webui in the background)")
    return letter_id


def cmd_status(args):
    letter = db.get_letter_by_id(args.id)
    if not letter:
        raise SystemExit(f"No letter #{args.id}")
    print(f"#{letter['id']} to {letter['recipient']}: {letter['status']}")
    if letter.get("rejection_note"):
        print(f"  rejection note: {letter['rejection_note']}")
    if letter.get("delivery_result_json"):
        result = json.loads(letter["delivery_result_json"])
        print(f"  delivery: {result.get('status')} "
              f"{'(mode ' + result['mode'] + ')' if result.get('mode') else ''}".rstrip())


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

def habit_goal_id(date: str) -> int | None:
    """Today's Resistor habit row in Pantheon, read-only. Jarvis ticks it via
    mcp__pantheon__complete_goal; this module never writes Pantheon."""
    if not PANTHEON_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{PANTHEON_DB.as_posix()}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT id FROM goals WHERE date=? AND lower(content) LIKE '%resist%' "
            "ORDER BY id DESC LIMIT 1", (date,)).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def cmd_log(args):
    today = datetime.now().strftime("%Y-%m-%d")
    letters = _query("SELECT id, recipient, topic, status FROM letters "
                     "WHERE session_date=? ORDER BY id", (today,))
    topics = sorted({l["topic"] for l in letters})
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO sessions (date, topics, notes) VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET topics=excluded.topics, notes=excluded.notes",
        (today, json.dumps(topics), args.notes or "voice block"))
    conn.commit()
    conn.close()

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    md = SESSIONS_DIR / f"{today}-voice-block.md"
    lines = [f"# Resistor voice block {today}", "", args.notes or "", "",
             "## Letters"]
    lines += [f"- #{l['id']} {l['recipient']}: {l['topic']} ({l['status']})"
              for l in letters] or ["- none"]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"LOGGED session {today}: {len(letters)} letter(s); notes at {md}")

    goal = habit_goal_id(today)
    if goal:
        print(f"HABIT: Jarvis, tick goal {goal} with mcp__pantheon__complete_goal "
              "(this tool never ticks it).")
    else:
        print(f"HABIT: no Resistor goal row for {today} in Pantheon; "
              "Jarvis, check today's journal goals.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.interpreter")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("actions"); p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("pick"); p.add_argument("n", type=int); p.add_argument("--recipient")
    p = sub.add_parser("draft"); p.add_argument("n", type=int)
    p.add_argument("--body-file", required=True); p.add_argument("--recipient")
    p.add_argument("--topic")
    p.add_argument("--channel", choices=["fax", "email", "mail", "web_form"],
                   help="override the rep's default channel (e.g. Hoyle by mail)")
    p = sub.add_parser("queue"); p.add_argument("md")
    p = sub.add_parser("status"); p.add_argument("id", type=int)
    p = sub.add_parser("log"); p.add_argument("--notes")
    args = parser.parse_args(argv)
    db.init_db()
    return {"actions": cmd_actions, "pick": cmd_pick, "draft": cmd_draft,
            "queue": cmd_queue, "status": cmd_status, "log": cmd_log}[args.cmd](args)


if __name__ == "__main__":
    main()
