"""Component 1 — Collector.

Pulls raw items from all free news sources, normalizes them to a common
schema, dedupes near-identical stories, and writes ``data/raw_this_week.json``.

No LLM calls happen here — this step is pure fetching / parsing.

Normalized item schema (every source must emit this shape)::

    {
        "title": str,
        "url": str,
        "source": str,            # e.g. "arxiv", "hn", "reddit:LocalLLaMA"
        "snippet": str,           # short excerpt/abstract, "" if unavailable
        "published_date": str,    # ISO 8601, "" if unknown
    }

Run standalone for a full collection pass::

    python src/collector.py            # runs every source, writes the JSON
    python src/collector.py --no-state # ...without touching data/seen_urls.json

Each ``fetch_*`` function is also runnable in isolation for testing::

    python -c "from src.collector import fetch_arxiv; import json; print(json.dumps(fetch_arxiv(), indent=2)[:2000])"
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collector")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_OUTPUT = DATA_DIR / "raw_this_week.json"
SEEN_URLS_FILE = DATA_DIR / "seen_urls.json"

LOOKBACK_DAYS = 7
HTTP_TIMEOUT = 10  # seconds — every network call must pass this
USER_AGENT = (
    "llm-digest-bot/0.1 (personal weekly LLM-industry digest; "
    "https://github.com/ugendar07)"
)

# --------------------------------------------------------------------------- #
# Shared keyword filter
# --------------------------------------------------------------------------- #
# Used by the *broad* sources (arXiv cs.CL/cs.AI, Hacker News search) to cut
# noise before the curator ever sees the batch. Defined once, reused. Matching
# is case-insensitive substring against ``title + snippet``. Curated blog feeds
# (OpenAI, Anthropic, ...) are already on-topic and skip this filter.

KEYWORDS: list[str] = [
    # --- core LLM / NLP concepts ---
    "llm", "large language model", "language model", "foundation model",
    "fine-tun", "fine tuning", "instruction tun", "pretrain", "pre-train",
    "rlhf", "dpo", "preference optimization", "alignment", "rag",
    "retrieval-augmented", "retrieval augmented", "agent", "agentic",
    "tool use", "tool-use", "function calling", "inference", "serving",
    "quantiz", "distillation", "distil", "speculative decoding", "kv cache",
    "context window", "long context", "context length", "tokenizer",
    "embedding", "prompt", "in-context", "chain-of-thought", "chain of thought",
    "reasoning model", "mixture-of-experts", "mixture of experts", "moe",
    "transformer", "multimodal", "vision-language", "vision language",
    "lora", "qlora", "peft", "benchmark", "eval", "hallucinat", "jailbreak",
    "guardrail", "synthetic data", "chatbot", "assistant model",
    # --- model families / product names ---
    # bare "gpt" covers gpt-3/4/5/6/7/oss/... ; keep it lowercase-substring
    "gpt", "chatgpt", "openai o1", "openai o3", "o1-preview", "o3-mini",
    "claude", "gemini", "gemma", "llama", "code llama", "mistral", "mixtral",
    "codestral", "qwen", "deepseek", "phi-2", "phi-3", "phi-4", "command r",
    "command-r", "grok", "falcon", "dbrx", "nemotron", "olmo", "smollm",
    "stable lm", "stablelm", "yi-34b", "internlm", "starcoder", "kimi",
    # --- labs / orgs that ship LLM news ---
    "openai", "anthropic", "deepmind", "google research", "meta ai", "fair",
    "mistral ai", "hugging face", "huggingface", "cohere", "ai21", "reka",
    "stability ai", "databricks", "nvidia", "microsoft research",
    "allen institute for ai", "ai2", "xai", "perplexity ai", "together ai",
]

_KEYWORDS_LOWER = [k.lower() for k in KEYWORDS]


def matches_keywords(*texts: str) -> bool:
    """True if any shared keyword appears in the concatenated lowercased text."""
    blob = " ".join(t for t in texts if t).lower()
    return any(k in blob for k in _KEYWORDS_LOWER)


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _within_lookback(dt: datetime, days: int = LOOKBACK_DAYS) -> bool:
    return dt >= _now_utc() - timedelta(days=days)


def _iso_from_struct_time(st) -> str:
    """feedparser ``*_parsed`` struct_time (UTC) -> ISO 8601 string, or ""."""
    if not st:
        return ""
    try:
        return datetime(*st[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip HTML tags from a snippet/title."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _http_get(url: str, *, retries: int = 1, **kwargs) -> requests.Response:
    """GET with the project User-Agent and the mandatory timeout baked in.

    Retries ``retries`` times on transient network errors with a short backoff.
    """
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", {}))
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            # only retry transient failures, not 4xx like 404/403
            if status is not None and status not in (429, 500, 502, 503, 504):
                raise
            if attempt < retries:
                retry_after = 0.0
                if getattr(e, "response", None) is not None:
                    try:
                        retry_after = float(e.response.headers.get("Retry-After", 0))
                    except (TypeError, ValueError):
                        retry_after = 0.0
                time.sleep(max(retry_after, 2.0 * (attempt + 1)))
    raise last_exc  # type: ignore[misc]


def _normalize_item(
    *, title: str, url: str, source: str, snippet: str = "", published_date: str = ""
) -> dict:
    return {
        "title": _clean_text(title),
        "url": (url or "").strip(),
        "source": source,
        "snippet": _clean_text(snippet)[:600],
        "published_date": published_date or "",
    }


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #

_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})")


def _norm_url(url: str) -> str:
    """Canonicalize a URL for dedup (scheme/host/tracking-insensitive)."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("#", 1)[0]
    u = re.sub(r"[?&](utm_[^=]+|ref|source)=[^&]*", "", u)
    u = u.rstrip("/?&")
    m = _ARXIV_ID_RE.search(u)
    if m:
        return f"arxiv:{m.group(1)}"
    return u


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def dedupe(items: list[dict]) -> list[dict]:
    """Drop items sharing a canonical URL, or a near-identical title, with an
    earlier item. First occurrence wins; ordering is preserved."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict] = []
    dropped = 0
    for it in items:
        ukey = _norm_url(it["url"])
        tkey = _norm_title(it["title"])
        # only trust title-dedup for reasonably specific titles
        title_dupe = len(tkey) >= 20 and tkey in seen_titles
        if (ukey and ukey in seen_urls) or title_dupe:
            dropped += 1
            continue
        if ukey:
            seen_urls.add(ukey)
        if len(tkey) >= 20:
            seen_titles.add(tkey)
        out.append(it)
    if dropped:
        log.info("dedupe: dropped %d duplicate item(s)", dropped)
    return out


def _load_seen_urls() -> set[str]:
    try:
        data = json.loads(SEEN_URLS_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []) if isinstance(data, dict) else data)
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as e:
        log.warning("seen_urls.json unreadable (%s) — treating as empty", e)
        return set()


def _save_seen_urls(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": _now_utc().isoformat(),
        "seen": sorted(seen),
    }
    SEEN_URLS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Source 1 — arXiv API (cs.CL + cs.AI, last 7 days, keyword-filtered)
# --------------------------------------------------------------------------- #

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_PAGE_SIZE = 100
ARXIV_MAX_PAGES = 3          # ~300 newest papers — arXiv volume makes a full
                            # 7-day scan pointless once the keep-cap applies
ARXIV_PAGE_PAUSE = 3.0       # arXiv asks callers to wait ~3s between requests
ARXIV_KEEP_CAP = 40          # keep only the N most recent keyword-matched papers


def fetch_arxiv() -> list[dict]:
    """The ~40 newest cs.CL / cs.AI submissions whose *title* matches the
    shared keyword list.

    arXiv's Atom API needs no key and is free. It posts ~200 cs.CL+cs.AI
    papers a day, so rather than scan the whole 7-day window we take the most
    recent ``ARXIV_PAGE_SIZE * ARXIV_MAX_PAGES`` papers, keep the title
    matches, and hand the curator the ``ARXIV_KEEP_CAP`` most recent. Items
    already outside the lookback window are still dropped (quiet weeks).
    """
    source = "arxiv"
    matched: list[dict] = []
    try:
        n_scanned = n_old = n_offtopic = 0
        stop = False

        for page in range(ARXIV_MAX_PAGES):
            if page:
                time.sleep(ARXIV_PAGE_PAUSE)
            params = {
                "search_query": "cat:cs.CL OR cat:cs.AI",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "start": page * ARXIV_PAGE_SIZE,
                "max_results": ARXIV_PAGE_SIZE,
            }
            try:
                resp = _http_get(ARXIV_API, params=params, retries=2)
            except Exception as e:  # noqa: BLE001
                if page == 0:
                    raise
                log.warning("arxiv: page %d failed (%s) — using %d collected so far",
                            page, e, len(matched))
                break
            feed = feedparser.parse(resp.content)
            if getattr(feed, "bozo", False) and not feed.entries:
                if page == 0:
                    raise RuntimeError(
                        f"unparseable feed: {getattr(feed, 'bozo_exception', '?')}"
                    )
                break
            if not feed.entries:
                break

            for entry in feed.entries:
                n_scanned += 1
                published = _iso_from_struct_time(entry.get("published_parsed"))
                if published and not _within_lookback(datetime.fromisoformat(published)):
                    n_old += 1
                    stop = True  # sorted desc — everything after this is older
                    continue
                title = _clean_text(entry.get("title", ""))
                summary = _clean_text(entry.get("summary", ""))
                if not matches_keywords(title):  # title-only: stronger signal
                    n_offtopic += 1
                    continue
                url = entry.get("link", "")
                for link in entry.get("links", []):
                    if link.get("rel") == "alternate" and link.get("href"):
                        url = link["href"]
                        break
                matched.append(
                    _normalize_item(
                        title=title,
                        url=url,
                        source=source,
                        snippet=summary,
                        published_date=published,
                    )
                )
            if stop:
                break

        matched.sort(key=lambda it: it["published_date"], reverse=True)
        kept = matched[:ARXIV_KEEP_CAP]
        log.info(
            "arxiv: kept %d/%d title-matched (scanned %d, %d off-topic, %d out-of-window)",
            len(kept), len(matched), n_scanned, n_offtopic, n_old,
        )
        return kept
    except Exception as e:  # noqa: BLE001 — one broken source must not kill the run
        log.warning("arxiv: skipped: %s", e)
        return []


# --------------------------------------------------------------------------- #
# Source 2 — Hacker News via the Algolia API (free, no key)
# --------------------------------------------------------------------------- #

HN_API = "https://hn.algolia.com/api/v1/search"
HN_MIN_POINTS = 50   # noise floor — tune after the first live run (see PLAN.md)
HN_QUERIES = [
    "LLM", "GPT", "Claude", "Anthropic", "OpenAI", "language model",
    "fine-tuning", "open source model", "Llama", "Mistral", "Gemini",
    "inference", "AI agents", "RAG",
]


def fetch_hn() -> list[dict]:
    """Front-page-grade Hacker News stories about LLMs from the last week.

    Runs a handful of targeted queries against the free Algolia HN API,
    keeps stories above ``HN_MIN_POINTS`` created within the lookback window,
    and de-dupes by story id before returning.
    """
    source = "hn"
    cutoff = int((_now_utc() - timedelta(days=LOOKBACK_DAYS)).timestamp())
    by_id: dict[str, tuple[int, dict]] = {}
    try:
        for query in HN_QUERIES:
            params = {
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{cutoff},points>={HN_MIN_POINTS}",
                "hitsPerPage": 50,
            }
            try:
                resp = _http_get(HN_API, params=params, retries=1)
                hits = resp.json().get("hits", [])
            except Exception as e:  # noqa: BLE001 — skip this query, keep going
                log.warning("hn: query %r failed: %s", query, e)
                continue

            for hit in hits:
                oid = str(hit.get("objectID", ""))
                if not oid or oid in by_id:
                    continue
                title = _clean_text(hit.get("title") or "")
                if not title:
                    continue
                points = hit.get("points") or 0
                comments = hit.get("num_comments") or 0
                story_text = _clean_text(hit.get("story_text") or "")
                hn_url = f"https://news.ycombinator.com/item?id={oid}"
                published = ""
                if hit.get("created_at_i"):
                    published = datetime.fromtimestamp(
                        hit["created_at_i"], tz=timezone.utc
                    ).isoformat()
                if not matches_keywords(title):  # title-only: cut fuzzy noise
                    continue
                snippet = (
                    f"{points} points, {comments} comments on Hacker News "
                    f"({hn_url})."
                )
                if story_text:
                    snippet += " " + story_text
                item = _normalize_item(
                    title=title,
                    url=hit.get("url") or hn_url,
                    source=source,
                    snippet=snippet,
                    published_date=published,
                )
                by_id[oid] = (points, item)

        items = [it for _, it in sorted(by_id.values(), key=lambda p: p[0], reverse=True)]
        log.info("hn: kept %d unique stor%s (>= %d points, %d queries)",
                 len(items), "y" if len(items) == 1 else "ies",
                 HN_MIN_POINTS, len(HN_QUERIES))
        return items
    except Exception as e:  # noqa: BLE001
        log.warning("hn: skipped: %s", e)
        return []


# --------------------------------------------------------------------------- #
# Source 3 — Reddit (top posts of the week via the free .rss endpoints)
# --------------------------------------------------------------------------- #

REDDIT_SUBREDDITS = ["LocalLLaMA", "MachineLearning"]
# Reddit now blocks most keyless traffic (403 / 429 / HTML login wall) from
# server IPs. This is a best-effort fetch: try each host in turn, and if none
# serve real XML, log it and move on — Reddit will simply be missing from the
# digest that week. (See PLAN.md for the OAuth upgrade path if this proves
# too flaky.)
REDDIT_HOSTS = ["https://old.reddit.com", "https://www.reddit.com"]
REDDIT_TOP_PATH = "/r/{sub}/top.rss?t=week&limit=40"
# these subs are already on-topic, so (per spec) the shared keyword filter is
# NOT applied here — the curator does the final relevance pass.

REDDIT_REQUEST_PAUSE = 2.0  # reddit rate-limits rapid RSS hits from one IP

_REDDIT_HREF_RE = re.compile(r'href="(https?://[^"]+)"[^>]*>\s*\[link\]', re.I)
_REDDIT_BOILERPLATE_RE = re.compile(
    r"\s*submitted by\s*/u/\S+\s*(\[link\]\s*)?(\[comments\]\s*)?$", re.I
)


def _reddit_get_feed(sub: str):
    """Return a parsed feed with entries, or None if Reddit blocked every host."""
    path = REDDIT_TOP_PATH.format(sub=sub)
    for i, host in enumerate(REDDIT_HOSTS):
        if i:
            time.sleep(REDDIT_REQUEST_PAUSE)
        try:
            resp = _http_get(host + path, retries=2)
        except Exception as e:  # noqa: BLE001
            log.info("reddit:%s: %s -> %s", sub, host, e)
            continue
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype.lower():
            log.info("reddit:%s: %s served an HTML wall (rate-limited)", sub, host)
            continue
        feed = feedparser.parse(resp.content)
        if feed.entries:
            return feed
        log.info("reddit:%s: %s returned no entries (bozo=%s)",
                 sub, host, getattr(feed, "bozo_exception", "?"))
    return None


def _fetch_one_subreddit(sub: str) -> list[dict]:
    source = f"reddit:{sub}"
    try:
        feed = _reddit_get_feed(sub)
        if feed is None:
            log.warning("%s: skipped: Reddit blocked all hosts (keyless access)", source)
            return []
        items: list[dict] = []
        n_old = 0
        for entry in feed.entries:
            published = _iso_from_struct_time(
                entry.get("published_parsed") or entry.get("updated_parsed")
            )
            if published and not _within_lookback(datetime.fromisoformat(published)):
                n_old += 1
                continue
            raw_content = ""
            if entry.get("content"):
                raw_content = entry["content"][0].get("value", "")
            raw_content = raw_content or entry.get("summary", "")

            # a link post embeds the external URL as `<a href=...>[link]</a>`;
            # prefer that over the reddit permalink so the digest points at the
            # actual article. Self/discussion posts keep the permalink.
            m = _REDDIT_HREF_RE.search(raw_content)
            permalink = entry.get("link", "")
            external = m.group(1) if m else ""
            link = external or permalink

            snippet = _clean_text(raw_content)
            snippet = _REDDIT_BOILERPLATE_RE.sub("", snippet).strip()
            if permalink and permalink not in snippet:
                prefix = f"Reddit discussion: {permalink}."
                snippet = f"{prefix} {snippet}".strip() if snippet else prefix

            items.append(
                _normalize_item(
                    title=entry.get("title", ""),
                    url=link,
                    source=source,
                    snippet=snippet,
                    published_date=published,
                )
            )
        log.info("%s: kept %d (skipped %d out-of-window) from %d entries",
                 source, len(items), n_old, len(feed.entries))
        return items
    except Exception as e:  # noqa: BLE001
        log.warning("%s: skipped: %s", source, e)
        return []


def fetch_reddit() -> list[dict]:
    """Top posts of the past week from the configured subreddits.

    Reddit now blocks most keyless access from server IPs, so this is
    best-effort (see :data:`REDDIT_HOSTS`). Each subreddit is fetched
    independently, and a blocked feed degrades to ``[]`` without affecting
    the others or the rest of the run.
    """
    items: list[dict] = []
    for i, sub in enumerate(REDDIT_SUBREDDITS):
        if i:
            time.sleep(REDDIT_REQUEST_PAUSE)
        items.extend(_fetch_one_subreddit(sub))
    return items


# --------------------------------------------------------------------------- #
# Sources 4-9 — curated RSS feeds (lab / org blogs)
# --------------------------------------------------------------------------- #
# These feeds are already on-topic, so the shared keyword filter is only
# applied where a feed mixes in community/unrelated posts (Hugging Face).

_MAX_UNDATED_KEPT = 3  # feeds are newest-first; keep a few undated entries only

_OG_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*'
    r'content=["\']([^"\']+)["\']',
    re.I,
)
_OG_DESC_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*'
    r'(?:property|name)=["\'](?:og:description|description)["\']',
    re.I,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', re.I
)


_MAX_ENRICH = 12  # cap per-article snippet fetches per feed

# og:descriptions that are just a site tagline, not per-article content
_BOILERPLATE_DESCRIPTIONS = (
    "on a journey to advance and democratize",
    "Anthropic is an AI safety and research company",
)


def _page_meta(url: str) -> tuple[str, str]:
    """Best-effort ``(og:title, og:description)`` for an article page.

    Never raises — returns ``("", "")`` on any failure.
    """
    try:
        resp = _http_get(url, retries=1, timeout=8)
        head = resp.text[:20000]
        tm = _OG_TITLE_RE.search(head)
        dm = _OG_DESC_RE.search(head) or _OG_DESC_RE_REV.search(head)
        title = _clean_text(tm.group(1)) if tm else ""
        desc = _clean_text(dm.group(1)) if dm else ""
        if any(b in desc for b in _BOILERPLATE_DESCRIPTIONS):
            desc = ""
        return title, desc
    except Exception as e:  # noqa: BLE001
        log.debug("page meta fetch failed for %s: %s", url, e)
        return "", ""


def _og_description(url: str) -> str:
    """Just the og:description half of :func:`_page_meta`."""
    return _page_meta(url)[1]


def _fetch_rss(
    source: str,
    url: str,
    *,
    keyword_filter: bool = False,
    limit: int | None = None,
    enrich_snippets: bool = False,
) -> list[dict]:
    """Generic RSS/Atom fetch → normalized items within the lookback window.

    ``enrich_snippets`` fills empty snippets from each article's
    og:description (best-effort, capped, never fatal) — for feeds whose
    ``<description>`` is blank (DeepMind, Hugging Face).
    """
    try:
        resp = _http_get(url, retries=2)
        feed = feedparser.parse(resp.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError(
                f"unparseable feed: {getattr(feed, 'bozo_exception', '?')}"
            )
        items: list[dict] = []
        n_old = n_offtopic = n_undated = 0
        for entry in feed.entries:
            published = _iso_from_struct_time(
                entry.get("published_parsed") or entry.get("updated_parsed")
            )
            if published:
                if not _within_lookback(datetime.fromisoformat(published)):
                    n_old += 1
                    continue
            else:
                n_undated += 1
                if n_undated > _MAX_UNDATED_KEPT:
                    continue

            title = _clean_text(entry.get("title", ""))
            summary = _clean_text(entry.get("summary", ""))
            if entry.get("content"):
                summary = summary or _clean_text(entry["content"][0].get("value", ""))
            if keyword_filter and not matches_keywords(title, summary):
                n_offtopic += 1
                continue

            items.append(
                _normalize_item(
                    title=title,
                    url=entry.get("link", ""),
                    source=source,
                    snippet=summary,
                    published_date=published,
                )
            )
            if limit and len(items) >= limit:
                break

        if enrich_snippets:
            enriched = 0
            for it in items:
                if it["snippet"] or enriched >= _MAX_ENRICH or not it["url"]:
                    continue
                it["snippet"] = _og_description(it["url"])
                enriched += 1

        log.info(
            "%s: kept %d (skipped %d out-of-window, %d off-topic; %d undated) of %d",
            source, len(items), n_old, n_offtopic, n_undated, len(feed.entries),
        )
        return items
    except Exception as e:  # noqa: BLE001
        log.warning("%s: skipped: %s", source, e)
        return []


def fetch_openai_blog() -> list[dict]:
    return _fetch_rss("openai_blog", "https://openai.com/news/rss.xml")


def fetch_deepmind_blog() -> list[dict]:
    return _fetch_rss(
        "deepmind_blog", "https://deepmind.google/blog/rss.xml", enrich_snippets=True
    )


def fetch_mistral_blog() -> list[dict]:
    return _fetch_rss("mistral_blog", "https://mistral.ai/news/rss")


def fetch_huggingface_blog() -> list[dict]:
    # feed mixes official + community posts → keyword-filter + cap
    return _fetch_rss(
        "huggingface_blog",
        "https://huggingface.co/blog/feed.xml",
        keyword_filter=True,
        limit=25,
        enrich_snippets=True,
    )


# --------------------------------------------------------------------------- #
# Source 8 — Anthropic news (no RSS anymore → lightweight scrape)
# --------------------------------------------------------------------------- #

ANTHROPIC_NEWS_URL = "https://www.anthropic.com/news"
_ANTHROPIC_ANCHOR_RE = re.compile(r'<a href="(/news/[a-z0-9-]+)"[^>]*>(.*?)</a>', re.S)
_ANTHROPIC_TIME_RE = re.compile(r"<time[^>]*>([^<]+)</time>")


def _parse_text_date(text: str) -> str:
    """'Sep 1, 2026' / 'September 1, 2026' -> ISO 8601 (UTC midnight), or ''."""
    text = text.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def fetch_anthropic_blog() -> list[dict]:
    """Anthropic dropped their RSS feed; scrape the /news index for recent posts.

    We take each ``/news/<slug>`` card's ``<time>`` date off the index, keep
    the ones inside the lookback window, then fetch those few article pages
    for a clean title + description.
    """
    source = "anthropic_blog"
    try:
        resp = _http_get(ANTHROPIC_NEWS_URL, retries=2)
        page = resp.text
        recent: list[tuple[str, str]] = []  # (slug, iso_date)
        seen: set[str] = set()
        scanned = 0
        for href, inner in _ANTHROPIC_ANCHOR_RE.findall(page):
            slug = href.rsplit("/", 1)[-1]
            if slug in seen:
                continue
            seen.add(slug)
            scanned += 1
            tm = _ANTHROPIC_TIME_RE.search(inner)
            iso = _parse_text_date(tm.group(1)) if tm else ""
            if not iso or not _within_lookback(datetime.fromisoformat(iso)):
                continue
            recent.append((slug, iso))

        items: list[dict] = []
        for slug, iso in recent[:_MAX_ENRICH]:
            url = f"https://www.anthropic.com/news/{slug}"
            title, desc = _page_meta(url)
            items.append(
                _normalize_item(
                    title=title or slug.replace("-", " ").capitalize(),
                    url=url,
                    source=source,
                    snippet=desc,
                    published_date=iso,
                )
            )
        log.info("anthropic_blog: kept %d of %d news links scanned", len(items), scanned)
        return items
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic_blog: skipped: %s", e)
        return []


# --------------------------------------------------------------------------- #
# Source 9 — Meta AI blog (no RSS, not cleanly scrapeable → Google News RSS)
# --------------------------------------------------------------------------- #

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
# `site:` + `when:` together returns nothing from Google News, so we scope by
# site only and do our own recency filtering below.
META_AI_NEWS_QUERY = "site:ai.meta.com/blog"


def fetch_meta_ai_blog() -> list[dict]:
    """Meta AI's blog has no feed and resists scraping (JS-rendered, only ~4
    stale posts in the server HTML).

    Best-effort fallback: Google News' free RSS search scoped to
    ``ai.meta.com/blog``, then filter hard to the lookback window. Google News
    doesn't date-sort ``site:`` queries, so most weeks this yields nothing and
    Meta releases reach the digest via Hacker News / r/LocalLLaMA instead.
    Links are Google redirect URLs. (See PLAN.md for the headless-scrape
    option if Meta coverage matters more later.)
    """
    source = "meta_ai_blog"
    try:
        params = {
            "q": META_AI_NEWS_QUERY,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        resp = _http_get(GOOGLE_NEWS_RSS, params=params, retries=2)
        feed = feedparser.parse(resp.content)
        items: list[dict] = []
        n_old = 0
        for entry in feed.entries:
            published = _iso_from_struct_time(
                entry.get("published_parsed") or entry.get("updated_parsed")
            )
            if published and not _within_lookback(datetime.fromisoformat(published)):
                n_old += 1
                continue
            title = _clean_text(entry.get("title", ""))
            # Google News appends " - AI at Meta" / " - <publisher>"
            title = re.sub(r"\s+-\s+[^-]+$", "", title).strip() or title
            items.append(
                _normalize_item(
                    title=title,
                    url=entry.get("link", ""),
                    source=source,
                    snippet=_clean_text(entry.get("summary", "")),
                    published_date=published,
                )
            )
        log.info("meta_ai_blog: kept %d (skipped %d out-of-window) of %d",
                 len(items), n_old, len(feed.entries))
        return items
    except Exception as e:  # noqa: BLE001
        log.warning("meta_ai_blog: skipped: %s", e)
        return []


# --------------------------------------------------------------------------- #
# Source registry — the main loop iterates this
# --------------------------------------------------------------------------- #

SOURCES: list = [
    fetch_arxiv,
    fetch_hn,
    fetch_reddit,
    fetch_openai_blog,
    fetch_anthropic_blog,
    fetch_deepmind_blog,
    fetch_meta_ai_blog,
    fetch_mistral_blog,
    fetch_huggingface_blog,
]


# --------------------------------------------------------------------------- #
# Main collection loop
# --------------------------------------------------------------------------- #


def collect(use_state: bool = True) -> list[dict]:
    """Run every registered source, dedupe, write ``raw_this_week.json``.

    When ``use_state`` is False, ``data/seen_urls.json`` is neither read nor
    written — handy for repeated local test runs.
    """
    all_items: list[dict] = []
    per_source_counts: dict[str, int] = {}
    empty_sources: list[str] = []

    for fetch_fn in SOURCES:
        name = fetch_fn.__name__
        started = time.monotonic()
        try:
            items = fetch_fn() or []
        except Exception as e:  # defensive: fetchers already guard, but be safe
            log.warning("%s: crashed unexpectedly: %s", name, e)
            items = []
        elapsed = time.monotonic() - started
        all_items.extend(items)
        for it in items:
            per_source_counts[it["source"]] = per_source_counts.get(it["source"], 0) + 1
        if not items:
            empty_sources.append(name)
        log.info("%s -> %d item(s) in %.1fs", name, len(items), elapsed)

    deduped = dedupe(all_items)

    # cross-run dedup: hide stories already carried in a previous week's digest
    seen = _load_seen_urls() if use_state else set()
    fresh = [it for it in deduped if _norm_url(it["url"]) not in seen]
    n_seen_before = len(deduped) - len(fresh)
    if n_seen_before:
        log.info("cross-run: dropped %d item(s) seen in a previous run", n_seen_before)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_OUTPUT.write_text(
        json.dumps(fresh, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if use_state:
        seen.update(_norm_url(it["url"]) for it in fresh)
        _save_seen_urls(seen)

    # --- run summary ---
    log.info("=" * 60)
    log.info("collected %d unique item(s) -> %s", len(fresh), RAW_OUTPUT)
    for src, count in sorted(per_source_counts.items()):
        log.info("  %-24s %d (fetched)", src, count)
    if empty_sources:
        log.warning("returned nothing this run: %s", ", ".join(empty_sources))
    if not fresh:
        log.error("no items collected from any source — check network / feeds")
    log.info("=" * 60)
    return fresh


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the digest collector.")
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="do not read/write data/seen_urls.json (useful for repeated tests)",
    )
    args = parser.parse_args(argv)
    items = collect(use_state=not args.no_state)
    return 0 if items else 1  # non-zero so CI surfaces a total collection failure


if __name__ == "__main__":
    sys.exit(_main())
