"""#9454: approving a web_form letter never reaches a sender.

The approve route marks it approved and hands back paste instructions; it
must never enqueue a delivery (fax, Lob or SMTP), even with the live senders.
"""
import json

from src import db, webui
from src.delivery import jobs
from src.outbox import queue_letter


def test_web_form_approve_never_enqueues_delivery(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("web_form approve reached a sender")

    monkeypatch.setattr(jobs, "enqueue_delivery", boom)
    monkeypatch.setattr(jobs, "deliver_letter", boom, raising=False)
    monkeypatch.setattr(jobs, "_dry_run_enabled", lambda: False)  # as if live

    letter_id = queue_letter("TEST #9454 do not send", "test", "Todd Walker 97402",
                             channel="web_form")
    try:
        client = webui.app.test_client()
        resp = client.post(f"/api/letter/{letter_id}/approve")
        assert resp.status_code == 200 and resp.get_json()["manual"] is True
        row = db.get_letter_by_id(letter_id)
        assert row["status"] == "approved"
        result = row["delivery_result_json"]
        result = json.loads(result) if isinstance(result, str) else result
        assert result["channel"] == "web_form" and result["status"] == "manual"
        assert client.post(f"/api/letter/{letter_id}/approve").status_code == 409
    finally:
        conn = db.get_connection()
        conn.execute("DELETE FROM letters WHERE id=?", (letter_id,))
        conn.commit()
        conn.close()
