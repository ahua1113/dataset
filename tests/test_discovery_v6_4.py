"""V6.4 论文身份筛选与OA位置排序测试。"""
from pathlib import Path
import unittest

from chemistry_collector.discovery import article_candidate, candidate_pdf_locations
from chemistry_collector.models import load_journals


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "config" / "journals_v6_4.json"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.journal = load_journals(JOURNALS)[0]

    def test_article_requires_oa_doi_and_matching_primary_journal(self):
        source_id = "S555"
        location = {
            "is_oa": True,
            "pdf_url": "https://www.nature.com/articles/a.pdf",
            "landing_page_url": "https://www.nature.com/articles/a",
            "version": "publishedVersion",
            "source": {"id": "https://openalex.org/S555"},
        }
        work = {
            "title": "Novel chemistry result",
            "type": "article",
            "open_access": {"is_oa": True},
            "primary_location": location,
            "best_oa_location": location,
            "locations": [location],
            "doi": "https://doi.org/10.5555/abc",
        }
        candidate, result = article_candidate(work, self.journal, source_id)
        self.assertEqual("accepted", result)
        self.assertEqual("10.5555/abc", candidate["doi"])

        work["primary_location"]["source"] = {"id": "https://openalex.org/S444"}
        self.assertIsNone(article_candidate(work, self.journal, source_id)[0])
        work["primary_location"]["source"] = {"id": "https://openalex.org/S555"}
        work["open_access"]["is_oa"] = False
        self.assertIsNone(article_candidate(work, self.journal, source_id)[0])

    def test_publisher_location_ranks_before_repository_and_preprint_is_optional(self):
        source_id = "S111"
        repository = {
            "is_oa": True,
            "pdf_url": "https://archive.example.org/work.pdf",
            "version": "acceptedVersion",
            "source": {"id": "https://openalex.org/S888", "type": "repository"},
        }
        publisher = {
            "is_oa": True,
            "pdf_url": "https://www.nature.com/articles/work.pdf",
            "version": "publishedVersion",
            "source": {"id": "https://openalex.org/S111"},
        }
        preprint = {
            "is_oa": True,
            "pdf_url": "https://www.biorxiv.org/content/10.1101/123.full.pdf",
            "version": "acceptedVersion",
            "source": {"id": "https://openalex.org/S888", "type": "repository"},
        }
        work = {"best_oa_location": repository, "locations": [preprint, publisher]}
        locations = candidate_pdf_locations(work, self.journal, source_id)
        self.assertEqual(2, len(locations))
        self.assertEqual(publisher["pdf_url"], locations[0]["url"])
        self.assertNotIn("biorxiv", {row["source_type"] for row in locations})


if __name__ == "__main__":
    unittest.main()
