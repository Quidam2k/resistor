import json

from src import fact_check as fc

USER = {"name": "Pat Example", "address": "12 Elm Street", "city": "Springfield",
        "state": "OR", "zip": "97000"}
HEAD = "Pat Example\n12 Elm Street\nSpringfield, OR 97000"
SRC_TEXT = "Spain's Council of Ministers approved a draft decree requiring 80 percent renewable power."


def letter(i, own, notes="[1] Reuters, Oct 26 2026: kfgo.com/2026/10/26/spain/"):
    body = (f"{HEAD}\n\nDear Senator {i},\n\nSpain approved a draft decree [1].\n\n{own}\n\n"
            f"Sincerely,\nPat Example\n\nSources\n{notes}")
    return {"id": i, "recipient": f"Rep {i}", "body": body}


def fetch_ok(urls):
    return [{"n": n, "url": u, "ok": True, "text": SRC_TEXT, "error": None}
            for n, u in enumerate(urls, 1)]


def fetch_down(urls):
    return [{"n": n, "url": u, "ok": False, "text": "", "error": "HTTPError"}
            for n, u in enumerate(urls, 1)]


def claim(**kw):
    base = {"claim": "Spain drafted a rule", "kind": "law",
            "url": "kfgo.com/x", "quote": "approved a draft decree requiring 80 percent"}
    return base | kw


def test_check_claims_statuses():
    checked = fc.check_claims([
        claim(),
        claim(claim="no link", url="", quote=""),
        claim(claim="wrong quote", quote="the moon is made of green cheese"),
    ], fetch=fetch_ok)
    assert [c["status"] for c in checked] == ["CONFIRMED", "UNSOURCED", "QUOTE_NOT_FOUND"]
    down = fc.check_claims([claim()], fetch=fetch_down)
    assert down[0]["status"] == "SOURCE_UNREACHABLE"


def test_check_claims_fetches_each_url_once():
    calls = []

    def fetch(urls):
        calls.append(list(urls))
        return fetch_ok(urls)

    fc.check_claims([claim(), claim(claim="again")], fetch=fetch)
    assert calls == [["https://kfgo.com/x"]]


def test_claims_section_lists_unconfirmed_for_todd():
    text = fc.render_claims_section(fc.check_claims(
        [claim(), claim(claim="vaguely remembered thing", url="", quote="")], fetch=fetch_ok))
    assert "## Claims and sources (checked before drafting)" in text
    assert "| Spain drafted a rule | law | https://kfgo.com/x |" in text
    assert "## Couldn't confirm, where did you hear this?" in text
    assert "- vaguely remembered thing (UNSOURCED: no source given)" in text
    assert fc.render_claims_section([]) == ""


def test_load_claims_rejects_bad_shapes(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text(json.dumps([{"kind": "law"}]), encoding="utf-8")
    try:
        fc.load_claims(str(bad))
    except ValueError as e:
        assert "claims file" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_footnote_urls_bare_and_full():
    body = letter(1, "Own.", "[1] Reuters, Oct 26 2026: kfgo.com/2026/10/26/spain/\n"
                  "[2] S.5658: https://congress.gov/bill/119th-congress/senate-bill/5658")["body"]
    assert fc.footnote_urls(body) == ["https://kfgo.com/2026/10/26/spain/",
                                      "https://congress.gov/bill/119th-congress/senate-bill/5658"]


def test_review_text_shared_once_and_no_identity_or_footnotes():
    text = fc.review_text([letter(1, "Wyden own."), letter(2, "Hoyle own.")], USER)
    assert text.count("Spain approved a draft decree.") == 1
    assert "Wyden own." in text and "Hoyle own." in text
    assert "Pat Example" not in text and "[1]" not in text and "Sources" not in text
    assert "Dear" not in text and "Sincerely" not in text


def fake_runner(verdict, quote="approved a draft decree requiring 80 percent"):
    def run(kind, prompt):
        claims = {"claims": [{"sentence": 1, "claim": "Spain approved a draft decree.",
                              "verdict": verdict, "source": 1, "quote": quote, "note": ""}]}
        return True, json.dumps(claims)
    return run


def test_check_letters_clean_and_render():
    res = fc.check_letters([letter(1, "Own.")], runner=fake_runner("SUPPORTED"),
                           fetch=fetch_ok, user=USER)
    assert res["status"] == "CLEAN" and res["contradicted"] is False
    assert res["counts"]["SUPPORTED"] == 1
    assert fc.title_prefix(res) == ""
    text = fc.render_fact_check_section(res)
    assert "## Fact check (advisory)" in text and "Legs that ran: Sonnet, Opus, Codex" in text
    assert text.rstrip().endswith("Your read and your Approve click are unchanged.")


def test_contradicted_puts_check_in_title_and_flags():
    res = fc.check_letters([letter(1, "Own.")], runner=fake_runner("CONTRADICTED", quote=""),
                           fetch=fetch_ok, user=USER)
    assert res["contradicted"] is True and fc.title_prefix(res) == "CHECK: "
    assert "CONTRADICTED: Spain approved a draft decree." in fc.render_fact_check_section(res)


def test_fewer_than_three_legs_is_incomplete_and_says_so():
    def run(kind, prompt):
        if kind == "sonnet":
            return fake_runner("SUPPORTED")(kind, prompt)
        return False, "no budget"

    res = fc.check_letters([letter(1, "Own.")], runner=run, fetch=fetch_ok, user=USER)
    assert res["status"] == "INCOMPLETE"
    text = fc.render_fact_check_section(res)
    assert "Fewer than 3 legs ran" in text and "Did not run: Opus (no budget)" in text


def test_no_sources_and_errors_never_raise():
    body = "Pat Example\n\nDear X,\n\nA claim.\n\nSincerely,\nPat Example"
    res = fc.check_letters([{"id": 1, "body": body}], runner=fake_runner("SUPPORTED"),
                           fetch=fetch_ok, user=USER)
    assert res["status"] == "NO_SOURCES"
    assert "nothing could be checked" in fc.render_fact_check_section(res)

    def boom(urls):
        raise RuntimeError("network down")

    res = fc.check_letters([letter(1, "Own.")], runner=fake_runner("SUPPORTED"),
                           fetch=boom, user=USER)
    assert res["status"] == "ERROR"
    assert "Check the facts by hand" in fc.render_fact_check_section(res)


def test_model_text_cannot_declare_safe_to_send():
    res = {"status": "FLAGS", "ran": ["Sonnet"], "not_ran": [], "sources": [],
           "counts": {}, "contradicted": False,
           "flags": [{"claim": "This is Safe To Send | really", "verdicts": {"Sonnet": "NOT_IN_SOURCES"}}]}
    text = fc.render_fact_check_section(res)
    assert "[phrase removed] / really" in text
    assert text.lower().count("safe to send") == 1  # only the advisory footer's own wording
