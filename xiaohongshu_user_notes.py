#!/usr/bin/env python3
"""Crawl Xiaohongshu user notes, with optional detail and comments hydration."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from playwright.sync_api import TimeoutError, sync_playwright

from browser_config import get_browser_args

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

PROFILE_URL_RE = re.compile(r"^https?://www\.xiaohongshu\.com/user/profile/([A-Za-z0-9]+)(?:[/?#].*)?$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl Xiaohongshu user notes.")
    parser.add_argument("--user-url", required=True, help="Xiaohongshu profile URL")
    parser.add_argument("--cookie", default="", help="Optional Cookie header for logged-in detail/comment crawling")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Optional browser user agent")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--out-dir", default="output", help="Output base directory")
    parser.add_argument("--max-scrolls", type=int, default=160, help="Max scroll rounds on profile page")
    parser.add_argument("--no-new-stop", type=int, default=8, help="Stop after N rounds without new cards")
    parser.add_argument("--page-delay-ms", type=int, default=1500, help="Wait time after each page action in ms")
    parser.add_argument("--detail-delay-ms", type=int, default=1500, help="Wait time after opening each detail modal in ms")
    parser.add_argument("--comment-scrolls", type=int, default=40, help="Max comment scroll rounds per note")
    return parser.parse_args()


def ensure_profile_url(url: str) -> Tuple[str, str]:
    clean = str(url or "").strip()
    match = PROFILE_URL_RE.match(clean)
    if not match:
        raise ValueError("链接格式不正确，必须是 https://www.xiaohongshu.com/user/profile/<id>")
    return clean, match.group(1)


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:60] or "xiaohongshu"


def create_run_dir(base_dir: Path, profile_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_dir / f"xiaohongshu_user_{profile_id}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def parse_cookie_string(cookie_string: str) -> List[Dict]:
    cookies: List[Dict] = []
    for chunk in str(cookie_string or "").split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".xiaohongshu.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def first_text(page, selectors: Iterable[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for idx in range(min(count, 6)):
            try:
                text = (locator.nth(idx).inner_text(timeout=1000) or "").strip()
            except Exception:
                continue
            if text:
                return text
    return ""


def longest_text(page, selectors: Iterable[str]) -> str:
    best = ""
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for idx in range(min(count, 10)):
            try:
                text = (locator.nth(idx).inner_text(timeout=1000) or "").strip()
            except Exception:
                continue
            if len(text) > len(best):
                best = text
    return best


def detect_login_overlay(page) -> bool:
    body_text = (page.locator("body").inner_text(timeout=3000) or "").strip()
    markers = ["登录即可查看 Ta 的笔记", "手机号登录", "小红书如何扫码"]
    return any(marker in body_text for marker in markers)


def extract_profile_meta(page) -> Dict:
    body_text = page.locator("body").inner_text(timeout=3000) or ""
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    user_name = ""
    user_id = ""
    bio = ""
    ip_location = ""
    followers = ""
    likes_and_collects = ""
    for idx, line in enumerate(lines):
        if not user_name and "小红书号：" in line and idx > 0:
            user_name = lines[idx - 1]
            user_id = line.replace("小红书号：", "").strip()
        if line.startswith("IP属地："):
            ip_location = line.replace("IP属地：", "").strip()
        if not bio and line.startswith("QR："):
            bio = line.replace("QR：", "").strip()
        if line.endswith("粉丝"):
            followers = line
        if "获赞与收藏" in line:
            likes_and_collects = line
    return {
        "user_name": user_name,
        "user_id": user_id,
        "bio": bio,
        "ip_location": ip_location,
        "followers": followers,
        "likes_and_collects": likes_and_collects,
    }


def collect_notes(page) -> List[Dict]:
    items = page.evaluate(
        """() => {
          const notes = Array.from(document.querySelectorAll('section.note-item'));
          return notes.map((el, index) => {
            const titleEl = el.querySelector('.title');
            const authorEl = el.querySelector('.name');
            const likeEl = el.querySelector('.count');
            const coverEl = el.querySelector('a.cover img');
            return {
              index,
              title: (titleEl?.textContent || '').trim(),
              author: (authorEl?.textContent || '').trim(),
              like_count: (likeEl?.textContent || '').trim(),
              cover_image: coverEl?.src || '',
              card_text: (el.textContent || '').replace(/\\s+/g, ' ').trim(),
              width: el.getAttribute('data-width') || '',
              height: el.getAttribute('data-height') || '',
            };
          });
        }"""
    )
    normalized: List[Dict] = []
    for item in items:
        title = str(item.get("title", "")).strip()
        cover = str(item.get("cover_image", "")).strip()
        normalized.append(
            {
                "title": title,
                "author": str(item.get("author", "")).strip(),
                "like_count": str(item.get("like_count", "")).strip(),
                "cover_image": cover,
                "card_text": str(item.get("card_text", "")).strip(),
                "width": str(item.get("width", "")).strip(),
                "height": str(item.get("height", "")).strip(),
            }
        )
    return normalized


def scroll_profile(page, max_scrolls: int, no_new_stop: int, page_delay_ms: int) -> List[Dict]:
    seen: Dict[Tuple[str, str], Dict] = {}
    no_new_rounds = 0
    current_height = 0
    for round_no in range(1, max_scrolls + 1):
        current_notes = collect_notes(page)
        new_count = 0
        for item in current_notes:
            key = (item.get("title", ""), item.get("cover_image", ""))
            if key in seen:
                continue
            seen[key] = item
            new_count += 1
        print(f"Page {round_no}: + {new_count} new, total {len(seen)}")
        if new_count:
            no_new_rounds = 0
        else:
            no_new_rounds += 1
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(page_delay_ms)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == current_height and no_new_rounds >= no_new_stop:
            break
        current_height = new_height
    return list(seen.values())


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_progress(run_dir: Path, total: int, processed: int, hydrated: int, failed: int) -> None:
    write_json(
        run_dir / "fulltext_progress.json",
        {
            "total": total,
            "processed": processed,
            "hydrated": hydrated,
            "failed": failed,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def open_note_modal(page, note: Dict, page_delay_ms: int) -> bool:
    title = note.get("title", "")
    locator = page.locator("section.note-item")
    count = locator.count()
    for idx in range(count):
        card = locator.nth(idx)
        try:
            card.scroll_into_view_if_needed(timeout=1000)
            page.wait_for_timeout(150)
            card_title = (card.locator(".title").inner_text(timeout=800) or "").strip()
        except Exception:
            continue
        if title and card_title != title:
            continue
        try:
            card.click(timeout=1500)
            page.wait_for_timeout(page_delay_ms)
            return True
        except Exception:
            continue
    return False


def close_note_modal(page) -> None:
    selectors = [
        'div.close-circle',
        'button.close',
        '[class*="close"]',
        'svg[class*="close"]',
    ]
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count():
            try:
                locator.first.click(timeout=1000)
                page.wait_for_timeout(400)
                return
            except Exception:
                continue
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


def extract_images(page) -> List[str]:
    urls = page.evaluate(
        """() => {
          const root = document.querySelector('.note-detail-mask') || document;
          return Array.from(root.querySelectorAll('img'))
          .map(img => img.currentSrc || img.src || '')
          .filter(Boolean)
          .filter(src => /xhscdn\\.com/.test(src))
        }"""
    )
    seen = []
    for url in urls:
        if url in seen:
            continue
        seen.append(url)
    return seen


def extract_comments(page, max_scrolls: int) -> List[Dict]:
    comments: Dict[Tuple[str, str], Dict] = {}
    container = page.locator(".comments-container")
    if not container.count():
        return []

    for _ in range(max_scrolls):
        rows = page.evaluate(
            """() => {
              const root = document.querySelector('.comments-container') || document;
              const blocks = Array.from(root.querySelectorAll('.comment-item'));
              return blocks.map(el => {
                const author = el.querySelector('.name')?.textContent || el.querySelector('[class*="author"]')?.textContent || '';
                const content = el.querySelector('.content')?.textContent || '';
                const time = el.querySelector('.date')?.textContent || el.querySelector('[class*="time"]')?.textContent || '';
                return {
                  author: author.replace(/\\s+/g, ' ').trim(),
                  content: content.replace(/\\s+/g, ' ').trim(),
                  time: time.replace(/\\s+/g, ' ').trim(),
                };
              }).filter(item => item.content);
            }"""
        )
        new_count = 0
        for row in rows:
            key = (row.get("author", ""), row.get("content", ""))
            if key in comments:
                continue
            comments[key] = row
            new_count += 1
        try:
            container.first.evaluate("(el) => el.scrollBy(0, el.clientHeight * 0.9)")
        except Exception:
            page.mouse.wheel(0, 2200)
        page.wait_for_timeout(700)
        if new_count == 0:
            break
    return list(comments.values())


def extract_note_detail(page, note: Dict, page_delay_ms: int, comment_scrolls: int) -> Dict:
    if detect_login_overlay(page):
        raise RuntimeError("详情页出现登录弹层，需要有效的小红书 Cookie。")

    title = first_text(page, [".note-content .title", ".note-scroller .title", ".note-content"]) or note.get("title", "")
    content = first_text(
        page,
        [
            ".note-content .desc",
            ".note-scroller .desc",
            ".note-content",
            ".note-scroller",
        ],
    )
    if content and title and content.startswith(title):
        content = content[len(title):].strip()
    images = extract_images(page)
    comments = extract_comments(page, comment_scrolls)
    like_text = first_text(page, [".note-detail-mask .like-wrapper .count", ".interact-container .count"])
    publish_time = first_text(page, [".note-content .date", ".note-scroller .date"])
    page.wait_for_timeout(page_delay_ms)
    return {
        **note,
        "detail_title": title,
        "content": content,
        "images": images,
        "image_count": len(images),
        "comments": comments,
        "comment_count": len(comments),
        "detail_like_text": like_text,
        "publish_time": publish_time,
        "hydrated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_stage1(run_dir: Path, profile_url: str, profile_id: str, meta: Dict, notes: List[Dict]) -> None:
    write_json(
        run_dir / "results_stage1.json",
        {
            "profile_url": profile_url,
            "profile_id": profile_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "profile": meta,
            "items": notes,
        },
    )


def write_csv(path: Path, rows: List[Dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "title",
                "detail_title",
                "author",
                "like_count",
                "detail_like_text",
                "publish_time",
                "image_count",
                "comment_count",
                "cover_image",
                "card_text",
                "content",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "title": row.get("title", ""),
                    "detail_title": row.get("detail_title", ""),
                    "author": row.get("author", ""),
                    "like_count": row.get("like_count", ""),
                    "detail_like_text": row.get("detail_like_text", ""),
                    "publish_time": row.get("publish_time", ""),
                    "image_count": row.get("image_count", 0),
                    "comment_count": row.get("comment_count", 0),
                    "cover_image": row.get("cover_image", ""),
                    "card_text": row.get("card_text", ""),
                    "content": row.get("content", ""),
                }
            )


def write_markdown(path: Path, meta: Dict, profile_url: str, rows: List[Dict]) -> None:
    parts = [
        f"# 小红书博主笔记 - {meta.get('user_name') or meta.get('user_id') or 'unknown'}",
        "",
        f"- 主页链接: {profile_url}",
        f"- 小红书号: {meta.get('user_id', '') or '-'}",
        f"- IP 属地: {meta.get('ip_location', '') or '-'}",
        f"- 粉丝: {meta.get('followers', '') or '-'}",
        f"- 获赞与收藏: {meta.get('likes_and_collects', '') or '-'}",
        f"- 笔记数量: {len(rows)}",
        "",
    ]
    if meta.get("bio"):
        parts.extend([f"- 简介: {meta['bio']}", ""])
    for index, row in enumerate(rows, start=1):
        parts.extend(
            [
                f"## {index}. {row.get('detail_title') or row.get('title') or f'笔记 {index}'}",
                "",
                f"- 作者: {row.get('author', '') or '-'}",
                f"- 点赞: {row.get('detail_like_text') or row.get('like_count') or '-'}",
                f"- 发布时间: {row.get('publish_time', '') or '-'}",
                f"- 图片数量: {row.get('image_count', 0)}",
                f"- 评论数量: {row.get('comment_count', 0)}",
                "",
                row.get("content") or row.get("card_text", ""),
                "",
            ]
        )
        images = row.get("images", [])
        if images:
            parts.append("### 图片")
            parts.append("")
            for image in images:
                parts.append(f"- {image}")
            parts.append("")
        comments = row.get("comments", [])
        if comments:
            parts.append("### 评论")
            parts.append("")
            for comment in comments:
                author = comment.get("author", "") or "匿名"
                time_text = comment.get("time", "") or "-"
                content = comment.get("content", "")
                parts.append(f"- {author} | {time_text} | {content}")
            parts.append("")
    path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")


def build_html(meta: Dict, profile_url: str, rows: List[Dict]) -> str:
    cards = []
    for index, row in enumerate(rows, start=1):
        title = row.get("detail_title") or row.get("title") or f"笔记 {index}"
        image_html = "".join(
            f'<img src="{html.escape(src)}" alt="" />' for src in row.get("images", [])[:8]
        ) or (f'<img src="{html.escape(row.get("cover_image", ""))}" alt="" />' if row.get("cover_image") else "")
        comments_html = "".join(
            f'<li><strong>{html.escape(comment.get("author", "") or "匿名")}</strong> {html.escape(comment.get("time", "") or "")} {html.escape(comment.get("content", ""))}</li>'
            for comment in row.get("comments", [])[:20]
        )
        cards.append(
            f"""
            <article class="card">
              <div class="index">{index}</div>
              <h2>{html.escape(title)}</h2>
              <div class="meta">{html.escape((row.get('author') or '-') + ' | 点赞 ' + (row.get('detail_like_text') or row.get('like_count') or '-'))}</div>
              <p>{html.escape(row.get('content') or row.get('card_text', ''))}</p>
              <div class="gallery">{image_html}</div>
              <div class="meta">评论 {row.get('comment_count', 0)} 条</div>
              <ul class="comments">{comments_html}</ul>
            </article>
            """
        )
    title = meta.get("user_name") or meta.get("user_id") or "小红书博主"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)} - 小红书笔记</title>
  <style>
    :root {{
      --bg: #f7f1ea;
      --card: rgba(255,255,255,0.92);
      --line: #ddcfbf;
      --ink: #1f1813;
      --muted: #6f645a;
      --accent: #c84f37;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "IBM Plex Sans","PingFang SC","Noto Sans SC",sans-serif; color: var(--ink); background: radial-gradient(900px 420px at 105% -10%, rgba(200,79,55,0.14), transparent 60%), radial-gradient(960px 440px at -5% 0%, rgba(15,118,110,0.10), transparent 58%), var(--bg); }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 32px 18px 60px; }}
    .hero, .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 24px; box-shadow: 0 18px 44px rgba(39,28,20,0.08); }}
    .hero {{ padding: 24px; margin-bottom: 18px; }}
    .hero h1 {{ margin: 0 0 10px; font-family: "Source Han Serif SC","Noto Serif CJK SC",serif; font-size: clamp(2rem, 4vw, 3rem); }}
    .hero p {{ margin: 6px 0; color: var(--muted); }}
    .list {{ display: grid; gap: 14px; }}
    .card {{ padding: 16px; position: relative; }}
    .index {{ position: absolute; top: 12px; right: 16px; color: rgba(200,79,55,0.18); font-size: 2rem; font-weight: 700; }}
    h2 {{ margin: 0 0 8px; font-size: 1.1rem; line-height: 1.5; }}
    .meta {{ color: var(--muted); font-size: 0.9rem; margin: 8px 0 10px; }}
    p {{ margin: 0 0 12px; line-height: 1.8; color: #342b24; white-space: pre-wrap; }}
    .gallery {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 12px 0; }}
    .gallery img {{ width: 100%; height: 180px; object-fit: cover; border-radius: 14px; background: #efe6db; }}
    .comments {{ margin: 0; padding-left: 20px; line-height: 1.7; color: #3b3129; }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <h1>{html.escape(title)}</h1>
      <p>主页链接: <a href="{html.escape(profile_url)}" target="_blank" rel="noreferrer">{html.escape(profile_url)}</a></p>
      <p>小红书号: {html.escape(meta.get('user_id', '') or '-')} | 粉丝: {html.escape(meta.get('followers', '') or '-')} | 获赞与收藏: {html.escape(meta.get('likes_and_collects', '') or '-')}</p>
      <p>当前版本先抓全部公开卡片，再在已登录时逐条补全文、全部图片和评论。</p>
    </section>
    <section class="list">{''.join(cards)}</section>
  </main>
</body>
</html>"""


