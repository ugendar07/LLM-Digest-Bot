"""Component 2 — Curator.

Takes ``data/raw_this_week.json`` (from the collector) and uses Groq (via
LiteLLM) to turn ~150 raw items into a ranked, categorized, de-duplicated
digest of the ~15-20 most significant stories, each with a short summary,
a "why it matters" line, and an estimated reading time.

Two-pass design (Groq free tier is 8000 tokens/min, so calls are paced):

    PASS 1  score every item 1-10 + assign a category, in batches
    PASS 2a cluster duplicates, pick the finalists with category balance
    PASS 2b write the summary + "why it matters" for each finalist (batched)

Prompts live in ``src/prompts/*.md`` — they are meant to be iterated on and
are deliberately not inlined here.

Usage::

    python src/curator.py --dry-run          # print curated JSON, write nothing
    python src/curator.py                     # write data/curated_digest.json
    python src/curator.py --input X --output Y --model groq/qwen/qwen3.8-27b
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import litellm  # noqa: E402  (must follow load_dotenv so keys are present)

litellm.drop_params = True  # silently ignore params a given model rejects
litellm.suppress_debug_info = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
log = logging.getLogger("curator")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SRC_DIR = Path(__file__).resolve().parent
DATA_DIR = SRC_DIR.parent / "data"
PROMPTS_DIR = SRC_DIR / "prompts"

RAW_INPUT = DATA_DIR / "raw_this_week.json"
CURATED_OUTPUT = DATA_DIR / "curated_digest.json"

PRIMARY_MODEL = os.environ.get("GROQ_MODEL", "groq/openai/gpt-oss-120b")
FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "groq/qwen/qwen3.8-27b")

CATEGORIES = [
    "Model Releases",
    "Research",
    "Tooling & Infra",
    "Industry & Business",
    "Community Discourse",
]
_CATEGORY_SET = set(CATEGORIES)

PASS1_BATCH_SIZE = 20
PASS1_SNIPPET_CAP = 240          # scoring needs the gist, not the whole snippet
PASS1_MAX_TOKENS = 1400         # ~20 short score records fit comfortably
WRITEUP_BATCH_SIZE = 6

MIN_ITEMS = 15
MAX_ITEMS = 20
CATEGORY_CAP = 6                 # max finalists from any one category
SELECT_POOL_SIZE = 55           # how many top-scored items pass 2a considers

TPM_BUDGET = 7500               # stay under Groq's 8000 tok/min (with headroom)
LLM_TIMEOUT = 60
LLM_MAX_RETRIES = 3

# reading-time heuristic: rough minutes to read the *linked* source
_READ_MINUTES_BY_SOURCE = {
    "arxiv": 15,
    "huggingface_blog": 7,
    "openai_blog": 6,
    "anthropic_blog": 6,
    "deepmind_blog": 6,
    "mistral_blog": 6,
    "meta_ai_blog": 6,
    "hn": 4,
}
_READ_MINUTES_DEFAULT = 5


class CuratorError(RuntimeError):
    """Unrecoverable failure in the curation pipeline."""


# --------------------------------------------------------------------------- #
# Prompt loading
# --------------------------------------------------------------------------- #


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise CuratorError(f"cannot read prompt {path}: {e}") from e


# --------------------------------------------------------------------------- #
# Groq / LiteLLM call wrapper (paced + JSON-validated + retrying)
# --------------------------------------------------------------------------- #

_token_log: list[tuple[float, int]] = []  # (timestamp, total_tokens) last 60s


def _pace(est_tokens: int) -> None:
    """Block until issuing a call of ~`est_tokens` keeps us under TPM_BUDGET."""
    now = time.time()
    _token_log[:] = [(t, n) for (t, n) in _token_log if now - t < 60]
    used = sum(n for _, n in _token_log)
    if used + est_tokens > TPM_BUDGET and _token_log:
        wait = 61 - (now - min(t for t, _ in _token_log))
        if wait > 0:
            log.info("pacing %.0fs (used ~%d tok/min, +%d incoming)",
                     wait, used, est_tokens)
            time.sleep(wait)


def _record_tokens(total: int) -> None:
    _token_log.append((time.time(), total))


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of a model response, tolerating stray fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: grab the outermost {...}
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def llm_json(
    system_prompt: str,
    user_content: str,
    *,
    label: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    reasoning_effort: str = "low",
) -> dict:
    """One JSON call to Groq, hardened for the free tier:

    - paced to stay under the TPM limit, with 429 backoff
    - server JSON mode first; on Groq's ``json_validate_failed`` or a parse
      failure, one repair retry, then a plain-text retry with a bigger budget
    - falls back from the primary model to the fallback model
    - raises CuratorError only if every path fails
    """
    # rough token estimate: ~4 chars/token for the prompt, plus a fraction of
    # the output cap. Actual usage feeds back via _record_tokens and a 429 is
    # still retried, so a small undershoot is safe.
    est = len(system_prompt + user_content) // 4 + min(max_tokens, 900)
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        messages = list(base_messages)
        use_json_mode = True
        tokens = max_tokens
        repaired = False

        for attempt in range(1, LLM_MAX_RETRIES + 1):
            _pace(est)
            kwargs = dict(
                model=model, messages=messages, temperature=temperature,
                max_tokens=tokens, reasoning_effort=reasoning_effort,
                timeout=LLM_TIMEOUT,
            )
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = litellm.completion(**kwargs)
            except litellm.RateLimitError as e:
                wait = _retry_after(e) or 20 * attempt
                log.warning("%s: rate limited on %s, sleeping %ds", label, model, wait)
                time.sleep(wait)
                continue
            except litellm.BadRequestError as e:
                if "json_validate_failed" in str(e).lower():
                    log.warning("%s: %s truncated JSON output; retrying plain w/ more tokens",
                                label, model)
                    use_json_mode = False
                    tokens = int(tokens * 1.6)
                    continue
                raise CuratorError(f"{label}: bad request to {model}: {e}") from e
            except (
                litellm.APIError, litellm.APIConnectionError, litellm.Timeout,
                litellm.InternalServerError, litellm.ServiceUnavailableError,
            ) as e:
                log.warning("%s: %s on %s (attempt %d): %s",
                            label, type(e).__name__, model, attempt, e)
                time.sleep(3 * attempt)
                continue

            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            if usage:
                _record_tokens(usage.total_tokens)

            try:
                return _extract_json(content)
            except (json.JSONDecodeError, ValueError):
                if not repaired:
                    repaired = True
                    log.warning("%s: invalid JSON from %s, asking for a repair", label, model)
                    messages = list(base_messages) + [
                        {"role": "assistant", "content": content[:2000]},
                        {"role": "user", "content":
                            "That was not valid JSON. Reply with ONLY the JSON "
                            "object — no prose, no markdown fences."},
                    ]
                    continue
                log.warning("%s: still invalid JSON from %s after repair", label, model)
                break  # move on to the fallback model

        if model != FALLBACK_MODEL:
            log.warning("%s: falling back to %s", label, FALLBACK_MODEL)

    raise CuratorError(f"{label}: could not obtain valid JSON from Groq")


def _retry_after(exc: Exception) -> int:
    m = re.search(r"(?:retry|try) again in ([\d.]+)s", str(exc), re.I)
    if m:
        return int(float(m.group(1))) + 1
    return 0


# --------------------------------------------------------------------------- #
# PASS 1 — batch scoring
# --------------------------------------------------------------------------- #


def _batched(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def score_items(items: list[dict]) -> list[dict]:
    """Return items annotated with `significance`, `category`, `score_reason`.

    Items in a batch that fails twice are dropped from the pool (logged).
    """
    system = load_prompt("curator_pass1.md")
    scored: list[dict] = []
    batches = list(_batched(items, PASS1_BATCH_SIZE))
    log.info("pass 1: scoring %d items in %d batch(es)", len(items), len(batches))

    def _payload(subset: list[dict]) -> str:
        return json.dumps(
            [
                {
                    "id": it["_id"],
                    "title": it["title"],
                    "source": it["source"],
                    "snippet": it["snippet"][:PASS1_SNIPPET_CAP],
                }
                for it in subset
            ],
            ensure_ascii=False,
        )

    for bi, batch in enumerate(batches, 1):
        by_id = {it["_id"]: it for it in batch}
        pending = dict(by_id)  # ids still needing a score

        for attempt in (1, 2):
            if not pending:
                break
            try:
                result = llm_json(
                    system,
                    _payload(list(pending.values())),
                    label=f"pass1[{bi}/{len(batches)}]"
                    + ("" if attempt == 1 else f" backfill x{len(pending)}"),
                    max_tokens=PASS1_MAX_TOKENS,
                )
            except CuratorError as e:
                log.error("pass 1: batch %d failed permanently: %s", bi, e)
                break
            for rec in result.get("scores", []):
                it = pending.pop(rec.get("id"), None)
                if it is None:
                    continue
                category = rec.get("category", "")
                if category not in _CATEGORY_SET:
                    category = _closest_category(category)
                it["significance"] = _clamp_int(rec.get("significance"), 1, 10, default=3)
                it["category"] = category
                it["score_reason"] = str(rec.get("reason", "")).strip()
                scored.append(it)

        if pending:
            log.warning("pass 1: batch %d dropped %d unscored item(s)", bi, len(pending))

    cats: dict[str, int] = {}
    solid: dict[str, int] = {}
    for it in scored:
        cats[it["category"]] = cats.get(it["category"], 0) + 1
        if it["significance"] >= FLOOR_MIN_SIGNIFICANCE:
            solid[it["category"]] = solid.get(it["category"], 0) + 1
    log.info("pass 1: %d/%d scored — by category %s", len(scored), len(items),
             {c: f"{cats.get(c, 0)} ({solid.get(c, 0)} solid)" for c in CATEGORIES})
    return scored


def _clamp_int(value, lo: int, hi: int, *, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _closest_category(raw: str) -> str:
    raw_l = (raw or "").lower()
    for cat in CATEGORIES:
        if cat.lower() in raw_l or raw_l in cat.lower():
            return cat
    aliases = {
        "tooling": "Tooling & Infra", "infra": "Tooling & Infra",
        "tools": "Tooling & Infra", "industry": "Industry & Business",
        "business": "Industry & Business", "community": "Community Discourse",
        "discourse": "Community Discourse", "release": "Model Releases",
        "model": "Model Releases", "paper": "Research", "research": "Research",
    }
    for key, cat in aliases.items():
        if key in raw_l:
            return cat
    return "Community Discourse"


# --------------------------------------------------------------------------- #
# PASS 2a — cluster + select finalists
# --------------------------------------------------------------------------- #


def select_finalists(scored: list[dict]) -> list[dict]:
    """Return finalist cluster specs: {primary, duplicates: [...], category}."""
    pool = sorted(scored, key=lambda it: it["significance"], reverse=True)[:SELECT_POOL_SIZE]
    by_id = {it["_id"]: it for it in pool}

    system = (
        load_prompt("curator_pass2_select.md")
        .replace("{min_items}", str(MIN_ITEMS))
        .replace("{max_items}", str(MAX_ITEMS))
        .replace("{cat_cap}", str(CATEGORY_CAP))
    )
    payload = [
        {
            "id": it["_id"],
            "title": it["title"],
            "source": it["source"],
            "significance": it["significance"],
            "category": it["category"],
            "reason": it["score_reason"],
        }
        for it in pool
    ]
    result = llm_json(
        system,
        json.dumps(payload, ensure_ascii=False),
        label="pass2a-select",
        max_tokens=3500,
        reasoning_effort="low",  # medium ate the output budget and truncated JSON (see CLAUDE.md)
    )

    finalists: list[dict] = []
    used_primary: set[int] = set()
    for spec in result.get("finalists", []):
        pid = spec.get("primary_id")
        primary = by_id.get(pid)
        if primary is None or pid in used_primary:
            continue
        used_primary.add(pid)
        dups = [by_id[d] for d in spec.get("duplicate_ids", []) if d in by_id and d != pid]
        category = spec.get("category")
        if category not in _CATEGORY_SET:
            category = primary["category"]
        finalists.append({"primary": primary, "duplicates": dups, "category": category})

    used_ids = set(used_primary)
    for f in finalists:
        used_ids.update(d["_id"] for d in f["duplicates"])

    finalists = _enforce_balance(finalists, scored, used_ids)
    dist = {}
    for f in finalists:
        dist[f["category"]] = dist.get(f["category"], 0) + 1
    log.info("pass 2a: %d finalists selected — %s", len(finalists),
             ", ".join(f"{k}:{v}" for k, v in dist.items()))
    return finalists


FLOOR_PER_CATEGORY = 2       # min finalists for a category that has real material
FLOOR_MIN_SIGNIFICANCE = 5   # ...where "real material" means >=2 items at this score
                            # (5 = "solid / real substance" on the pass-1 rubric)


def _enforce_balance(
    finalists: list[dict],
    scored: list[dict],
    used_ids: set[int],
) -> list[dict]:
    """Balance the finalist set: cap any category, guarantee a floor for
    categories with real material, keep the total in [MIN_ITEMS, MAX_ITEMS]."""

    def _add(it: dict, per_cat: dict, kept: list) -> None:
        per_cat[it["category"]] = per_cat.get(it["category"], 0) + 1
        used_ids.add(it["_id"])
        kept.append({"primary": it, "duplicates": [], "category": it["category"]})

    # 1. cap per category, highest significance first
    kept: list[dict] = []
    per_cat: dict[str, int] = {}
    for f in sorted(finalists, key=lambda x: x["primary"]["significance"], reverse=True):
        if per_cat.get(f["category"], 0) >= CATEGORY_CAP:
            continue
        per_cat[f["category"]] = per_cat.get(f["category"], 0) + 1
        kept.append(f)

    unused = sorted(
        (it for it in scored if it["_id"] not in used_ids),
        key=lambda it: it["significance"], reverse=True,
    )

    # 2. category floor: any category with >=2 solid items gets >=FLOOR finalists
    solid_by_cat: dict[str, int] = {}
    for it in scored:
        if it["significance"] >= FLOOR_MIN_SIGNIFICANCE:
            solid_by_cat[it["category"]] = solid_by_cat.get(it["category"], 0) + 1
    for cat, n_solid in solid_by_cat.items():
        if n_solid < FLOOR_PER_CATEGORY:
            continue
        while per_cat.get(cat, 0) < FLOOR_PER_CATEGORY and len(kept) < MAX_ITEMS:
            pick = next((it for it in unused if it["category"] == cat
                         and it["_id"] not in used_ids), None)
            if pick is None:
                break
            _add(pick, per_cat, kept)

    # 3. pad up to MIN_ITEMS by raw significance
    for it in unused:
        if len(kept) >= MIN_ITEMS:
            break
        if it["_id"] in used_ids or per_cat.get(it["category"], 0) >= CATEGORY_CAP:
            continue
        _add(it, per_cat, kept)

    # 4. trim to MAX_ITEMS, dropping lowest significance but never a category's
    #    last finalist when that category has material
    if len(kept) > MAX_ITEMS:
        protected = {c for c, n in solid_by_cat.items() if n >= FLOOR_PER_CATEGORY}
        kept.sort(key=lambda f: f["primary"]["significance"])
        result = list(kept)
        for f in kept:
            if len(result) <= MAX_ITEMS:
                break
            cat_count = sum(1 for x in result if x["category"] == f["category"])
            if f["category"] in protected and cat_count <= 1:
                continue
            result.remove(f)
        kept = result

    kept.sort(key=lambda f: f["primary"]["significance"], reverse=True)
    return kept[:MAX_ITEMS]


# --------------------------------------------------------------------------- #
# PASS 2b — write-ups
# --------------------------------------------------------------------------- #


def write_finalists(finalists: list[dict]) -> None:
    """Fill `summary` / `why_it_matters` on each finalist's primary item."""
    system = load_prompt("curator_pass2_writeups.md")
    flat = []
    for f in finalists:
        p = f["primary"]
        also = sorted({d["source"] for d in f["duplicates"] if d["source"] != p["source"]})
        flat.append((f, {
            "id": p["_id"],
            "title": p["title"],
            "source": p["source"],
            "url": p["url"],
            "snippet": p["snippet"],
            "also_covered_by": also,
        }))

    batches = list(_batched(flat, WRITEUP_BATCH_SIZE))
    log.info("pass 2b: writing %d finalists in %d batch(es)", len(flat), len(batches))
    writeups: dict[int, dict] = {}
    for bi, batch in enumerate(batches, 1):
        payload = [item for _, item in batch]
        try:
            result = llm_json(
                system,
                json.dumps(payload, ensure_ascii=False),
                label=f"pass2b[{bi}/{len(batches)}]",
                max_tokens=2200,
                temperature=0.3,
                reasoning_effort="medium",
            )
        except CuratorError as e:
            log.error("pass 2b: batch %d failed, finalists get snippet fallback: %s", bi, e)
            result = {}
        for rec in result.get("writeups", []):
            if rec.get("id") is not None:
                writeups[rec["id"]] = rec

    for f in finalists:
        p = f["primary"]
        w = writeups.get(p["_id"], {})
        p["summary"] = _clean_writeup(w.get("summary")) or _fallback_summary(p)
        p["why_it_matters"] = _clean_writeup(w.get("why_it_matters")) or ""


