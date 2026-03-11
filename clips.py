#!/usr/bin/env python3
"""NASEM Daily Clips Aggregator — automated news clip collection.

Scans Google News and Bing News for NASEM-related coverage, separates
institutional coverage from PNAS paper coverage, deduplicates, categorizes
with Claude, and formats a daily clips digest.

Usage:
    python clips.py                          # Today's clips, plain text
    python clips.py --days 3                 # Last 3 days (for Mondays)
    python clips.py --html                   # HTML email format
    python clips.py --json                   # JSON output
"""

import argparse
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

# Search terms for institutional NASEM coverage
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

# Search terms specifically for PNAS paper coverage (separate tab)
PNAS_SEARCH_TERMS = [
    '"PNAS" study',
    '"Proceedings of the National Academy of Sciences"',
    '"PNAS Nexus"',
]

# Bing News RSS terms (supplementary)
BING_SEARCH_TERMS = [
    '"national academies"',
    '"national academies of sciences"',
    '"National Academy of Medicine"',
    '"Transportation Research Board"',
]

BING_PNAS_TERMS = [
    "PNAS study",
]

# Optional: Google Alerts RSS feeds
GOOGLE_ALERT_FEEDS: list[str] = []

# Prominence ranking for outlets (higher = more prominent)
OUTLET_TIERS = {
    "nytimes.com": 5, "washingtonpost.com": 5, "wsj.com": 5,
    "apnews.com": 5, "reuters.com": 5, "cnn.com": 5,
    "nbcnews.com": 5, "abcnews.go.com": 5, "cbsnews.com": 5,
    "politico.com": 5, "usatoday.com": 5, "bbc.com": 4, "bbc.co.uk": 4,
    "thehill.com": 4, "axios.com": 4, "npr.org": 4,
    "scientificamerican.com": 4, "nature.com": 4, "science.org": 4,
    "statnews.com": 4, "wired.com": 4, "arstechnica.com": 4,
    "theguardian.com": 4, "bloomberg.com": 4, "forbes.com": 4,
    "theatlantic.com": 3, "vox.com": 3, "slate.com": 3,
    "pbs.org": 3, "time.com": 3, "newsweek.com": 3,
    "health.com": 3, "livescience.com": 3, "sciencedaily.com": 3,
    "phys.org": 3, "eurekalert.org": 3, "medicalxpress.com": 3,
}

# Domains to skip (NASEM's own sites + press release wires)
SKIP_DOMAINS = {
    "nationalacademies.org", "www.nationalacademies.org",
    "nap.nationalacademies.org", "nap.edu",
    "nasonline.org", "www.nasonline.org",
    "nam.edu", "www.nam.edu",
    "nae.edu", "www.nae.edu",
    "trb.org", "www.trb.org",
    "iom.edu", "www.iom.edu",
    "pnas.org", "www.pnas.org",
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "newswire.com",
}

# Keywords that indicate an article is about a PNAS paper, not NASEM the institution
PNAS_TITLE_KEYWORDS = [
    "pnas", "proceedings of the national academy",
    "pnas nexus", "published in the proceedings",
]

# Keywords that strongly suggest institutional NASEM coverage (not just PNAS)
NASEM_INSTITUTIONAL_KEYWORDS = [
    "national academies", "nasem", "academy report",
    "academy panel", "academy committee", "academy study",
    "transportation research board", "gulf research program",
    "institute of medicine", "national academy of medicine",
    "national academy of engineering",
    "marcia mcnutt", "victor dzau", "neil shubin",
    "monica bertagnolli", "tsu-jae liu",
]


# --- RSS Scanners ---

def fetch_google_news_rss(query: str, days: int = 1) -> list[dict]:
    """Fetch articles from Google News RSS for a search query."""
    encoded = quote_plus(query)
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
            "source_type": "google_news",
        })

    return articles


def fetch_bing_news_rss(query: str, days: int = 1) -> list[dict]:
    """Fetch articles from Bing News RSS feed."""
    encoded = quote_plus(query)
    freshness = "Day" if days <= 1 else ("Week" if days <= 7 else "Month")
    url = f"https://www.bing.com/news/search?q={encoded}&format=rss&freshness={freshness}"

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=15, headers=HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Bing RSS failed for '{query[:40]}...': {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles = []

    for item in soup.find_all("item"):
        title = item.find("title")
        link = item.find("link")
        pub_date = item.find("pubDate")
        source_el = item.find("news:Source") or item.find("source")

        if not title or not link:
            continue

        title_text = title.get_text(strip=True)
        link_text = link.get_text(strip=True)
        source_name = source_el.get_text(strip=True) if source_el else ""

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
            "source_url": "",
            "published": pub_dt.isoformat() if pub_dt else "",
            "published_dt": pub_dt,
            "search_term": query,
            "source_type": "bing_news",
        })

    return articles


