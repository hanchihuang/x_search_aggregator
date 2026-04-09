#!/usr/bin/env python3
"""Fetch Folo timeline data with a user-provided cookie and generate local HTML reports."""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote
from urllib.request import Request, urlopen

API_BASE = "https://api.folo.is"
TRANSLATE_API_BASE = "https://translate.googleapis.com/translate_a/single"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

VIEW_OPTIONS = {
    0: "文章",
    1: "社交",
    2: "图片",
    3: "视频",
}

VIEW_SLUGS = {
    0: "articles",
    1: "social",
    2: "pictures",
    3: "videos",
}

STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "you", "your", "are",
    "into", "have", "has", "will", "its", "was", "but", "they", "their", "about",
    "what", "when", "where", "than", "then", "after", "before", "also", "more",
    "less", "just", "over", "under", "onto", "update", "adds", "new", "how",
    "why", "can", "all", "not", "out", "via", "too", "now",
}

EFFICIENCY_TERMS = {
    "agent", "agents", "workflow", "automation", "automate", "productivity",
    "tool", "tools", "plugin", "plugins", "github", "actions", "cli",
    "code", "coding", "developer", "developers", "deploy", "deployment",
    "integration", "integrations", "dashboard", "platform", "prompt",
    "prompts", "orchestration", "ci", "cd", "testing", "review", "infra",
    "sdk", "api", "mcp", "copilot", "claude", "codex",
}

AI_RESEARCH_TERMS = {
    "llm", "llms", "agi", "transformer", "transformers", "model", "models",
    "training", "inference", "reasoning", "alignment", "eval", "evaluation",
    "benchmark", "benchmarks", "agentic", "retrieval", "embedding",
    "finetuning", "fine-tuning", "distillation", "multimodal", "diffusion",
    "rl", "rlhf", "policy", "policies", "research", "paper", "papers",
    "openai", "anthropic", "deepmind", "architecture", "token", "tokens",
    "memory", "planning", "generalization", "causal",
}

TERM_TRANSLATIONS = {
    "agent": "智能体",
    "agents": "智能体",
    "workflow": "工作流",
    "automation": "自动化",
    "automate": "自动化",
    "productivity": "生产力",
    "tool": "工具",
    "tools": "工具",
    "plugin": "插件",
    "plugins": "插件",
    "github": "代码托管平台",
    "actions": "自动化流程",
    "cli": "命令行",
    "code": "代码",
    "coding": "编程",
    "developer": "开发者",
    "developers": "开发者",
    "deploy": "部署",
    "deployment": "部署",
    "integration": "集成",
    "integrations": "集成",
    "dashboard": "仪表盘",
    "platform": "平台",
    "prompt": "提示词",
    "prompts": "提示词",
    "orchestration": "编排",
    "testing": "测试",
    "review": "审查",
    "infra": "基础设施",
    "sdk": "开发工具包",
    "api": "编程接口",
    "mcp": "模型上下文协议",
    "copilot": "编程助手",
    "claude": "智能助手",
    "codex": "代码助手",
    "llm": "大语言模型",
    "llms": "大语言模型",
    "agi": "通用人工智能",
    "transformer": "变换器架构",
    "transformers": "变换器架构",
    "model": "模型",
    "models": "模型",
    "training": "训练",
    "inference": "推理",
    "reasoning": "推理",
    "alignment": "对齐",
    "eval": "评测",
    "evaluation": "评测",
    "benchmark": "基准测试",
    "benchmarks": "基准测试",
    "agentic": "智能体化",
    "retrieval": "检索",
    "embedding": "嵌入",
    "finetuning": "微调",
    "fine-tuning": "微调",
    "distillation": "蒸馏",
    "multimodal": "多模态",
    "diffusion": "扩散模型",
    "rl": "强化学习",
    "rlhf": "基于人类反馈的强化学习",
    "policy": "策略",
    "policies": "策略",
    "research": "研究",
    "paper": "论文",
    "papers": "论文",
    "openai": "某头部人工智能公司",
    "anthropic": "某人工智能研究公司",
    "deepmind": "某研究实验室",
    "architecture": "架构",
    "token": "词元",
    "tokens": "词元",
    "memory": "记忆",
    "planning": "规划",
    "generalization": "泛化",
    "causal": "因果",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Folo timeline and build HTML summary.")
    parser.add_argument("--cookie", required=True, help="Full Folo cookie header")
    parser.add_argument("--view", type=int, default=0, help="Timeline view: 0 articles, 1 social, 2 pictures, 3 videos")
    parser.add_argument("--limit", type=int, default=20, help="Number of entries to keep in report")
    return parser.parse_args()


def http_json(path: str, cookie: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
        "Origin": "https://app.folo.is",
        "Referer": "https://app.folo.is/",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{API_BASE}{path}", data=data, method=method, headers=headers)
    with urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset))


