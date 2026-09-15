# Token Bites

A weekly digest of what happened in the LLM industry, delivered straight to
your inbox. Every week, it pulls together new model releases, research
papers, official announcements from AI labs, and what the community is
actually discussing on Hacker News and Reddit — then uses an LLM to sift the
noise, cluster duplicate stories, and write a short summary of each one with
a note on why it matters. No dashboard to check, no feed to scroll — just one
email a week with the stuff that was actually worth knowing about.

## How it works

The pipeline is four small, independent stages that run in sequence:

1. **Collector** — pulls raw items from RSS feeds, the arXiv API, and the HN
   Algolia API (plus a couple of best-effort sources), and dedupes them into
   one clean list. No LLM calls here — pure fetching and parsing.
2. **Curator** — sends the deduped batch to an LLM, which scores, clusters,
   and ranks stories into categories, writing a short summary and a
   "why it matters" line for each one that makes the cut.
3. **Digest builder** — turns the curated data into a clean, inline-styled
   HTML email body. Pure templating, no external calls.
4. **Mailer** — sends the finished HTML digest over SMTP.

`src/run_weekly.py` runs all four in order and is the single entry point the
scheduler invokes.

## Completely free to run

Every piece of this project sits on a genuinely free tier — no trial period,
no credit card, no paid upgrade required to keep it running:

- **Sources** — RSS feeds, the arXiv API, and the HN Algolia API are all
  free and keyless.
- **LLM curation** — [Groq](https://console.groq.com)'s free API tier.
- **Scheduling** — GitHub Actions cron, on the free minutes tier.
- **Email delivery** — Gmail SMTP with an App Password, no paid email
  service or API needed.
- **State/dedup storage** — a flat JSON file committed back to the repo,
  no database.

## Running your own instance

1. **Clone the repo.**
2. **Create `.env`** from `.env.example` and fill in your own values — a
   Groq API key, a Gmail address, and a
   [Gmail App Password](https://myaccount.google.com/apppasswords) (needs
   2FA enabled on the account). This `.env` is for local testing only and is
   gitignored.
3. **Add the same four values as GitHub Actions repo secrets** (Settings →
   Secrets and variables → Actions): `GROQ_API_KEY`, `GMAIL_ADDRESS`,
   `GMAIL_APP_PASSWORD`, `DIGEST_RECIPIENT`. These are what the scheduled
   workflow actually reads from — the `.env` file is never used in CI.
4. **Done.** The included workflow (`.github/workflows/weekly_digest.yml`)
   runs on its own weekly cron, or you can trigger it manually from the
   Actions tab (`Run workflow`) to test it right away.

Each pipeline stage can also be run standalone for local testing, e.g.
`python src/collector.py`.

## License

MIT — see [LICENSE](LICENSE).
