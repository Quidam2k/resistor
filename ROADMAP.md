# Resistor Roadmap

## Phase A: Claude Code Project (Current)

Users clone the repo, configure their reps and API keys, and run sessions in Claude Code. The CLAUDE.md teaches Claude how to orchestrate everything.

**Status: Working.** Fax delivery, voting record tracking, bill text downloads, correspondence database, fact-check workflow all functional.

### Remaining Phase A work
- [ ] Email delivery (SMTP setup for state/local reps)
- [ ] Semi-automated Playwright for web forms (user clicks CAPTCHA, script fills the rest)
- [ ] Lob API integration for physical mail (Supreme Court, high-impact letters)
- [ ] Setup wizard script that walks new users through configuration
- [ ] Example config files with clear documentation
- [ ] Look up user's city council ward from address
- [ ] Session note auto-generation at end of each session

---

## Phase B: CLI Tools

Standalone commands for the mechanical parts. The conversational session stays in Claude Code.

```
resist status          # Show correspondence history, upcoming votes, last session date
resist votes update    # Download latest voting records from GovTrack
resist bills download  # Download bill text for tracked legislation
resist send <file>     # Send a letter via its configured channel
resist research        # Pull latest news headlines relevant to user's priorities
resist history <rep>   # Show all correspondence with a specific representative
```

### Why
- Lets users run maintenance tasks without a full Claude session
- Makes the project useful even between weekly sessions
- `resist send` means letters composed in any editor can be delivered

---

## Phase C: Claude API Integration

Replace the "Claude Code as interface" dependency with direct Anthropic API calls.

```
resist session         # Interactive session: research → discuss → compose → send
resist compose <topic> # Draft a letter on a topic using full context
resist factcheck <file> # Verify all claims in a draft letter
```

### Why
- Works without Claude Code subscription
- Session workflow becomes reproducible and scriptable
- Users can choose their model (Opus for sessions, Haiku for fact-checks)
- Opens the door to scheduled/automated research runs

### Architecture
- `src/session.py` — orchestrates the research → discuss → compose → review → send flow
- `src/prompts/` — system prompts for each phase (research, composition, fact-check)
- `src/context.py` — builds context window from correspondence history, voting records, bill text
- User provides `ANTHROPIC_API_KEY` in `.env`

---

## Phase D: Multi-User / Community

- Web interface for non-technical users
- Shared research across users in the same district (privacy-preserving)
- Coordination: "10 people in OR-4 wrote about the PRO Act this week"
- Public dashboard showing aggregate civic engagement metrics
- Integration with 5 Calls, Resistbot, and other civic tech platforms

### Why
- The research says fewer than 50 personalized letters can move an undecided member
- A coordinated group using Resistor could be those 50 people
- The strategic timing and research benefits multiply with more users

---

## Design Principles (All Phases)

1. **Original composition always** — never send form letters. The entire value is that each letter is unique, informed, and specific.
2. **Fact-check before send** — every claim verified against sources. Credibility is the currency.
3. **Track everything** — correspondence history is what makes letter #10 more powerful than letter #1.
4. **Low friction** — if it's hard, people won't do it. Automate everything that doesn't require human judgment.
5. **Unbiased research, opinionated letters** — research from AP/Reuters, letters from the heart.
6. **Privacy first** — personal info stays local. No telemetry, no shared databases (until Phase D, opt-in).
