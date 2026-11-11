import json
import sqlite3

import pytest

from src import rep_media
from src.rep_record import ensure_schema


QUOTE = "Every Oregon community deserves clean drinking water and reliable services."


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def rep():
    return {
        "name": "Jane Doe",
        "title": "Senator",
        "chamber_hint": "Senate",
        "in_office_since": "2023",
    }


@pytest.fixture
def progress_calls(monkeypatch):
    calls = []
    original = rep_media.update_progress

    def record(connection, rep_name, source, dates_list, **kwargs):
        result = original(connection, rep_name, source, dates_list, **kwargs)
        calls.append({
            "rep": rep_name,
            "source": source,
            "dates": list(dates_list),
            **kwargs,
        })
        return result

    monkeypatch.setattr(rep_media, "update_progress", record)
    return calls


def _candidate(**overrides):
    item = {
        "url": "https://example.test/article",
        "outlet": "Example News",
        "date": "2024-05-17",
        "title": "An interview about Oregon",
        "kind": "article",
        "quotes": [QUOTE],
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    "template",
    [
        "```json\n%s\n```",
        "Here are the articles:\n%s\nThese are the results.",
        "Here are the articles:\n```json\n%s\n```\nEnd of results.",
    ],
)
def test_parse_candidates_accepts_fences_and_surrounding_prose(template):
    result = rep_media.parse_candidates(
        template % json.dumps([_candidate()]), 2024
    )

    assert result == [_candidate()]


def test_parse_candidates_filters_invalid_items_and_deduplicates_urls():
    items = [
        _candidate(date="2023-05-17"),
        _candidate(quotes=["Too short", "", "   ", None, 123]),
        _candidate(url="ftp://example.test/article"),
        _candidate(url="https:///missing-host"),
        _candidate(date="2024-02-30"),
        _candidate(quotes=QUOTE),
        _candidate(url=None),
        _candidate(date=None),
        None,
        "not an object",
        _candidate(),
        _candidate(title="A duplicate URL"),
    ]

    assert rep_media.parse_candidates(json.dumps(items), 2024) == [
        _candidate()
    ]


@pytest.mark.parametrize(
    ("raw_date", "expected_date"),
    [
        ("2024", "2024-01-01"),
        ("2024-02", "2024-02-01"),
        ("2024-02-29", "2024-02-29"),
    ],
)
def test_parse_candidates_pads_partial_dates(raw_date, expected_date):
    result = rep_media.parse_candidates(
        json.dumps([_candidate(date=raw_date)]), "2024"
    )

    assert len(result) == 1
    assert result[0]["date"] == expected_date


def test_parse_candidates_trims_filters_and_limits_quotes():
    valid_quotes = [
        f"Statement number {index} is long enough to be a valid quotation."
        for index in range(7)
    ]
    quotes = [None, 123, "", "short", "x" * 24, " " * 30]
    quotes += [f"  {quote}  " for quote in valid_quotes]

    result = rep_media.parse_candidates(
        json.dumps([_candidate(quotes=quotes)]), 2024
    )

    assert result[0]["quotes"] == valid_quotes[:5]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (" ARTICLE ", "article"),
        (" Interview ", "interview"),
        (" OP-ED ", "op-ed"),
        (" VIDEO ", "video"),
        ("unknown", "article"),
    ],
)
def test_parse_candidates_normalizes_kind(kind, expected):
    result = rep_media.parse_candidates(
        json.dumps([_candidate(kind=kind)]), 2024
    )

    assert result[0]["kind"] == expected


@pytest.mark.parametrize("text", ["", "not JSON", "[", "```json\n[broken]\n```"])
def test_parse_candidates_returns_empty_for_bad_json(text):
    assert rep_media.parse_candidates(text, 2024) == []


@pytest.mark.parametrize(
    "text",
    [
        f"Senator dOe said: {QUOTE}",
        f'{QUOTE} That was Senator DOE speaking.',
    ],
)
def test_attribute_verifies_surname_before_or_after_quote(text):
    def unexpected_judge(*args):
        pytest.fail("A nearby surname should establish attribution directly")

    assert rep_media.attribute(
        QUOTE, text, "Doe", judge=unexpected_judge
    ) == (1, "verified")


