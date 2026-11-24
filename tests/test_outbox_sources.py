"""queue_letter refuses a letter with no Sources block (#6825, Todd: no letter without sources)."""

import pytest

from src import db, outbox
from src.config import load_user


def _body(extra: str = "") -> str:
    u = load_user()
    head = "\n".join(str(u[k]) for k in ("name", "address") if u.get(k))
    return (f"{head}\n{u['city']}, {u['state']} {u['zip']}\n"
            f"{u.get('phone', '')}\n{u.get('email', '')}\n\n"
            f"Dear Senator Wyden,\n\nPlease cosponsor S. 844 [1]. {extra}\n\nSincerely,\n{u['name']}")


SOURCES = "\n\nSources\n[1] Congress.gov: congress.gov/bill/119th-congress/senate-bill/844"


def _count() -> int:
    conn = db.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0]
    finally:
        conn.close()


def test_queue_letter_refuses_missing_sources_and_writes_nothing():
    before = _count()
    with pytest.raises(ValueError, match="no Sources list"):
        outbox.queue_letter("Ron Wyden", "t", _body(), channel="fax")
    assert _count() == before


def test_queue_letter_accepts_sources_and_the_escape_flag():
    ids = [outbox.queue_letter("Ron Wyden", "t", _body() + SOURCES, channel="fax"),
           outbox.queue_letter("Ron Wyden", "t", _body(), channel="fax", no_outside_facts=True)]
    try:
        assert all(db.get_letter_by_id(i)["status"] == "pending_approval" for i in ids)
    finally:
        conn = db.get_connection()
        conn.executemany("DELETE FROM letters WHERE id=?", [(i,) for i in ids])
        conn.commit()
        conn.close()


def test_queue_letter_keeps_other_lint_errors():
    with pytest.raises(ValueError, match="URL"):
        outbox.queue_letter("Ron Wyden", "t", _body("See https://x.org/a.") + SOURCES, channel="fax")


def test_queue_letter_from_markdown_passes_the_flag(tmp_path):
    from src.letter import write_letter_markdown
    path = write_letter_markdown("Ron Wyden", "t", _body(), "fax", status="draft")
    with pytest.raises(ValueError, match="no Sources list"):
        outbox.queue_letter_from_markdown(str(path), "Ron Wyden")
    lid = outbox.queue_letter_from_markdown(str(path), "Ron Wyden", no_outside_facts=True)
    conn = db.get_connection()
    conn.execute("DELETE FROM letters WHERE id=?", (lid,))
    conn.commit()
    conn.close()
