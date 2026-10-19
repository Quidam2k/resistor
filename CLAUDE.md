# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Resistor** is a civic engagement force multiplier powered by Claude Code. Instead of a generic texting service, this is a conversational workflow between a user and Claude that:

1. **Researches** recent news since the last session (weekly cadence)
2. **Discusses** issues with the user to identify what matters most and what's actionable
3. **Composes** focused, original letters to the user's elected representatives
4. **Delivers** those letters (currently manual, with Playwright automation planned)
5. **Tracks** all correspondence, responses, and context to build continuity over time

The goal is to make civic engagement low-friction enough that you actually do it consistently. Every letter should be original, focused on one topic, and informed by the full history of prior correspondence — including any responses received from representatives.

## Your Representatives

See `config/representatives.yaml` for the full list with contact URLs. This file should contain your elected officials at every level — federal, state, and local. See the included example for the expected format.

Personal info goes in `config/user.yaml` (gitignored). This must include your name, address, city, state, and zip — staffers use this to verify constituent status.

## Political Context

Research should prioritize **factual, minimally biased sources** — AP, Reuters, and similar. Users may be passionate and opinionated, but the research feeding their letters must be solid. Stick to verifiable facts. The user's `config/user.yaml` and conversation history establish their political perspective — adapt accordingly, but never fabricate or cherry-pick research to confirm bias.

## Tech Stack

- **Python** — primary language
- **SQLite** (`data/resist.db`) — structured storage for correspondence history, representative info, topic tracking
- **Markdown files** — human-readable session notes and letter archives
- Both storage systems are used: SQLite for querying, markdown for readability

## Project Structure

```
config/
  user.yaml              # Your personal info (GITIGNORED)
  representatives.yaml   # Elected officials and contact info
data/
  resist.db              # SQLite database (gitignored)
  sessions/              # Markdown notes from each session
  letters/               # Sent correspondence as markdown
  responses/             # Responses received from representatives
src/                     # Python source code
```

## Session Workflow

A typical session follows this flow:

1. **News research** — Search for significant events since last session, focusing on federal policy, legislation, executive actions, and Oregon-specific issues
2. **Discussion** — Present findings to the user, discuss what's most actionable and where constituent letters could make a difference
3. **Composition** — Draft focused letters (one topic per letter) incorporating:
   - Prior correspondence history with that representative
   - Any responses received (quote them for continuity)
   - Specific asks or positions, not just venting
4. **Review** — Show the user the full text for approval before sending
5. **Delivery** — Send via the best available channel
6. **Recording** — Save the session notes, letter text, and delivery status

## Delivery Strategy (In Progress)

Congressional offices primarily use **web forms** — not direct email. The plan:

Delivery channels are configured per-representative in `src/delivery/router.py`:

- **Fax** (Notifyre API, ~$0.03/page) — primary channel for federal senators
- **Email** (SMTP) — for state/local reps with direct email addresses
- **Web forms** (Playwright, semi-automated) — fallback for reps with no fax/email
- **Physical mail** (Lob API, ~$1-3/letter) — for Supreme Court and high-impact letters

Route opportunistically — use whatever channel is fastest and most automatable for each rep. Thread email responses for continuity.

## Letter Writing Guidelines

- **One topic per letter** — staffers track by topic, so bundling reduces impact
- **Original composition always** — our advantage over form letters is that each one is unique and thoughtful
- **Include constituent info** — name and address at top to verify constituent status
- **Reference prior correspondence** — "In my letter of [date], I wrote about X. Your office responded that Y. I'm following up because Z." This kind of continuity is rare and gets attention.
- **Be specific** — reference bill numbers, specific actions, specific asks
- **Assertive but respectful tone** — passionate is fine, abusive is counterproductive
- **No claims about Todd's feelings, reactions, presence, or continuous actions** (#8364) — never write "I can't stop thinking about it", "heartbroken", "I keep...", "ever since...", "I was there/attended/marched" unless Todd wrote that exact sentence himself. The interpreter lint blocks these; his own sentences pass via `draft --todd-wrote "<exact sentence>"`.
- **Don't recount the public record** (#8364) — name the event ("the Bend school shooting"); no death tolls, ages, or blow-by-blow. Staffers already know.
- **Be succinct** (#8364) — Todd reads every letter himself; aim for ~200 words.
- **Multi-recipient batches: one shared body + one personal paragraph each** (#8419) — identical shared paragraphs in every letter, plus ONE genuinely member-specific paragraph per rep (their committee, cosponsor status, past vote or statement, and their ask); greeting is per letter. Build the review card with `python -m src.review_card --ids A,B,C` (shows shared text once, verifies byte-for-byte reassembly; `--attach` adds approve buttons).

## Important Behavioral Notes

- **Never send anything without the user's explicit approval of the full text**
- **Fact-check every letter** after drafting but before sending — verify all statistics, dates, vote counts, quotes, and bill numbers against web sources. Present verification results before requesting send approval.
- News research must cite sources and prefer AP/Reuters-tier factual reporting
- Track everything — the accumulated context is what makes this more effective over time
- When composing for allies, frame letters as constituent support/pressure for specific actions, not complaints
- Look for opportunities beyond letters: public comment periods, town halls, other engagement methods
- Periodically remind the user about the Supreme Court physical mail option