def test_attribute_reports_quote_that_is_not_verbatim():
    def unexpected_judge(*args):
        pytest.fail("The judge must not receive an unmatched quote")

    text = "Doe said every community should have safe water and good services."

    assert rep_media.attribute(
        QUOTE, text, "Doe", judge=unexpected_judge
    ) == (0, "reported")


def test_attribute_reports_matching_quote_without_nearby_surname_or_judge():
    text = f"Doe {'x' * 450} {QUOTE} {'y' * 450} Doe"

    assert rep_media.attribute(QUOTE, text, "Doe") == (0, "reported")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (True, (1, "verified-judge")),
        (False, (0, "reported")),
        (None, (0, "reported")),
    ],
)
def test_attribute_uses_judge_for_matching_quote_without_nearby_surname(
    answer, expected
):
    text = f"Doe {'x' * 600} {QUOTE} {'y' * 1500}"
    calls = []

    def judge(quote, context, name):
        calls.append((quote, context, name))
        return answer

    assert rep_media.attribute(QUOTE, text, "Doe", judge=judge) == expected
    assert len(calls) == 1
    quote, context, name = calls[0]
    assert quote == QUOTE
    assert name == "Doe"
    assert len(context) == 1200
    assert QUOTE in context
    assert "Doe" not in context


def test_collect_media_stores_source_kinds_and_verified_flags(
    conn, rep, progress_calls
):
    cases = [
        ("article", "We must build more affordable homes for working families.", 1),
        ("interview", "Public transit connects people to jobs and education.", 0),
        ("op-ed", "Investing in public schools strengthens our shared future.", 1),
        ("video", "Rural communities need dependable broadband and services.", 0),
        ("article", "We should protect public lands for future generations.", 0),
    ]
    candidates = [
        _candidate(
            url=f"https://example.test/{index}",
            kind=kind,
            quotes=[quote],
            verified=1,
        )
        for index, (kind, quote, _) in enumerate(cases)
    ]
    pages = {
        candidates[0]["url"]: f'Doe said: "{cases[0][1]}"',
        candidates[1]["url"]: cases[1][1],
        candidates[2]["url"]: f'{cases[2][1]} Written by Jane Doe.',
        candidates[3]["url"]: "Doe discussed expanding internet access.",
        candidates[4]["url"]: None,
    }
    events = []

    def gate():
        events.append("gate")

    def discover(prompt):
        events.append("discover")
        assert "Jane Doe" in prompt
        assert "PUBLISHED IN 2024" in prompt
        return json.dumps(candidates), 0.25

    def fetch(url):
        events.append(url)
        return pages[url]

    result = rep_media.collect_media(
        conn,
        rep,
        [2024],
        discover=discover,
        fetch=fetch,
        gate=gate,
        oldest=2024,
    )

    assert result == {
        "inserted": 5,
        "verified": 2,
        "reported": 3,
        "years_done": [2024],
        "usd": 0.25,
    }
    assert events == ["gate", "discover"] + [
        candidate["url"] for candidate in candidates
    ]

    rows = {
        row["url"]: dict(row)
        for row in conn.execute(
            "SELECT rep, date, title, venue, quote, quote_kind, url, "
            "source_kind, verified FROM rep_statements"
        )
    }
    assert len(rows) == len(candidates)
    source_kinds = {
        "article": "article",
        "interview": "article",
        "op-ed": "op-ed",
        "video": "video",
    }
    for candidate, (kind, quote, verified) in zip(candidates, cases):
        assert rows[candidate["url"]] == {
            "rep": rep["name"],
            "date": candidate["date"],
            "title": candidate["title"],
            "venue": candidate["outlet"],
            "quote": quote,
            "quote_kind": "personal",
            "url": candidate["url"],
            "source_kind": source_kinds[kind],
            "verified": verified,
        }

    assert progress_calls == [{
        "rep": rep["name"],
        "source": "statements:media",
        "dates": ["2024-05-17"] * 5,
        "cursor": "2024",
        "exhausted": 1,
        "note": None,
    }]


