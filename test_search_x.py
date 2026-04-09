import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import TimeoutError

from search_keyword_500 import merge_items_with_network_recovery, navigate_with_retry, open_search_with_recovery
from search_x import END_MARKER_SELECTORS, TWEET_SELECTORS, wait_for_search_results
from tweet_fulltext import hydrate_items_with_fulltext
from web_app import resolve_fulltext_stats


class FakePage:
    def __init__(self, successful_selectors=None):
        self.successful_selectors = set(successful_selectors or [])
        self.calls = []

    def wait_for_selector(self, selector, timeout=None):
        self.calls.append((selector, timeout))
        if selector in self.successful_selectors:
            return object()
        raise TimeoutError("not found")

    def query_selector(self, selector):
        if selector in self.successful_selectors:
            return object()
        return None

    def query_selector_all(self, selector):
        if selector in self.successful_selectors:
            return [object()]
        return []

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))


class WaitForSearchResultsTests(unittest.TestCase):
    def test_does_not_treat_generic_main_container_as_loaded_results(self):
        page = FakePage(successful_selectors={"main, section, div[role=\"feed\"]"})

        ready = wait_for_search_results(page, timeout=1000)

        self.assertFalse(ready)

    def test_accepts_visible_tweet_card(self):
        page = FakePage(successful_selectors={TWEET_SELECTORS[0]})

        ready = wait_for_search_results(page, timeout=1000)

        self.assertTrue(ready)

    def test_accepts_end_marker_when_search_has_no_more_results(self):
        page = FakePage(successful_selectors={END_MARKER_SELECTORS[0]})

        ready = wait_for_search_results(page, timeout=1000)

        self.assertTrue(ready)


class NetworkRecoveryTests(unittest.TestCase):
    def test_merges_network_items_when_dom_collection_stalls(self):
        dom_items = [
            {"tweet_id": "1", "text": "from dom", "posted_at": "2026-04-05T01:00:00.000Z"},
        ]
        network_items = [
            {"tweet_id": "1", "text": "duplicate from network", "posted_at": "2026-04-05T01:00:00.000Z"},
            {"tweet_id": "2", "text": "only in network", "posted_at": "2026-04-05T00:59:00.000Z"},
        ]

        merged, recovered = merge_items_with_network_recovery(dom_items, network_items)

        self.assertTrue(recovered)
        self.assertEqual(["1", "2"], [item["tweet_id"] for item in merged])
        self.assertEqual("from dom", merged[0]["text"])


class FakeGotoPage:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.goto_calls = 0
        self.waits = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


class NavigateWithRetryTests(unittest.TestCase):
    def test_retries_transient_err_network_changed(self):
        page = FakeGotoPage([RuntimeError("Page.goto: net::ERR_NETWORK_CHANGED"), object()])
        logs = []

        ok = navigate_with_retry(page, "https://x.com/search?q=test", logger=logs.append)

        self.assertTrue(ok)
        self.assertEqual(2, page.goto_calls)
        self.assertTrue(any("ERR_NETWORK_CHANGED" in line for line in logs))

    def test_stops_retrying_on_non_transient_navigation_error(self):
        page = FakeGotoPage([RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")])
        logs = []

        ok = navigate_with_retry(page, "https://x.com/search?q=test", logger=logs.append)

        self.assertFalse(ok)
        self.assertEqual(1, page.goto_calls)


class RecoveryNavigationTests(unittest.TestCase):
    def test_open_search_recovery_ignores_destroyed_execution_context_while_page_is_navigating(self):
        class FakeRecoveryPage:
            def __init__(self):
                self.query_calls = 0

            def query_selector(self, selector):
                self.query_calls += 1
                raise RuntimeError(
                    "Page.query_selector: Execution context was destroyed, most likely because of a navigation"
                )

        page = FakeRecoveryPage()

        with patch("search_keyword_500.navigate_with_retry", return_value=True), patch(
            "search_keyword_500.wait_for_search_results", return_value=True
        ):
            open_search_with_recovery(page, "https://x.com/search?q=test", "test", "Latest", "")


class FakeHydrationPage:
    def goto(self, url, wait_until=None, timeout=None):
        return None

    def wait_for_timeout(self, timeout):
        return None

    def query_selector_all(self, selector):
        return []

    def close(self):
        return None


class FakeHydrationContext:
    def new_page(self):
        return FakeHydrationPage()


class FulltextHydrationTests(unittest.TestCase):
    def test_retains_existing_card_text_without_counting_failure(self):
        items = [
            {
                "tweet_id": "123",
                "url": "https://x.com/example/status/123",
                "text": "Existing card text that is already usable.",
            }
        ]

        with TemporaryDirectory() as tmpdir:
            hydrated = hydrate_items_with_fulltext(
                FakeHydrationContext(),
                items,
                Path(tmpdir),
                checkpoint_every=1,
                delay_ms=0,
            )

            self.assertEqual("retained", hydrated[0]["full_text_status"])
            self.assertEqual("Existing card text that is already usable.", hydrated[0]["full_text"])
            stats = resolve_fulltext_stats({}, Path(tmpdir))
            self.assertEqual(1, stats["fulltext_hydrated"])
            self.assertEqual(0, stats["fulltext_failed"])


class WebAppFulltextStatsTests(unittest.TestCase):
    def test_progress_file_zero_values_override_stale_task_values(self):
        task = {
            "fulltext_total": 69,
            "fulltext_processed": 69,
            "fulltext_hydrated": 69,
            "fulltext_failed": 69,
        }

        with TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            (run_dir / "fulltext_progress.json").write_text(
                '{"total": 69, "processed": 69, "hydrated": 69, "failed": 0}',
                encoding="utf-8",
            )

            stats = resolve_fulltext_stats(task, run_dir)

            self.assertEqual(69, stats["fulltext_total"])
            self.assertEqual(69, stats["fulltext_processed"])
            self.assertEqual(69, stats["fulltext_hydrated"])
            self.assertEqual(0, stats["fulltext_failed"])


if __name__ == "__main__":
    unittest.main()
