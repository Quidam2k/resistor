"""FOIA draft composition: unsent only, statutory cites only, precision guard."""

import uuid
from datetime import date

import pytest

from src.db import get_connection
from src.foia import compose, tracker

REQUESTER = {"name": "Test Person", "address": "1 Main St", "city": "Eugene",
             "state": "OR", "zip": "97401"}
DESCRIPTION = ("Correspondence between the Oregon State Office and Senator "
               "Wyden's office about the Eugene district timber sale schedule")
RANGE = ("2025-01-01", "2025-12-31")


@pytest.fixture(autouse=True)
def fresh_drafts():
    compose.ensure_drafts_table()
    conn = get_connection()
    conn.execute("DELETE FROM foia_drafts")  # scratch DB copy (conftest)
    conn.commit()
    conn.close()


def _agency():
    return f"Bureau of Land Management {uuid.uuid4().hex[:6]}"


def test_request_body_shape_and_saved_unsent():
    result = compose.draft_request("Department of Health and Human Services", DESCRIPTION,
                                   RANGE, requester=REQUESTER, today=date(2026, 10, 13))
    body = result["body"]
    assert body.startswith(compose.DRAFT_HEADER)
    assert "October 13, 2026" in body
    assert "5 U.S.C. 552(a)(3)(A)" in body
    assert "Date range: 2025-01-01 through 2025-12-31." in body
    assert "20 business days" in body
    assert "—" not in body
    assert compose.check_citations(body) == []

    drafts = compose.list_drafts()
    assert drafts[0]["id"] == result["draft_id"]
    assert drafts[0]["status"] == "draft_unsent"


def test_module_has_no_send_path():
    import inspect
    source = inspect.getsource(compose)
    for word in ("smtp", "requests.", "send_", "submit(", "UPDATE foia_drafts"):
        assert word not in source


@pytest.mark.parametrize("agency", [["EPA", "BLM"], "EPA; BLM", "EPA / BLM"])
def test_one_agency_per_request(agency):
    with pytest.raises(ValueError, match="one agency"):
        compose.draft_request(agency, DESCRIPTION, RANGE, requester=REQUESTER, save=False)


@pytest.mark.parametrize("desc", ["too short a description",
                                  "any and all records about timber sales in Oregon from every office please now"])
def test_vague_descriptions_refused(desc):
    with pytest.raises(ValueError, match="specific"):
        compose.draft_request(_agency(), desc, RANGE, requester=REQUESTER, save=False)


def test_long_range_refused_unless_allowed():
    with pytest.raises(ValueError):
        compose.draft_request(_agency(), DESCRIPTION, ("2020-01-01", "2025-01-01"),
                              requester=REQUESTER, save=False)
    compose.draft_request(_agency(), DESCRIPTION, ("2020-01-01", "2025-01-01"),
                          requester=REQUESTER, save=False, allow_long_range=True)


def test_daily_cap():
    for _ in range(compose.MAX_DRAFTS_PER_DAY):
        compose.draft_request(_agency(), DESCRIPTION, RANGE, requester=REQUESTER)
    with pytest.raises(ValueError, match="per day"):
        compose.draft_request(_agency(), DESCRIPTION, RANGE, requester=REQUESTER)


def test_check_citations_flags_case_law_and_unknown_cites():
    text = ("See Milner v. Department of the Navy, 562 U.S. 562 (2011), and "
            "5 U.S.C. 552(a)(8)(A). 5 U.S.C. 552(b)(6) is fine.")
    found = compose.check_citations(text)
    assert "552(a)(8)(A)" in found
    assert any("v. D" in f for f in found)
    assert "562 U.S. 562" in found
    assert "552(b)(6)" not in found


def test_fee_waiver_reason_quoted_with_placeholder():
    body = compose.draft_request(_agency(), DESCRIPTION, RANGE, requester=REQUESTER,
                                 fee_waiver_reason="Records will be published free online",
                                 save=False)["body"]
    assert '"Records will be published free online"' in body
    assert "552(a)(4)(A)(iii)" in body
    assert "fill in before sending" in body


def test_no_determination_appeal_requires_overdue():
    rid = tracker.create_request(_agency(), "timber", "BLM-26-1", received_date="2026-10-01")
    # Due 2026-10-30 (hand count in test_foia_tracker).
    with pytest.raises(ValueError, match="not overdue yet: due 2026-10-30"):
        compose.draft_appeal(rid, "no_determination", today=date(2026, 10, 30),
                             requester=REQUESTER, save=False)
    body = compose.draft_appeal(rid, "no_determination", today=date(2026, 11, 10),
                                requester=REQUESTER)["body"]
    assert body.startswith(compose.DRAFT_HEADER)
    assert "received 2026-10-01" in body
    assert "2026-10-30" in body
    assert "552(a)(6)(C)(i)" in body
    assert body.rstrip().endswith(tracker.DISCLAIMER)


def test_denial_appeal_leaves_reasons_placeholder():
    rid = tracker.create_request(_agency(), "timber", received_date="2026-10-01")
    with pytest.raises(ValueError):
        compose.draft_appeal(rid, "denial", requester=REQUESTER, save=False)
    tracker.add_event(rid, "determination", "2026-10-20", "denied")
    body = compose.draft_appeal(rid, "denial", today=date(2026, 10, 21),
                                requester=REQUESTER, save=False)["body"]
    assert "I appeal the denial dated 2026-10-20" in body
    assert "do not file until filled in" in body
    assert "Calculated appeal window closes 2027-01-18." in body
    assert compose.check_citations(body) == []
