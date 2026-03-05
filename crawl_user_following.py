#!/usr/bin/env python3
"""Crawl all accounts followed by a target user on X and generate detailed reports."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from search_x import create_context, scroll_feed, validate_auth_state

HANDLE_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})/?$")
ZH_RE = re.compile(r"[\u4e00-\u9fff]")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "you", "your", "our", "are", "was",
    "一个", "这个", "那个", "我们", "你们", "他们", "关注", "简介", "没有", "默认", "用户",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl a user's following list on X")
    p.add_argument("--user-url", required=True, help="User URL, e.g. https://x.com/vista8")
    p.add_argument("--state", default="auth_state.json", help="Playwright storage state path")
    p.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    p.add_argument("--out-dir", default="output", help="Output base directory")
    p.add_argument("--max-items", type=int, default=0, help="Max accounts to collect (0 means no hard limit)")
    p.add_argument("--max-scrolls", type=int, default=1200, help="Max scroll rounds")
    p.add_argument("--no-new-stop", type=int, default=35, help="Stop after N rounds with no new users")
    p.add_argument("--scroll-pause", type=int, default=1400, help="Pause between scrolls in ms")
    return p.parse_args()


def safe_name(text: str) -> str:
    text = re.sub(r"\s+", "_", str(text).strip())
    text = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "", text)
    return text[:80] or "user"


def parse_handle(user_url: str) -> str:
    u = user_url.strip()
    if u.startswith("@"):
        return u[1:]
    if u.startswith("http://") or u.startswith("https://"):
        path = urlparse(u).path.strip("/")
        if not path:
            raise ValueError(f"Invalid --user-url: {user_url}")
        return path.split("/")[0].lstrip("@")
    return u.strip("/").lstrip("@")


def extract_user_cards(page):
    selectors = [
        '[data-testid="UserCell"]',
        'div[data-testid="cellInnerDiv"]:has(a[href^="/"])',
    ]
    cards = []
    for sel in selectors:
        cards.extend(page.query_selector_all(sel))
    return cards


def extract_following_user(card) -> dict | None:
    links = card.query_selector_all('a[href^="/"]')
    handle = ""
    profile_href = ""
    for link in links:
        href = (link.get_attribute("href") or "").split("?")[0]
        m = HANDLE_RE.match(href)
        if m:
            handle = m.group(1)
            profile_href = href
            break
    if not handle:
        return None

    user_name = ""
    name_block = card.query_selector('[data-testid="User-Name"]')
    if name_block:
        spans = name_block.query_selector_all("span")
        for sp in spans:
            t = (sp.inner_text() or "").strip()
            if t and not t.startswith("@"):
                user_name = t
                break

    bio = ""
    bio_el = card.query_selector('[data-testid="UserDescription"]')
    if bio_el:
        bio = (bio_el.inner_text() or "").strip()

    card_text = (card.inner_text() or "")
    verified = any(x in card_text for x in ["Verified", "已认证", "已验证"]) or bool(
        card.query_selector('svg[aria-label*="Verified"], svg[aria-label*="已认证"], svg[aria-label*="已验证"]')
    )

    return {
        "handle": handle,
        "name": user_name,
        "bio": bio,
        "verified": bool(verified),
        "profile_url": f"https://x.com{profile_href}",
    }


def collect_following(page, max_items: int, max_scrolls: int, no_new_stop: int, scroll_pause: int) -> list[dict]:
    seen = {}
    no_new_rounds = 0

    for idx in range(max_scrolls):
        cards = extract_user_cards(page)
        new_count = 0

        for card in cards:
            item = extract_following_user(card)
            if not item:
                continue
            h = item["handle"].lower()
            if h in seen:
                continue
            seen[h] = item
            new_count += 1
            if max_items > 0 and len(seen) >= max_items:
                print(f"Reached max items: {max_items}")
                return list(seen.values())

        print(f"Scroll {idx + 1}/{max_scrolls}: +{new_count} new, total {len(seen)}")

        if new_count == 0:
            no_new_rounds += 1
        else:
            no_new_rounds = 0

        if no_new_rounds >= no_new_stop:
            print(f"No new users for {no_new_rounds} rounds. Stop scrolling.")
            break

        scroll_feed(page, idx)
        page.wait_for_timeout(scroll_pause if new_count > 0 else int(scroll_pause * 1.35))

    return list(seen.values())


def analyze_following(items: list[dict], owner_handle: str) -> dict:
    total = len(items)
    bios = [i.get("bio", "") for i in items]
    names = [i.get("name", "") for i in items]

    verified_count = sum(1 for i in items if i.get("verified"))
    with_bio = sum(1 for b in bios if b.strip())
    zh_bio = sum(1 for b in bios if ZH_RE.search(b or ""))

    tokens = Counter()
    for txt in bios + names:
        for tk in WORD_RE.findall((txt or "").lower()):
            if tk in STOPWORDS or len(tk) < 2:
                continue
            tokens[tk] += 1

    first_letter = Counter()
    for i in items:
        h = (i.get("handle") or "").strip()
        if h:
            first_letter[h[0].lower()] += 1

    domains = Counter()
    for i in items:
        u = i.get("profile_url", "")
        if u:
            domains[urlparse(u).netloc] += 1

    return {
        "target_user": owner_handle,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_following_collected": total,
        "profile_stats": {
            "verified_count": verified_count,
            "verified_ratio": round(verified_count / total, 4) if total else 0,
            "bio_filled_count": with_bio,
            "bio_filled_ratio": round(with_bio / total, 4) if total else 0,
            "bio_has_chinese_count": zh_bio,
            "bio_has_chinese_ratio": round(zh_bio / total, 4) if total else 0,
        },
        "top_keywords": [{"keyword": k, "count": v} for k, v in tokens.most_common(60)],
        "handle_initial_distribution": [{"initial": k, "count": v} for k, v in first_letter.most_common()],
        "top_domains": [{"domain": k, "count": v} for k, v in domains.most_common()],
    }


def write_csv(path: Path, items: list[dict]) -> None:
    fields = ["handle", "name", "bio", "verified", "profile_url"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(items)


def write_detailed_md(path: Path, analysis: dict) -> None:
    s = analysis["profile_stats"]
    lines = [
        f"# Following Detailed Report: @{analysis['target_user']}",
        "",
        f"- Generated at (UTC): {analysis['generated_at_utc']}",
        f"- Total following collected: {analysis['total_following_collected']}",
        f"- Verified ratio: {s['verified_ratio']}",
        f"- Bio filled ratio: {s['bio_filled_ratio']}",
        f"- Bio has Chinese ratio: {s['bio_has_chinese_ratio']}",
        "",
        "## Top keywords from name/bio",
    ]
    lines.extend([f"- {x['keyword']}: {x['count']}" for x in analysis["top_keywords"][:30]])
    lines.append("")
    lines.append("## Handle initials")
    lines.extend([f"- {x['initial']}: {x['count']}" for x in analysis["handle_initial_distribution"][:30]])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_detailed_html(path: Path, analysis: dict) -> None:
    s = analysis["profile_stats"]
    kw_rows = "".join(
        f"<tr><td>{x['keyword']}</td><td>{x['count']}</td></tr>" for x in analysis["top_keywords"][:40]
    )
    html_doc = f"""<!doctype html>
