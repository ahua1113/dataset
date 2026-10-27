import json
from pathlib import Path
import tempfile
import unittest

import collector
from chemistry_collector.client import Client, SourceError
from chemistry_collector.policy import (
    apply_journal_policies,
    load_journal_policies,
    policy_for,
)


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "config" / "journals_v6_3.json"
POLICY = ROOT / "config" / "journal_policy_v6_3.json"


class CollectorV63StructureAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.journals = collector.load_journals(JOURNALS)
        self.policies = load_journal_policies(POLICY)

    def test_v63_keeps_42_traceable_journals_but_skips_three_low_yield(self):
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
        with self.assertRaisesRegex(SourceError, "circuit open"):
            client.get("https://blocked.example/paper.pdf", stream=True)

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
        self.assertIn("journals_v6_3.json", runner)
        self.assertIn("journal_policy_v6_3.json", runner)


if __name__ == "__main__":
    unittest.main()

