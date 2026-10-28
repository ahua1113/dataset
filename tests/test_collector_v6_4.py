"""V6.4 项目结构、期刊策略与服务器入口的离线回归测试。"""
import json
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import collector
from chemistry_collector.client import Client, SourceError
from chemistry_collector.disciplines import (
    classify_source,
    classify_work,
    load_disciplines,
    openalex_locations,
    unpaywall_locations,
)
from chemistry_collector.policy import (
    apply_journal_policies,
    load_journal_policies,
    policy_for,
)


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "config" / "journals_v6_4.json"
POLICY = ROOT / "config" / "journal_policy_v6_4.json"
DISCIPLINES = ROOT / "config" / "disciplines_v6_4.json"


# 本测试类集中验证配置与采集规则的兼容性，避免依赖在线服务。
class CollectorV64StructureAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.journals = collector.load_journals(JOURNALS)
        self.policies = load_journal_policies(POLICY)

    def test_v64_keeps_42_traceable_journals_but_skips_three_low_yield(self):
        raw = json.loads(JOURNALS.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, list)
        self.assertEqual(42, len(self.journals))
        selected, skipped = apply_journal_policies(self.journals, self.policies)
        self.assertEqual(39, len(selected))
        self.assertEqual(
            {"ACS Omega", "RSC Advances", "ChemistryOpen"},
            {journal.name for journal, _ in skipped},
        )

    def test_successful_candidate_sources_run_before_low_efficiency_sources(self):
        candidates = [journal for journal in self.journals if journal.status == "candidate"]
        selected, _ = apply_journal_policies(candidates, self.policies)
        self.assertEqual(
            [
                "Beilstein Journal of Organic Chemistry",
                "Beilstein Journal of Nanotechnology",
                "Journal of Cheminformatics",
            ],
            [journal.name for journal in selected[:3]],
        )
        jacs = next(journal for journal in selected if journal.name == "JACS Au")
        self.assertEqual(80, policy_for(self.policies, jacs).max_download_attempts)

    def test_low_yield_sources_can_only_return_with_explicit_override(self):
        selected, skipped = apply_journal_policies(
            self.journals, self.policies, include_low_yield=True
        )
        self.assertEqual(42, len(selected))
        self.assertEqual([], skipped)

    def test_domain_circuit_opens_after_repeated_access_failures(self):
        client = Client(respect_robots=False, domain_failure_threshold=2)
        client.record_access_failure("blocked.example")
        client.record_access_failure("blocked.example")
        self.assertTrue(client.is_circuit_open("https://blocked.example/paper.pdf"))
        self.assertFalse(client.is_circuit_open("https://mirror.example/paper.pdf"))
        with self.assertRaisesRegex(SourceError, "circuit open"):
            client.get("https://blocked.example/paper.pdf", stream=True)

    def test_biorxiv_and_medrxiv_are_preprints_not_repositories(self):
        self.assertEqual(
            "biorxiv",
            classify_source(
                "https://www.biorxiv.org/content/10.1101/123.full.pdf",
                "", (), "repository",
            ),
        )
        self.assertEqual(
            "medrxiv",
            classify_source(
                "https://www.medrxiv.org/content/10.1101/456.full.pdf",
                "", (), "repository",
            ),
        )

    def test_preprint_switch_blocks_biorxiv_for_both_metadata_providers(self):
        openalex_location = {
            "is_oa": True,
            "pdf_url": "https://www.biorxiv.org/content/10.1101/123.full.pdf",
            "landing_page_url": "https://www.biorxiv.org/content/10.1101/123",
            "version": "acceptedVersion",
            "license": "cc-by-nc-nd",
            "source": {"type": "repository", "id": "https://openalex.org/S999"},
        }
        work = {"locations": [openalex_location]}
        self.assertEqual([], openalex_locations(work, (), "S111", allow_preprints=False))
        allowed = openalex_locations(work, (), "S111", allow_preprints=True)
        self.assertEqual("biorxiv", allowed[0]["source_type"])
        self.assertEqual("submittedVersion", allowed[0]["version"])

        unpaywall_location = {
            "url_for_pdf": "https://www.medrxiv.org/content/10.1101/456.full.pdf",
            "url_for_landing_page": "https://www.medrxiv.org/content/10.1101/456",
            "host_type": "repository",
            "version": "acceptedVersion",
            "license": "cc-by",
        }
        data = {"oa_locations": [unpaywall_location]}
        self.assertEqual([], unpaywall_locations(data, (), allow_preprints=False))
        allowed = unpaywall_locations(data, (), allow_preprints=True)
        self.assertEqual("medrxiv", allowed[0]["source_type"])
        self.assertEqual("submittedVersion", allowed[0]["version"])

    def test_real_auxetic_dna_title_is_classified_as_materials(self):
        result = classify_work(
            {
                "title": (
                    "Computational Design Strategies for Nanoscale 3D Auxetic "
                    "Metastructures from DNA"
                ),
                "topics": [{"display_name": "Cellular"}],
            },
            "综合化学",
            load_disciplines(DISCIPLINES),
        )
        self.assertEqual("materials_nanochemistry", result["primary_discipline"])
        self.assertGreater(result["discipline_score"], 3)

    def test_open_circuit_is_not_counted_as_http_attempt(self):
        class FakeResponse:
            url = "https://mirror.example/paper.pdf"
            headers = {
                "Content-Type": "application/pdf",
                "Content-Length": "2057",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_content(self, chunk_size=262144):
                del chunk_size
                yield b"%PDF-1.7\n" + b"x" * 2048

        class FakeClient:
            def is_circuit_open(self, url):
                return "blocked.example" in url

            def get(self, url, *, stream=False):
                self_url = url
                del stream
                if "blocked.example" in self_url:
                    raise AssertionError("open circuit must be skipped before client.get")
                return FakeResponse()

        def candidates(_client, journal, *_args, **_kwargs):
            yield {
                "category": "papers",
                "resource_kind": "research_article",
                "title": "Circuit fallback chemistry",
                "journal": journal.name,
                "journal_issn": journal.issn,
                "publisher": journal.publisher,
                "doi": "10.9999/circuit-fallback",
                "openalex_id": "W123",
                "pdf_locations": [
                    {
                        "url": "https://blocked.example/paper.pdf",
                        "version": "publishedVersion",
                        "oa_host": "blocked.example",
                        "landing_page": "https://blocked.example/article",
                        "license": "CC-BY",
                        "license_status": "verified_open_license",
                        "publisher_pdf": True,
                        "source_type": "publisher",
                        "metadata_provider": "openalex",
                        "fallback_rank": 1,
                    },
                    {
                        "url": "https://mirror.example/paper.pdf",
                        "version": "acceptedVersion",
                        "oa_host": "mirror.example",
                        "landing_page": "https://mirror.example/article",
                        "license": "CC-BY",
                        "license_status": "verified_open_license",
                        "publisher_pdf": False,
                        "source_type": "institutional_repository",
                        "metadata_provider": "openalex",
                        "fallback_rank": 2,
                    },
                ],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journals_path = root / "journals.json"
            journals_path.write_text(json.dumps([{
                "name": "Circuit Journal",
                "issn": "1234-567X",
                "publisher": "Example",
                "website": "https://publisher.example/journal",
                "group": "综合化学",
                "origin": "test",
                "publisher_hosts": ["publisher.example"],
                "status": "candidate",
            }]), encoding="utf-8")
            args = SimpleNamespace(
                journals_file=journals_path,
                journal_stage="all",
                journal_policy_file=None,
                include_low_yield_journals=False,
                disciplines_file=None,
                allow_preprint_fallback=False,
                per_page=10,
                max_pages_per_journal=1,
                per_journal_target=1,
                dry_run=False,
                download=True,
                max_attempts=10,
                use_unpaywall=False,
                max_pdf_mirrors=2,
                max_file_mb=5,
            )
            with patch.object(collector, "journal_article_iterator", side_effect=candidates):
                with redirect_stdout(io.StringIO()):
                    collector.collect_papers(FakeClient(), args, root)
            report = json.loads(
                (root / "journal_feasibility_v6_4.json").read_text(encoding="utf-8")
            )
            row = report["journals"][0]
            self.assertEqual(1, row["download_attempts"])
            self.assertEqual(1, row["circuit_skips"])
            self.assertEqual(1, row["new_downloads"])

    def test_policy_rejects_impossible_measurements(self):
        bad = {
            "defaults": {"max_download_attempts": 5},
            "journals": {
                "Bad Journal": {
                    "measured_attempts": 1,
                    "measured_successes": 2
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot exceed"):
                load_journal_policies(path)

    def test_default_runner_contains_no_personal_email(self):
        runner = (ROOT / "crawl" / "crawl_papers.py").read_text(encoding="utf-8")
        all_runners = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "crawl").glob("*.py")
        )
        self.assertNotIn("@qq.com", all_runners)
        self.assertIn("journals_v6_4.json", runner)
        self.assertIn("journal_policy_v6_4.json", runner)
        self.assertIn('"--continuous"', runner)
        self.assertIn('"chemistry_dataset" / "papers"', runner)


if __name__ == "__main__":
    unittest.main()

