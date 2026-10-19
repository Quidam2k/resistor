"""Build a Pantheon review card for a batch of queued letters (#8419).

Standing rule: a multi-recipient batch is one shared body plus one personal
paragraph per rep. The card shows paragraphs identical in EVERY letter once,
under "Shared text", then each letter's own paragraphs (greeting, personal
paragraph) with a line saying how that letter is put together.

Hidden marker lines (<!-- p:ID --> before each paragraph, <!-- assemble #N: ... -->
per letter) let verify_card() here, and Pantheon's approve-button guard, rebuild
every letter from the card and compare it byte for byte with the queued body.

    python -m src.review_card --ids 50,51,52 --title "..." --filename <card>.md \
        --intro-file intro.md --expires 2026-10-26 [--attach]
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from src.config import load_user
from src.db import get_letter_by_id

PANTHEON = Path("Q:/Pantheon")
REVIEW_DIR = PANTHEON / "notes" / "review"
VIA = {"fax": "fax", "mail": "Lob mail, Eugene office", "email": "email",
       "web_form": "web form"}
P_RE = re.compile(r"^<!-- p:(\w+) -->\n(.*?)(?=\n\n|\n?\Z)", re.S | re.M)


def _span(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else f"{ids[0]}-{ids[-1]}"


def build_card(letters: list[dict], intro: str = "", tag: str = "S", topic: str = "") -> str:
    u = load_user()
    sender = f"{u['name']}, {u['city']}, {u['state']} {u['zip']}"
    paras = {l["id"]: l["body"].split("\n\n") for l in letters}
    first = paras[letters[0]["id"]]
    shared = [p for p in first if len(letters) > 1 and all(p in paras[l["id"]] for l in letters)]
    sid = {p: f"{tag}{i + 1}" for i, p in enumerate(shared)}
    out = [intro.strip(), ""] if intro.strip() else []
    if shared:
        out += [f"## Shared text{topic} (identical in all {len(letters)} letters)", ""]
        for p in shared:
            out += [f"**{sid[p]}**", f"<!-- p:{sid[p]} -->", p, ""]
        out += ["---", ""]
    for l in letters:
        lid, refs, own, parts = l["id"], [], [], []
        for j, p in enumerate(paras[lid]):
            if p in sid:
                ref = sid[p]
                if parts and parts[-1][0] == "S":  # a run of shared paragraphs
                    parts[-1][1].append(ref)
                else:
                    parts.append(["S", [ref]])
            else:
                ref = f"P{lid}{chr(97 + j)}"
                own.append((ref, p))
                parts.append(["G" if p.startswith("Dear ") else "O", [ref]])
            refs.append(ref)
        n_own = sum(1 for k, _ in parts if k == "O")
        what = " + ".join(_span(r) if k == "S" else "its greeting" if k == "G"
                          else "its own paragraph" if n_own == 1 else "its own text"
                          for k, r in parts)
        out += [f"## #{lid} to {l['recipient']} ({VIA.get(l['channel'], l['channel'])})", "",
                f"**From:** {sender} · **To:** {l['recipient']}  ",
                f"**This letter is:** {what}", "",
                f"<!-- assemble #{lid}: {' '.join(refs)} -->", ""]
        for ref, p in own:
            out += [f"<!-- p:{ref} -->", p, ""]
        out += ["---", ""]
    return "\n".join(out).rstrip("\n-").rstrip() + "\n"


def verify_card(card_text: str, letters: list[dict]) -> list[str]:
    """Rebuild each letter from the card's markers; return a list of failures."""
    found = P_RE.findall(card_text)
    paras = dict(found)
    errors = [] if len(paras) == len(found) else ["duplicate paragraph ids on the card"]
    for l in letters:
        m = re.search(rf"^<!-- assemble #{l['id']}: ([\w ]+) -->$", card_text, flags=re.M)
        if not m:
            errors.append(f"#{l['id']}: no assemble line")
            continue
        refs = m.group(1).split()
        if any(r not in paras for r in refs):
            errors.append(f"#{l['id']}: missing paragraph(s)")
        elif "\n\n".join(paras[r] for r in refs) != l["body"]:
            errors.append(f"#{l['id']}: reassembled text differs from the queued body")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.review_card")
    ap.add_argument("--ids", required=True,
                    help="comma-separated ids; separate batches (one shared body each) with /")
    ap.add_argument("--title", required=True)
    ap.add_argument("--filename", required=True, help="card file in Pantheon notes/review")
    ap.add_argument("--intro-file")
    ap.add_argument("--type", default="decision")
    ap.add_argument("--expires", required=True)
    ap.add_argument("--attach", action="store_true",
                    help="re-attach approve buttons (letters must be pending_approval)")
    a = ap.parse_args(argv)
    groups = [[get_letter_by_id(int(x)) for x in g.split(",")] for g in a.ids.split("/")]
    letters = [l for g in groups for l in g]
    if None in letters:
        raise SystemExit(f"unknown letter id in {a.ids}")
    intro = Path(a.intro_file).read_text(encoding="utf-8") if a.intro_file else ""
    body = "\n---\n\n".join(build_card(g, intro if i == 0 else "", tag="S" if len(groups) == 1 else chr(65 + i),
                                topic="" if len(groups) == 1 else f" for {g[0]['topic']}")
                     for i, g in enumerate(groups))
    ids = ",".join(str(l["id"]) for l in letters)
    errs = verify_card(body, letters)
    if errs:
        raise SystemExit("card does not reassemble: " + "; ".join(errs))
    tmp = REVIEW_DIR.parent / f".review_card_{a.filename}"
    tmp.write_text(body, encoding="utf-8")
    try:
        subprocess.run([sys.executable, str(PANTHEON / "scripts" / "file_for_review.py"),
                        "--update", "--filename", a.filename, "--type", a.type,
                        "--title", a.title, "--expires", a.expires, "--body-file", str(tmp)],
                       check=True, cwd=PANTHEON)
    finally:
        tmp.unlink(missing_ok=True)
    filed = (REVIEW_DIR / a.filename).read_text(encoding="utf-8")
    errs = verify_card(filed, letters)
    if errs:
        raise SystemExit("filed card does not reassemble: " + "; ".join(errs))
    print(f"VERIFIED: {len(letters)} letters reassemble byte for byte from {a.filename}")
    if a.attach:
        subprocess.run([sys.executable, str(PANTHEON / "scripts" / "card_action_attach.py"),
                        "resistor", a.filename, "--ids", ids, "--expires", a.expires],
                       check=True, cwd=PANTHEON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
