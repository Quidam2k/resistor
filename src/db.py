"""SQLite database setup and access."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import DATA_DIR

DB_PATH = DATA_DIR / "resist.db"

# Letter status lifecycle:
#   draft -> pending_approval -> approved -> sent | failed
#                             -> rejected
# (legacy rows may also be 'superseded')


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS letters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,          -- rep name
            topic TEXT NOT NULL,
            body TEXT NOT NULL,               -- full letter text
            channel TEXT NOT NULL,            -- email, fax, web_form, mail
            status TEXT NOT NULL DEFAULT 'draft',  -- draft, approved, sent, failed
            sent_at TEXT,
            session_date TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            topics TEXT,                      -- JSON list of topics discussed
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            letter_id INTEGER REFERENCES letters(id),
            representative TEXT NOT NULL,
            received_date TEXT NOT NULL,
            body TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS asks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            letter_id INTEGER REFERENCES letters(id),
            rep TEXT NOT NULL,
            congress INTEGER NOT NULL,
            bill_type TEXT NOT NULL,          -- s, hr, sjres, hjres, ...
            bill_number INTEGER NOT NULL,
            ask_summary TEXT NOT NULL,
            asked_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',   -- open, landed, dropped
            landed_date TEXT,
            last_checked TEXT,
            last_status_note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS voting_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            representative TEXT NOT NULL,
            vote_date TEXT NOT NULL,
            bill_id TEXT,                     -- e.g. "S.1234" or "H.R.5678"
            bill_title TEXT,
            vote TEXT NOT NULL,               -- Yea, Nay, Not Voting, Present
            chamber TEXT,                     -- Senate, House, OR_Senate, OR_House
            session TEXT,                     -- e.g. "119th Congress"
            source_url TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_asks_status ON asks(status);
        CREATE INDEX IF NOT EXISTS idx_asks_letter ON asks(letter_id);
        CREATE INDEX IF NOT EXISTS idx_letters_recipient ON letters(recipient);
        CREATE INDEX IF NOT EXISTS idx_letters_topic ON letters(topic);
        CREATE INDEX IF NOT EXISTS idx_voting_representative ON voting_records(representative);
        CREATE INDEX IF NOT EXISTS idx_voting_bill ON voting_records(bill_id);
    """)
    conn.commit()
    _migrate(conn)
    conn.close()


# Columns added after the original schema; run_migrations is idempotent.
_MIGRATION_COLUMNS = {
    "approved_at": "TEXT",
    "rejected_at": "TEXT",
    "rejection_note": "TEXT",
    "delivery_result_json": "TEXT",
    "lob_thumbnail_url": "TEXT",
    "estimated_cost_cents": "INTEGER",
    "md_path": "TEXT",
}


def _migrate(conn: sqlite3.Connection):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(letters)")}
    for column, col_type in _MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE letters ADD COLUMN {column} {col_type}")
    conn.commit()


def run_migrations():
    conn = get_connection()
    _migrate(conn)
    conn.close()


def save_letter(recipient: str, topic: str, body: str, channel: str,
                session_date: str = None, status: str = "draft") -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO letters (recipient, topic, body, channel, session_date, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recipient, topic, body, channel, session_date, status)
    )
    letter_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return letter_id


def mark_letter_sent(letter_id: int):
    conn = get_connection()
    conn.execute(
        "UPDATE letters SET status='sent', sent_at=? WHERE id=?",
        (datetime.now().isoformat(), letter_id)
    )
    conn.commit()
    conn.close()


def mark_letter_failed(letter_id: int, notes: str = None):
    conn = get_connection()
    conn.execute(
        "UPDATE letters SET status='failed', notes=? WHERE id=?",
        (notes, letter_id)
    )
    conn.commit()
    conn.close()


def get_letters_to(recipient: str) -> list[dict]:
    """Get all letters sent to a specific representative, newest first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM letters WHERE recipient=? ORDER BY created_at DESC",
        (recipient,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_prior_correspondence(recipient: str, topic: str = None) -> list[dict]:
    """Get correspondence history for composing follow-up letters."""
    conn = get_connection()
    if topic:
        rows = conn.execute(
            "SELECT l.*, r.body as response_body, r.received_date as response_date "
            "FROM letters l LEFT JOIN responses r ON r.letter_id = l.id "
            "WHERE l.recipient=? AND l.topic=? AND l.status='sent' "
            "ORDER BY l.sent_at DESC",
            (recipient, topic)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT l.*, r.body as response_body, r.received_date as response_date "
            "FROM letters l LEFT JOIN responses r ON r.letter_id = l.id "
            "WHERE l.recipient=? AND l.status='sent' "
            "ORDER BY l.sent_at DESC",
            (recipient,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_voting_record(representative: str, bill_id: str = None) -> list[dict]:
    """Get voting record for a representative, optionally filtered by bill."""
    conn = get_connection()
    if bill_id:
        rows = conn.execute(
            "SELECT * FROM voting_records WHERE representative=? AND bill_id=? "
            "ORDER BY vote_date DESC",
            (representative, bill_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM voting_records WHERE representative=? "
            "ORDER BY vote_date DESC",
            (representative,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_letter_by_id(letter_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM letters WHERE id=?", (letter_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_pending_approval_letters() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM letters WHERE status='pending_approval' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_approval_history(limit: int = 50) -> list[dict]:
    """Letters that have passed through the approval gate, newest first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM letters WHERE status IN ('approved','sent','failed','rejected') "
        "ORDER BY COALESCE(approved_at, rejected_at, created_at) DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_letter_status(letter_id: int, status: str, extra: dict = None):
    """Set a letter's status plus any extra columns, and sync the md file header.

    extra may contain any of the letters table columns (e.g. sent_at,
    rejection_note, delivery_result_json). delivery_result_json may be
    passed as a dict and will be serialized.
    """
    extra = dict(extra or {})
    if isinstance(extra.get("delivery_result_json"), (dict, list)):
        extra["delivery_result_json"] = json.dumps(extra["delivery_result_json"])

    allowed = {"sent_at", "approved_at", "rejected_at", "rejection_note",
               "delivery_result_json", "lob_thumbnail_url",
               "estimated_cost_cents", "md_path", "notes", "channel"}
    bad = set(extra) - allowed
    if bad:
        raise ValueError(f"update_letter_status: unknown columns {bad}")

    sets = ["status=?"] + [f"{k}=?" for k in extra]
    values = [status] + list(extra.values()) + [letter_id]

    conn = get_connection()
    conn.execute(f"UPDATE letters SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    md_path = conn.execute(
        "SELECT md_path FROM letters WHERE id=?", (letter_id,)
    ).fetchone()
    conn.close()

    if md_path and md_path["md_path"]:
        from src.letter import update_markdown_status
        update_markdown_status(md_path["md_path"], status)


