#!/usr/bin/env python3
"""NASEM Daily Clips Aggregator — automated news clip collection.

Scans Google News RSS for NASEM-related coverage, deduplicates, categorizes
with Claude, and formats a daily clips digest.

Usage:
    python clips.py                          # Today's clips, plain text
    python clips.py --days 3                 # Last 3 days (for Mondays)
    python clips.py --html                   # HTML email format
    python clips.py --json                   # JSON output
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import anthropic
import httpx
from bs4 import BeautifulSoup

# --- Configuration ---

CLAUDE_MODEL = os.environ.get("CLIPS_MODEL", "claude-sonnet-4-20250514")

HEADERS = {"User-Agent": "NASEM-DailyClips/1.0 (science communications tool)"}

# Search terms from the Daily Clips Tutorial
SEARCH_TERMS = [
    '"National Academies of Sciences, Engineering, and Medicine"',
    '"National Academy of Sciences"',
    '"National Academy of Medicine"',
    '"National Academy of Engineering"',
    '"Transportation Research Board"',
    '"Gulf Research Program"',
    '"Institute of Medicine"',
    '"Marcia McNutt"',
    '"Victor Dzau"',
    '"Neil Shubin"',
    '"Monica Bertagnolli"',
    '"Tsu-Jae Liu"',
]

# Additional terms for PNAS clips
PNAS_SEARCH_TERMS = [
    '"PNAS" study',
    '"Proceedings of the National Academy of Sciences"',
    '"PNAS Nexus"',
]

# Prominence ranking for outlets (higher = more prominent)
OUTLET_TIERS = {
    # Tier 1 — major national
    "nytimes.com": 5, "washingtonpost.com": 5, "wsj.com": 5,
    "apnews.com": 5, "reuters.com": 5, "cnn.com": 5,
    "nbcnews.com": 5, "abcnews.go.com": 5, "cbsnews.com": 5,
    "politico.com": 5, "usatoday.com": 5, "bbc.com": 4, "bbc.co.uk": 4,
    # Tier 2 — major outlets
    "thehill.com": 4, "axios.com": 4, "npr.org": 4,
    "scientificamerican.com": 4, "nature.com": 4, "science.org": 4,
    "statnews.com": 4, "wired.com": 4, "arstechnica.com": 4,
    "theguardian.com": 4, "bloomberg.com": 4, "forbes.com": 4,
    # Tier 3 — solid outlets
    "theatlantic.com": 3, "vox.com": 3, "slate.com": 3,
    "pbs.org": 3, "time.com": 3, "newsweek.com": 3,
    "health.com": 3, "livescience.com": 3, "sciencedaily.com": 3,
    "phys.org": 3, "eurekalert.org": 3, "medicalxpress.com": 3,
}

# Domains to skip (press release wires, NASEM's own site)
SKIP_DOMAINS = {
    "nationalacademies.org", "www.nationalacademies.org",
    "nap.nationalacademies.org", "nasonline.org",
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "newswire.com",
}


# --- Google News RSS Scanner ---

def fetch_google_news_rss(query: str, days: int = 1) -> list[dict]:
    """Fetch articles from Google News RSS for a search query."""
    encoded = quote_plus(query)
    # when:Nd restricts to last N days
    url = f"https://news.google.com/rss/search?q={encoded}+when:{days}d&hl=en-US&gl=US&ceid=US:en"

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=15, headers=HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: RSS fetch failed for '{query[:40]}...': {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles = []

    for item in soup.find_all("item"):
        title = item.find("title")
        link = item.find("link")
        pub_date = item.find("pubDate")
        source_el = item.find("source")

        if not title or not link:
            continue

        title_text = title.get_text(strip=True)
        link_text = link.get_text(strip=True)
        source_name = source_el.get_text(strip=True) if source_el else ""
        source_url = source_el.get("url", "") if source_el else ""

        # Parse publication date
        pub_dt = None
        if pub_date:
            try:
                pub_dt = datetime.strptime(
                    pub_date.get_text(strip=True),
                    "%a, %d %b %Y %H:%M:%S %Z"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        articles.append({
            "title": title_text,
            "url": link_text,
            "source_name": source_name,
            "source_url": source_url,
            "published": pub_dt.isoformat() if pub_dt else "",
            "published_dt": pub_dt,
            "search_term": query,
        })

    return articles


def resolve_google_news_url(url: str) -> str:
    """Resolve Google News redirect URL to the actual article URL."""
    if "news.google.com" not in url:
        return url
    try:
        resp = httpx.head(url, follow_redirects=True, timeout=10, headers=HEADERS)
        return str(resp.url)
    except Exception:
        return url


def scan_all_sources(days: int = 1) -> list[dict]:
    """Scan Google News RSS for all search terms. Returns raw article list."""
    all_articles = []

    print(f"Scanning Google News for {len(SEARCH_TERMS)} NASEM terms + {len(PNAS_SEARCH_TERMS)} PNAS terms...",
          file=sys.stderr)

    for term in SEARCH_TERMS + PNAS_SEARCH_TERMS:
        articles = fetch_google_news_rss(term, days)
        for a in articles:
            a["is_pnas_search"] = term in PNAS_SEARCH_TERMS
        all_articles.extend(articles)
        time.sleep(0.5)  # Be polite to Google

    print(f"  Found {len(all_articles)} raw articles", file=sys.stderr)
    return all_articles


# --- Deduplication & Filtering ---

def _normalize_title(title: str) -> str:
    """Normalize title for comparison."""
    title = title.lower().strip()
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title)
    return title


def _title_similarity(a: str, b: str) -> float:
    """Jaccard similarity on word sets."""
    words_a = set(_normalize_title(a).split())
    words_b = set(_normalize_title(b).split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _get_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def _outlet_prominence(source_url: str, source_name: str) -> int:
    """Score outlet prominence (0-5). Higher = more prominent."""
    domain = _get_domain(source_url)
    if domain in OUTLET_TIERS:
        return OUTLET_TIERS[domain]
    # Check if source name matches any known outlet
    name_lower = source_name.lower()
    for d, score in OUTLET_TIERS.items():
        base = d.split(".")[0]
        if base in name_lower:
            return score
    return 1  # Unknown outlet


def deduplicate(articles: list[dict]) -> list[dict]:
    """Remove duplicate articles based on title similarity."""
    unique = []
    seen_titles = []

    for article in articles:
        title = article["title"]
        is_dup = False

        for seen in seen_titles:
            if _title_similarity(title, seen) > 0.55:
                is_dup = True
                break

        if not is_dup:
            unique.append(article)
            seen_titles.append(title)

    removed = len(articles) - len(unique)
    if removed:
        print(f"  Removed {removed} duplicates, {len(unique)} unique articles", file=sys.stderr)
    return unique


def filter_articles(articles: list[dict]) -> list[dict]:
    """Filter out NASEM's own pages and press release wires."""
    filtered = []
    for article in articles:
        domain = _get_domain(article.get("source_url", ""))
        if domain in SKIP_DOMAINS:
            continue
        # Also check the article URL itself
        article_domain = _get_domain(article.get("url", ""))
        if article_domain in SKIP_DOMAINS:
            continue
        filtered.append(article)

    removed = len(articles) - len(filtered)
    if removed:
        print(f"  Filtered {removed} press wires/NASEM pages, {len(filtered)} remain", file=sys.stderr)
    return filtered


