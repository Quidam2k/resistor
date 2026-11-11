"""Offline tests for representative records and vote collectors (#5863)."""

import json
import sqlite3
from datetime import date

import pytest
import requests

from src import db, rep_record, rep_votes


PERSONAL = "Working families deserve reliable services and a fair chance to succeed."
JOINT = "We urge immediate action to protect essential services in our communities."
OTHER = "Our communities need meaningful investments in transportation and housing."
FEED_URL = "https://example.test/feed"
RSS = f"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<item><title>Services announcement</title><link>http://example.test/release</link>
<pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>Washington, D.C. – October 15, 2026</p>
<p>“{PERSONAL}” Hoyle said.</p>]]></content:encoded></item>
<item><title>Index announcement</title><link>http://example.test/index</link>
<pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate>
<description>“{PERSONAL}” Hoyle said.</description></item>
</channel></rss>"""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail(f"Unexpected network call: {args!r}")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    for module in (rep_record, rep_votes):
        monkeypatch.setattr(module, "PACE_SECONDS", 0, raising=False)
        monkeypatch.setattr(module, "fetch_text", blocked)
        monkeypatch.setattr(module, "odata", blocked)
    monkeypatch.setattr(rep_votes, "_get_with_backoff", blocked, raising=False)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "record.db")
    db.init_db()
    connection = db.get_connection()
    rep_record.ensure_schema(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def rss_rep(monkeypatch):
    def fetch(url):
        assert url == FEED_URL
        return RSS

    monkeypatch.setattr(rep_record, "fetch_text", fetch)
    return {"name": "Val Hoyle", "title": "US Representative",
            "feeds": [{"kind": "rss", "url": FEED_URL}],
            "or_code": None, "chamber_hint": "house"}


def test_normalize():
    assert rep_record.normalize("  ‘Fair’\u00a0 “services” —\n now  ") == (
        "'Fair' \"services\" - now"
    )


def test_verify_quote_requires_verbatim_normalized_substring():
    source = "She said, “Reliable\u00a0services — for everyone.”"
    assert rep_record.verify_quote('"Reliable services - for everyone."', source)
    assert not rep_record.verify_quote('"Reliable services - for somebody."', source)


def test_page_text_removes_noncontent_blocks():
    raw = """<SCRIPT>bad script</SCRIPT><style>bad style</style><nav>bad nav</nav>
    <header>bad header</header><footer>bad footer</footer>
    <p>Clean <b>text</b> &amp; useful information.</p>"""
    assert rep_record.page_text(raw) == "Clean text & useful information."


def test_extract_quotes_attribution():
    personal = f'“{PERSONAL},” Hoyle said.'
    joint = f'"{JOINT}" the lawmakers wrote.'
    other = f'"{OTHER}" said Senator Anthony Broadman (D - Bend).'
    release = ("\n" + "Background information. " * 20).join(
        [joint, personal, other]
    )
    assert rep_record.extract_quotes(release, "Hoyle") == [
        {"quote": PERSONAL, "kind": "personal"},
        {"quote": JOINT, "kind": "joint_letter"},
    ]
    assert rep_record.extract_quotes(other, "Kotek") == []


def test_extract_quotes_never_credits_a_cosigner():
    text = (f'"{OTHER}," said Congressman Ryan Zinke. "{JOINT}" '
            f'Background. "{PERSONAL}" Hoyle said. "{JOINT} Again and again."')
    quotes = rep_record.extract_quotes(text, "Hoyle")
    assert quotes[0] == {"quote": PERSONAL, "kind": "personal"}
    assert {"quote": JOINT + " Again and again.", "kind": "personal"} in quotes  # continuation
    assert all(q["quote"] not in (OTHER, JOINT) for q in quotes)  # Zinke's words


def test_extract_quotes_stops_at_sentence_end():
    text = f'"{OTHER}" said Zinke. Hoyle attended the event. "{PERSONAL}," said Rep. Val Hoyle.'
    assert rep_record.extract_quotes(text, "Hoyle") == [{"quote": PERSONAL, "kind": "personal"}]


def test_parse_rss_preserves_full_body_only():
    items = rep_record.parse_rss(RSS)
    assert len(items) == 2
    assert [item["date"] for item in items] == ["2026-10-15", "2026-10-16"]
    assert [item["url"] for item in items] == [
        "https://example.test/release", "https://example.test/index"
    ]
    assert items[0]["title"] == "Services announcement"
    assert PERSONAL in items[0]["body_html"]
    assert items[1]["body_html"] is None


def test_parse_listing_filters_and_deduplicates_in_order():
    raw = """<a href="/news/">Index</a><a href="/news/first">First</a>
    <a href="https://example.test/news/first">Duplicate</a>
    <a href="/news/table/2">Table</a><a href="/news/second?view=all">Query</a>
    <a href="/elsewhere">Elsewhere</a><a href="/news/second">Second</a>"""
    assert rep_record.parse_listing(raw, "https://example.test/news/", "/news/") == [
        "https://example.test/news/first", "https://example.test/news/second"
    ]


def test_find_date_and_venue():
    assert rep_record.find_date("June 03, 2026; July 4, 2026") == "2026-06-03"
    assert rep_record.find_date("No publication date") is None
    assert rep_record.find_venue("Washington, D.C. – News") == "Washington, D.C."
    assert rep_record.find_venue("Salem, OR – News") == "Salem, OR"
    assert rep_record.find_venue("No dateline here") == "not stated"
    assert rep_record.find_venue("x" * 1501 + " Salem, OR – News") == "not stated"


def test_collect_statements_verified_index_only_and_idempotent(conn, rss_rep):
    assert len(rep_record.collect_statements(conn, rss_rep, "2026-10-01")) == 2
    rows = conn.execute("SELECT * FROM rep_statements ORDER BY date").fetchall()
    assert len(rows) == 2
    assert rows[0]["quote"] == PERSONAL
    assert rows[0]["quote_kind"] == "personal"
    assert rows[0]["verified"] == 1
    assert rows[0]["venue"] == "Washington, D.C."
    assert rows[0]["url"] == "https://example.test/release"
    assert rows[0]["source_kind"] == "official"
    assert rows[1]["quote"] is None and rows[1]["quote_kind"] is None
    assert rows[1]["verified"] == 0 and rows[1]["venue"] == "not stated"
    assert rows[1]["url"] == "https://example.test/index"
    assert len({row["content_hash"] for row in rows}) == 2
    assert rep_record.collect_statements(conn, rss_rep, "2026-10-01") == []
    assert conn.execute("SELECT COUNT(*) FROM rep_statements").fetchone()[0] == 2


def test_collect_statements_checks_quote_against_fetched_text(
    conn, rss_rep, monkeypatch
):
    monkeypatch.setattr(rep_record, "extract_quotes", lambda text, last_name: [
        {"quote": OTHER, "kind": "personal"}
    ])
    rep_record.collect_statements(conn, rss_rep, "2026-10-01")
    row = conn.execute(
        "SELECT quote, verified FROM rep_statements WHERE quote IS NOT NULL"
    ).fetchone()
    assert row["quote"] == OTHER and row["verified"] == 0


def test_add_media_statement_requires_same_rep_official_match(conn, rss_rep):
    def add(rep, suffix):
        return rep_record.add_media_statement(
            conn, rep, "2026-10-17", f"https://example.test/media/{suffix}",
            PERSONAL, title="Interview", venue="Salem, OR"
        )

    assert add("Val Hoyle", "before")["verified"] == 0
    rep_record.collect_statements(conn, rss_rep, "2026-10-01")
    matched = add("Val Hoyle", "after")
    assert matched["verified"] == 1 and matched["source_kind"] == "media"
    assert add("Tina Kotek", "different-rep")["verified"] == 0
    padded = rep_record.add_media_statement(
        conn, "Val Hoyle", "2026-10-18", "https://example.test/media/padded",
        PERSONAL + " And a sentence she never said.")
    assert padded["verified"] == 0


@pytest.mark.parametrize("congress,kind,number,path", [
    (119, "S", 5658, "119th-congress/senate-bill/5658"),
    (111, "HR", 1, "111th-congress/house-bill/1"),
    (112, "SJRES", 3, "112th-congress/senate-joint-resolution/3"),
    (121, "HRES", 2, "121st-congress/house-resolution/2"),
])
def test_bill_url_ordinals(congress, kind, number, path):
    assert rep_votes.bill_url(congress, kind, number) == (
        f"https://www.congress.gov/bill/{path}"
    )


def test_bill_url_unknown_type():
    assert rep_votes.bill_url(119, "UNKNOWN", 1) is None


def test_collect_voteview_and_progress(conn, monkeypatch):
    rollcalls = """rollnumber,date,session,clerk_rollnumber,bill_number,vote_question,vote_desc,vote_result,yea_count,nay_count