def test_collect_media_gates_every_topic_and_completes_years(
    conn, rep, progress_calls
):
    events = []

    def gate():
        events.append("gate")

    def discover(prompt):
        events.append(prompt)
        return "[]", 0.1

    def unexpected_fetch(url):
        pytest.fail("An empty candidate list must not fetch articles")

    result = rep_media.collect_media(
        conn,
        rep,
        [2024, 2023],
        discover=discover,
        fetch=unexpected_fetch,
        gate=gate,
        topics=("housing", "water"),
        oldest=2023,
    )

    assert result["years_done"] == [2024, 2023]
    assert result["inserted"] == result["verified"] == result["reported"] == 0
    assert result["usd"] == pytest.approx(0.4)
    assert len(events) == 8
    for index, (year, topic) in enumerate(
        [(2024, "housing"), (2024, "water"), (2023, "housing"), (2023, "water")]
    ):
        assert events[index * 2] == "gate"
        prompt = events[index * 2 + 1]
        assert f"PUBLISHED IN {year}" in prompt
        assert f"Restrict discovery to this topic: {topic}." in prompt

    assert progress_calls == [
        {
            "rep": rep["name"],
            "source": "statements:media",
            "dates": [],
            "cursor": "2024",
            "exhausted": None,
            "note": None,
        },
        {
            "rep": rep["name"],
            "source": "statements:media",
            "dates": [],
            "cursor": "2023",
            "exhausted": 1,
            "note": None,
        },
    ]
    assert rep_media.years_to_collect(
        conn, rep, 2024, 2023, "statements:media"
    ) == [2024]


def test_collect_media_budget_stop_preserves_committed_first_year(
    conn, rep, progress_calls
):
    spent = [0.0]
    events = []

    def gate():
        events.append("gate")

    def discover(prompt):
        events.append("discover")
        assert "PUBLISHED IN 2024" in prompt
        return json.dumps([_candidate()]), 0.5

    with pytest.raises(rep_media.BudgetStop):
        rep_media.collect_media(
            conn,
            rep,
            [2024, 2023],
            discover=discover,
            fetch=lambda url: f"Doe said: {QUOTE}",
            gate=gate,
            budget_usd=0.5,
            spent=spent,
            oldest=2023,
        )

    assert spent == [0.5]
    assert events == ["gate", "discover"]
    assert progress_calls == [{
        "rep": rep["name"],
        "source": "statements:media",
        "dates": ["2024-05-17"],
        "cursor": "2024",
        "exhausted": None,
        "note": None,
    }]
    assert not conn.in_transaction

    conn.rollback()
    rows = conn.execute(
        "SELECT date, quote, verified FROM rep_statements"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("2024-05-17", QUOTE, 1)]


def test_wait_for_idle_loops_and_announces_once(capsys):
    idle_values = iter([0, 599, 600])
    sleeps = []

    rep_media.wait_for_idle(
        min_idle=600,
        poll=7,
        sleep=sleeps.append,
        idle=lambda: next(idle_values),
    )

    assert sleeps == [7, 7]
    assert capsys.readouterr().out == "waiting for keyboard idle\n"


def test_credit_backend_requires_credit_source_environment(monkeypatch):
    monkeypatch.delenv("RESISTOR_CREDIT_SRC", raising=False)

    with pytest.raises(
        rep_media.BackendUnavailable, match="RESISTOR_CREDIT_SRC"
    ):
        rep_media.credit_backend()


@pytest.mark.parametrize(("name", "expected"), [("James Manning Jr.", "Manning"), ("Ron Wyden", "Wyden"), ("Cher", "Cher")])
def test_surname_drops_generational_suffix(name, expected):
    assert rep_media.surname(name) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ('It\u2019s a very significant factor," Wyden said.', "It\u2019s a very significant factor"),
    ("Never bet against Oregon,\u201d he said after the meeting.", "Never bet against Oregon"),
    ("A plain quote with no tag at all, which stays.", "A plain quote with no tag at all, which stays."),
])
def test_clean_quote_drops_dialogue_tag(raw, expected):
    assert rep_media.clean_quote(raw) == expected
