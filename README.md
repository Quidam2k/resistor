# Resistor

A civic engagement force multiplier powered by [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Resistor is a civic engagement force multiplier. It turns one person with 20 minutes a week into a constituent whose letters read like they came from a staffer — citing specific bill numbers, referencing voting records, building on prior correspondence, and arriving at the strategically right moment.

It doesn't just make civic engagement easier. It makes each contact dramatically more effective.

## What Does a Session Look Like?

You open your terminal, start Claude Code in this project, and say:

> "Let's do a session. What's happened this week?"

Claude researches headlines, checks for new congressional votes, and identifies what's actionable. You talk about what matters to you. Claude drafts a letter — one topic, one representative — citing specific bills, referencing your prior letters, pulling from their voting record. It fact-checks every claim against sources, shows you the verification, and on your approval, faxes it directly to the senator's office.

20 minutes. Multiple substantive letters. Delivered.

## Why This Matters

The research from the [Congressional Management Foundation](https://www.congressfoundation.org/) is clear:

- A **personalized letter is worth 1,000 form letters** to congressional staffers
- **Fewer than 50** personalized contacts can change an undecided member's position in 70% of offices
- The medium doesn't matter (email, fax, postal mail) — **what matters is that it's original, specific, and from a real constituent**
- **Referencing prior correspondence** and **citing specific bill numbers** signals sustained engagement that staffers can't ignore

One person using Resistor weekly generates the kind of informed, persistent constituent pressure that advocacy organizations spend millions trying to create. It's a block and tackle — mechanical advantage applied to democracy.

Resistor automates the tedious parts (research, delivery, record-keeping) so you can focus on the part that matters: having an informed opinion.

## Quick Start

### What You'll Need

1. **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — Anthropic's CLI tool. This is how you talk to Claude. Requires an Anthropic account ($20/month for Pro, which includes Claude Code).

2. **Python 3.10 or newer** — [Download here](https://www.python.org/downloads/) if you don't have it. To check: open a terminal and type `python --version`.

3. **A Congress.gov API key** (free) — For downloading voting records and bill text. Takes 30 seconds to get one at [api.congress.gov/sign-up](https://api.congress.gov/sign-up/).

4. **A Notifyre account** (for fax delivery) — Sign up at [notifyre.com](https://notifyre.com). Pre-fund with $10 (gets you ~333 faxes at $0.03 each). Grab your API key from the dashboard.

5. **Your representatives' info** — You'll need to know who represents you. Look it up at [congress.gov/members/find-your-member](https://www.congress.gov/members/find-your-member) for federal, and your state legislature's website for state reps.

### Step-by-Step Setup

**1. Get the code:**
```bash
git clone https://github.com/yourusername/resistor.git
cd resistor
pip install -r requirements.txt
```

**2. Set up your personal info:**
```bash
cp config/user.example.yaml config/user.yaml
```
Open `config/user.yaml` in any text editor and fill in your name, address, city, state, and zip. This is how representatives verify you're a real constituent. (This file is gitignored — it never leaves your computer.)

**3. Set up your representatives:**

Edit `config/representatives.yaml` with your elected officials. The included file has an example format showing federal senators, house reps, and state legislators. Replace them with yours.

**4. Add your API keys:**
```bash
cp .env.example .env
```
Open `.env` and paste in your API keys:
```
CONGRESS_API_KEY=your_key_here      # From api.congress.gov
NOTIFYRE_API_KEY=your_key_here      # From notifyre.com dashboard
```

(Email delivery keys are optional — add them later if your state reps have direct email.)

**5. Initialize the database:**
```bash
python -m src.db
```

**6. Download your representatives' voting records:**
```bash
python -m src.congress_api votes
python -m src.congress_api bills
```
This pulls every vote your reps have cast in the current Congress (usually 500-700+ each) and the full text of major bills. Takes a couple minutes.

**7. Start your first session:**
```bash
claude
```
That's it. You're in Claude Code, in the project directory. Claude reads the CLAUDE.md, sees your config, and knows what to do. Start with:

> "This is my first session. Can you walk me through how this works?"

### What Happens Next

Over time, Resistor gets more powerful:

- Your **correspondence database** grows. Claude references what you've already written: *"In my letter of March 17, I urged you to support S. 2845. I'm writing today because..."*
- **Voting records** update each session. Claude can cite how your rep voted on related bills.
- **Responses** from representatives get logged. If a staffer writes back, tell Claude and it'll weave their response into your next letter.

The accumulated context is what makes letter #10 dramatically more effective than letter #1.

## Project Structure

```
config/
  user.yaml                # Your name/address (GITIGNORED)
  user.example.yaml        # Template to copy
  representatives.yaml     # Your elected officials
data/                      # All user data (gitignored)
  letters/                 # Sent letters as markdown
  sessions/                # Session notes
  responses/               # Responses from representatives
  voting_records/           # Downloaded vote data
  bills/                   # Downloaded bill text
src/
  config.py                # Configuration loader
  db.py                    # SQLite database
  congress_api.py          # Congress.gov + GovTrack APIs
  letter.py                # Letter formatting and storage
  import_votes.py          # Vote record importer
  delivery/
    router.py              # Routes letters to the right channel
    email_sender.py        # SMTP email delivery
    fax_sender.py          # Notifyre fax API
```

## Delivery Channels

| Channel | Cost | Speed | Setup Effort | Best For |
|---------|------|-------|------------|----------|
| **Fax** (Notifyre) | ~$0.03/page | Minutes | Sign up, add API key | Federal reps (senators, house) |
| **Email** (SMTP) | Free | Instant | Need email account credentials | State/local reps with direct email |
| **Web form** (coming soon) | Free | Minutes | Semi-automated (you click one CAPTCHA) | Reps with no fax or email |
| **Physical mail** (Lob, coming soon) | ~$1-3/letter | Days | Sign up, add API key | Supreme Court, high-impact moments |

Most federal reps can be reached by fax. Most state reps have direct email. Between those two channels, you'll cover almost everyone.

## Frequently Asked Questions

**Do I need to know how to code?**
No. Once set up, you just talk to Claude in plain English. The setup involves copying files and pasting API keys — the instructions above walk you through every step.

**How much does it cost?**
- Claude Code: $20/month (Anthropic Pro)
- Faxes: ~$0.03 each (a session sending 4 faxes costs $0.12)
- Congress.gov API: Free
- Total: About $20/month, almost entirely the Claude subscription you may already have

**Won't my reps think I'm a bot?**
No. Every letter is original — composed in conversation between you and Claude, informed by your actual opinions and priorities. These read like thoughtful constituent mail because that's what they are. You approve every word before it sends.

**How is this different from Resistbot?**
Resistbot sends form-style messages. Resistor composes original letters that reference specific bills, cite voting records, and build on your prior correspondence. The research shows this kind of personalized engagement is orders of magnitude more effective.

**How often should I use it?**
Weekly is the sweet spot, according to congressional staffers. Different topic each week. Resistor makes this sustainable because Claude handles the research and drafting — you just need 20 minutes and an opinion.

**Does it work for [my state]?**
The federal features (Congress.gov API, fax delivery, voting records) work for any US state. State legislature integration currently has Oregon-specific code, but the architecture supports any state — contributions welcome.

## See Also

- **[ROADMAP.md](ROADMAP.md)** — Where this project is headed: CLI tools, direct API integration, multi-user coordination
- **[CLAUDE.md](CLAUDE.md)** — The instructions that teach Claude how to run sessions (read this if you want to understand or customize the workflow)

## Contributing

This project started as one person's civic engagement tool and is designed to be forked and personalized. PRs welcome, especially for:
- State legislature integrations beyond Oregon
- New delivery channel drivers
- Improvements to the session workflow in CLAUDE.md

## License

MIT
