#!/usr/bin/env python3
"""Crawl all answer full texts from a Zhihu question page."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib import parse as urlparse
from urllib import request as urlrequest

from playwright.sync_api import TimeoutError, sync_playwright

from browser_config import get_browser_args

QUESTION_URL_RE = re.compile(r"^https?://www\.zhihu\.com/question/(\d+)(?:[/?#].*)?$")
ANSWER_URL_RE = re.compile(r"https?://www\.zhihu\.com/question/(\d+)/answer/(\d+)")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl all answer full texts from a Zhihu question.")
    parser.add_argument("--question-url", required=True, help="Zhihu question URL, e.g. https://www.zhihu.com/question/547768388")
    parser.add_argument("--cookie", required=True, help="Cookie header copied from the browser request")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Optional browser user agent")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--out-dir", default="output", help="Output base directory")
    parser.add_argument("--max-scrolls", type=int, default=80, help="Max scroll rounds on the question page")
    parser.add_argument("--no-new-stop", type=int, default=6, help="Stop after N rounds without new answer links")
    parser.add_argument("--page-delay-ms", type=int, default=1800, help="Wait time after each page action in ms")
    return parser.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:80] or "zhihu"


def ensure_question_url(url: str) -> Tuple[str, str]:
    clean = str(url or "").strip()
    match = QUESTION_URL_RE.match(clean)
    if not match:
        raise ValueError("问题链接格式不正确，必须是 https://www.zhihu.com/question/<id>")
    return clean.rstrip("/"), match.group(1)


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
                "domain": ".zhihu.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    if not cookies:
        raise ValueError("Cookie 为空或格式不正确。请粘贴浏览器请求头里的完整 Cookie 字符串。")
    return cookies


def cookie_header_from_string(cookie_string: str) -> str:
    parts = []
    for chunk in str(cookie_string or "").split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            parts.append(f"{name}={value}")
    if not parts:
        raise ValueError("Cookie 为空或格式不正确。请粘贴浏览器请求头里的完整 Cookie 字符串。")
    return "; ".join(parts)


def create_run_dir(base_dir: Path, question_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_dir / f"zhihu_question_{question_id}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def fetch_answer_urls_via_api(question_id: str, cookie_header: str, user_agent: str, page_delay_ms: int) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    limit = 20
    offset = 0
    totals = 0
    page_no = 0
    while True:
        page_no += 1
        next_url = (
            f"https://www.zhihu.com/api/v4/questions/{question_id}/answers"
            f"?limit={limit}&offset={offset}&sort_by=default&include=data[*].author.name"
        )
        req = urlrequest.Request(
            next_url,
            headers={
                "Cookie": cookie_header,
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://www.zhihu.com/question/{question_id}",
                "X-Requested-With": "fetch",
            },
        )
        with urlrequest.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
        data = list(payload.get("data") or [])
        new_items = 0
        for item in data:
            answer_id = str(item.get("id") or "").strip()
            if not answer_id:
                continue
            answer_url = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
            if answer_url in seen:
                continue
            seen.add(answer_url)
            found.append(answer_url)
            new_items += 1
        totals = int(payload.get("paging", {}).get("totals", totals) or totals or 0)
        print(f"API page {page_no}: + {new_items} new, total {len(found)} / {totals or '?'}")
        if totals and len(found) >= totals:
            break
        if not data or new_items == 0:
            break
        offset += limit
    return found


def scroll_question_page(page, question_id: str, max_scrolls: int, no_new_stop: int, page_delay_ms: int) -> List[str]:
    found: set[str] = set()
    no_new_rounds = 0
    current_height = 0
    for round_no in range(1, max_scrolls + 1):
        click_expand_buttons(page)
        current_round = extract_answer_urls(page, question_id)
        new_items = current_round - found
        found.update(current_round)
        print(f"Page {round_no}: + {len(new_items)} new, total {len(found)}")
        if new_items:
            no_new_rounds = 0
        else:
            no_new_rounds += 1
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(page_delay_ms)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == current_height and no_new_rounds >= no_new_stop:
            break
        current_height = new_height
    return sorted(found, key=answer_sort_key)


def click_expand_buttons(page) -> None:
    labels = ["阅读全文", "展开阅读全文", "显示全部", "查看全部"]
    for label in labels:
        locator = page.locator(f'button:has-text("{label}"), div[role="button"]:has-text("{label}")')
        count = min(locator.count(), 8)
        for idx in range(count):
            try:
                locator.nth(idx).click(timeout=800)
                page.wait_for_timeout(120)
            except Exception:
                continue


def extract_answer_urls(page, question_id: str) -> set[str]:
    links = page.eval_on_selector_all(
        "a[href]",
        """elements => elements
            .map(el => el.href || "")
            .filter(Boolean)
        """,
    )
    result = set()
    prefix = f"https://www.zhihu.com/question/{question_id}/answer/"
    for href in links:
        if not href.startswith(prefix):
            continue
        match = ANSWER_URL_RE.match(href)
        if match:
            result.add(match.group(0))
    return result


def answer_sort_key(url: str) -> Tuple[int, int]:
    match = ANSWER_URL_RE.match(url)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def detect_risk_or_login(page) -> None:
    body_text = (page.locator("body").inner_text(timeout=3000) or "").strip()
    risk_markers = [
        "您当前请求存在异常",
        "暂时限制本次访问",
        "验证你是不是机器人",
        "登录后即可查看",
    ]
    if any(marker in body_text for marker in risk_markers):
        raise RuntimeError("知乎返回了风控或登录校验页面。请更新 Cookie 后重试。")


def first_text(page, selectors: Iterable[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for idx in range(min(count, 4)):
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
        for idx in range(min(count, 8)):
            try:
                text = (locator.nth(idx).inner_text(timeout=1000) or "").strip()
            except Exception:
                continue
            if len(text) > len(best):
                best = text
    return best


def extract_vote_count(page) -> str:
    candidates = [
        'button:has-text("赞同")',
        '[aria-label*="赞同"]',
        'text=/\\d+\\s*人赞同了该回答/',
    ]
    for selector in candidates:
        locator = page.locator(selector)
        count = locator.count()
        for idx in range(min(count, 4)):
            try:
                text = (locator.nth(idx).inner_text(timeout=800) or "").strip()
            except Exception:
                continue
            if text:
                return text
    return ""


def extract_answer(page, answer_url: str, question_title_hint: str, page_delay_ms: int) -> Dict:
    page.goto(answer_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(page_delay_ms)
    detect_risk_or_login(page)
    click_expand_buttons(page)
    page.wait_for_timeout(500)

    title = first_text(page, ["h1.QuestionHeader-title", "h1"]) or question_title_hint
    author = first_text(
        page,
        [
            ".AuthorInfo-content .UserLink-link",
            ".AuthorInfo-content",
            "a[href*='/people/']",
        ],
    )
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
    if not content:
        raise RuntimeError(f"回答页未提取到正文：{answer_url}")

    times = []
    time_locator = page.locator("time")
    for idx in range(min(time_locator.count(), 4)):
        try:
            text = (time_locator.nth(idx).inner_text(timeout=500) or "").strip()
        except Exception:
            continue
        if text:
            times.append(text)

    parsed = ANSWER_URL_RE.match(answer_url)
    answer_id = parsed.group(2) if parsed else ""
    return {
        "question_title": title,
        "question_url": answer_url.rsplit("/answer/", 1)[0],
        "answer_id": answer_id,
        "answer_url": answer_url,
        "author": author,
        "vote_text": extract_vote_count(page),
        "times": times,
        "content": content,
        "content_length": len(content),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: List[Dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "answer_id",
                "author",
                "vote_text",
                "times",
                "answer_url",
                "content_length",
                "content",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "answer_id": row.get("answer_id", ""),
                    "author": row.get("author", ""),
                    "vote_text": row.get("vote_text", ""),
                    "times": " | ".join(row.get("times", [])),
                    "answer_url": row.get("answer_url", ""),
                    "content_length": row.get("content_length", 0),
                    "content": row.get("content", ""),
                }
            )


def write_markdown(path: Path, question_title: str, question_url: str, rows: List[Dict]) -> None:
    parts = [
        f"# {question_title}",
        "",
        f"- 问题链接: {question_url}",
        f"- 回答数量: {len(rows)}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        parts.extend(
            [
                f"## {index}. {row.get('author') or '匿名用户'}",
                "",
                f"- 回答链接: {row.get('answer_url', '')}",
                f"- 赞同信息: {row.get('vote_text', '') or '-'}",
                f"- 时间信息: {' | '.join(row.get('times', [])) or '-'}",
                "",
                row.get("content", ""),
                "",
            ]
        )
    path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")


def build_html(question_title: str, question_url: str, rows: List[Dict]) -> str:
    cards = []
    for index, row in enumerate(rows, start=1):
        meta = " | ".join(x for x in [row.get("author", ""), row.get("vote_text", ""), " / ".join(row.get("times", []))] if x)
        cards.append(
            f"""
            <article class="card">
              <div class="index">{index}</div>
              <div class="meta">{html.escape(meta or '无元数据')}</div>
              <a class="link" href="{html.escape(row.get('answer_url', '#'))}" target="_blank" rel="noreferrer">打开原回答</a>
              <pre>{html.escape(row.get("content", ""))}</pre>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(question_title)} - 知乎回答全文</title>
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
    .hero, .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 22px;
      backdrop-filter: blur(10px);
      box-shadow: 0 18px 44px rgba(39,28,20,0.08);
    }}
    .hero {{ padding: 24px; margin-bottom: 18px; }}
    .hero h1 {{
      margin: 0 0 10px;
      font-family: "Source Han Serif SC", "Noto Serif CJK SC", serif;
      font-size: clamp(2rem, 4vw, 3rem);
    }}
    .hero p {{ margin: 6px 0; color: var(--muted); }}
    .list {{ display: grid; gap: 14px; }}
    .card {{ padding: 18px; position: relative; }}
    .index {{
      position: absolute;
      top: 14px;
      right: 16px;
      color: rgba(15,118,110,0.22);
      font-size: 2.2rem;
      font-weight: 700;
    }}
    .meta {{ color: var(--muted); padding-right: 56px; }}
    .link {{
      display: inline-flex;
      margin-top: 10px;
      color: white;
      background: var(--accent);
      padding: 8px 12px;
      border-radius: 999px;
      text-decoration: none;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 14px 0 0;
      font-family: inherit;
      line-height: 1.8;
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <h1>{html.escape(question_title)}</h1>
      <p>问题链接: <a href="{html.escape(question_url)}" target="_blank" rel="noreferrer">{html.escape(question_url)}</a></p>
      <p>已抓取回答 {len(rows)} 条，以下内容为逐条打开回答页后提取的完整正文文本。</p>
    </section>
    <section class="list">
      {''.join(cards)}
    </section>
  </main>
</body>
</html>"""


def write_outputs(run_dir: Path, question_title: str, question_url: str, rows: List[Dict]) -> None:
    payload = {
        "question_title": question_title,
        "question_url": question_url,
        "answer_count": len(rows),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "answers": rows,
    }
    write_json(run_dir / "results.json", payload)
    write_csv(run_dir / "results.csv", rows)
    write_markdown(run_dir / "all_answers.md", question_title, question_url, rows)
    (run_dir / "article.html").write_text(build_html(question_title, question_url, rows), encoding="utf-8")
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                f"# {question_title}",
                "",
                f"- 问题链接: {question_url}",
                f"- 回答数量: {len(rows)}",
                f"- 输出目录: {run_dir}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    question_url, question_id = ensure_question_url(args.question_url)
    cookie_header = cookie_header_from_string(args.cookie)
    cookies = parse_cookie_string(args.cookie)
    output_base = Path(args.out_dir)
    output_base.mkdir(parents=True, exist_ok=True)
    run_dir = create_run_dir(output_base, question_id)
    print(f"Run directory: {run_dir.resolve()}")
    print(f"Question URL: {question_url}")

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
        page.goto(question_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(args.page_delay_ms)
        detect_risk_or_login(page)

        question_title = first_text(page, ["h1.QuestionHeader-title", "h1"]) or f"知乎问题 {question_id}"
        print(f"Question title: {question_title}")
        answer_urls: List[str] = []
        try:
            answer_urls = fetch_answer_urls_via_api(question_id, cookie_header, args.user_agent, args.page_delay_ms)
        except Exception as exc:
            print(f"[SYSTEM] 知乎回答 API 拉取失败，回退到页面滚动模式: {exc}")
        if not answer_urls:
            answer_urls = scroll_question_page(page, question_id, args.max_scrolls, args.no_new_stop, args.page_delay_ms)
        if not answer_urls:
            raise RuntimeError("没有抓到任何回答链接。请检查 Cookie 是否仍然有效，或确认该问题页在当前账号下可见。")
        print(f"Discovered {len(answer_urls)} answer links")

        answers: List[Dict] = []
        detail_page = context.new_page()
        for index, answer_url in enumerate(answer_urls, start=1):
            print(f"[ANSWER] {index}/{len(answer_urls)} {answer_url}")
            try:
                answer = extract_answer(detail_page, answer_url, question_title, args.page_delay_ms)
            except TimeoutError as exc:
                raise RuntimeError(f"回答页打开超时：{answer_url}") from exc
            answers.append(answer)
            write_outputs(run_dir, question_title, question_url, answers)

        detail_page.close()
        page.close()
        context.close()
        browser.close()

    print(f"Saved {len(answers)} answers")
    print(f"Results JSON: {run_dir / 'results.json'}")
    print(f"Article HTML: {run_dir / 'article.html'}")
    print(f"Markdown: {run_dir / 'all_answers.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
