import sqlite3

import pytest
import requests

from src import rep_crec, rep_record, rep_wayback


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.headers = {} if headers is None else headers
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self
            )


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Unexpected real HTTP request")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    rep_record.ensure_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def rep():
    return {
        "name": "Jordan Lee",
        "in_office_since": "2008",
        "campaign_domains": ["example.org"],
    }


def words(prefix, count):
    return " ".join(f"{prefix}{index}" for index in range(count))


def crec_item(granule, date="2026-03-04"):
    return {
        "packageId": "CREC-2026-03-04",
        "granuleId": granule,
        "dateIssued": date,
        "title": f"Remarks {granule}",
    }


def progress(conn, rep, source):
    return conn.execute(
        "SELECT * FROM backfill_progress WHERE rep=? AND source=?",
        (rep["name"], source),
    ).fetchone()


def test_speaker_segments_separates_turns_and_ignores_narrative_marker():
    raw = (
        "<pre>\n"
        "  Mr. LEE. First opening [[Page S1]] continues.\n"
        "  Mr. President, I rise to support this measure.\n"
        "  More of the first turn.\n"
        "  The PRESIDING OFFICER. The time has expired.\n"
        "  Mr. LEE. Second closing.\n"
        "  Mr. LEED. This belongs to someone else.\n"
        "</pre>"
    )

    assert rep_crec.speaker_segments(raw, "Lee") == [
        "First opening continues. "
        "Mr. President, I rise to support this measure. "
        "More of the first turn.",
        "Second closing.",
    ]


def test_speaker_segments_matches_hoyle_with_state():
    raw = (
        "\n  Ms. HOYLE of Oregon. Oregon families deserve support.\n"
        "  Mr. LEED. Another member speaks.\n"
    )

    assert rep_crec.speaker_segments(raw, "Hoyle") == [
        "Oregon families deserve support."
    ]


def test_collect_crec_venues_verification_word_filter_and_rerun(conn, rep):
    senate_quote = words("senate", 40)
    extension_quote = words("extension", 41)
    short_quote = words("short", 39)
    senate_tokens = senate_quote.split()
    senate_raw = (
        "\n  Mr. LEE. "
        + " ".join(senate_tokens[:20])
        + " [[Page S1]] "
        + " ".join(senate_tokens[20:])
        + "\n  The PRESIDING OFFICER. Next speaker.\n"
        + "  Mr. LEE. "
        + short_quote
    )
    items = [crec_item("CREC-PgS1"), crec_item("CREC-PgE2")]
    search_calls = []
    fetch_calls = []

    def search(surname, year):
        search_calls.append((surname, year))
        return items

    def fetch(url):
        fetch_calls.append(url)
        if url.endswith("/CREC-PgS1.htm"):
            return senate_raw
        assert url.endswith("/CREC-PgE2.htm")
        return "\n  Mr. LEE. " + extension_quote

    result = rep_crec.collect_crec(
        conn, rep, [2026], search=search, fetch=fetch
    )

    assert result == {"inserted": 2, "errors": [], "years_done": [2026]}
    rows = conn.execute(
        "SELECT * FROM rep_statements ORDER BY id"
    ).fetchall()
    assert [row["quote"] for row in rows] == [
        senate_quote,
        extension_quote,
    ]
    assert [row["venue"] for row in rows] == [
        "Senate floor",
        "Extensions of Remarks",
    ]
    assert all(row["verified"] == 1 for row in rows)
    assert all(row["quote_kind"] == "floor" for row in rows)
    assert all(row["source_kind"] == "floor" for row in rows)
    assert [row["url"] for row in rows] == fetch_calls
    assert search_calls == [("Lee", 2026)]

    rerun = rep_crec.collect_crec(
        conn, rep, [2026], search=search, fetch=fetch
    )

    assert rerun == {"inserted": 0, "errors": [], "years_done": [2026]}
    assert conn.execute("SELECT COUNT(*) FROM rep_statements").fetchone()[0] == 2
    saved_progress = progress(conn, rep, "statements:crec")
    assert saved_progress["cursor"] == "2026"
    assert saved_progress["items"] == 2
    assert saved_progress["exhausted"] == 1


def test_collect_crec_records_granule_http_error_and_continues(conn, rep):
    calls = []
    quote = words("saved", 40)

    def search(surname, year):
        return [crec_item("bad-PgS1"), crec_item("good-PgE2")]

    def fetch(url):
        calls.append(url)
        if url.endswith("/bad-PgS1.htm"):
            FakeResponse(404).raise_for_status()
        return "\n  Mr. LEE. " + quote

    result = rep_crec.collect_crec(
        conn, rep, [2026], search=search, fetch=fetch
    )

    assert result == {
        "inserted": 1,
        "errors": [{
            "rep": rep["name"],
            "source": "statements:crec",
            "error": "HTTP 404",
        }],
        "years_done": [2026],
    }
    assert len(calls) == 2
    row = conn.execute("SELECT * FROM rep_statements").fetchone()
    assert row["quote"] == quote
    assert row["url"].endswith("/good-PgE2.htm")
    assert progress(conn, rep, "statements:crec")["items"] == 1