def fetch_google_alert_rss(feed_url: str) -> list[dict]:
    """Fetch articles from a Google Alerts RSS feed."""
    try:
        resp = httpx.get(feed_url, follow_redirects=True, timeout=15, headers=HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Warning: Google Alert fetch failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles = []

    for entry in soup.find_all("entry"):
        title = entry.find("title")
        link = entry.find("link")
        published = entry.find("published") or entry.find("updated")

        if not title:
            continue

        url = ""
        if link:
            url = link.get("href", "") or link.get_text(strip=True)

        pub_dt = None
        if published:
            try:
                date_str = published.get_text(strip=True)
                pub_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        title_text = BeautifulSoup(title.get_text(), "html.parser").get_text(strip=True)

        articles.append({
            "title": title_text,
            "url": url,
            "source_name": "",
            "source_url": "",
            "published": pub_dt.isoformat() if pub_dt else "",
            "published_dt": pub_dt,
            "search_term": "google_alert",
            "source_type": "google_alert",
        })

    return articles


# --- Scanning ---

def scan_all_sources(days: int = 1) -> tuple[list[dict], list[dict]]:
    """Scan all sources. Returns (nasem_articles, pnas_articles) separately."""
    nasem_raw = []
    pnas_raw = []

    # 1. Google News RSS — NASEM institutional terms
    print(f"Scanning Google News RSS ({len(SEARCH_TERMS)} NASEM terms)...", file=sys.stderr)
    for term in SEARCH_TERMS:
        articles = fetch_google_news_rss(term, days)
        nasem_raw.extend(articles)
        time.sleep(0.5)
    print(f"  Google News NASEM: {len(nasem_raw)} articles", file=sys.stderr)

    # 2. Google News RSS — PNAS terms (goes to PNAS tab)
    print(f"Scanning Google News RSS ({len(PNAS_SEARCH_TERMS)} PNAS terms)...", file=sys.stderr)
    for term in PNAS_SEARCH_TERMS:
        articles = fetch_google_news_rss(term, days)
        pnas_raw.extend(articles)
        time.sleep(0.5)
    print(f"  Google News PNAS: {len(pnas_raw)} articles", file=sys.stderr)

    # 3. Bing News RSS — NASEM terms
    print(f"Scanning Bing News RSS ({len(BING_SEARCH_TERMS)} NASEM terms)...", file=sys.stderr)
    for term in BING_SEARCH_TERMS:
        articles = fetch_bing_news_rss(term, days)
        nasem_raw.extend(articles)
        time.sleep(0.5)

    # 4. Bing News RSS — PNAS terms
    print(f"Scanning Bing News RSS ({len(BING_PNAS_TERMS)} PNAS terms)...", file=sys.stderr)
    for term in BING_PNAS_TERMS:
        articles = fetch_bing_news_rss(term, days)
        pnas_raw.extend(articles)
        time.sleep(0.5)

    # 5. Google Alerts RSS feeds (if configured — goes to NASEM)
    if GOOGLE_ALERT_FEEDS:
        print(f"Checking {len(GOOGLE_ALERT_FEEDS)} Google Alert feeds...", file=sys.stderr)
        for feed_url in GOOGLE_ALERT_FEEDS:
            articles = fetch_google_alert_rss(feed_url)
            nasem_raw.extend(articles)
            time.sleep(0.5)

    print(f"  Total raw: {len(nasem_raw)} NASEM + {len(pnas_raw)} PNAS", file=sys.stderr)
    return nasem_raw, pnas_raw


# --- Deduplication & Filtering ---

def _normalize_title(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title)
    return title


def _title_similarity(a: str, b: str) -> float:
    words_a = set(_normalize_title(a).split())
    words_b = set(_normalize_title(b).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _get_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def _outlet_prominence(source_url: str, source_name: str) -> int:
    domain = _get_domain(source_url)
    if domain in OUTLET_TIERS:
        return OUTLET_TIERS[domain]
    name_lower = source_name.lower()
    for d, score in OUTLET_TIERS.items():
        base = d.split(".")[0]
        if base in name_lower:
            return score
    return 1


def _is_pnas_article(title: str) -> bool:
    """Check if an article title indicates PNAS paper coverage (not institutional)."""
    title_lower = title.lower()
    for kw in PNAS_TITLE_KEYWORDS:
        if kw in title_lower:
            return True
    return False


def _is_nasem_institutional(title: str) -> bool:
    """Check if title has clear institutional NASEM keywords."""
    title_lower = title.lower()
    for kw in NASEM_INSTITUTIONAL_KEYWORDS:
        if kw in title_lower:
            return True
    return False


def deduplicate(articles: list[dict]) -> list[dict]:
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
        print(f"  Removed {removed} duplicates, {len(unique)} unique", file=sys.stderr)
    return unique


def cross_deduplicate(nasem: list[dict], pnas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove articles from PNAS list that are already in NASEM list."""
    nasem_titles = [a["title"] for a in nasem]
    filtered_pnas = []
    for article in pnas:
        is_dup = False
        for nt in nasem_titles:
            if _title_similarity(article["title"], nt) > 0.55:
                is_dup = True
                break
        if not is_dup:
            filtered_pnas.append(article)
    removed = len(pnas) - len(filtered_pnas)
    if removed:
        print(f"  Cross-dedup: removed {removed} PNAS articles already in NASEM list", file=sys.stderr)
    return nasem, filtered_pnas


def filter_articles(articles: list[dict]) -> list[dict]:
    filtered = []
    for article in articles:
        domain = _get_domain(article.get("source_url", ""))
        if domain in SKIP_DOMAINS:
            continue
        article_domain = _get_domain(article.get("url", ""))
        if article_domain in SKIP_DOMAINS:
            continue
        filtered.append(article)
    removed = len(articles) - len(filtered)
    if removed:
        print(f"  Filtered {removed} press wires/NASEM pages", file=sys.stderr)
    return filtered


def classify_nasem_articles(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """Move PNAS-about articles from NASEM list to PNAS list.

    Articles found by institutional search terms but whose titles indicate
    they're about PNAS papers (not the institution) get reclassified.
    """
    nasem = []
    reclassified_pnas = []

    for a in articles:
        title = a["title"]
        if _is_pnas_article(title) and not _is_nasem_institutional(title):
            reclassified_pnas.append(a)
        else:
            nasem.append(a)

    if reclassified_pnas:
        print(f"  Reclassified {len(reclassified_pnas)} PNAS articles from NASEM results", file=sys.stderr)

    return nasem, reclassified_pnas


def rank_articles(articles: list[dict]) -> list[dict]:
    for a in articles:
        a["prominence"] = _outlet_prominence(a.get("source_url", ""), a.get("source_name", ""))
    return sorted(articles, key=lambda a: (
        a.get("published", ""),
        a["prominence"],
    ), reverse=True)


def resolve_urls(articles: list[dict]) -> list[dict]:
    """Resolve redirect URLs and verify accessibility."""
    if not articles:
        return articles

    print(f"  Resolving URLs ({len(articles)} articles)...", file=sys.stderr)
    resolved_count = 0
    inaccessible = 0

    for a in articles:
        url = a["url"]
        a["accessible"] = True
        a["resolved_url"] = url

        needs_resolve = "news.google.com" in url or "bing.com/news" in url
        if needs_resolve:
            try:
                resp = httpx.get(url, follow_redirects=True, timeout=10, headers=HEADERS)
                a["resolved_url"] = str(resp.url)
                a["url"] = a["resolved_url"]
                a["accessible"] = resp.status_code == 200
                if not a["source_name"]:
                    a["source_name"] = _get_domain(a["resolved_url"])
                resolved_count += 1
            except Exception:
                a["accessible"] = False
                inaccessible += 1
        else:
            try:
                resp = httpx.head(url, follow_redirects=True, timeout=8, headers=HEADERS)
                a["accessible"] = resp.status_code < 400
            except Exception:
                a["accessible"] = False
                inaccessible += 1

        time.sleep(0.2)

    if resolved_count:
        print(f"  Resolved {resolved_count} redirect URLs", file=sys.stderr)
    if inaccessible:
        print(f"  {inaccessible} inaccessible", file=sys.stderr)

    return articles


# --- Claude Categorization (NASEM only) ---

CATEGORIZE_SYSTEM = """You are a news editor at the National Academies of Sciences, Engineering, and Medicine (NASEM).
You are compiling the daily news clips digest for the Office of News and Public Information (ONPI).
This digest goes to senior leadership and is presented at the Daily Huddle.

Your job: categorize and group ALL articles, with the most important NASEM coverage first."""

CATEGORIZE_PROMPT = """Here are today's news articles found by searching for NASEM-related terms.
Categorize ALL of them into meaningful groups. Every article must appear in exactly one group.

Return a JSON object:

{{
  "groups": [
    {{
      "topic": "Short, specific topic label (report name, event, person, or theme)",
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
  ]
}}

RULES FOR ORDERING GROUPS (this order matters — senior leadership reads top-down):
1. FIRST: Coverage of NASEM reports, studies, recommendations, consensus statements
2. SECOND: Articles about NASEM presidents (Marcia McNutt, Victor Dzau, Monica Bertagnolli, Tsu-Jae Liu, Neil Shubin)
3. THIRD: NASEM events, workshops, convenings, policy citations
4. FOURTH: NAS/NAM/NAE member elections, appointments, awards
5. LAST: Articles where NASEM/NAS is mentioned but isn't the main subject

RULES FOR GROUPING:
- Give every group a specific, descriptive name. NEVER use generic labels like "Other", "Miscellaneous", or "Tangential".
  Instead use the actual topic: "NAS Member Appointments", "U.S. Life Expectancy Research", "Engineering Faculty Awards", etc.
- Group articles by the report, event, or topic they share. One article per group is fine if it's a unique topic.
- Within each group, put the most prominent outlet first.
- Flag press release reposts (is_press_release_repost=true).
- Flag anything extremely negative (is_negative=true) with a note.
- Include ALL articles. Do not exclude any.
- Return ONLY valid JSON. No markdown, no commentary.

ARTICLES:
{articles_json}"""


def categorize_with_claude(articles: list[dict]) -> dict:
    """Use Claude to categorize and group NASEM institutional articles."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    articles_for_claude = []
    for i, a in enumerate(articles):
        articles_for_claude.append({
            "index": i,
            "title": a["title"],
            "source": a["source_name"],
            "published": a.get("published", ""),
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
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    return json.loads(text)


# --- Output Formatting ---

def format_plain_nasem(articles: list[dict], categories: dict, date_label: str) -> str:
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
            if art.get("summary"):
                lines.append(f"    {art['summary']}")
            lines.append(f"    URL: {a['url']}")
            lines.append("")
        lines.append("")

    lines.append(f"Total: {len(articles)} articles")
    return "\n".join(lines)


def format_plain_pnas(articles: list[dict], date_label: str) -> str:
    lines = [f"PNAS COVERAGE — {date_label}", "=" * 50, ""]
    for i, a in enumerate(articles):
        lines.append(f"  {i+1}. {a['title']}")
        lines.append(f"     Source: {a['source_name']}")
        lines.append(f"     URL: {a['url']}")
        lines.append("")
    lines.append(f"Total: {len(articles)} PNAS articles")
    return "\n".join(lines)


def format_html_nasem(articles: list[dict], categories: dict, date_label: str) -> str:
    groups_html = []
    rendered_count = 0

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
            rendered_count += 1

        if items:
            groups_html.append(f"""
            <div style="margin-bottom:24px;">
                <h3 style="font-size:16px;font-weight:700;color:#1a5276;margin-bottom:8px;border-bottom:2px solid #e2e8f0;padding-bottom:4px;">{group['topic']}</h3>
                <ul style="margin:0;padding-left:20px;list-style:disc;">{''.join(items)}</ul>
            </div>""")

    return f"""<div style="font-family:'DM Sans',Helvetica,Arial,sans-serif;max-width:700px;margin:20px auto;">
    <div style="background:#1a5276;color:white;padding:18px 24px;border-radius:8px 8px 0 0;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;opacity:0.85;margin-bottom:4px;">Daily Clips</div>
        <div style="font-size:22px;font-weight:700;">NASEM News Coverage</div>
        <div style="font-size:14px;opacity:0.85;margin-top:4px;">{date_label} &mdash; {rendered_count} articles</div>
    </div>
    <div style="background:white;padding:20px 24px;border:1px solid #e0e0e0;border-top:0;border-radius:0 0 8px 8px;">
        {''.join(groups_html)}
        <div style="font-size:12px;color:#999;border-top:1px solid #f0f0f0;padding-top:10px;margin-top:16px;">
            Generated {datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p")} UTC | NASEM Daily Clips Aggregator
        </div>
    </div>
</div>"""


def format_html_pnas(articles: list[dict], date_label: str) -> str:
    items = []
    for a in articles:
        prominence_stars = "+" * a.get("prominence", 1)
        items.append(f"""<li style="margin-bottom:12px;line-height:1.5;">
            <a href="{a['url']}" style="color:#0369a1;font-weight:600;text-decoration:none;font-size:15px;">{a['title']}</a><br>
            <span style="color:#666;font-size:13px;"><strong>{a['source_name']}</strong> [{prominence_stars}]</span>
        </li>""")

    return f"""<div style="font-family:'DM Sans',Helvetica,Arial,sans-serif;max-width:700px;margin:20px auto;">
    <div style="background:#0369a1;color:white;padding:18px 24px;border-radius:8px 8px 0 0;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;opacity:0.85;margin-bottom:4px;">PNAS Coverage</div>
        <div style="font-size:22px;font-weight:700;">Journal Paper Coverage</div>
        <div style="font-size:14px;opacity:0.85;margin-top:4px;">{date_label} &mdash; {len(articles)} articles &mdash; Pick one for the daily clip</div>
    </div>
    <div style="background:white;padding:20px 24px;border:1px solid #e0e0e0;border-top:0;border-radius:0 0 8px 8px;">
        <ul style="margin:0;padding-left:20px;list-style:decimal;">
            {''.join(items)}
        </ul>
        <div style="font-size:12px;color:#999;border-top:1px solid #f0f0f0;padding-top:10px;margin-top:16px;">
            Choose one article above for the daily PNAS clip. Prefer articles that link back to PNAS/PNAS Nexus.
        </div>
    </div>
</div>"""


def format_json_output(nasem: list[dict], nasem_categories: dict,
                       pnas: list[dict], date_label: str) -> str:
    def clean(articles):
        return [{
            "title": a["title"], "url": a["url"],
            "source_name": a["source_name"],
            "published": a.get("published", ""),
            "prominence": a.get("prominence", 1),
        } for a in articles]

    return json.dumps({
        "date": date_label,
        "nasem": {"articles": clean(nasem), "categories": nasem_categories},
        "pnas": {"articles": clean(pnas)},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, ensure_ascii=False)


# --- Main Pipeline ---

def run_pipeline(days: int = 1, use_claude: bool = True) -> dict:
    """Run the full clips pipeline.

    Returns dict with keys: nasem_articles, nasem_categories, pnas_articles,
    date_label, stats.
    """
    today = datetime.now(timezone.utc)
    if days == 1:
        date_label = today.strftime("%A, %B %d, %Y")
    else:
        start = (today - timedelta(days=days)).strftime("%B %d")
        end = today.strftime("%B %d, %Y")
        date_label = f"{start} — {end}"

    # Step 1: Scan (returns separate NASEM and PNAS lists)
    nasem_raw, pnas_raw = scan_all_sources(days)
    raw_total = len(nasem_raw) + len(pnas_raw)

    if raw_total == 0:
        return {"error": "No articles found.", "stats": {"raw": 0}}

    # Step 2: Filter both lists
    nasem_filtered = filter_articles(nasem_raw)
    pnas_filtered = filter_articles(pnas_raw)

    # Step 3: Deduplicate each list
    print("Deduplicating NASEM articles...", file=sys.stderr)
    nasem_unique = deduplicate(nasem_filtered)
    print("Deduplicating PNAS articles...", file=sys.stderr)
    pnas_unique = deduplicate(pnas_filtered)

    # Step 4: Reclassify — move PNAS-about articles from NASEM to PNAS
    nasem_clean, reclassified = classify_nasem_articles(nasem_unique)
    pnas_unique.extend(reclassified)
    # Re-dedup PNAS after adding reclassified
    if reclassified:
        pnas_unique = deduplicate(pnas_unique)

    # Step 5: Cross-deduplicate (remove PNAS articles that are also in NASEM)
    nasem_clean, pnas_unique = cross_deduplicate(nasem_clean, pnas_unique)

    # Step 6: Resolve URLs and verify accessibility
    print("Resolving NASEM URLs...", file=sys.stderr)
    nasem_resolved = resolve_urls(nasem_clean)
    nasem_accessible = [a for a in nasem_resolved if a.get("accessible", True)]

    print("Resolving PNAS URLs...", file=sys.stderr)
    pnas_resolved = resolve_urls(pnas_unique)
    pnas_accessible = [a for a in pnas_resolved if a.get("accessible", True)]

    # Step 7: Re-filter after URL resolution (catches NASEM pages that were
    # hidden behind Google/Bing redirect URLs during the first filter pass)
    print("Re-filtering after URL resolution...", file=sys.stderr)
    nasem_accessible = filter_articles(nasem_accessible)
    pnas_accessible = filter_articles(pnas_accessible)

    # Step 8: Rank
    nasem_ranked = rank_articles(nasem_accessible)
    pnas_ranked = rank_articles(pnas_accessible)

    # Cap NASEM at 50 for Claude
    if len(nasem_ranked) > 50:
        print(f"  Capping NASEM at 50 (had {len(nasem_ranked)})", file=sys.stderr)
        nasem_ranked = nasem_ranked[:50]

    # Step 9: Categorize NASEM with Claude
    nasem_categories = None
    if use_claude and nasem_ranked:
        print("Categorizing NASEM coverage with Claude...", file=sys.stderr)
        try:
            nasem_categories = categorize_with_claude(nasem_ranked)
        except Exception as e:
            print(f"  Claude categorization failed: {e}", file=sys.stderr)

    if nasem_categories is None:
        nasem_categories = {
            "groups": [{"topic": "All Coverage", "articles": [
                {"index": i, "summary": "", "is_press_release_repost": False,
                 "is_negative": False, "negative_note": ""}
                for i in range(len(nasem_ranked))
            ]}]
        }

    stats = {
        "raw_total": raw_total,
        "raw_nasem": len(nasem_raw),
        "raw_pnas": len(pnas_raw),
        "nasem_articles": len(nasem_ranked),
        "pnas_articles": len(pnas_ranked),
        "nasem_groups": len(nasem_categories.get("groups", [])),
        "inaccessible": (len(nasem_resolved) - len(nasem_accessible)) +
                        (len(pnas_resolved) - len(pnas_accessible)),
    }

    return {
        "nasem_articles": nasem_ranked,
        "nasem_categories": nasem_categories,
        "pnas_articles": pnas_ranked,
        "date_label": date_label,
        "stats": stats,
    }


def main():
    parser = argparse.ArgumentParser(description="NASEM Daily Clips Aggregator")
    parser.add_argument("--days", type=int, default=1,
                        help="Number of days to look back (default: 1, use 3 for Mondays)")
    parser.add_argument("--html", action="store_true", help="Output as styled HTML")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--no-claude", action="store_true",
                        help="Skip Claude categorization (just scan and dedup)")
    parser.add_argument("--pnas-only", action="store_true",
                        help="Show only PNAS articles")

    args = parser.parse_args()

    result = run_pipeline(days=args.days, use_claude=not args.no_claude)

    if "error" in result:
        print(result["error"], file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(format_json_output(
            result["nasem_articles"], result["nasem_categories"],
            result["pnas_articles"], result["date_label"]
        ))
    elif args.pnas_only:
        if args.html:
            print(format_html_pnas(result["pnas_articles"], result["date_label"]))
        else:
            print(format_plain_pnas(result["pnas_articles"], result["date_label"]))
    elif args.html:
        print(format_html_nasem(
            result["nasem_articles"], result["nasem_categories"], result["date_label"]
        ))
        if result["pnas_articles"]:
            print("\n\n")
            print(format_html_pnas(result["pnas_articles"], result["date_label"]))
    else:
        print(format_plain_nasem(
            result["nasem_articles"], result["nasem_categories"], result["date_label"]
        ))
        if result["pnas_articles"]:
            print("\n\n")
            print(format_plain_pnas(result["pnas_articles"], result["date_label"]))

    # Print stats
    s = result["stats"]
    print(f"\n--- Stats ---", file=sys.stderr)
    print(f"Raw: {s['raw_total']} ({s['raw_nasem']} NASEM + {s['raw_pnas']} PNAS)", file=sys.stderr)
    print(f"Final: {s['nasem_articles']} NASEM articles in {s['nasem_groups']} groups, {s['pnas_articles']} PNAS articles", file=sys.stderr)
    if s["inaccessible"]:
        print(f"Removed {s['inaccessible']} inaccessible articles", file=sys.stderr)


if __name__ == "__main__":
    main()
