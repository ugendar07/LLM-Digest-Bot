"""Component 3 — Digest builder.

Turns ``data/curated_digest.json`` (from the curator) into a single
email-safe HTML string. Pure templating: no network, no LLM, no external
CSS or fonts — every style is an inline ``style="..."`` attribute because
Gmail and most other clients strip ``<style>`` blocks and linked stylesheets.

Usage::

    python src/digest_builder.py                                   # HTML to stdout
    python src/digest_builder.py --input data/curated_digest.json --output preview.html

The mailer imports :func:`build_html` directly.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CURATED_INPUT = DATA_DIR / "curated_digest.json"

# Must match the collector's lookback window — used only for the header's
# "week of" date range.
LOOKBACK_DAYS = 7

# Fixed section order. A category absent from the input, or present but empty,
# is skipped entirely (no bare header).
CATEGORY_ORDER = [
    "Model Releases",
    "Research",
    "Tooling & Infra",
    "Industry & Business",
    "Community Discourse",
]

# --- palette / type (kept tiny and neutral; all applied inline) ------------- #
FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)
INK = "#1a1a1a"
MUTED = "#6b7280"
FAINT = "#9ca3af"
ACCENT = "#2563eb"
HAIRLINE = "#e5e7eb"
WHY_BG = "#f5f7ff"
BADGE_BG = "#eef1f4"
PAGE_BG = "#f3f4f6"
CARD_BG = "#ffffff"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _esc(text: object) -> str:
    """Escape text for HTML body context."""
    return html.escape("" if text is None else str(text), quote=False)


def _attr(text: object) -> str:
    """Escape text for an HTML attribute value."""
    return html.escape("" if text is None else str(text), quote=True)


def _safe_url(url: object) -> str:
    """Return the URL if it is a plain http(s) link, else '' (drop it)."""
    s = ("" if url is None else str(url)).strip()
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    if parts.scheme in ("http", "https") and parts.netloc:
        return s
    return ""


def _domain(url: str) -> str:
    """'https://www.openai.com/index/x' -> 'openai.com' (for link labels)."""
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or url


def _fmt_range(generated_at: str) -> str:
    """Human date range the digest covers, e.g. 'Aug 30 – Sep 6, 2026'."""
    try:
        end = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        end = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    if (start.month, start.year) == (end.month, end.year):
        return f"{start:%b} {start.day}–{end.day}, {end:%Y}"
    if start.year == end.year:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end:%Y}"
    return f"{start:%b} {start.day}, {start:%Y} – {end:%b} {end.day}, {end:%Y}"


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _render_also_covered(also: list[dict], primary_url: str) -> str:
    seen_urls = {primary_url}
    seen_domains = {_domain(primary_url)} if primary_url else set()
    links = []
    for entry in also or []:
        u = _safe_url(entry.get("url") if isinstance(entry, dict) else entry)
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        dom = _domain(u)
        if dom in seen_domains:  # one link per outlet keeps the line readable
            continue
        seen_domains.add(dom)
        links.append(
            f'<a href="{_attr(u)}" style="color:{MUTED};text-decoration:underline;">'
            f'{_esc(dom)}</a>'
        )
    if not links:
        return ""
    return (
        f'<div style="margin-top:8px;font-size:12px;color:{FAINT};">'
        f'Also covered by: {", ".join(links)}</div>'
    )


def _render_item(item: dict) -> str:
    title = _esc(item.get("title") or "(untitled)")
    url = _safe_url(item.get("url"))
    summary = _esc(item.get("summary") or "")
    why = _esc(item.get("why_it_matters") or "")
    try:
        minutes = int(item.get("reading_time_min") or 0)
    except (TypeError, ValueError):
        minutes = 0

    title_html = (
        f'<a href="{_attr(url)}" style="color:{ACCENT};text-decoration:none;">{title}</a>'
        if url else title
    )

    badge = ""
    if minutes > 0:
        badge = (
            f'<span style="display:inline-block;background-color:{BADGE_BG};'
            f'color:{MUTED};font-size:12px;line-height:1;padding:4px 9px;'
            f'border-radius:11px;white-space:nowrap;">{minutes} min read</span>'
        )

    why_html = ""
    if why:
        why_html = (
            f'<div style="margin-top:10px;padding:8px 12px;background-color:{WHY_BG};'
            f'border-left:3px solid {ACCENT};font-size:14px;color:{INK};">'
            f'<strong style="color:{ACCENT};">Why it matters:</strong> {why}</div>'
        )

    summary_html = ""
    if summary:
        summary_html = (
            f'<div style="margin-top:8px;font-size:15px;line-height:1.55;'
            f'color:{INK};">{summary}</div>'
        )

    also_html = _render_also_covered(item.get("also_covered_by") or [], url)

    return (
        f'<div style="padding:18px 0;border-bottom:1px solid {HAIRLINE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr>'
        f'<td style="font-size:16px;font-weight:600;line-height:1.4;color:{INK};'
        f'padding-right:10px;">{title_html}</td>'
        f'<td style="width:1%;vertical-align:top;text-align:right;">{badge}</td>'
        f'</tr></table>'
        f'{summary_html}{why_html}{also_html}'
        f'</div>'
    )


def _render_category(name: str, items: list[dict]) -> str:
    rows = "".join(_render_item(it) for it in items)
    return (
        f'<div style="margin-top:28px;">'
        f'<h2 style="margin:0 0 4px;font-size:13px;font-weight:700;'
        f'letter-spacing:0.08em;text-transform:uppercase;color:{ACCENT};'
        f'border-bottom:2px solid {ACCENT};padding-bottom:6px;">{_esc(name)}</h2>'
        f'{rows}</div>'
    )


def build_html(data: dict) -> str:
    """Render a curated-digest dict into an email-safe HTML document."""
    categories = {
        c.get("name"): (c.get("items") or [])
        for c in data.get("categories", [])
        if isinstance(c, dict)
    }
    sections = [
        _render_category(name, categories[name])
        for name in CATEGORY_ORDER
        if categories.get(name)
    ]
    # include any unexpected category names too, after the known ones
    for name, items in categories.items():
        if name not in CATEGORY_ORDER and items:
            sections.append(_render_category(name, items))

    total = sum(len(items) for items in categories.values())
    considered = data.get("items_considered")
    date_range = _fmt_range(data.get("generated_at", ""))
    # "groq/openai/gpt-oss-120b" -> "gpt-oss-120b" for the footer
    model = _esc((data.get("model") or "an LLM").rsplit("/", 1)[-1])

    body_inner = "".join(sections) or (
        f'<p style="color:{MUTED};font-size:15px;">No items curated this week.</p>'
    )

    considered_note = (
        f" from {considered} collected items" if isinstance(considered, int) else ""
    )
    footer = (
        f'<div style="margin-top:32px;padding-top:16px;border-top:1px solid {HAIRLINE};'
        f'font-size:12px;line-height:1.6;color:{FAINT};">'
        f'{total} stor{"y" if total == 1 else "ies"} this week{considered_note}. '
        f'Automatically selected and summarized by {model} — no human review; '
        f'summaries may contain errors, so follow the links for primary sources.'
        f'</div>'
    )

    header = (
        f'<div style="border-bottom:2px solid {INK};padding-bottom:12px;">'
        f'<h1 style="margin:0;font-size:22px;font-weight:800;color:{INK};">'
        f'This Week at AI/ML</h1>'
        f'<div style="margin-top:4px;font-size:14px;color:{MUTED};">'
        f'Week of {_esc(date_range)}</div>'
        f'</div>'
    )

    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>This Week at AI/ML — Week of {_esc(date_range)}</title>\n'
        '</head>\n'
        f'<body style="margin:0;padding:0;background-color:{PAGE_BG};">\n'
        f'<div style="max-width:640px;margin:0 auto;padding:24px 16px;">\n'
        f'<div style="background-color:{CARD_BG};border:1px solid {HAIRLINE};'
        f'border-radius:8px;padding:28px 28px 32px;font-family:{FONT_STACK};'
        f'color:{INK};">\n'
        f'{header}\n{body_inner}\n{footer}\n'
        '</div>\n</div>\n</body>\n</html>\n'
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the HTML digest email body.")
    parser.add_argument("--input", type=Path, default=CURATED_INPUT,
                        help="curated digest JSON (default: data/curated_digest.json)")
    parser.add_argument("--output", type=Path,
                        help="write HTML here instead of stdout (for browser preview)")
    args = parser.parse_args(argv)

    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"error: cannot read {args.input}: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"error: {args.input} is not valid JSON: {e}", file=sys.stderr)
        return 1

    htmldoc = build_html(data)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(htmldoc, encoding="utf-8")
        print(f"wrote {args.output} ({len(htmldoc)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(htmldoc)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
