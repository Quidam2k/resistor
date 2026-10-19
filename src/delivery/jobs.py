"""Background delivery jobs for the approval web UI.

Faxes take 30-60s (upload -> convert -> submit), so the approve endpoint
must not block. Each approval spawns a daemon thread; the UI polls
get_job() for status.

WEBUI_DRY_RUN=1 in the environment routes deliveries through dry_run so
the whole approve pipeline can be tested without sending anything.
"""

import os
import threading
import traceback
import uuid
from datetime import datetime

from src.db import get_letter_by_id, update_letter_status
from src.delivery.router import deliver_letter

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _dry_run_enabled() -> bool:
    return os.environ.get("WEBUI_DRY_RUN") == "1"


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def enqueue_delivery(letter_id: int) -> str:
    """Start delivering an approved letter in a background thread.

    The caller must have already claimed the letter (status 'approved')
    via db.claim_letter_for_approval — this function does not re-check.
    """
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "letter_id": letter_id,
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "result": None,
        }
    thread = threading.Thread(target=_deliver, args=(job_id, letter_id),
                              daemon=True)
    thread.start()
    return job_id


def _deliver(job_id: str, letter_id: int):
    dry_run = _dry_run_enabled()
    try:
        letter = get_letter_by_id(letter_id)
        result = deliver_letter(
            recipient_name=letter["recipient"],
            subject=letter["topic"],
            body=letter["body"],
            channel_override=letter["channel"],
            dry_run=dry_run,
            use_live=not dry_run,
        )
    except Exception as e:
        result = {"status": "error", "error": str(e),
                  "traceback": traceback.format_exc()}

    delivered = result.get("status") in ("sent", "dry_run", "test_created")
    if dry_run:
        # Leave the letter 'approved' — nothing actually went out.
        update_letter_status(letter_id, "approved",
                             {"delivery_result_json": result})
    elif delivered:
        update_letter_status(letter_id, "sent", {
            "sent_at": datetime.now().isoformat(),
            "delivery_result_json": result,
        })
    else:
        update_letter_status(letter_id, "failed",
                             {"delivery_result_json": result})

    with _jobs_lock:
        _jobs[job_id]["status"] = "done" if delivered else "failed"
        _jobs[job_id]["result"] = result
        _jobs[job_id]["finished_at"] = datetime.now().isoformat()
        _jobs[job_id]["dry_run"] = dry_run
