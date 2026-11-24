"""fara: row quirks, name-match guards, contribution detection, resumable refresh and output (stubbed, no network)."""
import sqlite3

import pytest

from src import fara

SEN = {"name": "Ron Wyden", "chamber": "Senate"}
HOU = {"name": "Val Hoyle", "chamber": "House"}
FLAGS = {"values_flags": ["Israel", "Russia"], "country_aliases": {"RUSSIAN FEDERATION": "Russia"}, "iran_note": "",
         "icc": [{"country": "Israel", "person": "P. Minister", "checked": "2026-11-24"}]}


def test_rows_three_shapes():
    assert fara.rows({"ROWSET": ""}) == []
    assert fara.rows({"ROWSET": {"ROW": {"a": 1}}}) == [{"a": 1}]
    assert fara.rows({"ROWSET": {"ROW": [{"a": 1}, {"a": 2}]}}) == [{"a": 1}, {"a": 2}]
    assert fara.rows({"REGISTRANTS_ACTIVE": {"ROW": [{"a": 1}]}}) == [{"a": 1}]
    assert fara.rows(None) == []


def test_norm_country():
    assert fara.norm_country("RUSSIAN FEDERATION", FLAGS) == "Russia"
    assert fara.norm_country("SYRIA", FLAGS) == "Syria"
    assert fara.norm_country("  ", FLAGS) == ""


@pytest.mark.parametrize("text,found", [
    ("We met with Senator Wyden's staff in March.", True),
    ("Office of Sen. Ron Wyden was emailed.", True),
    ("Ron Wyden", True),
    ("Wyden, Senate Finance Committee", True),
    ("A Wyden Family Foundation grant", False),          # bare last name, no Senate/Oregon context
    ("the White House and Wyden met", False),            # "House" is not Senate context for a senator
])
def test_senator_guards(text, found):
    assert bool(fara.scan_text(text, [SEN])) is found


def test_house_guard_ignores_white_house():
    assert not fara.scan_text("White House aide Hoyle spoke", [HOU])
    assert fara.scan_text("Representative Hoyle (D-OR) was briefed", [HOU])
    assert fara.scan_text("Office of Rep. Val Hoyle", [HOU])


def test_contribution_in_section_with_amount():
    text = ("(c) DISBURSEMENTS-POLITICAL CONTRIBUTIONS\n 07/14/2026 Firm PAC   Ron Wyden   Check   $ 2,500.00\n"
            "Received by NSD/FARA\n V - INFORMATIONAL MATERIALS\n Later we mention Senator Wyden in a meeting.")
    hits = fara.scan_text(text, [SEN])
    kinds = sorted((h["kind"], h["amount"]) for h in hits)
    assert kinds == [("contribution", 2500.0), ("mention", None)]


def test_contribution_list_spans_page_footers():
    text = ("POLITICAL CONTRIBUTIONS 1/1/2025 A Bee Other Cand $ 5.00 Received by NSD/FARA Registration Unit "
            "Received by NSD/FARA Registration Unit 2/2/2025 C Dee Wyden for Oregon Check $ 250.00 "
            "V - INFORMATIONAL MATERIALS")
    h = fara.scan_text(text, [SEN])[0]
    assert (h["kind"], h["amount"]) == ("contribution", 250.0)


def test_amount_from_next_row_is_not_used():
    text = "POLITICAL CONTRIBUTIONS 1/1/2025 A Bee Wyden for Oregon Check $ 12/17/2025 C Dee Other Cand $ 99.00 V - INFORMATIONAL MATERIALS"
    h = fara.scan_text(text, [SEN])[0]
    assert (h["kind"], h["amount"]) == ("contribution", None)


def test_wrapped_amount_still_a_contribution():
    text = ("POLITICAL CONTRIBUTIONS 12/16/2025 Ed Pagano Wyden for Oregon Credit card " + "x" * 200 + " $ 500.00 Received by NSD")
    h = fara.scan_text(text, [SEN])[0]
    assert h["kind"] == "contribution" and h["amount"] is None


def test_excerpt_is_short_and_collapsed():
    h = fara.scan_text("x " * 400 + "Senator Wyden" + " y" * 400, [SEN])[0]
    assert len(h["excerpt"]) <= 310 and "\n" not in h["excerpt"]


