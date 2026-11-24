"""The voice loop can queue, but nothing it touches can send for real."""

import ast
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src import db, interpreter, webui
from src.config import get_env, load_user
from src.delivery import mail_sender, router

INTERP_SRC = Path(interpreter.__file__).read_text(encoding="utf-8")
SEND_NAMES = {"deliver_letter", "send_fax", "send_email", "send_letter",
              "enqueue_delivery", "claim_letter_for_approval", "complete_goal"}


def _table_hash(table: str) -> str:
    conn = db.get_connection()
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    conn.close()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


def _body(extra: str = "") -> str:
    u = load_user()
    return (f"{u['name']}\n{u['address']}\n{u['city']}, {u['state']} {u['zip']}\n\n"
            f"Dear Congresswoman Hoyle,\n\nPlease cosponsor H.R. 6589. {extra}\n\n"
            f"Sincerely,\n{u['name']}")


def _sourced(body: str) -> str:
    return body.replace("H.R. 6589.", "H.R. 6589 [1].", 1) + (
        "\n\nSources\n[1] Congress.gov: congress.gov/bill/119th-congress/house-bill/6589")


@pytest.fixture
def recorder(monkeypatch):
    """Replace every sender with a recorder; any real HTTP POST fails the test."""
    calls = []

    def rec(name):
        def fake(*a, **kw):
            calls.append((name, kw))
            return {"status": "dry_run" if kw.get("dry_run", True) else "test_created",
                    "mode": "live" if kw.get("use_live") else "test"}
        return fake

    monkeypatch.setattr(router, "send_fax", rec("fax"))
    monkeypatch.setattr(router, "send_email", rec("email"))
    monkeypatch.setattr(router, "send_letter", rec("lob"))
    monkeypatch.setattr(mail_sender, "send_letter", rec("lob_render"))
    import requests

    def no_post(*a, **kw):
        raise AssertionError(f"real HTTP POST attempted: {a[:1]}")
    monkeypatch.setattr(requests, "post", no_post)
    return calls


def test_runs_against_scratch_db():
    assert "resist-test-" in str(db.DB_PATH)


def test_interpreter_imports_no_send_path():
    tree = ast.parse(INTERP_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.startswith("src.delivery"):
            assert node.module == "src.delivery.router"
            assert [a.name for a in node.names] == ["get_delivery_info"]
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("src.delivery") for a in node.names)
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            assert name not in SEND_NAMES, f"interpreter references {name}"


def test_no_approve_or_send_subcommand():
    for cmd in ("approve", "send"):
        with pytest.raises(SystemExit):
            interpreter.main([cmd, "1"])


def test_live_credentials_are_blank():
    for key in ("LOB_API_KEY", "NOTIFYRE_API_KEY", "SMTP_USER", "SMTP_PASSWORD"):
        assert get_env(key) == ""
    with pytest.raises(RuntimeError):
        mail_sender._api_key(use_live=True)


def test_menu_and_pick_only_read(capsys):
    before = {t: _table_hash(t) for t in ("letters", "asks", "responses")}
    items = interpreter.main(["actions"])
    assert items
    for n in range(1, len(items) + 1):
        interpreter.main(["pick", str(n)])
    after = {t: _table_hash(t) for t in ("letters", "asks", "responses")}
    assert before == after


def test_lint_blocks_em_dash_and_missing_constituent(tmp_path):
    item = {"bills": ["HR.6589"]}
    errors, _ = interpreter.lint_letter(_body("Now — please."), item)
    assert any("dash" in e for e in errors)
    errors, _ = interpreter.lint_letter("Dear Rep, hi.", item)
    assert any("name" in e for e in errors)
    errors, warnings = interpreter.lint_letter(_body("See also S. 999."), item)
    assert not errors and any("999" in w for w in warnings)
    assert not any("6589" in w for w in warnings)  # H.R. 6589 == HR.6589


