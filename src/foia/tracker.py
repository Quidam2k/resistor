"""Passive FOIA deadline calculations to verify against agency letters."""

import argparse
import json
from datetime import date, timedelta

from src.db import get_connection
from src.foia.calendar import add_business_days, business_days_between

DISCLAIMER = (
    "Calculated estimate only - verify against the agency's own "
    "acknowledgment/determination letter and 5 U.S.C. 552(a)(6). "
    "Not legal advice."
)

EVENT_TYPES = (
    "received", "clarification_request", "clarification_response",
    "fee_request", "fee_response", "unusual_circumstances",
    "determination", "appeal_filed", "appeal_decision", "note",
)
DETERMINATIONS = ("granted", "partial", "denied", "no_records", "fee_denial")
_ADVERSE = ("partial", "denied", "no_records", "fee_denial")
_INITIAL_STATUTE = "5 U.S.C. 552(a)(6)(A)(i)"
_APPEAL_WINDOW_STATUTE = "5 U.S.C. 552(a)(6)(A)(i)(III)(aa)"
_APPEAL_DECISION_STATUTE = "5 U.S.C. 552(a)(6)(A)(ii)"


def ensure_tables():
    """Create only the FOIA tables."""
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS foia_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agency TEXT NOT NULL,
                subject TEXT NOT NULL,
                tracking_number TEXT,
                received_date TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                include_inauguration INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS foia_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL REFERENCES foia_requests(id),
                event_type TEXT NOT NULL,
                event_date TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS foia_deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL REFERENCES foia_requests(id),
                kind TEXT NOT NULL,
                due_date TEXT,
                inputs_json TEXT NOT NULL,
                method TEXT NOT NULL,
                computed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
    finally:
        conn.close()


def _parse_date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("date must be an ISO YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must be an ISO YYYY-MM-DD string") from None
    if parsed.isoformat() != value:
        raise ValueError("date must be an ISO YYYY-MM-DD string")
    return parsed


def create_request(agency: str, subject: str, tracking_number: str = None,
                   received_date: str = None, notes: str = None,
                   include_inauguration: bool = True) -> int:
    ensure_tables()
    if received_date is not None:
        _parse_date(received_date)
    request_status = "filed" if received_date is not None else "draft"
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO foia_requests "
                "(agency, subject, tracking_number, received_date, status, "
                "notes, include_inauguration) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agency, subject, tracking_number, received_date,
                 request_status, notes, int(bool(include_inauguration)))
            )
            request_id = cursor.lastrowid
            if received_date is not None:
                conn.execute(
                    "INSERT INTO foia_events "
                    "(request_id, event_type, event_date) VALUES (?, 'received', ?)",
                    (request_id, received_date)
                )
        return request_id
    finally:
        conn.close()


