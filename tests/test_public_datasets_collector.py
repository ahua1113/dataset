from __future__ import annotations

import argparse
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from public_datasets import pubchem, qm9
from public_datasets.cli import dry_run_plan, parse_args


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
    def __init__(self, fail_sdf: bool = False) -> None:
        self.fail_sdf = fail_sdf

    def get(self, url: str, **_kwargs):
        if url.endswith("/JSON"):
            cid_text = url.split("/cid/", 1)[1].split("/property/", 1)[0]
            rows = [
                {"CID": int(cid), "MolecularFormula": "H2O"}
                for cid in cid_text.split(",")
                if int(cid) != 2
            ]
            return FakeResponse({"PropertyTable": {"Properties": rows}})
        if "/record/SDF" in url:
            if self.fail_sdf:
                raise RuntimeError("simulated SDF failure")
            return FakeResponse(content=b"fake sdf\n$$$$\n")
        raise AssertionError(url)


class CliTests(unittest.TestCase):
    def test_dry_run_keeps_output_separate(self) -> None:
        args = parse_args(
            ["--source", "all", "--pubchem-start-cid", "5", "--pubchem-count", "10"]
        )
        plan = dry_run_plan(args)
        self.assertTrue(plan["independent_from_paper_collector"])
        self.assertEqual(plan["pubchem"]["end_cid"], 14)
        self.assertEqual(str(args.out), "chemistry_dataset/public_datasets")


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
            resumed = pubchem.collect(self.args, FakeClient(), root)
            self.assertEqual(resumed["collected_in_run"], 0)
            self.assertEqual(
                len((root / "pubchem" / "records.jsonl").read_text().splitlines()),
                2,
            )

    def test_sdf_failure_does_not_commit_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertLogs(level="ERROR"):
                summary = pubchem.collect(self.args, FakeClient(fail_sdf=True), root)
            self.assertEqual(summary["records_in_range"], 0)
            self.assertFalse((root / "pubchem" / "records.jsonl").exists())


class Qm9Tests(unittest.TestCase):
    def test_extract_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "qm9" / "raw"
            raw.mkdir(parents=True)
            archive = raw / "dsgdb9nsd.xyz.tar.bz2"
            payload = b"2\ncomment\nH 0 0 0\nH 0 0 1\n"
            with tarfile.open(archive, "w:bz2") as bundle:
                info = tarfile.TarInfo("dsgdb9nsd.xyz/dsgdb9nsd_000001.xyz")
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))

            args = argparse.Namespace(
                qm9_url="https://example.invalid/qm9",
                qm9_force_download=False,
                qm9_extract=True,
                min_free_gb=0,
            )
            first = qm9.collect(args, FakeClient(), root)
            second = qm9.collect(args, FakeClient(), root)
            self.assertEqual(first["extracted_xyz_count"], 1)
            self.assertEqual(second["extracted_now"], 0)
            self.assertEqual(second["skipped_existing"], 1)


if __name__ == "__main__":
    unittest.main()
