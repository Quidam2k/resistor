"""pac_groups: committee-group receipts, IEs and dark-money rows from stubbed FEC data (no network)."""
import sqlite3

from src import pac_groups

GROUP = {"label": "Test group", "footer_extra": "Extra note.", "categories": {"direct": "Direct", "ie": "IE", "separate": "Sep"},
         "committees": [{"id": "C1", "name": "Alpha PAC", "category": "direct"}, {"id": "C2", "name": "Beta Super PAC", "category": "ie"},
                        {"id": "C3", "name": "Gamma PAC", "category": "separate"}],
         "not_covered": ["Individual donors"]}


def _a(**kw):
    return {"is_individual": False, "memo_code": None, "contribution_receipt_amount": 1000, **kw}


def _e(**kw):
    return {"candidate_id": "REP", "candidate_office": "H", "candidate_office_district": "04", "support_oppose_indicator": "S",
            "expenditure_amount": 100, "election_type": "G2022", "committee_id": "C2", "payee_name": "P", "expenditure_date": "d",
            "is_notice": False, **kw}


def _fake(receipts=(), ie=(), by_cand=(), seen=None):
    def get(path, **params):
        if seen is not None: seen.append((path, params))
        if path == "schedules/schedule_a/": return {"results": list(receipts), "pagination": {}}
        if path == "schedules/schedule_e/": return {"results": list(ie), "pagination": {}}
        if path == "schedules/schedule_e/by_candidate/": return {"results": list(by_cand)}
        raise AssertionError(path)
    return get


def _rows(get, cycle=2022, ballot=(2022,), party="DEM"):
    return pac_groups.group_rows(get, "g", GROUP, ["REPCMTE"], "REP", cycle, "H", "04", "OR", set(ballot), party)


def test_receipts_ignore_individuals_sum_conduit_and_net_refunds():
    seen = []
    rows = _rows(_fake(receipts=[
        _a(contributor_id="C1", contribution_receipt_amount=2100, receipt_type="15K", contributor_name="FILER TEXT"),
        _a(contributor_id="C1", contribution_receipt_amount=-100),
        _a(contributor_id="C1", contribution_receipt_amount=1000, memo_code="X"),
        _a(contributor_id="C1", contribution_receipt_amount=500, memo_code="X"),
        _a(contributor_id="C1", contribution_receipt_amount=999, is_individual=True, contributor_name="A PERSON"),
        _a(contributor_id="C9", contribution_receipt_amount=777)], seen=seen))
    by_kind = {r[0]: r for r in rows if r[0] != "g_ie"}
    assert by_kind["g_pac"][1:5] == ("Alpha PAC", 2000, 2, "direct")  # name from config, refund netted
    assert by_kind["g_conduit"][1:5] == ("Alpha PAC", 1500, 2, "earmarked through")
    assert not any("PERSON" in str(r) or "FILER" in str(r) for r in rows)
    assert seen[0][1]["contributor_id"] == ["C1", "C2", "C3"]


def test_ie_support_primary_opponent_dedupe_and_drops():
    rows = [r for r in _rows(_fake(ie=[
        _e(), _e(is_notice=True, filing_form="F24"),                       # duplicate notice counts once
        _e(support_oppose_indicator="O", candidate_id="OPP", election_type="P2022", expenditure_amount=50, candidate_party="DEM"),
        _e(support_oppose_indicator="O", candidate_id="OTHERPARTY", election_type="P2022", candidate_party="REP"),  # other party's primary: dropped
        _e(support_oppose_indicator="O", candidate_id="GEN", election_type="G2022"),   # general opponent: dropped
        _e(candidate_id="OTHER", candidate_office_district="05", election_type="P2022", candidate_party="DEM"),  # other race: dropped
        _e(election_type="G2020"),                                          # wrong cycle: dropped
    ])) if r[0] == "g_ie"]
    assert {(r[4], r[2]) for r in rows} == {("support", 100), ("oppose primary opponent", 50)}


def test_dark_rows_only_non_disclosing_filers():
    rows = pac_groups.dark_rows(_fake(by_cand=[
        {"committee_id": "C90000001", "committee_name": "SOME 501C", "total": 12.0, "count": 2, "support_oppose_indicator": "O"},
        {"committee_id": "C00000002", "committee_name": "REAL PAC", "total": 99.0, "support_oppose_indicator": "S"},
        {"committee_id": "C90000003", "committee_name": "ZERO", "total": 0, "support_oppose_indicator": "S"}]), "REP", 2022)
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [("dark_ie", "SOME 501C", 12.0, "oppose")]


