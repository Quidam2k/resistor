"""FOIA tracker tests with hand-counted deadline expectations.

Federal holidays used below:
* 2026-10-12 (Monday): Columbus Day.
* 2026-11-11 (Wednesday): Veterans Day.
* 2026-11-26 (Thursday): Thanksgiving.
Expected dates are literals, never calculated with src.foia.calendar.
"""

import json
from contextlib import closing
from datetime import date
from uuid import uuid4

import pytest

from src import db
from src.foia import tracker


@pytest.fixture
def request_factory():
    def create(received_date="2026-10-01"):
        suffix = uuid4().hex
        request_id = tracker.create_request(
            agency=f"pytest FOIA agency {suffix}",
            subject=f"pytest FOIA subject {suffix}",
            tracking_number=f"pytest-{suffix}",
            received_date=received_date,
            notes="Created by test_foia_tracker.py",
            include_inauguration=True,
        )
        assert isinstance(request_id, int)
        return request_id

    return create


def _deadlines(request_id, record=True):
    result = tracker.compute_deadlines(request_id, record=record)
    assert isinstance(result, dict)
    assert isinstance(result["warnings"], list)
    assert all(isinstance(warning, str) for warning in result["warnings"])
    assert result["disclaimer"] == tracker.DISCLAIMER

    for kind in ("initial_determination", "appeal_window", "appeal_decision"):
        if kind in result:
            deadline = result[kind]
            assert {"due", "method", "inputs", "statute"} <= deadline.keys()
            assert deadline["due"] is None or isinstance(deadline["due"], date)
            assert isinstance(deadline["method"], str)
            assert isinstance(deadline["inputs"], dict)
            assert isinstance(deadline["statute"], str)

    return result


def _status_for(request_id, today, record=True):
    rows = tracker.status(today=today, record=record)
    assert isinstance(rows, list)
    matches = [row for row in rows if row["request_id"] == request_id]
    assert len(matches) == 1
    row = matches[0]
    assert {
        "request_id",
        "agency",
        "subject",
        "tracking_number",
        "state",
        "due",
        "days_overdue",
        "message",
        "disclaimer",
    } <= row.keys()
    assert row["disclaimer"] == tracker.DISCLAIMER
    return row


def _audit_rows(request_id):
    with closing(db.get_connection()) as connection:
        return connection.execute(
            """
            SELECT due_date, inputs_json, method
            FROM foia_deadlines
            WHERE request_id = ? AND kind = ?
            ORDER BY due_date
            """,
            (request_id, "initial_determination"),
        ).fetchall()


def test_initial_determination_deadline(request_factory):
    request_id = request_factory()

    # Received Thursday, Oct 1; exclude receipt day and Monday, Oct 12.
    # Twenty business days: Oct 2,5,6,7,8,9,13,14,15,16,
    # 19,20,21,22,23,26,27,28,29,30. Oct 30 is Friday.
    expected = date(2026, 10, 30)
    assert date(2026, 10, 1).weekday() == 3
    assert expected.weekday() == 4

    result = _deadlines(request_id)

    assert result["initial_determination"]["due"] == expected
    assert "20 business days" in result["initial_determination"]["method"]
    assert "appeal_window" not in result
    assert "appeal_decision" not in result


def test_unusual_circumstances_adds_ten_business_days(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "unusual_circumstances", "2026-10-15")

    # The first 20 business days end Friday, Oct 30.
    # Ten more: Nov 2,3,4,5,6,9,10,12,13,16; skip Wednesday,
    # Nov 11 (Veterans Day). The 30th day is Monday, Nov 16.
    expected = date(2026, 11, 16)
    assert date(2026, 10, 30).weekday() == 4
    assert date(2026, 11, 11).weekday() == 2
    assert expected.weekday() == 0

    assert _deadlines(request_id)["initial_determination"]["due"] == expected


def test_clarification_response_resumes_remaining_sixteen_days(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "clarification_request", "2026-10-07")
    tracker.add_event(request_id, "clarification_response", "2026-10-14")

    # Receipt is Thursday, Oct 1. Through Wednesday, Oct 7, count
    # Oct 2,5,6,7 = 4 days, leaving 16. Resume AFTER Wednesday, Oct 14:
    # Oct 15,16,19,20,21,22,23,26,27,28,29,30, Nov 2,3,4,5.
    # The 16th remaining day is Thursday, Nov 5.
    expected = date(2026, 11, 5)
    assert date(2026, 10, 7).weekday() == 2
    assert date(2026, 10, 14).weekday() == 2
    assert expected.weekday() == 3

    assert _deadlines(request_id)["initial_determination"]["due"] == expected


