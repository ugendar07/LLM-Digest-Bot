"""Weekly run orchestrator — the single entry point for the whole pipeline.

Runs the four components in sequence, passing data through the intermediate
files each one already reads/writes:

    collector      -> data/raw_this_week.json
    curator        -> data/curated_digest.json
    digest_builder -> data/digest.html
    mailer         -> Gmail SMTP

Cross-run dedup state (``data/seen_urls.json``) is handled *here*, not in the
collector, so that URLs are only marked "seen" once the digest has actually
been sent. A failure in curation or mail therefore never causes this week's
stories to be silently dropped from next week's collection.

This is what the GitHub Actions weekly cron invokes::

    python src/run_weekly.py

Exit code is 0 only if a digest was sent (or there was genuinely nothing new
to send); any stage failure logs the cause and exits non-zero so the Actions
run is marked failed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Components live alongside this file; ``python src/run_weekly.py`` puts src/
# on sys.path[0] so these resolve.
import collector
import curator
import digest_builder
import mailer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_weekly")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DIGEST_HTML = DATA_DIR / "digest.html"


class PipelineError(RuntimeError):
    """A stage failed hard enough that no digest should be sent."""


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


def stage_collect(seen: set[str]) -> list[dict]:
    """Run every source, then filter out URLs already carried in a past digest.

    Returns the fresh items and (re)writes ``data/raw_this_week.json`` with
    exactly that set, so the curator sees only new stories.
    """
    log.info("[Collector] starting — %d URLs seen in prior runs", len(seen))
    try:
        collected = collector.collect(use_state=False)
    except Exception as e:  # noqa: BLE001 — collector guards sources, but be safe
        raise PipelineError(f"collector crashed: {e}") from e

    if not collected:
        raise PipelineError(
            "collector returned 0 items from every source — likely a network "
            "or feed-wide problem, not a quiet week"
        )

    fresh = [it for it in collected if collector._norm_url(it["url"]) not in seen]
    n_filtered = len(collected) - len(fresh)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    collector.RAW_OUTPUT.write_text(
        json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info(
        "[Collector] %d raw items, %d filtered as duplicates of prior weeks, "
        "%d new -> %s",
        len(collected), n_filtered, len(fresh), collector.RAW_OUTPUT.name,
    )
    return fresh


def stage_curate() -> dict:
    log.info("[Curator] starting — this is the slow stage (~6-7 min, Groq TPM paced)")
    try:
        result = curator.curate(
            curator.RAW_INPUT, dry_run=False, output_path=curator.CURATED_OUTPUT
        )
    except curator.CuratorError as e:
        raise PipelineError(f"curation failed: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"curator crashed: {e}") from e

    n_selected = result.get("items_selected", 0)
    categories = result.get("categories", [])
    if not n_selected or not categories:
        raise PipelineError("curator produced an empty digest")

    log.info(
        "[Curator] %d items selected across %d categories (from %d considered): %s",
        n_selected, len(categories), result.get("items_considered", "?"),
        ", ".join(f"{c['name']} ({len(c['items'])})" for c in categories),
    )
    return result


def stage_build(curated: dict) -> str:
    try:
        html = digest_builder.build_html(curated)
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"digest builder failed: {e}") from e

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_HTML.write_text(html, encoding="utf-8")
    n_stories = sum(len(c.get("items", [])) for c in curated.get("categories", []))
    log.info("[Digest] built HTML body (%d bytes, %d stories) -> %s",
             len(html), n_stories, DIGEST_HTML.name)
    return html


def stage_mail(html: str) -> tuple[str, str]:
    """Send the digest. Returns ``(recipient, message_id)``."""
    sender = mailer._env("GMAIL_ADDRESS")
    recipient = mailer._env("DIGEST_RECIPIENT") or sender
    app_password = mailer._env("GMAIL_APP_PASSWORD")

    missing = [
        n for n, v in (
            ("GMAIL_ADDRESS", sender),
            ("DIGEST_RECIPIENT", recipient),
            ("GMAIL_APP_PASSWORD", app_password),
        ) if not v
    ]
    if missing:
        raise PipelineError(
            f"mailer config missing: {', '.join(missing)} "
            "(set as .env locally / GitHub Actions repo secrets in production)"
        )

    try:
        msg = mailer.compose(html, sender=sender, recipient=recipient)
        mailer.send(msg, sender=sender, app_password=app_password)
    except mailer.MailerError as e:
        raise PipelineError(f"send failed: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"mailer crashed: {e}") from e

    message_id = msg["Message-ID"]
    log.info("[Mailer] sent to %s, message-id: %s, subject: %r",
             recipient, message_id, msg["Subject"])
    return recipient, message_id


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run() -> int:
    started_wall = datetime.now()
    started = time.monotonic()
    log.info("=" * 64)
    log.info("weekly digest run started at %s", started_wall.isoformat(timespec="seconds"))
    log.info("=" * 64)

    seen_before = collector._load_seen_urls()

    fresh = stage_collect(seen_before)
    if not fresh:
        # every collected story was already in a previous digest. Not a
        # failure — just nothing to send. seen_urls.json is unchanged.
        log.info("[run_weekly] nothing new since last week — no digest sent")
        log.info("run finished in %.1fs (no-op)", time.monotonic() - started)
        return 0

    curated = stage_curate()
    html = stage_build(curated)
    recipient, message_id = stage_mail(html)

    # --- only now, after a confirmed send, persist dedup state --------------- #
    seen_after = set(seen_before)
    seen_after.update(collector._norm_url(it["url"]) for it in fresh)
    added = len(seen_after) - len(seen_before)
    collector._save_seen_urls(seen_after)
    log.info("[State] seen_urls.json updated: %d -> %d URLs (+%d this week) -> %s",
             len(seen_before), len(seen_after), added, collector.SEEN_URLS_FILE.name)

    elapsed = time.monotonic() - started
    log.info("=" * 64)
    log.info("weekly digest run OK in %.1fs (%.1f min) — sent to %s, msg-id %s",
             elapsed, elapsed / 60, recipient, message_id)
    log.info("=" * 64)
    return 0


def _main() -> int:
    try:
        return run()
    except PipelineError as e:
        log.error("=" * 64)
        log.error("RUN FAILED: %s", e)
        log.error("no digest was sent; seen_urls.json left unchanged")
        log.error("=" * 64)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(_main())