1,2026-01-05,2,7,S.1,On Passage,First bill,Passed,60,40
2,2026-01-06,2,42,S.2,On Cloture,Second bill,Failed,49,51
3,2026-01-07,2,43,,On Motion,Third motion,Passed,70,30
"""
    votes = """congress,chamber,icpsr,rollnumber,cast_code
119,Senate,14871,1,1
119,Senate,14871,2,6
119,Senate,99999,1,4
119,Senate,14871,3,0
"""
    payloads = {
        "https://voteview.com/static/data/out/rollcalls/S119_rollcalls.csv": rollcalls,
        "https://voteview.com/static/data/out/votes/S119_votes.csv": votes,
    }
    monkeypatch.setattr(rep_votes, "fetch_text", lambda url: payloads[url])
    rep = {"name": "Ron Wyden", "voteview_icpsr": 14871, "feeds": []}
    for _ in range(2):
        rep_votes.collect_voteview(conn, rep, "S", [119], 14871)
        rows = conn.execute(
            "SELECT * FROM voting_records ORDER BY vote_date"
        ).fetchall()
        assert [row["vote"] for row in rows] == ["Yea", "Nay"]
        assert [row["roll_url"] for row in rows] == [
            "https://www.senate.gov/legislative/LIS/roll_call_votes/"
            f"vote1192/vote_119_2_{roll:05d}.htm" for roll in (7, 42)
        ]
        assert all(row["session"] == "119-2" for row in rows)
        assert all(row["chamber"] == "Senate" for row in rows)
        assert rows[0]["source_url"] == "https://voteview.com/rollcall/RS1190001"
        progress = conn.execute(
            "SELECT * FROM backfill_progress WHERE rep=? AND source=?",
            ("Ron Wyden", "votes:voteview-senate"),
        ).fetchone()
        assert progress is not None and progress["items"] == 2
        assert progress["oldest_date"] == "2026-01-05"
        assert progress["newest_date"] == "2026-01-06"


def test_import_senate_rejects_entire_batch_for_invalid_url(conn, tmp_path):
    valid = {
        "rep": "Ron Wyden", "vote_number": 1, "date": "2026-01-05",
        "question": "On Passage", "measure": "S.1", "title": "First bill",
        "vote": "Yea", "url": (
            "https://www.senate.gov/legislative/LIS/roll_call_votes/"
            "vote1192/vote_119_2_00001.htm"
        ),
    }
    path = tmp_path / "senate.json"
    path.write_text(json.dumps([
        valid, dict(valid, vote_number=2, url="https://example.test/vote")
    ]), encoding="utf-8")
    with pytest.raises(ValueError):
        rep_votes.import_senate_votes(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM voting_records").fetchone()[0] == 0


@pytest.mark.parametrize("oldest,year,expected", [
    ("2023-01-01", 2020, 50), ("2010-01-01", 2020, 100),
    ("2027-01-01", 2020, 0), ("2026-01-01", 2020, 0),
    (None, 2020, None), ("2023-01-01", None, None), ("2023-01-01T09:28:55", 2020, 50),
])
def test_coverage_pct(oldest, year, expected):
    assert rep_record.coverage_pct(oldest, year, date(2026, 1, 1)) == expected


def test_ensure_schema_migrates_existing_votes_idempotently():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("""CREATE TABLE voting_records (
            id INTEGER PRIMARY KEY, representative TEXT NOT NULL,
            vote_date TEXT NOT NULL, bill_id TEXT, bill_title TEXT,
            vote TEXT NOT NULL, chamber TEXT, session TEXT, source_url TEXT)""")
        connection.execute(
            "INSERT INTO voting_records (representative, vote_date, vote) "
            "VALUES ('Existing Rep', '2026-01-01', 'Yea')"
        )
        rep_record.ensure_schema(connection)
        rep_record.ensure_schema(connection)
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(voting_records)"
        )}
        assert {"roll_url", "question"} <= columns
        assert connection.execute("SELECT COUNT(*) FROM voting_records").fetchone()[0] == 1
        for table in ("rep_statements", "rep_bills", "backfill_progress"):
            assert connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()