@pytest.mark.parametrize("claim", [
    "I have not been able to stop thinking about it.",
    "I am heartbroken.",
    "I keep coming back to this.",
    "Ever since then I have written weekly.",
    "I was there on Saturday.",
    "I marched to City Hall.",
])
def test_lint_blocks_feelings_continuous_and_presence_claims(claim):
    errors, _ = interpreter.lint_letter(_body(claim), {"bills": ["HR.6589"]})
    assert any("feelings/actions/presence" in e for e in errors), claim


def test_lint_exempts_only_todds_exact_sentence():
    item = {"bills": ["HR.6589"]}
    mine = "I am heartbroken."
    assert not interpreter.lint_letter(_body(mine), item, [mine])[0]
    # Anything broader than his exact sentence stays blocked.
    errors, _ = interpreter.lint_letter(_body("I am heartbroken. I keep at it."),
                                        item, [mine])
    assert any("I keep" in e for e in errors)
    assert interpreter.lint_letter(_body("I am heartbroken."), item,
                                   ["heartbroken"])[0]
    assert not interpreter.lint_letter(_body("I support the people who turned out."),
                                       item)[0]


def test_queue_relint_honors_todd_authored_header(recorder, tmp_path):
    interpreter.main(["actions"])
    mine = "I am heartbroken."
    body_file = tmp_path / "body.txt"
    body_file.write_text(_sourced(_body(mine)), encoding="utf-8")
    args = ["draft", "1", "--recipient", "Val Hoyle", "--channel", "fax",
            "--body-file", str(body_file)]
    assert interpreter.main(args) is None  # blocked without the override
    md = interpreter.main(args + ["--todd-wrote", mine])
    assert f"**Todd-authored:** {mine}" in md.read_text(encoding="utf-8")
    letter_id = interpreter.main(["queue", str(md)])
    assert db.get_letter_by_id(letter_id)["status"] == "pending_approval"
    conn = db.get_connection()
    conn.execute("DELETE FROM letters WHERE id=?", (letter_id,))
    conn.commit()
    conn.close()


def test_full_loop_sends_nothing_live(recorder, tmp_path):
    interpreter.main(["actions"])
    body_file = tmp_path / "body.txt"
    body_file.write_text(_sourced(_body()), encoding="utf-8")
    md = interpreter.main(["draft", "1", "--recipient", "Val Hoyle",
                           "--channel", "mail", "--body-file", str(body_file)])
    assert md and "**Sources:** asks#" in md.read_text(encoding="utf-8")

    letter_id = interpreter.main(["queue", str(md)])
    assert db.get_letter_by_id(letter_id)["status"] == "pending_approval"

    # Simulated click through the web UI's real endpoint, WEBUI_DRY_RUN=1.
    client = webui.app.test_client()
    job_id = client.post(f"/api/letter/{letter_id}/approve").get_json()["job_id"]
    for _ in range(40):
        job = client.get(f"/api/delivery/{job_id}/status").get_json()
        if job["status"] != "running":
            break
        time.sleep(0.25)
    assert job["dry_run"] is True
    assert db.get_letter_by_id(letter_id)["status"] == "approved"  # never 'sent'

    assert recorder, "expected the pipeline to reach a (recorded) sender"
    for name, kw in recorder:
        assert not kw.get("use_live"), f"{name} called with use_live"
        if name != "lob_render":  # the queue-time render is the free TEST key
            assert kw.get("dry_run") is True, f"{name} called without dry_run"

    interpreter.main(["log", "--notes", "pytest"])
    conn = db.get_connection()
    conn.execute("DELETE FROM letters WHERE id=?", (letter_id,))
    conn.commit()
    conn.close()