def test_fetch_pdf_refuses_other_hosts(tmp_path):
    with pytest.raises(ValueError):
        fara.fetch_pdf("https://evil.example/x.pdf", tmp_path / "x.pdf")
    with pytest.raises(ValueError):
        fara.fetch_pdf("http://efile.fara.gov/docs/x.pdf", tmp_path / "x.pdf")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(fara, "DATA_DIR", tmp_path)
    c = sqlite3.connect(":memory:")
    fara.ensure_schema(c)
    return c


def _stub(monkeypatch, tmp_path, texts):
    calls = []

    def fetch_json(url, ttl=0):
        calls.append(url)
        if url.endswith("/Registrants/json/Active"):
            return {"REGISTRANTS_ACTIVE": {"ROW": [
                {"Registration_Number": 1, "Name": "Alpha Lobby", "Registration_Date": "01/02/2020"},
                {"Registration_Number": 2, "Name": "Beta Group", "Registration_Date": "01/02/2021"}]}}
        if url.endswith("/Registrants/json/Terminated"):
            return {"REGISTRANTS_TERMINATED": {"ROW": [
                {"Registration_Number": 3, "Name": "Old Firm", "Registration_Date": "01/02/2010", "Termination_Date": "03/04/2019"}]}}
        if "ForeignPrincipals/json/Active/1" in url:
            return {"ROWSET": {"ROW": {"FP_NAME": "Ministry X", "COUNTRY_NAME": "ISRAEL"}}}
        if "ForeignPrincipals/json/Active/2" in url:
            return {"ROWSET": {"ROW": [{"FP_NAME": "Board Y", "COUNTRY_NAME": "JAPAN"}]}}
        if "RegDocs/json/1" in url:
            return {"ROWSET": {"ROW": [
                {"URL": "https://efile.fara.gov/docs/1-ss.pdf", "DATE_STAMPED": "2025-05-01T00:00:00", "DOCUMENT_TYPE": "Supplemental Statement"},
                {"URL": "https://efile.fara.gov/docs/1-old.pdf", "DATE_STAMPED": "2020-05-01T00:00:00", "DOCUMENT_TYPE": "Supplemental Statement"},
                {"URL": "https://efile.fara.gov/docs/1-ex.pdf", "DATE_STAMPED": "2025-05-01T00:00:00", "DOCUMENT_TYPE": "Exhibit C"}]}}
        raise AssertionError(url)

    def fetch_pdf(url, dest):
        dest.write_bytes(b"%PDF fake")
        return dest

    class R:
        returncode = 0

    def run(cmd, **kw):
        pdf, txt = cmd[-2], cmd[-1]
        name = pdf.replace("\\", "/").rsplit("/", 1)[1]
        open(txt, "w", encoding="utf-8").write(next(v for k, v in texts.items() if name.endswith(k)))
        return R()

    monkeypatch.setattr(fara, "fetch_json", fetch_json)
    monkeypatch.setattr(fara, "fetch_pdf", fetch_pdf)
    monkeypatch.setattr(fara.subprocess, "run", run)
    return calls


def test_refresh_scope_resume_and_show(db, tmp_path, monkeypatch, capsys):
    calls = _stub(monkeypatch, tmp_path, {"1-ss.pdf": "Contact: Senator Wyden's office, Senate staff."})
    fara.refresh(db, flags=FLAGS, reps=[SEN], since="2023-01-01")
    # terminated-before-2023 registrant is excluded; only the Israel-principal registrant's docs are fetched
    assert db.execute("SELECT COUNT(*) FROM fara_registrants").fetchone()[0] == 2
    assert not any("RegDocs/json/2" in c for c in calls)
    assert db.execute("SELECT url, state FROM fara_docs").fetchall() == [("https://efile.fara.gov/docs/1-ss.pdf", "ok")]
    n = len([c for c in calls if "/Registrants/" not in c])
    fara.refresh(db, flags=FLAGS, reps=[SEN], since="2023-01-01")
    assert len([c for c in calls if "/Registrants/" not in c]) == n  # resumable: nothing refetched
    capsys.readouterr()
    monkeypatch.setattr(fara, "load_flags", lambda: FLAGS)
    monkeypatch.setattr(fara, "_fed_reps", lambda: [SEN])
    fara.show(db, "ron wyden")
    out = capsys.readouterr().out
    assert "[values flag: principal in Israel]" in out and "mention found" in out and "Ministry X (Israel)" in out
    assert "ICC arrest warrant issued for P. Minister" in out and "checked 2026-11-24" in out
    assert "unregistered influence isn't visible" in out
    assert "contact" not in out.lower().replace("contact:", "")  # the excerpt itself may say contact; our wording must not


