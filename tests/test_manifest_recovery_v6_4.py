"""V6.4 孤立 PDF 元数据恢复测试。"""
import unittest

from tools.recover_manifest_metadata import recover_records


class ManifestRecoveryTests(unittest.TestCase):
    def test_only_exact_sha256_match_recovers_article_metadata(self):
        current = [{
            "category": "papers", "status": "downloaded", "title": "",
            "file": "raw/papers/papers_abc.pdf", "bytes": 123,
            "sha256": "a" * 64, "metadata_status": "orphan_pdf",
        }]
        legacy = [("old.jsonl", {
            "category": "papers", "status": "downloaded",
            "title": "Recovered chemistry paper", "journal": "Journal A",
            "doi": "10.1000/recovered", "sha256": "a" * 64,
            "file": "old/path.pdf", "bytes": 999,
        })]
        records, stats = recover_records(current, legacy)
        self.assertEqual(1, stats["recovered"])
        self.assertEqual("Recovered chemistry paper", records[0]["title"])
        self.assertEqual("raw/papers/papers_abc.pdf", records[0]["file"])
        self.assertEqual(123, records[0]["bytes"])
        self.assertEqual("sha256", records[0]["metadata_recovery_key"])

    def test_unmatched_sha_remains_orphan_without_fabricated_metadata(self):
        current = [{"title": "", "sha256": "b" * 64}]
        legacy = [("old.jsonl", {
            "title": "Different file", "journal": "Journal B",
            "doi": "10.1000/different", "sha256": "c" * 64,
        })]
        records, stats = recover_records(current, legacy)
        self.assertEqual(0, stats["recovered"])
        self.assertEqual(1, stats["unmatched"])
        self.assertEqual("", records[0]["title"])


if __name__ == "__main__":
    unittest.main()
