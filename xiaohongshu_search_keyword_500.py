#!/usr/bin/env python3
"""Search Xiaohongshu by keyword, save top 500 summaries, then hydrate note texts and comments."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from browser_config import get_browser_args, get_playwright_launch_kwargs
from xiaohongshu_user_notes import (
    DEFAULT_USER_AGENT,
    detect_login_overlay,
    extract_note_detail,
    parse_cookie_string,
    write_progress,
)

NOTE_URL_RE = re.compile(r"^https?://www\.xiaohongshu\.com/(?:explore|discovery/item)/([A-Za-z0-9]+)(?:[/?#].*)?$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search Xiaohongshu notes and hydrate top 500 results.")
    parser.add_argument("--keyword", required=True, help="Search keyword")
    parser.add_argument("--cookie", required=True, help="Cookie header copied from the browser request")
    parser.add_argument("--resume-dir", default="", help="Resume hydration from an existing run directory")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Optional browser user agent")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--out-dir", default="output", help="Output base directory")
    parser.add_argument("--max-items", type=int, default=500, help="Max search results to collect")
    parser.add_argument("--max-scrolls", type=int, default=180, help="Max scroll rounds on search page")
    parser.add_argument("--no-new-stop", type=int, default=10, help="Stop after N rounds without new result links")
    parser.add_argument("--page-delay-ms", type=int, default=1800, help="Wait time after each page action in ms")
    parser.add_argument("--detail-delay-ms", type=int, default=1500, help="Wait time after opening each detail page in ms")
    parser.add_argument("--comment-scrolls", type=int, default=40, help="Max comment scroll rounds per note")
    parser.add_argument("--checkpoint-every", type=int, default=10, help="Write hydrated checkpoint every N results")
    return parser.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:60] or "xiaohongshu_search"


def create_run_dir(base_dir: Path, keyword: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_dir / f"xiaohongshu_search_{safe_name(keyword)}_500_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def make_search_url(keyword: str) -> str:
    q = quote(re.sub(r"\s+", " ", keyword).strip())
    return f"https://www.xiaohongshu.com/search_result?keyword={q}&source=web_explore_feed"


def canonical_url(url: str) -> str:
    clean = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if clean.startswith("https://www.xiaohongshu.com/discovery/item/"):
        return clean.replace("/discovery/item/", "/explore/")
    return clean


def ensure_search_ready(page) -> None:
    body_text = (page.locator("body").inner_text(timeout=4000) or "").strip()
    if "IP存在风险" in body_text or "安全限制" in body_text:
        raise RuntimeError("小红书搜索页触发安全限制，请切换网络环境或更新 Cookie 后重试。")
    if "登录后查看更多" in body_text or "手机号登录" in body_text or detect_login_overlay(page):
        raise RuntimeError("小红书搜索页需要有效登录态，请填写可用 Cookie 后重试。")


def ensure_detail_ready(page) -> None:
    body_text = (page.locator("body").inner_text(timeout=4000) or "").strip()
    if "IP存在风险" in body_text or "安全限制" in body_text:
        raise RuntimeError("小红书详情页触发安全限制，请切换网络环境或更新 Cookie 后重试。")
    if "/404" in page.url or "当前笔记暂时无法浏览" in body_text or "请打开小红书App扫码查看" in body_text:
        raise RuntimeError("当前笔记暂时无法通过网页版访问，需在小红书 App 内查看。")


def collect_result_candidates(page) -> List[Dict]:
    return page.eval_on_selector_all(
        "a[href]",
        """
        elements => {
          const out = [];
          for (const a of elements) {
            const href = a.href || "";
            if (!/xiaohongshu\\.com\\/(?:explore|discovery\\/item)\\//.test(href)) continue;
            const card =
              a.closest('section') ||
              a.closest('article') ||
              a.closest('[class*="note"]') ||
              a.closest('[class*="feed"]') ||
              a.closest('[class*="search"]') ||
              a.parentElement;
            const titleEl =
              card?.querySelector('.title') ||
              card?.querySelector('[class*="title"]') ||
              card?.querySelector('h3') ||
              card?.querySelector('h2');
            const authorEl =
              card?.querySelector('.name') ||
              card?.querySelector('[class*="author"]') ||
              card?.querySelector('[class*="user"]');
            const imgEl = card?.querySelector('img');
            const title = (titleEl?.textContent || a.textContent || '').replace(/\\s+/g, ' ').trim();
            const context = (card?.innerText || '').replace(/\\s+/g, ' ').trim();
            out.push({
              href,
              title,
              author: (authorEl?.textContent || '').replace(/\\s+/g, ' ').trim(),
              context,
              cover_image: imgEl?.currentSrc || imgEl?.src || ''
            });
          }
          return out;
        }
        """,
    )


def extract_search_results(page, max_items: int, max_scrolls: int, no_new_stop: int, page_delay_ms: int) -> List[Dict]:
    seen: Dict[str, Dict] = {}
    no_new_rounds = 0
    current_height = 0
    for round_no in range(1, max_scrolls + 1):
        ensure_search_ready(page)
        candidates = collect_result_candidates(page)
        new_items = 0
        for item in candidates:
            url = canonical_url(item.get("href", ""))
            match = NOTE_URL_RE.match(url)
            if not match:
                continue
            if url in seen:
                continue
            title = str(item.get("title", "")).strip()
            context = str(item.get("context", "")).strip()
            snippet = context
            if title and snippet.startswith(title):
                snippet = snippet[len(title):].strip(" -|")
            snippet = snippet[:600]
            seen[url] = {
                "url": url,
                "note_id": match.group(1),
                "title": title[:240],
                "author": str(item.get("author", "")).strip(),
                "snippet": snippet,
                "cover_image": str(item.get("cover_image", "")).strip(),
                "stage1_collected_at": datetime.now().isoformat(timespec="seconds"),
            }
            new_items += 1
            if len(seen) >= max_items:
                break
        print(f"Page {round_no}: + {new_items} new, total {len(seen)}")
        if len(seen) >= max_items:
            break
        if new_items:
            no_new_rounds = 0
        else:
            no_new_rounds += 1
        page.mouse.wheel(0, 2800)
        page.wait_for_timeout(page_delay_ms)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == current_height and no_new_rounds >= no_new_stop:
            break
        current_height = new_height
    return list(seen.values())


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_stage1(run_dir: Path, keyword: str, search_url: str, items: List[Dict]) -> None:
    write_json(
        run_dir / "results_stage1.json",
        {
            "keyword": keyword,
            "search_url": search_url,
            "collected_count": len(items),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "items": items,
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
                "url",
                "publish_time",
                "detail_like_text",
                "image_count",
                "comment_count",
                "snippet",
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
                    "url": row.get("url", ""),
                    "publish_time": row.get("publish_time", ""),
                    "detail_like_text": row.get("detail_like_text", ""),
                    "image_count": row.get("image_count", 0),
                    "comment_count": row.get("comment_count", 0),
                    "snippet": row.get("snippet", ""),
                    "content": row.get("content", ""),
                }
            )


def write_markdown(path: Path, keyword: str, search_url: str, rows: List[Dict]) -> None:
    parts = [
        f"# 小红书搜索结果全文 - {keyword}",
        "",
        f"- 搜索链接: {search_url}",
        f"- 结果数量: {len(rows)}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        parts.extend(
            [
                f"## {index}. {row.get('detail_title') or row.get('title') or f'笔记 {index}'}",
                "",
                f"- 作者: {row.get('author', '') or '-'}",
                f"- 链接: {row.get('url', '') or '-'}",
                f"- 发布时间: {row.get('publish_time', '') or '-'}",
                f"- 点赞: {row.get('detail_like_text', '') or '-'}",
                f"- 图片数量: {row.get('image_count', 0)}",
                f"- 评论数量: {row.get('comment_count', 0)}",
                "",
                row.get("content") or row.get("snippet", ""),
                "",
            ]
        )
        comments = row.get("comments", [])
        if comments:
            parts.append("### 评论")
            parts.append("")
            for comment in comments:
                parts.append(
                    f"- {(comment.get('author') or '匿名')} | {(comment.get('time') or '-')} | {comment.get('content', '')}"
                )
            parts.append("")
    path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")


def build_html(keyword: str, search_url: str, rows: List[Dict]) -> str:
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
              <h2><a href="{html.escape(row.get('url', ''))}" target="_blank" rel="noreferrer">{html.escape(title)}</a></h2>
              <div class="meta">{html.escape((row.get('author') or '-') + ' | 点赞 ' + (row.get('detail_like_text') or '-'))}</div>
              <p>{html.escape(row.get('content') or row.get('snippet', ''))}</p>
              <div class="gallery">{image_html}</div>
              <div class="meta">评论 {row.get('comment_count', 0)} 条</div>
              <ul class="comments">{comments_html}</ul>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(keyword)} - 小红书搜索结果</title>
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
    h2 a {{ color: inherit; text-decoration: none; }}
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
      <h1>{html.escape(keyword)}</h1>
      <p>搜索链接: <a href="{html.escape(search_url)}" target="_blank" rel="noreferrer">{html.escape(search_url)}</a></p>
      <p>当前版本会先抓前 500 条链接和摘要，再逐条进入详情页抓正文、图片和评论。</p>
      <p>已补全文结果: {len(rows)} 条</p>
    </section>
    <section class="list">{''.join(cards)}</section>
  </main>
</body>
</html>"""


def write_outputs(run_dir: Path, keyword: str, search_url: str, rows: List[Dict]) -> None:
    write_json(
        run_dir / "results.json",
        {
            "keyword": keyword,
            "search_url": search_url,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "items": rows,
        },
    )
    write_json(
        run_dir / "comments.json",
        {
            "keyword": keyword,
            "search_url": search_url,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "items": [
                {
                    "title": row.get("detail_title") or row.get("title", ""),
                    "url": row.get("url", ""),
                    "comment_count": row.get("comment_count", 0),
                    "comments": row.get("comments", []),
                }
                for row in rows
            ],
        },
    )
    write_csv(run_dir / "results.csv", rows)
    write_markdown(run_dir / "all_notes.md", keyword, search_url, rows)
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                f"# 小红书搜索结果全文 - {keyword}",
                "",
                f"- 搜索链接: {search_url}",
                f"- 已补全文结果: {len(rows)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "article.html").write_text(build_html(keyword, search_url, rows), encoding="utf-8")


def load_existing_hydrated(run_dir: Path) -> List[Dict]:
    path = run_dir / "results.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(payload.get("items", []))


def load_failure_list(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(payload if isinstance(payload, list) else [])


def hydrate_details(context, items: List[Dict], detail_delay_ms: int, comment_scrolls: int, run_dir: Path, keyword: str, search_url: str, checkpoint_every: int) -> List[Dict]:
    hydrated: List[Dict] = load_existing_hydrated(run_dir)
    failures: List[Dict] = load_failure_list(run_dir / "failed_details.json")
    unavailable: List[Dict] = load_failure_list(run_dir / "unavailable_details.json")
    total = len(items)
    hydrated_urls = {row.get("url", "") for row in hydrated if row.get("url")}
    unavailable_urls = {row.get("url", "") for row in unavailable if row.get("url")}
    failed_urls = {row.get("url", "") for row in failures if row.get("url")}
    processed = len(hydrated) + len(unavailable) + len(failures)
    write_outputs(run_dir, keyword, search_url, hydrated)
    write_json(run_dir / "failed_details.json", failures)
    write_json(run_dir / "unavailable_details.json", unavailable)
    write_progress(run_dir, total, processed, len(hydrated), len(unavailable) + len(failures))

    for index, item in enumerate(items, start=1):
        if item.get("url") in hydrated_urls or item.get("url") in unavailable_urls or item.get("url") in failed_urls:
            continue
        page = context.new_page()
        try:
            page.goto(item["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(detail_delay_ms)
            ensure_detail_ready(page)
            print(f"[FULLTEXT] {index}/{total} {item.get('title', '')}")
            hydrated_item = extract_note_detail(page, item, detail_delay_ms, comment_scrolls)
            hydrated.append(hydrated_item)
            hydrated_urls.add(item.get("url", ""))
        except Exception as exc:
            error_text = str(exc)
            if "当前笔记暂时无法通过网页版访问" in error_text:
                unavailable.append({**item, "error": error_text})
                unavailable_urls.add(item.get("url", ""))
            else:
                failures.append({**item, "error": error_text})
                failed_urls.add(item.get("url", ""))
        finally:
            page.close()
        processed += 1
        write_progress(run_dir, total, processed, len(hydrated), len(unavailable) + len(failures))
        if processed % checkpoint_every == 0 or processed == total:
            write_outputs(run_dir, keyword, search_url, hydrated)
            write_json(run_dir / "failed_details.json", failures)
            write_json(run_dir / "unavailable_details.json", unavailable)
    return hydrated


def main() -> int:
    args = parse_args()
    output_base = Path(args.out_dir)
    output_base.mkdir(parents=True, exist_ok=True)
    resume_dir = Path(args.resume_dir).expanduser().resolve() if args.resume_dir else None
    run_dir = resume_dir if resume_dir else create_run_dir(output_base, args.keyword)
    search_url = make_search_url(args.keyword)
    print(f"Run directory: {run_dir.resolve()}")
    print(f"Search URL: {search_url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**get_playwright_launch_kwargs(headless=args.headless))
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
        if resume_dir:
            stage1_path = run_dir / "results_stage1.json"
            if not stage1_path.exists():
                raise RuntimeError(f"恢复目录缺少阶段1文件：{stage1_path}")
            payload = json.loads(stage1_path.read_text(encoding="utf-8"))
            search_url = str(payload.get("search_url") or search_url)
            stage1_items = list(payload.get("items", []))
            if not stage1_items:
                raise RuntimeError(f"阶段1文件里没有可恢复的结果：{stage1_path}")
            print(f"从已有目录恢复补全: {run_dir}")
            print(f"阶段1结果数量: {len(stage1_items)}")
        else:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(args.page_delay_ms)
            ensure_search_ready(page)
            stage1_items = extract_search_results(page, args.max_items, args.max_scrolls, args.no_new_stop, args.page_delay_ms)
            if not stage1_items:
                raise RuntimeError("没有抓到任何小红书搜索结果。请检查关键词、Cookie 或网络环境。")
            print(f"成功收集 {len(stage1_items)} 条小红书搜索结果")
            write_stage1(run_dir, args.keyword, search_url, stage1_items)

        print("开始第二阶段：逐条补全小红书正文与评论")
        hydrated = hydrate_details(
            context,
            stage1_items,
            args.detail_delay_ms,
            args.comment_scrolls,
            run_dir,
            args.keyword,
            search_url,
            max(1, args.checkpoint_every),
        )
        write_outputs(run_dir, args.keyword, search_url, hydrated)
        page.close()
        context.close()
        browser.close()

    print(f"Results stage1: {run_dir / 'results_stage1.json'}")
    print(f"Results JSON: {run_dir / 'results.json'}")
    print(f"Comments JSON: {run_dir / 'comments.json'}")
    print(f"Unavailable details: {run_dir / 'unavailable_details.json'}")
    print(f"Article HTML: {run_dir / 'article.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