def rank_articles(articles: list[dict]) -> list[dict]:
    """Sort articles by recency then prominence."""
    for a in articles:
        a["prominence"] = _outlet_prominence(a.get("source_url", ""), a.get("source_name", ""))

    return sorted(articles, key=lambda a: (
        a.get("published", ""),  # Most recent first
        a["prominence"],         # Then most prominent
    ), reverse=True)


# --- Claude-Powered Categorization ---

CATEGORIZE_SYSTEM = """You are a news editor at the National Academies of Sciences, Engineering, and Medicine (NASEM).
You are compiling the daily news clips digest — a curated summary of media coverage about NASEM and its work.

Your job: categorize, group, and annotate a list of news articles for the daily clips email."""

CATEGORIZE_PROMPT = """Here are today's news articles mentioning NASEM or related organizations.
Analyze them and return a JSON object with this structure:

{{
  "groups": [
    {{
      "topic": "Short topic label (report name, event, or theme)",
      "articles": [
        {{
          "index": 0,
          "summary": "1-sentence summary of what the article says about NASEM",
          "is_press_release_repost": false,
          "is_negative": false,
          "negative_note": ""
        }}
      ]
    }}
  ],
  "pnas_pick": {{
    "index": null,
    "reason": "Why this is a good PNAS clip (or null if none found)"
  }},
  "news_you_can_use": {{
    "index": null,
    "reason": "Why this is a good light/fun clip (or null if none found)"
  }}
}}

RULES:
1. Group articles by the report, event, or topic they cover. Most recent report/event first.
2. Flag articles that are just reposting a NASEM press release verbatim (is_press_release_repost=true).
3. Flag anything extremely negative (is_negative=true) with a brief note.
4. For pnas_pick: choose one article about a recent PNAS or PNAS Nexus paper, preferring one that links back to PNAS. Use the article index. null if none found.
5. For news_you_can_use: choose one lighter/fun clip suitable as a palate cleanser. null if none found.
6. Return ONLY valid JSON. No markdown, no commentary.

ARTICLES:
{articles_json}"""


