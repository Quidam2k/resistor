"""rep_trades: House Clerk index parsing, honest 'checked' wording, no partial writes, Senate/state lines (no network)."""
import sqlite3

import pytest
import requests

from src import rep_record, rep_trades

REP = {"name": "Val Hoyle", "chamber_hint": "House", "district": "OR-4"}


def _member(last, first, ftype, dst, year, date, doc):
    return (f"<Member><Prefix>Hon.</Prefix><Last>{last}</Last><First>{first}</First><Suffix /><FilingType>{ftype}</FilingType>"
            f"<StateDst>{dst}</StateDst><Year>{year}</Year><FilingDate>{date}</FilingDate><DocID>{doc}</DocID></Member>")


def _xml(*members):
    return ("﻿<?xml version=\"1.0\" encoding=\"utf-8\"?><FinancialDisclosure>" + "".join(members) + "</FinancialDisclosure>").encode("utf-8")


XML_2026 = _xml(
    _member("Hoyle", "Valerie", "P", "OR04", 2026, "5/15/2026", "20034581"),
    _member("Hoyle", "Valerie", "A", "OR04", 2026, "5/15/2026", "10099"),        # annual report, not a PTR
    _member("Hoyle", "Other", "P", "TX04", 2026, "5/16/2026", "20034999"),       # same surname, other district
    _member("Bonamici", "Suzanne", "P", "OR01", 2026, "5/17/2026", "20035000"),  # other Oregon member
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    rep_record.ensure_schema(c); rep_trades.ensure_schema(c)
    return c


def test_district_and_name_helpers():
    assert rep_trades._district_code(REP) == "OR04"
    assert rep_trades._district_code({"district": "WA-12"}) == "WA12"
    assert rep_trades._last_name({"name": "Bob Smith Jr."}) == "smith"


def test_parse_house_index_matches_only_this_members_ptrs():
    assert rep_trades.parse_house_index(XML_2026, REP) == [("20034581", 2026, "2026-05-15")]


def test_refresh_stores_filings_and_show_wording(conn, capsys):
    result = rep_trades.refresh_rep(conn, dict(REP), since_year=2026, force=True, fetch=lambda y: XML_2026)
    assert result == {"filings": 1, "years": 1, "errors": []}
    row = conn.execute("SELECT * FROM rep_trades").fetchone()
    assert row["url"] == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20034581.pdf" and row["filing_date"] == "2026-05-15"
    rep_trades.show(conn, "Val Hoyle", chamber="House")
    out = capsys.readouterr().out
    assert "1 periodic transaction report(s) on file" in out and "constituent letters only" in out


def test_none_on_file_is_not_called_clean(conn, capsys):
    empty = _xml(_member("Bonamici", "Suzanne", "P", "OR01", 2026, "5/17/2026", "1"))
    rep_trades.refresh_rep(conn, dict(REP), since_year=2026, force=True, fetch=lambda y: empty)
    rep_trades.show(conn, "Val Hoyle", chamber="House")
    out = capsys.readouterr().out
    assert "no periodic transaction reports on file" in out and "not that the member holds no stocks" in out and "clean" not in out


def test_one_failed_year_writes_nothing_and_does_not_stamp(conn):
    def fetch(year):
        if year == 2025: raise requests.ConnectionError("down")
        return XML_2026
    result = rep_trades.refresh_rep(conn, dict(REP), since_year=2025, force=True, fetch=fetch)
    assert result["errors"] and "2025" in result["errors"][0]
    assert conn.execute("SELECT COUNT(*) FROM rep_trades").fetchone()[0] == 0
    assert not rep_trades._recent(conn, "Val Hoyle")


def test_weekly_gate_and_missing_district(conn):
    rep_trades.refresh_rep(conn, dict(REP), since_year=2026, force=True, fetch=lambda y: XML_2026)
    assert rep_trades.refresh_rep(conn, dict(REP), since_year=2026, fetch=lambda y: pytest.fail("fetched")) == {"skipped": True}
    with pytest.raises(RuntimeError, match="district"):
        rep_trades.refresh_rep(conn, {"name": "No District"}, force=True, fetch=lambda y: XML_2026)


def test_senate_and_state_lines_never_claim_none(conn, capsys):
    rep_trades.show(conn, "Ron Wyden", chamber="Senate")
    rep_trades.show(conn, "Julie Fahey", chamber="OR_House")
    rep_trades.show(conn, "Val Hoyle", chamber="House")  # nothing fetched yet
    out = capsys.readouterr().out
    assert "senate_efd_refresh.py" in out and "not checked yet" in out
    assert "Statements of Economic Interest" in out and "not checked yet" in out


# ------------------------------------------------------------------ Senate eFD (mocked HTTP, no live calls)
SENATOR = {"name": "Ron Wyden", "chamber_hint": "Senate", "state": "OR"}
HOME = """<p>Title 1 of the Ethics in Government Act of 1978, as amended, 5 U.S.C. app. &sect; 105(c), states that:</p>
It shall be unlawful for any person to obtain or use a report: for any unlawful purpose; for any commercial purpose, other than by
news media; for determining the credit rating of any individual; or for use, directly or indirectly, in the solicitation of money.
<form action="" method="POST"><input type="hidden" name="csrfmiddlewaretoken" value="TOK123"></form>"""
ROWS = [
    ["Ron", "Wyden", "Wyden, Ron (Senator)", '<a href="/search/view/ptr/abc-1/" target="_blank">Periodic Transaction Report for 05/01/2026</a>', "05/05/2026"],
    ["Ron", "Wyden", "Wyden, Ron (Senator)", '<a href="/search/view/paper/def-2/" target="_blank">Periodic Transaction Report</a>', "01/09/2025"],
    ["Other", "Wydenx", "x", '<a href="/search/view/ptr/zzz/">x</a>', "01/01/2025"],
]


class FakeResp:
    def __init__(self, text="", url="", payload=None):
        self.text, self.url, self._payload = text, url, payload
    def raise_for_status(self): pass
    def json(self): return self._payload


class FakeSession:
    """Plays the eFD flow: home -> agreement POST -> /search/ -> report data."""
    def __init__(self, home=HOME, data_error=None, agree_url="https://efdsearch.senate.gov/search/"):
        self.home, self.data_error, self.agree_url = home, data_error, agree_url
        self.calls, self.cookies, self.headers = [], {"csrftoken": "C"}, {}
    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return FakeResp(self.home if url.endswith("/search/home/") else "<input name=\"csrfmiddlewaretoken\" value=\"T2\">", url)
    def post(self, url, data=None, headers=None, **kw):
        self.calls.append(("POST", url, dict(data or {})))
        if url.endswith("/search/home/"): return FakeResp(url=self.agree_url)
        if self.data_error: raise self.data_error
        return FakeResp(payload={"recordsFiltered": len(ROWS), "data": ROWS})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rep_trades, "_sleep", lambda s: None)