def extract_response_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    if isinstance(payload, list):
        return payload
    return []


def extract_next_cursor(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get("nextCursor"),
        payload.get("cursor"),
    ]
    for key in ("paging", "pagination", "pageInfo", "meta"):
        node = payload.get(key)
        if isinstance(node, dict):
            candidates.extend(
                [
                    node.get("nextCursor"),
                    node.get("next_cursor"),
                    node.get("cursor"),
                    node.get("endCursor"),
                    node.get("end_cursor"),
                ]
            )
    for value in candidates:
        if value not in (None, "", False):
            return str(value)
    return ""


def fetch_entries(cookie: str, view: int, limit: int) -> list[dict[str, Any]]:
    target = max(5, min(100, int(limit)))
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor = ""

    for page_no in range(1, 9):
        payload: dict[str, Any] = {
            "view": view,
            "limit": target,
            "pageSize": target,
            "perPage": target,
            "take": target,
        }
        if cursor:
            payload["cursor"] = cursor
        resp = http_json("/entries", cookie, method="POST", payload=payload)
        batch = extract_response_items(resp)
        if not batch:
            break

        new_count = 0
        for item in batch:
            entry = item.get("entries", {}) if isinstance(item, dict) else {}
            entry_id = str(entry.get("id") or item.get("id") or "").strip()
            dedupe_key = entry_id or json.dumps(item, ensure_ascii=False, sort_keys=True)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            items.append(item)
            new_count += 1
            if len(items) >= target:
                return items[:target]

        next_cursor = extract_next_cursor(resp)
        if new_count == 0 or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return items[:target]


def compact_entry(item: dict[str, Any]) -> dict[str, Any]:
    entry = item.get("entries", {}) or {}
    feed = item.get("feeds", {}) or {}
    subscription = item.get("subscriptions", {}) or {}
    media = entry.get("media") or []
    return {
        "id": entry.get("id"),
        "title": entry.get("title"),
        "titleOriginal": entry.get("title"),
        "url": entry.get("url"),
        "description": entry.get("description"),
        "descriptionOriginal": entry.get("description"),
        "summary": entry.get("summary"),
        "summaryOriginal": entry.get("summary"),
        "publishedAt": entry.get("publishedAt"),
        "insertedAt": entry.get("insertedAt"),
        "feedTitle": feed.get("title"),
        "feedTitleOriginal": feed.get("title"),
        "feedSiteUrl": feed.get("siteUrl"),
        "feedImage": feed.get("image"),
        "category": subscription.get("category"),
        "categoryOriginal": subscription.get("category"),
        "read": item.get("read"),
        "mediaCount": len(media),
    }


def looks_chinese(text: str) -> bool:
    if not text:
        return False
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    return cjk_count > 0 and cjk_count >= latin_count


def contains_latin(text: str | None) -> bool:
    return bool(text and re.search(r"[A-Za-z]", text))


def chinese_display_text(
    text: str | None,
    fallback: str,
    translator: "ZhTranslator",
) -> str:
    if not text:
        return fallback
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return fallback
    translated = translator.translate(normalized) or normalized
    translated = re.sub(r"\s+", " ", translated).strip()
    if looks_chinese(translated) or not contains_latin(translated):
        return translated
    return fallback


class ZhTranslator:
    def __init__(self) -> None:
        self.cache: dict[str, str] = {}

    def translate(self, text: str | None) -> str | None:
        if not text:
            return text
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or looks_chinese(normalized):
            return text
        cached = self.cache.get(normalized)
        if cached is not None:
            return cached

        query = quote(normalized, safe="")
        url = f"{TRANSLATE_API_BASE}?client=gtx&sl=auto&tl=zh-CN&dt=t&q={query}"
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

        self.cache[normalized] = text
        return text


