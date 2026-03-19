"""SQLite database setup and access."""

import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import DATA_DIR

DB_PATH = DATA_DIR / "resist.db"


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

        CREATE INDEX IF NOT EXISTS idx_letters_recipient ON letters(recipient);
        CREATE INDEX IF NOT EXISTS idx_letters_topic ON letters(topic);
        CREATE INDEX IF NOT EXISTS idx_voting_representative ON voting_records(representative);
        CREATE INDEX IF NOT EXISTS idx_voting_bill ON voting_records(bill_id);
    """)
    conn.commit()
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


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
