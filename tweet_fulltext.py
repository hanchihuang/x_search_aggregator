#!/usr/bin/env python3
"""Hydrate tweet list items with full text from tweet detail pages."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from playwright.sync_api import BrowserContext, Page

from search_x import TEXT_SELECTORS, TWEET_SELECTORS

STATUS_ID_RE = re.compile(r"/status/(\d+)")
NOISE_EXACT_TEXTS = {
    "查看新帖子",
    "Show new posts",
    "重试",
    "Retry",
}
NOISE_SUBSTRINGS = (
    "出错了。请尝试重新加载。",
    "Something went wrong. Try reloading.",
)


def _normalize_lines(lines: List[str]) -> str:
    cleaned: List[str] = []
    seen = set()
    for line in lines:
        text = re.sub(r"\s+", " ", str(line or "").strip())
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return "\n".join(cleaned).strip()


def _extract_text_from_article(article) -> str:
    blocks: List[str] = []
    for selector in TEXT_SELECTORS:
        for el in article.query_selector_all(selector):
            text = (el.inner_text() or "").strip()
            if text:
                blocks.append(text)
    if blocks:
        merged = _normalize_lines(blocks)
        if merged:
            return merged

    all_text = (article.inner_text() or "").strip()
    if not all_text:
        return ""
    lines = [line.strip() for line in all_text.splitlines() if line.strip()]
    return _normalize_lines(lines)


def _is_noise_text(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if not normalized:
        return True
    if normalized in NOISE_EXACT_TEXTS:
        return True
    return any(marker in normalized for marker in NOISE_SUBSTRINGS)


def _looks_like_valid_full_text(text: str, fallback_text: str = "") -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if _is_noise_text(normalized):
        return False
    if len(normalized) < 8 and normalized != re.sub(r"\s+", " ", str(fallback_text or "").strip()):
        return False
    return True


def _find_matching_article(page: Page, tweet_id: str):
    for selector in TWEET_SELECTORS:
        for article in page.query_selector_all(selector):
            href = ""
            try:
                link = article.query_selector(f'a[href*="/status/{tweet_id}"]')
                if link:
                    href = (link.get_attribute("href") or "").strip()
            except Exception:
                href = ""
            if f"/status/{tweet_id}" in href:
                return article
    return None


def extract_full_text_from_page(page: Page, tweet_id: str, fallback_text: str = "") -> str:
    article = _find_matching_article(page, tweet_id)
    if article:
        text = _extract_text_from_article(article)
        if _looks_like_valid_full_text(text, fallback_text):
            return text

    fallback_selectors = [
        'div[data-testid="tweetText"]',
        'article[data-testid="tweet"]',
    ]
    for selector in fallback_selectors:
        for el in page.query_selector_all(selector):
            text = (el.inner_text() or "").strip()
            normalized = _normalize_lines([line for line in text.splitlines() if line.strip()])
            if _looks_like_valid_full_text(normalized, fallback_text):
                return normalized
    return ""


def _write_checkpoint(
    run_dir: Path,
    items: List[Dict],
    progress: Dict,
    raw_name: str,
    final_name: str,
) -> Tuple[Path, Path]:
    raw_path = run_dir / raw_name
    final_path = run_dir / final_name
    progress_path = run_dir / "fulltext_progress.json"
    if not raw_path.exists():
        raw_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    final_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    return final_path, progress_path


def hydrate_items_with_fulltext(
    context: BrowserContext,
    items: List[Dict],
    run_dir: Path,
    raw_name: str = "results_stage1.json",
    final_name: str = "results.json",
    resume: bool = True,
    checkpoint_every: int = 10,
    delay_ms: int = 1200,
    logger: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    total = len(items)
    max_attempts = 3
    progress = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "processed": 0,
        "hydrated": 0,
        "failed": 0,
    }

    _write_checkpoint(run_dir, items, progress, raw_name, final_name)

    for index, item in enumerate(items, start=1):
        url = str(item.get("url") or "").strip()
        tweet_id = str(item.get("tweet_id") or "").strip()
        if not url or not tweet_id:
            item["full_text_status"] = "skipped"
            progress["processed"] += 1
            progress["failed"] += 1
            continue
        existing_status = str(item.get("full_text_status") or "").strip()
        existing_full_text = str(item.get("full_text") or item.get("text") or "").strip()
        if resume and existing_status in {"ok", "retained"} and _looks_like_valid_full_text(
            existing_full_text, str(item.get("card_text") or "")
        ):
            progress["processed"] += 1
            progress["hydrated"] += 1
            continue

        if logger:
            logger(f"[FULLTEXT] {index}/{total} {tweet_id}")

        previous_text = str(item.get("text") or "").strip()
        if previous_text and not item.get("card_text"):
            item["card_text"] = previous_text

        last_error = ""
        full_text = ""
        for attempt in range(1, max_attempts + 1):
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(delay_ms)
                full_text = extract_full_text_from_page(page, tweet_id, previous_text)
                if full_text:
                    break
                last_error = "empty"
            except Exception as exc:
                last_error = str(exc)
                if logger:
                    logger(f"[FULLTEXT][WARN] {tweet_id} attempt {attempt}/{max_attempts}: {exc}")
                try:
                    page.goto("about:blank", wait_until="load", timeout=5000)
                except Exception:
                    pass
                if attempt < max_attempts:
                    page.wait_for_timeout(delay_ms * attempt)
            finally:
                page.close()

        if full_text:
            item["full_text"] = full_text
            item["text"] = full_text
            item["full_text_status"] = "ok"
            item["full_text_fetched_at"] = datetime.now(timezone.utc).isoformat()
            progress["hydrated"] += 1
        elif _looks_like_valid_full_text(previous_text, str(item.get("card_text") or "")):
            item["full_text"] = previous_text
            item["text"] = previous_text
            item["full_text_status"] = "retained"
            progress["hydrated"] += 1
        else:
            item["full_text"] = previous_text
            item["full_text_status"] = "empty" if last_error == "empty" else f"error: {last_error}"
            progress["failed"] += 1
            if logger and last_error and last_error != "empty":
                logger(f"[FULLTEXT][ERROR] {tweet_id}: {last_error}")

        progress["processed"] += 1
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()

        if index % max(1, checkpoint_every) == 0 or index == total:
            _write_checkpoint(run_dir, items, progress, raw_name, final_name)

    return items
