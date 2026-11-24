"""state_donors: ORESTAR HTML parsing, aggregation, replace-on-refetch, weekly gate, error redaction (no network)."""
import sqlite3
from datetime import datetime

import pytest
import requests

from src import rep_donors, rep_record, state_donors

REP = {"name": "Test Legislator", "chamber_hint": "OR_House", "orestar_committee_id": 999}


def _tr(i, date, status, contributor, amount, subtype="Cash Contribution"):
    return (f'<tr valign="middle" class="evenRow"><td><a href="/orestar/gotoPublicTransactionDetail.do?tranRsn={i}"> {i} </a></td>'
            f'<td>{date}</td><td>{status}</td><td><a href="/orestar/sooDetail.do?cneCommitteeId=999">Friends of Test</a></td>'
            f'<td>{contributor}</td><td>{subtype}</td><td>{amount}</td></tr>')


PAGE = ("<html><body>3 records found for the above search criteria<table><tr><th>Tran ID</th></tr>"
        + _tr(1, "03/02/2026", "Original", "Acme Pharma &amp; Co **", "$5,000.00")
        + _tr(2, "04/09/2026", "Amended", "Acme Pharma &amp; Co **", "$1,500.50")
        + _tr(3, "04/10/2026", "Deleted", "Ghost LLC", "$9,999.00")
        + _tr(4, "05/01/2026", "Original", "Oregon Realtors PAC (191)", "($250.00)")
        + _tr(5, "05/02/2026", "Original", "Bad Amount Inc", "n/a")
        + "</table></body></html>")


def test_parse_results_filters_and_cleans():
    total, rows = state_donors.parse_results(PAGE)
    assert total == 3
    assert [r["tran_id"] for r in rows] == ["1", "2", "4"]  # deleted status and unparseable amount dropped
    assert rows[0]["contributor"] == "Acme Pharma & Co **" and rows[0]["amount"] == 5000.0
    assert rows[2]["amount"] == -250.0  # refunds in parentheses are signed
    assert state_donors.parse_results("<html>0 records found</html>") == (0, [])
    assert state_donors.parse_results("<html>nothing</html>") == (0, [])


def test_aggregate_normalizes_names():
    _total, rows = state_donors.parse_results(PAGE)
    agg = state_donors.aggregate(rows)
    assert agg["Acme Pharma & Co"] == (6500.5, 2)  # '**' out-of-state marker stripped, rows summed
    assert agg["Oregon Realtors PAC"] == (-250.0, 1)  # committee id suffix dropped
    assert state_donors.normalize_name("  Foo   Bar (12) ") == "Foo Bar"


class FakeClient:
    def __init__(self, fail_year=None):
        self.calls, self.fail_year = [], fail_year

    def search(self, committee_id, start, end, ctype, amount_from=None):
        self.calls.append((committee_id, start, ctype, amount_from))
        if self.fail_year and start.endswith(str(self.fail_year)):
            raise requests.HTTPError("boom ?OWASP_CSRFTOKEN=ABCD-1234&x=1")
        year = start[-4:]
        _total, rows = state_donors.parse_results(PAGE)
        if ctype == "B": return 3, rows[:2], False  # business: Acme twice
        if ctype == "C": return 1, rows[2:], False  # committee: the refund only, nets negative and is dropped
        if ctype == "I": return 0, [], False
        return 0, [], False


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    rep_record.ensure_schema(c); rep_donors.ensure_schema(c)
    return c


def test_refresh_stores_rows_and_totals(conn):
    client = FakeClient()
    result = state_donors.refresh_rep(conn, dict(REP), since_year=2026, force=True, client=client)
    assert result["errors"] == [] and result["warnings"] == [] and result["cycles"] == 1
    rows = list(conn.execute("SELECT * FROM rep_donors WHERE rep=? AND cycle=2026", (REP["name"],)))
    assert [(r["kind"], r["donor"], r["amount"], r["n"]) for r in rows] == [("employer", "Acme Pharma & Co", 6500.5, 2)]
    assert rows[0]["url"] == "https://secure.sos.state.or.us/orestar/sooDetail.do?cneCommitteeId=999"
    total = conn.execute("SELECT * FROM rep_finance_totals WHERE rep=? AND cycle=2026", (REP["name"],)).fetchone()
    assert total["receipts"] == 6500.5 and total["pac_contributions"] == 0 and total["coverage_end"] == "2026-05-01"  # latest tran date seen, refund row included
    assert ("999", "01/01/2026", "I", "1000") in [(str(a), b, c, d) for a, b, c, d in client.calls]  # large individuals only


def test_show_says_orestar_not_fec(conn, capsys):
    state_donors.refresh_rep(conn, dict(REP), since_year=2026, force=True, client=FakeClient())
    rep_donors.show(conn, REP["name"])
    out = capsys.readouterr().out
    assert out.startswith("ORESTAR-reported finances") and "Source: ORESTAR" in out and "FEC" not in out
    assert "Acme Pharma & Co" in out


def test_replace_not_duplicate_and_weekly_gate(conn):
    state_donors.refresh_rep(conn, dict(REP), since_year=2026, force=True, client=FakeClient())
    assert state_donors.refresh_rep(conn, dict(REP), since_year=2026, client=FakeClient()) == {"skipped": True}
    state_donors.refresh_rep(conn, dict(REP), since_year=2026, force=True, client=FakeClient())
    assert conn.execute("SELECT COUNT(*) FROM rep_donors").fetchone()[0] == 1


def test_error_is_redacted_and_blocks_stamp_and_partial_rows(conn):
    result = state_donors.refresh_rep(conn, dict(REP), since_year=2026, force=True, client=FakeClient(fail_year=2026))
    assert len(result["errors"]) == 1 and "ABCD-1234" not in result["errors"][0] and "OWASP_CSRFTOKEN=***" in result["errors"][0]
    assert conn.execute("SELECT COUNT(*) FROM rep_donors").fetchone()[0] == 0  # no partial rows
    assert not state_donors._recent(conn, REP["name"])  # failed run is retried, not stamped


def test_missing_committee_id_raises(conn):
    with pytest.raises(RuntimeError, match="orestar_committee_id"):
        state_donors.refresh_rep(conn, {"name": "No Id", "chamber_hint": "OR_House"}, force=True, client=FakeClient())


def test_client_paging_uses_next_after_first_page(monkeypatch):
    seen = []
    pages = [PAGE, PAGE]

    class Resp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    client = state_donors.OrestarClient()
    monkeypatch.setattr(client, "get_token", lambda: ("OWASP_CSRFTOKEN", "tok"))
    monkeypatch.setattr(state_donors, "PAGE_ROWS", 3)  # the fixture page has 3 usable rows
    monkeypatch.setattr(client, "_http", lambda m, path, **kw: (seen.append(kw["params"]["cneSearchButtonName"]), Resp(pages.pop(0)))[1])
    total, rows, truncated = client.search(999, "01/01/2026", "12/31/2026", "B")
    # page 0 = 'search', page 1 = 'next'; page 1 repeats the same ids so paging stops with no new rows
    assert seen == ["search", "next"] and len(rows) == 3 and total == 3 and not truncated


def test_capped_group_is_a_warning_not_silent(conn):
    class Capped(FakeClient):
        def search(self, committee_id, start, end, ctype, amount_from=None):
            total, rows, _t = super().search(committee_id, start, end, ctype, amount_from)
            return total, rows, ctype == "B"
    result = state_donors.refresh_rep(conn, dict(REP), since_year=2026, force=True, client=Capped())
    assert result["errors"] == [] and result["warnings"] == ["2026 employer/B hit the 2000-row cap; sums for it may be low"]
