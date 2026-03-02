#!/usr/bin/env python3
"""
AI World Newsletter Pipeline
=============================
Collects the latest news and updates from across the entire AI world,
scores and summarizes them with Gemini, and delivers a stunning HTML newsletter.

Usage:
    python newsletter_pipeline.py                  # full pipeline
    python newsletter_pipeline.py --collect-only   # only collect RSS feeds
    python newsletter_pipeline.py --send-only      # only generate & send newsletter
    python newsletter_pipeline.py --dry-run        # generate HTML but don't send

Cron (every Monday 9 AM):
    0 9 * * 1 cd /path/to/ai-newsletter && python newsletter_pipeline.py >> logs/pipeline.log 2>&1
"""

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import feedparser
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "pipeline.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("newsletter")

# ---------------------------------------------------------------------------
# RSS Feeds — Broad AI World Coverage (not company-specific)
# ---------------------------------------------------------------------------
RSS_FEEDS: list[dict] = [
    # --- Major AI News Outlets ---
    {"name": "TechCrunch AI",       "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": "Industry"},
    {"name": "The Verge AI",        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "category": "Industry"},
    {"name": "VentureBeat AI",      "url": "https://venturebeat.com/category/ai/feed/", "category": "Industry"},
    {"name": "Ars Technica",        "url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "category": "Industry"},
    {"name": "MIT Tech Review",     "url": "https://www.technologyreview.com/feed/", "category": "Research"},
    {"name": "IEEE Spectrum AI",    "url": "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss", "category": "Research"},
    {"name": "Wired AI",            "url": "https://www.wired.com/feed/tag/ai/latest/rss", "category": "Industry"},
    # --- Company Research & Product Blogs ---
    {"name": "OpenAI Blog",         "url": "https://openai.com/blog/rss.xml", "category": "Product Launch"},
    {"name": "Google DeepMind",     "url": "https://deepmind.google/blog/rss.xml", "category": "Research"},
    {"name": "Hugging Face Blog",   "url": "https://huggingface.co/blog/feed.xml", "category": "Research"},
    {"name": "NVIDIA AI Blog",      "url": "https://blogs.nvidia.com/feed/", "category": "Product Launch"},
    {"name": "AWS AI Blog",         "url": "https://aws.amazon.com/blogs/machine-learning/feed/", "category": "Product Launch"},
    # --- Broad AI News via Google News ---
    {"name": "AI News (Google)",    "url": "https://news.google.com/rss/search?q=artificial+intelligence+when:7d&hl=en-US&gl=US&ceid=US:en", "category": "Industry"},
    {"name": "AI Policy News",      "url": "https://news.google.com/rss/search?q=AI+regulation+policy+when:7d&hl=en-US&gl=US&ceid=US:en", "category": "Policy"},
]

# Max articles to keep per feed (prevents blog archives from flooding the DB)
MAX_ARTICLES_PER_FEED = 15

# Environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OUTLOOK_FROM_EMAIL = os.getenv("OUTLOOK_FROM_EMAIL", "")
OUTLOOK_PASSWORD = os.getenv("OUTLOOK_PASSWORD", "")
OUTLOOK_TO_EMAIL = os.getenv("OUTLOOK_TO_EMAIL", "")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "sqlite")
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", str(Path(__file__).parent / "newsletter.db"))

# Gemini API settings
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
)
# Separate model for Stage 2 (editorial brief) to avoid rate limit collision
GEMINI_BRIEF_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
)
GEMINI_RPM_LIMIT = 10  # conservative: gemini-2.5-flash free tier is tight
GEMINI_BATCH_SIZE = 8  # articles per API call


# ---------------------------------------------------------------------------
# SQLite Storage Layer
# ---------------------------------------------------------------------------

