# Source doc: Claude Desktop analysis (Todd pasted, msg 26005, 2026-09-05T16:44:57Z)

Preserved verbatim per assignment #4295 so it survives.

---

oh fuck yeah check this out- i just talked to Claude Desktop about using AI with FOIA, then had it look at the Resistor repo to see if that FOIA chat had any applicability. It did more than just that- it pointed out some real flaws.

# Resistor × FOIA: Closing the Loop

## The core observation

Resistor is a **write-side** tool. It takes your opinion and turns it into high-quality, well-sourced, well-timed pressure on a specific decision-maker.

FOIA is the **read-side** primitive. It's the only mechanism a private citizen has to compel the production of facts that nobody wants published.

Right now Resistor's evidence base is: news headlines, Congress.gov bill text, and roll call votes. All of that is *already public and already reported*. Every other constituent writing about the same bill has access to the identical corpus. Your letters are better because they're personalized and persistent — but they are not better because they contain information the office doesn't have.

A letter containing a primary-source document the staffer has never seen is a categorically different object. It's not advocacy anymore; it's intelligence delivery. Staffers read those.

That's the synergy. Resistor currently has a research phase that reads the news. It should have a research phase that reads the *record*.

---

## Part 1: The FOIA plays worth building

### 1.1 Reconnaissance before filing

The highest-leverage AI use in FOIA is not writing requests — it's avoiding bad ones. FOIA expert David Cuillier recommends pulling FOIA logs from MuckRock or governmentattic.org, loading them into an AI tool, and prompting it to check whether what you want has already been released.

Agency request logs (the log of every FOIA request received) are themselves FOIA-able and are the highest-value single document in the whole ecosystem: they reveal what records exist, what the agency calls them internally, and who else is already fighting for them. An LLM clustering a 5,000-row log by subject is real work nobody wants to do by hand.

### 1.2 Nomenclature mining

Requests die on vagueness. Practitioners advise learning the formal names of records — form numbers, internal acronyms — and identifying which agency actually holds them, before filing. Use a model to mine org charts, NARA records-retention schedules, IG reports, and congressional budget justifications for exact terminology, then write the request around those terms. This is the single biggest quality delta between an amateur and a professional request.

### 1.3 Post-release document processing

This is where AI earns its keep. DocumentCloud is free, open source, run by MuckRock, supports table extraction from PDFs, site scraping and monitoring, and AI summarization across large document collections, plus unlimited Whisper-based transcription. The recommended workflow is explicit: use AI to build a rough index, surface names and recurring terms, check the summary against the original records, and quote only from source documents.

### 1.4 Multi-jurisdiction comparison

The structurally underexploited move. File one narrow request to 50 sheriff's offices or 200 school districts, then use models to normalize wildly inconsistent responses into one comparable dataset. Nobody does this well because the normalization is tedious. Comparative datasets — jail deaths, use-of-force settlements, ALPR contracts, lead reporting — move policy in ways single-agency scoops don't.

### 1.5 Denial triage and appeal

Roughly half of improper denials go unappealed because people don't know they can. Parse the denial, identify the claimed exemption, check it against DOJ OIP guidance.

**Hard constraint:** the Pennsylvania Office of Open Records has warned that AI-generated public records filings have contained fabricated legal citations, inaccurate case-law summaries, and invented quotations from court decisions, and that the filer remains responsible for verifying anything they submit. Shepardize or don't file.

### 1.6 Monitoring infrastructure

When EPA decommissioned FOIAonline on September 30, 2023, POGO and MuckRock had to scrape and archive roughly 34,000 documents themselves because no one guaranteed the records would stay public. Durable scrapers plus change-detection over agency reading rooms is unglamorous and enormously valuable — and it's a passive system, which is the right shape.

---

## Part 2: What NOT to build

**Do not build a request-generation bot.** Federal agencies received more than 1.7 million FOIA requests in fiscal 2025. The FOIA Advisory Committee flagged AI-generated request floods as a critical priority for its 2026–2028 term. Nearly 19% of federal agencies are already using AI or machine learning in FOIA processing per the OGIS FY2025 Ombudsman Report.

Automated volume is the live pretext for restricting access: fee-waiver denials, "unreasonably burdensome" rejections, legislative narrowing. Every junk request degrades the commons and taxes the same understaffed office whose cooperation you need.

**This is the same failure mode Resistor already correctly avoids on the letter side.** Design Principle #1 ("Original composition always") is exactly right, and it generalizes: precision is both the ethic and the strategy. Whatever FOIA capability gets built into Resistor should inherit that principle verbatim.

---

## Part 3: Concrete integration points