<html lang=\"en\"><head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>@{analysis['target_user']} following detailed report</title>
<style>
body{{font-family:"IBM Plex Sans","Noto Sans",sans-serif;background:#f6f4ef;margin:0;color:#1f1f1c;}}
.wrap{{max-width:1000px;margin:0 auto;padding:24px;}}
.card{{background:#fff;border:1px solid #dfd7ca;border-radius:12px;padding:16px;margin-bottom:14px;}}
table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #ece5d9;padding:8px;text-align:left}}th{{background:#f4efe6}}
</style></head>
<body><main class=\"wrap\">
<section class=\"card\"><h1>@{analysis['target_user']} Following 详细分析</h1>
<p>Total: {analysis['total_following_collected']}</p>
<p>Verified ratio: {s['verified_ratio']}</p>
<p>Bio filled ratio: {s['bio_filled_ratio']}</p>
<p>Bio has Chinese ratio: {s['bio_has_chinese_ratio']}</p>
</section>
<section class=\"card\"><h2>Top Keywords</h2>
<table><thead><tr><th>Keyword</th><th>Count</th></tr></thead><tbody>{kw_rows}</tbody></table>
</section>
</main></body></html>"""
    path.write_text(html_doc, encoding="utf-8")


def main() -> None:
    args = parse_args()
    owner = parse_handle(args.user_url)

    out_base = Path(args.out_dir).expanduser().resolve()
    run_dir = out_base / f"following_{safe_name(owner)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    target_url = f"https://x.com/{owner}/following"
    print(f"Target user: @{owner}")
    print(f"Following URL: {target_url}")

    with sync_playwright() as p:
        context = create_context(p, args.state, args.headless)
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        if not validate_auth_state(page):
            print("Authentication issue detected. Please refresh login state with login_x.py.")
            context.close()
            return

        items = collect_following(
            page,
            max_items=args.max_items,
            max_scrolls=args.max_scrolls,
            no_new_stop=args.no_new_stop,
            scroll_pause=args.scroll_pause,
        )
        context.close()

    if not items:
        print("Warning: No following users were collected.")
        return

    analysis = analyze_following(items, owner)

    json_path = run_dir / "results.json"
    csv_path = run_dir / "results.csv"
    detail_json = run_dir / "detailed_report.json"
    detail_md = run_dir / "detailed_report.md"
    detail_html = run_dir / "detailed_report.html"

    json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, items)
    detail_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    write_detailed_md(detail_md, analysis)
    write_detailed_html(detail_html, analysis)

    print("=" * 60)
    print(f"Done. Collected {len(items)} following users from @{owner}.")
    print(f"Results JSON:      {json_path}")
    print(f"Results CSV:       {csv_path}")
    print(f"Detailed JSON:     {detail_json}")
    print(f"Detailed Markdown: {detail_md}")
    print(f"Detailed HTML:     {detail_html}")
    print("=" * 60)


if __name__ == "__main__":
    main()