class ArticleStore:
    """SQLite-backed article storage with deduplication."""

    def __init__(self, db_path: str = SQLITE_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    date TEXT,
                    source TEXT,
                    content_snippet TEXT,
                    category TEXT DEFAULT 'Uncategorized',
                    relevance_score INTEGER DEFAULT 0,
                    summary TEXT,
                    processed INTEGER DEFAULT 0,
                    collected_at TEXT NOT NULL,
                    processed_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON articles(hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_processed ON articles(processed)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON articles(date)")
            conn.commit()

    def hash_exists(self, url_hash: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM articles WHERE hash = ?", (url_hash,)).fetchone()
            return row is not None

    def insert_article(self, article: dict) -> bool:
        if self.hash_exists(article["hash"]):
            return False
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO articles (hash, title, url, date, source, content_snippet, category, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    article["hash"],
                    article["title"],
                    article["url"],
                    article.get("date", ""),
                    article.get("source", ""),
                    article.get("content_snippet", ""),
                    article.get("category", "Uncategorized"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        return True

    def get_unprocessed(self, days: int = 7) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM articles
                   WHERE processed = 0 AND collected_at >= ?
                   ORDER BY date DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, url_hash: str, score: int, summary: str, category: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE articles
                   SET processed = 1,
                       relevance_score = ?,
                       summary = ?,
                       category = ?,
                       processed_at = ?
                   WHERE hash = ?""",
                (score, summary, category, datetime.now(timezone.utc).isoformat(), url_hash),
            )
            conn.commit()

    def wipe(self):
        """Remove all articles. Used for fresh starts."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM articles")
            conn.commit()


def get_store():
    if STORAGE_BACKEND == "sheets":
        from newsletter_sheets import GoogleSheetsStore  # optional module
        return GoogleSheetsStore()
    return ArticleStore()


# ---------------------------------------------------------------------------
# RSS Collection
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(entry: dict) -> Optional[datetime]:
    """Try to extract a datetime from a feed entry."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        tp = entry.get(field)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for field in ("published", "updated", "created"):
        raw = entry.get(field, "")
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except Exception:
                pass
    return None


def collect_feeds(store) -> int:
    """Fetch all RSS feeds. Only keeps the most recent MAX_ARTICLES_PER_FEED per source."""
    new_count = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)  # only last 10 days

    for feed_info in RSS_FEEDS:
        name = feed_info["name"]
        url = feed_info["url"]
        default_category = feed_info["category"]
        log.info(f"Fetching: {name}")

        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                log.warning(f"  Feed error for {name}: {parsed.bozo_exception}")
                continue

            feed_articles = []
            for entry in parsed.entries:
                title = entry.get("title", "Untitled")
                link = entry.get("link", entry.get("id", ""))
                if not link:
                    continue

                # Parse and filter by date — skip old articles
                pub_dt = parse_date(entry)
                if pub_dt and pub_dt < cutoff:
                    continue  # skip articles older than 10 days

                pub_date = pub_dt.strftime("%a, %d %b %Y %H:%M:%S %z") if pub_dt else ""

                # Content snippet
                content_raw = ""
                if "content" in entry and entry["content"]:
                    content_raw = entry["content"][0].get("value", "")
                elif "summary" in entry:
                    content_raw = entry["summary"]
                elif "description" in entry:
                    content_raw = entry["description"]
                snippet = strip_html(content_raw)[:500]

                url_hash = hashlib.sha256(link.encode("utf-8")).hexdigest()

                feed_articles.append({
                    "hash": url_hash,
                    "title": title,
                    "url": link,
                    "date": pub_date,
                    "source": name,
                    "content_snippet": snippet,
                    "category": default_category,
                    "_sort_dt": pub_dt or datetime.min.replace(tzinfo=timezone.utc),
                })

            # Sort by date descending, keep only the most recent N
            feed_articles.sort(key=lambda x: x["_sort_dt"], reverse=True)
            feed_articles = feed_articles[:MAX_ARTICLES_PER_FEED]

            for a in feed_articles:
                del a["_sort_dt"]
                if store.insert_article(a):
                    new_count += 1

            log.info(f"  Kept {len(feed_articles)} recent articles from {name}")

        except Exception as e:
            log.error(f"  Failed to fetch {name}: {e}")

    log.info(f"Collection complete: {new_count} new articles stored")
    return new_count


# ---------------------------------------------------------------------------
# Gemini AI Processing — Two-Stage Pipeline
# Stage 1: Score and categorize individual articles
# Stage 2: Generate editorial intelligence brief from top articles
# ---------------------------------------------------------------------------

def _gemini_call(prompt: str, max_tokens: int = 8192) -> Optional[str]:
    """Low-level Gemini API call with retry logic. Returns raw text."""
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY is not set")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
        },
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120,
            )
            if resp.status_code == 429:
                wait = min(2 ** attempt * 15, 120)
                log.warning(f"  Rate limited. Retry {attempt+1}/{max_retries} in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except requests.exceptions.HTTPError as e:
            log.error(f"Gemini HTTP error: {e} — {e.response.text[:300]}")
            return None
        except Exception as e:
            log.error(f"Gemini error: {e}")
            return None
    return None


def _gemini_json(prompt: str, max_tokens: int = 8192, endpoint: str = None) -> Optional[list]:
    """Gemini call that returns parsed JSON. Optionally use a different endpoint."""
    ep = endpoint or GEMINI_ENDPOINT
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    max_retries = 4
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{ep}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120,
            )
            if resp.status_code == 429:
                wait = min(2 ** attempt * 20, 180)
                log.warning(f"  Rate limited. Retry {attempt+1}/{max_retries} in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(?:json)?\s*", "", text.strip())
            text = re.sub(r"\s*```$", "", text.strip())
            return json.loads(text)
        except Exception as e:
            log.error(f"Gemini JSON error: {e}")
            return None
    return None


def score_articles(articles: list[dict]) -> list[dict]:
    """Stage 1: Score and categorize articles in batches."""
    prompt_template = """You are a senior AI industry analyst. Score these articles for a weekly intelligence brief.
For EACH article:
1. Score relevance 1-10 (10 = industry-defining)
2. Write a sharp 2-sentence summary (specific: name companies, models, numbers)
3. Assign ONE category: "Market Signal", "Research / Technology", "Tools / Platforms", "Risk / Regulation"
Return ONLY a JSON array:
[{{"index":1,"relevance_score":8,"summary":"...","category":"Market Signal"}}]
Articles:
{articles}"""

    scored = []
    total = len(articles)
    for batch_start in range(0, total, GEMINI_BATCH_SIZE):
        batch = articles[batch_start:batch_start + GEMINI_BATCH_SIZE]
        batch_num = (batch_start // GEMINI_BATCH_SIZE) + 1
        total_batches = (total + GEMINI_BATCH_SIZE - 1) // GEMINI_BATCH_SIZE
        log.info(f"  Scoring batch {batch_num}/{total_batches} ({len(batch)} articles)...")

        article_text = ""
        for i, a in enumerate(batch, 1):
            article_text += f"\n--- Article {i} ---\nTitle: {a['title']}\nSource: {a.get('source','')}\nDate: {a.get('date','')}\nContent: {a.get('content_snippet','')}\n"

        results = _gemini_json(prompt_template.format(articles=article_text))
        if results:
            for r in results:
                idx = r.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    a = batch[idx]
                    a["relevance_score"] = r.get("relevance_score", 0)
                    a["summary"] = r.get("summary", "")
                    a["category"] = r.get("category", "Market Signal")
                    scored.append(a)

        if batch_start + GEMINI_BATCH_SIZE < total:
            time.sleep(60 / GEMINI_RPM_LIMIT)

    return scored


def generate_editorial_brief(top_articles: list[dict]) -> Optional[dict]:
    """Stage 2: Generate the editorial intelligence brief from scored articles."""
    article_digest = ""
    for a in top_articles[:25]:
        article_digest += f"- [{a.get('category','')}] {a['title']} ({a.get('source','')}) — {a.get('summary','')}\n"

    prompt = f"""You are the chief intelligence analyst for a company writing its internal weekly AI brief.
From the following scored articles, produce a structured intelligence brief.
Write in a strategic, authoritative, no-fluff tone. No emojis. No hype. Keep total under 900 words.

ARTICLES:
{article_digest}

Return a JSON object with exactly these keys:
{{
  "week_number": <int>,
  "executive_summary": "<3-4 sharp lines summarizing what changed this week and why it matters>",
  "section_01_market_signal": {{
    "headline": "<bold headline>",
    "body": "<2-3 concise paragraphs>",
    "why_it_matters": "<1 paragraph: direct implication for our company>"
  }},
  "section_02_research": {{
    "headline": "<bold headline>",
    "body": "<clear explanation without heavy jargon>",
    "strategic_takeaway": "<impact on product, engineering, operations, or leadership>"
  }},
  "section_03_tool": {{
    "name": "<tool/platform name>",
    "description": "<one-sentence description>",
    "use_case": "<practical internal application for our company>"
  }},
  "section_04_risk": {{
    "insight": "<short paragraph on regulation or risk>",
    "action": "<concrete recommendation>"
  }},
  "section_05_opportunity": "<one actionable AI experiment or initiative teams could explore this quarter>",
  "top_articles": [
    {{"title": "...", "url": "...", "source": "...", "score": 9, "summary": "...", "category": "..."}}
  ]
}}

Fill top_articles with the 8-12 most important articles from the input.
Return ONLY valid JSON."""

    results = _gemini_json(prompt, max_tokens=8192, endpoint=GEMINI_BRIEF_ENDPOINT)
    if isinstance(results, dict):
        return results
    if isinstance(results, list) and results:
        return results[0]
    return None


def process_articles(store, articles: list[dict]) -> Optional[dict]:
    """Full processing: score articles, then generate editorial brief."""
    if not articles:
        return None

    log.info("Stage 1: Scoring individual articles...")
    scored = score_articles(articles)

    # Mark all as processed in store
    for a in scored:
        store.mark_processed(
            a["hash"], a.get("relevance_score", 0),
            a.get("summary", ""), a.get("category", "")
        )

    top = [a for a in scored if a.get("relevance_score", 0) >= 7]
    top.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    log.info(f"  {len(top)}/{len(scored)} articles scored 7+")

    if not top:
        return None

    log.info("Stage 2: Generating editorial intelligence brief (via flash-lite)...")
    time.sleep(10)  # brief pause before switching models
    brief = generate_editorial_brief(top)

    if brief:
        # Inject full article data if not already there
        if "top_articles" not in brief or not brief["top_articles"]:
            brief["top_articles"] = [
                {"title": a["title"], "url": a["url"], "source": a.get("source",""),
                 "score": a.get("relevance_score",0), "summary": a.get("summary",""),
                 "category": a.get("category","")}
                for a in top[:12]
            ]
        brief["_all_scored"] = top
    return brief


# ---------------------------------------------------------------------------
# Inline SVG Icons (email-safe data URIs, dark luxury palette)
# ---------------------------------------------------------------------------

SVG_ICON_MARKET = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23261A00"/%3E%3Cpath d="M8 23l5-6 5 4 7-10" stroke="%23F59E0B" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/%3E%3Ccircle cx="25" cy="11" r="2.5" fill="%23FBBF24"/%3E%3C/svg%3E'
SVG_ICON_RESEARCH = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23052E16"/%3E%3Ccircle cx="14" cy="14" r="6" stroke="%2334D399" stroke-width="2.5"/%3E%3Cpath d="M19 19l5 5" stroke="%2310B981" stroke-width="2.5" stroke-linecap="round"/%3E%3Ccircle cx="14" cy="14" r="2" fill="%2334D399" opacity="0.5"/%3E%3C/svg%3E'
SVG_ICON_TOOL = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23172554"/%3E%3Crect x="8" y="5" width="16" height="22" rx="3" stroke="%2360A5FA" stroke-width="2"/%3E%3Cpath d="M12 11h8M12 15h8M12 19h4" stroke="%2393C5FD" stroke-width="1.5" stroke-linecap="round"/%3E%3C/svg%3E'
SVG_ICON_RISK = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23450A0A"/%3E%3Cpath d="M16 6l10 18H6L16 6z" stroke="%23F87171" stroke-width="2" stroke-linejoin="round"/%3E%3Cpath d="M16 14v4" stroke="%23FCA5A5" stroke-width="2" stroke-linecap="round"/%3E%3Ccircle cx="16" cy="21" r="1.2" fill="%23FCA5A5"/%3E%3C/svg%3E'
SVG_ICON_OPP = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%232E1065"/%3E%3Ccircle cx="16" cy="16" r="5" stroke="%23A78BFA" stroke-width="2"/%3E%3Cpath d="M16 6v4M16 22v4M6 16h4M22 16h4" stroke="%23C4B5FD" stroke-width="2" stroke-linecap="round"/%3E%3C/svg%3E'
SVG_ICON_LOGO = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="44" height="44" fill="none"%3E%3Crect width="44" height="44" rx="12" fill="%23050608"/%3E%3Cdefs%3E%3ClinearGradient id="lg" x1="0" y1="0" x2="44" y2="44"%3E%3Cstop stop-color="%236C6FFF"/%3E%3Cstop offset="1" stop-color="%23A78BFA"/%3E%3C/linearGradient%3E%3C/defs%3E%3Cpath d="M22 8c-5 0-9 3.5-9 8 0 2.5 1.2 4.5 3 6-1 1.5-2 4-2 6.5 0 4.5 3.5 8 8 8s8-3.5 8-8c0-2.5-1-5-2-6.5 1.8-1.5 3-3.5 3-6 0-4.5-4-8-9-8z" stroke="url(%23lg)" stroke-width="1.8" fill="none"/%3E%3Ccircle cx="22" cy="18" r="3" fill="%236C6FFF" opacity="0.3"/%3E%3Cpath d="M18 18h8M19 14h6M19 22h5" stroke="url(%23lg)" stroke-width="1" stroke-linecap="round" opacity="0.5"/%3E%3C/svg%3E'

# Company logo embedded as base64 (static - same every week)
COMPANY_LOGO_B64 = 'data:image/jpeg;base64,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6vKvvt9fVbq+m2UK33wO3633qWOyaw6x4jlyr6LbQZdwJquT8FY5SuA2xuj4qBAEvuG46n5I0gl5g96klAd8Z+XwoPQQMh0t7qmYGEu3Eq9AiLDk7PrV9UAuBs9lW/pQMZJET2nm8MlS0rEdWx4AqqsFip4YCIbdyooIQNBg819Vur6bZQMAhCOtHGAbAGAK+i21orqTEZaZoOBek3D4qPiA9cxysjt+VqvGV+8ohBQMGEO/xxUES9rd/am2pp/S58cJmPXQ3wpVXEFgNL7vscPJMN60an1gZprvbwUSBazzP6KSgMY6TpxLwm0cTopXnrzJ3NSpKLZRU6BPWozpsi2xoe/4oNcVLE52hKbmV2QMydRJqFH97I+TxWFLjH8xL0GCy23uX7pcvE5YHSxFYvMdRmgIYb/i5N0pSALbK5OaYhY5pcr7p/oFA5gkzV2pfBdUdDr9dTKguIwlRkKU8OSEaj76VaZ4OHt9Eai5EuQlEVy/RSDorG4X2o2TyK28Fi8u9EfSaJu+yNADMKw8DAURJ1wh0r+C9UmR5NQ12gSUQXFTTpbAMJ4U4QAlXBTAkQYpyQ74pLhD1JcmrRqEpghb8p6Bm8dYz3qYgNlcGImPYcelCAkSHPJ9OEaCUwGEvS3fhKed5xL1rVh7ABdggetX42R5q1sPPAvIq0Jo6Udf++nXSEilvIsLM8pBfWKgcdgDBd1qIrjhNBGMCP5SctGyETrBU0YKpbJ5Xaf5wqJJDcqCGMjgPIx+B4bwQPJX/osPAqJ2cFGZJHmHC0KIJO6Uax4OAbBWCpBh0kq6h4YesHC8tdY3dYoVrwE+wGKBJrAE/KjK7/8AS//aAAwDAQACAAMAAAAQ88888888888888888888888888888888888888888888888888888/Q864SN9xe+889pO1qP943JW88+9qzc8DfvN888Ms5uvrxXj/V8888O88csc8M888888888888888888888888888888888888888888888888888888//EACYRAQABBAEDAwUBAAAAAAAAAAERACExQWFRcZEQgaEgUGCxwdH/2gAIAQMBAT8Q+6kpwIl0ES+f7RiOUii2i+D3oKuHrH7JQBUsYTySegOkZLqFn5xxRQCFPW1R7iELTGnmocyUFonXfdSdBFSi+Aip2XDc7nqUsoWd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'


# ---------------------------------------------------------------------------
# PART 1 -- Executive Email (Dark Cinematic Luxury Theme)
# ---------------------------------------------------------------------------

def _load_section_image(filename):
    """Load a section image from assets/ and return as base64 data URI."""
    path = Path(__file__).parent / 'assets' / filename
    if path.exists():
        import base64 as b64mod
        with open(path, 'rb') as f:
            encoded = b64mod.b64encode(f.read()).decode()
        ext = path.suffix.lstrip('.').lower()
        mime = 'jpeg' if ext in ('jpg', 'jpeg') else ext
        return f'data:image/{mime};base64,{encoded}'
    return ''

def generate_email_html(brief: dict) -> str:
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    date_range = f"{week_ago.strftime('%b %d')} - {now.strftime('%b %d, %Y')}"
    wk = brief.get("week_number", (now - datetime(2026, 1, 1, tzinfo=timezone.utc)).days // 7 + 1)

    es = html.escape(brief.get("executive_summary", ""))
    s1 = brief.get("section_01_market_signal", {})
    s2 = brief.get("section_02_research", {})
    s3 = brief.get("section_03_tool", {})
    s4 = brief.get("section_04_risk", {})
    s5 = html.escape(brief.get("section_05_opportunity", ""))
    articles = brief.get("top_articles", [])
    num_articles = len(articles)
    num_sources = len(RSS_FEEDS)
    avg_score = round(sum(a.get("score", 0) for a in articles) / max(len(articles), 1), 1)

    # Load section images from assets/ (generated fresh each week)
    img_market = _load_section_image('section_market.jpg')
    img_research = _load_section_image('section_research.jpg')
    img_tool = _load_section_image('section_tool.jpg')
    img_risk = _load_section_image('section_risk.jpg')
    img_opportunity = _load_section_image('section_opportunity.jpg')

    def esc(v): return html.escape(str(v)) if v else ""

    cat_colors = {
        "Market Signal": "#FFB84D",
        "Research / Technology": "#06D6A0",
        "Tools / Platforms": "#8B8EFF",
        "Risk / Regulation": "#FF6B8A",
    }

    article_rows = ""
    for a in articles[:12]:
        t = esc(a.get("title", "")[:75])
        u = esc(a.get("url", "#"))
        s = esc(a.get("source", ""))
        sc = a.get("score", 0)
        cat = esc(a.get("category", ""))
        cc = cat_colors.get(a.get("category", ""), "#5C6478")
        sc_color = "#34D399" if sc >= 8 else "#FBBF24" if sc >= 7 else "#5C6478"
        article_rows += f"""<tr>
<td style="padding:14px 0;border-bottom:1px solid #1E2130;vertical-align:top;">
<a href="{u}" style="color:#F1F3F8;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:600;text-decoration:none;line-height:1.5;display:block;">{t}</a>
<table cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;"><tr>
<td style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#5C6478;padding-right:12px;">{s}</td>
<td style="background:{cc};color:#07080C;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:700;padding:3px 10px;border-radius:10px;letter-spacing:0.3px;">{cat}</td>
<td style="font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:700;color:{sc_color};padding-left:12px;">{sc}/10</td>
</tr></table>
</td>
</tr>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Intelligence Brief - Week {wk}</title></head>
<body style="margin:0;padding:0;background:#050608;font-family:Arial,Helvetica,sans-serif;-webkit-font-smoothing:antialiased;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#050608;">
<tr><td align="center" style="padding:40px 16px;">
<table cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;background:#0A0C12;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,0.06);">

<!-- GRADIENT ACCENT BAR -->
<tr><td style="height:3px;background:linear-gradient(90deg,#6C6FFF,#A78BFA,#06D6A0);font-size:0;line-height:0;">&nbsp;</td></tr>

<!-- HEADER -->
<tr><td style="padding:44px 48px 0 48px;">
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="vertical-align:middle;">
<p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:700;color:#6C6FFF;text-transform:uppercase;letter-spacing:3px;">AI Intelligence Brief</p>
<h1 style="margin:8px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:28px;font-weight:800;color:#F0F2F8;letter-spacing:-0.8px;line-height:1.15;">What's Shaping AI This Week</h1>
<p style="margin:8px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#5E667A;font-weight:500;letter-spacing:0.5px;">Week {wk} &nbsp;// &nbsp;{date_range}</p>
</td>
<td style="vertical-align:middle;text-align:right;width:50px;">
<img src="{COMPANY_LOGO_B64}" width="44" height="44" alt="AJMS Global" style="display:block;margin-left:auto;border-radius:8px;" />
</td>
</tr></table>
</td></tr>

<tr><td style="padding:28px 48px 0 48px;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0A0C12;border-radius:14px;border:1px solid rgba(255,255,255,0.06);"><tr>
<td style="text-align:center;padding:20px 8px;">
<p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:28px;font-weight:800;color:#6C6FFF;">{num_articles}</p>
<p style="margin:4px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:9px;font-weight:600;color:#5E667A;text-transform:uppercase;letter-spacing:1.5px;">Top Articles</p>
</td>
<td style="text-align:center;padding:20px 8px;border-left:1px solid rgba(255,255,255,0.06);border-right:1px solid rgba(255,255,255,0.06);">
<p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:28px;font-weight:800;color:#06D6A0;">{num_sources}</p>
<p style="margin:4px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:9px;font-weight:600;color:#5E667A;text-transform:uppercase;letter-spacing:1.5px;">Sources</p>
</td>
<td style="text-align:center;padding:20px 8px;">
<p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:28px;font-weight:800;color:#FFB84D;">{avg_score}</p>
<p style="margin:4px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:9px;font-weight:600;color:#5E667A;text-transform:uppercase;letter-spacing:1.5px;">Avg Score</p>
</td>
</tr></table>
</td></tr>

<!-- EXECUTIVE SUMMARY -->
<tr><td style="padding:28px 48px 0 48px;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#111827;border-radius:10px;border:1px solid #1E2130;"><tr>
<td style="padding:24px;border-top:3px solid #6366F1;">
<p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:800;color:#6366F1;text-transform:uppercase;letter-spacing:1.5px;">Executive Summary</p>
<p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:15px;color:#A0A7B8;line-height:1.7;font-weight:400;">{es}</p>
</td></tr></table>
</td></tr>

<!-- GRADIENT DIVIDER -->
<tr><td style="padding:28px 48px 0 48px;"><table width="100%"><tr><td style="height:1px;background:linear-gradient(90deg,#6366F1,#1E2130,#1E2130);border-radius:1px;"></td></tr></table></td></tr>

<!-- 01 MARKET SIGNAL -->
<tr><td style="padding:28px 48px 0 48px;">
{'<img src="' + img_market + '" width="100%" height="auto" alt="Market Signal" style="display:block;border-radius:12px;margin-bottom:20px;" />' if img_market else ''}
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="vertical-align:middle;width:36px;padding-right:14px;"><div style="width:32px;height:32px;border-radius:8px;background:#261A00;text-align:center;line-height:32px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:800;color:#FFB84D;">01</div></td>
<td style="vertical-align:middle;"><p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;color:#FFB84D;letter-spacing:1.5px;">THE SHIFT</p></td>
</tr></table>
<h2 style="margin:12px 0 16px 0;font-family:Arial,Helvetica,sans-serif;font-size:20px;font-weight:800;color:#F1F3F8;line-height:1.3;letter-spacing:-0.3px;">{esc(s1.get('headline',''))}</h2>
<p style="margin:0 0 16px 0;font-family:Georgia,'Times New Roman',serif;font-size:14px;color:#A0A7B8;line-height:1.75;">{esc(s1.get('body',''))}</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="background:#1A1708;border-left:3px solid #FBBF24;padding:14px 18px;border-radius:0 8px 8px 0;">
<p style="margin:0 0 4px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:800;color:#FBBF24;text-transform:uppercase;letter-spacing:1px;">Why This Matters</p>
<p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:13px;color:#A0A7B8;line-height:1.65;">{esc(s1.get('why_it_matters',''))}</p>
</td></tr></table>
</td></tr>

<!-- GRADIENT DIVIDER -->
<tr><td style="padding:28px 48px 0 48px;"><table width="100%"><tr><td style="height:1px;background:linear-gradient(90deg,#FBBF24,#1E2130,#1E2130);border-radius:1px;"></td></tr></table></td></tr>

<!-- 02 RESEARCH -->
<tr><td style="padding:28px 48px 0 48px;">
{'<img src="' + img_research + '" width="100%" height="auto" alt="Research" style="display:block;border-radius:12px;margin-bottom:20px;" />' if img_research else ''}
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="vertical-align:middle;width:36px;padding-right:14px;"><div style="width:32px;height:32px;border-radius:8px;background:#052E16;text-align:center;line-height:32px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:800;color:#06D6A0;">02</div></td>
<td style="vertical-align:middle;"><p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;color:#06D6A0;letter-spacing:1.5px;">THE BREAKTHROUGH</p></td>
</tr></table>
<h2 style="margin:12px 0 16px 0;font-family:Arial,Helvetica,sans-serif;font-size:20px;font-weight:800;color:#F1F3F8;line-height:1.3;letter-spacing:-0.3px;">{esc(s2.get('headline',''))}</h2>
<p style="margin:0 0 16px 0;font-family:Georgia,'Times New Roman',serif;font-size:14px;color:#A0A7B8;line-height:1.75;">{esc(s2.get('body',''))}</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="background:#081A11;border-left:3px solid #34D399;padding:14px 18px;border-radius:0 8px 8px 0;">
<p style="margin:0 0 4px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:800;color:#34D399;text-transform:uppercase;letter-spacing:1px;">Strategic Takeaway</p>
<p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:13px;color:#A0A7B8;line-height:1.65;">{esc(s2.get('strategic_takeaway',''))}</p>
</td></tr></table>
</td></tr>

<!-- GRADIENT DIVIDER -->
<tr><td style="padding:28px 48px 0 48px;"><table width="100%"><tr><td style="height:1px;background:linear-gradient(90deg,#34D399,#1E2130,#1E2130);border-radius:1px;"></td></tr></table></td></tr>

<!-- 03 TOOL -->
<tr><td style="padding:28px 48px 0 48px;">
{'<img src="' + img_tool + '" width="100%" height="auto" alt="Tool" style="display:block;border-radius:12px;margin-bottom:20px;" />' if img_tool else ''}
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="vertical-align:middle;width:36px;padding-right:14px;"><div style="width:32px;height:32px;border-radius:8px;background:#172554;text-align:center;line-height:32px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:800;color:#8B8EFF;">03</div></td>
<td style="vertical-align:middle;"><p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;color:#8B8EFF;letter-spacing:1.5px;">TOOL OF THE WEEK</p></td>
</tr></table>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#111433;border:1px solid #1E2130;border-radius:10px;margin-top:14px;">
<tr><td style="padding:24px;">
<h3 style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:20px;font-weight:800;color:#818CF8;">{esc(s3.get('name',''))}</h3>
<p style="margin:0 0 16px 0;font-family:Georgia,'Times New Roman',serif;font-size:14px;color:#A0A7B8;line-height:1.6;">{esc(s3.get('description',''))}</p>
<p style="margin:0 0 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:800;color:#6366F1;text-transform:uppercase;letter-spacing:1px;">Use Case for Our Team</p>
<p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:13px;color:#A0A7B8;line-height:1.65;">{esc(s3.get('use_case',''))}</p>
</td></tr></table>
</td></tr>

<!-- GRADIENT DIVIDER -->
<tr><td style="padding:28px 48px 0 48px;"><table width="100%"><tr><td style="height:1px;background:linear-gradient(90deg,#818CF8,#1E2130,#1E2130);border-radius:1px;"></td></tr></table></td></tr>

<!-- 04 RISK -->
<tr><td style="padding:28px 48px 0 48px;">
{'<img src="' + img_risk + '" width="100%" height="auto" alt="Risk" style="display:block;border-radius:12px;margin-bottom:20px;" />' if img_risk else ''}
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="vertical-align:middle;width:36px;padding-right:14px;"><div style="width:32px;height:32px;border-radius:8px;background:#450A0A;text-align:center;line-height:32px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:800;color:#FF6B8A;">04</div></td>
<td style="vertical-align:middle;"><p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;color:#FF6B8A;letter-spacing:1.5px;">RISK WATCH</p></td>
</tr></table>
<p style="margin:12px 0 16px 0;font-family:Georgia,'Times New Roman',serif;font-size:14px;color:#A0A7B8;line-height:1.75;">{esc(s4.get('insight',''))}</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="background:#1A0A0E;border-left:3px solid #FB7185;padding:14px 18px;border-radius:0 8px 8px 0;">
<p style="margin:0 0 4px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:800;color:#FB7185;text-transform:uppercase;letter-spacing:1px;">Action Consideration</p>
<p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:13px;color:#A0A7B8;line-height:1.65;">{esc(s4.get('action',''))}</p>
</td></tr></table>
</td></tr>

<!-- GRADIENT DIVIDER -->
<tr><td style="padding:28px 48px 0 48px;"><table width="100%"><tr><td style="height:1px;background:linear-gradient(90deg,#FB7185,#1E2130,#1E2130);border-radius:1px;"></td></tr></table></td></tr>

<!-- 05 OPPORTUNITY -->
<tr><td style="padding:28px 48px 0 48px;">
{'<img src="' + img_opportunity + '" width="100%" height="auto" alt="Opportunity" style="display:block;border-radius:12px;margin-bottom:20px;" />' if img_opportunity else ''}
<table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
<td style="vertical-align:middle;width:36px;padding-right:14px;"><div style="width:32px;height:32px;border-radius:8px;background:#2E1065;text-align:center;line-height:32px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:800;color:#A78BFA;">05</div></td>
<td style="vertical-align:middle;"><p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;color:#A78BFA;letter-spacing:1.5px;">OPPORTUNITY</p></td>
</tr></table>
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#13102B;border:1px solid #1E2130;border-radius:10px;margin-top:14px;">
<tr><td style="padding:24px;">
<p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:14px;color:#A0A7B8;line-height:1.75;">{s5}</p>
</td></tr></table>
</td></tr>

<!-- GRADIENT DIVIDER -->
<tr><td style="padding:28px 48px 0 48px;"><table width="100%"><tr><td style="height:1px;background:linear-gradient(90deg,#A78BFA,#1E2130,#1E2130);border-radius:1px;"></td></tr></table></td></tr>

<!-- SOURCE APPENDIX -->
<tr><td style="padding:28px 48px 0 48px;">
<p style="margin:0 0 16px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;color:#5C6478;letter-spacing:1.5px;text-transform:uppercase;">Source Articles</p>
<table cellpadding="0" cellspacing="0" border="0" width="100%">
{article_rows}
</table>
</td></tr>

<!-- FOOTER -->
<tr><td style="padding:36px 48px 40px 48px;">
<table width="100%"><tr><td style="padding-top:20px;border-top:1px solid #1E2130;">
<p style="margin:0 0 8px 0;font-family:Georgia,'Times New Roman',serif;font-size:13px;color:#5C6478;line-height:1.6;">
Reply with ideas or experiments inspired by this week's insights.</p>
<p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#3A3F50;">AI Intelligence Brief // {num_sources} sources // Powered by Gemini + Brevo</p>
</td></tr></table>
</td></tr>

</table>
</td></tr></table>
</body></html>"""


# ---------------------------------------------------------------------------
# PART 2 -- Web Interactive Experience (Dark Cinematic Luxury Scrollytelling)
# ---------------------------------------------------------------------------

def generate_web_html(brief: dict) -> str:
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    date_range = f"{week_ago.strftime('%b %d')} - {now.strftime('%b %d, %Y')}"
    wk = brief.get("week_number", (now - datetime(2026, 1, 1, tzinfo=timezone.utc)).days // 7 + 1)

    es = html.escape(brief.get("executive_summary", ""))
    s1 = brief.get("section_01_market_signal", {})
    s2 = brief.get("section_02_research", {})
    s3 = brief.get("section_03_tool", {})
    s4 = brief.get("section_04_risk", {})
    s5 = html.escape(brief.get("section_05_opportunity", ""))
    articles = brief.get("top_articles", [])
    num_articles = len(articles)
    num_sources = len(RSS_FEEDS)
    avg_score = round(sum(a.get("score", 0) for a in articles) / max(len(articles), 1), 1)

    def esc(v): return html.escape(str(v)) if v else ""

    web_cat_colors = {
        "Market Signal": "#FFB84D",
        "Research / Technology": "#06D6A0",
        "Tools / Platforms": "#8B8EFF",
        "Risk / Regulation": "#FF6B8A",
    }
    web_cat_border = {
        "Market Signal": "#FFB84D",
        "Research / Technology": "#06D6A0",
        "Tools / Platforms": "#6C6FFF",
        "Risk / Regulation": "#FF6B8A",
    }
    web_cat_glow = {
        "Market Signal": "rgba(255,184,77,0.12)",
        "Research / Technology": "rgba(6,214,160,0.12)",
        "Tools / Platforms": "rgba(108,111,255,0.12)",
        "Risk / Regulation": "rgba(255,107,138,0.12)",
    }

    article_cards = ""
    for a in articles[:12]:
        t = esc(a.get("title", "")[:80])
        u = esc(a.get("url", "#"))
        s = esc(a.get("source", ""))
        sc = a.get("score", 0)
        cat = esc(a.get("category", ""))
        sm = esc(a.get("summary", ""))
        cc = web_cat_colors.get(a.get("category", ""), "#5C6478")
        cb = web_cat_border.get(a.get("category", ""), "#1E2130")
        cg = web_cat_glow.get(a.get("category", ""), "rgba(99,102,241,0.1)")
        pct = round(sc * 10)
        dash = round(pct * 62.83 / 100, 1)
        sc_color = "#34D399" if sc >= 8 else "#FBBF24" if sc >= 7 else "#5C6478"
        article_cards += f"""
      <a href="{u}" target="_blank" class="article-card" style="border-top:3px solid {cb};" data-glow="{cg}">
        <div class="article-meta">
          <span class="article-cat" style="color:{cc}">{cat}</span>
          <svg class="score-ring" width="36" height="36" viewBox="0 0 36 36">
            <circle cx="18" cy="18" r="10" fill="none" stroke="#1E2130" stroke-width="2.5"/>
            <circle cx="18" cy="18" r="10" fill="none" stroke="{sc_color}" stroke-width="2.5" stroke-dasharray="{dash} 62.83" stroke-dashoffset="0" stroke-linecap="round" transform="rotate(-90 18 18)"/>
            <text x="18" y="18" text-anchor="middle" dominant-baseline="central" font-size="9" font-weight="700" fill="{sc_color}">{sc}</text>
          </svg>
        </div>
        <h4>{t}</h4>
        <p class="article-summary">{sm}</p>
        <span class="article-source">{s}</span>
      </a>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Intelligence Brief - Week {wk}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@300;400;500;600;700;800&family=Source+Serif+4:ital,opsz,wght@0,8..60,300;0,8..60,400;0,8..60,500;0,8..60,600;0,8..60,700;1,8..60,400;1,8..60,500&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
:root{{
  --bg:#050608;--surface:#0A0C12;--surface-2:#10131B;--surface-3:#161A26;--border:rgba(255,255,255,0.06);--border-hover:rgba(255,255,255,0.12);
  --text:#F0F2F8;--text-secondary:#C8CDD8;--body:#9BA3B8;--muted:#5E667A;--dim:#353B4D;
  --accent:#6C6FFF;--accent-light:#8B8EFF;--accent-glow:rgba(108,111,255,0.15);
  --cyan:#06D6A0;--cyan-glow:rgba(6,214,160,0.12);
  --amber:#FFB84D;--amber-glow:rgba(255,184,77,0.12);
  --emerald:#34D399;--emerald-glow:rgba(52,211,153,0.12);
  --rose:#FF6B8A;--rose-glow:rgba(255,107,138,0.12);
  --purple:#A78BFA;--purple-glow:rgba(167,139,250,0.12);
  --radius-sm:8px;--radius-md:14px;--radius-lg:20px;--radius-xl:28px;
  --space-xs:8px;--space-sm:16px;--space-md:24px;--space-lg:40px;--space-xl:64px;--space-2xl:100px;
}}
html{{scroll-behavior:smooth;}}
body{{background:var(--bg);color:var(--text);font-family:'Sora',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;line-height:1.6;overflow-x:hidden;}}

/* NOISE TEXTURE OVERLAY */
body::after{{content:'';position:fixed;inset:0;z-index:9999;pointer-events:none;opacity:0.022;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 512 512' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.75' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");background-repeat:repeat;background-size:256px;}}

/* GRADIENT MESH AMBIENT */
body::before{{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;background:
  radial-gradient(ellipse 80% 50% at 20% 20%,rgba(108,111,255,0.06),transparent),
  radial-gradient(ellipse 60% 40% at 80% 80%,rgba(167,139,250,0.04),transparent),
  radial-gradient(ellipse 50% 60% at 50% 50%,rgba(6,214,160,0.02),transparent);
animation:meshDrift 20s ease-in-out infinite alternate;}}
@keyframes meshDrift{{0%{{opacity:1;}}50%{{opacity:0.7;}}100%{{opacity:1;}}}}

/* READING PROGRESS BAR */
#reading-progress{{position:fixed;top:0;left:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--purple),var(--cyan));z-index:10000;width:0%;transition:width 0.08s linear;box-shadow:0 0 12px var(--accent-glow);}}

/* SCROLL ANIMATIONS */
.reveal{{opacity:0;transform:translateY(40px) scale(0.98);transition:opacity 0.8s cubic-bezier(0.16,1,0.3,1),transform 0.8s cubic-bezier(0.16,1,0.3,1);will-change:opacity,transform;}}
.reveal.visible{{opacity:1;transform:translateY(0) scale(1);}}
.reveal-delay-1{{transition-delay:0.15s;}}
.reveal-delay-2{{transition-delay:0.3s;}}
.reveal-delay-3{{transition-delay:0.45s;}}
.reveal-delay-4{{transition-delay:0.6s;}}

/* LAYOUT */
.container{{max-width:760px;margin:0 auto;padding:0 var(--space-md);}}
.container-wide{{max-width:1000px;margin:0 auto;padding:0 var(--space-md);}}

/* HERO */
.hero{{min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;position:relative;overflow:hidden;background:var(--bg);}}
.hero::before{{content:'';position:absolute;inset:0;background:
  radial-gradient(circle 600px at 50% 40%,rgba(108,111,255,0.08),transparent 70%),
  radial-gradient(circle 400px at 30% 60%,rgba(167,139,250,0.05),transparent 60%),
  radial-gradient(circle 300px at 70% 30%,rgba(6,214,160,0.04),transparent 50%);
z-index:0;}}
#particle-canvas{{position:absolute;inset:0;z-index:0;}}
.hero-content{{position:relative;z-index:2;}}
.hero-label{{display:inline-flex;align-items:center;gap:8px;font-size:11px;font-weight:600;color:var(--accent-light);letter-spacing:3.5px;text-transform:uppercase;padding:10px 24px;border-radius:40px;border:1px solid rgba(108,111,255,0.2);background:rgba(108,111,255,0.05);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);}}
.hero-label::before{{content:'';width:6px;height:6px;border-radius:50%;background:var(--accent);animation:labelPulse 2s ease-in-out infinite;}}
@keyframes labelPulse{{0%,100%{{opacity:1;box-shadow:0 0 0 0 var(--accent-glow);}}50%{{opacity:0.6;box-shadow:0 0 0 6px transparent;}}}}
.hero h1{{font-size:clamp(44px,8vw,84px);font-weight:800;letter-spacing:-3px;line-height:0.95;margin:32px 0;color:var(--text);}}
.hero h1 span{{background:linear-gradient(135deg,var(--accent) 0%,var(--cyan) 40%,var(--purple) 80%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-size:200% 200%;animation:hero-grad 6s ease infinite;filter:drop-shadow(0 0 30px var(--accent-glow));}}
@keyframes hero-grad{{0%,100%{{background-position:0% 50%;}}50%{{background-position:100% 50%;}}}}
.hero-sub{{font-family:'Source Serif 4',Georgia,serif;font-size:19px;font-weight:400;color:var(--body);max-width:540px;margin:0 auto;line-height:1.85;font-style:italic;}}
.hero-meta{{margin-top:32px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:1.5px;}}
.hero-meta span{{color:var(--accent-light);}}
.scroll-hint{{position:absolute;bottom:48px;left:50%;transform:translateX(-50%);animation:scrollFloat 3s ease-in-out infinite;z-index:2;}}
.scroll-hint svg{{opacity:0.4;}}
@keyframes scrollFloat{{0%,100%{{transform:translateX(-50%) translateY(0);opacity:0.4;}}50%{{transform:translateX(-50%) translateY(12px);opacity:0.7;}}}}

/* STAT CARDS */
.stats-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--space-md);margin:0 0 var(--space-2xl) 0;padding-top:var(--space-2xl);}}
@media(max-width:600px){{.stats-row{{grid-template-columns:1fr;gap:var(--space-sm);}}}}
.stat-card{{background:var(--surface);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:var(--radius-lg);padding:32px 24px;text-align:center;transition:all 0.4s cubic-bezier(0.16,1,0.3,1);position:relative;overflow:hidden;}}
.stat-card::before{{content:'';position:absolute;inset:0;border-radius:var(--radius-lg);padding:1px;background:linear-gradient(135deg,transparent 40%,rgba(108,111,255,0.15),transparent 60%);-webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);-webkit-mask-composite:xor;mask-composite:exclude;opacity:0;transition:opacity 0.4s;}}
.stat-card:hover{{transform:translateY(-8px);box-shadow:0 20px 60px rgba(108,111,255,0.08);}}
.stat-card:hover::before{{opacity:1;}}
.stat-number{{font-size:44px;font-weight:800;letter-spacing:-2px;line-height:1;}}
.stat-label{{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:2px;margin-top:8px;}}
.stat-indigo{{color:var(--accent-light);}}
.stat-emerald{{color:var(--cyan);}}
.stat-amber{{color:var(--amber);}}

/* GRADIENT DIVIDERS */
.grad-divider{{height:1px;border-radius:1px;margin:0;opacity:0.8;}}
.gd-rainbow{{background:linear-gradient(90deg,var(--accent),var(--purple),var(--cyan),var(--emerald));height:2px;opacity:0.6;}}
.gd-amber{{background:linear-gradient(90deg,var(--amber),transparent);}}
.gd-emerald{{background:linear-gradient(90deg,var(--cyan),transparent);}}
.gd-indigo{{background:linear-gradient(90deg,var(--accent),transparent);}}
.gd-rose{{background:linear-gradient(90deg,var(--rose),transparent);}}
.gd-purple{{background:linear-gradient(90deg,var(--purple),transparent);}}

/* SECTIONS */
section{{padding:var(--space-2xl) 0 120px 0;position:relative;}}
.section-icon{{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;justify-content:center;margin-bottom:var(--space-md);transition:all 0.4s cubic-bezier(0.16,1,0.3,1);background:var(--surface);border:1px solid var(--border);position:relative;}}
.section-icon::after{{content:'';position:absolute;inset:-4px;border-radius:50%;border:1px solid transparent;transition:all 0.4s;}}
.section-icon:hover{{transform:scale(1.15) rotate(5deg);}}
.section-icon svg{{width:22px;height:22px;}}
.si-amber:hover{{box-shadow:0 0 30px var(--amber-glow);border-color:rgba(255,184,77,0.3);}}
.si-emerald:hover{{box-shadow:0 0 30px var(--cyan-glow);border-color:rgba(6,214,160,0.3);}}
.si-indigo:hover{{box-shadow:0 0 30px var(--accent-glow);border-color:rgba(108,111,255,0.3);}}
.si-rose:hover{{box-shadow:0 0 30px var(--rose-glow);border-color:rgba(255,107,138,0.3);}}
.si-purple:hover{{box-shadow:0 0 30px var(--purple-glow);border-color:rgba(167,139,250,0.3);}}
.section-num{{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:10px;}}
.sn-amber{{color:var(--amber);}}
.sn-emerald{{color:var(--cyan);}}
.sn-indigo{{color:var(--accent-light);}}
.sn-rose{{color:var(--rose);}}
.sn-purple{{color:var(--purple);}}
.section-head{{font-size:clamp(28px,4.5vw,44px);font-weight:800;letter-spacing:-1px;line-height:1.1;margin-bottom:var(--space-md);color:var(--text);}}
.section-body{{font-family:'Source Serif 4',Georgia,serif;font-size:18px;color:var(--body);line-height:1.9;margin-bottom:var(--space-lg);max-width:640px;}}
.section-body::first-line{{color:var(--text-secondary);font-weight:500;}}

/* CALLOUT BOXES */
.callout{{border-radius:var(--radius-md);padding:28px 32px;margin:32px 0;background:var(--surface);position:relative;overflow:hidden;transition:all 0.3s;}}
.callout::before{{content:'';position:absolute;top:0;left:0;bottom:0;width:3px;}}
.callout:hover{{transform:translateX(4px);}}
.callout-label{{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;}}
.callout p.callout-text{{font-family:'Source Serif 4',Georgia,serif;font-size:15px;line-height:1.8;color:var(--body);}}
.callout-amber{{border:1px solid rgba(255,184,77,0.12);}}
.callout-amber::before{{background:var(--amber);}}
.callout-amber .callout-label{{color:var(--amber);}}
.callout-amber:hover{{box-shadow:0 8px 30px var(--amber-glow);}}
.callout-emerald{{border:1px solid rgba(6,214,160,0.12);}}
.callout-emerald::before{{background:var(--cyan);}}
.callout-emerald .callout-label{{color:var(--cyan);}}
.callout-emerald:hover{{box-shadow:0 8px 30px var(--cyan-glow);}}
.callout-rose{{border:1px solid rgba(255,107,138,0.12);}}
.callout-rose::before{{background:var(--rose);}}
.callout-rose .callout-label{{color:var(--rose);}}
.callout-rose:hover{{box-shadow:0 8px 30px var(--rose-glow);}}

/* TOOL CARD */
.tool-card{{background:var(--surface);border:1px solid rgba(108,111,255,0.1);border-radius:var(--radius-lg);padding:40px;margin:var(--space-md) 0;position:relative;overflow:hidden;transition:all 0.4s;}}
.tool-card::before{{content:'';position:absolute;top:-100px;right:-100px;width:300px;height:300px;border-radius:50%;background:radial-gradient(circle,var(--accent-glow),transparent 70%);transition:all 0.6s;}}
.tool-card:hover{{border-color:rgba(108,111,255,0.2);box-shadow:0 20px 60px rgba(108,111,255,0.06);}}
.tool-card:hover::before{{transform:scale(1.3);}}
.tool-name{{font-size:30px;font-weight:800;color:var(--accent-light);margin-bottom:12px;letter-spacing:-0.5px;position:relative;}}
.tool-desc{{font-family:'Source Serif 4',Georgia,serif;font-size:17px;color:var(--body);margin-bottom:28px;line-height:1.8;position:relative;}}
.tool-usecase-label{{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;color:var(--accent);letter-spacing:2px;text-transform:uppercase;margin-bottom:10px;position:relative;}}
.tool-usecase{{font-family:'Source Serif 4',Georgia,serif;font-size:15px;color:var(--body);line-height:1.8;position:relative;}}

/* CTA */
.cta-section{{text-align:center;padding:120px 0;position:relative;}}
.cta-section::before{{content:'';position:absolute;inset:0;background:radial-gradient(circle 400px at 50% 50%,var(--accent-glow),transparent);}}
.cta-section h2{{font-size:clamp(30px,5vw,48px);font-weight:800;letter-spacing:-1px;margin-bottom:var(--space-md);color:var(--text);position:relative;}}
.cta-btn{{display:inline-block;position:relative;background:linear-gradient(135deg,var(--accent),var(--purple));color:#fff;font-size:14px;font-weight:700;padding:18px 44px;border-radius:var(--radius-md);text-decoration:none;letter-spacing:0.5px;transition:all 0.3s cubic-bezier(0.16,1,0.3,1);box-shadow:0 4px 24px rgba(108,111,255,0.3);overflow:hidden;}}
.cta-btn::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,transparent 30%,rgba(255,255,255,0.1) 50%,transparent 70%);transform:translateX(-100%);transition:transform 0.6s;}}
.cta-btn:hover{{transform:translateY(-3px);box-shadow:0 12px 48px rgba(108,111,255,0.4);}}
.cta-btn:hover::before{{transform:translateX(100%);}}

/* ARTICLE GRID */
.article-grid{{display:grid;grid-template-columns:1fr;gap:var(--space-sm);margin-top:var(--space-lg);}}
@media(min-width:640px){{.article-grid{{grid-template-columns:1fr 1fr;}}}}
@media(min-width:900px){{.article-grid{{grid-template-columns:1fr 1fr 1fr;}}}}
.article-card{{display:block;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-md);padding:28px;text-decoration:none;transition:all 0.4s cubic-bezier(0.16,1,0.3,1);position:relative;overflow:hidden;}}
.article-card::before{{content:'';position:absolute;inset:0;border-radius:var(--radius-md);padding:1px;background:linear-gradient(135deg,transparent 40%,var(--border-hover),transparent 60%);-webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);-webkit-mask-composite:xor;mask-composite:exclude;opacity:0;transition:opacity 0.4s;}}
.article-card:hover{{transform:translateY(-8px) scale(1.02);}}
.article-card:hover::before{{opacity:1;}}
.article-meta{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;}}
.article-cat{{font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:1px;}}
.score-ring{{flex-shrink:0;}}
.article-card h4{{font-size:14px;font-weight:700;color:var(--text);line-height:1.5;margin-bottom:10px;transition:color 0.3s;}}
.article-card:hover h4{{color:var(--accent-light);}}
.article-summary{{font-family:'Source Serif 4',Georgia,serif;font-size:13px;color:var(--body);line-height:1.7;margin-bottom:14px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}}
.article-source{{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:0.5px;}}