def write_outputs(run_dir: Path, meta: Dict, profile_url: str, rows: List[Dict]) -> None:
    write_json(
        run_dir / "results.json",
        {
            "profile_url": profile_url,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "profile": meta,
            "items": rows,
        },
    )
    write_json(
        run_dir / "comments.json",
        {
            "profile_url": profile_url,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "items": [
                {
                    "title": row.get("detail_title") or row.get("title", ""),
                    "comment_count": row.get("comment_count", 0),
                    "comments": row.get("comments", []),
                }
                for row in rows
            ],
        },
    )
    write_csv(run_dir / "results.csv", rows)
    write_markdown(run_dir / "all_notes.md", meta, profile_url, rows)
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                f"# 小红书博主笔记 - {meta.get('user_name') or meta.get('user_id') or 'unknown'}",
                "",
                f"- 主页链接: {profile_url}",
                f"- 笔记数量: {len(rows)}",
                f"- 已补全文笔记: {sum(1 for row in rows if row.get('content'))}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "article.html").write_text(build_html(meta, profile_url, rows), encoding="utf-8")


def hydrate_details(
    context,
    profile_url: str,
    stage1_notes: List[Dict],
    page_delay_ms: int,
    comment_scrolls: int,
    run_dir: Path,
    meta: Dict,
) -> Tuple[List[Dict], List[Dict]]:
    hydrated: List[Dict] = []
    failures: List[Dict] = []
    total = len(stage1_notes)
    write_progress(run_dir, total, 0, 0, 0)

    for index, note in enumerate(stage1_notes, start=1):
        detail_page = context.new_page()
        try:
            detail_page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
            detail_page.wait_for_timeout(page_delay_ms)
            if not open_note_modal(detail_page, note, page_delay_ms):
                failures.append({**note, "error": "未找到对应卡片，无法打开详情"})
                write_progress(run_dir, total, index, len(hydrated), len(failures))
                detail_page.close()
                continue
            print(f"[FULLTEXT] {index}/{total} {note.get('title', '')}")
            hydrated_note = extract_note_detail(detail_page, note, page_delay_ms, comment_scrolls)
            hydrated.append(hydrated_note)
        except Exception as exc:
            failures.append({**note, "error": str(exc)})
        finally:
            try:
                close_note_modal(detail_page)
            except Exception:
                pass
            detail_page.close()
        write_progress(run_dir, total, index, len(hydrated), len(failures))
        write_outputs(run_dir, meta, profile_url, hydrated)
    return hydrated, failures