def categorize_with_claude(articles: list[dict]) -> dict:
    """Use Claude to categorize, group, and annotate articles."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # Prepare article list for Claude (strip internal fields)
    articles_for_claude = []
    for i, a in enumerate(articles):
        articles_for_claude.append({
            "index": i,
            "title": a["title"],
            "source": a["source_name"],
            "published": a.get("published", ""),
            "is_pnas_search": a.get("is_pnas_search", False),
        })

    prompt = CATEGORIZE_PROMPT.format(
        articles_json=json.dumps(articles_for_claude, indent=2, ensure_ascii=False)
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=CATEGORIZE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()

    # Extract JSON from response (handle markdown code blocks)
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    return json.loads(text)


# --- Output Formatting ---

def format_plain(articles: list[dict], categories: dict, date_label: str) -> str:
    """Format as plain text for terminal/review."""
    lines = [f"NASEM DAILY CLIPS — {date_label}", "=" * 50, ""]

    for group in categories.get("groups", []):
        lines.append(f">> {group['topic']}")
        lines.append("-" * 40)
        for art in group["articles"]:
            idx = art["index"]
            if idx >= len(articles):
                continue
            a = articles[idx]
            prefix = ""
            if art.get("is_press_release_repost"):
                prefix = "[PRESS RELEASE REPOST] "
            if art.get("is_negative"):
                prefix += "[NEGATIVE] "
            lines.append(f"  {prefix}{a['title']}")
            lines.append(f"    Source: {a['source_name']}")
            lines.append(f"    {art.get('summary', '')}")
            lines.append(f"    URL: {a['url']}")
            lines.append("")
        lines.append("")

    # PNAS pick
    pnas = categories.get("pnas_pick", {})
    if pnas.get("index") is not None and pnas["index"] < len(articles):
        lines.append(">> PNAS CLIP")
        lines.append("-" * 40)
        a = articles[pnas["index"]]
        lines.append(f"  {a['title']}")
        lines.append(f"    Source: {a['source_name']}")
        lines.append(f"    Reason: {pnas.get('reason', '')}")
        lines.append(f"    URL: {a['url']}")
        lines.append("")

    # News You Can Use
    nycu = categories.get("news_you_can_use", {})
    if nycu.get("index") is not None and nycu["index"] < len(articles):
        lines.append(">> NEWS YOU CAN USE")
        lines.append("-" * 40)
        a = articles[nycu["index"]]
        lines.append(f"  {a['title']}")
        lines.append(f"    Source: {a['source_name']}")
        lines.append(f"    Reason: {nycu.get('reason', '')}")
        lines.append(f"    URL: {a['url']}")
        lines.append("")

    lines.append(f"\nTotal: {len(articles)} unique articles")
    return "\n".join(lines)


def format_html(articles: list[dict], categories: dict, date_label: str) -> str:
    """Format as styled HTML suitable for email."""
    groups_html = []

    for group in categories.get("groups", []):
        items = []
        for art in group["articles"]:
            idx = art["index"]
            if idx >= len(articles):
                continue
            a = articles[idx]
            flags = ""
            if art.get("is_press_release_repost"):
                flags += '<span style="background:#fef3c7;color:#92400e;padding:2px 6px;border-radius:3px;font-size:11px;margin-left:6px;">PRESS RELEASE</span>'
            if art.get("is_negative"):
                flags += '<span style="background:#fef2f2;color:#991b1b;padding:2px 6px;border-radius:3px;font-size:11px;margin-left:6px;">NEGATIVE</span>'
                if art.get("negative_note"):
                    flags += f'<span style="font-size:12px;color:#991b1b;margin-left:4px;">({art["negative_note"]})</span>'

            prominence_stars = "+" * a.get("prominence", 1)
            items.append(f"""<li style="margin-bottom:12px;line-height:1.5;">
                <a href="{a['url']}" style="color:#1a5276;font-weight:600;text-decoration:none;font-size:15px;">{a['title']}</a>{flags}<br>
                <span style="color:#666;font-size:13px;"><strong>{a['source_name']}</strong> [{prominence_stars}] &mdash; {art.get('summary', '')}</span>
            </li>""")

        if items:
            groups_html.append(f"""
            <div style="margin-bottom:24px;">
                <h3 style="font-size:16px;font-weight:700;color:#1a5276;margin-bottom:8px;border-bottom:2px solid #e2e8f0;padding-bottom:4px;">{group['topic']}</h3>
                <ul style="margin:0;padding-left:20px;list-style:disc;">{''.join(items)}</ul>
            </div>""")

    # PNAS pick section
    pnas_html = ""
    pnas = categories.get("pnas_pick", {})
    if pnas.get("index") is not None and pnas["index"] < len(articles):
        a = articles[pnas["index"]]
        pnas_html = f"""
        <div style="margin-bottom:24px;background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:14px 18px;">
            <h3 style="font-size:14px;font-weight:700;color:#0369a1;margin-bottom:6px;">PNAS Clip</h3>
            <a href="{a['url']}" style="color:#1a5276;font-weight:600;text-decoration:none;font-size:15px;">{a['title']}</a><br>
            <span style="color:#666;font-size:13px;"><strong>{a['source_name']}</strong> &mdash; {pnas.get('reason', '')}</span>
        </div>"""

    # News You Can Use section
    nycu_html = ""
    nycu = categories.get("news_you_can_use", {})
    if nycu.get("index") is not None and nycu["index"] < len(articles):
        a = articles[nycu["index"]]
        nycu_html = f"""
        <div style="margin-bottom:24px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:14px 18px;">
            <h3 style="font-size:14px;font-weight:700;color:#166534;margin-bottom:6px;">News You Can Use</h3>
            <a href="{a['url']}" style="color:#1a5276;font-weight:600;text-decoration:none;font-size:15px;">{a['title']}</a><br>
            <span style="color:#666;font-size:13px;"><strong>{a['source_name']}</strong> &mdash; {nycu.get('reason', '')}</span>
        </div>"""

    return f"""<div style="font-family:'DM Sans',Helvetica,Arial,sans-serif;max-width:700px;margin:20px auto;">
    <div style="background:#1a5276;color:white;padding:18px 24px;border-radius:8px 8px 0 0;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;opacity:0.85;margin-bottom:4px;">Daily Clips</div>
        <div style="font-size:22px;font-weight:700;">NASEM News Coverage</div>
        <div style="font-size:14px;opacity:0.85;margin-top:4px;">{date_label} &mdash; {len(articles)} articles</div>
    </div>
    <div style="background:white;padding:20px 24px;border:1px solid #e0e0e0;border-top:0;border-radius:0 0 8px 8px;">
        {''.join(groups_html)}
        {pnas_html}
        {nycu_html}
        <div style="font-size:12px;color:#999;border-top:1px solid #f0f0f0;padding-top:10px;margin-top:16px;">
            Generated {datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p")} UTC | NASEM Daily Clips Aggregator
        </div>
    </div>
</div>"""


def format_json(articles: list[dict], categories: dict, date_label: str) -> str:
    """Format as JSON."""
    # Clean articles for JSON output (remove internal fields)
    clean_articles = []
    for a in articles:
        clean_articles.append({
            "title": a["title"],
            "url": a["url"],
            "source_name": a["source_name"],
            "published": a.get("published", ""),
            "prominence": a.get("prominence", 1),
        })

    return json.dumps({
        "date": date_label,
        "article_count": len(articles),
        "articles": clean_articles,
        "categories": categories,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, ensure_ascii=False)


# --- Main Pipeline ---

def run_pipeline(days: int = 1, fmt: str = "text", use_claude: bool = True) -> str:
    """Run the full clips pipeline. Returns formatted output."""
    today = datetime.now(timezone.utc)
    if days == 1:
        date_label = today.strftime("%A, %B %d, %Y")
    else:
        start = (today - timedelta(days=days)).strftime("%B %d")
        end = today.strftime("%B %d, %Y")
        date_label = f"{start} — {end}"

    # Step 1: Scan sources
    raw = scan_all_sources(days)
    if not raw:
        return "No articles found."

    # Step 2: Filter
    filtered = filter_articles(raw)

    # Step 3: Deduplicate
    unique = deduplicate(filtered)
    if not unique:
        return "No articles found after filtering."

    # Step 4: Rank
    ranked = rank_articles(unique)

    # Cap at 50 articles for Claude
    if len(ranked) > 50:
        print(f"  Capping at 50 articles (had {len(ranked)})", file=sys.stderr)
        ranked = ranked[:50]

    # Step 5: Categorize with Claude
    if use_claude:
        print("Categorizing with Claude...", file=sys.stderr)
        try:
            categories = categorize_with_claude(ranked)
        except Exception as e:
            print(f"  Claude categorization failed: {e}", file=sys.stderr)
            # Fallback: single group, no special picks
            categories = {
                "groups": [{"topic": "All Coverage", "articles": [
                    {"index": i, "summary": "", "is_press_release_repost": False, "is_negative": False}
                    for i in range(len(ranked))
                ]}],
                "pnas_pick": {"index": None, "reason": None},
                "news_you_can_use": {"index": None, "reason": None},
            }
    else:
        categories = {
            "groups": [{"topic": "All Coverage", "articles": [
                {"index": i, "summary": "", "is_press_release_repost": False, "is_negative": False}
                for i in range(len(ranked))
            ]}],
            "pnas_pick": {"index": None, "reason": None},
            "news_you_can_use": {"index": None, "reason": None},
        }

    # Step 6: Format
    if fmt == "html":
        return format_html(ranked, categories, date_label)
    elif fmt == "json":
        return format_json(ranked, categories, date_label)
    else:
        return format_plain(ranked, categories, date_label)


def main():
    parser = argparse.ArgumentParser(description="NASEM Daily Clips Aggregator")
    parser.add_argument("--days", type=int, default=1,
                        help="Number of days to look back (default: 1, use 3 for Mondays)")
    parser.add_argument("--html", action="store_true", help="Output as styled HTML")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--no-claude", action="store_true",
                        help="Skip Claude categorization (just scan and dedup)")

    args = parser.parse_args()

    fmt = "html" if args.html else ("json" if args.json else "text")
    result = run_pipeline(days=args.days, fmt=fmt, use_claude=not args.no_claude)
    print(result)


if __name__ == "__main__":
    main()