/* EXECUTIVE SUMMARY */
.exec-bar{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-xl);padding:44px;margin:var(--space-2xl) 0;position:relative;overflow:hidden;transition:all 0.4s;}}
.exec-bar::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--purple),var(--cyan),var(--emerald));}}
.exec-bar::after{{content:'';position:absolute;top:0;right:0;width:200px;height:200px;background:radial-gradient(circle,var(--accent-glow),transparent 70%);}}
.exec-bar:hover{{border-color:var(--border-hover);box-shadow:0 20px 80px rgba(108,111,255,0.04);}}
.exec-bar .exec-label{{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;color:var(--accent);letter-spacing:2.5px;text-transform:uppercase;margin-bottom:16px;position:relative;}}
.exec-bar p.exec-text{{font-family:'Source Serif 4',Georgia,serif;font-size:18px;color:var(--body);line-height:1.9;position:relative;}}

/* PROGRESS NAV */
.progress-nav{{position:fixed;right:28px;top:50%;transform:translateY(-50%);z-index:100;display:flex;flex-direction:column;gap:14px;padding:16px 8px;background:rgba(10,12,18,0.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-radius:20px;border:1px solid var(--border);}}
.progress-dot{{width:8px;height:8px;border-radius:50%;background:var(--dim);transition:all 0.4s cubic-bezier(0.16,1,0.3,1);cursor:pointer;border:none;display:block;}}
.progress-dot.active{{background:var(--accent);box-shadow:0 0 16px var(--accent-glow);transform:scale(1.5);}}
.progress-dot:hover:not(.active){{background:var(--muted);transform:scale(1.2);}}
@media(max-width:768px){{.progress-nav{{display:none;}}}}

/* ALT BG */
.alt-bg{{background:var(--surface);}}
.alt-bg::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 40% at 80% 50%,rgba(108,111,255,0.03),transparent);pointer-events:none;}}

