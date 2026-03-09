#!/usr/bin/env python3
"""Search Zhihu content by keyword, save top 500 summaries, then hydrate full texts."""

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

from playwright.sync_api import TimeoutError, sync_playwright

from browser_config import get_browser_args
from zhihu_question_answers import (
    DEFAULT_USER_AGENT,
    click_expand_buttons,
    detect_risk_or_login,
    first_text,
    longest_text,
    parse_cookie_string,
)

ANSWER_URL_RE = re.compile(r"^https?://www\.zhihu\.com/question/(\d+)/answer/(\d+)(?:[/?#].*)?$")
ARTICLE_URL_RE = re.compile(r"^https?://zhuanlan\.zhihu\.com/p/(\d+)(?:[/?#].*)?$")
QUESTION_URL_RE = re.compile(r"^https?://www\.zhihu\.com/question/(\d+)(?:[/?#].*)?$")
CONTENT_URL_RE = re.compile(
    r"^https?://(?:www\.zhihu\.com/question/\d+(?:/answer/\d+)?|zhuanlan\.zhihu\.com/p/\d+)(?:[/?#].*)?$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search Zhihu content and hydrate top 500 results.")
    parser.add_argument("--keyword", required=True, help="Search keyword")
    parser.add_argument("--cookie", required=True, help="Cookie header copied from the browser request")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Optional browser user agent")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--out-dir", default="output", help="Output base directory")
    parser.add_argument("--max-items", type=int, default=500, help="Max search results to collect")
    parser.add_argument("--max-scrolls", type=int, default=140, help="Max scroll rounds on search page")
    parser.add_argument("--no-new-stop", type=int, default=8, help="Stop after N rounds without new result links")
    parser.add_argument("--page-delay-ms", type=int, default=1800, help="Wait time after each page action in ms")
    parser.add_argument("--detail-delay-ms", type=int, default=1200, help="Wait time after opening each detail page in ms")
    parser.add_argument("--checkpoint-every", type=int, default=10, help="Write hydrated checkpoint every N results")
    return parser.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:60] or "zhihu_search"


