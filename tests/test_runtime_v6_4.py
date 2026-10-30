"""V6.4 连续运行、滚动配额与单实例保护测试。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chemistry_collector.disciplines import load_disciplines
from chemistry_collector.papers import (
    _active_total_target,
    _candidate_decision,
    _collection_target_reached,
    _prepare_cycle_target,
    _rolling_disciplines,
    collect_papers_v6_4,
)
from chemistry_collector.runtime import RunLock, write_runtime_state


ROOT = Path(__file__).resolve().parents[1]
DISCIPLINES = ROOT / "config" / "disciplines_v6_4.json"


class RuntimeV64Tests(unittest.TestCase):
    def test_incremental_cycle_target_is_retained_until_reached(self):
        args = SimpleNamespace(
            max_total_papers=0,
            papers_per_cycle=100,
            cycle_total_target=0,
        )
        self.assertEqual(652, _prepare_cycle_target(args, 552))
        self.assertEqual(652, args.cycle_total_target)
        self.assertEqual(652, _prepare_cycle_target(args, 612))
        self.assertEqual(652, _active_total_target(args))

        args.cycle_total_target = 0
        self.assertEqual(752, _prepare_cycle_target(args, 652))

    def test_total_target_counts_unclassified_and_bypasses_discipline_quota(self):
        disciplines = load_disciplines(DISCIPLINES)
        args = SimpleNamespace(
            max_total_papers=2,
            min_journals_per_discipline=3,
            max_journal_share=0.40,
            max_review_share=0.30,
        )
        records = [
            {"doi": "10.9999/classified", "primary_discipline": disciplines[0].id},
            {"doi": "10.9999/unclassified", "primary_discipline": "unclassified"},
        ]
        snapshot = {"counts": {}, "journals": {}, "reviews": {}}
        self.assertTrue(
            _collection_target_reached(records, snapshot, disciplines, args)
        )
        eligible, reason = _candidate_decision(
            {"primary_discipline": "unclassified"},
            snapshot,
            {discipline.id: discipline for discipline in disciplines},
            args,
        )
        self.assertTrue(eligible)
        self.assertEqual("total_target_eligible", reason)

    def test_total_target_stops_exactly_and_downloads_unclassified_papers(self):
        def candidates(_client, journal, *_args, **_kwargs):
            for index in range(3):
                yield {
                    "category": "papers",
                    "resource_kind": "research_article",
                    "title": f"General chemistry paper {index}",
                    "journal": journal.name,
                    "journal_issn": journal.issn,
                    "publisher": journal.publisher,
                    "doi": f"10.9999/total-{index}",
                    "openalex_id": f"W{index}",
                    "primary_discipline": "unclassified",
                    "primary_discipline_name": "未分类",
                    "secondary_disciplines": [],
                    "is_review": False,
                    "pdf_locations": [{
                        "url": f"https://repository.example/paper-{index}.pdf",
                        "version": "publishedVersion",
                        "oa_host": "repository.example",
                        "landing_page": "https://repository.example/article",
                        "license": "CC-BY",
                        "license_status": "verified_open_license",
                        "source_type": "institutional_repository",
                        "metadata_provider": "openalex",
                        "fallback_rank": 1,
                    }],
                }

        def fake_download(_client, item, root, **_kwargs):
            index = item["doi"].rsplit("-", 1)[1]
            relative = Path("raw") / "papers" / f"paper-{index}.pdf"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = b"%PDF-1.7\n" + index.encode() * 1024
            path.write_bytes(content)
            return "downloaded", {
                **{key: value for key, value in item.items()
                   if key != "pdf_locations"},
                "file": str(relative),
                "bytes": len(content),
                "sha256": index.zfill(64),
                "status": "downloaded",
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journals_path = root / "journals.json"
            journals_path.write_text(json.dumps([{
                "name": "Total Target Journal",
                "issn": "1234-567X",
                "publisher": "Example",
                "website": "https://publisher.example/journal",
                "group": "综合化学",
                "origin": "test",
                "publisher_hosts": ["publisher.example"],
                "status": "candidate",
            }]), encoding="utf-8")
            disciplines_path = root / "disciplines.json"
            disciplines_path.write_text(json.dumps([{
                "id": "organic_synthesis",
                "name": "有机与合成化学",
                "target": 20,
                "keywords": ["organic synthesis"],
            }]), encoding="utf-8")
            args = SimpleNamespace(
                journals_file=journals_path,
                journal_stage="all",
                journal_policy_file=None,
                include_low_yield_journals=False,
                disciplines_file=disciplines_path,
                continuous=False,
                max_total_papers=2,
                min_journals_per_discipline=1,
                max_journal_share=1.0,
                max_review_share=1.0,
                dry_run=False,
                download=True,
                per_page=10,
                max_pages_per_journal=1,
                max_attempts=10,
                allow_preprint_fallback=False,
                use_unpaywall=False,
                max_pdf_mirrors=1,
                max_file_mb=5,
                min_free_gb=0,
                report_every=10,
                analysis_dir=root / "analysis",
            )
            with patch(
                "chemistry_collector.papers.journal_article_iterator",
                side_effect=candidates,
            ), patch(
                "chemistry_collector.papers.download_file",
                side_effect=fake_download,
            ):
                report = collect_papers_v6_4(object(), args, root)

            manifest_rows = [
                json.loads(line)
                for line in (root / "manifest.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(2, len(manifest_rows))
            self.assertEqual(
                {"unclassified"},
                {row["primary_discipline"] for row in manifest_rows},
            )
            self.assertEqual(2, report["total_papers"])
            self.assertTrue(report["total_target_reached"])

    def test_rolling_target_fills_minimum_then_advances_by_twenty(self):
        disciplines = load_disciplines(DISCIPLINES)
        records = []
        for discipline in disciplines:
            for index in range(20):
                records.append({
                    "doi": f"10.9999/{discipline.id}-{index}",
                    "primary_discipline": discipline.id,
                    "journal": f"Journal {index % 3}",
                })
        rolling, target = _rolling_disciplines(records, disciplines, 20)
        self.assertEqual(40, target)
        self.assertEqual({40}, {discipline.target for discipline in rolling})

        records.pop()
        rolling, target = _rolling_disciplines(records, disciplines, 20)
        self.assertEqual(20, target)
        self.assertEqual({20}, {discipline.target for discipline in rolling})

    def test_run_lock_rejects_second_writer_and_state_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "collector.lock"
            state_path = root / "runtime_state.json"
            with RunLock(lock_path):
                self.assertTrue(lock_path.is_file())
                with self.assertRaisesRegex(RuntimeError, "采集器已在运行"):
                    RunLock(lock_path).acquire()
                write_runtime_state(state_path, status="running", cycle=3)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual("V6.4", state["version"])
                self.assertEqual(3, state["cycle"])
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
