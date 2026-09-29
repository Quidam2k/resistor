# Voice loop: how Jarvis runs the 10 AM Resistor block

## HARD RULE: approval is Todd's click, never his voice

Nothing is sent because of anything said out loud. A spoken "yes", "send it", or
"approved" does **not** approve a letter. The only way a letter goes out is
Todd clicking **Approve** in the approval web UI (`python -m src.webui`,
http://127.0.0.1:5317). The interpreter has no approve or send command, and
Jarvis never POSTs to the approve endpoint. This is Todd's design from
2026-07-19 and must not be removed or worked around. The reason: a spoken
yes would put a transcription between Todd and a real fax, and a misread
message could fire a real letter.

## The loop

| Step | Command | What Jarvis does |
|---|---|---|
| List | `python -m src.interpreter actions [--refresh]` | Reads the numbered menu aloud, grouped (open asks, follow-ups, redrafts, recent votes, already queued). `--refresh` re-checks open asks against Congress.gov first. |
| Pick | `python -m src.interpreter pick N [--recipient "Ron Wyden"]` | Todd says a number. Jarvis reads the brief: sources, channel, cost, prior letters and replies. |
| Draft | `python -m src.interpreter draft N --body-file F [--channel mail]` | Jarvis writes the letter with Todd, fact-checks every claim on the web, then saves. The lint blocks em dashes and a missing name or address. It warns when a bill is not in the action's sources. |
| Queue | `python -m src.interpreter queue <md>` | Puts the letter in `pending_approval` and prints the approval URL. For mail, it attaches a free Lob TEST render. |
| Approve | (web UI) | **Todd clicks.** Jarvis starts the UI in the background if needed and hands over the URL. |
| Status | `python -m src.interpreter status ID` | Confirms sent or failed after the click. |
| Log | `python -m src.interpreter log --notes "..."` | Records the session row and `data/sessions/<date>-voice-block.md`, then prints today's Resistor goal id. Jarvis ticks it with `mcp__pantheon__complete_goal`; the tool never does. |

Drafting rules still apply: one topic, no em dashes, invite a reply with the
rep's reasoning, reference prior correspondence. Any vote or bill claim cites
the `voting_records#` or `asks#` row from the brief, never model memory.

## Rehearsing without sending

```
WEBUI_DRY_RUN=1 RESIST_DATA_DIR=<scratch dir> LOB_API_KEY= NOTIFYRE_API_KEY= \
SMTP_USER= SMTP_PASSWORD= python scripts/rehearse_voice_loop.py \
    --pick 1 --recipient "Val Hoyle" --channel mail --body-file letter.txt
```

The rehearsal refuses to run unless all of those are set, with the live keys
present but empty. It copies `resist.db` to the scratch dir and allows only
Lob TEST-key POSTs. The approve click is simulated through the UI's own
endpoint. It then deletes the rehearsal letter and checks that the live DB
hash did not change. `python -m pytest tests` runs the same guarantees as
tests.
