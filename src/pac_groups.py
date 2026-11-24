"""Curated committee groups (config/pac_groups.yaml) and non-disclosing-filer spending per rep, FEC-reported only (#6847).

Committees, amounts, cycles and links only; never individuals. Everything is "FEC-reported", never verified.
"""
import re
from pathlib import Path

import yaml

SUFFIXES = ("pac", "conduit", "ie")
CATS = ("direct", "ie", "separate")
FOOTER = ("Source: FEC (api.open.fec.gov). Committee names, amounts and cycles only; "
          "this is FEC-reported money, not proof of motive; not verified.")
DARK_CAPTION = ("What we can't see: outside spending by non-committee filers (FEC Form 5), which report their spending "
                "but not a full list of who funds them; amounts only where FEC shows them.")
FEC = "https://www.fec.gov/data/"


def load_groups(path=None):
    p = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "pac_groups.yaml"
    with open(p, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("groups", {})


class Truncated(ValueError):
    """More pages than we will read: partial data must never read as 'none found'."""


def _paged(get, path, **params):
    """Yield result rows following FEC keyset paging (max 10 pages; raises Truncated rather than stopping silently)."""
    extra = {}
    for _ in range(10):
        page = get(path, **params, **extra) or {}
        yield from page.get("results") or []
        pagination = page.get("pagination") or {}
        last = pagination.get("last_indexes") or {}
        if last.get("last_index"):
            extra = dict(last)
        elif pagination.get("pages") and (pagination.get("page") or 1) < pagination["pages"]:
            extra = {"page": (pagination.get("page") or 1) + 1}  # aggregate endpoints (by_candidate) page by number
        else:
            return
    raise Truncated(f"{path}: more than 10 pages; narrow the query")


def _district(d):
    return str(d or "").zfill(2)


def group_rows(get, group_key, group, rep_committees, candidate_id, cycle, office, district, state, ballot_years=None, party=None):
    names = {c["id"]: c["name"] for c in group.get("committees", [])}
    ids = list(names)
    out = []
    # A. receipts
    direct, conduit = {}, {}
    seen = set()
    for cid in rep_committees:
        for r in _paged(get, "schedules/schedule_a/", committee_id=cid,
                        two_year_transaction_period=cycle, contributor_id=ids, per_page=100):
            if r.get("is_individual"):
                continue
            sid = r.get("sub_id")
            if sid is not None:
                if sid in seen:
                    continue
                seen.add(sid)
            gid = r.get("contributor_id")
            amt = r.get("contribution_receipt_amount") or 0
            if gid not in names or amt == 0:
                continue
            bucket = conduit if r.get("memo_code") == "X" else direct
            s = bucket.setdefault((cid, gid), [0, 0])
            s[0] += amt
            s[1] += 1
    for bucket, suffix, note in ((direct, "pac", "direct"), (conduit, "conduit", "earmarked through")):
        for (cid, gid), (amt, n) in bucket.items():
            if amt <= 0:
                continue
            url = (f"{FEC}receipts/?committee_id={cid}&contributor_id={gid}"
                   f"&two_year_transaction_period={cycle}")
            out.append((f"{group_key}_{suffix}", names[gid], amt, n, note, url))
    # B. independent expenditures
    dedup = {}
    for r in _paged(get, "schedules/schedule_e/", committee_id=ids, candidate_office_state=state, most_recent="true",
                    min_date=f"{cycle - 1}-01-01", max_date=f"{cycle}-12-31", per_page=100):
        m = re.match(r"^([A-Z])(\d{4})$", r.get("election_type") or "")
        year = int(m.group(2)) if m else None
        if year is None and r.get("candidate_id") == candidate_id and re.match(r"^\d{4}", r.get("expenditure_date") or ""):
            year = int(r["expenditure_date"][:4]); year += year % 2  # notices can lack election_type; date gives the cycle
        if year != cycle or r.get("committee_id") not in names:
            continue
        so = r.get("support_oppose_indicator")
        if so not in ("S", "O"):
            continue
        if r.get("candidate_id") == candidate_id:
            note = "support" if so == "S" else "oppose"
        elif (m and m.group(1) == "P" and cycle in (ballot_years or ()) and party and r.get("candidate_party") == party
              and r.get("candidate_office") == office
              and (office != "H" or _district(r.get("candidate_office_district")) == _district(district))):
            note = "support of primary opponent" if so == "S" else "oppose primary opponent"
        else:
            continue
        key = (r["committee_id"], r.get("candidate_id"), so, r.get("expenditure_amount"),
               r.get("payee_name"), r.get("expenditure_date"))
        old = dedup.get(key)
        if old is None or (old[0].get("is_notice") and not r.get("is_notice")):
            dedup[key] = (r, note)
    sums = {}
    for r, note in dedup.values():
        s = sums.setdefault((r["committee_id"], note), [0, 0, r.get("candidate_id")])
        s[0] += r.get("expenditure_amount") or 0
        s[1] += 1
    for (gid, note), (amt, n, rcid) in sums.items():
        if amt <= 0:
            continue
        who = f"candidate_id={rcid}&" if "opponent" not in note else ""  # opponents vary; link the committee's spending
        url = f"{FEC}independent-expenditures/?{who}committee_id={gid}&cycle={cycle}"
        out.append((f"{group_key}_ie", names[gid], amt, n, note, url))
    return out


def dark_rows(get, candidate_id, cycle):
    rows = list(_paged(get, "schedules/schedule_e/by_candidate/", candidate_id=candidate_id, cycle=cycle, per_page=100))
    url = f"{FEC}independent-expenditures/?candidate_id={candidate_id}&cycle={cycle}"
    out = []
    for r in rows:
        so = {"S": "support", "O": "oppose"}.get(r.get("support_oppose_indicator"))
        if (str(r.get("committee_id") or "").startswith("C9") and (r.get("total") or 0) > 0
                and r.get("committee_name") and so):
            out.append(("dark_ie", r["committee_name"], r["total"], r.get("count"), so, url))
    return out


def _m(x):
    return f"${x or 0:,.0f}"


def checked_rows(group_key, group, cycle):
    """Marker row stored when a group was fetched for a cycle, so 'none found' is never claimed for an unfetched rep."""
    return [("group_checked", group_key, 0, len(group.get("committees", [])), "checked",
             f"{FEC}receipts/?two_year_transaction_period={cycle}")]


def _cycles(conn, rep, key):
    """(checked, unchecked) cycles: every cycle we hold for the rep, split by whether this group was fetched for it."""
    have = {r[0] for r in conn.execute("SELECT DISTINCT cycle FROM rep_donors WHERE rep=?", (rep,))}
    done = {r[0] for r in conn.execute("SELECT cycle FROM rep_donors WHERE rep=? AND kind='group_checked' AND donor=?", (rep, key))}
    return sorted(done), sorted(have - done)


def _note(conn, rep, key):
    done, missing = _cycles(conn, rep, key)
    return (f" [cycles checked: {', '.join(map(str, done)) or 'none'}" + (f"; NOT yet checked: {', '.join(map(str, missing))}" if missing else "") + "]")


def _fetch(conn, rep, kinds):
    q = ",".join("?" * len(kinds))
    return conn.execute(
        f"SELECT cycle, kind, donor, amount, n, note, url FROM rep_donors "
        f"WHERE rep=? AND kind IN ({q}) ORDER BY cycle DESC, donor", (rep, *kinds)).fetchall()


def summary_lines(conn, rep, groups):
    lines = []
    for key, g in groups.items():
        cmd = f'python -m src.rep_donors group {key} "{rep}"'
        rows = _fetch(conn, rep, [f"{key}_{s}" for s in SUFFIXES])
        label = g.get("label", key)
        done, _missing = _cycles(conn, rep, key)
        if not done:
            lines.append(f"{label}: not checked yet for this rep -- python -m src.rep_donors refresh --rep \"{rep}\" --force")
            continue
        if not rows:
            n = len(g.get("committees", []))
            lines.append(f"{label}: none found among {n} listed committees "
                         f"(FEC-reported; listed only){_note(conn, rep, key)} -- {cmd}")
            continue
        catof = {c["name"]: c.get("category", "direct") for c in g.get("committees", [])}

        def part(rs, sep):
            rs = [r for r in rs if (catof.get(r["donor"]) == "separate") == sep]
            pac = [r for r in rs if r["kind"].endswith("_pac")]
            con = [r for r in rs if r["kind"].endswith("_conduit")]
            ie = {}
            for r in rs:
                if r["kind"].endswith("_ie"):
                    ie[r["note"]] = ie.get(r["note"], 0) + (r["amount"] or 0)
            ie_txt = ", ".join(f"{_m(v)} {k}" for k, v in sorted(ie.items())) or "$0"
            return (f"direct {_m(sum(r['amount'] for r in pac))} ({sum(r['n'] or 0 for r in pac)} gifts); earmarked through "
                    f"listed committees {_m(sum(r['amount'] for r in con))} ({sum(r['n'] or 0 for r in con)} items); independent spending {ie_txt}")
        text = f"{label} (FEC-reported, our list): {part(rows, False)}"
        if any(catof.get(r["donor"]) == "separate" for r in rows):
            text += f" | shown separately ({g.get('categories', {}).get('separate', 'separate')}): {part(rows, True)}"
        lines.append(f"{text}{_note(conn, rep, key)} -- {cmd}")
    dark = _fetch(conn, rep, ["dark_ie"])
    if dark:
        cyc = sorted({r["cycle"] for r in dark})
        lines.append("What we can't see (FEC-reported outside spending by non-committee filers, no full donor list): "
                     f"{_m(sum(r['amount'] for r in dark))} in cycle(s) "
                     + ", ".join(str(c) for c in cyc))
    elif conn.execute("SELECT 1 FROM rep_donors WHERE rep=? AND kind='group_checked' LIMIT 1", (rep,)).fetchone():
        lines.append("What we can't see (FEC-reported outside spending by non-committee filers, no full donor list): none reported in the checked cycles")
    return lines


def report_lines(conn, rep, group_key, group):
    label = group.get("label", group_key)
    cats = group.get("categories") or {}
    catof = {c["name"]: c.get("category", "direct") for c in group.get("committees", [])}
    rows = _fetch(conn, rep, [f"{group_key}_{s}" for s in SUFFIXES])
    lines = [f"{label} -- FEC-reported committee money for {rep} "
             "(committees on our list only; not verified)"]
    done, _missing = _cycles(conn, rep, group_key)
    lines.append(_note(conn, rep, group_key).strip())
    if not done:
        lines.append("Not checked yet for this rep: run python -m src.rep_donors refresh --force first.")
    elif not rows:
        lines.append(f"None found among {len(catof)} listed committees in the checked cycles.")
    for cyc in sorted({r["cycle"] for r in rows}, reverse=True):
        lines.append(f"Cycle {cyc}:")
        crow = [r for r in rows if r["cycle"] == cyc]
        for cat in CATS:
            part = [r for r in crow if catof.get(r["donor"], "direct") == cat]
            if not part:
                continue
            lines.append(f"  [{cat}] {cats.get(cat, '')}".rstrip())
            for r in part:
                lines.append(f"    {r['donor']}: {_m(r['amount'])} (n={r['n'] or 0}), "
                             f"{r['kind'].rsplit('_', 1)[1]}, {r['note']} -- {r['url']}")
    if done and not any(r["kind"].endswith("_ie") for r in rows):
        lines.append("Independent spending for/against this rep (or a primary opponent) by listed committees: none found in the checked cycles.")
    totals = {}
    for r in rows:
        k = (r["kind"].rsplit("_", 1)[1], r["note"])
        totals[k] = totals.get(k, 0) + (r["amount"] or 0)
    for (kind, note), v in sorted(totals.items()):
        lines.append(f"Total across the cycles above, {kind} ({note}): {_m(v)}")
    lines.append("Listed committees: " + "; ".join(c["name"] for c in group.get("committees", [])))
    lines.append("Not covered: " + "; ".join(group.get("not_covered") or []))
    if group.get("footer_extra"):
        lines.append(group["footer_extra"])
    lines.append(DARK_CAPTION)
    dark = _fetch(conn, rep, ["dark_ie"])
    if not dark:
        lines.append("  none reported in the checked cycles" if done else "  not checked yet")
    for r in dark:
        lines.append(f"  {r['cycle']}: {r['donor']} {_m(r['amount'])} {r['note']} -- {r['url']}")
    lines.append(FOOTER)
    return lines