def _clean_writeup(text) -> str:
    if not text:
        return ""
    return " ".join(str(text).split())


def _fallback_summary(item: dict) -> str:
    snip = item.get("snippet", "").strip()
    if snip:
        return snip[:400]
    return item["title"]


# --------------------------------------------------------------------------- #
# Assemble final output
# --------------------------------------------------------------------------- #


def _reading_time(item: dict) -> int:
    """Rough minutes to read the linked source: a per-source-type base
    nudged by how much text we have. Deliberately approximate."""
    src = item["source"]
    minutes = _READ_MINUTES_BY_SOURCE.get(
        src, _READ_MINUTES_BY_SOURCE.get(src.split(":")[0], _READ_MINUTES_DEFAULT)
    )
    words = len((item.get("snippet", "") + " " + item.get("title", "")).split())
    if words > 90:
        minutes += 2
    elif words < 20:
        minutes -= 1
    return max(1, minutes)


def build_output(finalists: list[dict], considered: int, model: str) -> dict:
    buckets: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for f in finalists:
        p = f["primary"]
        seen_urls = {p["url"]}
        also = []
        for d in f["duplicates"]:
            if d["url"] in seen_urls:
                continue
            seen_urls.add(d["url"])
            also.append({"source": d["source"], "url": d["url"]})
        buckets[f["category"]].append({
            "title": p["title"],
            "url": p["url"],
            "source": p["source"],
            "summary": p.get("summary", ""),
            "why_it_matters": p.get("why_it_matters", ""),
            "reading_time_min": _reading_time(p),
            "significance": p["significance"],
            "also_covered_by": also,
        })

    categories = []
    for name in CATEGORIES:
        items = sorted(buckets[name], key=lambda x: x["significance"], reverse=True)
        if items:
            categories.append({"name": name, "items": items})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "items_considered": considered,
        "items_selected": sum(len(c["items"]) for c in categories),
        "categories": categories,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _dump_debug(name: str, obj) -> None:
    path = DATA_DIR / f"curator_debug_{name}.json"
    try:
        path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
        log.info("debug: wrote %s", path)
    except OSError as e:
        log.warning("debug: could not write %s: %s", path, e)