def test_open_clarification_toll_is_paused(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "clarification_request", "2026-10-07")

    # Through Wednesday, Oct 7, Oct 2,5,6,7 consume 4 of 20 days.
    # Sixteen remain, but no response date exists to resume counting.
    assert date(2026, 10, 7).weekday() == 2
    assert date(2026, 10, 20).weekday() == 1

    deadline = _deadlines(request_id)["initial_determination"]
    assert deadline["due"] is None
    assert "16" in deadline["method"]

    row = _status_for(request_id, today=date(2026, 10, 20))
    assert row["state"] == "paused"


def test_second_clarification_request_does_not_toll(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "clarification_request", "2026-10-07")
    tracker.add_event(request_id, "clarification_response", "2026-10-14")

    # First toll uses Oct 2,5,6,7 = 4 days. After Wednesday, Oct 14,
    # the remaining 16 are Oct 15,16,19,20,21,22,23,26,27,28,29,30,
    # Nov 2,3,4,5, ending Thursday, Nov 5.
    expected = date(2026, 11, 5)
    assert expected.weekday() == 3
    assert _deadlines(request_id)["initial_determination"]["due"] == expected

    # A second request on Tuesday, Oct 20, even without a response,
    # cannot start another clarification toll.
    assert date(2026, 10, 20).weekday() == 1
    tracker.add_event(request_id, "clarification_request", "2026-10-20")

    result = _deadlines(request_id)
    assert result["initial_determination"]["due"] == expected
    assert any("552(a)(6)(A)(ii)(I)" in warning for warning in result["warnings"])


def test_fee_response_resumes_remaining_nineteen_days(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "fee_request", "2026-10-02")
    tracker.add_event(request_id, "fee_response", "2026-10-09")

    # Friday, Oct 2 consumes 1 day, leaving 19. Resume after Friday,
    # Oct 9; skip the weekend and Monday, Oct 12 (Columbus Day).
    # Nineteen days: Oct 13,14,15,16,19,20,21,22,23,26,27,28,29,30,
    # Nov 2,3,4,5,6. The deadline is Friday, Nov 6.
    expected = date(2026, 11, 6)
    assert date(2026, 10, 2).weekday() == 4
    assert date(2026, 10, 9).weekday() == 4
    assert expected.weekday() == 4

    assert _deadlines(request_id)["initial_determination"]["due"] == expected


def test_overdue_status_counts_business_days(request_factory):
    request_id = request_factory()

    # The 20-day deadline is Friday, Oct 30. Business days AFTER it
    # through Tuesday, Nov 10: Nov 2,3,4,5,6,9,10 = 7.
    assert date(2026, 10, 30).weekday() == 4
    assert date(2026, 11, 10).weekday() == 1

    row = _status_for(request_id, today=date(2026, 11, 10))

    assert row["state"] == "overdue"
    assert row["days_overdue"] == 7
    assert "7 business days past" in row["message"]
    assert "552(a)(6)(C)(i)" in row["message"]
    assert "[calculated - verify]" in row["message"]
    assert row["disclaimer"] == tracker.DISCLAIMER


def test_status_before_deadline_is_awaiting_determination(request_factory):
    request_id = request_factory()

    # Oct 2,5,6,7,8,9,13,14,15,16,19,20 = 12 business days
    # through Tuesday, Oct 20; the 20th is Friday, Oct 30.
    assert date(2026, 10, 20).weekday() == 1
    assert date(2026, 10, 30).weekday() == 4

    row = _status_for(request_id, today=date(2026, 10, 20))

    assert row["state"] == "awaiting_determination"
    assert row["days_overdue"] == 0


