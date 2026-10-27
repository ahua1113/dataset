"""文件与清单管理：多层去重、实际文件校验、原子保存和审计记录。"""
import csv
import hashlib
import json
from pathlib import Path
import re

from .client import Client, SourceError
from .constants import COLLECTOR_VERSION, MIME
from .utils import is_https, normalize_doi, normalized_id, normalized_title, now_utc

# 同时构造 DOI、论文标识、标题及候选链接键，用于下载前去重。
def identity_keys(item: dict) -> set[str]:
    keys = set()
    doi = normalize_doi(str(item.get("doi") or ""))
    if doi:
        keys.add("doi:" + doi)
    openalex = normalized_id(str(item.get("openalex_id") or ""))
    if openalex:
        keys.add("openalex:" + openalex)
    title = normalized_title(str(item.get("title") or ""))
    if title:
        keys.add("title:" + title)
    url = str(item.get("download_url") or "").strip().lower()
    if url:
        keys.add("url:" + url)
    for location in item.get("pdf_locations") or []:
        candidate_url = str(location.get("url") or "").strip().lower()
        if candidate_url:
            keys.add("url:" + candidate_url)
    return keys


# 只恢复清单中仍有对应本地文件的有效记录，避免虚增统计。
def load_existing_records(root: Path, *, category: str,
                          permitted_journals: set[str] | None = None) -> list[dict]:
    manifest = root / "manifest.jsonl"
    if not manifest.is_file():
        return []
    records = []
    for line_num, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(f"Bad manifest JSON on line {line_num}: {exc}") from exc
        if row.get("category") != category or row.get("status") != "downloaded":
            continue
        if permitted_journals is not None and row.get("journal") not in permitted_journals:
            raise RuntimeError(
                f"Existing manifest contains a non-whitelist journal record: {row.get('journal')!r}. "
                "Use an empty output folder; the program will NOT erase legacy data."
            )
        rel = Path(str(row.get("file") or ""))
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(f"Unsafe manifest path on line {line_num}: {rel}")
        path = root / rel
        if not path.is_file() or path.stat().st_size != row.get("bytes"):
            print(f"[WARN] Missing/size-mismatched old file, not counted: {rel}", flush=True)
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256") or ""), re.I):
            raise RuntimeError(f"Manifest missing valid SHA-256 on line {line_num}")
        records.append(row)
    return records


def file_signature_ok(first_bytes: bytes, ext: str) -> bool:
    if ext == ".pdf":
        return first_bytes.startswith(b"%PDF-")
    if ext == ".mp4":
        return len(first_bytes) >= 8 and first_bytes[4:8] == b"ftyp"
    if ext == ".docx":
        return first_bytes.startswith(b"PK\x03\x04")
    return False


# 使用临时文件流式下载，经过内容签名和 SHA-256 检查后再保存。
def download_file(client: Client, item: dict, root: Path, *, url: str,
                  ext: str, max_file_mb: int, seen_sha: set[str]) -> tuple[str, dict | None]:
    """先流式写入临时文件，验证文件签名与 SHA-256 后再原子重命名。

    返回下载状态及成功落盘的记录；重复文件不再保存。
    """
    if not is_https(url):
        raise SourceError(f"Non-HTTPS download: {url}")
    folder = root / "raw" / item["category"]
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{item['category']}_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}{ext}"
    target = folder / name
    tmp = folder / (name + ".part")
    limit = max_file_mb * 1024 * 1024
    total = 0
    sha = hashlib.sha256()
    first = bytearray()
    try:
        with client.get(url, stream=True) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower().strip()
            if content_type in {"text/html", "application/json", "text/plain", "application/xml"}:
                raise SourceError(f"Not a PDF/media response: {content_type}")
            clen = int(response.headers.get("Content-Length", "0") or 0)
            if clen > limit:
                raise SourceError(f"File exceeds {max_file_mb} MB limit")
            with tmp.open("wb") as dest:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    if len(first) < 16:
                        first.extend(chunk[:16 - len(first)])
                    total += len(chunk)
                    if total > limit:
                        raise SourceError(f"File exceeds {max_file_mb} MB limit")
                    dest.write(chunk)
                    sha.update(chunk)
        if total < 1024 or not file_signature_ok(bytes(first), ext):
            raise SourceError(f"Downloaded content is not a valid {ext} file signature")
        digest = sha.hexdigest()
        if digest in seen_sha:
            return "sha_duplicate", None
        if target.exists():
            # 对已存在的目标文件核验摘要，防止同名覆盖不同内容。
            # 如果上次中断发生在写入清单前，则允许复用摘要一致的孤立文件。
            existing_digest = hash_file(target)
            if existing_digest != digest:
                raise SourceError(f"Output filename collision with different file: {target}")
        else:
            tmp.replace(target)
        record = {k: v for k, v in item.items() if k != "pdf_locations"}
        record.update({
            "download_url": url, "file": str(target.relative_to(root)),
            "bytes": total, "sha256": digest,
            "mime_expected": MIME[ext], "collected_at": now_utc(),
            "status": "downloaded", "collector_version": COLLECTOR_VERSION,
        })
        return "downloaded", record
    finally:
        tmp.unlink(missing_ok=True)


def hash_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def append_csv(path: Path, item: dict, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(item)


PAPER_AUDIT_COLUMNS = [
    "at", "journal", "journal_issn", "title", "doi", "openalex_id",
    "source_page", "download_url", "oa_status", "oa_version", "oa_pdf_host",
    "primary_discipline", "primary_discipline_name", "oa_pdf_source",
    "oa_metadata_provider", "license_note", "license_verification",
    "filter_status", "filter_reason",
]


# 将候选论文的筛选结果写入审计表，保留失败原因。
def paper_audit(path: Path, item: dict, status: str, reason: str = "") -> None:
    append_csv(path, {"at": now_utc(), **item, "filter_status": status,
                      "filter_reason": reason}, PAPER_AUDIT_COLUMNS)

