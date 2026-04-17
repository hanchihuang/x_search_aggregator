#!/usr/bin/env python3
"""Crawl a specific X post and all visible comments by scrolling the detail page."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from playwright.sync_api import Page, sync_playwright

from search_x import (
    STATUS_PATH_RE,
    close_context,
    create_context,
    extract_tweet,
    get_cards,
    get_last_visible_anchor,
    scroll_feed,
    validate_auth_state,
)
from tweet_fulltext import extract_full_text_from_page

EXPAND_REPLY_SELECTORS = [
    'button:has-text("Show more replies")',
    'div[role="button"]:has-text("Show more replies")',
    'button:has-text("显示更多回复")',
    'div[role="button"]:has-text("显示更多回复")',
    'button:has-text("Show")',
    'div[role="button"]:has-text("Show")',
    'button:has-text("显示")',
    'div[role="button"]:has-text("显示")',
    'button:has-text("Show probable spam")',
    'div[role="button"]:has-text("Show probable spam")',
    'button:has-text("显示可能包含垃圾信息的回复")',
    'div[role="button"]:has-text("显示可能包含垃圾信息的回复")',
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl one X post plus all loaded comments.")
    p.add_argument("--post-url", required=True, help="X post URL, e.g. https://x.com/karpathy/status/123")
    p.add_argument("--state", default="auth_state.json", help="Playwright storage state path")
    p.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    p.add_argument("--out-dir", default="output", help="Output base directory")
    p.add_argument("--run-dir", default="", help="Explicit run directory path")
    p.add_argument("--max-comments", type=int, default=0, help="Optional max comments to collect; 0 means no hard limit")
    p.add_argument("--max-scrolls", type=int, default=400, help="Max scroll rounds")
    p.add_argument("--no-new-stop", type=int, default=12, help="Stop after N rounds with no new comments")
    p.add_argument("--scroll-pause", type=int, default=1800, help="Pause between scrolls in ms")
    p.add_argument("--checkpoint-every", type=int, default=5, help="Write checkpoint every N scrolls")
    p.add_argument("--cdp-url", default="", help="Existing Chrome CDP endpoint, e.g. http://127.0.0.1:9222")
    p.add_argument("--auto-launch", action="store_true", help="Auto launch Chrome with remote debugging when CDP is unavailable")
    p.add_argument("--chrome-path", default="/usr/bin/google-chrome", help="Chrome executable path for auto-launch")
    p.add_argument("--user-data-dir", default="chrome_profile", help="Chrome profile dir used for auto-launch")
    p.add_argument("--wait-seconds", type=int, default=20, help="Seconds to wait for CDP readiness after auto-launch")
    return p.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text or "").strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:80] or "x_post"


def normalize_post_url(url: str) -> tuple[str, str, str]:
    raw = str(url or "").strip()
    match = STATUS_PATH_RE.search(raw)
    if not match:
        raise ValueError("帖子链接格式不正确，必须包含 /status/<tweet_id>。")
    handle = match.group(1)
    tweet_id = match.group(2)
    canonical = f"https://x.com/{handle}/status/{tweet_id}"
    return canonical, handle, tweet_id


def ensure_run_dir(args: argparse.Namespace, handle: str, tweet_id: str) -> Path:
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
    else:
        out_base = Path(args.out_dir).expanduser().resolve()
        run_dir = out_base / f"post_{safe_name(handle)}_{tweet_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def click_expand_reply_buttons(page: Page) -> int:
    clicked = 0
    for selector in EXPAND_REPLY_SELECTORS:
        for element in page.query_selector_all(selector)[:4]:
            try:
                element.click(timeout=1200)
                page.wait_for_timeout(800)
                clicked += 1
            except Exception:
                continue
    return clicked


def enrich_item(item: Dict, root_tweet_id: str, position: int, is_target_post: bool) -> Dict:
    enriched = dict(item)
    enriched["conversation_root_id"] = root_tweet_id
    enriched["position"] = position
    enriched["is_target_post"] = is_target_post
    return enriched


def write_comments_csv(path: Path, post: Optional[Dict], comments: List[Dict]) -> None:
    fields = [
        "tweet_id",
        "url",
        "user_name",
        "user_handle",
        "posted_at",
        "text",
        "reply_count",
        "retweet_count",
        "like_count",
        "bookmark_count",
        "view_count",
        "conversation_root_id",
        "position",
        "is_target_post",
    ]
    rows = []
    if post:
        rows.append(post)
    rows.extend(comments)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_summary(post: Optional[Dict], comments: List[Dict], source_url: str) -> Dict:
    top_liked = sorted(comments, key=lambda item: int(item.get("like_count", 0)), reverse=True)[:10]
    commenters = Counter()
    timestamps = []
    for item in comments:
        handle = str(item.get("user_handle") or "").strip()
        if handle:
            commenters[handle] += 1
        posted_at = str(item.get("posted_at") or "").strip()
        if posted_at:
            timestamps.append(posted_at)
    timestamps.sort()
    return {
        "source_url": source_url,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_post": {
            "tweet_id": post.get("tweet_id", "") if post else "",
            "url": post.get("url", source_url) if post else source_url,
            "user_handle": post.get("user_handle", "") if post else "",
            "posted_at": post.get("posted_at") if post else None,
            "text": post.get("text", "") if post else "",
        },
        "comment_count": len(comments),
        "time_range": {
            "from": timestamps[0] if timestamps else None,
            "to": timestamps[-1] if timestamps else None,
        },
        "top_commenters": [{"user_handle": handle, "count": count} for handle, count in commenters.most_common(20)],
        "top_liked_comments": [
            {
                "tweet_id": item.get("tweet_id", ""),
                "url": item.get("url", ""),
                "user_handle": item.get("user_handle", ""),
                "posted_at": item.get("posted_at"),
                "like_count": item.get("like_count", 0),
                "reply_count": item.get("reply_count", 0),
                "text": (item.get("text") or "")[:280],
            }
            for item in top_liked
        ],
    }


def write_summary_md(path: Path, summary: Dict) -> None:
    post = summary["target_post"]
    lines = [
        "# X 帖子评论抓取摘要",
        "",
        f"- 原帖链接: {summary['source_url']}",
        f"- 抓取时间(UTC): {summary['generated_at_utc']}",
        f"- 原帖作者: @{post.get('user_handle') or 'unknown'}",
        f"- 评论数量: {summary['comment_count']}",
        f"- 评论时间范围: {summary['time_range']['from']} ~ {summary['time_range']['to']}",
        "",
        "## 原帖正文",
        post.get("text") or "（未提取到正文）",
        "",
        "## 高频评论用户",
    ]
    for item in summary["top_commenters"][:15]:
        lines.append(f"- @{item['user_handle']}: {item['count']}")
    lines.extend(["", "## 点赞最多评论"])
    for idx, item in enumerate(summary["top_liked_comments"][:10], start=1):
        lines.append(f"{idx}. @{item['user_handle']} | likes={item['like_count']} | {item['url']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html_report(path: Path, post: Optional[Dict], comments: List[Dict], summary: Dict) -> None:
    post = post or {}
    comment_cards = []
    for idx, item in enumerate(comments, start=1):
        comment_cards.append(
            f"""
            <article class="card">
              <div class="meta">#{idx} · @{html.escape(item.get("user_handle") or "unknown")} · {html.escape(item.get("posted_at") or "未知时间")}</div>
              <div class="stats">👍 {int(item.get("like_count", 0))} · 回复 {int(item.get("reply_count", 0))} · RT {int(item.get("retweet_count", 0))}</div>
              <p>{html.escape(item.get("text") or "（无内容）").replace(chr(10), "<br/>")}</p>
              <a href="{html.escape(item.get("url") or "#")}" target="_blank" rel="noreferrer">打开评论</a>
            </article>
            """
        )
    top_commenters = "".join(
        f"<li>@{html.escape(item['user_handle'])}: {item['count']}</li>"
        for item in summary["top_commenters"][:12]
    ) or "<li>暂无</li>"
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>X 帖子评论抓取结果</title>
  <style>
    :root {{
      --bg: #f4efe7;
      --panel: rgba(255,255,255,0.9);
      --ink: #201a16;
      --muted: #655f57;
      --line: #ddd2c2;
      --accent: #0f766e;
    }}
    body {{ margin: 0; background: radial-gradient(circle at top, #fffdf9 0, #f8f1e6 30%, var(--bg) 100%); color: var(--ink); font-family: "IBM Plex Sans", "PingFang SC", sans-serif; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 32px 18px 72px; }}
    .panel, .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 10px 30px rgba(80, 62, 32, 0.08); }}
    .panel {{ padding: 22px; margin-bottom: 18px; }}
    .card {{ padding: 18px; margin-bottom: 14px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .meta, .stats, .muted {{ color: var(--muted); font-size: 14px; }}
    .stats {{ margin: 8px 0 12px; }}
    p {{ line-height: 1.75; }}
    a {{ color: var(--accent); }}
    ul {{ margin: 10px 0 0; padding-left: 18px; }}
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <h1>X 指定帖子与评论</h1>
      <div class="muted">原帖链接：<a href="{html.escape(summary['source_url'])}" target="_blank" rel="noreferrer">{html.escape(summary['source_url'])}</a></div>
      <div class="muted">评论总数：{summary['comment_count']} 条</div>
    </section>
    <section class="panel">
      <h2>原帖正文</h2>
      <div class="meta">@{html.escape(post.get("user_handle") or "unknown")} · {html.escape(post.get("posted_at") or "未知时间")}</div>
      <div class="stats">👍 {int(post.get("like_count", 0))} · 回复 {int(post.get("reply_count", 0))} · RT {int(post.get("retweet_count", 0))}</div>
      <p>{html.escape(post.get("text") or "（未提取到正文）").replace(chr(10), "<br/>")}</p>
    </section>
    <section class="panel">
      <h2>高频评论用户</h2>
      <ul>{top_commenters}</ul>
    </section>
    <section>
      <h2>评论列表</h2>
      {''.join(comment_cards) or '<div class="panel">暂无已抓取评论。</div>'}
    </section>
  </main>
</body>
</html>"""
    path.write_text(page, encoding="utf-8")


