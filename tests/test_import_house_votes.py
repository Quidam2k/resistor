"""House roll-call import for Hoyle (#8371): parse by bioguide, dry-run writes nothing."""

import hashlib

import pytest

from src import db, import_votes

HOYLE = "H001094"


def _members(roll, cast=None):
    results = [{"bioguideID": "X000001", "voteCast": "Nay"}]
    if cast:
        results.append({"bioguideID": HOYLE, "voteCast": cast})
    return {"houseRollCallVoteMemberVotes": {
        "rollCallNumber": roll, "legislationType": "HR", "legislationNumber": "3424",
        "voteQuestion": "On Passage", "startDate": "2025-09-08T18:56:00-04:00",
        "sourceDataURL": f"https://clerk.house.gov/evs/2025/roll{roll}.xml",
        "results": results}}


@pytest.fixture
def fake_api(monkeypatch, tmp_path):
    monkeypatch.setattr(import_votes, "HOUSE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(import_votes, "PACE_SECONDS", 0)

    def fake(endpoint, params=None):
        parts = endpoint.split("/")
        if len(parts) == 3:  # list page
            votes = [{"rollCallNumber": 1}, {"rollCallNumber": 2}] if parts[2] == "1" else []
            return {"houseRollCallVotes": votes if params["offset"] == 0 else [],
                    "pagination": {}}
        return _members(int(parts[3]), "Yea" if parts[3] == "1" else None)
    monkeypatch.setattr(import_votes, "_congress_get", fake)


def _hash():
    conn = db.get_connection()
    rows = conn.execute("SELECT * FROM voting_records ORDER BY id").fetchall()
    conn.close()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


def test_fetch_picks_hoyle_by_bioguide_and_skips_absences(fake_api):
    rows = import_votes.fetch_house_votes("Val Hoyle")
    assert rows == [{
        "representative": "Val Hoyle", "vote_date": "2025-09-08T18:56:00-04:00",
        "bill_id": "HR.3424", "bill_title": "On Passage", "vote": "Yea",
        "chamber": "House", "session": "1",
        "source_url": "https://clerk.house.gov/evs/2025/roll1.xml"}]


def test_dry_run_counts_but_writes_nothing(fake_api):
    db.init_db()
    before = _hash()
    result = import_votes.main(["house-dryrun", "Val Hoyle"])
    assert result["applied"] is False and result["insert"] == 1
    assert _hash() == before


def test_apply_refuses_empty_import():
    with pytest.raises(ValueError):
        import_votes.replace_with_house_votes("Val Hoyle", [], apply=True)


def test_apply_is_one_transaction_and_idempotent(fake_api):
    rows = import_votes.fetch_house_votes("Val Hoyle")
    for _ in range(2):
        assert import_votes.replace_with_house_votes("Val Hoyle", rows, apply=True)["applied"]
    conn = db.get_connection()
    got = conn.execute("SELECT chamber, COUNT(*) FROM voting_records "
                       "WHERE representative='Val Hoyle' GROUP BY chamber").fetchall()
    conn.close()
    assert [tuple(r) for r in got] == [("House", 1)]


def test_apply_rolls_back_on_insert_failure(fake_api):
    before = _hash()
    with pytest.raises(Exception):
        import_votes.replace_with_house_votes("Val Hoyle", [{"bad": 1}], apply=True)
    assert _hash() == before