def test_collect_crec_search_http_error_stops_without_completing_year(conn, rep):
    calls = []

    def search(surname, year):
        calls.append((surname, year))
        FakeResponse(503).raise_for_status()

    def fetch(url):
        pytest.fail("Fetch must not run after search fails")

    result = rep_crec.collect_crec(
        conn, rep, [2025, 2026], search=search, fetch=fetch
    )

    assert result == {
        "inserted": 0,
        "errors": [{
            "rep": rep["name"],
            "source": "statements:crec",
            "error": "HTTP 503",
        }],
        "years_done": [],
    }
    assert calls == [("Lee", 2026)]
    assert progress(conn, rep, "statements:crec") is None
    assert conn.execute("SELECT COUNT(*) FROM rep_statements").fetchone()[0] == 0


@pytest.mark.parametrize(
    "stored,year,expected",
    [
        ("2010", 2026, None),
        ("2010", 2009, "2009"),
        (None, 2026, "2026"),
    ],
)
def test_resume_cursor(conn, rep, stored, year, expected):
    if stored is not None:
        rep_record.update_progress(
            conn, rep["name"], "statements:crec", [], cursor=stored
        )

    assert rep_crec.resume_cursor(
        conn, rep["name"], "statements:crec", year
    ) == expected


@pytest.mark.parametrize(
    "stored,exhausted,since,this_year,floor,expected",
    [
        ("2010", 1, "2008", 2026, 1994, [2026]),
        ("2010", 0, "2008", 2026, 1994, [2026, 2009, 2008]),
        (None, 0, "2008", 2010, 1994, [2010, 2009, 2008]),
        (None, 0, "1990", 1996, 1994, [1996, 1995, 1994]),
    ],
)
def test_years_to_collect(
    conn, rep, stored, exhausted, since, this_year, floor, expected
):
    rep = {**rep, "in_office_since": since}
    if stored is not None or exhausted:
        rep_record.update_progress(
            conn,
            rep["name"],
            "statements:crec",
            [],
            cursor=stored,
            exhausted=exhausted,
        )

    assert rep_crec.years_to_collect(
        conn, rep, this_year, floor=floor
    ) == expected


@pytest.mark.parametrize("first_outcome", [429, 503, "connection"])
def test_polite_get_retries_then_returns_response(first_outcome):
    sleeps = []
    calls = []
    success = FakeResponse(text="success")

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            if first_outcome == "connection":
                raise requests.ConnectionError("connection refused")
            return FakeResponse(first_outcome)
        return success

    result = rep_wayback.polite_get(
        "https://example.org/about",
        params={"example": "value"},
        get=get,
        sleep=sleeps.append,
    )

    assert result is success
    assert sleeps == [5.0, 60.0, 5.0]
    assert len(calls) == 2
    for url, kwargs in calls:
        assert url == "https://example.org/about"
        assert kwargs["params"] == {"example": "value"}
        assert kwargs["timeout"] == 60
        assert kwargs["headers"]["User-Agent"]


@pytest.mark.parametrize("outcome", [429, "connection"])
def test_polite_get_exhausts_four_attempts_without_final_backoff(outcome):
    sleeps = []
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if outcome == "connection":
            raise requests.ConnectionError("connection refused")
        return FakeResponse(429)

    with pytest.raises(RuntimeError, match="^wayback rate limited$"):
        rep_wayback.polite_get(
            "https://example.org", get=get, sleep=sleeps.append
        )

    assert len(calls) == 4
    assert sleeps == [5.0, 60.0, 5.0, 120.0, 5.0, 240.0, 5.0]


def test_polite_get_retry_after_overrides_shorter_backoff():
    sleeps = []
    responses = iter([
        FakeResponse(429, headers={"Retry-After": "300"}),
        FakeResponse(text="success"),
    ])

    def get(url, **kwargs):
        return next(responses)

    response = rep_wayback.polite_get(
        "https://example.org", get=get, sleep=sleeps.append
    )

    assert response.text == "success"
    assert sleeps == [5.0, 300.0, 5.0]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.org/", True),
        ("https://example.org/%41BOUT.html", True),
        ("https://example.org/press.aspx#latest", True),
        ("https://example.org/issues?topic=schools", False),
        ("https://example.org/wp-content/issues.html", False),
        ("https://example.org/plan.pdf", False),
        ("https://example.org/contact", False),
        ("http://[broken/about", False),
    ],
)
def test_wanted_path(url, expected):
    assert rep_wayback.wanted_path(url) is expected