def test_denied_determination_opens_ninety_calendar_day_appeal_window(
    request_factory,
):
    request_id = request_factory()
    tracker.add_event(request_id, "determination", "2026-10-20", details="denied")

    # Exclude Tuesday, Oct 20. Oct 21-31 = 11 calendar days;
    # + November's 30 = 41; + December's 31 = 72;
    # + January's first 18 = 90. Jan 18, 2027 is Monday.
    # This is a calendar-day window, including weekends and holidays.
    expected = date(2027, 1, 18)
    assert date(2026, 10, 20).weekday() == 1
    assert expected.weekday() == 0

    result = _deadlines(request_id)
    assert result["appeal_window"]["due"] == expected
    assert "appeal_decision" not in result


def test_granted_determination_does_not_open_appeal_window(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "determination", "2026-10-20", details="granted")

    assert "appeal_window" not in _deadlines(request_id)


def test_appeal_decision_deadline(request_factory):
    request_id = request_factory()
    tracker.add_event(request_id, "determination", "2026-10-20", details="denied")
    tracker.add_event(request_id, "appeal_filed", "2026-11-02")

    # Exclude Monday, Nov 2. Twenty business days:
    # Nov 3,4,5,6,9,10,12,13,16,17,18,19,20,23,24,25,27,30,
    # Dec 1,2. Skip Wednesday, Nov 11 and Thursday, Nov 26;
    # Friday, Nov 27 counts. Dec 2 is Wednesday.
    expected = date(2026, 12, 2)
    assert date(2026, 11, 2).weekday() == 0
    assert date(2026, 11, 11).weekday() == 2
    assert date(2026, 11, 26).weekday() == 3
    assert date(2026, 11, 27).weekday() == 4
    assert expected.weekday() == 2

    assert _deadlines(request_id)["appeal_decision"]["due"] == expected


def test_deadline_audit_records_only_changes(request_factory):
    request_id = request_factory()

    # Twenty days: Oct 2,5,6,7,8,9,13,14,15,16,19,20,21,22,23,
    # 26,27,28,29,30, ending Friday, Oct 30.
    assert date(2026, 10, 30).weekday() == 4
    _deadlines(request_id)
    _deadlines(request_id)

    rows = _audit_rows(request_id)
    assert len(rows) == 1
    assert rows[0]["due_date"] == "2026-10-30"
    inputs = json.loads(rows[0]["inputs_json"])
    assert isinstance(inputs, dict)
    assert "2026-10-01" in json.dumps(inputs)
    assert "20 business days" in rows[0]["method"]

    tracker.add_event(request_id, "unusual_circumstances", "2026-10-15")

    # Add Nov 2,3,4,5,6,9,10,12,13,16 = 10 business days,
    # skipping Wednesday, Nov 11; finish Monday, Nov 16.
    assert date(2026, 11, 16).weekday() == 0
    _deadlines(request_id)

    rows = _audit_rows(request_id)
    assert len(rows) == 2
    assert [row["due_date"] for row in rows] == ["2026-10-30", "2026-11-16"]

    _deadlines(request_id)
    assert len(_audit_rows(request_id)) == 2


def test_record_false_does_not_write_deadline_audit(request_factory):
    request_id = request_factory()
    assert _audit_rows(request_id) == []

    # Oct 2,5,6,7,8,9,13,14,15,16,19,20,21,22,23,26,27,28,29,30
    # are 20 business days; the deadline is Friday, Oct 30.
    expected = date(2026, 10, 30)
    assert expected.weekday() == 4
    assert (
        _deadlines(request_id, record=False)["initial_determination"]["due"]
        == expected
    )
    _status_for(request_id, today=date(2026, 10, 20), record=False)

    assert _audit_rows(request_id) == []


def test_unknown_event_type_raises_value_error(request_factory):
    request_id = request_factory()

    with pytest.raises(ValueError):
        tracker.add_event(request_id, "not_a_valid_foia_event", "2026-10-20")


@pytest.mark.parametrize("details", [None, "", "approved", "DENIED"])
def test_invalid_determination_details_raise_value_error(request_factory, details):
    request_id = request_factory()

    with pytest.raises(ValueError):
        tracker.add_event(
            request_id,
            "determination",
            "2026-10-20",
            details=details,
        )


def test_draft_request_is_excluded_from_status(request_factory):
    request_id = request_factory(received_date=None)

    rows = tracker.status(today=date(2026, 10, 20))

    assert isinstance(rows, list)
    assert [row for row in rows if row["request_id"] == request_id] == []
