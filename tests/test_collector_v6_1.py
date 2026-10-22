import json
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import collector


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "config" / "journals_v6_1.json"


class CollectorV61Tests(unittest.TestCase):
    def test_whitelist_has_twenty_unique_valid_journals(self):
        journals = collector.load_journals(JOURNALS)
        self.assertEqual(20, len(journals))
        self.assertEqual(20, len({j.name.casefold() for j in journals}))
        self.assertEqual(20, len({j.issn for j in journals}))

    def test_whitelist_rejects_wrong_size(self):
        rows = json.loads(JOURNALS.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journals.json"
            path.write_text(json.dumps(rows[:-1]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 20"):
                collector.load_journals(path)

    def test_dry_run_requires_target_from_every_journal(self):
        target = 20

        def candidates(_client, journal, _per_page, _pages, _error_path):
            for index in range(target + 2):
                yield {
                    "category": "papers",
                    "resource_kind": "research_article",
                    "title": f"{journal.name} test article {index}",
                    "journal": journal.name,
                    "journal_issn": journal.issn,
                    "publisher": journal.publisher,
                    "doi": f"10.9999/{journal.issn}.{index}",
                    "openalex_id": f"W{journal.issn.replace('-', '')}{index}",
                    "download_url": "",
                    "pdf_locations": [{"url": f"https://example.org/{journal.issn}/{index}.pdf"}],
                }

        args = SimpleNamespace(
            journals_file=JOURNALS,
            per_journal_target=target,
            dry_run=True,
            download=False,
            per_page=10,
            max_pages_per_journal=1,
            max_attempts=1000,
            max_pdf_mirrors=1,
            max_file_mb=10,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                collector, "journal_article_iterator", side_effect=candidates):
            out = Path(tmp)
            with redirect_stdout(io.StringIO()):
                collector.collect_papers(object(), args, out)
            report = json.loads(
                (out / "journal_feasibility_v6_1.json").read_text(encoding="utf-8")
            )
            self.assertTrue(report["all_journals_meet_target"])
            self.assertEqual(20, report["journals_meeting_target"])
            self.assertTrue(all(row["eligible_candidates"] == target
                                for row in report["journals"]))


if __name__ == "__main__":
    unittest.main()
