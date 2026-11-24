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
    assert "Senate eFD not checked" in out and "nothing is known either way" in out
    assert "Statements of Economic Interest" in out and "not checked yet" in out
