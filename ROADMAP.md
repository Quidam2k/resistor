# Resistor — Roadmap & Issue Pipeline
_Living doc. Updated each session. Two halves: what we write about (Issue Pipeline) and how the machine improves (System Upgrades)._

## Issue Pipeline
Status key: `proposed` (surfaced, not yet chosen) · `queued` · `sent` · `parked` · `landed` (rep acted)

### Active / recently sent
- **RCV cosponsorship (S.3425 / H.R.6589)** — `sent` 2026-08-18 to Wyden, Merkley, Hoyle. Next: follow-up round WITH response-invitation language; watch for cosponsorship movement.

### New candidates surfaced 2026-08-18 (not previously discussed) — all CONFIRMED as backlog by Todd 8/18
_Todd's call 8/18: log all four; do NOT draft yet. Re-scan facts fresh at draft time so letters are never based on month-old facts._
1. **Mid-decade redistricting / gerrymandering war** — `proposed`. **Todd: "100% in my lane, been following it closely."** TX drew a 5-seat GOP map (a state court called it an illegal racial gerrymander; stayed for Nov 2026); CA countered with a ~5-seat map; NC + MO also redrew. Directly on-brand with Todd's #1 (electoral reform, anti-two-party). Federal angle: ban on mid-decade redraws / independent-commission mandate (Freedom to Vote Act vehicle — verify current bill # before drafting). NATURAL COMPANION to the RCV thread. **Likely next letter.**
2. **Medicaid/SNAP cuts (H.R. 1, "One Big Beautiful Bill")** — `proposed`. **Todd: "in agreement — we need to be kinder to the least fortunate."** Most urgent/deadline-driven: Medicaid work requirements accelerated to Dec 31, 2026; SNAP -36% by 2034, Medicaid -15%; OR rural hospitals, clinics, Planned Parenthood at risk. Economic-justice + healthcare (ties to PeaceHealth stake). Ask senators to fight implementation / push restoration; ask Hoyle re: House.
3. **Oregon wildfire (record 2026 season) + air quality** — `proposed`. **Todd: "very serious, but feels like screaming into the void — doesn't make it less important."** Angle he named: write reps about the dangers of fossil fuels + reality of climate change. >2M acres burned (near state record); Cottage Grove fire (Lane County) started Aug 11; repeated unhealthy-air emergencies in Eugene-Springfield. Local + personal (his air) + climate.
4. **AI frontier-model safety** — `proposed`. **Todd: "very important... lots of meat on that bone, the trick is not choking on it."** Follows Robert Miles & Rational Animations — pitch to an alignment-literate reader; pick ONE concrete hook (e.g. a frontier-model-evaluation bill), don't cover all of AI risk. Real bills: AI Risk Evaluation Act (frontier evals), Future of AI Innovation Act, DEFIANCE Act (passed). Caution: some wrapped in a Blackburn "TRUMP AMERICA AI Act" — advocate substance, not vehicle. See [[todd-ai-safety-interest]].

### Backlog (from prior sessions / memory)
- Iran war-powers refresh · ICE/immigration accountability · Pentagon audit bill · Voting Rights Act restoration · congressional stock-trading ban / donor-conflict angle (Wyden not on ban) · PeaceHealth/ApolloMD SB 951 litigation (state-level).
- NOTE: NPVIC is a STATE target (OR already joined 2019) — not a federal letter.

## System Upgrades
Priority order (each should make the next session cheaper or more effective):
1. **Response capture + tracking loop** — **DONE 2026-08-18.** `db.record_response()` logs a reply to the `responses` table AND writes a markdown copy to `data/responses/`; `tracker.response_search_queries(since=)` gives Claude subject-filtered Gmail queries (filter by SUBJECT not sender — Merkley mixes newsletters + real replies from one address). Surfaced on the `python -m src.tracker` scoreboard, threads into future letters via `get_prior_correspondence()`. Back-imported 41 substantive Merkley replies (2022-2024, ResistBot era) as continuity material. NOTE: no Resistor-era (2026-03+) substantive reply yet; Wyden only auto-acknowledges.
2. **Outcome auto-check** — **DONE 2026-08-18.** New `asks` table + `src/tracker.py`. `python -m src.tracker check` polls Congress.gov for each open ask and flips it to `landed` the moment the rep sponsors OR cosponsors (matched by bioguideId). Seeded the 3 RCV asks (Wyden/Merkley→S.3425, Hoyle→H.R.6589); all confirmed `open` / not-yet-cosponsored via the live API today. Next: follow-up tickler (#4) to nudge stale open asks.
3. **This issue backlog** (DONE today — keep it fed). Implements "write about it all" as a sequence, not a choice.
4. **Follow-up tickler**: flag asks that are N weeks old with no response → auto-suggest a follow-up letter. Pair with the /schedule cron skill for a weekly nudge.
5. **Rep dossiers**: fold the ad-hoc donor-research .md files (trackaipac, FEC, etc.) into a structured per-rep file the drafter reads, so conflict-of-interest hooks are one lookup away.
6. **Postcard-with-art sender** (Lob /postcards) — deliberate visual/statement sends. Flagged 2026-08-18.
7. **Git hygiene**: lots of uncommitted src changes + research files sit uncommitted since initial commit. Commit to avoid losing work.
8. **Elicit a real Wyden response** (Todd flagged 2026-08-18): Wyden's office only sends the "Thank You for Contacting Me" auto-receipt — never a stated position, unlike Merkley. Try: a pointed single-question written ask via his web form, a call to the DC/Eugene office requesting a written policy response, a town-hall question, or a staff-meeting request. Goal is to break the boilerplate wall and get something quotable.
