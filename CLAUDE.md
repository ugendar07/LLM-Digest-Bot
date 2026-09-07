# AI Industry Token Bites  Bot

## What this project is
A weekly automated pipeline that collects LLM-industry news (research papers,
official lab blogs, HN, Reddit), uses an LLM to curate + summarize the top
items, and emails a formatted digest to the owner's Gmail. Runs on a free
weekly cron via GitHub Actions.

## Hard constraint — READ FIRST
**Everything in this project must be free.** No paid APIs, no paid hosting,
no paid tiers of anything. Before adding any new dependency, source, or
service, check that it has a genuinely free tier with no credit card /
trial-expiry trap. If in doubt, ask before adding it.

Approved free stack:
- Sources: RSS (blogs), arXiv API, HN Algolia API — all free, no key.
  Reality as of 2026-09-06 (see PLAN.md Component 1):
  - Reddit blocks keyless server traffic now → `.rss` is best-effort, often []
  - Anthropic dropped its RSS → lightweight scrape of the /news index
  - Meta AI has no feed and won't scrape → best-effort Google News RSS
  - Google News RSS (news.google.com/rss/search) added as a keyless fallback
- LLM curation: Groq free tier via LiteLLM (already used in owner's other projects — ISAT/BIKAS)
- Email: Gmail SMTP via App Password (smtplib) — free, no Gmail API OAuth needed
- Scheduling: GitHub Actions cron on a private repo — free minutes tier
- State/dedup storage: a flat JSON file committed back to the repo each run

## Architecture (4 components — build and test in this order)

1. **Collector** (`src/collector.py`)
   Pulls raw items from all sources, normalizes to:
   `{title, url, source, snippet, published_date}`. Dedupes near-identical
   stories (same URL, or same story from multiple sources). Writes
   `data/raw_this_week.json`. No LLM calls in this step — pure fetching/parsing.

2. **Curator** (`src/curator.py`)
   Sends the deduped batch to Groq (via LiteLLM), asks for structured JSON
   output: clustered stories, ranked by significance, grouped into categories
   (Model Releases, Research, Tooling/Infra, Industry/Business, Community
   Discourse). Each item gets: 3-4 sentence summary, "why it matters" line,
   estimated reading time. Writes `data/curated_digest.json`.

3. **Digest builder** (`src/digest_builder.py`)
   Turns `curated_digest.json` into a clean HTML email body. Pure templating,
   no external calls.

4. **Mailer** (`src/mailer.py`)
   Sends the HTML digest via Gmail SMTP + App Password. Reads secrets from
   environment variables, never hardcoded.

Orchestration: `src/run_weekly.py` calls all four in sequence. This is what
the GitHub Actions workflow invokes.

## Current build phase
- [x] Component 1: Collector — all 9 sources wired, full run clean (~150
      items/week). Reddit is best-effort (keyless access now blocked); Meta
      AI has no viable free feed and leans on HN/Reddit coverage. See PLAN.md.
- [x] Component 2: Curator — two-pass (score → cluster/select → write-ups)
      via Groq/LiteLLM, prompts in `src/prompts/*.md`. ~6.5 min/run (Groq
      8000 TPM free limit paces ~13 calls); budget the Actions job well
      above that. `gpt-oss-120b` primary, `qwen/qwen3.8-27b` fallback.
      Writes `data/curated_digest.json`. See PLAN.md.
- [ ] Component 3: Digest builder
- [ ] Component 4: Mailer
- [ ] GitHub Actions workflow + secrets wiring
- [ ] First live run + sanity check on real inbox

(Update this checklist as phases complete — this is the single source of
truth for "where are we" across sessions.)

## Folder structure
```
llm-digest-bot/
├── CLAUDE.md
├── PLAN.md                  # detailed working notes per component
├── .env.example             # documents required secrets, never commit real .env
├── .gitignore
├── requirements.txt
├── src/
│   ├── collector.py
│   ├── curator.py
│   ├── digest_builder.py
│   ├── mailer.py
│   └── run_weekly.py
├── data/
│   ├── seen_urls.json       # persisted dedup state, committed each run
│   ├── raw_this_week.json   # intermediate, gitignored
│   └── curated_digest.json  # intermediate, gitignored
└── .github/workflows/
    └── weekly_digest.yml
```

## Secrets (never hardcode, always env vars)
- `GROQ_API_KEY`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `DIGEST_RECIPIENT` (usually same as GMAIL_ADDRESS)

Locally: put these in `.env` (gitignored) and load with `python-dotenv`.
In production: GitHub Actions repo secrets, referenced in the workflow YAML.

## Conventions
- Python 3.11+, keep dependencies minimal (`feedparser`, `requests`,
  `python-dotenv`, `litellm` — check requirements.txt before adding more)
- Each component script should be runnable standalone for testing
  (`python src/collector.py` should work in isolation and print/save output)
- No component should silently swallow errors — log and continue where
  possible (a single broken RSS feed shouldn't kill the whole run), but
  surface failures clearly in the run log
- Keep the curator prompt in a separate constant/file, not inline buried in
  logic — it will need iteration
- Own preference: the owner (Ugender) works daily with retries/fallbacks/
  timeouts/structured logging in production systems (ISAT/BIKAS) — apply the
  same discipline here even though this is a small side project: wrap each
  source fetch in try/except, timeout every network call, log what was
  skipped and why

## Not in scope (unless explicitly asked)
- Twitter/X monitoring (no free API)
- YouTube video ingestion
- Real-time/instant alerts (this is a weekly batch job)
- A web UI / dashboard — email is the only interface