def localize_entries(entries: list[dict[str, Any]], translator: ZhTranslator) -> None:
    for entry in entries:
        title = chinese_display_text(entry.get("titleOriginal"), "标题待补充", translator)
        summary = chinese_display_text(entry.get("summaryOriginal"), "", translator)
        description = chinese_display_text(entry.get("descriptionOriginal"), "暂无中文说明", translator)
        feed_title = chinese_display_text(entry.get("feedTitleOriginal"), "来源待翻译", translator)
        category = chinese_display_text(entry.get("categoryOriginal"), "未分类", translator)
        entry["titleZh"] = title
        entry["summaryZh"] = summary
        entry["descriptionZh"] = description
        entry["feedTitleZh"] = feed_title
        entry["categoryZh"] = category
        entry["title"] = title
        entry["summary"] = summary
        entry["description"] = description
        entry["feedTitle"] = feed_title
        entry["category"] = category


def localize_subscriptions(subscriptions: list[dict[str, Any]], translator: ZhTranslator) -> None:
    for subscription in subscriptions:
        category = subscription.get("category")
        subscription["category"] = chinese_display_text(category, "未分类", translator)


def localize_keywords(keywords: list[str], translator: ZhTranslator) -> list[str]:
    localized = []
    for keyword in keywords:
        translated = TERM_TRANSLATIONS.get(keyword) or chinese_display_text(keyword, "", translator)
        if translated:
            localized.append(translated)
    return localized


def top_keywords(entries: list[dict[str, Any]], limit: int = 10) -> list[str]:
    bag: Counter[str] = Counter()
    for entry in entries:
        text = " ".join(
            part for part in [entry.get("titleOriginal"), entry.get("summaryOriginal"), entry.get("descriptionOriginal")] if part
        ).lower()
        words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", text)
        for word in words:
            if word not in STOPWORDS:
                bag[word] += 1
    return [word for word, _ in bag.most_common(limit)]


def score_entry(entry: dict[str, Any], terms: set[str]) -> tuple[int, list[str]]:
    text = " ".join(
        part for part in [entry.get("titleOriginal"), entry.get("summaryOriginal"), entry.get("descriptionOriginal")] if part
    ).lower()
    matched = sorted({term for term in terms if term in text})
    score = len(matched) * 3
    if entry.get("summary"):
        score += 2
    if entry.get("feedTitle"):
        score += 1
    if entry.get("mediaCount"):
        score += 1
    return score, matched


def build_efficiency_reason(entry: dict[str, Any], matched_terms: list[str]) -> str:
    parts = []
    title = (entry.get("titleOriginal") or "").lower()
    summary = (entry.get("summaryOriginal") or entry.get("descriptionOriginal") or "").lower()
    if any(term in matched_terms for term in ["agent", "agents", "automation", "workflow", "orchestration"]):
        parts.append("这条内容直接涉及智能体、自动化或工作流设计，适合用来改进日常开发流程。")
    if any(term in matched_terms for term in ["plugin", "plugins", "mcp", "cli", "sdk", "api"]):
        parts.append("它更偏工具落地，能帮助你把能力接进现有开发环境，而不是停留在概念层。")
    if any(term in matched_terms for term in ["claude", "codex", "copilot", "code", "coding"]):
        parts.append("内容和智能编程助手或代码生成直接相关，通常对提速写码、审查和协作最有帮助。")
    if "github" in matched_terms or "best-practice" in title or "best practice" in summary:
        parts.append("它还带有较强的工程实践属性，适合直接拿来参考、复用或对照优化。")
    if not parts:
        parts.append("这条内容和开发工具链或工程效率存在直接关联，适合优先阅读。")
    return "".join(parts)


