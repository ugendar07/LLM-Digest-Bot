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
- [x] Component 3: Digest builder — `src/digest_builder.py`, pure inline-style
      HTML templating from `data/curated_digest.json`, no external calls.
- [x] Component 4: Mailer — `src/mailer.py`, Gmail SMTP + App Password via
      smtplib, secrets from env. Verified with a real send.
- [x] `run_weekly.py` runs all four end-to-end in ~6.8 min, real email
      confirmed.
- [x] GitHub Actions workflow + secrets wiring — `.github/workflows/
      weekly_digest.yml`, cron `30 2 * * 1` (Mon 08:00 IST) +
      `workflow_dispatch`. Four separate repo secrets. See PLAN.md.
- [x] First live run + sanity check on real inbox — manual `workflow_dispatch`
      run verified fully green (2026-09-07), digest delivered,
      `seen_urls.json` auto-committed by the job.

**Status: fully deployed.** Next real event is the first scheduled Monday
run, which fires on its own. See "Operating this project" below.

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

## Operating this project

The bot runs itself — GitHub Actions cron `30 2 * * 1` (Mon 08:00 IST) invokes
`python src/run_weekly.py`. Normally there is nothing to do. This section is
for when you need to intervene.

### Manually trigger a run
GitHub repo → **Actions** tab → **Weekly digest** (left sidebar) → **Run
workflow** button (top right) → keep branch `main` → **Run workflow**.
Use this for an off-cycle digest or to re-test after a fix. The `concurrency`
group means a manual run won't collide with the scheduled one.

### Where to check logs
- **Actions tab** → click the run → click the **`digest`** job → expand steps.
- The **"Run weekly pipeline"** step has the full pipeline log (collector
  per-source lines, curator progress, mailer message-id, final `run OK` /
  `RUN FAILED` banner).
- A green ✓ job + the digest email in the inbox = success. A red ✗ job = a
  stage failed and **no email was sent**; `seen_urls.json` is left unchanged
  so nothing is lost — fix the cause and re-run manually.
- Exit 0 with `nothing new since last week — no digest sent` is a valid
  no-op (only expected on a re-run inside the same week).

### Secrets
Four **separate** repo secrets (Settings → Secrets and variables → Actions):
`GROQ_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_RECIPIENT`.
They are write-only once saved — to rotate, overwrite the value. A single
bundled secret does **not** work; the workflow reads each by name.

### If a run fails — triage by the `RUN FAILED:` line

| Symptom in the log | Cause | Fix |
|---|---|---|
| `curation failed: ... rate limit` / `429` / Groq TPM errors | Groq free tier (8000 TPM) contended or daily cap hit | Just re-run later — usually transient. The curator already does model fallback + repair retries; a hard fail means Groq was unavailable for the whole run. If it recurs weekly, move the cron off a busy UTC slot. |
| `curation failed: ... authentication` / `invalid api key` | `GROQ_API_KEY` wrong or revoked | Regenerate at console.groq.com, overwrite the repo secret, re-run. |
| `collector returned 0 items from every source` | Network blip or many feeds down at once | Re-run. Single dead feeds are logged as `skipped` and don't fail the run — a total-zero means something broad. Check the per-source lines for a pattern. |
| A source consistently `skipped` (esp. Reddit / Meta AI) | Expected — those are best-effort, keyless access is blocked | No action. See PLAN.md Component 1 for the OAuth upgrade path if coverage feels thin. |
| `send failed:` / `mailer crashed:` / SMTP auth error | Gmail App Password expired/revoked, or 2FA change | Generate a new App Password (Google Account → Security → App passwords), overwrite `GMAIL_APP_PASSWORD`, re-run. |
| `mailer config missing: X` | A secret is unset or misnamed | Re-add it with the exact name. |
| Job hits `timeout-minutes: 20` | A stage hung (usually a network call with no response) | Re-run. If it repeats, check which stage the log stops at. |

### Tuning knobs (see PLAN.md for detail)
- HN score threshold: `HN_MIN_POINTS` in `src/collector.py`.
- Keyword list: `KEYWORDS` in `src/collector.py` — add new model families as
  they ship.
- Curator category balance / Research floor: `_enforce_balance()` in
  `src/curator.py`.

## Not in scope (unless explicitly asked)
- Twitter/X monitoring (no free API)
- YouTube video ingestion
- Real-time/instant alerts (this is a weekly batch job)
- A web UI / dashboard — email is the only interface
