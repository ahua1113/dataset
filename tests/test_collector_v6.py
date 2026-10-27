"""Offline checks: no external HTTP calls and no real papers downloaded."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import collector as c


class FakeResponse:
    def __init__(self, url, data):
        self.url = url
        self._data = data
        self.headers = {"Content-Type": "application/pdf", "Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, chunk_size=262144):
        for off in range(0, len(self._data), chunk_size):
            yield self._data[off:off + chunk_size]


class FakeDownloader:
    def __init__(self, pdf):
        self.pdf = pdf

    def get(self, url, *, stream=False):
        return FakeResponse(url, self.pdf)


class FakeOpenAlexClient(FakeDownloader):
    def __init__(self, pdf):
        super().__init__(pdf)
        self.issn_to_id = {
            j.issn: f"S{1000 + i}" for i, j in enumerate(c.load_journals(ROOT / "config/journals_v6.json"))
        }
        self.id_to_journal = {
            self.issn_to_id[j.issn]: j for j in c.load_journals(ROOT / "config/journals_v6.json")
        }

    def json(self, url):
        if url.startswith(c.OPENALEX_SOURCES):
            issn = url.rsplit("issn:", 1)[-1]
            return {"id": "https://openalex.org/" + self.issn_to_id[issn],
                    "issn": [issn], "issn_l": issn}
        params = parse_qs(urlparse(url).query)
        source_id = params["filter"][0].split("primary_location.source.id:")[1].split(",")[0]
        if int(params["page"][0]) > 1:
            return {"results": []}
        journal = self.id_to_journal[source_id]
        # Only one research article in two journals for the integration test.
        if journal.name not in {"Nature Chemistry", "ACS Central Science"}:
            return {"results": []}
        suffix = "natchem" if journal.name == "Nature Chemistry" else "acs"
        pdf_url = "https://example.edu/repository/" + suffix + ".pdf"
        loc = {"is_oa": True, "pdf_url": pdf_url, "version": "acceptedVersion",
               "license": "cc-by", "landing_page_url": "https://example.edu/paper/" + suffix,
               "source": {"id": "https://openalex.org/S9876", "type": "repository"}}
        source = {"id": "https://openalex.org/" + source_id, "type": "journal"}
        return {"results": [{
            "id": "https://openalex.org/W" + suffix,
            "doi": "https://doi.org/10.9999/" + suffix,
            "title": "Synthetic molecules " + suffix,
            "type": "article", "publication_date": "2026-10-01",
            "open_access": {"is_oa": True, "oa_status": "green"},
            "primary_location": {"is_oa": False, "landing_page_url": "https://publisher.test/" + suffix,
                                 "source": source},
            "best_oa_location": loc,
            "locations": [loc],
            "is_retracted": False, "is_paratext": False,
        }]}


class CollectorV6Tests(unittest.TestCase):
    def setUp(self):
        self.journals = c.load_journals(ROOT / "config/journals_v6.json")

    def test_whitelist_is_exactly_ten_and_retains_original_five(self):
        self.assertEqual(len(self.journals), 10)
        names = {j.name for j in self.journals}
        self.assertIn("Nature Chemical Biology", names)
        self.assertIn("Communications Chemistry", names)
        self.assertIn("Journal of the American Chemical Society", names)
        self.assertNotIn("Nature Communications", names)

    def test_article_filters_by_oa_type_publisher_source(self):
        j = self.journals[0]
        sid = "S555"
        loc = {"is_oa": True, "pdf_url": "https://www.nature.com/articles/a.pdf",
               "version": "publishedVersion", "source": {"id": "https://openalex.org/S555"}}
        work = {"title": "Novel chemistry result", "type": "article",
                "open_access": {"is_oa": True},
                "primary_location": {**loc, "landing_page_url": "https://www.nature.com/articles/a"},
                "best_oa_location": loc, "locations": [loc],
                "doi": "https://doi.org/10.5555/abc"}
        candidate, result = c.article_candidate(work, j, sid)
        self.assertEqual(result, "accepted")
        self.assertEqual(candidate["doi"], "10.5555/abc")
        self.assertEqual(len(candidate["pdf_locations"]), 1)
        self.assertEqual(candidate["pdf_locations"][0]["version"], "publishedVersion")
        work["primary_location"]["source"] = {"id": "https://openalex.org/S444"}
        self.assertIsNone(c.article_candidate(work, j, sid)[0])
        work["primary_location"]["source"] = {"id": "https://openalex.org/S555"}
        work["open_access"]["is_oa"] = False
        self.assertIsNone(c.article_candidate(work, j, sid)[0])
        work["open_access"]["is_oa"] = True
        work["title"] = "Publisher Correction: Novel chemistry result"
        self.assertIsNone(c.article_candidate(work, j, sid)[0])

    def test_pdf_location_publisher_first_and_no_preprints(self):
        j = self.journals[0]
        repo = {"is_oa": True, "pdf_url": "https://archive.example.org/work.pdf",
                "version": "acceptedVersion", "source": {"id": "https://openalex.org/S888"}}
        published = {"is_oa": True, "pdf_url": "https://www.nature.com/articles/work.pdf",
                     "version": "publishedVersion", "source": {"id": "https://openalex.org/S111"}}
        preprint = {"is_oa": True, "pdf_url": "https://arxiv.org/pdf/something.pdf",
                    "version": "submittedVersion", "source": {"id": "https://openalex.org/S888"}}
        results = c.candidate_pdf_locations({"best_oa_location": repo,
                                             "locations": [preprint, published]}, j, "S111")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], published["pdf_url"])

    def test_sha256_duplicate_detected_even_when_url_differs(self):
        pdf = b"%PDF-1.7\n" + b"a" * 2048
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeDownloader(pdf)
            item = {"category": "papers", "title": "Chem paper"}
            status1, record = c.download_file(client, item, root,
                        url="https://example.edu/a.pdf", ext=".pdf", max_file_mb=5, seen_sha=set())
            self.assertEqual(status1, "downloaded")
            self.assertIsNotNone(record)
            status2, record2 = c.download_file(client, item, root,
                        url="https://other.edu/b.pdf", ext=".pdf", max_file_mb=5,
                        seen_sha={record["sha256"]})
            self.assertEqual(status2, "sha_duplicate")
            self.assertIsNone(record2)
            self.assertEqual(len(list((root / "raw" / "papers").glob("*.pdf"))), 1)
            self.assertEqual(c.hash_file(root / record["file"]), record["sha256"])

    def test_integration_openalex_mock_two_journals(self):
        pdf = b"%PDF-1.7\n" + b"z" * 2048
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(
                journals_file=ROOT / "config/journals_v6.json",
                limit=2, per_journal_target=1, dry_run=False, download=True, max_file_mb=5,
                max_pdf_mirrors=2, per_page=50, max_pages_per_journal=1, max_attempts=100)
            c.collect_papers(FakeOpenAlexClient(pdf), args, root)
            rows = [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]
            # Two DOIs, but identical PDF bytes; only one stored.
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "downloaded")
            self.assertEqual(rows[0]["oa_pdf_source"], "institutional_repository")
            self.assertEqual(rows[0]["oa_version"], "acceptedVersion")
            excluded = (root / "papers_v6_3_filtered_out.csv").read_text(encoding="utf-8-sig")
            self.assertIn("duplicate_sha256", excluded)

    def test_candidate_url_is_part_of_predownload_dedup(self):
        item = {"doi": "10.9999/article", "title": "Example paper",
                "pdf_locations": [{"url": "https://example.edu/a.pdf"}]}
        self.assertIn("url:https://example.edu/a.pdf", c.identity_keys(item))

    def test_existing_nonwhite_journal_rejected_to_protect_v6(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw" / "papers").mkdir(parents=True)
            content = b"%PDF-1.7" + b"X" * 2000
            (root / "raw" / "papers" / "old.pdf").write_bytes(content)
            c.append_jsonl(root / "manifest.jsonl", {
                "category": "papers", "status": "downloaded", "journal": "Some Other Journal",
                "file": "raw/papers/old.pdf", "bytes": len(content),
                "sha256": c.hash_file(root / "raw" / "papers" / "old.pdf"),
            })
            with self.assertRaisesRegex(RuntimeError, "non-whitelist journal"):
                c.load_existing_records(root, category="papers", permitted_journals={j.name for j in self.journals})


if __name__ == "__main__":
    unittest.main()
