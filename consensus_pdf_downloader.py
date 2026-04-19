#!/usr/bin/env python3
"""Download PDF references linked from a Consensus page."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

from browser_config import get_playwright_launch_kwargs
from search_x import cdp_ready, launch_chrome_for_cdp, parse_cdp_port, wait_cdp_ready

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = BASE_DIR / "output"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)
ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)
META_PDF_RE = re.compile(
    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download PDF references linked from a Consensus page")
    parser.add_argument("--url", required=True, help="Consensus page URL")
    parser.add_argument("--output-root", default=str(OUTPUT_ROOT), help="Output root directory")
    parser.add_argument("--max-scrolls", type=int, default=8, help="Maximum bottom-scroll rounds")
    parser.add_argument("--scroll-pause-ms", type=int, default=1800, help="Pause after each scroll")
    parser.add_argument("--max-downloads", type=int, default=0, help="0 means no limit")
    parser.add_argument("--cdp-url", default="", help="Optional existing Chrome CDP URL")
    parser.add_argument("--auto-launch", action="store_true", help="Auto launch Chrome with remote debugging when CDP is unavailable")
    parser.add_argument("--reuse-only", action="store_true", help="Only reuse an already-open Consensus tab; do not open a new tab automatically")
    parser.add_argument("--chrome-path", default="/usr/bin/google-chrome", help="Chrome executable path for auto-launch")
    parser.add_argument("--user-data-dir", default="chrome_profile_consensus", help="Chrome profile dir used for auto-launch")
    parser.add_argument("--wait-seconds", type=int, default=20, help="Seconds to wait for CDP readiness after auto-launch")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    return parser.parse_args()


def sanitize_name(value: str, limit: int = 96) -> str:
    cleaned = re.sub(r"\s+", "_", (value or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff.]+", "", cleaned)
    cleaned = cleaned.strip("._-")
    return cleaned[:limit] or "item"


def create_run_dir(output_root: Path, url: str) -> Path:
    parsed = urlparse(url)
    slug = sanitize_name(Path(parsed.path.rstrip("/")).name or parsed.netloc or "consensus")
    run_dir = output_root / f"consensus_pdfs_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "pdfs").mkdir(parents=True, exist_ok=True)
    return run_dir


def safe_page_title(page: Page) -> str:
    try:
        return (page.title() or "").strip()
    except PlaywrightError as exc:
        if "Execution context was destroyed" in str(exc):
            return ""
        raise


def safe_body_text(page: Page, timeout: int = 5000) -> str:
    try:
        return (page.locator("body").inner_text(timeout=timeout) or "").strip()
    except PlaywrightError as exc:
        if "Execution context was destroyed" in str(exc):
            return ""
        raise


def wait_for_stable_page(page: Page, settle_ms: int = 1200) -> None:
    deadline = time.time() + settle_ms / 1000
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=400)
        except Exception:
            pass
        page.wait_for_timeout(120)


def wait_for_consensus_page(page: Page, timeout_ms: int = 60000) -> None:
    deadline = datetime.now().timestamp() + timeout_ms / 1000
    while datetime.now().timestamp() < deadline:
        wait_for_stable_page(page, settle_ms=500)
        title = safe_page_title(page).lower()
        body_text = safe_body_text(page, timeout=2000).lower()
        if not title and not body_text:
            page.wait_for_timeout(500)
            continue
        if "just a moment" not in title and "enable javascript and cookies to continue" not in body_text:
            return
        page.wait_for_timeout(1500)
    raise RuntimeError("Consensus 页面长时间停留在 Cloudflare/挑战页，未能进入实际内容。请先在同一个 Chrome 会话里手动打开目标页面并确认可正常浏览，再通过 CDP 重试。")


def looks_like_reference_skeleton(page: Page) -> bool:
    body_text = safe_body_text(page, timeout=2000)
    compact = re.sub(r"\s+", " ", body_text)
    try:
        anchor_count = page.locator("a[href]").count()
    except Exception:
        anchor_count = 0
    if "Results" in compact and "References" in compact and "— · · ·" in compact:
        return True
    if anchor_count <= 2 and "References" in compact and "Sources" in compact:
        return True
    return False


def is_consensus_login_page(page: Page) -> bool:
    title = safe_page_title(page).lower()
    body_text = safe_body_text(page, timeout=2000).lower()
    current_url = (page.url or "").lower()
    if "/login" in current_url or "/sign-in" in current_url or "/sign_up" in current_url:
        return True
    if "sign up - consensus" in title or "sign in - consensus" in title:
        return True
    if "sign in" in body_text and "sign up" in body_text and "research starts here" in body_text:
        return True
    return False


def maybe_activate_reference_views(page: Page) -> None:
    for label in ("References", "Sources", "Corpus"):
        locator = page.locator("button", has_text=label)
        if locator.count() == 0:
            continue
        target = locator.first
        try:
            target.click(timeout=2000)
            page.wait_for_timeout(800)
        except Exception:
            pass


def find_existing_consensus_page(browser, target_url: str) -> tuple[Page | None, str]:
    target = target_url.rstrip("/")
    fallback_page = None
    for context in browser.contexts:
        for page in context.pages:
            current = (page.url or "").rstrip("/")
            if current == target:
                return page, "exact"
            if "consensus.app/" in current and fallback_page is None:
                fallback_page = page
    if fallback_page is not None:
        return fallback_page, "same-site"
    return None, "none"


def build_browser_and_page(playwright, cdp_url: str, headless: bool, target_url: str, reuse_only: bool):
    if cdp_url:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15000)
        existing_page, reuse_mode = find_existing_consensus_page(browser, target_url)
        if existing_page is not None:
            return browser, existing_page, reuse_mode
        if reuse_only:
            return browser, None, "reuse-only-miss"
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        return browser, page, "new"
    browser = playwright.chromium.launch(**get_playwright_launch_kwargs(headless=headless))
    page = browser.new_page()
    return browser, page, "fresh-browser"


def extract_reference_candidates(page: Page) -> List[Dict]:
    payload = page.evaluate(
        """() => {
          const compact = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const anchors = Array.from(document.querySelectorAll("a[href]"));
          return anchors.map((anchor) => {
            const href = anchor.href || "";
            const text = compact(anchor.innerText || anchor.textContent || "");
            const container = anchor.closest("article, li, section, [data-testid], .group, .card, div");
            let title = "";
            let context = "";
            if (container) {
              const titleAnchor = Array.from(container.querySelectorAll("a[href]")).find((item) => {
                const href = item.href || "";
                const text = compact(item.innerText || item.textContent || "");
                return href.includes("/papers/") && text.length >= 8;
              });
              title = titleAnchor ? compact(titleAnchor.innerText || titleAnchor.textContent || "") : "";
              context = compact(container.innerText || container.textContent || "").slice(0, 320);
            }
            return { href, text, title, context };
          }).filter((item) => item.href);
        }"""
    )
    seen = set()
    results: List[Dict] = []
    for item in payload:
        href = str(item.get("href") or "").strip()
        parsed = urlparse(href)
        lowered = href.lower()
        if not href or parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.endswith("doi.org") or "arxiv.org" in parsed.netloc or lowered.endswith(".pdf") or "/pdf/" in lowered:
            key = href
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "href": href,
                    "text": str(item.get("text") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "context": str(item.get("context") or "").strip(),
                }
            )
    return results


def extract_panel_pdf_candidates(page: Page) -> List[Dict]:
    payload = page.evaluate(
        """() => {
          const compact = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const anchors = Array.from(document.querySelectorAll("a[href]"));
          return anchors.map((anchor) => {
            const href = anchor.href || "";
            const text = compact(anchor.innerText || anchor.textContent || "");
            const region = anchor.closest("aside, [role='dialog'], section, div");
            const context = region ? compact(region.innerText || region.textContent || "").slice(0, 500) : "";
            return { href, text, context };
          }).filter((item) => item.href);
        }"""
    )
    seen = set()
    results: List[Dict] = []
    for item in payload:
        href = str(item.get("href") or "").strip()
        text = str(item.get("text") or "").strip()
        context = str(item.get("context") or "").strip()
        lowered_href = href.lower()
        lowered_text = text.lower()
        lowered_context = context.lower()
        if not href:
            continue
        if (
            lowered_href.endswith(".pdf")
            or ".pdf?" in lowered_href
            or "doi.org" in lowered_href
            or "arxiv.org" in lowered_href
            or ("pdf" in lowered_text and "paper" in lowered_context)
        ):
            key = href
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "href": href,
                    "text": text,
                    "title": "",
                    "context": context,
                }
            )
    return results


def extract_citation_tag_texts(page: Page) -> List[str]:
    values = page.evaluate(
        """() => Array.from(document.querySelectorAll('[data-testid="tag"]'))
        .map((el) => (el.textContent || '').replace(/\\s+/g, ' ').trim())
        .filter(Boolean)"""
    )
    tags: List[str] = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not re.fullmatch(r"\d{1,3}", text):
            continue
        if text in seen:
            continue
        seen.add(text)
        tags.append(text)
    return tags


def open_citation_panel(page: Page, tag_text: str) -> bool:
    return bool(
        page.evaluate(
            """(tagText) => {
              const target = Array.from(document.querySelectorAll('[data-testid="tag"]'))
                .find((el) => (el.textContent || '').replace(/\\s+/g, ' ').trim() === tagText);
              if (!target) return false;
              target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
              return true;
            }""",
            tag_text,
        )
    )


def collect_reference_candidates(page: Page, max_scrolls: int, pause_ms: int) -> List[Dict]:
    best: List[Dict] = []
    stable_rounds = 0
    for index in range(1, max_scrolls + 1):
        current = extract_reference_candidates(page)
        print(f"[SCAN] round={index} refs={len(current)}")
        if len(current) > len(best):
            best = current
            stable_rounds = 0
        else:
            stable_rounds += 1
        if stable_rounds >= 2:
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(pause_ms)
    return best


def collect_reference_candidates_from_citations(page: Page, pause_ms: int) -> List[Dict]:
    tags = extract_citation_tag_texts(page)
    print(f"[CITATIONS] unique_tags={len(tags)}")
    refs: List[Dict] = []
    seen = set()
    for tag_text in tags:
        if not open_citation_panel(page, tag_text):
            continue
        page.wait_for_timeout(min(max(pause_ms, 800), 2500))
        for item in extract_panel_pdf_candidates(page):
            href = item["href"]
            if href in seen:
                continue
            seen.add(href)
            item["context"] = f"[citation {tag_text}] {item.get('context') or ''}".strip()
            refs.append(item)
    return refs


def classify_reference(href: str) -> str:
    parsed = urlparse(href)
    lowered = href.lower()
    if lowered.endswith(".pdf") or "/pdf/" in lowered:
        return "direct_pdf"
    if "arxiv.org" in parsed.netloc:
        return "arxiv"
    if parsed.netloc.endswith("doi.org"):
        return "doi"
    return "other"


def extract_doi_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc.endswith("doi.org"):
        return ""
    doi = unquote(parsed.path.lstrip("/")).strip()
    return doi.rstrip(".,);]")


def arxiv_pdf_from_reference(url_or_doi: str) -> str:
    match = ARXIV_URL_RE.search(url_or_doi) or ARXIV_DOI_RE.search(url_or_doi)
    if not match:
        return ""
    return f"https://arxiv.org/pdf/{match.group(1)}.pdf"


def is_probable_pdf_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.endswith(".pdf") or ".pdf?" in lowered or "/pdf/" in lowered or "downloadpdf" in lowered


def choose_pdf_candidate(candidates: Iterable[str], base_url: str) -> str:
    scored: List[Tuple[int, str]] = []
    base_host = urlparse(base_url).netloc.lower()
    for raw in candidates:
        candidate = urljoin(base_url, raw.strip())
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        score = 0
        lowered = candidate.lower()
        if parsed.netloc.lower() == base_host:
            score += 3
        if lowered.endswith(".pdf") or ".pdf?" in lowered:
            score += 6
        if "/pdf" in lowered:
            score += 4
        if "download" in lowered:
            score += 2
        scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1] if scored else ""


def extract_pdf_candidates_from_html(html_text: str, base_url: str) -> str:
    meta_candidates = META_PDF_RE.findall(html_text)
    if meta_candidates:
        best = choose_pdf_candidate(meta_candidates, base_url)
        if best:
            return best
    href_candidates = []
    for href in HREF_RE.findall(html_text):
        if is_probable_pdf_url(href):
            href_candidates.append(href)
    return choose_pdf_candidate(href_candidates, base_url)


def resolve_doi_to_pdf(session: requests.Session, doi_url: str) -> Tuple[str, str, str]:
    doi = extract_doi_from_url(doi_url)
    if not doi:
        return "", "", "invalid doi"
    arxiv_pdf = arxiv_pdf_from_reference(doi)
    if arxiv_pdf:
        return arxiv_pdf, doi, "arxiv doi"

    response = session.get(
        doi_url,
        timeout=45,
        allow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )
    final_url = response.url
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/pdf" in content_type or is_probable_pdf_url(final_url):
        return final_url, doi, "publisher direct"
    pdf_url = extract_pdf_candidates_from_html(response.text, final_url)
    if pdf_url:
        return pdf_url, doi, "publisher html"
    return "", doi, f"no pdf found on landing page {final_url}"


def resolve_reference_to_pdf(session: requests.Session, ref: Dict) -> Dict:
    href = ref["href"]
    kind = classify_reference(href)
    title = ref.get("title") or ref.get("text") or ""
    record = {
        "source_url": href,
        "source_type": kind,
        "doi": "",
        "title": title,
        "context": ref.get("context") or "",
        "pdf_url": "",
        "status": "pending",
        "resolution_note": "",
        "filename": "",
    }
    try:
        if kind == "direct_pdf":
            record["pdf_url"] = href
            record["status"] = "resolved"
            record["resolution_note"] = "direct pdf"
            return record
        if kind == "arxiv":
            pdf_url = arxiv_pdf_from_reference(href)
            if not pdf_url:
                record["status"] = "skipped"
                record["resolution_note"] = "could not normalize arxiv link"
                return record
            record["pdf_url"] = pdf_url
            record["status"] = "resolved"
            record["resolution_note"] = "arxiv"
            return record
        if kind == "doi":
            pdf_url, doi, note = resolve_doi_to_pdf(session, href)
            record["doi"] = doi
            record["resolution_note"] = note
            if pdf_url:
                record["pdf_url"] = pdf_url
                record["status"] = "resolved"
            else:
                record["status"] = "unresolved"
            return record
        record["status"] = "skipped"
        record["resolution_note"] = "unsupported reference type"
        return record
    except Exception as exc:
        record["status"] = "failed"
        record["resolution_note"] = str(exc)
        return record


def infer_filename(record: Dict, index: int) -> str:
    stem = record.get("title") or record.get("doi") or Path(urlparse(record["pdf_url"]).path).stem or f"paper_{index:03d}"
    stem = sanitize_name(stem, limit=120)
    return f"{index:03d}_{stem}.pdf"


def download_pdf(session: requests.Session, pdf_url: str, target: Path) -> None:
    with session.get(
        pdf_url,
        timeout=90,
        stream=True,
        allow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"},
    ) as response:
        response.raise_for_status()
        header = response.raw.read(5, decode_content=True)
        if header != b"%PDF-":
            raise RuntimeError(f"response is not a PDF: {response.url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as fh:
            fh.write(header)
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)


def download_record_pdf(record: Dict, target: Path) -> Dict:
    worker_record = dict(record)
    session = requests.Session()
    try:
        download_pdf(session, worker_record["pdf_url"], target)
        worker_record["status"] = "downloaded"
        worker_record["resolution_note"] = worker_record["resolution_note"] or "ok"
    except Exception as exc:
        worker_record["status"] = "failed"
        worker_record["resolution_note"] = str(exc)
    finally:
        session.close()
    return worker_record


def write_outputs(run_dir: Path, source_url: str, extracted: List[Dict], resolved: List[Dict]) -> None:
    manifest = {
        "source_url": source_url,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "extracted_reference_count": len(extracted),
        "resolved_count": sum(1 for item in resolved if item["status"] == "downloaded"),
        "items": resolved,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_lines = [
        "# Consensus PDF 下载结果",
        "",
        f"- 来源页面: {source_url}",
        f"- 抓到引用链接数: {len(extracted)}",
        f"- 成功下载 PDF 数: {sum(1 for item in resolved if item['status'] == 'downloaded')}",
        "",
    ]
    for item in resolved:
        summary_lines.extend(
            [
                f"## {item.get('title') or item.get('doi') or item.get('source_url')}",
                "",
                f"- 状态: {item['status']}",
                f"- 来源类型: {item['source_type']}",
                f"- 来源链接: {item['source_url']}",
                f"- DOI: {item.get('doi') or ''}",
                f"- PDF 链接: {item.get('pdf_url') or ''}",
                f"- 本地文件: {item.get('filename') or ''}",
                f"- 说明: {item.get('resolution_note') or ''}",
                "",
            ]
        )
    (run_dir / "summary.md").write_text("\n".join(summary_lines), encoding="utf-8")

    rows = []
    for item in resolved:
        file_html = ""
        if item.get("filename"):
            file_html = f'<a href="pdfs/{html.escape(item["filename"])}" target="_blank" rel="noopener">打开 PDF</a>'
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.get('title') or item.get('doi') or '')}</td>"
            f"<td>{html.escape(item['status'])}</td>"
            f"<td>{html.escape(item['source_type'])}</td>"
            f"<td><a href=\"{html.escape(item['source_url'])}\" target=\"_blank\" rel=\"noopener\">source</a></td>"
            f"<td>{('<a href=\"' + html.escape(item['pdf_url']) + '\" target=\"_blank\" rel=\"noopener\">pdf</a>') if item.get('pdf_url') else ''}</td>"
            f"<td>{file_html}</td>"
            f"<td>{html.escape(item.get('resolution_note') or '')}</td>"
            "</tr>"
        )
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Consensus PDF 下载结果</title>
  <style>
    body {{ font-family: "IBM Plex Sans", "PingFang SC", sans-serif; margin: 28px; color: #1f2937; background: #f6f5f2; }}
    .card {{ background: white; border-radius: 18px; padding: 20px; box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ padding: 12px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }}
    th {{ background: #f9fafb; }}
    a {{ color: #0f766e; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Consensus PDF 下载结果</h1>
    <p>来源页面：<a href="{html.escape(source_url)}" target="_blank" rel="noopener">{html.escape(source_url)}</a></p>
    <p>抓到引用链接数：{len(extracted)}；成功下载 PDF 数：{sum(1 for item in resolved if item['status'] == 'downloaded')}</p>
    <table>
      <thead>
        <tr><th>标题/DOI</th><th>状态</th><th>类型</th><th>来源</th><th>PDF</th><th>本地文件</th><th>说明</th></tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
  </div>
</body>
</html>"""
    (run_dir / "summary.html").write_text(html_text, encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.max_scrolls <= 0:
        raise SystemExit("--max-scrolls must be greater than 0")
    if args.scroll_pause_ms <= 0:
        raise SystemExit("--scroll-pause-ms must be greater than 0")
    if args.max_downloads < 0:
        raise SystemExit("--max-downloads must be >= 0")

    output_root = Path(args.output_root).resolve()
    run_dir = create_run_dir(output_root, args.url)
    print(f"输出目录: {run_dir}")
    cdp_url = args.cdp_url.strip()
    if cdp_url:
        print(f"[CDP] {cdp_url}")

    with sync_playwright() as playwright:
        if cdp_url and not cdp_ready(cdp_url):
            if not args.auto_launch:
                raise SystemExit(f"CDP 地址不可达: {cdp_url}。请先启动 Chrome 远程调试，或启用自动拉起后再试。")
            cdp_port = parse_cdp_port(cdp_url)
            chrome_proc = launch_chrome_for_cdp(
                chrome_path=args.chrome_path,
                user_data_dir=Path(args.user_data_dir).expanduser().resolve(),
                cdp_port=cdp_port,
            )
            print(f"[CDP] 当前端点不可达，已尝试自动拉起 Chrome: {args.chrome_path}")
            if not wait_cdp_ready(playwright, cdp_url, args.wait_seconds):
                stderr = "(empty)"
                if chrome_proc.stderr and chrome_proc.poll() is not None:
                    stderr = chrome_proc.stderr.read().strip() or "(empty)"
                raise SystemExit(f"CDP 地址在 {args.wait_seconds}s 内仍未就绪: {cdp_url}\nChrome stderr:\n{stderr}")
        browser, page, reuse_mode = build_browser_and_page(playwright, cdp_url, args.headless, args.url, args.reuse_only)
        if page is None:
            browser.close()
            raise SystemExit("当前 CDP 会话里没有已打开的 Consensus 标签页。请先在这个 Chrome 会话里手动打开并登录目标页面，再重试。")
        print(f"[OPEN] {args.url}")
        if reuse_mode in {"exact", "same-site"}:
            if reuse_mode == "exact":
                print("[CDP] 已复用现有 Chrome 中已打开的目标 Consensus 标签页。")
            else:
                print("[CDP] 已复用现有 Chrome 中任意一个已打开的 Consensus 标签页，并将在该页内切换到目标链接。")
            try:
                page.bring_to_front()
            except Exception:
                pass
            wait_for_stable_page(page, settle_ms=1200)
            if page.url.rstrip("/") != args.url.rstrip("/"):
                page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
        else:
            if cdp_url:
                print("[CDP] 未找到任何已打开的 Consensus 标签页，将在现有 Chrome 会话中新开页面。")
            page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
        if is_consensus_login_page(page):
            browser.close()
            raise SystemExit("当前打开的是 Consensus 登录页。请先在同一个 Chrome 会话里完成登录，并确认目标页面可正常浏览后再重试。")
        wait_for_consensus_page(page)
        if is_consensus_login_page(page):
            browser.close()
            raise SystemExit("Consensus 页面跳回了登录态。请先在同一个 Chrome 会话里完成登录，并保持该标签页可正常浏览后再重试。")
        maybe_activate_reference_views(page)
        extracted = collect_reference_candidates(page, args.max_scrolls, args.scroll_pause_ms)
        if not extracted and not looks_like_reference_skeleton(page):
            print("[FALLBACK] 页面里没有直接 DOI/PDF 链接，开始扫描引用编号并尝试从右侧 Paper 面板提取 PDF。")
            extracted = collect_reference_candidates_from_citations(page, args.scroll_pause_ms)
        skeleton_only = looks_like_reference_skeleton(page)
        browser.close()

    print(f"[EXTRACT] references={len(extracted)}")
    if not extracted:
        if skeleton_only:
            raise SystemExit("当前页面只有 Consensus 引用区骨架屏，没拿到真实 DOI / arXiv / PDF 链接。请先在你自己的 Chrome 里手动打开并确认该页面引用列表已经渲染完成，再填入 CDP 地址重试。")
        raise SystemExit("未在页面中找到 DOI / arXiv / PDF 引用链接。建议优先填写 Chrome CDP 地址并复用已打开的真实浏览器页面。")

    session = requests.Session()
    resolved: List[Dict] = []
    download_limit = args.max_downloads or 10**9
    downloaded_urls = set()
    download_index = 0
    download_jobs: List[Tuple[int, Dict, Path]] = []
    for ref in extracted:
        record = resolve_reference_to_pdf(session, ref)
        if record["status"] != "resolved":
            print(f"[SKIP] {record['source_url']} -> {record['resolution_note']}")
            resolved.append(record)
            continue
        if record["pdf_url"] in downloaded_urls:
            record["status"] = "duplicate"
            record["resolution_note"] = "duplicate pdf url"
            print(f"[DUP] {record['pdf_url']}")
            resolved.append(record)
            continue
        if download_index >= download_limit:
            record["status"] = "skipped"
            record["resolution_note"] = f"reached max-downloads={args.max_downloads}"
            resolved.append(record)
            continue
        download_index += 1
        record["filename"] = infer_filename(record, download_index)
        target = run_dir / "pdfs" / record["filename"]
        downloaded_urls.add(record["pdf_url"])
        print(f"[QUEUE] {download_index} {record['pdf_url']}")
        download_jobs.append((download_index, record, target))

    session.close()
    if download_jobs:
        max_workers = min(4, len(download_jobs))
        print(f"[DOWNLOAD] parallel_workers={max_workers} queued={len(download_jobs)}")
        future_map = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for index, record, target in download_jobs:
                future = executor.submit(download_record_pdf, record, target)
                future_map[future] = (index, record["pdf_url"])
            for future in concurrent.futures.as_completed(future_map):
                index, pdf_url = future_map[future]
                try:
                    record = future.result()
                except Exception as exc:
                    print(f"[ERROR] {pdf_url} -> {exc}")
                    continue
                if record["status"] == "downloaded":
                    print(f"[DONE-PDF] {index} {pdf_url}")
                else:
                    print(f"[ERROR] {pdf_url} -> {record['resolution_note']}")
                resolved.append(record)

    resolved.sort(key=lambda item: (item.get("filename") or "zzz", item.get("source_url") or ""))
    write_outputs(run_dir, args.url, extracted, resolved)
    print(f"[DONE] downloaded={sum(1 for item in resolved if item['status'] == 'downloaded')}")


if __name__ == "__main__":
    main()
