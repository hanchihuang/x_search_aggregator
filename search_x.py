#!/usr/bin/env python3
"""Improved Search x.com by keyword, collect results, and generate summary outputs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import BrowserContext, Page, sync_playwright, TimeoutError

from html_report import write_html_article

# Enhanced regex patterns
COUNT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", re.IGNORECASE)
ZH_COUNT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([万亿])")
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_\u4e00-\u9fff]+)")
MENTION_RE = re.compile(r"@([A-Za-z0-9_]+)")
STATUS_PATH_RE = re.compile(r"(?:https?://x\.com)?/(?:i/web/)?([^/]+)/status/(\d+)")

# Multiple selector strategies for tweets
TWEET_SELECTORS = [
    'article[data-testid="tweet"]',
    'div[data-testid="cellInnerDiv"] article[data-testid="tweet"]',
    'main article[data-testid="tweet"]',
]

FEED_SELECTORS = [
    'section[role="region"] div[aria-label][tabindex="0"]',
    'div[role="main"]',
    "main",
]

END_MARKER_SELECTORS = [
    'span:has-text("No more results")',
    'span:has-text("Try searching for something else")',
    'span:has-text("没有更多结果")',
    'span:has-text("换个关键词试试")',
]

# Text content selectors with fallbacks
TEXT_SELECTORS = [
    'div[data-testid="tweetText"]',
    'div[lang] div:not([data-testid]):not([role])',
    'div[dir="auto"][lang]',
    'span[lang]',
]

# User info selectors
USER_SELECTORS = [
    'div[data-testid="User-Name"]',
    'a[href^="/"][role="link"]',
    'div[aria-label*="@"], span[aria-label*="@"]',
]

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search x.com and aggregate results.")
    parser.add_argument("--keyword", required=True, help="Search keyword")
    parser.add_argument("--max-items", type=int, default=200, help="Max tweets to collect")
    parser.add_argument("--max-scrolls", type=int, default=120, help="Max scroll rounds (increased)")
    parser.add_argument("--no-new-stop", type=int, default=8, help="Stop after N rounds with no new tweets (increased)")
    parser.add_argument("--sort", choices=["Top", "Latest"], default="Latest", help="Search tab")
    parser.add_argument("--lang", default="", help="Optional language code, e.g. en/zh")
    parser.add_argument("--state", default="auth_state.json", help="Playwright storage state path")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--out-dir", default="output", help="Output base directory")
    parser.add_argument("--scroll-pause", type=int, default=2000, help="Pause between scrolls in ms")
    parser.add_argument("--retry-attempts", type=int, default=3, help="Retry attempts for failed operations")
    parser.add_argument("--skip-fulltext", action="store_true", help="Skip stage-2 hydration from tweet detail pages")
    parser.add_argument("--fulltext-delay-ms", type=int, default=1200, help="Pause after opening each tweet detail page")
    parser.add_argument("--fulltext-checkpoint-every", type=int, default=10, help="Write hydration checkpoint every N tweets")
    return parser.parse_args()


def parse_count(raw: str) -> int:
    if not raw:
        return 0
    raw = str(raw).strip().replace(",", "").replace(" ", "")
    if not raw:
        return 0
    zh_m = ZH_COUNT_RE.search(raw)
    if zh_m:
        value = float(zh_m.group(1))
        unit = zh_m.group(2)
        multiplier = {"万": 10_000, "亿": 100_000_000}.get(unit, 1)
        return int(value * multiplier)

    m = COUNT_RE.search(raw)
    if not m:
        # Try to extract just numbers
        nums = re.findall(r'\d+', raw.replace(',', ''))
        if nums:
            return int(nums[0])
        return 0

    value = float(m.group(1))
    suffix = m.group(2).upper()
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(value * multiplier)


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:60] or "keyword"


def make_search_url(keyword: str, tab: str, lang: str) -> str:
    normalized_keyword = re.sub(r"\s+", " ", keyword).strip()
    q = normalized_keyword
    if lang:
        q = f"{normalized_keyword} lang:{lang}"
    f = "live" if tab == "Latest" else "top"
    # Ensure proper encoding for Chinese characters
    encoded_q = quote(q, safe=":")
    return f"https://x.com/search?q={encoded_q}&src=typed_query&f={f}"


def parse_status_href(href: str) -> tuple[Optional[str], Optional[str]]:
    if not href:
        return None, None
    match = STATUS_PATH_RE.search(href)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def extract_metric(article, testid: str) -> int:
    """Extract metrics with multiple fallback strategies"""
    # Primary method: data-testid buttons
    btn = article.query_selector(f'button[data-testid="{testid}"]')
    if btn:
        text = (btn.inner_text() or "").strip()
        if text:
            return parse_count(text)
    
    # Fallback: look for any button with the metric name
    fallback_selectors = [
        f'button[aria-label*="{testid}"]',
        f'div[aria-label*="{testid}"]',
        f'span[data-testid*="{testid}"]',
    ]
    
    for selector in fallback_selectors:
        element = article.query_selector(selector)
        if element:
            text = (element.inner_text() or "").strip()
            if text:
                return parse_count(text)
    
    return 0


def extract_views(article) -> int:
    """Extract view count with enhanced fallbacks"""
    # Primary: analytics button
    btn = article.query_selector('button[data-testid="analytics"]')
    if btn:
        text = (btn.inner_text() or "").strip()
        if text:
            return parse_count(text)
    
    # Fallback: look for view indicators
    view_selectors = [
        'span:has-text("view")',
        'div:has-text("view")',
        '[aria-label*="view"]',
    ]
    
    for selector in view_selectors:
        elements = article.query_selector_all(selector)
        for el in elements:
            text = (el.inner_text() or "").strip()
            if text and ('view' in text.lower() or '浏览' in text):
                return parse_count(text)
    
    return 0


def extract_text_content(article) -> str:
    """Extract tweet text with multiple fallback strategies"""
    for selector in TEXT_SELECTORS:
        elements = article.query_selector_all(selector)
        for el in elements:
            text = (el.inner_text() or "").strip()
            if text and len(text) > 0:
                return text
    
    # Last resort: get all text from the article
    all_text = (article.inner_text() or "").strip()
    if all_text:
        # Try to find the main content by splitting and taking reasonable chunks
        lines = [line.strip() for line in all_text.split('\n') if line.strip()]
        if lines:
            # Return the longest line that seems like content (not metadata)
            content_lines = [line for line in lines if len(line) > 10 and not line.startswith(('Reply', 'Retweet', 'Like', 'Bookmark'))]
            if content_lines:
                return max(content_lines, key=len)
    
    return ""


def extract_user_info(article) -> tuple[str, str]:
    """Extract user name and handle with fallbacks"""
    user_name = ""
    user_handle = ""
    
    # Try primary user block selector
    for selector in USER_SELECTORS:
        user_blocks = article.query_selector_all(selector)
        for block in user_blocks:
            # Extract user name
            name_spans = block.query_selector_all('span')
            for span in name_spans:
                span_text = (span.inner_text() or "").strip()
                if span_text and not span_text.startswith('@') and len(span_text) > 1:
                    user_name = span_text
                    break
            
            # Extract user handle
            handle_links = block.query_selector_all('a[href^="/"]')
            for link in handle_links:
                href = (link.get_attribute("href") or "").strip()
                if href and href.count('/') == 1 and not href.endswith('/'):
                    user_handle = href.strip('/')
                    break
            
            if user_name or user_handle:
                break
        
        if user_name or user_handle:
            break
    
    # Fallback: extract from URL if available
    if not user_handle:
        links = article.query_selector_all('a[href*="/status/"]')
        for a in links:
            href = (a.get_attribute("href") or "").strip()
            handle, _ = parse_status_href(href)
            if handle:
                user_handle = handle
                break

    return user_name, user_handle


def extract_tweet(article) -> Optional[Dict]:
    """Enhanced tweet extraction with robust fallbacks"""
    # Extract tweet URL and ID
    links = article.query_selector_all('a[href*="/status/"]')
    tweet_url = None
    tweet_id = None
    
    link_user_handle = ""
    for a in links:
        href = (a.get_attribute("href") or "").strip()
        handle, status_id = parse_status_href(href)
        if status_id:
            tweet_url = href if href.startswith("http") else f"https://x.com{href.split('?')[0]}"
            tweet_id = status_id
            link_user_handle = handle or ""
            break

    if not tweet_id:
        return None

    # Extract text content
    text = extract_text_content(article)

    # Extract timestamp
    posted_at = None
    time_el = article.query_selector("time")
    if time_el:
        posted_at = time_el.get_attribute("datetime")
    else:
        # Fallback: look for aria-label with timestamp
        time_aria = article.query_selector('[aria-label*="年"]')  # Chinese date format
        if not time_aria:
            time_aria = article.query_selector('[aria-label*=":"], [aria-label*="AM"], [aria-label*="PM"]')
        if time_aria:
            aria_label = time_aria.get_attribute("aria-label") or ""
            # Extract potential timestamp from aria-label
            if ":" in aria_label:
                posted_at = aria_label

    # Extract user info
    user_name, user_handle = extract_user_info(article)
    if not user_handle:
        user_handle = link_user_handle

    return {
        "tweet_id": tweet_id,
        "url": tweet_url,
        "user_name": user_name,
        "user_handle": user_handle,
        "posted_at": posted_at,
        "text": text,
        "reply_count": extract_metric(article, "reply"),
        "retweet_count": extract_metric(article, "retweet"),
        "like_count": extract_metric(article, "like"),
        "bookmark_count": extract_metric(article, "bookmark"),
        "view_count": extract_views(article),
    }


def wait_for_search_results(page: Page, timeout: int = 10000) -> bool:
    """Wait for search results to load with multiple validation strategies"""
    try:
        # Wait for any of the tweet selectors to appear
        for selector in TWEET_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=timeout//len(TWEET_SELECTORS))
                return True
            except TimeoutError:
                continue
        
        # Fallback: wait for general content
        page.wait_for_selector('main, section, div[role="feed"]', timeout=timeout)
        return True
    except TimeoutError:
        print("Warning: Could not detect search results loading")
        return False


def handle_search_error_retry(page: Page, attempts: int = 3) -> bool:
    """Click retry when search timeline returns transient loading errors."""
    retry_selectors = [
        'button:has-text("重试")',
        'button:has-text("Retry")',
        'div[role="button"]:has-text("重试")',
        'div[role="button"]:has-text("Retry")',
    ]
    for i in range(attempts):
        for selector in retry_selectors:
            btn = page.query_selector(selector)
            if not btn:
                continue
            try:
                btn.click(timeout=2000)
                page.wait_for_timeout(2200)
                if get_cards(page):
                    print(f"Recovered from search error after retry attempt {i + 1}.")
                    return True
            except Exception:
                continue
        page.wait_for_timeout(1200)
    return bool(get_cards(page))


def get_cards(page: Page):
    cards = []
    for selector in TWEET_SELECTORS:
        found = page.query_selector_all(selector)
        if found:
            cards.extend(found)
    return cards


def has_end_marker(page: Page) -> bool:
    for selector in END_MARKER_SELECTORS:
        if page.query_selector(selector):
            return True
    return False


def get_last_visible_anchor(page: Page) -> str:
    cards = get_cards(page)
    for card in reversed(cards):
        link = card.query_selector('a[href*="/status/"]')
        if not link:
            continue
        href = (link.get_attribute("href") or "").strip()
        handle, status_id = parse_status_href(href)
        if handle and status_id:
            return f"{handle}/{status_id}"
    return ""


def scroll_feed(page: Page, round_idx: int) -> None:
    page.evaluate(
        """(selectors) => {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && typeof el.scrollBy === "function") {
                    el.scrollBy(0, Math.floor(window.innerHeight * 1.4));
                    break;
                }
            }
        }""",
        FEED_SELECTORS,
    )
    page.mouse.wheel(0, 3200)
    page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 1.4))")
    if round_idx % 3 == 0:
        page.keyboard.press("End")


def collect_tweets(
    page: Page,
    max_items: int,
    max_scrolls: int,
    no_new_stop: int,
    scroll_pause: int,
    checkpoint_cb: Optional[Callable[[List[Dict], int, int], None]] = None,
) -> List[Dict]:
    """Enhanced tweet collection with better scrolling and detection"""
    seen: Dict[str, Dict] = {}
    seen_ids: Set[str] = set()
    no_new_rounds = 0
    anchor_stall_rounds = 0
    last_anchor = ""
    
    # Initial wait for page to load
    time.sleep(2)
    
    for idx in range(max_scrolls):
        cards = get_cards(page)
        new_count = 0
        print(f"Scroll {idx + 1}/{max_scrolls}: cards in viewport={len(cards)}")

        for card in cards:
            try:
                item = extract_tweet(card)
                if not item:
                    continue
                if item["tweet_id"] in seen_ids:
                    continue
                
                seen[item["tweet_id"]] = item
                seen_ids.add(item["tweet_id"])
                new_count += 1
                
                if len(seen) >= max_items:
                    if checkpoint_cb:
                        checkpoint_cb(list(seen.values()), idx, new_count)
                    print(f"Reached max items: {max_items}")
                    return list(seen.values())
            except Exception as e:
                print(f"Error extracting tweet: {e}")
                continue

        print(f"Scroll {idx + 1}/{max_scrolls}: +{new_count} new, total {len(seen)}")
        if checkpoint_cb:
            checkpoint_cb(list(seen.values()), idx, new_count)

        # Check if we should stop due to no new content
        if new_count == 0:
            no_new_rounds += 1
        else:
            no_new_rounds = 0

        current_anchor = get_last_visible_anchor(page)
        if current_anchor and current_anchor == last_anchor:
            anchor_stall_rounds += 1
        elif not current_anchor and new_count == 0:
            # If we still cannot locate any status anchor, treat it as a stall.
            anchor_stall_rounds += 1
        else:
            anchor_stall_rounds = 0
        last_anchor = current_anchor or last_anchor

        if has_end_marker(page) and new_count == 0:
            print("Detected end-of-results marker. Stop scrolling.")
            break

        if no_new_rounds >= min(no_new_stop, 6) and len(cards) == 0:
            print(
                f"No tweet cards detected for {no_new_rounds} rounds. Stop scrolling."
            )
            break

        if no_new_rounds >= no_new_stop and anchor_stall_rounds >= 3:
            print(
                f"No new tweets for {no_new_rounds} rounds and anchor stalled for {anchor_stall_rounds} rounds. Stop scrolling."
            )
            break

        scroll_feed(page, idx)
        pause_ms = scroll_pause if new_count > 0 else int(scroll_pause * 1.35)
        page.wait_for_timeout(pause_ms)

    return list(seen.values())


def checkpoint_search_outputs(run_dir: Path, items: List[Dict], keyword: str) -> None:
    summary = summarize(items, keyword)
    (run_dir / "results.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(run_dir / "results.csv", items)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_md(run_dir / "summary.md", summary)
    write_html_article(run_dir / "article.html", keyword, items)


def make_search_checkpoint_callback(
    run_dir: Path,
    keyword: str,
    every_n_scrolls: int = 5,
) -> Callable[[List[Dict], int, int], None]:
    last_saved = {"scroll": 0}

    def checkpoint(items: List[Dict], scroll_idx: int, new_count: int) -> None:
        if not items:
            return
        should_save = (
            scroll_idx == 0
            or new_count > 0 and (scroll_idx + 1 - last_saved["scroll"]) >= every_n_scrolls
        )
        if not should_save:
            return
        checkpoint_search_outputs(run_dir, items, keyword)
        last_saved["scroll"] = scroll_idx + 1
        print(f"Checkpoint saved: {run_dir / 'results.json'}")

    return checkpoint


def to_dt(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Handle various timestamp formats
        clean_ts = str(ts).strip()
        if clean_ts.endswith('Z'):
            return datetime.fromisoformat(clean_ts.replace("Z", "+00:00"))
        elif '+' in clean_ts or clean_ts.count('-') >= 2:
            return datetime.fromisoformat(clean_ts)
        else:
            # Try to parse other formats
            from dateutil import parser
            return parser.parse(clean_ts)
    except (ValueError, ImportError):
        return None


def summarize(items: List[Dict], keyword: str) -> Dict:
    hashtags = Counter()
    mentions = Counter()

    for it in items:
        txt = it.get("text") or ""
        hashtags.update([h.lower() for h in HASHTAG_RE.findall(txt)])
        mentions.update([m.lower() for m in MENTION_RE.findall(txt)])

    times = [to_dt(i.get("posted_at")) for i in items]
    times = [t for t in times if t is not None]
    times.sort()

    top_liked = sorted(items, key=lambda x: x.get("like_count", 0), reverse=True)[:10]

    return {
        "keyword": keyword,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_collected": len(items),
        "time_range": {
            "from": times[0].isoformat() if times else None,
            "to": times[-1].isoformat() if times else None,
        },
        "top_hashtags": hashtags.most_common(20),
        "top_mentions": mentions.most_common(20),
        "top_liked": [
            {
                "tweet_id": i["tweet_id"],
                "url": i["url"],
                "user_handle": i.get("user_handle", ""),
                "like_count": i.get("like_count", 0),
                "retweet_count": i.get("retweet_count", 0),
                "reply_count": i.get("reply_count", 0),
                "text": (i.get("text") or "")[:240],
            }
            for i in top_liked
        ],
    }


def write_csv(path: Path, items: List[Dict]) -> None:
    fields = [
        "tweet_id",
        "url",
        "user_name",
        "user_handle",
        "posted_at",
        "card_text",
        "text",
        "full_text",
        "full_text_status",
        "reply_count",
        "retweet_count",
        "like_count",
        "bookmark_count",
        "view_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(items)


def write_summary_md(path: Path, summary: Dict) -> None:
    lines = [
        f"# X Search Summary: {summary['keyword']}",
        "",
        f"- Generated at (UTC): {summary['generated_at']}",
        f"- Total collected: {summary['total_collected']}",
        f"- Time range: {summary['time_range']['from']} ~ {summary['time_range']['to']}",
        "",
        "## Top hashtags",
    ]
    for tag, cnt in summary["top_hashtags"]:
        lines.append(f"- #{tag}: {cnt}")

    lines.append("")
    lines.append("## Top mentions")
    for u, cnt in summary["top_mentions"]:
        lines.append(f"- @{u}: {cnt}")

    lines.append("")
    lines.append("## Top liked tweets")
    for i, row in enumerate(summary["top_liked"], start=1):
        lines.append(f"{i}. @{row['user_handle']} | likes={row['like_count']} | {row['url']}")

    path.write_text("\n".join(lines), encoding="utf-8")


def cdp_ready(cdp_url: str) -> bool:
    version_url = f"{cdp_url.rstrip('/')}/json/version"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(version_url, timeout=1.5) as resp:
            return resp.status == 200
    except URLError:
        return False
    except Exception:
        return False


def parse_cdp_port(cdp_url: str) -> int:
    return int(cdp_url.rstrip("/").split(":")[-1])


def launch_chrome_for_cdp(chrome_path: str, user_data_dir: Path, cdp_port: int):
    user_data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome_path,
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "https://x.com/home",
    ]
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        cmd.insert(-1, "--headless=new")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def can_connect_over_cdp(playwright, cdp_url: str) -> bool:
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=3000)
        browser.close()
        return True
    except Exception:
        return False


def wait_cdp_ready(playwright, cdp_url: str, wait_seconds: int) -> bool:
    for _ in range(max(1, wait_seconds * 2)):
        if cdp_ready(cdp_url) or can_connect_over_cdp(playwright, cdp_url):
            return True
        time.sleep(0.5)
    return False


def create_context(
    playwright,
    state: str,
    headless: bool,
    cdp_url: str = "",
    auto_launch: bool = False,
    chrome_path: str = "/usr/bin/google-chrome",
    user_data_dir: str = "chrome_profile",
    wait_seconds: int = 20,
) -> BrowserContext:
    # Import browser config for better fingerprinting
    try:
        from browser_config import get_browser_args, get_context_options
        launch_args = get_browser_args()
        context_options = get_context_options()
    except ImportError:
        launch_args = []
        context_options = {}
    
    if cdp_url:
        if not cdp_ready(cdp_url):
            if not auto_launch:
                raise RuntimeError(
                    "CDP endpoint is not reachable. Start Chrome with remote debugging or enable auto-launch."
                )
            cdp_port = parse_cdp_port(cdp_url)
            chrome_proc = launch_chrome_for_cdp(
                chrome_path=chrome_path,
                user_data_dir=Path(user_data_dir).expanduser().resolve(),
                cdp_port=cdp_port,
            )
            if not wait_cdp_ready(playwright, cdp_url, wait_seconds):
                stderr = "(empty)"
                if chrome_proc.stderr and chrome_proc.poll() is not None:
                    stderr = chrome_proc.stderr.read().strip() or "(empty)"
                raise RuntimeError(
                    f"CDP endpoint did not become ready within {wait_seconds}s.\nChrome stderr:\n{stderr}"
                )
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.new_context(**context_options)
        setattr(context, "_attached_browser", browser)
        return context

    browser = playwright.chromium.launch(headless=headless, args=launch_args)
    state_path = Path(state)

    if state_path.exists():
        context_options["storage_state"] = str(state_path)
        context = browser.new_context(**context_options)
        setattr(context, "_attached_browser", browser)
        return context

    print(
        f"[Warn] storage state not found: {state_path}. Continuing without login state."
    )
    context = browser.new_context(**context_options)
    setattr(context, "_attached_browser", browser)
    return context


def close_context(context: BrowserContext) -> None:
    browser = getattr(context, "_attached_browser", None)
    try:
        context.close()
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def validate_auth_state(page: Page) -> bool:
    """Validate that we're properly logged in"""
    try:
        # Check if we see the main feed or search page
        page.wait_for_selector('nav[aria-label="Primary"], input[data-testid="SearchBox_Search_Input"]', timeout=5000)
        return True
    except TimeoutError:
        # Check if we're on login page
        if page.url.startswith('https://x.com/i/flow/login') or 'login' in page.url.lower():
            print("Warning: Not logged in - you may need to run login_x.py first")
            return False
        return True


