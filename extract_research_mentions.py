#!/usr/bin/env python3
"""Extract paper, model, and trick mentions from tweet exports."""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s)>\]\"']+")
ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)
PAPER_CUE_RE = re.compile(r"\b(?:paper|papers|research|preprint|arxiv)\b|论文|研究|预印本", re.IGNORECASE)
PAPER_TITLE_PATTERNS = [
    re.compile(r"[“\"]([^\"\n]{12,180})[”\"]"),
    re.compile(
        r"(?:paper|preprint|论文|研究)[^A-Za-z0-9\u4e00-\u9fff]{0,12}(?:called|titled|title|名为|标题是)?[^A-Za-z0-9\u4e00-\u9fff]{0,8}([A-Z][A-Za-z0-9,:()'`\-/\s]{10,180})",
        re.IGNORECASE,
    ),
]
MODEL_NAME_RE = re.compile(
    r"\b(?:GPT|Claude|Gemini|Llama|Qwen|DeepSeek|Mistral|Mixtral|Yi|Phi|Grok|GLM|BERT|T5|FLAN|PaLM|Whisper|Sora|Flux|SDXL|ControlNet|SAM|InternVL|CogVLM|Hunyuan|MiniMax|ERNIE|Wan|Kimi|Seed|OpenCLIP|CLIP)(?:[-\s]?[A-Za-z0-9.]+){0,3}\b",
    re.IGNORECASE,
)
MODEL_CUE_RE = re.compile(r"\b(?:model|models|checkpoint|backbone)\b|模型|基座|权重|检查点", re.IGNORECASE)
TRICK_CUE_RE = re.compile(
    r"\b(?:trick|tricks|tip|tips|hack|hacks|recipe|recipes|workflow|pattern|heuristic|best practice|prompting)\b|技巧|诀窍|经验|心得|提示|踩坑|工作流|范式|套路",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?\n。！？])\s+")
