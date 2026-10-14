"""Committee roster + leverage-window tests against fixtures (no network)."""

import json
from datetime import date
from pathlib import Path

import pytest

from src import committees
from src.congress_api import MEMBER_IDS

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_get(url, params=None, ttl_seconds=0, as_text=False):
    name = url.rsplit("/", 1)[-1]
    if name.endswith(".yaml") or name.endswith(".xml"):
        file = "senate_hearings.xml" if name == "hearings.xml" else name
        return (FIXTURES / file).read_text(encoding="utf-8")
    if name == "house":
        return json.loads((FIXTURES / "house_meetings_list.json").read_text(encoding="utf-8"))
    return json.loads((FIXTURES / f"house_meeting_{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
    monkeypatch.setattr(committees, "cached_get", _fake_get)


def test_hoyle_bioguide_is_hoyle_not_hageman():
    # H001096 is Harriet Hageman (WY); Val Hoyle is H001094 (fixed 2026-10-13).
    assert MEMBER_IDS["Val Hoyle"]["bioguide"] == "H001094"


def test_normalize_code():
    assert committees.normalize_code("hsii13") == "HSII13"
    assert committees.normalize_code("hlzs00") == "HLZS"
    assert committees.normalize_code("SSFI00") == "SSFI"
    assert committees.normalize_code(None) == ""


def test_rep_committees_maps_by_bioguide():
    seats = committees.rep_committees()
    hoyle = {c["code"]: c for c in seats["Val Hoyle"]}
    assert set(hoyle) == {"HSII", "HSII13", "HSPW"}
    assert hoyle["HSII13"]["title"] == "Ranking Member"
    assert "Water" in hoyle["HSII13"]["name"]
    assert {c["code"] for c in seats["Ron Wyden"]} == {"SSFI", "SSFI11"}


def test_senate_meetings_skip_past_and_placeholder():
    meetings = committees.senate_meetings(today=date(2026, 10, 13))
    assert [(m["date"], m["codes"]) for m in meetings] == [
        ("2026-10-20", ["SSFI"]), ("2026-10-15", ["SSAP17"])]


def test_house_meetings_skip_past():
    meetings = committees.house_meetings(today=date(2026, 10, 13))
    assert len(meetings) == 1
    assert meetings[0]["codes"] == ["HSII13"]
    assert meetings[0]["date"] == "2026-10-21"


def test_leverage_windows_match_reps_soonest_first():
    windows = committees.leverage_windows(today=date(2026, 10, 13))
    got = [(w["date"], w["rep"], w["committee_code"]) for w in windows]
    assert got == [
        ("2026-10-15", "Jeff Merkley", "SSAP17"),
        ("2026-10-20", "Ron Wyden", "SSFI"),
        ("2026-10-21", "Val Hoyle", "HSII13"),
    ]
    line = committees.format_window(windows[0])
    assert "Jeff Merkley (Ranking Member)" in line
    assert "markup" in line


def test_house_meeting_not_matched_to_senators():
    windows = committees.leverage_windows(today=date(2026, 10, 13))
    assert all(w["rep"] == "Val Hoyle" for w in windows if w["chamber"] == "House")