def fallback_search_via_input(page: Page, keyword: str, tab: str, lang: str) -> None:
    query = keyword.strip()
    if lang:
        query = f"{query} lang:{lang}"
    selectors = [
        'input[data-testid="SearchBox_Search_Input"]',
        'input[aria-label*="Search"]',
        'input[aria-label*="搜索"]',
    ]
    for selector in selectors:
        inp = page.query_selector(selector)
        if not inp:
            continue
        inp.click()
        inp.fill("")
        inp.type(query, delay=50)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        if tab == "Latest":
            latest_tab = page.query_selector('a[href*="f=live"], span:has-text("Latest"), span:has-text("最新")')
            if latest_tab:
                latest_tab.click()
                page.wait_for_timeout(1200)
        return


def main() -> None:
    args = parse_args()

    out_base = Path(args.out_dir).expanduser().resolve()
    run_dir = out_base / f"{safe_name(args.keyword)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run directory: {run_dir}")

    search_url = make_search_url(args.keyword, args.sort, args.lang)
    print(f"Search URL: {search_url}")

    with sync_playwright() as p:
        context = create_context(p, args.state, args.headless)
        page = context.new_page()
        
        # Navigate to search URL
        page.goto(search_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        
        # Validate authentication state
        if not validate_auth_state(page):
            print("Authentication issue detected. Please refresh login state with login_x.py.")
            context.close()
            return
        
        # Wait for search results
        if not wait_for_search_results(page):
            print("Warning: Search results may not have loaded properly, trying input fallback...")
            fallback_search_via_input(page, args.keyword, args.sort, args.lang)
            wait_for_search_results(page, timeout=15000)
        
        # If search timeline temporarily errors, attempt retry button recovery.
        if not get_cards(page):
            handle_search_error_retry(page, attempts=5)
        
        items = collect_tweets(
            page=page,
            max_items=args.max_items,
            max_scrolls=args.max_scrolls,
            no_new_stop=args.no_new_stop,
            scroll_pause=args.scroll_pause,
            checkpoint_cb=make_search_checkpoint_callback(run_dir, args.keyword),
        )
        context.close()

    if not items:
        print("Warning: No tweets were collected. Check your authentication and network connection.")
        return

    stage1_json_path = run_dir / "results_stage1.json"
    stage1_json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Stage 1 saved:    {stage1_json_path}")

    if not args.skip_fulltext:
        from tweet_fulltext import hydrate_items_with_fulltext

        print("Stage 2: hydrating full text from tweet detail pages...")
        with sync_playwright() as p:
            context = create_context(p, args.state, args.headless)
            items = hydrate_items_with_fulltext(
                context=context,
                items=items,
                run_dir=run_dir,
                checkpoint_every=args.fulltext_checkpoint_every,
                delay_ms=args.fulltext_delay_ms,
                logger=print,
            )
            context.close()

    summary = summarize(items, args.keyword)

    json_path = run_dir / "results.json"
    csv_path = run_dir / "results.csv"
    summary_json = run_dir / "summary.json"
    summary_md = run_dir / "summary.md"
    article_html = run_dir / "article.html"

    checkpoint_search_outputs(run_dir, items, args.keyword)

    print("=" * 60)
    print(f"Done. Collected {len(items)} tweets.")
    print(f"Results JSON:   {json_path}")
    print(f"Results CSV:    {csv_path}")
    print(f"Summary JSON:   {summary_json}")
    print(f"Summary Markdown: {summary_md}")
    print(f"Summary Article:  {article_html}")
    print("=" * 60)


if __name__ == "__main__":
    main()