/* FOOTER */
footer{{text-align:center;padding:80px 0 60px;position:relative;}}
footer::before{{content:'';position:absolute;top:0;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,var(--border),transparent);}}
footer p{{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:0.5px;}}
.footer-pulse{{display:inline-block;width:6px;height:6px;background:var(--accent);border-radius:50%;margin-right:8px;animation:fpulse 2.5s ease-in-out infinite;vertical-align:middle;}}
@keyframes fpulse{{0%,100%{{opacity:1;box-shadow:0 0 0 0 var(--accent-glow);}}50%{{opacity:0.5;box-shadow:0 0 0 10px transparent;}}}}
</style>
</head>
<body>

<!-- READING PROGRESS BAR -->
<div id="reading-progress"></div>

<!-- PROGRESS NAV -->
<nav class="progress-nav">
  <a href="#hero" class="progress-dot active" title="Top"></a>
  <a href="#s1" class="progress-dot" title="Market Signal"></a>
  <a href="#s2" class="progress-dot" title="Research"></a>
  <a href="#s3" class="progress-dot" title="Tool"></a>
  <a href="#s4" class="progress-dot" title="Risk"></a>
  <a href="#s5" class="progress-dot" title="Opportunity"></a>
  <a href="#sources" class="progress-dot" title="Sources"></a>
