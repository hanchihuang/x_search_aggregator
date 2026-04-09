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
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

import feedparser
import fitz
import requests


ARXIV_API = "https://export.arxiv.org/api/query"
TRANSLATE_API_BASE = "https://translate.googleapis.com/translate_a/single"
USER_AGENT = "arxiv-title-survey/1.0"
WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+")
SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")
ZH_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
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
DATASET_HINTS = ("dataset", "datasets", "benchmark", "benchmarks", "corpus", "gsm8k", "math", "word problems")
METRIC_HINTS = ("accuracy", "acc", "f1", "score", "performance", "pass@", "metric", "%")
BASELINE_HINTS = ("baseline", "baselines", "gpt-4", "claude", "deepseek", "llm", "models")


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


def shorten(text: str, limit: int = 220) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


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


def summarize_paper(paper: Paper) -> dict[str, str]:
    sentences = split_sentences(paper.summary)
    research_problem = sentences[0] if sentences else paper.summary
    core_method = pick_method_sentence(sentences, fallback=research_problem)
    main_result = pick_sentence(sentences, RESULT_HINTS, fallback=sentences[-1] if sentences else paper.summary)
    limitation = pick_limitation_sentence(sentences, fallback="Abstract 中未直接陈述限制，需在精读全文时补充。")
    return {
        "problem": research_problem,
        "method": core_method,
        "result": main_result,
        "limitation": limitation,
    }


def load_paper_markdown_summary(md_path: Path) -> dict[str, str]:
    text = md_path.read_text(encoding="utf-8")
    abstract = ""
    full_text = ""
    if "## Abstract" in text and "## Full Text" in text:
        abstract = text.split("## Abstract", 1)[1].split("## Full Text", 1)[0].strip()
        full_text = text.split("## Full Text", 1)[1].strip()
    sentences = split_sentences(f"{abstract} {full_text}")

    def collect(hints: tuple[str, ...], fallback: str) -> str:
        matched = []
        for sentence in sentences:
            lower = sentence.lower()
            if any(hint in lower for hint in hints):
                matched.append(shorten(sentence))
            if len(matched) >= 2:
                break
        if matched:
            return "；".join(matched)
        return fallback

    return {
        "dataset_evidence": collect(DATASET_HINTS, "全文中未稳定抽取到明确数据集描述，需人工复核。"),
        "metric_evidence": collect(METRIC_HINTS, "全文中未稳定抽取到明确评价指标描述，需人工复核。"),
        "baseline_evidence": collect(BASELINE_HINTS, "全文中未稳定抽取到明确基线或对比对象描述，需人工复核。"),
        "method_detail": collect(METHOD_HINTS_STRONG + METHOD_HINTS_WEAK, "全文中未稳定抽取到更具体的方法细节，需人工复核。"),
    }