def test_collect_wayback_deduplicates_skips_short_and_skips_http_error(conn, rep):
    entries = [
        ("20260102030405", "https://example.org/about"),
        ("20260203040506", "https://example.org/issues"),
        ("20260304050607", "https://example.org/plan"),
        ("20260405060708", "https://example.org/press"),
    ]
    long_raw = "<title>Our values</title><article>" + words("policy", 80) + "</article>"
    short_raw = "<article>" + words("short", 79) + "</article>"
    calls = []

    def get(url, params=None):
        calls.append(url)
        if url == rep_wayback._CDX:
            assert ("from", "2026") in params
            assert ("to", "2026") in params
            return FakeResponse(
                text="cdx results",
                payload=[["timestamp", "original"], *entries],
            )
        if url.endswith("/about") or url.endswith("/issues"):
            return FakeResponse(text=long_raw)
        if url.endswith("/plan"):
            return FakeResponse(text=short_raw)
        assert url.endswith("/press")
        FakeResponse(404).raise_for_status()

    result = rep_wayback.collect_wayback(conn, rep, [2026], get=get)

    assert result == {"inserted": 1, "errors": [], "years_done": [2026]}
    assert calls == [rep_wayback._CDX] + [
        f"{rep_wayback._WEB}{stamp}id_/{original}"
        for stamp, original in entries
    ]
    rows = conn.execute("SELECT * FROM rep_statements").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["rep"] == rep["name"]
    assert row["date"] == "2026-01-02"
    assert row["title"] == "Our values"
    assert row["venue"] == "campaign site example.org (archived)"
    assert row["quote"] == rep_record.normalize(rep_record.page_text(long_raw))
    assert row["quote_kind"] == "campaign"
    assert row["source_kind"] == "campaign"
    assert row["verified"] == 1
    assert row["url"] == (
        f"{rep_wayback._WEB}{entries[0][0]}/{entries[0][1]}"
    )
    saved_progress = progress(conn, rep, "statements:wayback")
    assert saved_progress["cursor"] == "2026"
    assert saved_progress["items"] == 1
    assert saved_progress["exhausted"] == 1
    assert saved_progress["note"] == ""
    assert not conn.in_transaction


def test_collect_wayback_runtime_error_stops_walk_and_preserves_saved_page(conn, rep):
    entries = [
        ("20260102030405", "https://example.org/about"),
        ("20260203040506", "https://example.org/issues"),
        ("20260304050607", "https://example.org/plan"),
    ]
    calls = []

    def get(url, params=None):
        calls.append(url)
        if url == rep_wayback._CDX:
            assert ("from", "2026") in params
            return FakeResponse(
                text="cdx results",
                payload=[["timestamp", "original"], *entries],
            )
        if url.endswith("/about"):
            return FakeResponse(text="<article>" + words("policy", 80) + "</article>")
        if url.endswith("/issues"):
            raise RuntimeError("wayback rate limited")
        pytest.fail("Walk continued after rate limiting")

    result = rep_wayback.collect_wayback(
        conn, rep, [2025, 2026], get=get
    )

    assert result == {
        "inserted": 1,
        "errors": ["wayback rate limited"],
        "years_done": [],
    }
    assert calls == [
        rep_wayback._CDX,
        f"{rep_wayback._WEB}{entries[0][0]}id_/{entries[0][1]}",
        f"{rep_wayback._WEB}{entries[1][0]}id_/{entries[1][1]}",
    ]
    assert conn.execute("SELECT COUNT(*) FROM rep_statements").fetchone()[0] == 1
    saved_progress = progress(conn, rep, "statements:wayback")
    assert saved_progress["cursor"] is None
    assert saved_progress["exhausted"] == 0
    assert saved_progress["items"] == 1
    assert saved_progress["oldest_date"] == "2026-01-02"
    assert saved_progress["note"] == "rate limited, resume later"
    assert not conn.in_transaction


@pytest.mark.parametrize("domains", [[], None])
def test_collect_wayback_empty_campaign_domains_returns_immediately(conn, rep, domains):
    rep = {**rep, "campaign_domains": domains}

    def get(*args, **kwargs):
        pytest.fail("No HTTP should occur without campaign domains")

    result = rep_wayback.collect_wayback(conn, rep, [2026], get=get)

    assert result == {
        "inserted": 0,
        "errors": [],
        "years_done": [],
        "note": "no campaign_domains configured",
    }
    assert progress(conn, rep, "statements:wayback") is None
    assert conn.execute("SELECT COUNT(*) FROM rep_statements").fetchone()[0] == 0


def test_partial_run_is_not_exhausted(conn, rep):
    """backfill-crec --max-years 1 walks one year; the career floor, not the list, decides exhaustion."""
    rep_crec.collect_crec(conn, rep, [2026], search=lambda s, y: [], fetch=lambda u: "", oldest=2008)
    assert progress(conn, rep, "statements:crec")["exhausted"] == 0
    assert rep_crec.years_to_collect(conn, rep, 2026)[:2] == [2026, 2025]