### 3.1 The killer feature: FOIA your own representative's agency correspondence

Congress is **not** subject to FOIA. But executive agencies are — and agencies hold the letters members of Congress send them.

That means you can FOIA an agency for correspondence from your senator's office. This reveals what your member actually pushes for privately versus what they say publicly. A letter that opens *"On April 3 your office wrote to the BLM state director requesting X — I'm writing because Y"* is not something a staffer batches into a tally.

**Verify before building:** the "congressional control" doctrine lets Congress designate documents as retaining congressional control, which can exempt them from FOIA as not being "agency records." Agencies also routinely apply Exemption 6 to constituent identities. This works, but not universally, and the case law is worth reading before you promise it in a README.

### 3.2 Regulatory comments — the biggest missing feature, FOIA or not

This is the single largest strategic gap in Resistor, and it happens to compound with FOIA.

A constituent letter to Congress creates political pressure. A comment on a proposed rule at regulations.gov creates **a legal obligation**: agencies must respond to substantive comments in the final rule, and failure to do so is grounds for an Administrative Procedure Act challenge. A well-constructed comment from one person can generate more binding obligation than a thousand letters.

Resistor is already architected for this — research → compose → fact-check → deliver, with a delivery router. Regulations.gov is just another delivery channel, with a public API. The composition prompt changes (comments need to raise specific factual or legal defects, not express preference), but the machinery exists.

**The FOIA compound:** file a comment on a proposed rule, then FOIA the agency for the docket's ex parte communications and meeting logs to see who is actually lobbying on it. Then comment again citing what you found. That's a loop no advocacy org bothers to run at the individual level.

### 3.3 Implementation-outcome letters

FOIA state and federal agencies for implementation data on bills your member voted for, then write: *"You voted for X. Here's what your district actually received."* This is the most persuasive letter form available, and it's almost never used because assembling the data is tedious. It's also exactly the kind of work an agent loop is good at.

### 3.4 Shared document corpus — the right shape for Phase D

Phase D currently proposes coordinating *letters* across users in a district. That's the astroturf failure mode; 10 near-identical letters is a form campaign with extra steps, and it's precisely what the anti-AI-flood backlash is aimed at.

Invert it: **share documents, not letters.** A district-level DocumentCloud collection that every Resistor user in OR-4 draws from, where each person writes their own letter from a common evidence base. That gets the coordination benefit (50 letters citing real records) without the detection risk, and it sidesteps the privacy problem entirely — the shared artifact is public documents, not user data.

### 3.5 Architectural fit

```
src/
  foia/
    logs.py          # ingest + cluster agency FOIA logs
    compose.py       # draft narrow requests (human-approved, rate-limited)
    tracker.py       # 20-business-day clock, appeal deadlines, tolling events
    ingest.py        # OCR + index responsive records
    corpus.py        # local document store; feeds letter composition context
  delivery/
    regulations_gov.py
    foia_portal.py   # foia.gov + agency-specific portals
```

The `tracker.py` piece is the highest value-per-line in the whole list. FOIA deadlines are legally meaningful (20 business days for the initial determination, with defined tolling and extension conditions), agencies blow through them constantly, and almost nobody tracks them well enough to escalate. A passive tracker that says "Agency X is 47 days past statutory deadline on request 24-1183, here is your appeal draft" is exactly the turbine-in-the-wind design you'd want.

---

## Part 4: Feedback on Resistor independent of FOIA

### 4.1 Verify your data dependencies — this is the urgent one

`congress_api.py` is documented as "Congress.gov + GovTrack APIs" and ROADMAP has `resist votes update # Download latest voting records from GovTrack`.

**GovTrack ended its bulk data and API.** They announced the shutdown and now direct bulk-data users to the open-source `unitedstates/congress` scrapers. GovTrack's own about-our-data page describes its open-data offering as having run until 2017, when Congress began publishing structured data itself. Whatever your code is actually hitting, this dependency needs auditing before you tell users to rely on it.

**Congress.gov roll call votes are House-only.** The House Roll Call Votes endpoints launched in beta in May 2025 in partnership with the Clerk of the House, covering votes from 2023 (118th Congress) forward, with the beta label removed in December 2025. There is no equivalent Senate votes endpoint — Senate roll calls come from senate.gov XML.

This directly contradicts the README: *"This pulls every vote your reps have cast in the current Congress (usually 500-700+ each)."* For senators, via that API, it can't. And senators are your primary fax target. Either the code is doing something the README doesn't describe, or the README is wrong. Both are fixable; shipping either is a credibility problem for a project whose stated currency is credibility.