MODEL_STOPWORDS = {
    "MODEL",
    "MODELS",
    "CHECKPOINT",
    "BACKBONE",
    "PAPER",
    "PAPERS",
    "RESEARCH",
    "ARXIV",
}
MODEL_TRAILING_WORDS = {
    "base",
    "instruct",
    "chat",
    "reasoning",
    "reasoner",
    "preview",
    "thinking",
    "improvements",
    "improvement",
    "release",
    "update",
    "weights",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract paper/model/trick mentions from tweet results.json")
    parser.add_argument("--input", required=True, help="Path to tweet results JSON")
    parser.add_argument("--out-dir", default="", help="Output directory; defaults to input file parent")
    return parser.parse_args()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def shorten(text: str, limit: int = 220) -> str:
    compact = normalize_space(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def load_items(path: Path) -> List[Dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("results.json must contain a list")
    return [item for item in payload if isinstance(item, dict)]


def extract_urls(text: str) -> List[str]:
    return sorted({match.group(0).rstrip(".,;:!?") for match in URL_RE.finditer(text)})


def split_sentences(text: str) -> List[str]:
    lines = [normalize_space(part) for part in SENTENCE_SPLIT_RE.split(text) if normalize_space(part)]
    return lines or ([normalize_space(text)] if normalize_space(text) else [])


def classify_url(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "arxiv.org" in host:
        return "arxiv"
    if "huggingface.co" in host:
        return "huggingface"
    if "github.com" in host:
        return "github"
    return host or "other"


def looks_like_paper_title(text: str) -> bool:
    candidate = normalize_space(text).strip(".,:;!?-")
    if len(candidate) < 12 or len(candidate) > 180:
        return False
    if candidate.lower().startswith(("http://", "https://")):
        return False
    if len(candidate.split()) < 3:
        return False
    letters = sum(ch.isalpha() for ch in candidate)
    if letters < 8:
        return False
    uppercase_words = sum(
        1 for part in candidate.split() if part[:1].isupper() or any(ch.isupper() for ch in part[1:])
    )
    if uppercase_words < 2 and "论文" not in candidate and "研究" not in candidate:
        return False
    return True


def extract_paper_title_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    for pattern in PAPER_TITLE_PATTERNS:
        for match in pattern.finditer(text):
            candidate = normalize_space(match.group(1))
            if not looks_like_paper_title(candidate):
                continue
            lowered = candidate.lower()
            replaced = False
            skip = False
            for index, existing in enumerate(list(candidates)):
                existing_lower = existing.lower()
                if lowered == existing_lower or lowered in existing_lower:
                    skip = True
                    break
                if existing_lower in lowered:
                    candidates[index] = candidate
                    replaced = True
                    break
            if skip:
                continue
            if not replaced:
                candidates.append(candidate)
    return candidates[:5]


def extract_paper_entries(items: Sequence[Dict]) -> List[Dict]:
    entries: List[Dict] = []
    for item in items:
        text = normalize_space(str(item.get("full_text") or item.get("text") or item.get("card_text") or ""))
        if not text:
            continue
        urls = extract_urls(text)
        arxiv_ids = sorted({match.group(1) for match in ARXIV_ID_RE.finditer(text)})
        url_types = sorted({classify_url(url) for url in urls})
        title_candidates = extract_paper_title_candidates(text)
        has_signal = bool(arxiv_ids or PAPER_CUE_RE.search(text) or "arxiv" in url_types or title_candidates)
        if not has_signal:
            continue
        entries.append(
            {
                "tweet_id": item.get("tweet_id"),
                "tweet_url": item.get("url"),
                "posted_at": item.get("posted_at"),
                "arxiv_ids": arxiv_ids,
                "title_candidates": title_candidates,
                "linked_urls": urls,
                "link_types": url_types,
                "excerpt": shorten(text, 300),
            }
        )
    return entries


def canonical_model_name(raw: str) -> str:
    token = normalize_space(raw).strip(".,:;()[]{}")
    if not token:
        return ""
    pieces = token.split()
    while len(pieces) > 1 and pieces[-1].lower() in MODEL_TRAILING_WORDS:
        pieces.pop()
    token = " ".join(pieces).strip()
    if not token:
        return ""
    if token.upper() in MODEL_STOPWORDS:
        return ""
    parts = token.split()
    normalized_parts = []
    for part in parts:
        if len(part) <= 4 and any(ch.isupper() for ch in part):
            normalized_parts.append(part.upper())
        elif re.search(r"\d", part):
            normalized_parts.append(part.upper() if len(part) <= 4 else part)
        else:
            normalized_parts.append(part[0].upper() + part[1:] if part else part)
    return " ".join(normalized_parts)


def extract_model_mentions(items: Sequence[Dict]) -> List[Dict]:
    groups: Dict[str, Dict] = {}
    for item in items:
        text = normalize_space(str(item.get("full_text") or item.get("text") or item.get("card_text") or ""))
        if not text:
            continue
        candidates = {canonical_model_name(match.group(0)) for match in MODEL_NAME_RE.finditer(text)}
        if MODEL_CUE_RE.search(text):
            for token in re.findall(r"\b[A-Za-z][A-Za-z0-9.-]{2,30}(?:-[A-Za-z0-9.]{1,20}){0,2}\b", text):
                if any(ch.isdigit() for ch in token) or any(ch.isupper() for ch in token[1:]):
                    candidates.add(canonical_model_name(token))
        candidates.discard("")
        for name in sorted(candidates):
            bucket = groups.setdefault(
                name,
                {
                    "name": name,
                    "count": 0,
                    "tweet_ids": set(),
                    "examples": [],
                },
            )
            tweet_id = str(item.get("tweet_id") or "")
            if tweet_id and tweet_id in bucket["tweet_ids"]:
                continue
            if tweet_id:
                bucket["tweet_ids"].add(tweet_id)
            bucket["count"] += 1
            if len(bucket["examples"]) < 3:
                bucket["examples"].append(
                    {
                        "tweet_id": item.get("tweet_id"),
                        "tweet_url": item.get("url"),
                        "posted_at": item.get("posted_at"),
                        "excerpt": shorten(text, 220),
                    }
                )
    results = []
    for value in groups.values():
        value["tweet_ids"] = sorted(value["tweet_ids"])
        results.append(value)
    results.sort(key=lambda item: (-item["count"], item["name"].lower()))
    return results


def extract_trick_entries(items: Sequence[Dict]) -> List[Dict]:
    entries: List[Dict] = []
    for item in items:
        text = normalize_space(str(item.get("full_text") or item.get("text") or item.get("card_text") or ""))
        if not text or not TRICK_CUE_RE.search(text):
            continue
        highlights = []
        for sentence in split_sentences(text):
            if TRICK_CUE_RE.search(sentence):
                highlights.append(shorten(sentence, 220))
            if len(highlights) >= 3:
                break
        entries.append(
            {
                "tweet_id": item.get("tweet_id"),
                "tweet_url": item.get("url"),
                "posted_at": item.get("posted_at"),
                "highlights": highlights or [shorten(text, 220)],
                "excerpt": shorten(text, 300),
            }
        )
    return entries


def build_summary(items: Sequence[Dict], papers: Sequence[Dict], models: Sequence[Dict], tricks: Sequence[Dict]) -> Dict:
    top_models = [{"name": item["name"], "count": item["count"]} for item in models[:20]]
    top_paper_titles = []
    seen_titles = set()
    for paper in papers:
        for title in paper.get("title_candidates", []):
            lowered = title.lower()
            if lowered in seen_titles:
                continue
            seen_titles.add(lowered)
            top_paper_titles.append(title)
            if len(top_paper_titles) >= 20:
                break
        if len(top_paper_titles) >= 20:
            break
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tweet_count": len(items),
        "paper_tweet_count": len(papers),
        "model_mention_count": len(models),
        "trick_tweet_count": len(tricks),
        "top_models": top_models,
        "paper_title_candidate_count": len(seen_titles),
        "top_paper_titles": top_paper_titles,
    }


def render_html_report(summary: Dict, papers: Sequence[Dict], models: Sequence[Dict], tricks: Sequence[Dict]) -> str:
    title_rows = "".join(
        f"<li>{html.escape(title)}</li>"
        for title in summary.get("top_paper_titles", [])[:30]
    )
    paper_rows = "".join(
        (
            "<article class=\"item-card\">"
            f"<div class=\"item-head\"><a href=\"{html.escape(str(entry.get('tweet_url') or '#'))}\" target=\"_blank\" rel=\"noreferrer\">tweet</a>"
            f"<span>{html.escape(str(entry.get('posted_at') or 'unknown'))}</span></div>"
            f"<div class=\"excerpt\">{html.escape(entry.get('excerpt') or '')}</div>"
            f"<div class=\"meta-line\"><strong>候选标题</strong>: {html.escape(' | '.join(entry.get('title_candidates') or []) or '-')}</div>"
            f"<div class=\"meta-line\"><strong>arXiv IDs</strong>: {html.escape(', '.join(entry.get('arxiv_ids') or []) or '-')}</div>"
            "</article>"
        )
        for entry in papers[:120]
    )
    model_rows = "".join(
        (
            "<article class=\"item-card\">"
            f"<div class=\"item-head\"><strong>{html.escape(item['name'])}</strong><span>{item['count']} tweets</span></div>"
            f"<div class=\"muted\">{html.escape((item.get('examples') or [{}])[0].get('excerpt', ''))}</div>"
            "</article>"
        )
        for item in models[:80]
    )
    trick_rows = "".join(
        (
            "<article class=\"item-card\">"
            f"<div class=\"item-head\"><a href=\"{html.escape(str(entry.get('tweet_url') or '#'))}\" target=\"_blank\" rel=\"noreferrer\">tweet</a>"
            f"<span>{html.escape(str(entry.get('posted_at') or 'unknown'))}</span></div>"
            f"<div>{html.escape(' | '.join(entry.get('highlights') or []))}</div>"
            f"<div class=\"muted\">{html.escape(entry.get('excerpt') or '')}</div>"
            "</article>"
        )
        for entry in tricks[:120]
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Twitter Research Mentions</title>
  <style>
    :root {{
      --bg: #f6f3ec;
      --card: #fffdf8;
      --border: #d9cfbe;
      --text: #1e1a16;
      --muted: #70665b;
      --accent: #8f3d1f;
    }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif; background: radial-gradient(circle at top, #fffaf2, var(--bg)); color: var(--text); }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 28px 20px 56px; }}
    .hero, section {{ background: var(--card); border: 1px solid var(--border); border-radius: 18px; padding: 20px; margin-bottom: 18px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
    .stat {{ background: #f4ede1; border-radius: 14px; padding: 14px; }}
    .stat strong {{ display: block; font-size: 28px; color: var(--accent); }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin-bottom: 12px; }}
    a {{ color: var(--accent); }}
    .muted {{ color: var(--muted); font-size: 14px; margin-top: 4px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
    .item-card {{ background: #fff9f0; border: 1px solid #eadfcf; border-radius: 14px; padding: 14px; }}
    .item-head {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; font-size: 14px; color: var(--muted); }}
    .excerpt {{ font-size: 15px; line-height: 1.6; }}
    .meta-line {{ margin-top: 8px; font-size: 14px; color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>推特论文 / 模型 / Tricks 提取</h1>
      <div class="grid">
        <div class="stat"><strong>{summary['tweet_count']}</strong>总推文</div>
        <div class="stat"><strong>{summary['paper_tweet_count']}</strong>论文相关推文</div>
        <div class="stat"><strong>{summary.get('paper_title_candidate_count', 0)}</strong>论文标题候选</div>
        <div class="stat"><strong>{summary['model_mention_count']}</strong>模型名称</div>
        <div class="stat"><strong>{summary['trick_tweet_count']}</strong>tricks 相关推文</div>
      </div>
      <p class="muted">当前提取基于启发式规则，适合快速筛选线索，不是严格实体消歧。</p>
    </section>
    <section>
      <h2>论文标题候选</h2>
      <ul>{title_rows or '<li>未提取到明显论文标题候选。</li>'}</ul>
    </section>
    <section>
      <h2>模型提及 Top</h2>
      <div class="cards">{model_rows or '<article class="item-card">未提取到明显模型名。</article>'}</div>
    </section>
    <section>
      <h2>论文相关推文</h2>
      <div class="cards">{paper_rows or '<article class="item-card">未提取到明显论文相关推文。</article>'}</div>
    </section>
    <section>
      <h2>Tricks / Tips / Workflows</h2>
      <div class="cards">{trick_rows or '<article class="item-card">未提取到明显 tricks 相关推文。</article>'}</div>
    </section>
  </main>
</body>
</html>
"""


def render_markdown_report(summary: Dict, papers: Sequence[Dict], models: Sequence[Dict], tricks: Sequence[Dict]) -> str:
    lines = [
        "# 推特论文 / 模型 / Tricks 提取",
        "",
        f"- 总推文数: {summary['tweet_count']}",
        f"- 论文相关推文数: {summary['paper_tweet_count']}",
        f"- 论文标题候选数: {summary.get('paper_title_candidate_count', 0)}",
        f"- 提取出的模型名称数: {summary['model_mention_count']}",
        f"- tricks 相关推文数: {summary['trick_tweet_count']}",
        "",
        "## 论文标题候选",
    ]
    if summary.get("top_paper_titles"):
        for title in summary["top_paper_titles"][:30]:
            lines.append(f"- {title}")
    else:
        lines.append("- 未提取到明显论文标题候选。")

    lines.extend([
        "",
        "## 模型提及 Top",
    ])
    if models:
        for item in models[:40]:
            lines.append(f"- {item['name']}: {item['count']} tweets")
    else:
        lines.append("- 未提取到明显模型名。")

    lines.extend(["", "## 论文相关推文"])
    if papers:
        for entry in papers[:80]:
            lines.append(f"- 链接: {entry.get('tweet_url') or '-'}")
            lines.append(f"时间: {entry.get('posted_at') or 'unknown'}")
            lines.append(f"候选标题: {' | '.join(entry.get('title_candidates') or []) or '-'}")
            lines.append(f"arXiv IDs: {', '.join(entry.get('arxiv_ids') or []) or '-'}")
            lines.append(f"摘录: {entry.get('excerpt') or ''}")
            lines.append("")
    else:
        lines.append("- 未提取到明显论文相关推文。")

    lines.extend(["", "## Tricks / Tips / Workflows"])
    if tricks:
        for entry in tricks[:80]:
            lines.append(f"- 链接: {entry.get('tweet_url') or '-'}")
            lines.append(f"时间: {entry.get('posted_at') or 'unknown'}")
            lines.append(f"Highlights: {' | '.join(entry.get('highlights') or [])}")
            lines.append(f"摘录: {entry.get('excerpt') or ''}")
            lines.append("")
    else:
        lines.append("- 未提取到明显 tricks 相关推文。")
    return "\n".join(lines) + "\n"


def extract_research_mentions(items: Sequence[Dict]) -> Dict:
    papers = extract_paper_entries(items)
    models = extract_model_mentions(items)
    tricks = extract_trick_entries(items)
    summary = build_summary(items, papers, models, tricks)
    return {
        "summary": summary,
        "papers": papers,
        "models": models,
        "tricks": tricks,
    }


def write_outputs(result: Dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "research_mentions.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "research_mentions.md").write_text(
        render_markdown_report(result["summary"], result["papers"], result["models"], result["tricks"]),
        encoding="utf-8",
    )
    (out_dir / "research_mentions.html").write_text(
        render_html_report(result["summary"], result["papers"], result["models"], result["tricks"]),
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else input_path.parent
    items = load_items(input_path)
    result = extract_research_mentions(items)
    write_outputs(result, out_dir)
    print(f"Research mentions JSON: {out_dir / 'research_mentions.json'}")
    print(f"Research mentions MD:   {out_dir / 'research_mentions.md'}")
    print(f"Research mentions HTML: {out_dir / 'research_mentions.html'}")


if __name__ == "__main__":
    main()