def build_research_reason(entry: dict[str, Any], matched_terms: list[str]) -> str:
    parts = []
    summary = (entry.get("summaryOriginal") or entry.get("descriptionOriginal") or "").lower()
    if any(term in matched_terms for term in ["llm", "llms", "transformer", "transformers", "model", "models"]):
        parts.append("这条内容更接近模型层讨论，能帮助你理解当前主流方法的能力边界和结构特点。")
    if any(term in matched_terms for term in ["training", "inference", "reasoning", "evaluation", "benchmark", "benchmarks"]):
        parts.append("它覆盖训练、推理、推理机制或评测问题，对研究思路和实验设计有启发。")
    if any(term in matched_terms for term in ["agentic", "planning", "memory", "retrieval"]):
        parts.append("内容涉及智能体系统能力扩展，例如规划、记忆或检索，这类主题对做人工智能系统研究很关键。")
    if any(term in matched_terms for term in ["alignment", "rlhf", "policy", "policies"]):
        parts.append("如果你关注对齐、策略优化或行为控制，这条内容的研究相关性会比较强。")
    if "paper" in matched_terms or "research" in matched_terms or "openai" in matched_terms or "anthropic" in matched_terms:
        parts.append("来源或表述本身带有明显研究导向，适合用于跟踪行业前沿和形成问题意识。")
    if "deep dive" in summary or "under the hood" in summary:
        parts.append("它还不是纯新闻摘要，而是偏机制解释型内容，适合深入看。")
    if not parts:
        parts.append("这条内容和人工智能方法、系统能力或研究方向相关，值得作为研究线索保存。")
    return "".join(parts)


