# Build plan — working notes

High-level phase checklist lives in CLAUDE.md. This file is for
component-level detail, open questions, and decisions made along the way.

## Component 1: Collector
Status: DONE (2026-09-06) — all 9 sources wired, full run clean (~151 items).

Sources implemented (in `src/collector.py`, registered in `SOURCES`):
- [x] arXiv API — `cs.CL OR cs.AI`, newest ~300 scanned (3 pages), TITLE
      keyword-match, keep 40 most recent. Full 7-day pagination is pointless
      given arXiv volume + the keep-cap, so we don't. `fetch_arxiv`
- [x] HN via Algolia API — 14 LLM queries, `points>=50`, 7-day window,
      TITLE keyword-match, dedup by story id, sorted by points. `fetch_hn`
- [x] Reddit — r/LocalLLaMA + r/MachineLearning, `top.rss?t=week`.
      **Best-effort only**: Reddit now 403/429s keyless server traffic.
      Tries old.reddit.com then www.reddit.com; degrades to [] when blocked.
      `fetch_reddit`
- [x] OpenAI blog — `https://openai.com/news/rss.xml` (blog/rss.xml redirects
      here). `fetch_openai_blog`
- [x] Anthropic — **no RSS anymore** (all paths 404). Lightweight scrape of
      `/news` index: `<time>` dates off the cards + og:title/og:description
      per article. `fetch_anthropic_blog`
- [x] Google DeepMind — `https://deepmind.google/blog/rss.xml` (feed has no
      `<description>`, so snippets are enriched from og:description).
      `fetch_deepmind_blog`
- [x] Meta AI — **no RSS, not scrapeable** (JS-rendered, only ~4 stale posts
      server-side). Best-effort Google News RSS `site:ai.meta.com/blog` +
      own recency filter. Yields nothing most weeks — Meta releases come
      through HN/Reddit. `fetch_meta_ai_blog`
- [x] Mistral — `https://mistral.ai/news/rss` (RSS exists after all, no
      scrape needed). `fetch_mistral_blog`
- [x] Hugging Face — `https://huggingface.co/blog/feed.xml` (mixes official +
      community → keyword-filter + cap 25 + og enrich). `fetch_huggingface_blog`

Also done: shared `KEYWORDS` list + `matches_keywords()` (arXiv/HN only),
`dedupe()` (canonical-URL + near-identical-title, first wins), cross-run
dedup via `data/seen_urls.json`, `collect()` main loop, `--no-state` flag
for repeated local test runs, exit 1 when zero items collected.

Resolved questions:
- HN points threshold: started at 50 (`HN_MIN_POINTS`) — tune after 1st live run
- Keyword list: first draft in `KEYWORDS` — tune weekly (note: bare "gpt"
  covers gpt-N; add new model families as they ship)

Open follow-ups (not blocking):
- Reddit best-effort is flaky from cloud IPs. If it misses too often, upgrade
  to Reddit OAuth "script" app (free, no card): REDDIT_CLIENT_ID/SECRET +
  ~30 lines hitting oauth.reddit.com.
- Meta AI has no good free path. Revisit with a headless scrape (playwright)
  if HN/Reddit coverage of Meta feels thin.
- Anthropic scrape only sees ~11 server-rendered `/news` cards; a research
  post under `/research/` (e.g. "Formalizing Fermat's Last Theorem") won't
  be caught. Add `/research` scanning if needed.
- arXiv `published_date` is original submission; papers revised this week but
  submitted earlier are missed. Acceptable for a digest.

## Component 2: Curator
Status: DONE (2026-09-06) — two-pass curation, reviewed dry-run output
approved, `data/curated_digest.json` written.

Design (`src/curator.py`), prompts in `src/prompts/*.md`:
- [x] PASS 1 — batch scoring. 151 items in batches of 20, each item gets
      significance 1-10 + one of the 5 fixed categories + a one-line reason.
      Missing-id backfill retry; a batch that fails twice is dropped + logged.
      Prompt: `curator_pass1.md`.
- [x] PASS 2a — cluster + select. Top 55 scored items → LLM clusters
      duplicates (e.g. GPT-6 Astra merged 8 raw items), picks 15-20 finalists.
      Then `_enforce_balance()` in code: cap any category at 6, guarantee a
      floor of 2 for categories with >=2 items scoring >=5, keep total in
      [15, 20]. Prompt: `curator_pass2_select.md`.
- [x] PASS 2b — write-ups. Finalists in batches of 6 → 3-4 sentence summary
      + "why it matters", grounded strictly in title+snippet. On batch
      failure the item falls back to its raw snippet. Prompt:
      `curator_pass2_writeups.md`.
- [x] `reading_time_min` computed in Python (source-type base + snippet-length
      nudge — deliberately rough).
- [x] Category set is FIXED: Model Releases / Research / Tooling & Infra /
      Industry & Business / Community Discourse (`CATEGORIES` in curator.py).
- [x] Invalid-JSON handling: server JSON mode → on Groq `json_validate_failed`
      or a parse error, one repair retry, then a plain-text retry with a
      bigger token budget, then fall back from `gpt-oss-120b` to
      `qwen/qwen3.8-27b`. Only raises (exit 1) if every path fails — never
      writes partial/garbage output.
- [x] Output schema: `{generated_at, model, items_considered, items_selected,
      categories:[{name, items:[{title,url,source,summary,why_it_matters,
      reading_time_min,significance,also_covered_by:[{source,url}]}]}]}`.
      Only non-empty categories, canonical order.
- [x] CLI: `--dry-run` (print, don't write), `--debug` (dump pass-1 scores +
      pass-2a selection to `data/curator_debug_*.json`), `--input/--output/
      --model` overrides.

Runtime characteristics (matters for cron timing / GH Actions job timeout):
- **~6.5 min per run.** Groq free tier is **8000 tokens/min** (TPM), which
  forces pacing between the ~13 LLM calls a run makes — the pacing waits,
  not the model, dominate wall-clock.
- **~50k tokens total** per run; ~13 requests (well under the 1000 req/day
  free limit).
- Model: `groq/openai/gpt-oss-120b` primary, `groq/qwen/qwen3.8-27b` fallback
  (both 8000 TPM). `reasoning_effort=low` for scoring/selection (medium ate
  the output budget and truncated JSON), `medium` for write-ups.
- Implication: the GitHub Actions job timeout must allow well over 6.5 min
  for curation alone (collector adds ~1 min, mailer trivial) — budget the
  workflow at 20+ min and don't schedule the cron near a TPM-contended time.

Tunable knobs noted during review (left as-is, working well):
- pass-2a picks a news-aggregator URL as `primary` for clustered stories, with
  the vendor blog URL in `also_covered_by` — could prefer vendor-domain URLs.
- Research is floor-limited to 2 finalists (model rates individual papers
  below releases/business for an *industry* digest) — raise its floor if
  wanted.
- `also_covered_by` entries carry the originating item's `source` ("hn" even
  when the URL is openai.com) — digest builder should render these as plain
  links, not "covered by HN".

## Component 3: Digest builder
Status: not started

- [ ] HTML template — simple, one section per category, no external CSS
      framework needed (email clients strip most of it anyway — inline
      styles only)

## Component 4: Mailer
Status: not started

- [ ] Gmail App Password generated and stored locally in `.env`
- [ ] Confirm SMTP send works to owner's own address before automating

## GitHub Actions
Status: not started

- [ ] Private repo created
- [ ] Secrets added: GROQ_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_RECIPIENT
- [ ] Weekly cron confirmed correct in IST (cron is UTC — convert carefully)
- [ ] First scheduled run verified end-to-end