def create_run_dir(base_dir: Path, keyword: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_dir / f"zhihu_search_{safe_name(keyword)}_500_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def make_search_url(keyword: str) -> str:
    q = quote(re.sub(r"\s+", " ", keyword).strip())
    return f"https://www.zhihu.com/search?type=content&q={q}"


def canonical_url(url: str) -> str:
    clean = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return clean


def detect_content_type(url: str) -> str:
    if ANSWER_URL_RE.match(url):
        return "answer"
    if ARTICLE_URL_RE.match(url):
        return "article"
    if QUESTION_URL_RE.match(url):
        return "question"
    return "unknown"


def collect_result_candidates(page) -> List[Dict]:
    return page.eval_on_selector_all(
        "a[href]",
        """
        elements => {
          const out = [];
          for (const a of elements) {
            const href = a.href || "";
            if (!href) continue;
            if (!/^https?:\\/\\//.test(href)) continue;
            const card =
              a.closest('[class*="SearchResult"]') ||
              a.closest('[class*="List-item"]') ||
              a.closest('article') ||
              a.closest('section') ||
              a.parentElement;
            const title = (a.textContent || "").replace(/\\s+/g, " ").trim();
            const context = (card?.innerText || "").replace(/\\s+/g, " ").trim();
            out.push({ href, title, context });
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
        click_expand_buttons(page)
        candidates = collect_result_candidates(page)
        new_items = 0
        for item in candidates:
            url = canonical_url(item.get("href", ""))
            if not CONTENT_URL_RE.match(url):
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
                "title": title[:240],
                "snippet": snippet,
                "content_type": detect_content_type(url),
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
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(page_delay_ms)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == current_height and no_new_rounds >= no_new_stop:
            break
        current_height = new_height
    return list(seen.values())


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_stage1(run_dir: Path, keyword: str, search_url: str, items: List[Dict]) -> None:
    payload = {
        "keyword": keyword,
        "search_url": search_url,
        "collected_count": len(items),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "results": items,
    }
    write_json(run_dir / "results_stage1.json", payload)


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


def extract_detail(page, item: Dict, detail_delay_ms: int) -> Dict:
    url = item["url"]
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(detail_delay_ms)
    detect_risk_or_login(page)
    click_expand_buttons(page)
    page.wait_for_timeout(500)

    content_type = item.get("content_type") or detect_content_type(url)
    title = item.get("title", "")
    author = ""
    content = ""
    if content_type == "answer":
        title = first_text(page, ["h1.QuestionHeader-title", "h1"]) or title
        author = first_text(page, [".AuthorInfo-content .UserLink-link", ".AuthorInfo-content", "a[href*='/people/']"])
        content = longest_text(
            page,
            [
                ".RichContent-inner .RichText.ztext",
                ".RichText.ztext",
                ".RichContent-inner",
                "article",
                "main",
            ],
        )
    elif content_type == "article":
        title = first_text(page, ["h1.Post-Title", "h1.ArticleItem-Title", "h1"]) or title
        author = first_text(page, [".AuthorInfo-name", ".Post-Author .UserLink-link", "a[href*='/people/']"])
        content = longest_text(
            page,
            [
                ".Post-RichTextContainer",
                ".RichText.ztext",
                "article",
                "main",
            ],
        )
    else:
        title = first_text(page, ["h1.QuestionHeader-title", "h1"]) or title
        author = first_text(page, [".AuthorInfo-content .UserLink-link", ".AuthorInfo-content", "a[href*='/people/']"])
        content = longest_text(page, [".RichText.ztext", ".QuestionRichText", "article", "main"])

    if not content:
        raise RuntimeError(f"详情页未提取到正文：{url}")

    return {
        **item,
        "detail_title": title,
        "author": author,
        "content": content,
        "content_length": len(content),
        "hydrated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_csv(path: Path, rows: List[Dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "content_type",
                "title",
                "detail_title",
                "author",
                "url",
                "content_length",
                "snippet",
                "content",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "content_type": row.get("content_type", ""),
                    "title": row.get("title", ""),
                    "detail_title": row.get("detail_title", ""),
                    "author": row.get("author", ""),
                    "url": row.get("url", ""),
                    "content_length": row.get("content_length", 0),
                    "snippet": row.get("snippet", ""),
                    "content": row.get("content", ""),
                }
            )


def write_markdown(path: Path, keyword: str, search_url: str, rows: List[Dict]) -> None:
    parts = [
        f"# 知乎搜索结果全文 - {keyword}",
        "",
        f"- 搜索链接: {search_url}",
        f"- 结果数量: {len(rows)}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        heading = row.get("detail_title") or row.get("title") or row.get("url") or f"结果 {index}"
        parts.extend(
            [
                f"## {index}. {heading}",
                "",
                f"- 类型: {row.get('content_type', '')}",
                f"- 作者: {row.get('author', '') or '-'}",
                f"- 链接: {row.get('url', '')}",
                "",
                row.get("content", row.get("snippet", "")),
                "",
            ]
        )
    path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")


def build_html(keyword: str, search_url: str, rows: List[Dict]) -> str:
    cards = []
    for index, row in enumerate(rows, start=1):
        title = row.get("detail_title") or row.get("title") or row.get("url") or f"结果 {index}"
        meta = " | ".join(
            x
            for x in [
                row.get("content_type", ""),
                row.get("author", ""),
                f"{row.get('content_length', 0)} chars" if row.get("content") else "",
            ]
            if x
        )
        cards.append(
            f"""
            <article class="card">
              <div class="index">{index}</div>
              <h2>{html.escape(title)}</h2>
              <div class="meta">{html.escape(meta or '无元数据')}</div>
              <a class="link" href="{html.escape(row.get('url', '#'))}" target="_blank" rel="noreferrer">打开原文</a>
              <pre>{html.escape(row.get("content", row.get("snippet", "")))}</pre>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>知乎搜索全文 - {html.escape(keyword)}</title>
  <style>
    :root {{
      --bg: #f4efe7;
      --card: rgba(255,255,255,0.84);
      --line: #d8cfbf;
      --ink: #1e1914;
      --muted: #6a6259;
      --accent: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "IBM Plex Sans", "PingFang SC", "Noto Sans SC", sans-serif;
      background:
        radial-gradient(900px 420px at 105% -10%, rgba(15,118,110,0.16), transparent 60%),
        radial-gradient(960px 440px at -5% 0%, rgba(201,109,29,0.12), transparent 58%),
        var(--bg);
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 32px 18px 60px; }}
    .hero, .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 22px; }}
    .hero {{ padding: 24px; margin-bottom: 18px; }}
    .hero h1 {{ margin: 0 0 10px; font-family: "Source Han Serif SC", "Noto Serif CJK SC", serif; font-size: clamp(2rem, 4vw, 3rem); }}
    .hero p {{ margin: 6px 0; color: var(--muted); }}
    .list {{ display: grid; gap: 14px; }}
    .card {{ padding: 18px; position: relative; box-shadow: 0 18px 44px rgba(39,28,20,0.08); }}
    .index {{ position: absolute; top: 12px; right: 16px; color: rgba(15,118,110,0.22); font-size: 2.2rem; font-weight: 700; }}
    .meta {{ color: var(--muted); margin: 8px 0; padding-right: 56px; }}
    .link {{ display: inline-flex; margin-top: 2px; color: white; background: var(--accent); padding: 8px 12px; border-radius: 999px; text-decoration: none; }}
    pre {{ white-space: pre-wrap; word-break: break-word; margin: 14px 0 0; font-family: inherit; line-height: 1.8; }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <h1>知乎搜索全文 - {html.escape(keyword)}</h1>
      <p>搜索链接: <a href="{html.escape(search_url)}" target="_blank" rel="noreferrer">{html.escape(search_url)}</a></p>
      <p>已抓取 {len(rows)} 条结果。系统先保存摘要和链接，再逐条打开详情页补全文。</p>
    </section>
    <section class="list">{''.join(cards)}</section>
  </main>
</body>
</html>"""


def write_outputs(run_dir: Path, keyword: str, search_url: str, rows: List[Dict], stage1_total: int) -> None:
    payload = {
        "keyword": keyword,
        "search_url": search_url,
        "hydrated_count": len(rows),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "results": rows,
    }
    write_json(run_dir / "results.json", payload)
    write_csv(run_dir / "results.csv", rows)
    write_markdown(run_dir / "all_results.md", keyword, search_url, rows)
    (run_dir / "article.html").write_text(build_html(keyword, search_url, rows), encoding="utf-8")
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                f"# 知乎搜索全文 - {keyword}",
                "",
                f"- 搜索链接: {search_url}",
                f"- 已收集摘要数量: {stage1_total}",
                f"- 已补全文数量: {len(rows)}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    output_base = Path(args.out_dir)
    output_base.mkdir(parents=True, exist_ok=True)
    run_dir = create_run_dir(output_base, args.keyword)
    search_url = make_search_url(args.keyword)
    print(f"Run directory: {run_dir.resolve()}")
    print(f"Search URL: {search_url}")
    print(f"目标: 收集前 {args.max_items} 条")

    cookies = parse_cookie_string(args.cookie)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=get_browser_args())
        context = browser.new_context(
            user_agent=args.user_agent,
            viewport={"width": 1440, "height": 1100},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        context.add_cookies(cookies)

        page = context.new_page()
        page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(args.page_delay_ms)
        detect_risk_or_login(page)
        print(f"搜索关键词: {args.keyword}")

        stage1_items = extract_search_results(
            page,
            args.max_items,
            args.max_scrolls,
            args.no_new_stop,
            args.page_delay_ms,
        )
        if not stage1_items:
            raise RuntimeError("没有抓到任何搜索结果。请检查 Cookie 是否有效，或确认该关键词在当前账号下有内容结果。")
        write_stage1(run_dir, args.keyword, search_url, stage1_items)
        print(f"成功收集 {len(stage1_items)} 条搜索结果（目标: {args.max_items}条）")
        print("开始第二阶段：逐条补全知乎全文")

        hydrated: List[Dict] = []
        failed: List[Dict] = []
        detail_page = context.new_page()
        total = len(stage1_items)
        write_progress(run_dir, total, 0, 0, 0)
        for index, item in enumerate(stage1_items, start=1):
            print(f"[FULLTEXT] {index}/{total} {index}")
            print(f"[DETAIL] {index}/{total} {item['url']}")
            try:
                hydrated_item = extract_detail(detail_page, item, args.detail_delay_ms)
                hydrated.append(hydrated_item)
            except TimeoutError as exc:
                failed.append({**item, "error": f"timeout: {exc}"})
            except Exception as exc:
                failed.append({**item, "error": str(exc)})

            write_progress(run_dir, total, index, len(hydrated), len(failed))
            if index % max(1, args.checkpoint_every) == 0 or index == total:
                write_outputs(run_dir, args.keyword, search_url, hydrated, total)

        detail_page.close()
        page.close()
        context.close()
        browser.close()

    write_outputs(run_dir, args.keyword, search_url, hydrated, total)
    write_json(run_dir / "failed_details.json", failed)
    print(f"补全文完成: 成功 {len(hydrated)} 条, 失败 {len(failed)} 条")
    print(f"Results stage1: {run_dir / 'results_stage1.json'}")
    print(f"Results JSON: {run_dir / 'results.json'}")
    print(f"Article HTML: {run_dir / 'article.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
