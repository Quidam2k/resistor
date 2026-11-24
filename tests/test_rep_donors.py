"""rep_donors parsing, replace-on-refetch, weekly gate and key redaction (FEC mocked; no network)."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src import rep_donors, rep_record

REP = {"name": "Test Senator", "chamber_hint": "Senate", "fec_candidate_id": "S0TEST"}


def _fake_get(path, **params):
    if path == "candidate/S0TEST/": return {"results": [{"cycles": [2022, 2024, 2025, 2026]}]}
    if path == "candidate/S0TEST/totals/":
        return {"results": [{"cycle": 2024, "receipts": 100.0, "individual_itemized_contributions": 60.0,
                             "individual_unitemized_contributions": 20.0, "other_political_committee_contributions": 10.0,
                             "coverage_end_date": "2024-12-31T00:00:00"}, {"cycle": None, "receipts": 1}]}
    if path == "candidate/S0TEST/committees/": return {"results": [{"committee_id": "C1", "cycles": [2022, 2024, 2026]}]}
    if path == "schedules/schedule_a/by_employer/":
        return {"results": [{"employer": "NOT EMPLOYED", "total": 999, "count": 9}, {"employer": "ACME PHARMA", "total": 50, "count": 2}]}
    if path == "schedules/schedule_a/by_size/": return {"results": [{"size": 200, "total": 30, "count": 3}, {"size": 0, "total": None}]}
    if path == "schedules/schedule_a/":
        if params.get("last_index"): return {"results": [], "pagination": {}}
        return {"results": [
            {"contributor_name": "Big Bank PAC", "contribution_receipt_amount": 5000, "entity_type": "PAC"},
            {"contributor_name": "Big Bank PAC", "contribution_receipt_amount": 2000, "entity_type": "PAC"},
            {"contributor_name": "SENATOR FOR TEST", "contribution_receipt_amount": 9000, "entity_type": "COM"},
            {"contributor_name": "ACTBLUE", "contribution_receipt_amount": 8000, "entity_type": "PAC"},
            {"contributor_name": "Some Corp", "contribution_receipt_amount": 700, "entity_type": "ORG"}],
            "pagination": {"last_indexes": {"last_index": "9", "last_contribution_receipt_amount": "700"}}}
    if path == "committees/": return {"results": [{"committee_id": "C9", "name": "TEST LEADERSHIP PAC"}]}
    if path == "committee/C9/totals/": return {"results": [{"receipts": 123.0}]}
    if path == "schedules/schedule_e/by_candidate/":
        return {"results": [{"committee_name": "OUTSIDE GROUP", "support_oppose_indicator": "O", "total": 40.0, "count": 1},
                            {"committee_name": "ZERO", "support_oppose_indicator": "S", "total": 0}]}
    raise AssertionError(path)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(rep_donors, "_get", _fake_get)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    rep_record.ensure_schema(c)
    return c


def _rows(conn, kind): return {r["donor"]: r for r in conn.execute("SELECT * FROM rep_donors WHERE kind=? AND cycle=2024", (kind,))}


def test_refresh_parses_and_filters(conn):
    result = rep_donors.refresh_rep(conn, dict(REP), since_cycle=2022, force=True)
    assert not result["errors"] and result["cycles"] == 3  # 2022 has no totals row but is still fetched; odd cycle 2025 skipped
    assert list(_rows(conn, "employer")) == ["ACME PHARMA"]  # 'NOT EMPLOYED' dropped
    assert _rows(conn, "pac")["BIG BANK PAC"]["amount"] == 7000 and _rows(conn, "pac")["BIG BANK PAC"]["n"] == 2
    assert set(_rows(conn, "pac")) == {"BIG BANK PAC"}  # own committee, ActBlue conduit and ORG rows dropped
    assert _rows(conn, "size")["$200-499"]["amount"] == 30 and len(_rows(conn, "size")) == 1
    assert _rows(conn, "leadership_pac")["TEST LEADERSHIP PAC"]["amount"] == 123
    assert _rows(conn, "outside_spend")["OUTSIDE GROUP"]["note"] == "oppose"
    total = conn.execute("SELECT * FROM rep_finance_totals WHERE rep=? AND cycle=2024", (REP["name"],)).fetchone()
    assert (total["receipts"], total["pac_contributions"], total["coverage_end"]) == (100.0, 10.0, "2024-12-31")


def test_refetch_replaces_rather_than_duplicates(conn):
    rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024, force=True)
    rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024, force=True)
    assert conn.execute("SELECT count(*) FROM rep_donors WHERE cycle=2024 AND kind='pac'").fetchone()[0] == 1


def test_weekly_gate(conn):
    rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024, force=True)
    assert rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024) == {"skipped": True}
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    conn.execute("UPDATE backfill_progress SET updated_at=? WHERE source='donors:fec'", (old,))
    assert "skipped" not in rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024)


def test_error_in_one_cycle_does_not_stop_others_and_hides_key(conn, monkeypatch):
    def flaky(path, **params):
        if path == "schedules/schedule_a/by_size/" and params.get("cycle") == 2022:
            raise requests.HTTPError("500 for url: https://x/?cycle=2022&api_key=SECRET123&a=1")
        return _fake_get(path, **params)
    monkeypatch.setattr(rep_donors, "_get", flaky)
    result = rep_donors.refresh_rep(conn, dict(REP), since_cycle=2022, force=True)
    assert result["cycles"] == 2 and len(result["errors"]) == 1 and "SECRET123" not in result["errors"][0]
    assert conn.execute("SELECT count(*) FROM backfill_progress WHERE source='donors:fec'").fetchone()[0] == 0  # retried next run


def test_sector_matching_is_word_prefixed():
    assert "securities/finance" in rep_donors.sector_for("Umpqua Bank")
    assert rep_donors.sector_for("Metalworks Union Hall") == ["labor"]  # 'meta' must not match 'Metalworks'
    assert rep_donors.sector_for("Fund for Oregon Kids") == []


def test_donor_context_is_labelled_fec_reported(conn):
    rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024, force=True)
    items = rep_donors.donor_context(conn, REP["name"], "pharma pricing bill")
    assert {i["label"] for i in items} == {"FEC-reported"} and "ACME PHARMA" in items[0]["text"]
    assert items[0]["url"].startswith("https://www.fec.gov/")
    assert rep_donors.donor_context(conn, REP["name"], "") == []
    assert rep_record.record_context(conn, REP["name"], "pharma")[0]["kind"] == "donor"


def test_sweep_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(rep_record, "SWEEP_STAMP", tmp_path / "last_sweep.txt")
    assert "missing" in rep_record.sweep_warning()
    (tmp_path / "last_sweep.txt").write_text("ok")
    assert rep_record.sweep_warning() is None
    assert "STALE" in rep_record.sweep_warning(now=(tmp_path / "last_sweep.txt").stat().st_mtime + 40 * 3600)


def test_conflicts_ignore_opposition_and_own_leadership_pac(conn, capsys, monkeypatch):
    rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024, force=True)
    conn.execute("INSERT INTO rep_donors (rep,cycle,kind,donor,amount,n,note,url,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (REP["name"], 2024, "outside_spend", "ANTI TECHNOLOGY GROUP", 500.0, 1, "oppose", "u", "now"))
    conn.execute("INSERT INTO rep_donors (rep,cycle,kind,donor,amount,n,note,url,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (REP["name"], 2024, "outside_spend", "PRO TECHNOLOGY GROUP", 400.0, 1, "support", "u", "now"))
    monkeypatch.setattr("src.committees.rep_committees", lambda: {REP["name"]: []})
    rep_donors.conflicts(conn, REP["name"])
    out = capsys.readouterr().out
    assert "PRO TECHNOLOGY GROUP" in out and "ANTI TECHNOLOGY GROUP" not in out and "TEST LEADERSHIP PAC" not in out


def test_donor_context_phrases_each_kind(conn):
    rep_donors.refresh_rep(conn, dict(REP), since_cycle=2024, force=True)
    text = rep_donors.donor_context(conn, REP["name"], "outside group", limit=1)[0]["text"]
    assert "independent expenditures filed as 'oppose'" in text and "gave" not in text
    assert "leadership PAC" in rep_donors.donor_context(conn, REP["name"], "leadership", limit=1)[0]["text"]
