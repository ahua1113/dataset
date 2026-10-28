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
DISCIPLINES = ROOT / "config" / "disciplines_v6_4.json"


class AnalysisTests(unittest.TestCase):
    def test_stronger_v64_title_evidence_repairs_stale_classification(self):
        records = classify_missing_disciplines([{
            "title": "Computational Design of Nanoscale Auxetic DNA Metastructures",
            "journal_group": "综合化学",
            "primary_discipline": "biological_medicinal",
            "primary_discipline_name": "生物与药物化学",
            "discipline_score": 3,
            "discipline_classifier": "v6.3_weighted_keywords",
        }], load_disciplines(DISCIPLINES))
        self.assertEqual("materials_nanochemistry", records[0]["primary_discipline"])
        self.assertEqual(
            "reclassified_stale_classifier",
            records[0]["analysis_discipline_origin"],
        )
        self.assertEqual(
            "biological_medicinal", records[0]["previous_primary_discipline"]
        )

    def test_multiple_paper_roots_are_deduplicated_consolidated_and_analyzed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_root = Path(tmp) / "chemistry_dataset"
            first = dataset_root / "papers_dataset"
            second = dataset_root / "legacy_smoke"
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

            disciplines = load_disciplines(DISCIPLINES)
            records, result = consolidate_entries(
                unique, canonical, disciplines=disciplines
            )
            self.assertEqual(2, result["copied"])
            self.assertEqual(1, result["missing"])
            self.assertEqual(2, len(list((canonical / "raw" / "papers").glob("*.pdf"))))
            self.assertEqual(3, len((canonical / "manifest.jsonl").read_text(
                encoding="utf-8").splitlines()))
            self.assertIn(
                canonical.resolve(),
                discover_paper_roots(dataset_root, canonical),
            )

            manifest_records = [json.loads(line) for line in (
                canonical / "manifest.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            self.assertTrue(manifest_records[0].get("primary_discipline"))
            classified = classify_missing_disciplines(records, disciplines)
            summary = write_quantitative_analysis(
                classified, analysis_dir, source_roots=roots,
                duplicate_records=duplicates, warnings=warnings,
                disciplines=load_disciplines(DISCIPLINES),
            )
            self.assertEqual(3, summary["unique_papers"])
            self.assertEqual(1, summary["duplicate_records_removed"])
            self.assertEqual(2, summary["local_pdf_verified"])
            quantitative = (analysis_dir / "quantitative_report.csv").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn("publication_year,publication_year,2025", quantitative)
            self.assertIn("publication_month,publication_month,2025-01", quantitative)
            self.assertIn("journal,journal,Journal A", quantitative)
            self.assertIn("discipline,materials_nanochemistry,材料与纳米化学", quantitative)
            for filename in (
                "quantitative_summary.json", "quantitative_report.csv",
                "papers_inventory.csv", "README.md",
            ):
                self.assertTrue((analysis_dir / filename).is_file(), filename)
            self.assertFalse((analysis_dir / "by_journal.csv").exists())


if __name__ == "__main__":
    unittest.main()