def test_paging_follows_keyset_and_stops_without_it():
    calls = []

    def get(path, **params):
        calls.append(params)
        return {"results": [_a(contributor_id="C1")], "pagination": {"last_indexes": {"last_index": "5", "last_contribution_receipt_amount": "1"}}} \
            if len(calls) == 1 else {"results": [], "pagination": {"last_indexes": {}}}
    list(pac_groups._paged(get, "schedules/schedule_a/", committee_id="X"))
    assert len(calls) == 2 and calls[1]["last_index"] == "5"


def _db(rows=(), marker=True):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE rep_donors (rep, cycle, kind, donor, amount, n, note, url, fetched_at)")
    if marker: c.execute("INSERT INTO rep_donors VALUES ('R',2022,'group_checked','g',0,3,'checked','u','t')")
    for r in rows: c.execute("INSERT INTO rep_donors VALUES ('R',?,?,?,?,?,?,?,'t')", r)
    return c


def test_lines_none_found_not_checked_and_forbidden_words():
    assert "none found among 3 listed" in pac_groups.summary_lines(_db(), "R", {"g": GROUP})[0]
    assert "not checked yet" in pac_groups.summary_lines(_db(marker=False), "R", {"g": GROUP})[0]
    assert "None found among 3" in chr(10).join(pac_groups.report_lines(_db(), "R", "g", GROUP))
    assert "Not checked yet" in chr(10).join(pac_groups.report_lines(_db(marker=False), "R", "g", GROUP))
    rows = [(2022, "g_pac", "Alpha PAC", 2000, 2, "direct", "http://x"), (2022, "g_ie", "Beta Super PAC", 50, 1, "oppose primary opponent", "http://y"),
            (2022, "dark_ie", "SOME 501C", 12, 1, "oppose", "http://z")]
    text = chr(10).join(pac_groups.summary_lines(_db(rows), "R", {"g": GROUP}) + pac_groups.report_lines(_db(rows), "R", "g", GROUP))
    assert "$2,000" in text and "oppose primary opponent" in text and "SOME 501C" in text and "Extra note." in text
    assert all(w not in text.lower() for w in ("paid by", "government", "jewish"))


def test_no_opponent_rows_when_rep_not_on_that_cycles_ballot():
    opp = _e(support_oppose_indicator="O", candidate_id="OPP", election_type="P2022", candidate_party="DEM")
    assert [r for r in _rows(_fake(ie=[opp]), ballot=(2020, 2026)) if r[0] == "g_ie"] == []
    assert [r for r in _rows(_fake(ie=[opp]), ballot=(2022,)) if r[0] == "g_ie"]


def test_notice_without_election_type_uses_date_for_own_candidate_only():
    rows = [r for r in _rows(_fake(ie=[_e(election_type=None, expenditure_date="2021-11-02"),
                                       _e(election_type=None, candidate_id="OPP", expenditure_date="2021-11-02")])) if r[0] == "g_ie"]
    assert [(r[4], r[2]) for r in rows] == [("support", 100)]  # 2021 date -> cycle 2022; opponent without election_type dropped


def test_ie_query_is_date_windowed_and_truncation_raises():
    import pytest
    seen = []
    _rows(_fake(seen=seen))
    ie = [p for path, p in seen if path == "schedules/schedule_e/"][0]
    assert (ie["min_date"], ie["max_date"]) == ("2021-01-01", "2022-12-31")

    def endless(path, **params):
        return {"results": [], "pagination": {"last_indexes": {"last_index": "1"}}}
    with pytest.raises(pac_groups.Truncated):
        list(pac_groups._paged(endless, "x"))


def test_per_cycle_marker_older_cycle_is_not_checked():
    c = _db()
    c.execute("INSERT INTO rep_donors VALUES ('R',2020,'employer','ACME',5,1,'','u','t')")  # cycle 2020 held, but never fetched for the group
    line = pac_groups.summary_lines(c, "R", {"g": GROUP})[0]
    assert "NOT yet checked: 2020" in line and "cycles checked: 2022" in line
    assert "NOT yet checked: 2020" in chr(10).join(pac_groups.report_lines(c, "R", "g", GROUP))


def test_dark_rows_paged_with_per_page_100():
    seen = []
    pac_groups.dark_rows(_fake(seen=seen), "REP", 2022)
    assert seen[0][1]["per_page"] == 100


def test_number_paged_endpoints_are_followed_and_capped():
    import pytest
    calls = []

    def get(path, **params):
        calls.append(params.get("page"))
        page = params.get("page") or 1
        return {"results": [{"i": page}], "pagination": {"page": page, "pages": 3}}
    assert [r["i"] for r in pac_groups._paged(get, "schedules/schedule_e/by_candidate/")] == [1, 2, 3]
    with pytest.raises(pac_groups.Truncated):
        list(pac_groups._paged(lambda path, **p: {"results": [], "pagination": {"page": p.get("page") or 1, "pages": 99}}, "x"))
