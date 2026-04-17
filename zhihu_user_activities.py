#!/usr/bin/env python3
"""Crawl all activities of a Zhihu user: answers, articles, pins, votes, likes, collections."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib import parse as urlparse
from urllib import request as urlrequest

from playwright.sync_api import TimeoutError, sync_playwright

from browser_config import get_browser_args, get_playwright_launch_kwargs
from zhihu_question_answers import (
    DEFAULT_USER_AGENT,
    click_expand_buttons,
    cookie_header_from_string,
    detect_risk_or_login,
    extract_answer,
    first_text,
    is_retryable_navigation_error,
    longest_text,
    parse_cookie_string,
)

USER_URL_RE = re.compile(r"^https?://www\.zhihu\.com/people/([^/]+)(?:[/?#].*)?$")
ANSWER_URL_RE = re.compile(r"https?://www\.zhihu\.com/question/(\d+)/answer/(\d+)")
ARTICLE_URL_RE = re.compile(r"https?://zhuanlan\.zhihu\.com/p/(\d+)")
PIN_URL_RE = re.compile(r"https?://www\.zhihu\.com/pin/(\d+)")
VIDEO_URL_RE = re.compile(r"https?://www\.zhihu\.com/video/(\d+)")
EXPECTED_TYPES = {
    "answers": {"answer"},
    "posts": {"article"},
    "pins": {"pin"},
    "videos": {"video"},
    "votes": {"answer", "article", "pin", "video"},
    "likes": {"answer", "article", "pin", "video"},
    "collections": {"answer", "article", "pin", "video"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl all activities of a Zhihu user: answers, articles, pins, votes, likes, collections."
    )
    parser.add_argument("--user-url", required=True, help="Zhihu user URL, e.g. https://www.zhihu.com/people/youkaichao")
    parser.add_argument("--cookie", required=True, help="Cookie header copied from the browser request")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Optional browser user agent")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--out-dir", default="output", help="Output base directory")
    parser.add_argument("--max-scrolls", type=int, default=100, help="Max scroll rounds on each activity tab")
    parser.add_argument("--no-new-stop", type=int, default=6, help="Stop after N rounds without new items")
    parser.add_argument("--page-delay-ms", type=int, default=1800, help="Wait time after each page action in ms")
    return parser.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:60] or "zhihu_user"


def ensure_user_url(url: str) -> Tuple[str, str]:
    clean = str(url or "").strip()
    match = USER_URL_RE.match(clean)
    if not match:
        raise ValueError("用户链接格式不正确，必须是 https://www.zhihu.com/people/<user_id>")
    return clean.rstrip("/"), match.group(1)


def normalize_href(href: str) -> str:
    clean = str(href or "").strip()
    if not clean:
        return ""
    if clean.startswith("//"):
        clean = "https:" + clean
    elif clean.startswith("/"):
        clean = "https://www.zhihu.com" + clean
    return clean.split("?")[0].split("#")[0]


def detect_item_type(href: str) -> str:
    if "/answer/" in href:
        return "answer"
    if "zhuanlan.zhihu.com/p/" in href:
        return "article"
    if "/pin/" in href:
        return "pin"
    if "/video/" in href:
        return "video"
    return "unknown"


def create_run_dir(base_dir: Path, user_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_dir / f"zhihu_user_{safe_name(user_id)}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def load_user_profile(page) -> Dict:
    """Load user profile information."""
    profile = {}
    try:
        name_elem = page.query_selector('h1[class*="ProfileHeader-title"]')
        if name_elem:
            profile["name"] = name_elem.inner_text().strip()
    except Exception:
        pass

    try:
        desc_elem = page.query_selector('div[class*="ProfileHeader-description"]')
        if desc_elem:
            profile["description"] = desc_elem.inner_text().strip()
    except Exception:
        pass

    try:
        stats = {}
        for stat in page.query_selector_all('li[class*="ProfileFollowCard"]'):
            try:
                num = stat.query_selector('span[class*="Counter"]')
                label = stat.query_selector('span[class*="Label"]')
                if num and label:
                    stats[label.inner_text().strip()] = num.inner_text().strip()
            except Exception:
                pass
        profile["stats"] = stats
    except Exception:
        pass

    return profile


def collect_items_from_tab(page, tab_type: str) -> List[Dict]:
    """Collect item links from a user's activity tab."""
    items = []
    seen_urls: Set[str] = set()

    no_new_count = 0
    max_scrolls = 100
    no_new_stop = 6
    allowed_types = EXPECTED_TYPES.get(tab_type, {"answer", "article", "pin", "video"})

    for scroll_idx in range(max_scrolls):
        time.sleep(1.5)

        try:
            page.evaluate("window.scrollBy(0, 800)")
        except Exception:
            pass

        time.sleep(1.2)

        try:
            selectors = [
                'a[href*="/question/"]',
                'a[href*="/answer/"]',
                'a[href*="zhuanlan.zhihu.com"]',
                'a[href*="/pin/"]',
                'a[href*="/video/"]',
            ]
            links = []
            for sel in selectors:
                links.extend(page.query_selector_all(sel))

            new_count = 0
            for a in links:
                try:
                    href = normalize_href(a.get_attribute("href"))
                    if not href or href in seen_urls:
                        continue
                    if href in seen_urls:
                        continue
                    item_type = detect_item_type(href)
                    if item_type == "unknown" or item_type not in allowed_types:
                        continue
                    seen_urls.add(href)
                    items.append({"url": href, "type": item_type})
                    new_count += 1
                except Exception:
                    continue
        except Exception:
            pass

        if new_count == 0:
            no_new_count += 1
            if no_new_count >= no_new_stop:
                break
        else:
            no_new_count = 0

    return items


