"""Rehearse the voice loop end to end on a SCRATCH copy of resist.db.

Fails closed unless the environment proves nothing can go out:
  WEBUI_DRY_RUN=1, RESIST_DATA_DIR pointing outside the real data dir, and
  LOB_API_KEY / NOTIFYRE_API_KEY / SMTP_USER / SMTP_PASSWORD present but EMPTY
  (present-but-empty wins over .env, so the live senders have no creds).

The Approve step is a SIMULATED click through the web UI's own endpoint.
In real use that click is Todd's and only Todd's.

    WEBUI_DRY_RUN=1 RESIST_DATA_DIR=<scratch> LOB_API_KEY= NOTIFYRE_API_KEY= \
    SMTP_USER= SMTP_PASSWORD= python scripts/rehearse_voice_loop.py \
        --pick 1 --recipient "Val Hoyle" --channel mail --body-file letter.txt
"""

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = PROJECT_ROOT / "data" / "resist.db"
BLANKED = ("LOB_API_KEY", "NOTIFYRE_API_KEY", "SMTP_USER", "SMTP_PASSWORD")
sys.path.insert(0, str(PROJECT_ROOT))


def fail_closed():
    problems = []
    if os.environ.get("WEBUI_DRY_RUN") != "1":
        problems.append("WEBUI_DRY_RUN must be 1")
    scratch = os.environ.get("RESIST_DATA_DIR")
    if not scratch:
        problems.append("RESIST_DATA_DIR must point at a scratch dir")
    elif Path(scratch).resolve() == (PROJECT_ROOT / "data").resolve():
        problems.append("RESIST_DATA_DIR is the REAL data dir")
    for key in BLANKED:
        if os.environ.get(key, None) != "":
            problems.append(f"{key} must be set to empty")
    if problems:
        sys.exit("REFUSING TO RUN: " + "; ".join(problems))
    return Path(scratch)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def guard_network():
    """Belt and braces: the only HTTP POST allowed is a Lob TEST-key letter."""
    import requests
    real_post = requests.post

    def guarded(url, *a, **kw):
        key = (kw.get("auth") or ("",))[0]
        if not url.startswith("https://api.lob.com/") or not key.startswith("test_"):
            raise RuntimeError(f"BLOCKED non-test POST to {url}")
        print(f"    [network] Lob TEST-key POST {url} (free, never printed)")
        return real_post(url, *a, **kw)
    requests.post = guarded


def main():
    scratch = fail_closed()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", type=int, required=True)
    ap.add_argument("--recipient")
    ap.add_argument("--channel")
    ap.add_argument("--body-file", required=True)
    args = ap.parse_args()

    live_before = sha(LIVE_DB)
    scratch.mkdir(parents=True, exist_ok=True)
    scratch_db = scratch / "resist.db"
    if scratch_db.exists():
        scratch_db.unlink()
    src = sqlite3.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(scratch_db)
    src.backup(dst)
    src.close(); dst.close()
    print(f"live resist.db sha256 {live_before} ; scratch copy at {scratch_db}")

    guard_network()
    from src import interpreter, db  # imported after env checks
    from src import webui
    assert db.DB_PATH.resolve() == scratch_db.resolve(), "DB not redirected"

    def step(title, argv):
        print(f"\n>>> {title}: python -m src.interpreter {' '.join(argv)}")
        return interpreter.main(argv)

    step("LIST", ["actions"])
    pick = [str(args.pick)] + (["--recipient", args.recipient] if args.recipient else [])
    step("PICK", ["pick"] + pick)
    draft = ["draft"] + pick + ["--body-file", args.body_file] + \
        (["--channel", args.channel] if args.channel else [])
    md = step("DRAFT", draft)
    if not md:
        sys.exit("draft failed lint")
    letter_id = step("QUEUE", ["queue", str(md)])

    print(f"\n>>> APPROVE: SIMULATED click on /api/letter/{letter_id}/approve "
          "(rehearsal only; in real use this is Todd's click)")
    client = webui.app.test_client()
    resp = client.post(f"/api/letter/{letter_id}/approve").get_json()
    print(f"    ui replied: {resp}")
    if resp.get("job_id"):
        for _ in range(60):
            job = client.get(f"/api/delivery/{resp['job_id']}/status").get_json()
            if job["status"] != "running":
                break
            time.sleep(0.5)
        result = job["result"] or {}
        print(f"    job {job['status']} dry_run={job.get('dry_run')} "
              f"delivery status={result.get('status')} mode={result.get('mode')}")
    step("STATUS", ["status", str(letter_id)])
    step("LOG", ["log", "--notes", "rehearsal on scratch copy"])

    # Rider: nothing rehearsed stays queued, even in the scratch copy.
    conn = db.get_connection()
    conn.execute("DELETE FROM letters WHERE id=?", (letter_id,))
    conn.commit(); conn.close()
    print(f"\ncleaned rehearsal letter #{letter_id} out of the scratch copy")

    live_after = sha(LIVE_DB)
    print(f"live resist.db sha256 before {live_before} after {live_after} "
          f"-> {'UNCHANGED' if live_before == live_after else 'CHANGED!'}")
    if live_before != live_after:
        sys.exit(1)


if __name__ == "__main__":
    main()