</nav>

<!-- HERO -->
<div class="hero" id="hero">
<canvas id="particle-canvas"></canvas>
<div class="hero-content">
  <div class="reveal">
    <p class="hero-label">AI Intelligence Brief &nbsp;// &nbsp;Week {wk}</p>
    <h1>What's Shaping<br><span>AI This Week</span></h1>
    <p class="hero-sub">Curated signal from across the world of artificial intelligence. The breakthroughs, tools, and trends your team needs to know.</p>
    <p class="hero-meta"><span>Week {wk}</span> &nbsp;// &nbsp;{date_range} &nbsp;// &nbsp;<span>{num_sources} sources</span></p>
  </div>
</div>
<div class="scroll-hint">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12l7 7 7-7" stroke="var(--muted)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
</div>
</div>

<!-- STATS ROW -->
<div class="container">
<div class="stats-row">
  <div class="stat-card reveal">
    <div class="stat-number stat-indigo" data-count="{num_articles}">0</div>
    <div class="stat-label">Top Articles</div>
  </div>
  <div class="stat-card reveal reveal-delay-1">
    <div class="stat-number stat-emerald" data-count="{num_sources}">0</div>
    <div class="stat-label">Sources Scanned</div>
  </div>
  <div class="stat-card reveal reveal-delay-2">
    <div class="stat-number stat-amber" data-count="{avg_score}" data-decimals="1">0</div>
    <div class="stat-label">Avg. Score</div>
  </div>