def fetch_full_content(
    page,
    url: str,
    item_type: str,
    cookie_header: str,
    user_agent: str,
    page_delay_ms: int,
) -> Dict:
    """Fetch full content of an item."""
    result = {
        "url": url,
        "type": item_type,
        "title": "",
        "content": "",
        "author": "",
        "created_at": "",
        "stats": {},
        "error": "",
    }

    try:
        url = normalize_href(url)
        result["url"] = url
        if item_type == "answer":
            answer = extract_answer(
                page,
                url,
                "",
                page_delay_ms,
                cookie_header=cookie_header,
                user_agent=user_agent,
            )
            result["title"] = answer.get("question_title", "")
            result["content"] = answer.get("content", "")
            result["author"] = answer.get("author", "")
            result["created_at"] = " | ".join(answer.get("times", []))
            if answer.get("vote_text"):
                result["stats"]["vote_text"] = answer["vote_text"]
            result["fetch_source"] = answer.get("fetch_source", "")
            return result

        last_error = None
        for attempt in range(1, 4):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(max(page_delay_ms / 1000.0, 1.2))
                break
            except Exception as exc:
                last_error = exc
                if not is_retryable_navigation_error(exc) or attempt >= 3:
                    raise
                time.sleep(attempt)

        if item_type == "article":
            click_expand_buttons(page)
            page.wait_for_timeout(500)
            result["title"] = first_text(page, ["h1.Post-Title", "h1.ArticleItem-Title", "h1"])
            result["author"] = first_text(page, [".AuthorInfo-name", ".Post-Author .UserLink-link", "a[href*='/people/']"])
            result["content"] = longest_text(
                page,
                [
                    ".Post-RichTextContainer",
                    ".RichText.ztext",
                    ".Article-content",
                    "article",
                    "main",
                ],
            )
            try:
                time_elem = page.query_selector("time")
                if time_elem:
                    result["created_at"] = time_elem.get_attribute("datetime") or time_elem.inner_text().strip()
            except Exception:
                pass
            result["fetch_source"] = "page"

        elif item_type == "pin":
            try:
                content_elem = page.query_selector('div[class*="PinContent"]')
                if content_elem:
                    result["content"] = content_elem.inner_text().strip()
            except Exception:
                pass
            result["fetch_source"] = "page"

        elif item_type == "video":
            try:
                title_elem = page.query_selector('h1[class*="Video"]')
                if title_elem:
                    result["title"] = title_elem.inner_text().strip()
            except Exception:
                pass

            try:
                desc_elem = page.query_selector('div[class*="VideoDescription"]')
                if desc_elem:
                    result["content"] = desc_elem.inner_text().strip()
            except Exception:
                pass
            result["fetch_source"] = "page"

    except Exception as e:
        result["error"] = str(e)

    return result


