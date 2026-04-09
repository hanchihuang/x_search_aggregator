#!/usr/bin/env python3
"""搜索X.com关键词，返回前500条最新内容。"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

# 导入search_x.py中的功能
from search_x import (
    create_context,
    validate_auth_state,
    wait_for_search_results,
    get_cards,
    handle_search_error_retry,
    collect_tweets,
    summarize,
    make_search_url,
    fallback_search_via_input,
    write_csv,
    write_summary_md,
    safe_name,
    make_search_checkpoint_callback,
    checkpoint_search_outputs,
)
from search_x_long_runner import walk_collect


TRANSLATE_API_BASE = "https://translate.googleapis.com/translate_a/single"
ZH_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
TRANSIENT_NAV_ERRORS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_TIMED_OUT",
)


def looks_chinese(text: str) -> bool:
    if not text:
        return False
    cjk_count = len(ZH_RE.findall(text))
    latin_count = len(LATIN_RE.findall(text))
    return cjk_count > 0 and cjk_count >= latin_count


class ZhTranslator:
    def __init__(self) -> None:
        self.cache: dict[str, str] = {}

    def translate(self, text: str | None) -> str | None:
        if not text:
            return text
        normalized = re.sub(r"\s+", " ", str(text)).strip()
        if not normalized or looks_chinese(normalized):
            return normalized
        cached = self.cache.get(normalized)
        if cached is not None:
            return cached
        url = f"{TRANSLATE_API_BASE}?client=gtx&sl=auto&tl=zh-CN&dt=t&q={quote(normalized, safe='')}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            translated = "".join(part[0] for part in payload[0] if part and part[0]).strip()
            if translated:
                self.cache[normalized] = translated
                return translated
        except Exception:
            pass
        self.cache[normalized] = normalized
        return normalized


def resolve_search_input(keyword: str, search_url: str, lang: str) -> tuple[str, str, str]:
    search_url = (search_url or "").strip()
    keyword = (keyword or "").strip()
    if not search_url:
        if not keyword:
            raise ValueError("关键词和搜索链接不能同时为空。")
        return keyword, make_search_url(keyword, "Latest", lang), "Latest"

    parsed = urlparse(search_url)
    host = (parsed.netloc or "").lower()
    if host and host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError("搜索链接必须来自 x.com 或 twitter.com。")
    if "/search" not in (parsed.path or ""):
        raise ValueError("当前只支持搜索结果页链接。")

    qs = parse_qs(parsed.query)
    query_text = (qs.get("q") or [""])[0].strip() or keyword
    if not query_text:
        raise ValueError("搜索链接里缺少 q 参数。")
    raw_tab = (qs.get("f") or [""])[0].strip().lower()
    tab = "Latest" if raw_tab in {"live", "latest"} else ""
    path = parsed.path or "/search"
    normalized_url = f"https://x.com{path}"
    if parsed.query:
        normalized_url += f"?{parsed.query}"
    return query_text, normalized_url, tab


def clamp_range(start_rank: int, end_rank: int) -> tuple[int, int]:
    start = max(1, int(start_rank or 1))
    end = max(1, int(end_rank or 50))
    if end < start:
        start, end = end, start
    return start, end


def best_text(item: dict) -> str:
    for key in ("full_text", "text", "card_text"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def build_selected_items(items: list[dict], start_rank: int, end_rank: int, already_sliced: bool = False) -> list[dict]:
    translator = ZhTranslator()
    selected_items = []
    source_items = items if already_sliced else items[start_rank - 1:end_rank]
    for idx, item in enumerate(source_items, start=start_rank):
        original_text = best_text(item)
        selected_items.append(
            {
                "rank": idx,
                "tweet_id": item.get("tweet_id", ""),
                "url": item.get("url", ""),
                "user_name": item.get("user_name", ""),
                "user_handle": item.get("user_handle", ""),
                "posted_at": item.get("posted_at", ""),
                "reply_count": item.get("reply_count", 0),
                "retweet_count": item.get("retweet_count", 0),
                "like_count": item.get("like_count", 0),
                "bookmark_count": item.get("bookmark_count", 0),
                "view_count": item.get("view_count", 0),
                "text_original": original_text,
                "text_zh": translator.translate(original_text) or original_text,
            }
        )
    return selected_items


def write_selected_outputs(
    run_dir: Path,
    query_text: str,
    search_url: str,
    start_rank: int,
    end_rank: int,
    selected_items: list[dict],
) -> None:
    payload = {
        "query": query_text,
        "search_url": search_url,
        "start_rank": start_rank,
        "end_rank": end_rank,
        "count": len(selected_items),
        "items": selected_items,
    }
    (run_dir / "selected_zh.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        f"# X 搜索结果中文整理（第 {start_rank} 到第 {end_rank} 条）",
        "",
        f"- 搜索条件: {query_text}",
        f"- 搜索链接: {search_url}",
        f"- 实际返回: {len(selected_items)} 条",
        "",
    ]
    for item in selected_items:
        md_lines.extend(
            [
                f"## 第 {item['rank']} 条 - @{item['user_handle'] or 'unknown'}",
                f"- 时间: {item['posted_at'] or '未知'}",
                f"- 链接: {item['url'] or ''}",
                f"- 互动: 👍 {item['like_count']}  RT {item['retweet_count']}  回复 {item['reply_count']}",
                "",
                "### 中文版",
                item["text_zh"] or "（无内容）",
                "",
                "### 原文",
                item["text_original"] or "（无内容）",
                "",
            ]
        )
    (run_dir / "selected_zh.md").write_text("\n".join(md_lines), encoding="utf-8")

    cards = []
    for item in selected_items:
        cards.append(
            f"""
            <article class="card">
              <div class="meta">第 {item['rank']} 条 · @{html.escape(item['user_handle'] or 'unknown')} · {html.escape(item['posted_at'] or '未知时间')}</div>
              <div class="metrics">👍 {item['like_count']} · RT {item['retweet_count']} · 回复 {item['reply_count']} · <a href="{html.escape(item['url'] or '#')}" target="_blank" rel="noreferrer">原帖</a></div>
              <h3>中文版</h3>
              <p>{html.escape(item['text_zh'] or '（无内容）').replace(chr(10), '<br/>')}</p>
              <details>
                <summary>查看原文</summary>
                <p>{html.escape(item['text_original'] or '（无内容）').replace(chr(10), '<br/>')}</p>
              </details>
            </article>
            """
        )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>X 搜索结果中文整理</title>
  <style>
    :root {{
      --bg: #f6f3ee;
      --panel: #fffdf9;
      --ink: #1e1d1a;
      --muted: #6d665c;
      --line: #ddd4c7;
      --accent: #a4431f;
    }}
    body {{ margin: 0; background: radial-gradient(circle at top, #fff6e6 0, var(--bg) 42%, #efe7dd 100%); color: var(--ink); font-family: "Noto Serif SC", "Source Han Serif SC", serif; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 40px 20px 72px; }}
    h1 {{ font-size: 32px; margin-bottom: 10px; }}
    .intro {{ color: var(--muted); margin-bottom: 28px; line-height: 1.7; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 20px; margin: 18px 0; box-shadow: 0 12px 30px rgba(90, 67, 33, 0.08); }}
    .meta, .metrics {{ color: var(--muted); font-size: 14px; }}
    .metrics {{ margin: 8px 0 16px; }}
    h3 {{ margin: 14px 0 10px; color: var(--accent); }}
    p {{ line-height: 1.8; }}
    a {{ color: var(--accent); }}
    details summary {{ cursor: pointer; color: var(--accent); }}
  </style>
</head>
<body>
  <main>
    <h1>X 搜索结果中文整理</h1>
    <p class="intro">搜索条件：{html.escape(query_text)}<br/>区间：第 {start_rank} 到第 {end_rank} 条<br/>实际返回：{len(selected_items)} 条<br/><a href="{html.escape(search_url)}" target="_blank" rel="noreferrer">打开原始搜索链接</a></p>
    {''.join(cards)}
  </main>
</body>
</html>"""
    (run_dir / "selected_zh.html").write_text(page, encoding="utf-8")