</div>
</div>

<!-- EXECUTIVE SUMMARY -->
<div class="container">
<div class="exec-bar reveal">
  <p class="exec-label">Executive Summary</p>
  <p class="exec-text">{es}</p>
</div>
</div>

<!-- GRADIENT DIVIDER -->
<div class="container"><div class="grad-divider gd-rainbow reveal"></div></div>

<!-- 01 THE SHIFT -->
<section id="s1">
<div class="container">
  <div class="section-icon si-amber reveal">
    <svg viewBox="0 0 24 24" fill="none"><path d="M3 17l4-5 4 3 6-8" stroke="#FBBF24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="21" cy="7" r="2" fill="#FBBF24"/></svg>
  </div>
  <p class="section-num sn-amber reveal">01 &mdash; The Shift</p>
  <h2 class="section-head reveal reveal-delay-1">{esc(s1.get('headline',''))}</h2>
  <p class="section-body reveal reveal-delay-2">{esc(s1.get('body',''))}</p>
  <div class="callout callout-amber reveal reveal-delay-3">
    <p class="callout-label">Why This Matters</p>
    <p class="callout-text">{esc(s1.get('why_it_matters',''))}</p>
  </div>
</div>
</section>
<div class="container"><div class="grad-divider gd-amber"></div></div>

