#!/usr/bin/env python3

import unittest

from extract_research_mentions import extract_research_mentions


class ExtractResearchMentionsTest(unittest.TestCase):
    def test_extracts_papers_models_and_tricks(self):
        items = [
            {
                "tweet_id": "1",
                "url": "https://x.com/a/status/1",
                "posted_at": "2026-04-10T00:00:00+00:00",
                "text": 'New paper "Scaling Test-Time Compute for Qwen2.5-72B" on arXiv https://arxiv.org/abs/2501.01234 introduces Qwen2.5-72B reasoning improvements.',
            },
            {
                "tweet_id": "2",
                "url": "https://x.com/a/status/2",
                "posted_at": "2026-04-10T01:00:00+00:00",
                "text": "A useful trick: force the model to write a verifier pass before final answer. This hack reduced mistakes for Claude 3.7 Sonnet.",
            },
        ]

        result = extract_research_mentions(items)

        self.assertEqual(result["summary"]["paper_tweet_count"], 1)
        self.assertEqual(result["summary"]["paper_title_candidate_count"], 1)
        self.assertTrue(any(model["name"] == "Qwen2.5-72B" for model in result["models"]))
        self.assertTrue(any(model["name"] == "Claude 3.7 Sonnet" for model in result["models"]))
        self.assertEqual(result["summary"]["trick_tweet_count"], 1)
        self.assertEqual(result["papers"][0]["arxiv_ids"], ["2501.01234"])
        self.assertEqual(result["papers"][0]["title_candidates"], ["Scaling Test-Time Compute for Qwen2.5-72B"])


if __name__ == "__main__":
    unittest.main()