def checkpoint_outputs(run_dir: Path, source_url: str, post: Optional[Dict], comments: List[Dict]) -> None:
    summary = build_summary(post, comments, source_url)
    payload = {
        "source_url": source_url,
        "post": post,
        "comments": comments,
        "summary": summary,
    }
    (run_dir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "comments.json").write_text(json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_comments_csv(run_dir / "comments.csv", post, comments)
    write_summary_md(run_dir / "summary.md", summary)
    write_html_report(run_dir / "article.html", post, comments, summary)


def make_checkpoint_callback(
    run_dir: Path,
    source_url: str,
    every_n_scrolls: int,
) -> Callable[[Optional[Dict], List[Dict], int, int], None]:
    last_saved = {"scroll": 0}

    def checkpoint(post: Optional[Dict], comments: List[Dict], scroll_idx: int, new_count: int) -> None:
        if not post and not comments:
            return
        should_save = (
            scroll_idx == 0
            or new_count > 0 and (scroll_idx + 1 - last_saved["scroll"]) >= every_n_scrolls
        )
        if not should_save:
            return
        checkpoint_outputs(run_dir, source_url, post, comments)
        last_saved["scroll"] = scroll_idx + 1
        print(f"Checkpoint saved: {run_dir / 'results.json'}")

    return checkpoint


def hydrate_target_post(page: Page, target_tweet_id: str, fallback_post: Optional[Dict]) -> Optional[Dict]:
    if not fallback_post:
        return None
    post = dict(fallback_post)
    full_text = extract_full_text_from_page(page, target_tweet_id, str(post.get("text") or ""))
    if full_text:
        post["text"] = full_text
        post["full_text"] = full_text
        post["full_text_status"] = "ok"
    return post


def collect_post_and_comments(
    page: Page,
    post_url: str,
    target_tweet_id: str,
    max_comments: int,
    max_scrolls: int,
    no_new_stop: int,
    scroll_pause: int,
    checkpoint_cb: Optional[Callable[[Optional[Dict], List[Dict], int, int], None]] = None,
) -> tuple[Optional[Dict], List[Dict]]:
    target_post: Optional[Dict] = None
    seen_comments: Dict[str, Dict] = {}
    no_new_rounds = 0
    anchor_stall_rounds = 0
    last_anchor = ""

    for idx in range(max_scrolls):
        clicked = click_expand_reply_buttons(page)
        if clicked:
            print(f"Expanded hidden replies: {clicked}")

        cards = get_cards(page)
        new_count = 0

        for card in cards:
            try:
                item = extract_tweet(card)
            except Exception as exc:
                print(f"Error extracting item: {exc}")
                continue
            if not item:
                continue
            tweet_id = str(item.get("tweet_id") or "").strip()
            if not tweet_id:
                continue
            if tweet_id == target_tweet_id:
                if target_post is None:
                    target_post = enrich_item(item, target_tweet_id, 0, True)
                continue
            if tweet_id in seen_comments:
                continue
            seen_comments[tweet_id] = enrich_item(item, target_tweet_id, len(seen_comments) + 1, False)
            new_count += 1
            if max_comments > 0 and len(seen_comments) >= max_comments:
                comments = list(seen_comments.values())
                if target_post is None:
                    target_post = hydrate_target_post(page, target_tweet_id, target_post)
                if checkpoint_cb:
                    checkpoint_cb(target_post, comments, idx, new_count)
                print(f"Reached max comments: {max_comments}")
                return target_post, comments

        if target_post is None:
            target_post = hydrate_target_post(page, target_tweet_id, target_post)

        comments = list(seen_comments.values())
        print(f"Scroll {idx + 1}/{max_scrolls}: +{new_count} new, total {len(comments)}")
        if checkpoint_cb:
            checkpoint_cb(target_post, comments, idx, new_count)

        if new_count == 0:
            no_new_rounds += 1
        else:
            no_new_rounds = 0

        current_anchor = get_last_visible_anchor(page)
        if current_anchor and current_anchor == last_anchor:
            anchor_stall_rounds += 1
        elif not current_anchor and new_count == 0:
            anchor_stall_rounds += 1
        else:
            anchor_stall_rounds = 0
        last_anchor = current_anchor or last_anchor

        if no_new_rounds >= no_new_stop and anchor_stall_rounds >= 3:
            print(
                f"No new comments for {no_new_rounds} rounds and anchor stalled for {anchor_stall_rounds} rounds. Stop scrolling."
            )
            break

        scroll_feed(page, idx)
        pause_ms = scroll_pause if new_count > 0 else int(scroll_pause * 1.35)
        page.wait_for_timeout(pause_ms)

    return target_post, list(seen_comments.values())


def main() -> None:
    args = parse_args()
    post_url, handle, tweet_id = normalize_post_url(args.post_url)
    run_dir = ensure_run_dir(args, handle, tweet_id)
    print(f"Run directory: {run_dir}")
    print(f"Target post: {post_url}")
    if args.max_comments > 0:
        print(f"目标: 收集前 {args.max_comments} 条")

    with sync_playwright() as playwright:
        context = create_context(
            playwright,
            state=args.state,
            headless=args.headless,
            cdp_url=args.cdp_url,
            auto_launch=args.auto_launch,
            chrome_path=args.chrome_path,
            user_data_dir=args.user_data_dir,
            wait_seconds=args.wait_seconds,
        )
        try:
            page = context.new_page()
            page.goto(post_url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            validate_auth_state(page)
            checkpoint_cb = make_checkpoint_callback(run_dir, post_url, max(1, args.checkpoint_every))
            target_post, comments = collect_post_and_comments(
                page=page,
                post_url=post_url,
                target_tweet_id=tweet_id,
                max_comments=max(0, args.max_comments),
                max_scrolls=max(1, args.max_scrolls),
                no_new_stop=max(2, args.no_new_stop),
                scroll_pause=max(600, args.scroll_pause),
                checkpoint_cb=checkpoint_cb,
            )
            target_post = hydrate_target_post(page, tweet_id, target_post)
            checkpoint_outputs(run_dir, post_url, target_post, comments)
            print(f"成功收集 {len(comments)} 条评论")
            print(f"Results saved to: {run_dir}")
        finally:
            close_context(context)


if __name__ == "__main__":
    main()
