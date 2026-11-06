from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from public_datasets import pubchem
from public_datasets.cli import dry_run_plan, main, parse_args
from public_datasets.common import HttpRequestError


class FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload=None, content: bytes = b"") -> None:
        self.payload = payload
        self.content = content

    def json(self):
        return self.payload

    def close(self) -> None:
        return None


class FakeClient:
    def __init__(self, fail_sdf_for: int | None = None) -> None:
        self.fail_sdf_for = fail_sdf_for
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        if url.endswith("/JSON"):
            cid_text = url.split("/cid/", 1)[1].split("/property/", 1)[0]
            rows = [
                {"CID": int(cid), "MolecularFormula": "H2O"}
                for cid in cid_text.split(",")
                if int(cid) != 2
            ]
            return FakeResponse({"PropertyTable": {"Properties": rows}})
        if "/record/SDF" in url:
            cid_text = url.split("/cid/", 1)[1].split("/record/", 1)[0]
            cids = [int(cid) for cid in cid_text.split(",")]
            if self.fail_sdf_for in cids:
                raise RuntimeError("simulated SDF failure")
            content = "".join(
                f"{cid}\n> <PUBCHEM_COMPOUND_CID>\n{cid}\n\n$$$$\n" for cid in cids
            )
            return FakeResponse(content=content.encode("ascii"))
        raise AssertionError(url)

    def close(self) -> None:
        return None


class CliTests(unittest.TestCase):
    def test_dry_run_keeps_output_separate(self) -> None:
        args = parse_args(
            ["--source", "all", "--pubchem-start-cid", "5", "--pubchem-count", "10"]
        )
        plan = dry_run_plan(args)
        self.assertTrue(plan["independent_from_paper_collector"])
        self.assertEqual(plan["pubchem"]["end_cid"], 14)
        self.assertEqual(args.out, Path("chemistry_dataset") / "public_datasets")

    def test_partial_pubchem_failure_reaches_top_level_state_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("public_datasets.cli.HttpClient", return_value=FakeClient(fail_sdf_for=3)),
                patch("public_datasets.cli.configure_logging"),
                patch("public_datasets.cli.install_signal_handlers"),
                self.assertLogs(level="ERROR"),
            ):
                exit_code = main([
                    "--source", "pubchem", "--pubchem-count", "3",
                    "--pubchem-batch-size", "1", "--min-free-gb", "0", "--out", temporary,
                ])
            self.assertEqual(exit_code, 1)
            state = json.loads((Path(temporary) / "runtime_state.json").read_text())
            self.assertEqual(state["status"], "completed_with_errors")
            self.assertEqual(state["results"]["pubchem"]["failed_batches"], 1)
            self.assertEqual(state["results"]["pubchem"]["records_in_range"], 1)


class PubChemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(
            pubchem_start_cid=1,
            pubchem_count=3,
            pubchem_batch_size=3,
            pubchem_sdf=True,
            pubchem_record_type="2d",
            min_free_gb=0,
        )

    def test_collect_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = pubchem.collect(self.args, FakeClient(), root)
            self.assertEqual(summary["records_in_range"], 2)
            self.assertEqual(summary["missing_in_run"], 1)
            manifest_path = root / "pubchem" / "batch_manifest.jsonl"
            legacy_batch = json.loads(manifest_path.read_text())
            del legacy_batch["sdf"]["cids"]
            manifest_path.write_text(json.dumps(legacy_batch) + "\n")
            client = FakeClient()
            resumed = pubchem.collect(self.args, client, root)
            self.assertEqual(resumed["collected_in_run"], 0)
            self.assertEqual(resumed["sdf_records_in_range"], 2)
            self.assertEqual(client.urls, [])
            self.assertEqual(
                len((root / "pubchem" / "records.jsonl").read_text().splitlines()),
                2,
            )

    def test_bad_request_and_legacy_missing_cid_remain_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            errors_path = root / "pubchem" / "errors.jsonl"
            errors_path.parent.mkdir()
            errors_path.write_text('{"cid": 1, "error": "CID not found"}\n', encoding="utf-8")
            self.args.pubchem_count = 1
            self.args.pubchem_sdf = False
            client = FakeClient()
            error = HttpRequestError(400, "bad request", pubchem.property_url([1]))
            with patch.object(client, "get", side_effect=error), self.assertLogs(level="ERROR"):
                failed = pubchem.collect(self.args, client, root)
            self.assertEqual(failed["failed_batches"], 1)
            self.assertEqual(failed["missing_in_run"], 0)

            retry = FakeClient()
            resumed = pubchem.collect(self.args, retry, root)
            self.assertEqual(retry.urls, [pubchem.property_url([1])])
            self.assertEqual(resumed["records_in_range"], 1)

    def test_resumes_after_partial_utf8_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records_path = root / "pubchem" / "records.jsonl"
            records_path.parent.mkdir()
            records_path.write_bytes(b'{"CID":1}\n{"CID":3,"note":"\xe4')
            self.args.pubchem_sdf = False
            with self.assertLogs(level="WARNING"):
                summary = pubchem.collect(self.args, FakeClient(), root)
            self.assertEqual(summary["failed_batches"], 0)
            self.assertEqual(summary["collected_in_run"], 1)
            self.assertEqual(summary["records_in_range"], 2)
            last_row = json.loads(records_path.read_bytes().splitlines()[-1])
            self.assertEqual(last_row["CID"], 3)

    def test_backfills_2d_and_3d_without_fetching_or_duplicating_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.args.pubchem_sdf = False
            pubchem.collect(self.args, FakeClient(), root)
            records_path = root / "pubchem" / "records.jsonl"
            saved_records = records_path.read_bytes()

            self.args.pubchem_sdf = True
            for record_type in ("2d", "3d"):
                with self.subTest(record_type=record_type):
                    self.args.pubchem_record_type = record_type
                    client = FakeClient()
                    summary = pubchem.collect(self.args, client, root)
                    self.assertEqual(summary["collected_in_run"], 0)
                    self.assertEqual(summary["sdf_collected_in_run"], 2)
                    self.assertEqual(len(client.urls), 1)
                    self.assertTrue(client.urls[0].endswith(f"/record/SDF?record_type={record_type}"))
                    self.assertEqual(records_path.read_bytes(), saved_records)

                    client = FakeClient()
                    repeated = pubchem.collect(self.args, client, root)
                    self.assertEqual(repeated["sdf_records_in_range"], 2)
                    self.assertEqual(repeated["sdf_collected_in_run"], 0)
                    self.assertEqual(client.urls, [])

    def test_sdf_failure_does_not_commit_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertLogs(level="ERROR"):
                summary = pubchem.collect(self.args, FakeClient(fail_sdf_for=3), root)
            self.assertEqual(summary["records_in_range"], 0)
            self.assertFalse((root / "pubchem" / "records.jsonl").exists())

    def test_refetches_sdf_with_a_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pubchem.collect(self.args, FakeClient(), root)
            old_sdf = next((root / "pubchem" / "sdf").glob("*.gz"))
            old_sdf.write_bytes(b"corrupt")

            client = FakeClient()
            with self.assertLogs(level="WARNING"):
                summary = pubchem.collect(self.args, client, root)
            self.assertEqual(summary["sdf_collected_in_run"], 2)
            self.assertEqual(summary["collected_in_run"], 0)
            self.assertEqual(len(client.urls), 1)
            self.assertEqual(old_sdf.read_bytes(), b"corrupt")

            client = FakeClient()
            with self.assertLogs(level="WARNING"):
                repeated = pubchem.collect(self.args, client, root)
            self.assertEqual(repeated["sdf_records_in_range"], 2)
            self.assertEqual(client.urls, [])

    def test_incomplete_sdf_response_counts_as_a_failed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one_record = FakeClient().get(f"{pubchem.PUG_BASE}/compound/cid/1/record/SDF").content
            with (
                patch("public_datasets.pubchem.fetch_sdf", return_value=one_record),
                self.assertLogs(level="ERROR"),
            ):
                summary = pubchem.collect(self.args, FakeClient(), root)
            self.assertEqual(summary["failed_batches"], 1)
            self.assertEqual(summary["sdf_records_in_range"], 0)
            self.assertFalse((root / "pubchem" / "batch_manifest.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