def is_transient_navigation_error(exc: Exception) -> bool:
    message = str(exc or "")
    return any(token in message for token in TRANSIENT_NAV_ERRORS)


def safe_query_selector_exists(page, selector: str) -> bool:
    try:
        return bool(page.query_selector(selector))
    except Exception as exc:
        message = str(exc or "")
        if "Execution context was destroyed" in message or "Target page, context or browser has been closed" in message:
            return False
        raise


def navigate_with_retry(page, url: str, attempts: int = 3, logger=print, timeout: int = 120000, settle_ms: int = 3000) -> bool:
    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(settle_ms)
            return True
        except Exception as exc:
            last_exc = exc
            logger(f"警告: 页面加载超时或出错({exc})，尝试继续...")
            if not is_transient_navigation_error(exc) or attempt >= attempts:
                break
            page.wait_for_timeout(1200 * attempt)
    return False


def open_search_with_recovery(page, search_url: str, query_text: str, sort: str, lang: str) -> None:
    network_error_selectors = [
        'span:has-text("似乎你的连接已断开")',
        'span:has-text("我们将继续重试")',
        'span:has-text("Your connection was interrupted")',
        'span:has-text("We’ll keep retrying")',
    ]

    def has_network_interrupted() -> bool:
        return any(safe_query_selector_exists(page, selector) for selector in network_error_selectors)

    navigate_with_retry(page, search_url)

    for attempt in range(3):
        if has_network_interrupted():
            print(f"警告: 搜索页显示连接中断，执行第 {attempt + 1} 次重载...")
            navigate_with_retry(page, search_url, logger=lambda msg: print(msg.replace("页面加载", "重载搜索页")))
            continue
        break

    if wait_for_search_results(page):
        return

    print("警告: 搜索结果可能未正确加载，尝试使用输入框回退方法...")
    fallback_search_via_input(page, query_text, sort, lang)
    if wait_for_search_results(page, timeout=15000):
        return

    print("警告: 当前页回退失败，尝试跳转到 Explore 页重新发起搜索...")
    navigate_with_retry(page, "https://x.com/explore", logger=lambda msg: print(msg.replace("页面加载", "Explore 页加载")), settle_ms=2500)
    fallback_search_via_input(page, query_text, sort, lang)
    wait_for_search_results(page, timeout=20000)


