#!/usr/bin/env python3
"""Browser configuration with anti-detection measures for X scraping."""

from __future__ import annotations

from shutil import which
from typing import Any, Dict

def get_browser_args() -> list:
    """Get browser arguments to reduce detection."""
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--disable-gpu",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-blink-features=AutomationControlled",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

def get_context_options() -> Dict[str, Any]:
    """Get context options to reduce fingerprint detection."""
    return {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "bypass_csp": True,
        "java_script_enabled": True,
        "ignore_https_errors": True,
        "permissions": ["geolocation"],
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    }

def get_launch_options(headless: bool = False) -> Dict[str, Any]:
    """Get browser launch options."""
    return {
        "headless": headless,
        "args": get_browser_args(),
        "ignore_default_args": ["--enable-automation"],
    }


def find_system_chromium() -> str | None:
    """Return a usable system Chromium/Chrome path when Playwright browsers are absent."""
    for candidate in (
        "chromium-browser",
        "chromium",
        "google-chrome",
        "google-chrome-stable",
    ):
        resolved = which(candidate)
        if resolved:
            return resolved
    return None


def get_playwright_launch_kwargs(headless: bool = False, args: list | None = None) -> Dict[str, Any]:
    """Build Playwright launch kwargs with a system-browser fallback."""
    launch_args = list(args if args is not None else get_browser_args())
    launch_kwargs: Dict[str, Any] = {
        "headless": headless,
        "args": launch_args,
    }
    executable_path = find_system_chromium()
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    return launch_kwargs