def generate_summary_files(run_dir: Path, profile: Dict, full_contents: List[Dict]) -> None:
    counts: Dict[str, int] = {}
    for item in full_contents:
        key = item.get("activity_type", item.get("type", "unknown"))
        counts[key] = counts.get(key, 0) + 1

    sections = []
    for key, label in [
        ("answers", "回答"),
        ("posts", "文章"),
        ("pins", "想法"),
        ("videos", "视频"),
        ("votes", "赞同"),
        ("likes", "喜欢"),
        ("collections", "收藏"),
    ]:
        matched = [item for item in full_contents if item.get("activity_type") == key]
        if not matched:
            continue
        sections.append(f"<section><h2>{html.escape(label)} ({len(matched)})</h2><ul>")
        for item in matched[:80]:
            title = item.get("title") or item.get("content") or item.get("url") or "(无标题)"
            title = re.sub(r"\s+", " ", str(title)).strip()[:120]
            sections.append(
                f'<li><a href="{html.escape(item.get("url", ""))}" target="_blank" rel="noreferrer">{html.escape(title)}</a></li>'
            )
        sections.append("</ul></section>")

    summary_lines = [
        f"# 知乎用户动态抓取结果 - {profile.get('name') or run_dir.name}",
        "",
        f"- 输出目录: {run_dir.name}",
        f"- 回答: {counts.get('answers', 0)}",
        f"- 文章: {counts.get('posts', 0)}",
        f"- 想法: {counts.get('pins', 0)}",
        f"- 视频: {counts.get('videos', 0)}",
        f"- 赞同: {counts.get('votes', 0)}",
        f"- 喜欢: {counts.get('likes', 0)}",
        f"- 收藏: {counts.get('collections', 0)}",
        "",
    ]
    (run_dir / "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")

    article = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(profile.get('name') or run_dir.name)} - 知乎用户动态</title>
  <style>
    body {{ margin: 0; font-family: "IBM Plex Sans","PingFang SC","Noto Sans SC",sans-serif; background: #f5f1ea; color: #1d1a16; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 28px 18px 48px; }}
    .hero, section {{ background: rgba(255,255,255,0.92); border: 1px solid #d8cebf; border-radius: 20px; padding: 18px 20px; box-shadow: 0 14px 36px rgba(39,28,20,0.08); margin-bottom: 14px; }}
    h1, h2 {{ margin: 0 0 10px; }}
    p, li {{ line-height: 1.7; }}
    ul {{ margin: 0; padding-left: 20px; }}
    a {{ color: #0f766e; }}
    .meta {{ color: #5b5348; }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>{html.escape(profile.get('name') or run_dir.name)}</h1>
      <p class="meta">{html.escape(profile.get('description') or '')}</p>
      <p class="meta">回答 {counts.get('answers', 0)} · 文章 {counts.get('posts', 0)} · 想法 {counts.get('pins', 0)} · 视频 {counts.get('videos', 0)}</p>
    </section>
    {''.join(sections) if sections else '<section><p>本次没有抓到可展示内容。</p></section>'}
  </main>
</body>
</html>"""
    (run_dir / "article.html").write_text(article, encoding="utf-8")


def crawl_user_activities(
    user_url: str,
    cookie: str,
    user_agent: str,
    headless: bool,
    out_dir: Path,
    page_delay_ms: int,
):
    """Main function to crawl all user activities."""
    user_url, user_id = ensure_user_url(user_url)
    run_dir = create_run_dir(out_dir, user_id)

    print(f"目标用户: {user_id}")
    print(f"输出目录: {run_dir}")

    cookie_header = cookie_header_from_string(cookie)
    cookies = parse_cookie_string(cookie)
    browser_args = get_browser_args()

    activities = {
        "answers": [],
        "articles": [],
        "pins": [],
        "videos": [],
        "votes": [],
        "likes": [],
        "collections": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(**get_playwright_launch_kwargs(headless=headless, args=browser_args))
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        context.add_cookies(cookies)

        page = context.new_page()
        page.set_default_timeout(45000)

        print(f"\n访问用户主页: {user_url}")
        try:
            page.goto(user_url, wait_until="networkidle", timeout=60000)
            time.sleep(3)
            detect_risk_or_login(page)
        except Exception as e:
            raise RuntimeError(f"访问用户主页失败: {e}") from e

        profile = load_user_profile(page)
        print(f"\n用户信息: {profile}")

        profile_file = run_dir / "profile.json"
        profile_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

        tabs = [
            ("answers", "回答"),
            ("posts", "文章"),
            ("pins", "想法"),
            ("videos", "视频"),
        ]

        for tab_key, tab_name in tabs:
            print(f"\n\n=== 正在爬取 {tab_name} ===")
            try:
                tab_url = f"{user_url}/{tab_key}"
                page.goto(tab_url, wait_until="networkidle", timeout=60000)
                time.sleep(3)

                items = collect_items_from_tab(page, tab_key)
                print(f"找到 {len(items)} 个 {tab_name} 链接")
                if tab_key == "posts":
                    activities["articles"] = items
                else:
                    activities[tab_key] = items
            except Exception as e:
                print(f"爬取 {tab_name} 失败: {e}")

        print(f"\n\n=== 正在爬取点赞、喜欢、收藏 ===")
        actions = [
            ("votes", "赞同"),
            ("likes", "喜欢"),
            ("collections", "收藏"),
        ]

        for action_key, action_name in actions:
            try:
                actions_url = f"{user_url}/{action_key}"
                page.goto(actions_url, wait_until="networkidle", timeout=60000)
                time.sleep(3)

                items = collect_items_from_tab(page, action_key)
                print(f"找到 {len(items)} 个 {action_name} 链接")

                activities[action_key] = items
            except Exception as e:
                print(f"爬取 {action_name} 失败: {e}")

        browser.close()

    links_file = run_dir / "activity_links.json"
    links_file.write_text(json.dumps(activities, ensure_ascii=False, indent=2), encoding="utf-8")

    all_items = []
    for key, items in activities.items():
        for item in items:
            item["activity_type"] = key
            all_items.append(item)

    print(f"\n\n=== 共收集 {len(all_items)} 条动态链接 ===")

    if all_items:
        print("\n开始获取完整内容...")

        with sync_playwright() as p:
            browser = p.chromium.launch(**get_playwright_launch_kwargs(headless=headless, args=browser_args))
            context = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            context.add_cookies(cookies)
            page = context.new_page()
            page.set_default_timeout(45000)

            full_contents = []
            total = len(all_items)

            for idx, item in enumerate(all_items):
                if idx % 10 == 0:
                    print(f"进度: {idx+1}/{total}")

                content = fetch_full_content(
                    page,
                    item["url"],
                    item["type"],
                    cookie_header=cookie_header,
                    user_agent=user_agent,
                    page_delay_ms=page_delay_ms,
                )
                content["activity_type"] = item["activity_type"]
                full_contents.append(content)

                time.sleep(1)

            browser.close()

        contents_file = run_dir / "full_contents.json"
        contents_file.write_text(json.dumps(full_contents, ensure_ascii=False, indent=2), encoding="utf-8")

        csv_file = run_dir / "activities.csv"
        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["URL", "类型", "动态类型", "标题", "作者", "内容", "创建时间", "统计", "抓取来源", "错误"])
            for item in full_contents:
                writer.writerow([
                    item.get("url", ""),
                    item.get("type", ""),
                    item.get("activity_type", ""),
                    item.get("title", ""),
                    item.get("author", ""),
                    item.get("content", ""),
                    item.get("created_at", ""),
                    json.dumps(item.get("stats", {}), ensure_ascii=False),
                    item.get("fetch_source", ""),
                    item.get("error", ""),
                ])

        generate_summary_files(run_dir, profile, full_contents)

        print(f"\n完成! 结果保存在: {run_dir}")


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    crawl_user_activities(
        args.user_url,
        args.cookie,
        args.user_agent,
        args.headless,
        out_dir,
        args.page_delay_ms,
    )


if __name__ == "__main__":
    main()
