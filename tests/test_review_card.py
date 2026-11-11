from src.review_card import build_card, verify_card

HEAD = "A Constituent\nSpringfield, ST 00000\n\nOctober 19, 2026"
SIGN = "Sincerely,\nA Constituent"
LETTERS = [
    {"id": 50, "recipient": "Ron Wyden", "channel": "fax",
     "body": f"{HEAD}\n\nDear Senator Wyden,\n\nShared argument.\n\nWyden ask.\n\n{SIGN}"},
    {"id": 51, "recipient": "Val Hoyle", "channel": "mail",
     "body": f"{HEAD}\n\nDear Representative Hoyle,\n\nShared argument.\n\nHoyle ask.\n\n{SIGN}"},
]


def test_shared_text_shown_once_and_reassembles():
    card = build_card(LETTERS, "# Batch")
    assert card.count("Shared argument.") == 1
    assert "Wyden ask." in card and "Hoyle ask." in card
    assert "**This letter is:** S1-S2 + its greeting + S3 + its own paragraph + S4" in card
    assert verify_card(card, LETTERS) == []


def test_one_changed_character_fails():
    card = build_card(LETTERS).replace("Hoyle ask.", "Hoyle ask!")
    assert verify_card(card, LETTERS) == ["#51: reassembled text differs from the queued body"]


def test_no_shared_paragraphs_still_reassembles():
    solo = [dict(LETTERS[0], body="Dear X,\n\nOnly mine."), dict(LETTERS[1], body="Dear Y,\n\nAlso mine.")]
    card = build_card(solo)
    assert "Shared text" not in card
    assert verify_card(card, solo) == []


def test_card_has_no_raw_html_besides_markers():
    import re
    card = build_card(LETTERS)
    rest = re.sub(r"^<!-- (?:p:\w+|assemble #\d+: [\w ]+) -->$", "", card, flags=re.M)
    assert "<" not in rest


def _group(ids, topic_line):
    return [{"id": i, "recipient": f"Rep {i}", "channel": "fax",
             "body": f"{HEAD}\n\nDear Rep {i},\n\n{topic_line}\n\nOwn ask {i}.\n\n{SIGN}"} for i in ids]


# #5525
def test_address_date_signoff_shown_once_across_batches():
    from src.review_card import common_block, common_paragraphs
    groups = [_group([1, 2], "Topic A text."), _group([3, 4], "Topic B text.")]
    letters = [l for g in groups for l in g]
    common = common_paragraphs(groups)
    assert list(common.values()) == ["X1", "X2", "X3"]  # address, date, sign-off
    cards = [build_card(g, tag=chr(65 + i), common=common,
                        common_md=common_block(common, len(letters)) if i == 0 else "")
             for i, g in enumerate(groups)]
    card = "\n---\n\n".join(cards)
    assert card.count("A Constituent\nSpringfield") == 1 and card.count("Sincerely,") == 1
    assert card.count("Topic A text.") == 1 and card.count("Topic B text.") == 1
    assert verify_card(card, letters) == []
    assert "**This letter is:** X1-X2 + its greeting + A1 + its own paragraph + X3" in card


def test_single_batch_has_no_common_block():
    from src.review_card import common_paragraphs
    assert common_paragraphs([_group([1, 2], "T.")]) == {}


def test_record_block_is_reviewer_only(monkeypatch):
    from src import rep_record
    monkeypatch.setattr(rep_record, "record_context", lambda conn, rep, topic: [
        dict(date="2026-01-02", label="reported, unverified", text='article: "x"', url="https://e.x/a")])
    letters = [dict(l, topic="tariffs") for l in LETTERS]
    card = build_card(letters)
    assert card.count("Their record on this topic") == 2
    assert "[reported, unverified]" in card
    assert verify_card(card, letters) == []  # the block never enters a letter
