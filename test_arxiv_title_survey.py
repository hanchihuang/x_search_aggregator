import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

import fitz

from arxiv_title_survey import (
    Paper,
    build_summary_html,
    build_survey_markdown,
    build_title_query,
    convert_pdf_and_delete,
    run,
    title_contains_all_keywords,
)


class TitleFilterTests(unittest.TestCase):
    def test_requires_all_keyword_tokens_in_title(self):
        self.assertTrue(
            title_contains_all_keywords(
                "Multi-Agent Planning with Structured Reasoning",
                "multi agent reasoning",
            )
        )
        self.assertFalse(
            title_contains_all_keywords(
                "Planning with Structured Reasoning",
                "multi agent reasoning",
            )
        )

    def test_query_targets_title_field_only(self):
        self.assertEqual('ti:"multi" AND ti:"agent"', build_title_query("multi agent"))


class PdfCleanupTests(unittest.TestCase):
    def test_deletes_pdf_after_markdown_conversion(self):
        paper = Paper(
            arxiv_id="1234.5678",
            title="Test Paper",
            summary="We propose a test method.",
            published="2026-04-09T00:00:00Z",
            updated="2026-04-09T00:00:00Z",
            authors=["Alice", "Bob"],
            abs_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://arxiv.org/pdf/1234.5678.pdf",
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            pdf_path = tmp / "paper.pdf"
            md_path = tmp / "paper.md"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Hello PDF")
            doc.save(pdf_path)
            doc.close()

            convert_pdf_and_delete(pdf_path, md_path, paper)

            self.assertFalse(pdf_path.exists())
            self.assertTrue(md_path.exists())
            self.assertIn("# Test Paper", md_path.read_text(encoding="utf-8"))


class PartialCorpusTests(unittest.TestCase):
    def test_run_keeps_actual_count_when_less_than_requested(self):
        paper = Paper(
            arxiv_id="1234.5678",
            title="GSM8K Test Paper",
            summary="We propose a test method. Results improve performance.",
            published="2026-04-09T00:00:00Z",
            updated="2026-04-09T00:00:00Z",
            authors=["Alice"],
            abs_url="https://arxiv.org/abs/1234.5678",
            pdf_url="https://arxiv.org/pdf/1234.5678.pdf",
        )
        with TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "out"

            def fake_download(_pdf_url, pdf_path):
                doc = fitz.open()
                page = doc.new_page()
                page.insert_text((72, 72), "Hello PDF")
                doc.save(pdf_path)
                doc.close()

            with patch("arxiv_title_survey.fetch_arxiv_papers", return_value=[paper]), patch(
                "arxiv_title_survey.download_pdf", side_effect=fake_download
            ):
                run_dir = run("gsm8k", 10, output_root, 100)

            manifest = (run_dir / "manifest.json").read_text(encoding="utf-8")
            self.assertIn('"requested_limit": 10', manifest)
            self.assertIn('"actual_count": 1', manifest)
            survey = (run_dir / "survey.md").read_text(encoding="utf-8")
            self.assertIn("- Requested corpus size: 10 papers", survey)
            self.assertIn("- Actual corpus size: 1 papers", survey)


if __name__ == "__main__":
    unittest.main()
