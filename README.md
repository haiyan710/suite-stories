# Suite Stories: a multi-agent editorial pipeline for luxury hotels

A Slack copilot that turns "research this hotel and write about it" into a
finished promotional editorial: a verified evidence package, the case for
booking the hotel, a day-by-day itinerary on real dates, a sample article and
magazine-style HTML layouts built with the hotel's own photos.

It is built for a small content team that writes for luxury travellers who
compare their options closely and book when they can see why a hotel fits the trip.
The pipeline's job is to find the property-specific details a listing leaves
out (how the service works, which room to ask for, the best time for the pool,
the hotel's stories), prove each one with a source, and write them up in a
calm, concrete voice that sells without hype.

- Python 3.12, Slack Bolt (Socket Mode), SQLite
- One process with one in-process worker queue. No broker, no microservices.
- Pluggable LLM backend: the local Claude Code CLI (runs on a logged-in Claude
  account, no API key) or GLM through Z.ai's Anthropic-compatible API
- Free OpenStreetMap services for travel times (Photon geocoding, OSRM routing)

---

## What it produces

For a request such as `@bot research Park Hyatt Bangkok for a honeymoon couple`
the bot posts its progress as a log in the Slack thread and ends with:

1. **A writing outline**: headline options, the angle, why readers will want
   it, a day-by-day story outline, proof points, claims to avoid and what to
   check with the hotel. A "Getting around" block gives estimated travel times.
2. **A sample draft** (on request): headline `place line | Hotel`, a one-line
   standfirst, a Day 1 info line (check-in, room to book, area), timed moments
   for each day, `Insider notes` and `Before you go`. Missing details are
   marked `[TK: …]` instead of being invented.
3. **HTML layouts** (on request): self-contained magazine pages built from
   the draft in one of five styles: classic, side by side (photo and text
   alternating), photo first (full-width chapter photos), photos inside the
   text, or a hybrid of the last two. Photos the hotel shared in Slack (PDF
   decks or plain JPG/PNG/TIFF files) are captioned and placed automatically;
   photos of spaces closed on the trip dates close the page as a "when you
   come back" strip.
4. **Options** (on request): "give me 3 options" returns clearly different
   angles (A, B, C) in one thread; the team can switch between them, draft
   any of them, or combine two ("B's structure with C's voice").

Each step is checked by a critic before the team sees it, and every fact in
the output can be traced to a stored source. The copy itself reads as direct
recommendations: sources stay in the database, not in the text. Where only the
hotel can answer (a dish from the winter menu, a group table), the draft
carries an `[ASK <contact>: …]` note for the editor.

### Example output

[`examples/park-hyatt-bangkok/`](examples/park-hyatt-bangkok/) holds a real run
for a 3-night honeymoon at Park Hyatt Bangkok, written from public evidence
only:

- [`draft.md`](examples/park-hyatt-bangkok/draft.md): the sample draft after
  two QA rounds, the final fixes and the wording polish (verdict `PASS`, with
  the `[TK]` items left for the hotel to confirm)
- [`layout.html`](examples/park-hyatt-bangkok/layout.html): the same draft as a
  magazine layout. No hotel photos were shared for this run, so every section
  has an empty photo slot with a shot brief. Open it in a browser to drop
  images in.

---

## Agents

The system has **one orchestrator and seven specialist agents**. Each
specialist has a stable system prompt and a narrow role, and receives a typed
task brief from the orchestrator for each call.

| # | Agent | File | Role | Never does |
|---|---|---|---|---|
| 0 | **Orchestrator** (`EditorialOrchestrator`) | `app/orchestration/orchestrator.py` | Classifies intent, plans, delegates, inspects each result, retries within budgets, writes the reply | Browse or write copy itself |
| 1 | **ResearchAgent** | `app/agents/researcher.py` | Web and document evidence: official pages, press, guides, guest reviews, hotel-shared PDFs; classifies each fact by confidence | Propose concepts |
| 2 | **EditorialAgent** | `app/agents/editorial.py` | Turns gated evidence into an experience-led concept: the booking case plus the itinerary | Browse the web |
| 3 | **CriticAgent** | `app/agents/critic.py` | QA in three modes: `EDITORIAL` (concept), `DRAFT` (sample article), `VISUAL` (mockup) | Rewrite the work |
| 4 | **LogisticsAgent** | `app/agents/logistics.py` | Geocodes each day's stops and estimates car and walking times with time-of-day traffic; flags legs that do not fit and days with too much transit | Quote estimates as facts |
| 5 | **WriterAgent** | `app/agents/writer.py` | Writes the sample draft, removes AI-writing tells (`humanize`) and applies the critic's last fixes as exact edits (`apply_fixes`) | Add facts not in the evidence |
| 6 | **DesignAgent** | `app/agents/designer.py` | Visual direction spec for a concept | Write HTML |
| 7 | **BuilderAgent** | `app/agents/builder.py` | Versioned static HTML/CSS mockups | Overwrite an earlier version |

Smaller single-call steps run on the fast model and are not separate agents:
reply routing, outline condensation (`outline.md`), dinner picks from the
evidence (`dining_picks.md`), stop notes (`venue_notes.md`: why each place is
worth the visit, closed days, a dish, which hotel spaces are open on the trip
dates), critique summaries, stop extraction for logistics, and photo placement
for layouts (`layout_images.md`).

Two steps are plain Python, with no model at all:

- **Trip calendar** (`orchestration/calendar.py`): dates and weekdays come
  from the request ("6-10 Feb 2027") or a Korean holiday and year ("Seollal
  2027", with substitute holidays). Every agent gets the same calendar, and it
  overrides any date a model wrote earlier.
- **Draft checks** (`orchestration/draft_checks.py`): before and after each
  critic pass, the day headings get their real dates, `[TK]` notes get the
  right weekday, and wrong dates, banned phrases, a stop on its closed day, a
  closed hotel space written into the stay, or an implausible car time become
  revision instructions.

### Flow

```
Slack thread
   │
SlackEventRouter ──► AgentExecutor (worker thread, progress log, cancellation)
                         │
                 EditorialOrchestrator
                         │  intent → plan → delegate → inspect → retry / continue
   ┌─────────────────────┼───────────────────────────────────────────────┐
   │ research            │ concept                     │ draft             │
   ▼                     ▼                             ▼                   │
ResearchAgent ──► evidence gate ──► EditorialAgent ──► CriticAgent        │
 trip calendar ─────────────────────────┘ (dates for every agent)          │
 (official, press,   (only verified   (booking case +   (EDITORIAL)        │
  guest reviews,      facts reach      itinerary)          │               │
  venue research)     editorial)            ▲   retry ◄────┘               │
                                            │                              │
                         LogisticsAgent + stop notes ──► outline ──► Slack │
                                                                           │
   WriterAgent ──► humanize ──► draft checks + CriticAgent (DRAFT) ──► revise ◄┘
        │                              │
        └── apply_fixes ◄──────────────┘ final fixes + re-check (max 2), then polish
                │
          layouts (HTML, 1-4 styles) ──► Slack file upload
                         │
                  SQLite: projects, sources, fact cache, concepts,
                  versions, workflow trace, Slack event dedup
```

Budgets that bound every run: at most 2 targeted research rounds, 2 editorial
attempts (plus one repair-only pass), 2 draft QA rounds, 2 final fix passes
and 2 visual revision cycles.

---

## Design principles

- **Evidence before creativity.** Facts are classified as `verified_official`,
  `verified_authoritative_secondary`, `corroborated_secondary`,
  `search_snippet_only`, `inferred` or `unsupported`. Only the first three
  reach the editorial agent, and a central claim resting on weaker evidence
  cannot be shortlisted.
- **Provenance in Python, not in the model.** The app assigns source ids,
  renders source lists from the database and checks citations in model output.
  Search hits are marked `grounded` only when they came from a live search.
- **The experience is the unit of ideation.** A concept fails if swapping in
  another hotel's name keeps about 80% of it. Superlatives, "only hotel" and
  "minutes away" claims need evidence.
- **Promotional, not a review.** Drawbacks appear only as helpful tips.
  Booking-platform perks are never presented as the hotel's own offers.
- **Recommendations, not citations.** The copy never names its sources ("one
  reviewer", "per the hotel", a publication): claims read as direct advice,
  and the writer's claim-to-source map stays with the editor. Guest reports
  give texture and tips, never name anyone, and never alone support an hour,
  a price or the booking case.
- **House style the team can edit.** Rules and banned phrases live in the
  database and are changed from Slack (`style rule: …`, `banned phrase: …`);
  the writer, the critics and the draft checks all read them.
- **Dates are computed, not written.** A model once put a holiday on the wrong
  weekday and every later step copied it; dates now come from Python, and a
  correction in the thread overrides the stored concept.
- **Plain, translatable English.** Drafts are written to be translated, so the
  writer and humanizer avoid idioms. The humanizer removes AI-writing tells
  (patterns adapted from blader/humanizer and mikiarlo3/ai-copywriter) and
  must keep every fact, `[TK]` and subhead.
- **Traveller segments.** Couples, families, slow travel, wellness, design and
  culture, golf and food-led trips each set the activities to favour, a
  transit limit and the voice (about 60% information, 40% the traveller's
  voice). A request for several types, or for every reader, becomes a mixed
  party: shared meals, and days that split give each group its own line, with
  the slower option for grandparents.
- **Memory against repetition.** Shortlisted concepts are stored and fed back
  to the editorial agent and the critic, so new pieces do not reuse old angles.
  Test threads (`[test]`) are kept out of this memory.
- **Verified-fact cache.** Facts verified for a hotel are reused for later
  threads within `FACT_CACHE_DAYS`.
- **Hotel material stays private.** Hotel-shared documents and photos are
  git-ignored. When they are present, only Claude writes the draft; GLM sees
  public evidence only.
- **Methodology lives in prompts.** Editorial behaviour is in
  `app/prompts/*.md` (22 files). Tune those files before adding code.

---

## Project structure

```
app/
  __main__.py              entry point (single-instance lock)
  slack_app.py             Slack Bolt Socket Mode: allowlist, threads, worker queue,
                           progress log, missed-message catch-up, socket watchdog
  config.py                Settings / load_settings (the only place env is read)
  formatting.py            Markdown → Slack mrkdwn, message splitting, sources, citations
  orchestration/
    orchestrator.py        the conversational brain: intents, plans, gates, retries, replies
    intents.py             Intent vocabulary
    handoffs.py            typed task/result dataclasses between agents
    planning.py            plans, task briefs, evidence confidence, research compaction
    calendar.py            trip dates, weekdays and Korean holidays, computed in Python
    draft_checks.py        deterministic draft fixes and checks (dates, house style, closures)
    golden.py              non-leaking quality evaluator for the golden test
  agents/                  the seven specialists (see table above) + base protocol
  llm/
    base.py                LLMProvider protocol (complete, complete_with_search)
    __init__.py            build_provider(settings, role=...): the only constructor
    claude_cli.py          Claude Code CLI provider (no API key, stripped child env)
    zai_glm.py             GLM via Z.ai with server-side web search
    fallback.py            GLM → Claude fallback on errors, quota or empty answers
  tools/
    web.py                 search, source typing, guide/press discovery
    pages.py               fetch, URL normalisation, HTML → evidence text
    documents.py           Slack file download, PDF text, photo files and PDF photos, captions
    geo.py                 Photon geocoding + OSRM routing, cached
    assets.py              image inventory
  artifacts/
    manager.py             append-only versioned mockups (vNNN), restore by pointer
    layout.py              classic magazine layout with photo slots
    layout_styles.py       side-by-side, photo-first, inline and hybrid layouts
  storage/
    schema.py, db.py       SQLite schema (in-place migrations) and data access
  prompts/                 methodology, one markdown file per role or step
  agent.py                 deprecated single-agent implementation (kept for imports/tests)
scripts/
  chat_cli.py              run the same pipeline in a terminal, no Slack
  run_golden.py            live golden regression (JSON report)
deploy/launchd/            macOS launchd agent: start at login, restart on exit
examples/                  sample output (draft + layout) from a real run
tests/                     offline tests with fake LLM providers and fetchers
```

---

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in the Slack tokens and allowed user ids
```

**Slack app**: create an app with Socket Mode on (app token with
`connections:write`). Bot scopes: `app_mentions:read`, `chat:write`,
`im:history`, `reactions:write`, `channels:history`, `groups:history`,
`files:read` (shared PDFs and photos) and `files:write` (layout upload). Events:
`app_mention`, `message.im`, `message.channels`, `message.groups`. Invite the
bot to the channel. A thread starts with an @mention; after that, allowlisted
users can reply without mentioning it.

**LLM backend** (`LLM_PROVIDER`):

- `claude_cli` runs `claude -p` under the Claude account logged in on the
  machine. `ANTHROPIC_API_KEY` is stripped from the child process, so it
  never switches to API billing. The researcher uses `CLAUDE_CLI_RESEARCH_MODEL`
  and the other agents use `CLAUDE_CLI_MODEL`.
- `zai` uses GLM through Z.ai (`ZAI_API_KEY`). `WRITER_PROVIDER=zai` lets GLM
  write drafts from public evidence, with a Claude fallback.

All options and their defaults are listed in `.env.example`.

## Run

```bash
.venv/bin/python -m app                 # Slack bot
.venv/bin/python scripts/chat_cli.py    # same pipeline in the terminal
```

Only one bot process may run (`data/bot.lock`). On macOS the launchd agent in
`deploy/launchd/` keeps it running. The plist is a template; install it from
the project root:

```bash
sed -e "s#__PROJECT_DIR__#$PWD#g" -e "s#__HOME__#$HOME#g" \
    deploy/launchd/com.haiyen.suite-stories-bot.plist \
    > ~/Library/LaunchAgents/com.haiyen.suite-stories-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.haiyen.suite-stories-bot.plist
```

Restart it after a code change with
`launchctl kickstart -k gui/$(id -u)/com.haiyen.suite-stories-bot`.

## Usage

```
@bot research <hotel> for a honeymoon couple        → outline + Getting around
@bot research <hotel>, 4-night trip for a family    → itinerary of that length
@bot research <hotel>, 4 nights over Seollal 2027,
     for all traveller groups, 3 options            → options A-C on real dates, mixed party
use option B / combine B and C: <what from each>    → switch or merge options
write a sample draft                                → draft with QA verdict
write a sample draft. <correction>                  → the correction overrides the stored concept
make the layout / side by side / all four layouts   → HTML layout files in the thread
<attach the hotel's PDF deck or photo files>        → facts and photos added as hotel evidence
Hotel confirmed: <pasted reply>                     → confirms open claims and re-runs editorial
style rules / style rule: … / banned phrase: a | b  → house style, used by every draft
hotel contact: <name> / editor: <name>              → who the [ASK] notes are for
critique this / show workflow / use v1 again
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The 267 tests run offline with fake LLM providers and fetchers. The live
scripts in `scripts/` use the network and cost tokens.

## Limitations

- Many hotel chain sites block automated fetchers or render with JavaScript
  only. The researcher falls back to press and guides and does not bypass bot
  protection.
- Travel times are free-flow routing plus traffic multipliers, always written
  as "about". Train and boat legs are left to the timetable, and opening days
  come from the evidence (stop notes), not from a live opening-hours source.
- Korean holiday dates are a table that must be extended each year.
- Web images are not rights-cleared, so layouts use only hotel-supplied
  photos.
- The bot runs on one machine and works only while that machine is awake.

## License

© 2026 haiyan710. All rights reserved. Shared for portfolio viewing; no license
is granted to use or modify the code.

`app/prompts/humanizer.md` adapts patterns from two MIT-licensed projects;
their notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