def claim_letter_for_approval(letter_id: int) -> bool:
    """Atomically move pending_approval -> approved. Returns False if the
    letter was not pending (already approved/sent/rejected) — the
    double-click guard."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE letters SET status='approved', approved_at=? "
        "WHERE id=? AND status='pending_approval'",
        (datetime.now().isoformat(), letter_id)
    )
    claimed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    if claimed:
        row = get_letter_by_id(letter_id)
        if row and row.get("md_path"):
            from src.letter import update_markdown_status
            update_markdown_status(row["md_path"], "approved")
    return claimed


def record_response(letter_id: int | None, representative: str,
                    received_date: str, body: str, notes: str = None) -> int:
    """Log a reply received from a representative.

    Inserts a row into `responses` (which get_prior_correspondence LEFT JOINs
    into letter history) and writes a markdown copy to data/responses/ for
    readability, mirroring the letters/ convention.
    """
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO responses (letter_id, representative, received_date, body, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (letter_id, representative, received_date, body, notes)
    )
    response_id = cursor.lastrowid
    conn.commit()
    conn.close()

    responses_dir = DATA_DIR / "responses"
    responses_dir.mkdir(exist_ok=True)
    slug = representative.lower().replace(" ", "-").replace(".", "")
    md_path = responses_dir / f"{received_date}-{slug}.md"
    # Avoid clobbering a same-day, same-rep reply already on disk.
    if md_path.exists():
        md_path = responses_dir / f"{received_date}-{slug}-{response_id}.md"
    header = [
        f"# Response from {representative}",
        f"Received: {received_date}",
        f"Letter ID: {letter_id}" if letter_id else "Letter ID: (unmatched)",
    ]
    if notes:
        header.append(f"Notes: {notes}")
    md_path.write_text("\n".join(header) + "\n\n" + body + "\n", encoding="utf-8")

    return response_id


def add_ask(letter_id: int | None, rep: str, congress: int, bill_type: str,
            bill_number: int, ask_summary: str, asked_date: str = None) -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO asks (letter_id, rep, congress, bill_type, bill_number, "
        "ask_summary, asked_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (letter_id, rep, congress, bill_type, bill_number, ask_summary, asked_date)
    )
    ask_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ask_id


def get_open_asks() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM asks WHERE status='open' ORDER BY asked_date DESC, id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_asks() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM asks ORDER BY asked_date DESC, id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ask_exists_for_letter(letter_id: int) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM asks WHERE letter_id=? LIMIT 1", (letter_id,)
    ).fetchone()
    conn.close()
    return row is not None


def update_ask_status(ask_id: int, status: str = None, landed_date: str = None,
                      last_checked: str = None, last_status_note: str = None):
    """Update an ask's tracking columns; only non-None values are written."""
    fields = {
        "status": status,
        "landed_date": landed_date,
        "last_checked": last_checked,
        "last_status_note": last_status_note,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [ask_id]
    conn = get_connection()
    conn.execute(f"UPDATE asks SET {sets} WHERE id=?", values)
    conn.commit()
    conn.close()


def get_sent_letters_without_response() -> list[dict]:
    """Sent letters that have no logged response, for follow-up flagging."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT l.* FROM letters l "
        "LEFT JOIN responses r ON r.letter_id = l.id "
        "WHERE l.status='sent' AND r.id IS NULL "
        "ORDER BY l.sent_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_responses() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM responses ORDER BY received_date DESC, id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_responses(representative: str = None, keyword: str = None) -> list[dict]:
    """Find logged replies by rep and/or topic keyword, newest first.

    Use this at draft time to pull a rep's prior stated position on a topic --
    including back-imported replies that aren't linked to a letter row (so
    get_prior_correspondence, which joins on letter_id, won't surface them).
    keyword matches against both the reply body and the notes (which carry the
    imported topic), case-insensitively.
    """
    clauses, params = [], []
    if representative:
        clauses.append("representative = ?")
        params.append(representative)
    if keyword:
        clauses.append("(body LIKE ? OR notes LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM responses {where} ORDER BY received_date DESC, id DESC",
        params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