def test_show_without_hits_never_says_none(db, monkeypatch, capsys):
    monkeypatch.setattr(fara, "load_flags", lambda: FLAGS)
    monkeypatch.setattr(fara, "_fed_reps", lambda: [SEN])
    fara.show(db, "Ron Wyden")
    out = capsys.readouterr().out
    assert "no mention found in the 0 documents scanned so far" in out and "unregistered influence" in out


def test_time_cap_stops_cleanly(db, tmp_path, monkeypatch, capsys):
    _stub(monkeypatch, tmp_path, {})
    fara.refresh(db, max_minutes=0, flags=FLAGS, reps=[SEN])
    assert "stopping cleanly" in capsys.readouterr().out


def test_ranking_flag_is_separate_column(db, monkeypatch, capsys):
    monkeypatch.setattr(fara, "load_flags", lambda: FLAGS)
    db.executemany("INSERT INTO fara_principals VALUES(?,?,?,?,?)", [
        (1, "A", "JAPAN", "Japan", "active"), (2, "B", "JAPAN", "Japan", "active"), (3, "C", "ISRAEL", "Israel", "active")])
    fara.ranking(db)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split(" | ")[1].strip() == "Japan" and "VALUES FLAG" not in lines[1]
    assert "VALUES FLAG" in lines[2] and "n/a: not in the FARA API" in lines[2]


def test_redirect_off_host_is_never_requested(monkeypatch):
    requested = []

    class R:
        status_code = 302
        headers = {"Location": "https://evil.example/x"}

        def raise_for_status(self): pass

    monkeypatch.setattr(fara.requests, "get", lambda url, **k: requested.append(url) or R())
    monkeypatch.setattr(fara, "_pace", lambda: None)
    with pytest.raises(ValueError):
        fara._get("https://efile.fara.gov/docs/x.pdf", 5)
    assert requested == ["https://efile.fara.gov/docs/x.pdf"]  # the off-host hop was refused before any request


def test_same_host_redirect_is_followed(monkeypatch):
    seen = []

    class Hop:
        status_code = 302
        headers = {"Location": "/CallAPI/GetJSON?a=1"}

    class Done:
        status_code = 200
        headers = {}

        def raise_for_status(self): pass

    monkeypatch.setattr(fara.requests, "get", lambda url, **k: seen.append(url) or (Hop() if len(seen) == 1 else Done()))
    monkeypatch.setattr(fara, "_pace", lambda: None)
    assert fara._get("https://efile.fara.gov/api/v1/RegDocs/json/1", 5).status_code == 200
    assert seen[1] == "https://efile.fara.gov/CallAPI/GetJSON?a=1"


def test_rescan_keeps_hits_when_text_missing(db, tmp_path, monkeypatch):
    db.execute("INSERT INTO fara_docs VALUES('https://efile.fara.gov/docs/9-x.pdf',9,'Supplemental Statement','2025-01-01','','','ok')")
    db.execute("INSERT INTO fara_hits(rep,reg_number,registrant,doc_type,doc_date,kind,amount,excerpt,url) "
               "VALUES('Ron Wyden',9,'R','S','2025-01-01','mention',NULL,'e','https://efile.fara.gov/docs/9-x.pdf')")
    fara.rescan(db, reps=[SEN])
    assert db.execute("SELECT COUNT(*) FROM fara_hits").fetchone()[0] == 1


def test_stale_lists_are_refetched(db, tmp_path, monkeypatch):
    calls = _stub(monkeypatch, tmp_path, {"1-ss.pdf": "nothing here"})
    fara.refresh(db, flags=FLAGS, reps=[SEN])
    db.execute("UPDATE fara_registrants SET docs_at='2000-01-01T00:00:00Z'")
    n = len([c for c in calls if "RegDocs" in c])
    fara.refresh(db, flags=FLAGS, reps=[SEN])
    assert len([c for c in calls if "RegDocs" in c]) > n


def test_pdf_name_is_the_file_name():
    assert fara.pdf_name("https://efile.fara.gov/docs/7-ss-1.pdf") == "7-ss-1.pdf"
