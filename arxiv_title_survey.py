#!/usr/bin/env python3
"""从 arXiv 按标题关键词抓取论文，转 Markdown，并生成规范化 survey。"""

from __future__ import annotations

import argparse
import json
import html
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

import feedparser
import fitz
import requests


ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "arxiv-title-survey/1.0"
WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "their",
    "this",
    "to",
    "using",
    "via",
    "we",
    "with",
}
METHOD_HINTS_STRONG = (
    "we propose",
    "we present",
    "we introduce",
    "propose",
    "present",
    "introduce",
    "framework",
    "approach",
    "architecture",
    "network",
    "model",
)
METHOD_HINTS_WEAK = ("method", "design", "develop")
RESULT_HINTS = ("result", "show", "demonstrate", "achieve", "outperform", "improve", "gain", "performance")
LIMIT_HINTS = ("limit", "future", "challenge", "however", "remain", "still", "yet")


@dataclass
class Paper:
    arxiv_id: str
    title: str
    summary: str
    published: str
    updated: str
    authors: list[str]
    abs_url: str
    pdf_url: str


def safe_name(text: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text.strip(), flags=re.UNICODE)
    return cleaned.strip("._") or "arxiv"


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(text or "")]


def title_contains_all_keywords(title: str, keyword: str) -> bool:
    title_tokens = set(tokenize(title))
    keyword_tokens = [token for token in tokenize(keyword) if token not in STOPWORDS]
    if not keyword_tokens:
        return False
    return all(token in title_tokens for token in keyword_tokens)


def build_title_query(keyword: str) -> str:
    tokens = [token for token in tokenize(keyword) if token not in STOPWORDS]
    if not tokens:
        raise ValueError("关键词为空，无法构造标题搜索。")
    return " AND ".join(f'ti:"{token}"' for token in tokens)


def fetch_arxiv_papers(keyword: str, limit: int, max_results: int) -> list[Paper]:
    query = build_title_query(keyword)
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max(limit * 5, max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API}?{ '&'.join(f'{k}={quote_plus(str(v))}' for k, v in params.items()) }"
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    papers: list[Paper] = []
    seen_ids: set[str] = set()
    for entry in feed.entries:
        title = " ".join((entry.get("title") or "").split())
        if not title_contains_all_keywords(title, keyword):
            continue
        abs_url = entry.get("id") or ""
        arxiv_id = abs_url.rstrip("/").split("/")[-1]
        if not arxiv_id or arxiv_id in seen_ids:
            continue
        pdf_url = ""
        for link in entry.get("links", []):
            href = link.get("href") or ""
            link_type = (link.get("type") or "").lower()
            if href.endswith(".pdf") or "pdf" in link_type:
                pdf_url = href
                break
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                title=title,
                summary=" ".join((entry.get("summary") or "").split()),
                published=entry.get("published") or "",
                updated=entry.get("updated") or "",
                authors=[author.get("name", "").strip() for author in entry.get("authors", []) if author.get("name")],
                abs_url=abs_url,
                pdf_url=pdf_url,
            )
        )
        seen_ids.add(arxiv_id)
        if len(papers) >= limit:
            break
    return papers


def download_pdf(pdf_url: str, pdf_path: Path) -> None:
    with requests.get(pdf_url, stream=True, timeout=60, headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        with pdf_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 128):
                if chunk:
                    fh.write(chunk)


def clean_pdf_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = text.replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not blank:
                cleaned.append("")
            blank = True
            continue
        cleaned.append(stripped)
        blank = False
    return "\n".join(cleaned).strip()