def collect_items_from_search_responses(response_bodies: list[str]) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for body in response_bodies:
        if not body.strip():
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        batch: list[dict] = []
        walk_collect(data, batch)
        for item in batch:
            tid = str(item.get("tweet_id") or "").strip()
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            rows.append(item)
    return rows


def merge_items_with_network_recovery(dom_items: list[dict], network_items: list[dict]) -> tuple[list[dict], bool]:
    merged = list(dom_items)
    seen_ids = {str(item.get("tweet_id") or "").strip() for item in dom_items if str(item.get("tweet_id") or "").strip()}
    recovered = False
    for item in network_items:
        tweet_id = str(item.get("tweet_id") or "").strip()
        if not tweet_id or tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        merged.append(item)
        recovered = True
    return merged, recovered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="搜索X.com关键词，返回前500条最新内容",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python search_keyword_500.py --keyword "AI工具" --lang zh
  python search_keyword_500.py --keyword "python programming" --lang en
        """
    )
    parser.add_argument("--keyword", default="", help="搜索关键词")
    parser.add_argument("--search-url", default="", help="直接传入 X 搜索链接，支持 min_retweets 等条件")
    parser.add_argument("--lang", default="", help="语言过滤，例如: zh/en")
    parser.add_argument("--state", default="auth_state_cookie.json", help="Playwright存储状态路径")
    parser.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    parser.add_argument("--cdp-url", default="", help="连接现有 Chrome 的 CDP 地址，例如 http://127.0.0.1:9222")
    parser.add_argument("--auto-launch", action="store_true", help="若 CDP 不可用则自动拉起 Chrome")
    parser.add_argument("--out-dir", default="output", help="输出基础目录")
    parser.add_argument("--max-scrolls", type=int, default=200, help="最大滚动轮数")
    parser.add_argument("--no-new-stop", type=int, default=10, help="连续N轮无新推文后停止")
    parser.add_argument("--scroll-pause", type=int, default=2000, help="滚动间隔（毫秒）")
    parser.add_argument("--start-rank", type=int, default=1, help="返回区间起始序号，默认 1")
    parser.add_argument("--end-rank", type=int, default=50, help="返回区间结束序号，默认 50")
    parser.add_argument("--skip-fulltext", action="store_true", help="跳过第二阶段全文补全")
    parser.add_argument("--fulltext-delay-ms", type=int, default=1200, help="打开推文详情页后的等待毫秒数")
    parser.add_argument("--fulltext-checkpoint-every", type=int, default=10, help="每补全N条写一次检查点")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    query_text, search_url, sort = resolve_search_input(args.keyword, args.search_url, args.lang)
    start_rank, end_rank = clamp_range(args.start_rank, args.end_rank)
    max_items = end_rank
    recovered_from_network = False

    out_base = Path(args.out_dir).expanduser().resolve()
    run_dir = out_base / f"{safe_name(query_text)}_{start_rank}_{end_rank}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"运行目录: {run_dir}")

    print(f"搜索关键词: {query_text}")
    print(f"搜索URL: {search_url}")
    print(f"目标: 至少收集到第{end_rank}条内容")
    print(f"返回区间: 第{start_rank}到第{end_rank}条中文版内容")
    print("=" * 60)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = create_context(
            p,
            args.state,
            args.headless,
            cdp_url=args.cdp_url,
            auto_launch=args.auto_launch,
        )
        page = context.new_page()
        search_response_bodies: list[str] = []

        def on_response(resp) -> None:
            if "/SearchTimeline?" not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:
                return
            if body and body not in search_response_bodies:
                search_response_bodies.append(body)

        page.on("response", on_response)

        # 导航到搜索URL，并在必要时回退到 Explore 页重试。
        open_search_with_recovery(page, search_url, query_text, sort, args.lang)
        
        # 验证认证状态
        if not validate_auth_state(page):
            print("错误: 检测到认证问题。请使用 login_x.py 刷新登录状态。")
            context.close()
            sys.exit(1)
        
        # 如果搜索时间线暂时出错，尝试重试按钮恢复
        if not get_cards(page):
            handle_search_error_retry(page, attempts=5)
        
        # 收集推文
        items = collect_tweets(
            page=page,
            max_items=max_items,
            max_scrolls=args.max_scrolls,
            no_new_stop=args.no_new_stop,
            scroll_pause=args.scroll_pause,
            checkpoint_cb=make_search_checkpoint_callback(run_dir, query_text),
        )
        if search_response_bodies:
            network_items = collect_items_from_search_responses(search_response_bodies)
            if not items and network_items:
                print(f"列表页未渲染出卡片，尝试从 {len(search_response_bodies)} 个 SearchTimeline 网络响应中恢复结果...")
                items = network_items
                recovered_from_network = True
            elif items and network_items:
                merged_items, recovered_more = merge_items_with_network_recovery(items, network_items)
                if recovered_more:
                    recovered_count = len(merged_items) - len(items)
                    print(
                        f"检测到 DOM 滚动结果可能不完整，已从 {len(search_response_bodies)} 个 SearchTimeline 网络响应补回 {recovered_count} 条结果..."
                    )
                    items = merged_items
        context.close()

    if not items:
        print("警告: 未收集到任何推文。请检查您的认证和网络连接。")
        sys.exit(1)

    # 确保只返回前500条（按时间排序，最新的在前）
    from datetime import timezone as tz
    from search_x import to_dt
    epoch = datetime(1970, 1, 1, tzinfo=tz.utc)
    items_with_time = [(item, to_dt(item.get("posted_at"))) for item in items]
    items_with_time.sort(
        key=lambda x: x[1].replace(tzinfo=tz.utc) if x[1] and x[1].tzinfo is None else (x[1] if x[1] else epoch),
        reverse=True,
    )
    items = [item for item, _ in items_with_time[:max_items]]

    print(f"成功收集 {len(items)} 条推文（目标: {max_items}条）")

    if len(items) < start_rank:
        print(
            f"错误: 仅收集到 {len(items)} 条内容，未达到你要求的起始序号第 {start_rank} 条。"
        )
        sys.exit(2)

    requested_items = items[start_rank - 1:end_rank]
    print(f"按请求区间实际返回 {len(requested_items)} 条内容")

    stage1_json_path = run_dir / "results_stage1.json"
    stage1_json_path.write_text(json.dumps(requested_items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"阶段1结果已保存: {stage1_json_path}")

    if recovered_from_network:
        print("检测到结果来自 SearchTimeline 网络恢复，自动跳过第二阶段全文补全。")
    elif not args.skip_fulltext:
        from tweet_fulltext import hydrate_items_with_fulltext

        print("开始第二阶段：逐条补全推文全文...")
        with sync_playwright() as p:
            context = create_context(p, args.state, args.headless)
            requested_items = hydrate_items_with_fulltext(
                context=context,
                items=requested_items,
                run_dir=run_dir,
                checkpoint_every=args.fulltext_checkpoint_every,
                delay_ms=args.fulltext_delay_ms,
                logger=print,
            )
            context.close()

    # 生成摘要
    summary = summarize(requested_items, query_text)

    # 保存结果
    json_path = run_dir / "results.json"
    csv_path = run_dir / "results.csv"
    summary_json = run_dir / "summary.json"
    summary_md = run_dir / "summary.md"
    article_html = run_dir / "article.html"

    checkpoint_search_outputs(run_dir, requested_items, query_text)
    selected_items = build_selected_items(requested_items, start_rank, end_rank, already_sliced=True)
    write_selected_outputs(run_dir, query_text, search_url, start_rank, end_rank, selected_items)

    print("=" * 60)
    print(f"完成！按区间返回 {len(requested_items)} 条推文。")
    print(f"区间中文JSON: {run_dir / 'selected_zh.json'}")
    print(f"区间中文Markdown: {run_dir / 'selected_zh.md'}")
    print(f"区间中文HTML: {run_dir / 'selected_zh.html'}")
    print(f"结果JSON:   {json_path}")
    print(f"结果CSV:    {csv_path}")
    print(f"摘要JSON:   {summary_json}")
    print(f"摘要Markdown: {summary_md}")
    print(f"摘要文章:   {article_html}")
    print("=" * 60)


if __name__ == "__main__":
    main()
