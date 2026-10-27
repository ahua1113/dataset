"""V6.3 本地文件校验、去重与白名单边界测试。"""
from pathlib import Path
import tempfile
import unittest

from chemistry_collector.models import load_journals
from chemistry_collector.storage import (
    download_file,
    hash_file,
    identity_keys,
    load_existing_records,
)
from chemistry_collector.utils import append_jsonl


ROOT = Path(__file__).resolve().parents[1]
JOURNALS = ROOT / "config" / "journals_v6_3.json"


class FakeResponse:
    def __init__(self, url: str, data: bytes):
        self.url = url
        self.data = data
        self.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(data)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def iter_content(self, chunk_size=262144):
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset:offset + chunk_size]


class FakeDownloader:
    def __init__(self, pdf: bytes):
        self.pdf = pdf

    def get(self, url: str, *, stream=False):
        del stream
        return FakeResponse(url, self.pdf)


class StorageTests(unittest.TestCase):
    def test_candidate_urls_are_part_of_predownload_identity(self):
        item = {
            "doi": "10.9999/article",
            "title": "Example paper",
            "pdf_locations": [{"url": "https://example.edu/a.pdf"}],
        }
        self.assertIn("url:https://example.edu/a.pdf", identity_keys(item))

    def test_sha256_duplicate_is_not_saved_twice(self):
        pdf = b"%PDF-1.7\n" + b"a" * 2048
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeDownloader(pdf)
            item = {"category": "papers", "title": "Chem paper"}
            status, record = download_file(
                client, item, root, url="https://example.edu/a.pdf",
                ext=".pdf", max_file_mb=5, seen_sha=set(),
            )
            self.assertEqual("downloaded", status)
            self.assertEqual(
                record["sha256"], hash_file(root / record["file"])
            )
            duplicate, second = download_file(
                client, item, root, url="https://other.edu/b.pdf",
                ext=".pdf", max_file_mb=5, seen_sha={record["sha256"]},
            )
            self.assertEqual("sha_duplicate", duplicate)
            self.assertIsNone(second)
            self.assertEqual(1, len(list((root / "raw" / "papers").glob("*.pdf"))))

    def test_nonwhitelist_manifest_record_is_rejected(self):
        allowed = {journal.name for journal in load_journals(JOURNALS)}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "raw" / "papers" / "old.pdf"
            path.parent.mkdir(parents=True)
            content = b"%PDF-1.7" + b"x" * 2048
            path.write_bytes(content)
            append_jsonl(root / "manifest.jsonl", {
                "category": "papers",
                "status": "downloaded",
                "journal": "Some Other Journal",
                "file": "raw/papers/old.pdf",
                "bytes": len(content),
                "sha256": hash_file(path),
            })
            with self.assertRaisesRegex(RuntimeError, "non-whitelist journal"):
                load_existing_records(
                    root, category="papers", permitted_journals=allowed
                )


if __name__ == "__main__":
    unittest.main()