def pdf_to_markdown(pdf_path: Path, md_path: Path, paper: Paper) -> None:
    doc = fitz.open(pdf_path)
    pages: list[str] = []
    try:
        for idx, page in enumerate(doc, start=1):
            text = clean_pdf_text(page.get_text("text", sort=True))
            if not text:
                continue
            pages.append(f"## Page {idx}\n\n{text}")
    finally:
        doc.close()

    md = [
        f"# {paper.title}",
        "",
        f"- arXiv ID: {paper.arxiv_id}",
        f"- Published: {paper.published or 'unknown'}",
        f"- Updated: {paper.updated or 'unknown'}",
        f"- Authors: {', '.join(paper.authors) if paper.authors else 'unknown'}",
        f"- Abs URL: {paper.abs_url}",
        f"- PDF URL: {paper.pdf_url}",
        "",
        "## Abstract",
        "",
        paper.summary or "No abstract found.",
        "",
        "## Full Text",
        "",
        "\n\n".join(pages) if pages else "No extractable text found in PDF.",
        "",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")


def convert_pdf_and_delete(pdf_path: Path, md_path: Path, paper: Paper) -> None:
    pdf_to_markdown(pdf_path, md_path, paper)
    if pdf_path.exists():
        pdf_path.unlink()


def split_sentences(text: str) -> list[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    sentences = [part.strip() for part in SENTENCE_RE.split(text) if part.strip()]
    return sentences or [text]


def pick_sentence(sentences: Iterable[str], hints: tuple[str, ...], fallback: str = "") -> str:
    for sentence in sentences:
        lower = sentence.lower()
        if any(hint in lower for hint in hints):
            return sentence
    return fallback


def pick_method_sentence(sentences: list[str], fallback: str = "") -> str:
    for sentence in sentences:
        lower = sentence.lower()
        if any(hint in lower for hint in METHOD_HINTS_STRONG):
            return sentence
    for sentence in sentences:
        lower = sentence.lower()
        if "current method" in lower or "current methods" in lower or "existing method" in lower:
            continue
        if any(hint in lower for hint in METHOD_HINTS_WEAK):
            return sentence
    return fallback


def pick_limitation_sentence(sentences: list[str], fallback: str = "") -> str:
    for sentence in sentences:
        lower = sentence.lower()
        if "to overcome this limitation" in lower:
            continue
        if any(hint in lower for hint in LIMIT_HINTS):
            return sentence
    return fallback


def extract_focus_terms(papers: list[Paper], top_k: int = 8) -> list[str]:
    counter: Counter[str] = Counter()
    for paper in papers:
        for token in tokenize(f"{paper.title} {paper.summary}"):
            if token in STOPWORDS or len(token) <= 2:
                continue
            counter[token] += 1
    return [token for token, _ in counter.most_common(top_k)]


def build_normalized_paper_note(paper: Paper, md_path: Path, keyword: str) -> str:
    text = md_path.read_text(encoding="utf-8")
    abstract = ""
    if "## Abstract" in text and "## Full Text" in text:
        abstract = text.split("## Abstract", 1)[1].split("## Full Text", 1)[0].strip()
    sentences = split_sentences(abstract)
    research_problem = sentences[0] if sentences else paper.summary
    core_method = pick_method_sentence(sentences, fallback=research_problem)
    main_result = pick_sentence(sentences, RESULT_HINTS, fallback=sentences[-1] if sentences else paper.summary)
    limitation = pick_limitation_sentence(sentences, fallback="Abstract 中未直接陈述限制，需在精读全文时补充。")
    return "\n".join(
        [
            f"# Normalized Note: {paper.title}",
            "",
            "## Metadata",
            "",
            f"- Keyword: {keyword}",
            f"- arXiv ID: {paper.arxiv_id}",
            f"- Authors: {', '.join(paper.authors) if paper.authors else 'unknown'}",
            f"- Published: {paper.published or 'unknown'}",
            f"- Source Markdown: {md_path.name}",
            "",
            "## Standardized Summary",
            "",
            f"- Research problem: {research_problem}",
            f"- Core method: {core_method}",
            f"- Main result: {main_result}",
            f"- Limitation / next step: {limitation}",
            "",
            "## Relevance To Query",
            "",
            f"- The title contains every keyword token from `{keyword}`, so this paper remains inside the strict title-matching corpus.",
            "",
            "## Reading Checklist",
            "",
            "- Confirm task setting and assumptions.",
            "- Record model / method innovations.",
            "- Record datasets, metrics, and baselines.",
            "- Record explicit limitations or unanswered questions.",
            "",
        ]
    )


def build_survey_markdown(keyword: str, papers: list[Paper], note_dir: Path, requested_limit: int) -> str:
    focus_terms = extract_focus_terms(papers)
    rows = [
        "| # | Title | Year | arXiv ID |",
        "| --- | --- | --- | --- |",
    ]
    for idx, paper in enumerate(papers, start=1):
        year = paper.published[:4] if paper.published else "unknown"
        rows.append(f"| {idx} | {paper.title} | {year} | {paper.arxiv_id} |")

    notes = []
    for idx, paper in enumerate(papers, start=1):
        note_path = note_dir / f"{idx:02d}_{safe_name(paper.arxiv_id)}.md"
        note_text = note_path.read_text(encoding="utf-8")
        summary_block = note_text.split("## Standardized Summary", 1)[1].split("## Relevance To Query", 1)[0].strip()
        notes.extend([f"### Paper {idx}: {paper.title}", "", summary_block, ""])

    theme_text = ", ".join(focus_terms) if focus_terms else "No stable theme terms extracted"
    return "\n".join(
        [
            f"# Survey Draft: {keyword}",
            "",
            "## Corpus Protocol",
            "",
            f"- Retrieval date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- Requested corpus size: {requested_limit} papers",
            f"- Actual corpus size: {len(papers)} papers",
            f"- Selection rule: every keyword token from `{keyword}` must appear in the paper title",
            "- Source: arXiv API metadata + downloaded PDFs converted to Markdown",
            "- PDF retention policy: delete each PDF immediately after Markdown conversion succeeds",
            "",
            "## Corpus Overview",
            "",
            *rows,
            "",
            "## Dominant Themes",
            "",
            f"- Frequent title/abstract terms across the 10-paper corpus: {theme_text}",
            "- Use these terms as the first-pass grouping signal, then refine after manual reading if you need a publication-ready survey.",
            "",
            "## Standardized Paper Notes",
            "",
            *notes,
            "## Suggested Writing Flow",
            "",
            "1. Read the 10 normalized notes before revisiting full Markdown files.",
            "2. Group papers by problem setting, not just by publication year.",
            "3. Compare methods, datasets, and evaluation criteria under the same subsection.",
            "4. Separate confirmed experimental findings from author claims in abstracts.",
            "5. End with open gaps, reproducibility concerns, and future directions.",
            "",
            "## Gap Checklist",
            "",
            "- Which assumptions recur across papers but remain weakly justified?",
            "- Which baselines or datasets are missing from cross-paper comparison?",
            "- Which claims rely only on abstract-level evidence and still need full-text verification?",
            "",
        ]
    )


def build_summary_html(keyword: str, papers: list[Paper], requested_limit: int) -> str:
    cards: list[str] = []
    for idx, paper in enumerate(papers, start=1):
        base_name = f"{idx:02d}_{safe_name(paper.arxiv_id)}.md"
        cards.append(
            f"""
            <article class="card">
              <div class="meta">Paper {idx} · {html.escape(paper.arxiv_id)} · {html.escape(paper.published[:10] if paper.published else "unknown")}</div>
              <h2>{html.escape(paper.title)}</h2>
              <p>{html.escape(paper.summary[:360] + ("..." if len(paper.summary) > 360 else ""))}</p>
              <div class="links">
                <a href="papers_md/{html.escape(base_name)}" target="_blank" rel="noreferrer">论文 Markdown</a>
                <a href="paper_notes/{html.escape(base_name)}" target="_blank" rel="noreferrer">规范化笔记</a>
                <a href="{html.escape(paper.abs_url)}" target="_blank" rel="noreferrer">arXiv 页面</a>
              </div>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(keyword)} - arXiv Survey</title>
  <style>
    :root {{
      --bg: #f4efe8;
      --panel: rgba(255,255,255,0.9);
      --ink: #1f1b16;
      --muted: #6f6559;
      --line: #ddd3c6;
      --accent: #0f766e;
    }}
    body {{ margin: 0; background: radial-gradient(circle at top, #fff8ef 0, var(--bg) 38%, #ede4d9 100%); color: var(--ink); font-family: "IBM Plex Sans","PingFang SC","Noto Sans SC",sans-serif; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 28px 18px 48px; }}
    .hero, .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 22px; box-shadow: 0 18px 44px rgba(39,28,20,0.08); }}
    .hero {{ padding: 24px; margin-bottom: 16px; }}
    .card {{ padding: 18px; margin-bottom: 14px; }}
    h1, h2 {{ font-family: "Source Han Serif SC","Noto Serif CJK SC",serif; }}
    h1 {{ margin: 0 0 8px; }}
    h2 {{ margin: 0 0 10px; font-size: 1.1rem; }}
    p, li {{ line-height: 1.75; }}
    .meta {{ color: var(--muted); font-size: 0.92rem; margin-bottom: 10px; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .top-links {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>{html.escape(keyword)} · arXiv 标题严格匹配 Survey</h1>
      <p>该语料只保留标题中包含查询关键词全部 token 的论文。请求篇数 {requested_limit}，实际返回 {len(papers)}。每篇 PDF 在成功转成 Markdown 后立即删除，当前目录保留论文 Markdown、规范化笔记和 survey 草稿。</p>
      <div class="top-links">
        <a href="survey.md" target="_blank" rel="noreferrer">打开 survey.md</a>
        <a href="manifest.json" target="_blank" rel="noreferrer">打开 manifest.json</a>
      </div>
    </section>
    {''.join(cards)}
  </main>
</body>
</html>"""
def run(keyword: str, limit: int, output_root: Path, max_results: int) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root / f"{safe_name(keyword)}_{timestamp}"
    pdf_dir = run_dir / "pdf_tmp"
    md_dir = run_dir / "papers_md"
    note_dir = run_dir / "paper_notes"
    run_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    note_dir.mkdir(parents=True, exist_ok=True)

    papers = fetch_arxiv_papers(keyword, limit=limit, max_results=max_results)
    actual_count = len(papers)
    if actual_count == 0:
        raise SystemExit(f"没有找到标题包含 `{keyword}` 全部关键词的 arXiv 论文。")
    if actual_count < limit:
        print(f"只找到 {actual_count} 篇满足条件的论文，低于请求的 {limit} 篇；将按实际篇数继续生成输出。")

    manifest = {
        "keyword": keyword,
        "requested_limit": limit,
        "actual_count": actual_count,
        "selection_rule": "all keyword tokens must appear in title",
        "generated_at": datetime.now().isoformat(),
        "papers": [],
    }

    for idx, paper in enumerate(papers, start=1):
        base_name = f"{idx:02d}_{safe_name(paper.arxiv_id)}"
        pdf_path = pdf_dir / f"{base_name}.pdf"
        md_path = md_dir / f"{base_name}.md"
        note_path = note_dir / f"{base_name}.md"
        print(f"[{idx}/{actual_count}] 下载 PDF: {paper.title}")
        download_pdf(paper.pdf_url, pdf_path)
        print(f"[{idx}/{actual_count}] 转 Markdown 并删除 PDF: {paper.arxiv_id}")
        convert_pdf_and_delete(pdf_path, md_path, paper)
        note_path.write_text(build_normalized_paper_note(paper, md_path, keyword), encoding="utf-8")
        manifest["papers"].append(
            {
                "index": idx,
                "arxiv_id": paper.arxiv_id,
                "title": paper.title,
                "published": paper.published,
                "authors": paper.authors,
                "markdown_file": str(md_path.relative_to(run_dir)),
                "normalized_note": str(note_path.relative_to(run_dir)),
                "pdf_deleted": not pdf_path.exists(),
            }
        )

    survey_path = run_dir / "survey.md"
    survey_path.write_text(build_survey_markdown(keyword, papers, note_dir, limit), encoding="utf-8")
    (run_dir / "summary.html").write_text(build_summary_html(keyword, papers, limit), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        pdf_dir.rmdir()
    except OSError:
        pass
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取标题严格匹配关键词的 arXiv 论文，转 Markdown，删除 PDF，并生成 survey。"
    )
    parser.add_argument("keyword", help="搜索关键词。所有关键词 token 都必须出现在标题里。")
    parser.add_argument("--limit", type=int, default=10, help="需要保留的论文数量，默认 10。")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("output/arxiv_title_surveys"),
        help="输出根目录，默认 output/arxiv_title_surveys。",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="arXiv API 的最大拉取量，用于在严格标题过滤前扩大候选集。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit <= 0:
        raise SystemExit("--limit 必须大于 0")
    run_dir = run(args.keyword, args.limit, args.output_root, args.max_results)
    print("")
    print("完成。")
    print(f"输出目录: {run_dir}")
    print(f"论文 Markdown: {run_dir / 'papers_md'}")
    print(f"规范化笔记: {run_dir / 'paper_notes'}")
    print(f"Survey: {run_dir / 'survey.md'}")


if __name__ == "__main__":
    main()