<!-- 02 THE BREAKTHROUGH -->
<section class="alt-bg" id="s2">
<div class="container">
  <div class="section-icon si-emerald reveal">
    <svg viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="6" stroke="#34D399" stroke-width="2"/><path d="M16 16l5 5" stroke="#34D399" stroke-width="2" stroke-linecap="round"/><circle cx="11" cy="11" r="2.5" fill="#34D399" opacity="0.3"/></svg>
  </div>
  <p class="section-num sn-emerald reveal">02 &mdash; The Breakthrough</p>
  <h2 class="section-head reveal reveal-delay-1">{esc(s2.get('headline',''))}</h2>
  <p class="section-body reveal reveal-delay-2">{esc(s2.get('body',''))}</p>
  <div class="callout callout-emerald reveal reveal-delay-3">
    <p class="callout-label">Strategic Takeaway</p>
    <p class="callout-text">{esc(s2.get('strategic_takeaway',''))}</p>
  </div>
</div>
</section>
<div class="container"><div class="grad-divider gd-emerald"></div></div>

<!-- 03 THE TOOL -->
<section id="s3">
<div class="container">
  <div class="section-icon si-indigo reveal">
    <svg viewBox="0 0 24 24" fill="none"><rect x="5" y="3" width="14" height="18" rx="2" stroke="#818CF8" stroke-width="2"/><path d="M9 8h6M9 12h6M9 16h3" stroke="#818CF8" stroke-width="1.5" stroke-linecap="round"/></svg>
  </div>
  <p class="section-num sn-indigo reveal">03 &mdash; Tool of the Week</p>
  <div class="tool-card reveal reveal-delay-1">
    <p class="tool-name">{esc(s3.get('name',''))}</p>
    <p class="tool-desc">{esc(s3.get('description',''))}</p>
    <p class="tool-usecase-label">Use Case for Our Team</p>
    <p class="tool-usecase">{esc(s3.get('use_case',''))}</p>
  </div>
</div>
</section>
<div class="container"><div class="grad-divider gd-indigo"></div></div>

<!-- 04 RISK & RESPONSIBILITY -->
<section class="alt-bg" id="s4">
<div class="container">
  <div class="section-icon si-rose reveal">
    <svg viewBox="0 0 24 24" fill="none"><path d="M12 4l9 16H3L12 4z" stroke="#FB7185" stroke-width="2" stroke-linejoin="round"/><path d="M12 10v4" stroke="#FB7185" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17" r="1" fill="#FB7185"/></svg>
  </div>
  <p class="section-num sn-rose reveal">04 &mdash; Risk Watch</p>
  <p class="section-body reveal reveal-delay-1">{esc(s4.get('insight',''))}</p>
  <div class="callout callout-rose reveal reveal-delay-2">
    <p class="callout-label">Action Consideration</p>
    <p class="callout-text">{esc(s4.get('action',''))}</p>
  </div>
</div>
</section>
<div class="container"><div class="grad-divider gd-rose"></div></div>

<!-- 05 INTERNAL MOMENTUM -->
<section class="cta-section" id="s5">
<div class="container">
  <div class="reveal">
    <div class="section-icon si-purple" style="margin:0 auto 16px auto;">
      <svg viewBox="0 0 24 24" fill="none"><path d="M12 3v4M12 17v4M3 12h4M17 12h4" stroke="#A78BFA" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="4" stroke="#A78BFA" stroke-width="2"/></svg>
    </div>
    <p class="section-num sn-purple" style="margin-bottom:16px;">05 &mdash; Opportunity</p>
    <h2>{s5[:80] if len(s5) > 80 else 'What could we build this quarter?'}</h2>
    <p class="section-body" style="margin:16px auto;text-align:center;">{s5}</p>
    <a href="mailto:ai-team@company.com?subject=AI Experiment Proposal" class="cta-btn">Propose an AI Experiment</a>
  </div>
</div>
</section>

<!-- SOURCE ARTICLES -->
<section class="alt-bg" id="sources">
<div class="container-wide">
  <div style="text-align:center;">
    <p class="section-num sn-indigo reveal">Source Intelligence</p>
    <h2 class="section-head reveal reveal-delay-1" style="font-size:28px;text-align:center;">This week's top signals</h2>
  </div>
  <div class="article-grid reveal reveal-delay-2">
    {article_cards}
  </div>
</div>
</section>

<!-- FOOTER -->
<footer>
<div class="container">
  <p><span class="footer-pulse"></span>Powered by AI</p>
  <p style="margin-top:8px;color:var(--dim);">AI Intelligence Brief // Week {wk} // {num_sources} sources</p>
</div>
</footer>

<script>
// Reading progress bar (optimized with rAF)
(function(){{
  var bar=document.getElementById('reading-progress');
  var ticking=false;
  window.addEventListener('scroll',function(){{
    if(!ticking){{ticking=true;requestAnimationFrame(function(){{
      var h=document.documentElement.scrollHeight-window.innerHeight;
      bar.style.width=h>0?((window.scrollY/h)*100)+'%':'0%';
      ticking=false;
    }});}}}});
}})();

// Scroll reveal with stagger
(function(){{
  var reveals=document.querySelectorAll('.reveal');
  var obs=new IntersectionObserver(function(entries){{
    entries.forEach(function(e){{
      if(e.isIntersecting){{e.target.classList.add('visible');obs.unobserve(e.target);}}
    }});
  }},{{threshold:0.08,rootMargin:'0px 0px -60px 0px'}});
  reveals.forEach(function(el){{obs.observe(el);}});
}})();

// Progress dots with smooth transitions
(function(){{
  var dots=document.querySelectorAll('.progress-dot');
  var sections=['hero','s1','s2','s3','s4','s5','sources'];
  var obs=new IntersectionObserver(function(entries){{
    entries.forEach(function(e){{
      if(e.isIntersecting){{
        dots.forEach(function(d){{d.classList.remove('active');}});
        var m=document.querySelector('.progress-dot[href="#'+e.target.id+'"]');
        if(m) m.classList.add('active');
      }}
    }});
  }},{{threshold:0.25}});
  sections.forEach(function(id){{
    var el=document.getElementById(id);
    if(el) obs.observe(el);
  }});
}})();