def curate(
    input_path: Path, *, dry_run: bool, output_path: Path, debug: bool = False
) -> dict:
    if not os.environ.get("GROQ_API_KEY"):
        raise CuratorError("GROQ_API_KEY is not set (put it in .env)")

    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise CuratorError(f"cannot read {input_path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise CuratorError(f"{input_path} has no items — run the collector first")

    for i, it in enumerate(raw):
        it["_id"] = i
    log.info("loaded %d raw items from %s", len(raw), input_path)

    scored = score_items(raw)
    if not scored:
        raise CuratorError("pass 1 produced no scored items")
    if debug:
        _dump_debug("pass1_scores", sorted(
            ({"id": it["_id"], "significance": it["significance"],
              "category": it["category"], "reason": it["score_reason"],
              "title": it["title"], "source": it["source"]} for it in scored),
            key=lambda x: x["significance"], reverse=True,
        ))

    finalists = select_finalists(scored)
    if not finalists:
        raise CuratorError("pass 2a selected no finalists")
    if debug:
        _dump_debug("pass2a_finalists", [
            {"category": f["category"],
             "primary": {"id": f["primary"]["_id"], "title": f["primary"]["title"],
                         "source": f["primary"]["source"],
                         "significance": f["primary"]["significance"]},
             "duplicates": [{"id": d["_id"], "title": d["title"],
                             "source": d["source"]} for d in f["duplicates"]]}
            for f in finalists
        ])

    write_finalists(finalists)
    output = build_output(finalists, considered=len(raw), model=PRIMARY_MODEL)

    if dry_run:
        log.info("dry run — not writing %s", output_path)
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("wrote %s (%d items across %d categories)",
                 output_path, output["items_selected"], len(output["categories"]))
    return output


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Curate the weekly digest.")
    parser.add_argument("--input", type=Path, default=RAW_INPUT)
    parser.add_argument("--output", type=Path, default=CURATED_OUTPUT)
    parser.add_argument("--dry-run", action="store_true",
                        help="print curated JSON to stdout, don't write the file")
    parser.add_argument("--debug", action="store_true",
                        help="dump pass-1 scores and pass-2a selection to data/curator_debug_*.json")
    parser.add_argument("--model", help="override GROQ_MODEL for this run")
    args = parser.parse_args(argv)

    if args.model:
        global PRIMARY_MODEL
        PRIMARY_MODEL = args.model

    try:
        curate(args.input, dry_run=args.dry_run, output_path=args.output,
               debug=args.debug)
    except CuratorError as e:
        log.error("curation failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
