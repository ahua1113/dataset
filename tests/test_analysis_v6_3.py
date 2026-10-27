"""论文目录合并与量化统计测试。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from chemistry_collector.analysis import (
    classify_missing_disciplines,
    consolidate_entries,
    deduplicate_entries,
    discover_paper_roots,
    load_paper_entries,
    write_quantitative_analysis,
)
from chemistry_collector.disciplines import load_disciplines


ROOT = Path(__file__).resolve().parents[1]
DISCIPLINES = ROOT / "config" / "disciplines_v6_3.json"


class AnalysisTests(unittest.TestCase):
    def test_multiple_paper_roots_are_deduplicated_consolidated_and_analyzed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_root = Path(tmp) / "chemistry_dataset"
            first = dataset_root / "papers_dataset"
            second = dataset_root / "v6_3_smoke"
            canonical = dataset_root / "papers"
            analysis_dir = dataset_root / "quantitative_analysis"
            first_pdf = first / "raw" / "papers" / "first.pdf"
            second_pdf = second / "raw" / "papers" / "duplicate.pdf"
            orphan_pdf = second / "raw" / "papers" / "orphan.pdf"
            for path in (first_pdf, second_pdf, orphan_pdf):
                path.parent.mkdir(parents=True, exist_ok=True)
            content = b"%PDF-1.7\n" + b"a" * 2048
            first_pdf.write_bytes(content)
            second_pdf.write_bytes(content)
            orphan_pdf.write_bytes(b"%PDF-1.7\n" + b"b" * 2048)
            digest = hashlib.sha256(content).hexdigest()

            first_record = {
                "category": "papers", "status": "downloaded",
                "title": "Nanoscale auxetic material", "journal": "Journal A",
                "journal_group": "材料化学", "doi": "10.1000/same",
                "published_at": "2025-01-02", "file": "raw\\papers\\first.pdf",
                "bytes": len(content), "sha256": digest,
            }
            second_record = {
                **first_record,
                "file": "raw/papers/duplicate.pdf",
                "license_note": "CC-BY",
            }
            missing_record = {
                "category": "papers", "status": "downloaded",
                "title": "Electrochemical battery study", "journal": "Journal B",
                "journal_group": "能源", "doi": "10.1000/missing",
                "published_at": "2024-06-01", "file": "raw/papers/missing.pdf",
                "sha256": "c" * 64,
            }
            (first / "manifest.jsonl").write_text(
                json.dumps(first_record) + "\n", encoding="utf-8"
            )
            (second / "manifest.jsonl").write_text(
                json.dumps(second_record) + "\n" + json.dumps(missing_record) + "\n",
                encoding="utf-8",
            )

            roots = discover_paper_roots(dataset_root, canonical)
            self.assertEqual({first.resolve(), second.resolve()}, set(roots))
            entries, warnings = load_paper_entries(roots)
            unique, duplicates = deduplicate_entries(entries)
            self.assertEqual(4, len(entries))
            self.assertEqual(3, len(unique))
            self.assertEqual(1, duplicates)
            self.assertTrue(any("orphan PDF" in warning for warning in warnings))

            records, result = consolidate_entries(unique, canonical)
            self.assertEqual(2, result["copied"])
            self.assertEqual(1, result["missing"])
            self.assertEqual(2, len(list((canonical / "raw" / "papers").glob("*.pdf"))))
            self.assertEqual(3, len((canonical / "manifest.jsonl").read_text(
                encoding="utf-8").splitlines()))

            classified = classify_missing_disciplines(
                records, load_disciplines(DISCIPLINES)
            )
            summary = write_quantitative_analysis(
                classified, analysis_dir, source_roots=roots,
                duplicate_records=duplicates, warnings=warnings,
            )
            self.assertEqual(3, summary["unique_papers"])
            self.assertEqual(1, summary["duplicate_records_removed"])
            self.assertEqual(2, summary["local_pdf_verified"])
            self.assertIn(
                "2025", (analysis_dir / "by_publication_year.csv").read_text(
                    encoding="utf-8-sig"
                )
            )
            self.assertIn(
                "2025-01", (analysis_dir / "by_publication_month.csv").read_text(
                    encoding="utf-8-sig"
                )
            )
            for filename in (
                "quantitative_summary.json", "by_journal.csv",
                "by_discipline.csv", "by_publication_year.csv",
                "by_discipline_origin.csv", "by_file_size_bucket.csv",
                "discipline_journal_matrix.csv", "discipline_year_matrix.csv",
                "data_quality.csv", "papers_inventory.csv", "README.md",
            ):
                self.assertTrue((analysis_dir / filename).is_file(), filename)


if __name__ == "__main__":
    unittest.main()