def test_rehearsal_fails_closed_without_dry_run():
    env = dict(os.environ)
    env.pop("WEBUI_DRY_RUN")
    proc = subprocess.run(
        [sys.executable, "scripts/rehearse_voice_loop.py", "--pick", "1",
         "--body-file", "x"], cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "REFUSING" in proc.stderr


def test_rehearsal_fails_closed_with_live_key_present():
    env = dict(os.environ)
    env.pop("LOB_API_KEY")
    proc = subprocess.run(
        [sys.executable, "scripts/rehearse_voice_loop.py", "--pick", "1",
         "--body-file", "x"], cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "LOB_API_KEY" in proc.stderr


def test_lint_blocks_urls():
    errors, _ = interpreter.lint_letter(_body("See https://example.org/x for more."), {"bills": []})
    assert any("URL" in e for e in errors)
    errors, _ = interpreter.lint_letter(_body("Details at www.example.org today."), {"bills": []})
    assert any("URL" in e for e in errors)


SOURCES = ("\n\nSources\n[1] Reuters, Oct 26 2026: kfgo.com/2026/10/26/spain\n"
           "[2] S.5658: congress.gov/bill/119th-congress/senate-bill/5658")


def test_lint_allows_urls_in_closing_sources_block():
    body = _body("Spain moved a draft rule forward [1], and S.5658 [2] is mine.") + SOURCES
    errors, warnings = interpreter.lint_letter(body, {"bills": ["S.5658"]})
    assert not any("URL" in e for e in errors)
    assert not any("footnote" in w or "cited" in w for w in warnings)


def test_lint_still_blocks_urls_in_prose_with_footnotes():
    body = _body("See https://example.org/x [1].") + SOURCES
    errors, _ = interpreter.lint_letter(body, {"bills": []})
    assert any("URL" in e for e in errors)


def test_lint_warns_on_orphan_markers_and_footnotes():
    body = _body("Spain moved ahead [3].") + SOURCES
    _, warnings = interpreter.lint_letter(body, {"bills": ["S.5658"]})
    assert any("[3]" in w and "no footnote" in w for w in warnings)
    assert any("footnote 1" in w and "never cited" in w for w in warnings)


def test_lint_sources_must_be_last_paragraph():
    body = _body("A claim [1].") + "\n\nSources\n[1] kfgo.com/x\n\nSincerely,\nTodd"
    prose, notes = interpreter.split_footnotes(body)
    assert notes == [] and "Sources" in prose


def test_lint_requires_sources_list_when_asked():
    errors, _ = interpreter.lint_letter(_body(), {"bills": []}, require_sources=True)
    assert any("no Sources list" in e for e in errors)
    errors, _ = interpreter.lint_letter(_sourced(_body()), {"bills": []}, require_sources=True)
    assert not any("Sources" in e for e in errors)
    errors, _ = interpreter.lint_letter(_body(), {"bills": []})  # off by default
    assert errors == []


def test_draft_without_sources_is_refused_unless_no_outside_facts(recorder, tmp_path):
    interpreter.main(["actions"])
    body_file = tmp_path / "body.txt"
    body_file.write_text(_body(), encoding="utf-8")
    args = ["draft", "1", "--recipient", "Val Hoyle", "--channel", "fax",
            "--body-file", str(body_file)]
    assert interpreter.main(args) is None
    assert interpreter.main(args + ["--no-outside-facts"]) is not None


def test_lint_warns_on_reply_ask_without_channel():
    item = {"bills": ["HR.6589"]}
    _, w = interpreter.lint_letter(_body("Please reply with your reasoning."), item)
    assert any("where to send" in x for x in w)
    _, w = interpreter.lint_letter(
        _body("Please reply with your reasoning. Write to me at a@b.org."), item)
    assert not any("where to send" in x for x in w)
    _, w = interpreter.lint_letter(_body("Your office responded in May."), item)
    assert not any("where to send" in x for x in w)


def test_reply_line_and_format_letter(monkeypatch):
    from src import letter
    base = {"name": "A B", "address": "1 St", "city": "Eugene", "state": "OR", "zip": "97401"}
    assert letter.reply_line(base) == ""
    assert letter.reply_line({**base, "email": " "}) == ""
    with_email = {**base, "email": "x@example.org"}
    assert letter.reply_line(with_email) == "You can reach me at x@example.org."
    monkeypatch.setattr(letter, "load_user", lambda: base)
    plain = letter.format_letter("Ron Wyden", "Senator", "t", "Body.")
    assert plain.endswith("Eugene, OR 97401")
    monkeypatch.setattr(letter, "load_user", lambda: with_email)
    out = letter.format_letter("Ron Wyden", "Senator", "t", "Body.")
    assert out == plain + "\n\nYou can reach me at x@example.org."