def test_agreement_flow_posts_checkbox_with_csrf_and_returns_text():
    session = FakeSession()
    text = rep_trades.efd_accept_agreement(session)
    assert "105(c)" in text and "solicitation of money" in text
    assert session.calls[1] == ("POST", "https://efdsearch.senate.gov/search/home/", {"prohibition_agreement": "1", "csrfmiddlewaretoken": "TOK123"})


def test_changed_agreement_accepts_nothing():
    session = FakeSession(home=HOME.replace("solicitation of money", "something new"))
    with pytest.raises(rep_trades.AgreementChanged):
        rep_trades.efd_accept_agreement(session)
    assert all(c[0] == "GET" for c in session.calls)  # no POST happened


def test_agreement_not_accepted_when_still_on_home():
    with pytest.raises(RuntimeError, match="did not accept"):
        rep_trades.efd_accept_agreement(FakeSession(agree_url="https://efdsearch.senate.gov/search/home/"))


def test_parse_efd_rows_keeps_paper_and_skips_other_senators():
    assert rep_trades.parse_efd_rows(ROWS, SENATOR) == [
        ("abc-1", 2026, "2026-05-05", "https://efdsearch.senate.gov/search/view/ptr/abc-1/"),
        ("def-2", 2025, "2025-01-09", "https://efdsearch.senate.gov/search/view/paper/def-2/")]
    with pytest.raises(ValueError):
        rep_trades.parse_efd_rows([["R", "Wyden", "", "no link", "05/05/2026"]], SENATOR)


def test_senate_refresh_and_show_wording_and_use_note(conn, capsys):
    result = rep_trades.refresh_senate_rep(conn, dict(SENATOR), FakeSession(), since_year=2025, force=True)
    assert result == {"filings": 2, "errors": []}
    rep_trades.show(conn, "Ron Wyden", chamber="Senate")
    out = capsys.readouterr().out
    assert "2 periodic transaction report(s) on file with the Senate eFD" in out and "1 filed on paper" in out
    assert "(paper)" in out and "5 U.S.C. app. 105(c)" in out and "fundraising" in out


def test_senate_partial_failure_writes_nothing(conn):
    result = rep_trades.refresh_senate_rep(conn, dict(SENATOR), FakeSession(data_error=requests.ConnectionError("down")), force=True)
    assert result["errors"] and conn.execute("SELECT COUNT(*) FROM rep_trades").fetchone()[0] == 0
    assert not rep_trades._recent(conn, "Ron Wyden", rep_trades.SENATE_SOURCE)


def test_use_flag_is_enforced_in_code(conn):
    rep_trades.refresh_senate_rep(conn, dict(SENATOR), FakeSession(), since_year=2025, force=True)
    assert len(rep_trades.read_rows(conn, "Ron Wyden", rep_trades.SENATE_SOURCE, "constituent_letter")) == 2
    for purpose in ("fundraising", "donation_ask", "shop", "third_wish", ""):
        with pytest.raises(PermissionError):
            rep_trades.read_rows(conn, "Ron Wyden", rep_trades.SENATE_SOURCE, purpose)
    assert {r[0] for r in conn.execute("SELECT use_flag FROM rep_trades")} == {rep_trades.USE_FLAG}


def test_agreement_doc_written_with_text_and_date(tmp_path):
    path = rep_trades.write_agreement_doc("TEXT 105(c)", root=tmp_path)
    body = path.read_text(encoding="utf-8")
    assert "TEXT 105(c)" in body and "First accepted: 20" in body and "msgs 67415, 67451" in body


def test_missing_state_is_an_error_not_a_default(conn):
    result = rep_trades.refresh_senate_rep(conn, {"name": "Ron Wyden"}, FakeSession(), force=True)
    assert result["errors"] and "state" in result["errors"][0]


def test_short_page_does_not_skip_rows():
    class Paged(FakeSession):
        def post(self, url, data=None, headers=None, **kw):
            if url.endswith("/search/home/"): return super().post(url, data, headers)
            start = int(data["start"]); self.calls.append(("POST", url, start))
            return FakeResp(payload={"recordsFiltered": 3, "data": ROWS[start:start + 2]})  # server caps pages at 2
    rows = rep_trades._efd_rows(Paged(), SENATOR, 2025)
    assert len(rows) == 3