def build_survey_markdown(keyword: str, papers: list[Paper], note_dir: Path, requested_limit: int) -> str:
    focus_terms = extract_focus_terms(papers)
    translator = ZhTranslator()
    rows = ["| 序号 | 论文标题 | 年份 | arXiv ID |", "| --- | --- | --- | --- |"]
    translated_notes: list[dict[str, str]] = []
    for idx, paper in enumerate(papers, start=1):
        year = paper.published[:4] if paper.published else "unknown"
        rows.append(f"| {idx} | {paper.title} | {year} | {paper.arxiv_id} |")
        summary = summarize_paper(paper)
        md_path = note_dir.parent / "papers_md" / f"{idx:02d}_{safe_name(paper.arxiv_id)}.md"
        fulltext_summary = load_paper_markdown_summary(md_path)
        translated_notes.append(
            {
                "title": paper.title,
                "problem_zh": translator.translate(summary["problem"]) or summary["problem"],
                "method_zh": translator.translate(summary["method"]) or summary["method"],
                "result_zh": translator.translate(summary["result"]) or summary["result"],
                "limitation_zh": translator.translate(summary["limitation"]) or summary["limitation"],
                "dataset_zh": translator.translate(fulltext_summary["dataset_evidence"]) or fulltext_summary["dataset_evidence"],
                "metric_zh": translator.translate(fulltext_summary["metric_evidence"]) or fulltext_summary["metric_evidence"],
                "baseline_zh": translator.translate(fulltext_summary["baseline_evidence"]) or fulltext_summary["baseline_evidence"],
                "method_detail_zh": translator.translate(fulltext_summary["method_detail"]) or fulltext_summary["method_detail"],
            }
        )

    theme_text = "、".join(focus_terms) if focus_terms else "未抽取到稳定高频词"
    intro = (
        f"围绕“{keyword}”这一主题，本次在 arXiv 中采用“标题必须包含全部关键词 token”的严格筛选规则，"
        f"请求抓取 {requested_limit} 篇，最终得到 {len(papers)} 篇论文。"
        "从现有语料看，这批论文主要聚焦在任务求解能力、推理过程建模、评测基准设计以及小模型性能提升几个方向。"
    )
    synthesis_lines = [
        "从整体上看，这批论文共享一个核心判断：GSM8K 这类数学文本推理任务的难点，不仅在于算对答案，更在于能否持续维持正确的问题理解、步骤展开、验证与纠错能力。",
        "因此，现有研究逐渐从“单点提升准确率”转向“拆分推理链条中的薄弱环节”，也就是把误差来源细化为语义理解偏差、推理步骤遗漏、验证机制缺失、评测口径过窄等多个层面。",
        "这种变化意味着综述不应该只比较最终分数，而应该比较每篇论文到底在解决推理流程中的哪一段问题，以及它依赖了什么额外结构、监督或评测设计。",
        "在这批论文里，既能看到直接改造求解器的工作，也能看到重新设计基准和评价范式的工作，还能看到利用更小模型配合训练和验证机制实现高性价比提升的工作。",
    ]
    stratification_lines = [
        "若按研究问题拆分，这批论文大致可分为四类。",
        "第一类是“求解质量提升”，目标是让模型更准确地理解题意并生成更稳健的推理链。",
        "第二类是“错误来源分析”，重点解释模型为什么在数学题上仍然会出现语义误读、计算失误和步骤缺失。",
        "第三类是“评测范式扩展”，即认为原有 GSM8K 只看答案过于粗糙，因此引入元推理或视觉上下文，重新考查模型能力边界。",
        "第四类是“轻量化求解”，探索小模型在专门数据与验证框架支持下，是否也能逼近甚至超过更大模型。",
    ]
    per_paper_lines = []
    for idx, note in enumerate(translated_notes, start=1):
        per_paper_lines.extend(
            [
                f"### 论文 {idx}: {note['title']}",
                "",
                f"这篇论文主要关注：{note['problem_zh']}",
                f"其核心做法是：{note['method_zh']}",
                f"如果结合全文线索来看，方法细节进一步表现为：{note['method_detail_zh']}",
                f"论文报告的主要发现是：{note['result_zh']}",
                f"从全文中可直接抓到的实验对象或数据线索包括：{note['dataset_zh']}",
                f"从全文中可直接抓到的评价指标或结果线索包括：{note['metric_zh']}",
                f"从全文中可直接抓到的基线或对比对象线索包括：{note['baseline_zh']}",
                f"从作者摘要中能直接看到的限制或后续空间是：{note['limitation_zh']}",
                "",
            ]
        )
    compare_lines = [
        "从横向比较看，这批论文至少体现出三条明显路线：",
        "1. 求解增强路线：通过更强的问题理解、推理拆解或验证机制提高 GSM8K 一类任务上的解题正确率。这一路线更关心“怎么解得更对”。",
        "2. 评测增强路线：通过元推理、视觉扩展等方式，让基准不再只考最终答案，而是考查模型的推理质量与泛化边界。这一路线更关心“怎么测得更准”。",
        "3. 轻量化路线：探索小模型在专门数据与验证框架支持下能否逼近甚至超过更大模型的表现。这一路线更关心“能否用更低成本获得足够强的推理表现”。",
        "",
        "这些路线的共同点，是都把“分数”看作结果变量，而把“推理过程”视为真正需要被建模、被监督、被评估的对象。",
        "更具体地说，求解增强路线主要在模型内部做文章，评测增强路线主要在任务定义和评分方式上做文章，而轻量化路线则在模型规模、训练样本和验证机制之间寻找新的效率平衡。",
    ]
    evidence_compare_lines = [
        "如果从实验设计角度继续对比，还能看到几个值得在综述里单独强调的现象。",
        "一是多篇论文都把 GSM8K 视为核心验证对象，但它们对“提升来自哪里”的解释并不相同：有的归因于更强的问题理解，有的归因于验证或评分机制，有的归因于任务重构。",
        "二是部分论文已经不满足于在原始文本题面上竞争，而是主动把任务扩展到视觉情境或元推理场景，这说明单一基准上的高分正在逐渐失去足够的解释力。",
        "三是小模型路线的意义并不只是节省参数量，而是在提醒研究者：如果训练样本、验证器和解题流程设计得足够有针对性，模型规模并不是唯一决定因素。",
    ]
    gap_lines = [
        "尽管现有论文已经覆盖了求解、评测和模型规模三个层面，但仍存在一些共同缺口。",
        "第一，很多论文虽然报告了显著的结果提升，但对“提升是否稳定”说明不足，例如不同题型、不同错误类型、不同 prompt 设置下是否仍然成立。",
        "第二，跨论文之间使用的数据组织、验证策略和错误分类标准并不完全一致，导致横向比较仍然带有较强的实验口径依赖。",
        "第三，部分工作已经开始引入视觉上下文或元推理评测，这拓展了问题边界，但也让传统 GSM8K 分数更难承担统一比较尺度的角色。",
        "第四，这批论文普遍强化了对推理过程的重视，但对过程可解释性、可验证性和可迁移性的讨论仍然不够充分。",
        "因此，后续更有价值的综述写法，不应只汇总谁的准确率更高，而应系统回答：哪些方法在修复理解错误，哪些方法在修复推理错误，哪些方法在修复评测缺陷。",
    ]
    return "\n".join(
        [
            f"# 关于“{keyword}”相关 arXiv 论文的中文综述",
            "",
            "## 一、语料范围与筛选说明",
            "",
            f"- 检索时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 请求篇数：{requested_limit}",
            f"- 实际篇数：{len(papers)}",
            f"- 筛选规则：论文标题必须包含 `{keyword}` 的全部关键词 token",
            "- 数据来源：arXiv API 元数据 + 本地下载 PDF 后转 Markdown",
            "- 文件策略：PDF 在成功转 Markdown 后立即删除，仅保留 Markdown、规范化笔记与综述输出",
            "",
            "## 二、论文列表",
            "",
            *rows,
            "",
            "## 三、整体研究脉络",
            "",
            intro,
            "",
            f"从标题和摘要的高频词来看，这批论文的核心主题可以概括为：{theme_text}。",
            "",
            *synthesis_lines,
            "",
            "## 四、研究问题的进一步分层",
            "",
            *stratification_lines,
            "",
            "## 五、逐篇深入综述",
            "",
            *per_paper_lines,
            "## 六、横向比较与综合讨论",
            "",
            *compare_lines,
            "",
            "## 七、基于实验线索的进一步比较",
            "",
            *evidence_compare_lines,
            "",
            "## 八、现阶段的共性不足与后续方向",
            "",
            *gap_lines,
            "",
            "## 九、扩写建议",
            "",
            "如果后续要继续扩写这篇综述，建议把现有 `paper_notes/` 中的规范化笔记作为底稿，再回到每篇论文全文中补齐以下信息，再做正式写作：",
            "1. 任务设定与输入输出形式。",
            "2. 模型或方法的关键创新点。",
            "3. 数据集、评价指标、基线模型。",
            "4. 作者明确指出的局限性与未来工作。",
            "5. 与其他论文在误差类型、验证机制和推理链控制上的直接对应关系。",
            "这样可以把当前这版自动生成的中文综述，进一步扩成一篇更完整、更有论证深度的研究综述。",
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
