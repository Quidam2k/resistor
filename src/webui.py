"""Approval gate web UI.

Claude queues letters (src/outbox.py); only a human click here can send.
Run:  python -m src.webui   (binds 127.0.0.1 only, port WEBUI_PORT or 5317)
"""

import json
import os

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from src.db import (claim_letter_for_approval, get_approval_history,
                    get_letter_by_id, get_pending_approval_letters, init_db,
                    update_letter_status)
from src.delivery import jobs
from src.delivery.router import get_delivery_info
from src.outbox import reject_letter

app = Flask(__name__)
# Pantheon mounts this read-only at /apps/resistor (#8363): trust ONLY the
# X-Forwarded-Prefix header so url_for() emits prefixed links; with no header
# (standalone on :5317) every URL is unchanged.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=0, x_host=0, x_port=0, x_prefix=1)


def _destination(recipient: str, channel: str) -> str:
    info = get_delivery_info(recipient)
    if channel == "fax":
        return info.get("fax_number", "?")
    if channel == "email":
        return info.get("address", "?")
    if channel == "web_form":
        return info.get("url", "?")
    if channel == "mail":
        addr = info.get("mail_address", {})
        return f"{addr.get('address_line1', '?')}, {addr.get('address_city', '')} {addr.get('address_state', '')}"
    return "?"


def _decorate(letter: dict) -> dict:
    letter["destination"] = _destination(letter["recipient"], letter["channel"])
    cents = letter.get("estimated_cost_cents")
    letter["cost_display"] = f"${cents / 100:.2f}" if cents else "free"
    if letter.get("delivery_result_json"):
        try:
            letter["delivery_result"] = json.loads(letter["delivery_result_json"])
        except (ValueError, TypeError):
            letter["delivery_result"] = None
    return letter


@app.route("/")
def index():
    letters = [_decorate(l) for l in get_pending_approval_letters()]
    return render_template("index.html", letters=letters,
                           dry_run=os.environ.get("WEBUI_DRY_RUN") == "1")


@app.route("/letter/<int:letter_id>")
def letter_detail(letter_id):
    letter = get_letter_by_id(letter_id)
    if not letter:
        abort(404)
    return render_template("letter.html", letter=_decorate(letter),
                           dry_run=os.environ.get("WEBUI_DRY_RUN") == "1")


@app.route("/history")
def history():
    letters = [_decorate(l) for l in get_approval_history(limit=100)]
    return render_template("history.html", letters=letters)


@app.route("/api/letter/<int:letter_id>/approve", methods=["POST"])
def approve(letter_id):
    letter = get_letter_by_id(letter_id)
    if not letter:
        return jsonify({"error": "not found"}), 404

    if not claim_letter_for_approval(letter_id):
        return jsonify({"error": "already_processed",
                        "message": f"Letter is already '{letter['status']}' — "
                                   "nothing sent by this click."}), 409

    if letter["channel"] == "web_form":
        info = get_delivery_info(letter["recipient"])
        result = {
            "status": "manual",
            "channel": "web_form",
            "url": info.get("url", ""),
            "instructions": "Copy the letter text and paste it into the web form.",
        }
        update_letter_status(letter_id, "approved",
                             {"delivery_result_json": result})
        return jsonify({"manual": True, "url": result["url"],
                        "instructions": result["instructions"],
                        "body": letter["body"]})

    job_id = jobs.enqueue_delivery(letter_id)
    return jsonify({"job_id": job_id})


@app.route("/api/delivery/<job_id>/status")
def delivery_status(job_id):
    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.route("/api/letter/<int:letter_id>/reject", methods=["POST"])
def reject(letter_id):
    letter = get_letter_by_id(letter_id)
    if not letter:
        return jsonify({"error": "not found"}), 404
    if letter["status"] != "pending_approval":
        return jsonify({"error": "already_processed",
                        "message": f"Letter is already '{letter['status']}'."}), 409
    note = (request.get_json(silent=True) or {}).get("note", "")
    reject_letter(letter_id, note)
    return jsonify({"ok": True})


def main():
    init_db()
    port = int(os.environ.get("WEBUI_PORT", "5317"))
    print(f"Resistor approval UI: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