**Recommendation:** vendor `unitedstates/congress` (public domain, CC0) as the vote/legislator data source, use Congress.gov API for bill text and House votes, and add a startup healthcheck that verifies each upstream returns data before a session begins.

### 4.2 Your README violates your own Design Principle #2

Principle #2 is "Fact-check before send — every claim verified against sources." The README's own claims don't meet that bar.

**"A personalized letter is worth 1,000 form letters"** — I can't find CMF stating this. What CMF's staff surveys actually show: about 90% of congressional staff said individualized postal letters would have a lot of positive influence on an undecided member, 88% said the same of individualized email, and only 3% said form messages have "a lot" of influence on undecided lawmakers. That's a strong finding. "1,000×" is a folk-wisdom compression of it, not a result.

**"Fewer than 50 personalized contacts can change an undecided member's position in 70% of offices"** — this figure circulates, including on at least one commercial AI-letter vendor's site, but the CMF-sourced number I can find is different: about 90% of congressional office respondents agreed that fewer than 100 personalized emails on an issue is enough to get the office to consider taking the requested action. Similar spirit, different number.

**"The medium doesn't matter"** — this one holds up. The research found essentially no distinction between email and postal mail once you control for personalization.

Fix: cite the specific CMF publication and year for each figure, or restate them at the level the research supports. Anyone hostile who wants to discredit this project will start here, and a tool that fact-checks letters but not its own pitch is an easy target.

Also pin the Anthropic pricing claim to a doc link rather than a hardcoded number — plan terms change and a stale price in a README is a support burden. See https://support.claude.com.

### 4.3 Timing is the missing variable

The README promises letters arrive "at the strategically right moment," but nothing in the architecture models timing. Floor votes are the *worst* moment — positions are locked. The leverage window is:

- Bill referred to a committee your member sits on, **before markup**
- Appropriations subcommittee, during the request/markup cycle
- Nomination pending before a committee your member sits on
- Comment period open on a rule (see 3.2)
- Recess, when district offices have bandwidth

Add `committees` to `representatives.yaml`, pull committee assignments and scheduled markups, and let the session open with "your rep sits on the committee that has HR 2201 — markup is likely in the next three weeks" rather than "here's what's in the news."

That's the highest-ROI change in this document, and it's small.

### 4.4 State and local is under-weighted

You have Oregon code and it's framed as an incomplete feature. It's arguably the main event. Marginal influence per contact at a city council or state legislative office is one to two orders of magnitude above a Senate office, response rates are far higher, and state open-records laws are frequently *faster* and broader than federal FOIA. A person who can't move a senator can absolutely move a county commission.

### 4.5 Close the outcome loop

You track sent letters and logged responses. You don't track whether it worked. Log the ask, then check the subsequent vote, cosponsorship, or public statement against it. Without that, the tool's central claim — that this is more effective — is unfalsifiable, which is a bad property for a project built on epistemic rigor.

### 4.6 Smaller notes

- **Delivery confirmation.** Fax has a real bounce rate and congressional office fax numbers rot. Log per-office success/failure and surface a "this channel has failed 3× for Sen. X" warning rather than silently burning $0.03.
- **Composition anti-patterns.** Your worst failure mode is letters that read as LLM output — em-dash cadence, tricolons, "It's not just X, it's Y." Staffers pattern-match on this now. Consider an explicit style constraint in the composition prompt and a fingerprint check in the fact-check pass.
- **License friction.** MIT is fine, but a project explicitly designed to be forked for civic ends might consider whether you want commercial astroturf shops forking it. You probably can't prevent that; worth deciding deliberately rather than by default.
- **Disclosure.** Decide now whether letters disclose AI assistance. Norms are forming, several state open-records offices are already reacting to AI-generated filings, and being on the early-and-transparent side of that is cheaper than being caught on the other side.

---

## Summary

The one-sentence version: **Resistor makes your voice louder; FOIA gives it something to say that nobody else has.**

If you build one thing from this document, build the FOIA deadline tracker (3.5) — it's small, passive, and legally consequential. If you build two, add regulatory comments as a delivery channel (3.2) — it's the only path in this whole space that produces a legally binding obligation rather than political pressure.

And before any of it, audit the GovTrack/Congress.gov dependency and fix the README's stats. A civic-tech tool whose selling point is verification has to be verifiable.

---

### Sources worth bookmarking

- MuckRock — FOIA filing, tracking, and the DocumentCloud corpus
- governmentattic.org — released-document archive
- foia.gov — federal portal and agency contacts
- NARA OGIS — ombudsman, mediation, annual reports
- unitedstates/congress — public-domain congressional data scrapers
- regulations.gov — rulemaking dockets and comment API
- Congressional Management Foundation — the actual research, cited properly