def add_event(request_id: int, event_type: str, event_date: str,
              details: str = None) -> int:
    ensure_tables()
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    _parse_date(event_date)
    if event_type == "determination" and details not in DETERMINATIONS:
        raise ValueError(f"invalid determination: {details}")

    conn = get_connection()
    try:
        with conn:
            row = conn.execute(
                "SELECT id FROM foia_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"request not found: {request_id}")
            cursor = conn.execute(
                "INSERT INTO foia_events "
                "(request_id, event_type, event_date, details) VALUES (?, ?, ?, ?)",
                (request_id, event_type, event_date, details)
            )
            event_id = cursor.lastrowid
            if event_type == "received":
                conn.execute(
                    "UPDATE foia_requests SET received_date=?, status='filed' "
                    "WHERE id=?", (event_date, request_id)
                )
            else:
                request_status = {
                    "determination": "determined",
                    "appeal_filed": "appealed",
                    "appeal_decision": "appeal_decided",
                }.get(event_type)
                if request_status:
                    conn.execute(
                        "UPDATE foia_requests SET status=? WHERE id=?",
                        (request_status, request_id)
                    )
        return event_id
    finally:
        conn.close()


def _read_request(conn, request_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM foia_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"request not found: {request_id}")
    request = dict(row)
    events = conn.execute(
        "SELECT * FROM foia_events WHERE request_id=? ORDER BY event_date, id",
        (request_id,)
    ).fetchall()
    request["events"] = [dict(event) for event in events]
    return request


def get_request(request_id: int) -> dict:
    ensure_tables()
    conn = get_connection()
    try:
        return _read_request(conn, request_id)
    finally:
        conn.close()


def _last_event(events: list[dict], event_type: str) -> dict | None:
    return next(
        (event for event in reversed(events)
         if event["event_type"] == event_type),
        None
    )


def _toll_windows(events: list[dict]) -> tuple[list[dict], list[str]]:
    windows = []
    warnings = []
    clarification_seen = False
    responses = {
        "clarification_request": "clarification_response",
        "fee_request": "fee_response",
    }
    for index, event in enumerate(events):
        event_type = event["event_type"]
        if event_type not in responses:
            continue
        if event_type == "clarification_request":
            if clarification_seen:
                warnings.append(
                    "additional clarification request does not toll "
                    "(552(a)(6)(A)(ii)(I))"
                )
                continue
            clarification_seen = True
        response = next(
            (candidate for candidate in events[index + 1:]
             if candidate["event_type"] == responses[event_type]),
            None
        )
        windows.append({
            "start": event["event_date"],
            "end": response["event_date"] if response else None,
            "type": event_type,
        })
    return windows, warnings


def _initial_deadline(inputs: dict) -> dict:
    statute = inputs["statute"]
    if inputs["received_date"] is None:
        return {
            "due": None, "method": "not yet received",
            "inputs": inputs, "statute": statute,
        }

    include_inauguration = inputs["include_inauguration"]
    cursor = _parse_date(inputs["received_date"])
    remaining = 20
    steps = [f"received {cursor} + 20 business days ({statute})"]
    if inputs["unusual_circumstances"]:
        remaining += 10
        steps.append(
            "+10 business days unusual circumstances (552(a)(6)(B)(i))"
        )

    for window in inputs["toll_windows"]:
        start = _parse_date(window["start"])
        # Overlapping windows and pre-receipt events cannot rewind the clock.
        used = max(0, business_days_between(
            cursor, start, include_inauguration=include_inauguration
        ))
        if used >= remaining:
            steps.append(f"deadline reached before toll starting {start}")
            break
        remaining -= used
        if window["end"] is None:
            steps.append(
                f"paused since {start} awaiting requester response; "
                f"{remaining} business days remain"
            )
            return {
                "due": None, "method": "; ".join(steps),
                "inputs": inputs, "statute": statute,
            }
        end = _parse_date(window["end"])
        steps.append(
            f"tolled {start}..{end} ({window['type']}; "
            f"{used} business days used before toll; "
            f"{remaining} business days remain)"
        )
        cursor = max(cursor, end)

    due = add_business_days(
        cursor, remaining, include_inauguration=include_inauguration
    )
    steps.append(f"{cursor} + {remaining} business days => {due}")
    return {
        "due": due, "method": "; ".join(steps),
        "inputs": inputs, "statute": statute,
    }


def _calculate(request: dict) -> dict:
    events = request["events"]
    windows, warnings = _toll_windows(events)
    base = {
        "received_date": request["received_date"],
        "toll_windows": windows,
        "unusual_circumstances": any(
            event["event_type"] == "unusual_circumstances" for event in events
        ),
        "include_inauguration": bool(request["include_inauguration"]),
    }
    result = {
        "initial_determination": _initial_deadline({
            **base, "statute": _INITIAL_STATUTE,
        }),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }

    determination = _last_event(events, "determination")
    if determination and determination["details"] in _ADVERSE:
        determined = _parse_date(determination["event_date"])
        due = determined + timedelta(days=90)
        inputs = {
            **base,
            "determination_date": determination["event_date"],
            "determination": determination["details"],
            "statute": _APPEAL_WINDOW_STATUTE,
        }
        result["appeal_window"] = {
            "due": due,
            "method": (
                f"determination {determined} + 90 calendar days "
                f"({_APPEAL_WINDOW_STATUTE}) => {due}; "
                "statute guarantees not less than 90 days; "
                "the agency letter may allow more"
            ),
            "inputs": inputs,
            "statute": _APPEAL_WINDOW_STATUTE,
        }

    appeal = _last_event(events, "appeal_filed")
    if appeal:
        filed = _parse_date(appeal["event_date"])
        due = add_business_days(
            filed, 20, include_inauguration=base["include_inauguration"]
        )
        inputs = {
            **base,
            "appeal_filed_date": appeal["event_date"],
            "statute": _APPEAL_DECISION_STATUTE,
        }
        result["appeal_decision"] = {
            "due": due,
            "method": (
                f"appeal filed {filed} + 20 business days "
                f"({_APPEAL_DECISION_STATUTE}) => {due}; "
                "uses the filing date as proxy for agency receipt"
            ),
            "inputs": inputs,
            "statute": _APPEAL_DECISION_STATUTE,
        }
    return result


def _record_deadlines(conn, request_id: int, deadlines: dict):
    for kind, deadline in deadlines.items():
        if kind in ("warnings", "disclaimer"):
            continue
        due_date = deadline["due"].isoformat() if deadline["due"] else None
        inputs_json = json.dumps(deadline["inputs"], sort_keys=True)
        latest = conn.execute(
            "SELECT due_date, inputs_json FROM foia_deadlines "
            "WHERE request_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (request_id, kind)
        ).fetchone()
        if latest is not None:
            if (latest["due_date"] == due_date
                    and latest["inputs_json"] == inputs_json):
                continue
        conn.execute(
            "INSERT INTO foia_deadlines "
            "(request_id, kind, due_date, inputs_json, method) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, kind, due_date, inputs_json, deadline["method"])
        )


def compute_deadlines(request_id: int, record: bool = True) -> dict:
    ensure_tables()
    conn = get_connection()
    try:
        if record:
            conn.execute("BEGIN IMMEDIATE")
        request = _read_request(conn, request_id)
        deadlines = _calculate(request)
        if record:
            _record_deadlines(conn, request_id, deadlines)
            conn.commit()
        return deadlines
    finally:
        conn.close()


def _request_status(request: dict, deadlines: dict, today: date) -> dict:
    agency = request["agency"]
    reference = request["tracking_number"] or f"#{request['id']}"
    include_inauguration = bool(request["include_inauguration"])
    events = request["events"]
    determination = _last_event(events, "determination")
    due = None
    days_overdue = 0

    if (request["status"] == "appeal_decided"
            or _last_event(events, "appeal_decision")):
        state = "done"
        message = f"{agency} appeal decided on request {reference}."
    elif "appeal_decision" in deadlines:
        due = deadlines["appeal_decision"]["due"]
        if today > due:
            state = "appeal_overdue"
        else:
            state = "awaiting_appeal_decision"
        message = ""
    elif determination and determination["details"] == "granted":
        state = "done"
        message = f"{agency} granted request {reference}."
    elif "appeal_window" in deadlines:
        due = deadlines["appeal_window"]["due"]
        if today <= due:
            state = "appeal_window_open"
            message = (
                f"Appeal window on request {reference} closes {due} "
                "(90 calendar days from determination)."
            )
        else:
            state = "appeal_window_closed"
            message = (
                f"Calculated appeal window on request {reference} closed {due} "
                "(90 calendar days from determination); "
                "the agency letter may allow more."
            )
    else:
        initial = deadlines["initial_determination"]
        due = initial["due"]
        if due is None:
            if request["received_date"] is None:
                state = "awaiting_determination"
                message = (
                    f"{agency} receipt not yet logged on request {reference}; "
                    "determination deadline not yet calculated."
                )
            else:
                state = "paused"
                message = (
                    f"{agency} clock paused awaiting your response "
                    f"on request {reference}."
                )
        elif today > due:
            state = "overdue"
            message = ""
        else:
            state = "awaiting_determination"
            message = ""

    if due is not None and today > due:
        days_overdue = business_days_between(
            due, today, include_inauguration=include_inauguration
        )

    if state in ("overdue", "appeal_overdue"):
        deadline_name = (
            "statutory deadline" if state == "overdue"
            else "statutory appeal decision deadline"
        )
        message = (
            f"{agency} is {days_overdue} business days past the "
            f"{deadline_name} ({due}) on request {reference}. "
            "You may file an appeal or treat administrative remedies as "
            "constructively exhausted (5 U.S.C. 552(a)(6)(C)(i))."
        )
    elif state in ("awaiting_determination", "awaiting_appeal_decision"):
        if due is not None:
            left = business_days_between(
                today, due, include_inauguration=include_inauguration
            )
            action = (
                "determination" if state == "awaiting_determination"
                else "appeal decision"
            )
            message = (
                f"{agency} {action} due {due} ({left} business days left) "
                f"on request {reference}."
            )

    return {
        "request_id": request["id"],
        "agency": agency,
        "subject": request["subject"],
        "tracking_number": request["tracking_number"],
        "state": state,
        "due": due,
        "days_overdue": days_overdue,
        "message": message + " [calculated - verify]",
        "disclaimer": DISCLAIMER,
    }


def status(today: date | None = None, record: bool = True) -> list[dict]:
    ensure_tables()
    if today is None:
        today = date.today()
    conn = get_connection()
    try:
        if record:
            conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM foia_requests "
            "WHERE status NOT IN ('draft', 'closed') ORDER BY id"
        ).fetchall()
        results = []
        for row in rows:
            request = _read_request(conn, row["id"])
            deadlines = _calculate(request)
            if record:
                _record_deadlines(conn, row["id"], deadlines)
            results.append(_request_status(request, deadlines, today))
        if record:
            conn.commit()
        return results
    finally:
        conn.close()


def _ascii(value) -> str:
    return str(value).encode("ascii", errors="backslashreplace").decode("ascii")


def _main():
    parser = argparse.ArgumentParser(
        description="Passive FOIA deadline calculations to verify."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show active request deadline estimates")
    show = commands.add_parser("show", help="Show a request and its calculations")
    show.add_argument("id", type=int)
    args = parser.parse_args()

    try:
        if args.command == "status":
            for item in status():
                print(_ascii(item["message"]))
        else:
            request = get_request(args.id)
            deadlines = compute_deadlines(args.id)
            fields = {
                key: value for key, value in request.items() if key != "events"
            }
            print(json.dumps(fields, indent=2, sort_keys=True, ensure_ascii=True))
            print("Events:")
            for event in request["events"]:
                print(json.dumps(event, sort_keys=True, ensure_ascii=True))
            print("Calculated deadlines:")
            for kind, deadline in deadlines.items():
                if kind not in ("warnings", "disclaimer"):
                    print(_ascii(f"{kind}: {deadline['method']}"))
            for warning in deadlines["warnings"]:
                print(_ascii(f"Warning: {warning}"))
        print(DISCLAIMER)
    except ValueError as exc:
        parser.error(_ascii(exc))


if __name__ == "__main__":
    _main()