def main() -> int:
    args = parse_args()
    profile_url, profile_id = ensure_profile_url(args.user_url)
    output_base = Path(args.out_dir)
    output_base.mkdir(parents=True, exist_ok=True)
    run_dir = create_run_dir(output_base, profile_id)
    print(f"Run directory: {run_dir.resolve()}")
    print(f"Profile URL: {profile_url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=get_browser_args())
        context = browser.new_context(
            user_agent=args.user_agent,
            viewport={"width": 1440, "height": 1100},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        cookies = parse_cookie_string(args.cookie)
        if cookies:
            context.add_cookies(cookies)

        page = context.new_page()
        page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(args.page_delay_ms)

        meta = extract_profile_meta(page)
        print(f"User name: {meta.get('user_name', '')}")
        stage1_notes = scroll_profile(page, args.max_scrolls, args.no_new_stop, args.page_delay_ms)
        if not stage1_notes:
            raise RuntimeError("没有抓到任何笔记卡片。请检查主页链接是否有效。")
        print(f"成功收集 {len(stage1_notes)} 条小红书笔记")
        write_stage1(run_dir, profile_url, profile_id, meta, stage1_notes)

        if not cookies:
            print("[SYSTEM] 未提供小红书 Cookie，仅保存公开卡片摘要。若要抓正文、全部图片和评论，请在控制台中填写 Cookie。")
            write_outputs(run_dir, meta, profile_url, stage1_notes)
            write_json(run_dir / "failed_details.json", [])
        else:
            print("开始第二阶段：逐条补全小红书正文与评论")
            hydrated, failures = hydrate_details(
                context,
                profile_url,
                stage1_notes,
                args.detail_delay_ms,
                args.comment_scrolls,
                run_dir,
                meta,
            )
            write_outputs(run_dir, meta, profile_url, hydrated)
            write_json(run_dir / "failed_details.json", failures)
            print(f"补全文完成: 成功 {len(hydrated)} 条, 失败 {len(failures)} 条")

        page.close()
        context.close()
        browser.close()

    print(f"Results stage1: {run_dir / 'results_stage1.json'}")
    print(f"Results JSON: {run_dir / 'results.json'}")
    print(f"Comments JSON: {run_dir / 'comments.json'}")
    print(f"Article HTML: {run_dir / 'article.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