// Animated counters with spring easing
(function(){{
  var targets=[];
  document.querySelectorAll('[data-count]').forEach(function(el){{
    targets.push({{el:el,target:parseFloat(el.getAttribute('data-count')),decimals:parseInt(el.getAttribute('data-decimals')||'0'),done:false}});
  }});
  var obs=new IntersectionObserver(function(entries){{
    entries.forEach(function(e){{
      if(!e.isIntersecting) return;
      var t=targets.find(function(x){{return x.el===e.target;}});
      if(!t||t.done) return;
      t.done=true;
      var start=performance.now();
      var dur=1800;
      function tick(now){{
        var p=Math.min((now-start)/dur,1);
        var ease=1-Math.pow(1-p,4);
        var v=ease*t.target;
        t.el.textContent=t.decimals>0?v.toFixed(t.decimals):Math.round(v);
        if(p<1) requestAnimationFrame(tick);
      }}
      requestAnimationFrame(tick);
    }});
  }},{{threshold:0.4}});
  targets.forEach(function(t){{obs.observe(t.el);}});
}})();

// Article card magnetic hover with glow
document.querySelectorAll('.article-card').forEach(function(card){{
  var glow=card.getAttribute('data-glow')||'rgba(108,111,255,0.08)';
  card.addEventListener('mouseenter',function(){{
    card.style.boxShadow='0 20px 60px '+glow+',0 0 0 1px rgba(255,255,255,0.08)';
  }});
  card.addEventListener('mouseleave',function(){{
    card.style.boxShadow='none';
    card.style.transform='';
  }});
}});

// Stat card 3D tilt
document.querySelectorAll('.stat-card').forEach(function(card){{
  card.addEventListener('mousemove',function(e){{
    var rect=card.getBoundingClientRect();
    var x=(e.clientX-rect.left)/rect.width-0.5;
    var y=(e.clientY-rect.top)/rect.height-0.5;
    card.style.transform='translateY(-8px) perspective(600px) rotateX('+(-y*8)+'deg) rotateY('+(x*8)+'deg)';
  }});
  card.addEventListener('mouseleave',function(){{
    card.style.transform='';
    card.style.transition='all 0.4s cubic-bezier(0.16,1,0.3,1)';
  }});
  card.addEventListener('mouseenter',function(){{
    card.style.transition='transform 0.1s ease-out';
  }});
}});

// Particle constellation with mouse interaction
(function(){{
  var c=document.getElementById('particle-canvas');
  if(!c) return;
  var ctx=c.getContext('2d');
  var dpr=window.devicePixelRatio||1;
  var W,H,particles=[],mouse={{x:-999,y:-999}};
  function resize(){{
    var cw=c.parentElement.offsetWidth||window.innerWidth;
    var ch=c.parentElement.offsetHeight||window.innerHeight;
    c.width=cw*dpr;c.height=ch*dpr;
    c.style.width=cw+'px';c.style.height=ch+'px';
    ctx.scale(dpr,dpr);
    W=cw;H=ch;
    // Re-distribute particles across full canvas
    particles.forEach(function(p){{if(p.x>W)p.x=Math.random()*W;if(p.y>H)p.y=Math.random()*H;}});
  }}
  resize();
  window.addEventListener('resize',function(){{ctx.setTransform(1,0,0,1,0,0);resize();}});
  c.addEventListener('mousemove',function(e){{var r=c.getBoundingClientRect();mouse.x=e.clientX-r.left;mouse.y=e.clientY-r.top;}});
  c.addEventListener('mouseleave',function(){{mouse.x=-999;mouse.y=-999;}});
  var colors=[
    [108,111,255],[6,214,160],[167,139,250],[255,184,77]
  ];
  for(var i=0;i<150;i++){{
    var ci=colors[Math.floor(Math.random()*colors.length)];
    particles.push({{x:Math.random()*Math.max(W,1920),y:Math.random()*Math.max(H,1080),vx:(Math.random()-0.5)*0.35,vy:(Math.random()-0.5)*0.35,r:Math.random()*3+1,a:Math.random()*0.5+0.15,c:ci}});
  }}
  function draw(){{
    ctx.clearRect(0,0,W,H);
    for(var i=0;i<particles.length;i++){{
      var p=particles[i];
      // Mouse repulsion
      var dx=p.x-mouse.x,dy=p.y-mouse.y,dm=Math.sqrt(dx*dx+dy*dy);
      if(dm<200&&dm>0){{var f=(200-dm)/200*1.2;p.x+=dx/dm*f;p.y+=dy/dm*f;}}
      p.x+=p.vx;p.y+=p.vy;
      if(p.x<0)p.x=W;if(p.x>W)p.x=0;
      if(p.y<0)p.y=H;if(p.y>H)p.y=0;
      // Glow effect
      ctx.beginPath();ctx.arc(p.x,p.y,p.r*3,0,Math.PI*2);
      ctx.fillStyle='rgba('+p.c[0]+','+p.c[1]+','+p.c[2]+','+(p.a*0.15)+')';ctx.fill();
      // Core dot
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);
      ctx.fillStyle='rgba('+p.c[0]+','+p.c[1]+','+p.c[2]+','+p.a+')';ctx.fill();
      for(var j=i+1;j<particles.length;j++){{
        var q=particles[j];
        var ddx=p.x-q.x,ddy=p.y-q.y,d=Math.sqrt(ddx*ddx+ddy*ddy);
        if(d<200){{
          var la=0.12*(1-d/200);
          ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(q.x,q.y);
          ctx.strokeStyle='rgba(108,111,255,'+la+')';
          ctx.lineWidth=1.2;ctx.stroke();
        }}
      }}
    }}
    requestAnimationFrame(draw);
  }}
  draw();
}})();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Email Sending via Brevo
# ---------------------------------------------------------------------------

def send_email(html_content: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not OUTLOOK_FROM_EMAIL or not OUTLOOK_PASSWORD:
        log.error("OUTLOOK_FROM_EMAIL or OUTLOOK_PASSWORD is not set")
        return False
    if not OUTLOOK_TO_EMAIL:
        log.error("OUTLOOK_TO_EMAIL is not set")
        return False

    now = datetime.now(timezone.utc)
    wk = (now - datetime(2026, 1, 1, tzinfo=timezone.utc)).days // 7 + 1
    subject = f"AI Intelligence Brief // Week {wk} // {now.strftime('%b %d, %Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = OUTLOOK_FROM_EMAIL
    msg["To"] = OUTLOOK_TO_EMAIL
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP("smtp.office365.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(OUTLOOK_FROM_EMAIL, OUTLOOK_PASSWORD)
            server.sendmail(OUTLOOK_FROM_EMAIL, OUTLOOK_TO_EMAIL.split(","), msg.as_string())
        log.info(f"Email sent via Outlook SMTP to {OUTLOOK_TO_EMAIL}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"Outlook auth failed: {e}")
        return False
    except Exception as e:
        log.error(f"Send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(collect: bool = True, send: bool = True, dry_run: bool = False):
    log.info("=" * 60)
    log.info("AI Intelligence Brief Pipeline — Starting")
    log.info("=" * 60)

    store = get_store()

    if collect:
        log.info("--- Step 1: Collecting RSS feeds ---")
        new_count = collect_feeds(store)
        log.info(f"Collected {new_count} new articles")

    if not send:
        log.info("Collection-only mode — stopping here")
        return

    log.info("--- Step 2: Fetching unprocessed articles ---")
    unprocessed = store.get_unprocessed(days=7)
    log.info(f"Found {len(unprocessed)} unprocessed articles")

    if not unprocessed:
        log.info("No articles to process — exiting")
        return

    log.info("--- Step 3: Processing with Gemini ---")
    brief = process_articles(store, unprocessed)

    if not brief:
        log.warning("Failed to generate brief")
        return

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')

    # Generate BOTH outputs
    log.info("--- Step 4: Generating email version ---")
    email_html = generate_email_html(brief)
    email_file = output_dir / f"brief_email_{ts}.html"
    email_file.write_text(email_html, encoding="utf-8")
    log.info(f"Email version: {email_file}")

    log.info("--- Step 5: Generating web experience ---")
    web_html = generate_web_html(brief)
    web_file = output_dir / f"brief_web_{ts}.html"
    web_file.write_text(web_html, encoding="utf-8")
    log.info(f"Web version:   {web_file}")

    # Save raw brief JSON
    json_file = output_dir / f"brief_data_{ts}.json"
    # Remove non-serializable fields
    safe_brief = {k: v for k, v in brief.items() if k != "_all_scored"}
    json_file.write_text(json.dumps(safe_brief, indent=2, default=str), encoding="utf-8")

    if dry_run:
        log.info("Dry run — email not sent")
        log.info(f"Preview email: {email_file}")
        log.info(f"Preview web:   {web_file}")
        return

    log.info("--- Step 6: Sending via Brevo ---")
    success = send_email(email_html)
    if not success:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="AI Intelligence Brief Pipeline")
    parser.add_argument("--collect-only", action="store_true", help="Only collect feeds")
    parser.add_argument("--send-only", action="store_true", help="Only generate & send")
    parser.add_argument("--dry-run", action="store_true", help="Generate HTML, don't send")
    parser.add_argument("--fresh", action="store_true", help="Wipe DB and start fresh")
    args = parser.parse_args()

    if args.collect_only and args.send_only:
        log.error("Cannot use --collect-only and --send-only together")
        sys.exit(1)

    if args.fresh:
        log.info("Wiping database for fresh start...")
        store = get_store()
        store.wipe()

    run_pipeline(collect=not args.send_only, send=not args.collect_only, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