def curated_highlights(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    efficiency = []
    research = []
    for entry in entries:
        eff_score, eff_terms = score_entry(entry, EFFICIENCY_TERMS)
        res_score, res_terms = score_entry(entry, AI_RESEARCH_TERMS)
        if eff_terms:
            efficiency.append({
                "entry": entry,
                "score": eff_score,
                "matchedTerms": [TERM_TRANSLATIONS.get(term, "相关主题") for term in eff_terms[:8]],
                "reason": build_efficiency_reason(entry, eff_terms[:8]),
            })
        if res_terms:
            research.append({
                "entry": entry,
                "score": res_score,
                "matchedTerms": [TERM_TRANSLATIONS.get(term, "相关主题") for term in res_terms[:8]],
                "reason": build_research_reason(entry, res_terms[:8]),
            })
    efficiency.sort(key=lambda item: item["score"], reverse=True)
    research.sort(key=lambda item: item["score"], reverse=True)
    return {"efficiency": efficiency[:6], "research": research[:6]}


def generate_summary(
    session: dict[str, Any],
    subscriptions: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    view: int,
    total_entries: int,
    translator: ZhTranslator,
) -> dict[str, Any]:
    user = session.get("user", {}) if isinstance(session, dict) else {}
    category_counts = Counter((item.get("category") or "未分类") for item in subscriptions)
    source_counts = Counter(entry.get("feedTitle") or "未知来源" for entry in entries)
    unread_count = sum(1 for entry in entries if not entry.get("read"))
    keywords = localize_keywords(top_keywords(entries), translator)
    highlights = curated_highlights(entries)

    top_sources = [{"name": name, "count": count} for name, count in source_counts.most_common(6)]
    top_categories = [{"name": name, "count": count} for name, count in category_counts.most_common(8)]

    summary_lines = [
        f"{user.get('name') or '当前用户'} 当前共订阅 {len(subscriptions)} 个源，本次抓取了 {len(entries)} 条 {VIEW_OPTIONS.get(view, '时间线')} 时间线内容。",
    ]
    if unread_count:
        summary_lines.append(f"当前窗口中未读条目约 {unread_count} 条，适合优先处理最新更新。")
    if top_sources:
        summary_lines.append("近期最活跃的来源是 " + "、".join(f"{row['name']}（{row['count']}）" for row in top_sources[:3]) + "。")
    if top_categories:
        summary_lines.append("订阅分类主要集中在 " + "、".join(f"{row['name']}（{row['count']}）" for row in top_categories[:4]) + "。")
    if keywords:
        summary_lines.append("最近内容中的高频关键词包括：" + "、".join(keywords[:8]) + "。")
    if highlights["efficiency"]:
        summary_lines.append("最值得优先阅读的提效内容偏向 " + "、".join(item["entry"].get("title") or "无标题" for item in highlights["efficiency"][:2]) + "。")
    if highlights["research"]:
        summary_lines.append("对人工智能研究更有启发的内容偏向 " + "、".join(item["entry"].get("title") or "无标题" for item in highlights["research"][:2]) + "。")

    return {
        "user": {
            "name": user.get("name"),
            "email": user.get("email"),
            "image": user.get("image"),
            "role": session.get("role"),
        },
        "stats": {
            "subscriptions": len(subscriptions),
            "entryWindow": len(entries),
            "entryTotal": total_entries,
            "unreadInWindow": unread_count,
            "viewName": VIEW_OPTIONS.get(view, "时间线"),
        },
        "topSources": top_sources,
        "topCategories": top_categories,
        "keywords": keywords,
        "highlights": highlights,
        "entries": entries,
        "summaryText": " ".join(summary_lines),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def build_report_html(payload: dict[str, Any]) -> str:
    user = payload.get("user", {})
    stats = payload.get("stats", {})
    top_sources = payload.get("topSources", [])
    top_categories = payload.get("topCategories", [])
    keywords = payload.get("keywords", [])
    entries = payload.get("entries", [])
    highlights = payload.get("highlights", {})

    def pills(items: list[dict[str, Any]], fmt) -> str:
        return "".join(f'<span class="pill">{html.escape(fmt(item))}</span>' for item in items)

    def render_highlights(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return '<div class="highlight-reason">当前结果里没有明显匹配的条目。</div>'
        rendered = []
        for row in rows:
            entry = row["entry"]
            rendered.append(f"""
            <article class="highlight-item">
              <a href="{html.escape(entry.get('url') or '#')}" target="_blank" rel="noreferrer">{html.escape(entry.get('title') or '无标题')}</a>
              <div class="highlight-meta">{html.escape(entry.get('feedTitle') or '未知来源')} · 分数 {row['score']} · {html.escape(' / '.join(row.get('matchedTerms', [])) or '无关键词')}</div>
              <div class="highlight-reason">{html.escape(row.get('reason') or '')}</div>
            </article>""")
        return "".join(rendered)

    entries_html = "".join(
        f"""
        <article class="entry">
          <h4><a href="{html.escape(entry.get('url') or '#')}" target="_blank" rel="noreferrer">{html.escape(entry.get('title') or '无标题')}</a></h4>
          <div class="meta">{html.escape(entry.get('feedTitle') or '未知来源')} · {html.escape(entry.get('category') or '未分类')} · {html.escape(entry.get('publishedAt') or '')} · {'已读' if entry.get('read') else '未读'}</div>
          <div class="desc">{html.escape(entry.get('summary') or entry.get('description') or '无摘要')}</div>
        </article>"""
        for entry in entries
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>订阅流时间线报告</title>
  <style>
    :root {{
      --bg: #f5efe8;
      --panel: rgba(255, 251, 245, 0.92);
      --line: rgba(82, 63, 46, 0.14);
      --text: #241c15;
      --muted: #746354;
      --accent: #0f1115;
      --shadow: 0 24px 60px rgba(61, 45, 31, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at 12% 10%, rgba(191, 100, 51, 0.14), transparent 22%),
        radial-gradient(circle at 88% 14%, rgba(15, 17, 21, 0.08), transparent 24%),
        linear-gradient(180deg, #f8f4ed 0%, #ece1d5 100%);
      min-height: 100vh;
      padding: 24px 14px 40px;
    }}
    .wrap {{ max-width: 1180px; margin: 0 auto; }}
    .hero, .card {{ background: var(--panel); border: 1px solid var(--line); box-shadow: var(--shadow); border-radius: 28px; }}
    .hero {{ padding: 30px; margin-bottom: 20px; }}
    .card {{ padding: 20px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .stat {{ border: 1px solid var(--line); border-radius: 18px; padding: 14px; background: rgba(255,255,255,0.6); }}
    .k {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.06em; }}
    .v {{ font-size: 24px; font-weight: 800; line-height: 1; }}
    .summary, .mini, .highlight-card, .entry {{ border: 1px solid var(--line); border-radius: 20px; background: rgba(255,255,255,0.66); }}
    .summary {{ margin-top: 16px; padding: 16px; line-height: 1.8; color: #4a3b2f; }}
    .two-col, .highlight-grid {{ margin-top: 16px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .mini, .highlight-card {{ padding: 16px; }}
    .pill-list {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .pill {{ display: inline-flex; border-radius: 999px; padding: 6px 10px; background: rgba(191, 100, 51, 0.1); color: #8b4a25; font-size: 12px; font-weight: 700; }}
    .highlight-item {{ border-top: 1px solid rgba(82,63,46,0.10); padding-top: 12px; margin-top: 12px; }}
    .highlight-item:first-child {{ border-top: 0; padding-top: 0; margin-top: 0; }}
    .highlight-item a {{ color: inherit; text-decoration: none; font-weight: 700; line-height: 1.6; }}
    .highlight-meta, .meta {{ color: var(--muted); font-size: 12px; line-height: 1.7; margin-top: 5px; }}
    .highlight-reason, .desc {{ color: #4b3c30; font-size: 13px; line-height: 1.75; margin-top: 6px; }}
    .entries {{ margin-top: 16px; display: grid; gap: 12px; }}
    .entry {{ padding: 16px; }}
    .entry h4 {{ margin: 0 0 8px; font-size: 17px; line-height: 1.45; }}
    .entry h4 a {{ color: inherit; text-decoration: none; }}
    @media (max-width: 940px) {{
      .stats, .two-col, .highlight-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>订阅流时间线报告</h1>
      <p>{html.escape((user.get("name") or "当前用户") + " · " + stats.get("viewName", "时间线"))}</p>
    </section>
    <section class="card">
      <div class="stats">
        <div class="stat"><div class="k">当前用户</div><div class="v">{html.escape(user.get("name") or "未识别")}</div></div>
        <div class="stat"><div class="k">订阅数</div><div class="v">{stats.get("subscriptions", 0)}</div></div>
        <div class="stat"><div class="k">抓取窗口</div><div class="v">{stats.get("entryWindow", 0)}</div></div>
        <div class="stat"><div class="k">未读条目</div><div class="v">{stats.get("unreadInWindow", 0)}</div></div>
      </div>
      <div class="summary">{html.escape(payload.get("summaryText") or "")}</div>
      <div class="two-col">
        <section class="mini"><h3>活跃来源</h3><div class="pill-list">{pills(top_sources, lambda i: f"{i['name']} · {i['count']}")}</div></section>
        <section class="mini"><h3>订阅分类</h3><div class="pill-list">{pills(top_categories, lambda i: f"{i['name']} · {i['count']}")}</div></section>
      </div>
      <section class="mini" style="margin-top: 16px;"><h3>关键词</h3><div class="pill-list">{''.join(f'<span class="pill">{html.escape(k)}</span>' for k in keywords)}</div></section>
      <section class="highlight-grid">
        <div class="highlight-card"><h3>最值得优先阅读的提效内容</h3>{render_highlights(highlights.get('efficiency', []))}</div>
        <div class="highlight-card"><h3>对人工智能研究最有启发</h3>{render_highlights(highlights.get('research', []))}</div>
      </section>
      <section class="entries">{entries_html}</section>
    </section>
  </div>
</body>
</html>"""


def create_run_dir(view: int) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"folo_{VIEW_SLUGS.get(view, f'view{view}')}_{ts}"
    run_dir = OUTPUT_DIR / name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def main() -> None:
    args = parse_args()
    limit = max(5, min(100, int(args.limit)))
    view = int(args.view)

    print("开始抓取订阅流数据...")
    session = http_json("/better-auth/get-session", args.cookie)
    subscriptions_resp = http_json("/subscriptions", args.cookie)
    raw_entries = fetch_entries(args.cookie, view, limit)
    subscriptions = subscriptions_resp.get("data", []) if isinstance(subscriptions_resp, dict) else []
    entries = [compact_entry(item) for item in raw_entries[:limit]]
    translator = ZhTranslator()
    print("正在把标题和摘要转换为中文展示...")
    localize_subscriptions(subscriptions, translator)
    localize_entries(entries, translator)

    payload = generate_summary(session, subscriptions, entries, view, len(raw_entries), translator)
    run_dir = create_run_dir(view)
    print(f"运行目录: {run_dir}")

    (run_dir / "results.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_content = build_report_html(payload)
    (run_dir / "summary.html").write_text(html_content, encoding="utf-8")
    (run_dir / "article.html").write_text(html_content, encoding="utf-8")

    print(f"已抓取 {len(entries)} 条，原始总数 {len(raw_entries)} 条")
    print(f"摘要 JSON: {run_dir / 'summary.json'}")
    print(f"摘要 HTML: {run_dir / 'summary.html'}")
    print(f"文章 HTML: {run_dir / 'article.html'}")


if __name__ == "__main__":
    main()
