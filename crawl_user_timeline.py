#!/usr/bin/env python3
"""Crawl a user's timeline on X and generate detailed analysis reports."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from html_report import write_html_article
from search_x import (
    HASHTAG_RE,
    MENTION_RE,
    TWEET_SELECTORS,
    close_context,
    create_context,
    extract_tweet,
    get_cards,
    get_last_visible_anchor,
    has_end_marker,
    parse_count,
    scroll_feed,
    summarize,
    validate_auth_state,
    wait_for_search_results,
    write_summary_md,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl user's historical tweets from X timeline")
    p.add_argument("--user-url", required=True, help="User profile URL, e.g. https://x.com/vista8")
    p.add_argument("--state", default="auth_state.json", help="Playwright storage state path")
    p.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    p.add_argument("--out-dir", default="output", help="Output base directory")
    p.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Max tweets to collect (0 means no hard limit, crawl until feed stalls)",
    )
    p.add_argument("--max-scrolls", type=int, default=1000, help="Max scroll rounds")
    p.add_argument("--no-new-stop", type=int, default=25, help="Stop after N rounds with no new items")
    p.add_argument("--scroll-pause", type=int, default=1500, help="Pause between scrolls in ms")
    p.add_argument("--with-replies", action="store_true", help="Crawl /with_replies timeline")
    p.add_argument("--skip-fulltext", action="store_true", help="Skip stage-2 hydration from tweet detail pages")
    p.add_argument("--fulltext-delay-ms", type=int, default=1200, help="Pause after opening each tweet detail page")
    p.add_argument("--fulltext-checkpoint-every", type=int, default=10, help="Write hydration checkpoint every N tweets")
    p.add_argument("--cdp-url", default="", help="Existing Chrome CDP endpoint, e.g. http://127.0.0.1:9222")
    p.add_argument("--auto-launch", action="store_true", help="Auto launch Chrome with remote debugging when CDP is unavailable")
    p.add_argument("--chrome-path", default="/usr/bin/google-chrome", help="Chrome executable path for auto-launch")
    p.add_argument("--user-data-dir", default="chrome_profile", help="Chrome profile dir used for auto-launch")
    p.add_argument("--wait-seconds", type=int, default=20, help="Seconds to wait for CDP readiness after auto-launch")
    return p.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:80] or "user"


def parse_user_handle(user_url: str) -> str:
    u = user_url.strip()
    if u.startswith("@"):
        return u[1:]
    if u.startswith("http://") or u.startswith("https://"):
        path = urlparse(u).path.strip("/")
        if not path:
            raise ValueError(f"Invalid --user-url: {user_url}")
        return path.split("/")[0].lstrip("@")
    return u.strip("/").lstrip("@")


def parse_views(article) -> int:
    btn = article.query_selector('button[data-testid="analytics"]')
    if not btn:
        return 0
    text = (btn.inner_text() or "").strip()
    return parse_count(text)


def normalize_item(item: Dict, card) -> Dict:
    item = dict(item)
    item["view_count"] = parse_views(card)
    return item


def wait_for_timeline(page, timeout: int = 15000) -> bool:
    if wait_for_search_results(page, timeout=timeout) and get_cards(page):
        return True
    for selector in TWEET_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=max(1000, timeout // max(1, len(TWEET_SELECTORS))))
            if get_cards(page):
                return True
        except Exception:
            continue
    return bool(get_cards(page))


def click_tab_by_labels(page, labels: List[str]) -> bool:
    selectors = []
    for label in labels:
        selectors.extend(
            [
                f'[role="tab"]:has-text("{label}")',
                f'a[role="tab"]:has-text("{label}")',
                f'button:has-text("{label}")',
                f'div[role="tab"]:has-text("{label}")',
                f'a[href$="/{label.lower()}"]',
            ]
        )
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if target.count() == 0:
                continue
            target.click(timeout=2500)
            page.wait_for_timeout(2000)
            if wait_for_timeline(page, timeout=8000):
                return True
        except Exception:
            continue
    return False


def recover_timeline(
    page,
    target_url: str,
    base_url: str,
    with_replies: bool,
    attempt: int,
    scroll_pause: int,
) -> bool:
    strategy = attempt % 4
    try:
        if strategy == 0:
            print("Recovery: nudge timeline and jump to end.")
            page.evaluate("window.scrollBy(0, -Math.floor(window.innerHeight * 0.8))")
            page.wait_for_timeout(900)
            page.mouse.wheel(0, 2200)
            page.keyboard.press("End")
            page.wait_for_timeout(max(1800, scroll_pause))
            return bool(get_cards(page))

        if strategy == 1:
            print(f"Recovery: reload {target_url}")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2200)
            return wait_for_timeline(page)

        if strategy == 2:
            print(f"Recovery: revisit timeline {target_url}")
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2400)
            return wait_for_timeline(page)

        labels = ["Replies", "回复"] if with_replies else ["Posts", "推文", "帖子"]
        print(f"Recovery: reopen profile {base_url} and retry tab {labels[0]}")
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2200)
        if with_replies and click_tab_by_labels(page, labels):
            return True
        if not with_replies and click_tab_by_labels(page, labels):
            return True
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2200)
        return wait_for_timeline(page)
    except Exception as exc:
        print(f"Recovery attempt failed: {exc}")
        return False


def collect_user_tweets(
    page,
    target_url: str,
    base_url: str,
    with_replies: bool,
    max_items: int,
    max_scrolls: int,
    no_new_stop: int,
    scroll_pause: int,
    checkpoint_cb: Optional[Callable[[List[Dict], int, int], None]] = None,
) -> List[Dict]:
    seen: Dict[str, Dict] = {}
    no_new_rounds = 0
    anchor_stall_rounds = 0
    last_anchor = ""
    recovery_attempts = 0
    max_recoveries = 8
    recovery_threshold = min(max(4, no_new_stop // 3), max(4, no_new_stop))

    for idx in range(max_scrolls):
        cards = get_cards(page)
        new_count = 0

        for card in cards:
            item = extract_tweet(card)
            if not item:
                continue
            tid = item["tweet_id"]
            if tid in seen:
                continue

            seen[tid] = normalize_item(item, card)
            new_count += 1

            if max_items > 0 and len(seen) >= max_items:
                if checkpoint_cb:
                    checkpoint_cb(list(seen.values()), idx, new_count)
                print(f"Reached max items: {max_items}")
                return list(seen.values())

        print(f"Scroll {idx + 1}/{max_scrolls}: +{new_count} new, total {len(seen)}")
        if checkpoint_cb:
            checkpoint_cb(list(seen.values()), idx, new_count)

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

        if has_end_marker(page) and new_count == 0:
            print("Detected end marker on timeline. Stop scrolling.")
            break

        should_recover = (
            new_count == 0
            and no_new_rounds >= recovery_threshold
            and anchor_stall_rounds >= 2
            and recovery_attempts < max_recoveries
        )
        if should_recover:
            recovery_attempts += 1
            recovered = recover_timeline(
                page=page,
                target_url=target_url,
                base_url=base_url,
                with_replies=with_replies,
                attempt=recovery_attempts - 1,
                scroll_pause=scroll_pause,
            )
            cards_after_recovery = len(get_cards(page))
            print(
                f"Recovery {recovery_attempts}/{max_recoveries}: recovered={recovered}, cards={cards_after_recovery}, total={len(seen)}"
            )
            if recovered:
                no_new_rounds = 0
                anchor_stall_rounds = 0
                continue

        if no_new_rounds >= no_new_stop and anchor_stall_rounds >= 3:
            print(
                f"No new tweets for {no_new_rounds} rounds and anchor stalled for {anchor_stall_rounds} rounds. Stop scrolling."
            )
            break

        scroll_feed(page, idx)
        pause_ms = scroll_pause if new_count > 0 else int(scroll_pause * 1.35)
        page.wait_for_timeout(pause_ms)

    return list(seen.values())


def checkpoint_user_timeline_outputs(run_dir: Path, items: List[Dict], handle: str) -> None:
    summary = summarize(items, f"@{handle} history")
    detailed = build_detailed_analysis(items, handle)
    (run_dir / "results.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(run_dir / "results.csv", items)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_md(run_dir / "summary.md", summary)
    write_html_article(run_dir / "article.html", f"@{handle} 历史推文", items)
    (run_dir / "detailed_report.json").write_text(json.dumps(detailed, ensure_ascii=False, indent=2), encoding="utf-8")
    write_detailed_md(run_dir / "detailed_report.md", detailed)
    write_detailed_html(run_dir / "detailed_report.html", detailed)


def make_user_timeline_checkpoint_callback(
    run_dir: Path,
    handle: str,
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
        checkpoint_user_timeline_outputs(run_dir, items, handle)
        last_saved["scroll"] = scroll_idx + 1
        print(f"Checkpoint saved: {run_dir / 'results.json'}")

    return checkpoint


def to_dt(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def build_detailed_analysis(items: List[Dict], user_handle: str) -> Dict:
    likes = [int(i.get("like_count", 0)) for i in items]
    rts = [int(i.get("retweet_count", 0)) for i in items]
    replies = [int(i.get("reply_count", 0)) for i in items]
    views = [int(i.get("view_count", 0)) for i in items if int(i.get("view_count", 0)) > 0]

    total = len(items)
    times = [to_dt(i.get("posted_at")) for i in items]
    times = [t for t in times if t is not None]
    times.sort()

    by_month = Counter()
    by_weekday = Counter()
    by_hour = Counter()
    if times:
        for t in times:
            by_month[t.strftime("%Y-%m")] += 1
            by_weekday[t.strftime("%A")] += 1
            by_hour[t.strftime("%H")] += 1

    tags = Counter()
    mentions = Counter()
    for i in items:
        txt = i.get("text") or ""
        tags.update([x.lower() for x in HASHTAG_RE.findall(txt)])
        mentions.update([x.lower() for x in MENTION_RE.findall(txt)])

    top_eng = sorted(
        items,
        key=lambda x: int(x.get("like_count", 0)) + int(x.get("retweet_count", 0)) * 2 + int(x.get("reply_count", 0)),
        reverse=True,
    )[:20]

    span_days = 0
    if len(times) >= 2:
        span_days = max(1, (times[-1] - times[0]).days + 1)

    analysis = {
        "user_handle": user_handle,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_collected": total,
        "time_range": {
            "from": times[0].isoformat() if times else None,
            "to": times[-1].isoformat() if times else None,
            "days": span_days,
        },
        "activity": {
            "avg_tweets_per_day": round(total / span_days, 3) if span_days else None,
            "by_month": [{"month": k, "count": v} for k, v in by_month.most_common()],
            "by_weekday": [{"weekday": k, "count": v} for k, v in by_weekday.most_common()],
            "by_hour": [{"hour": k, "count": v} for k, v in by_hour.most_common()],
        },
        "engagement": {
            "avg_like": round(sum(likes) / total, 3) if total else 0,
            "median_like": median(likes) if likes else 0,
            "avg_retweet": round(sum(rts) / total, 3) if total else 0,
            "avg_reply": round(sum(replies) / total, 3) if total else 0,
            "avg_view": round(sum(views) / len(views), 3) if views else None,
        },
        "top_hashtags": [{"tag": k, "count": v} for k, v in tags.most_common(30)],
        "top_mentions": [{"username": k, "count": v} for k, v in mentions.most_common(30)],
        "top_engagement_tweets": [
            {
                "tweet_id": x.get("tweet_id"),
                "url": x.get("url"),
                "posted_at": x.get("posted_at"),
                "like_count": x.get("like_count", 0),
                "retweet_count": x.get("retweet_count", 0),
                "reply_count": x.get("reply_count", 0),
                "view_count": x.get("view_count", 0),
                "text": (x.get("text") or "")[:260],
            }
            for x in top_eng
        ],
    }
    return analysis


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
        "full_text_fetched_at",
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


def write_detailed_md(path: Path, analysis: Dict) -> None:
    lines = [
        f"# User Timeline Detailed Report: @{analysis['user_handle']}",
        "",
        f"- Generated at (UTC): {analysis['generated_at_utc']}",
        f"- Total collected: {analysis['total_collected']}",
        f"- Time range: {analysis['time_range']['from']} ~ {analysis['time_range']['to']}",
        "",
        "## Activity",
        f"- Avg tweets/day: {analysis['activity']['avg_tweets_per_day']}",
        "",
        "## Engagement",
        f"- Avg like: {analysis['engagement']['avg_like']}",
        f"- Median like: {analysis['engagement']['median_like']}",
        f"- Avg retweet: {analysis['engagement']['avg_retweet']}",
        f"- Avg reply: {analysis['engagement']['avg_reply']}",
        "",
        "## Top hashtags",
    ]

    for x in analysis["top_hashtags"][:15]:
        lines.append(f"- #{x['tag']}: {x['count']}")

    lines.append("")
    lines.append("## Top mentions")
    for x in analysis["top_mentions"][:15]:
        lines.append(f"- @{x['username']}: {x['count']}")

    lines.append("")
    lines.append("## Top engagement tweets")
    for i, x in enumerate(analysis["top_engagement_tweets"][:15], start=1):
        lines.append(
            f"{i}. likes={x['like_count']} rt={x['retweet_count']} reply={x['reply_count']} | {x['url']}"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_detailed_html(path: Path, analysis: Dict) -> None:
    cards = "".join(
        [
            f"<li><strong>{x['month']}</strong>: {x['count']}</li>"
            for x in analysis["activity"]["by_month"][:24]
        ]
    )
    top_rows = "".join(
        [
            (
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{x['like_count']}</td>"
                f"<td>{x['retweet_count']}</td>"
                f"<td>{x['reply_count']}</td>"
                f"<td>{(x.get('text') or '').replace('<', '&lt;').replace('>', '&gt;')[:120]}</td>"
                f"<td><a href=\"{x['url']}\" target=\"_blank\">link</a></td>"
                "</tr>"
            )
            for i, x in enumerate(analysis["top_engagement_tweets"][:20], start=1)
        ]
    )

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>@{analysis['user_handle']} Detailed Timeline Report</title>
  <style>
    body {{ font-family: "IBM Plex Sans", "Noto Sans", sans-serif; margin: 0; background:#f7f6f3; color:#1d1d1d; }}
    .wrap {{ max-width: 1040px; margin: 0 auto; padding: 24px; }}
    .card {{ background:#fff; border:1px solid #ddd5c8; border-radius:12px; padding:16px; margin-bottom:14px; }}
    h1,h2 {{ margin: 0 0 10px; }}
    ul {{ margin:0; padding-left:18px; }}
    table {{ width:100%; border-collapse: collapse; font-size:14px; }}
    th,td {{ border-bottom:1px solid #ece7df; padding:8px; text-align:left; }}
    th {{ background:#f3efe8; }}
  </style>
</head>
<body>
  <main class=\"wrap\">
    <section class=\"card\">
      <h1>@{analysis['user_handle']} 历史推文详细分析</h1>
      <p>Generated at (UTC): {analysis['generated_at_utc']}</p>
      <p>Total collected: {analysis['total_collected']}</p>
      <p>Time range: {analysis['time_range']['from']} ~ {analysis['time_range']['to']}</p>
    </section>

    <section class=\"card\">
      <h2>活跃月份分布</h2>
      <ul>{cards}</ul>
    </section>

    <section class=\"card\">
      <h2>高互动推文 Top 20</h2>
      <table>
        <thead><tr><th>#</th><th>Like</th><th>RT</th><th>Reply</th><th>Text</th><th>URL</th></tr></thead>
        <tbody>{top_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def main() -> None:
    args = parse_args()
    handle = parse_user_handle(args.user_url)

    out_base = Path(args.out_dir).expanduser().resolve()
    run_dir = out_base / f"user_{safe_name(handle)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run directory: {run_dir}")

    base_url = f"https://x.com/{handle}"
    target_url = f"{base_url}/with_replies" if args.with_replies else base_url
    print(f"Target user: @{handle}")
    print(f"Timeline URL: {target_url}")

    with sync_playwright() as p:
        if args.cdp_url:
            print(f"Using existing Chrome via CDP: {args.cdp_url}")
        context = create_context(
            p,
            args.state,
            args.headless,
            cdp_url=args.cdp_url,
            auto_launch=args.auto_launch,
            chrome_path=args.chrome_path,
            user_data_dir=args.user_data_dir,
            wait_seconds=args.wait_seconds,
        )
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if not validate_auth_state(page):
            print("Authentication issue detected. Please refresh login state with login_x.py.")
            close_context(context)
            return

        if not wait_for_timeline(page):
            print("Warning: timeline content did not become visible after initial load.")

        items = collect_user_tweets(
            page=page,
            target_url=target_url,
            base_url=base_url,
            with_replies=args.with_replies,
            max_items=args.max_items,
            max_scrolls=args.max_scrolls,
            no_new_stop=args.no_new_stop,
            scroll_pause=args.scroll_pause,
            checkpoint_cb=make_user_timeline_checkpoint_callback(run_dir, handle),
        )
        close_context(context)

    if not items:
        print("Warning: No tweets were collected.")
        return

    stage1_json_path = run_dir / "results_stage1.json"
    stage1_json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Stage 1 saved:      {stage1_json_path}")

    if not args.skip_fulltext:
        from tweet_fulltext import hydrate_items_with_fulltext

        print("Stage 2: hydrating full text from tweet detail pages...")
        with sync_playwright() as p:
            context = create_context(
                p,
                args.state,
                args.headless,
                cdp_url=args.cdp_url,
                auto_launch=args.auto_launch,
                chrome_path=args.chrome_path,
                user_data_dir=args.user_data_dir,
                wait_seconds=args.wait_seconds,
            )
            items = hydrate_items_with_fulltext(
                context=context,
                items=items,
                run_dir=run_dir,
                checkpoint_every=args.fulltext_checkpoint_every,
                delay_ms=args.fulltext_delay_ms,
                logger=print,
            )
            close_context(context)

    summary = summarize(items, f"@{handle} history")
    detailed = build_detailed_analysis(items, handle)

    json_path = run_dir / "results.json"
    csv_path = run_dir / "results.csv"
    summary_json = run_dir / "summary.json"
    summary_md = run_dir / "summary.md"
    article_html = run_dir / "article.html"
    detailed_json = run_dir / "detailed_report.json"
    detailed_md = run_dir / "detailed_report.md"
    detailed_html = run_dir / "detailed_report.html"

    checkpoint_user_timeline_outputs(run_dir, items, handle)

    print("=" * 60)
    print(f"Done. Collected {len(items)} tweets from @{handle}.")
    print(f"Results JSON:      {json_path}")
    print(f"Results CSV:       {csv_path}")
    print(f"Summary JSON:      {summary_json}")
    print(f"Summary Markdown:  {summary_md}")
    print(f"Article HTML:      {article_html}")
    print(f"Detailed JSON:     {detailed_json}")
    print(f"Detailed Markdown: {detailed_md}")
    print(f"Detailed HTML:     {detailed_html}")
    print("=" * 60)


if __name__ == "__main__":
    main()
