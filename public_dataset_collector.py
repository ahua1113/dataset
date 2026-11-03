#!/usr/bin/env python3
"""独立采集 QM9 与 PubChem 公开化学数据。

该入口不导入、修改或复用 ``collector.py`` / ``chemistry_collector``，默认写入
``chemistry_dataset/public_datasets``，因此可以和论文采集器同时运行。

常用命令：

    python public_dataset_collector.py --source qm9
    python public_dataset_collector.py --source pubchem --pubchem-count 1000
    python public_dataset_collector.py --source all --pubchem-count 1000

QM9 下载 Figshare 原始 XYZ 压缩包并可断点续传、校验、解压；PubChem 按连续
CID 区间采集属性 JSONL，并可同时保存按批次压缩的二维 SDF。两个数据源各自
维护状态与 manifest，重复执行会跳过已经完成的记录。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sys
import tarfile
import time
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # 允许在尚未安装依赖时查看 --help / --dry-run
    requests = None  # type: ignore[assignment]


VERSION = "1.0.0"
QM9_ARCHIVE_URL = "https://figshare.com/ndownloader/files/3195389"
PUBCHEM_PUG_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_PROPERTIES = (
    "MolecularFormula",
    "MolecularWeight",
    "CanonicalSMILES",
    "IsomericSMILES",
    "InChI",
    "InChIKey",
    "XLogP",
    "TPSA",
    "Complexity",
    "HBondDonorCount",
    "HBondAcceptorCount",
    "RotatableBondCount",
    "HeavyAtomCount",
    "Charge",
)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
STOP_REQUESTED = False


class CollectorError(RuntimeError):
    """可预期的采集错误。"""


class CollectorLockError(CollectorError):
    """另一个公开数据集采集进程仍在运行。"""


class HttpRequestError(CollectorError):
    def __init__(self, status_code: int, message: str, url: str) -> None:
        super().__init__(f"HTTP {status_code}: {message} ({url})")
        self.status_code = status_code
        self.url = url


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json_dumps(row) + "\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        logging.warning("状态文件无法读取，将重新生成：%s", path)
        return default


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def free_disk_gib(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / (1024**3)


def require_free_disk(path: Path, minimum_gib: float) -> None:
    available = free_disk_gib(path)
    if available < minimum_gib:
        raise CollectorError(
            f"磁盘可用空间仅 {available:.2f} GiB，低于阈值 {minimum_gib:.2f} GiB"
        )


def install_signal_handlers() -> None:
    def request_stop(signum: int, _frame: Any) -> None:
        global STOP_REQUESTED
        STOP_REQUESTED = True
        logging.warning("收到信号 %s，将在当前安全边界停止", signum)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        state = load_json(path, {})
        previous_pid = int(state.get("pid", 0) or 0)
        if pid_is_running(previous_pid):
            raise CollectorLockError(
                f"公开数据集采集器已在运行（PID {previous_pid}）。锁文件：{path}"
            )
        logging.warning("移除失效锁文件：%s", path)
        path.unlink(missing_ok=True)

    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        payload = json_dumps({"pid": os.getpid(), "created_at": utc_now()}) + "\n"
        os.write(descriptor, payload.encode("utf-8"))
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)


class HttpClient:
    def __init__(
        self,
        *,
        contact_email: str,
        timeout_seconds: float,
        max_retries: int,
        request_interval_seconds: float,
    ) -> None:
        if requests is None:
            raise CollectorError(
                "缺少 requests 依赖，请先运行：python -m pip install -r requirements.txt"
            )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.request_interval_seconds = request_interval_seconds
        self.last_request_at = 0.0
        self.session = requests.Session()
        contact = contact_email.strip() or "not-provided"
        self.session.headers.update(
            {
                "User-Agent": (
                    f"chemistry-public-dataset-collector/{VERSION} "
                    f"(contact: {contact})"
                ),
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def close(self) -> None:
        self.session.close()

    def _pace(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        acceptable_status: set[int] | None = None,
    ) -> requests.Response:
        acceptable = acceptable_status or {200}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if STOP_REQUESTED:
                raise CollectorError("采集任务已请求停止")
            self._pace()
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    stream=stream,
                    timeout=(15.0, self.timeout_seconds),
                )
                self.last_request_at = time.monotonic()
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(60.0, 2.0**attempt)
                logging.warning("网络请求失败，%.1f 秒后重试：%s", delay, exc)
                time.sleep(delay)
                continue

            if response.status_code in acceptable:
                throttle = response.headers.get("X-Throttling-Control", "")
                if "Red" in throttle:
                    time.sleep(5.0)
                elif "Yellow" in throttle:
                    time.sleep(1.0)
                return response

            message = response.text[:400].replace("\n", " ")
            if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                response.close()
                raise HttpRequestError(response.status_code, message, url)

            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(60.0, 2.0**attempt)
            except ValueError:
                delay = min(60.0, 2.0**attempt)
            response.close()
            logging.warning("HTTP %s，%.1f 秒后重试", response.status_code, delay)
            time.sleep(delay)

        raise CollectorError(f"网络请求最终失败：{url}；{last_error}")


def download_with_resume(
    client: HttpClient,
    url: str,
    destination: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    existing = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else None
    response = client.get(url, headers=headers, stream=True, acceptable_status={200, 206})

    append = existing > 0 and response.status_code == 206
    if existing and not append:
        logging.warning("下载端不接受断点续传，将从头下载：%s", destination.name)
        existing = 0
    mode = "ab" if append else "wb"
    expected = response.headers.get("Content-Length")
    expected_bytes = int(expected) + existing if expected and expected.isdigit() else None
    downloaded = existing
    last_report = time.monotonic()

    with partial.open(mode) as handle:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if STOP_REQUESTED:
                response.close()
                raise CollectorError("采集任务已请求停止；QM9 断点文件已保留")
            if not chunk:
                continue
            handle.write(chunk)
            downloaded += len(chunk)
            if time.monotonic() - last_report >= 10:
                if expected_bytes:
                    logging.info(
                        "QM9 下载进度：%.1f%%（%.1f MiB）",
                        downloaded * 100 / expected_bytes,
                        downloaded / (1024**2),
                    )
                else:
                    logging.info("QM9 已下载 %.1f MiB", downloaded / (1024**2))
                last_report = time.monotonic()
        handle.flush()
        os.fsync(handle.fileno())
    response.close()

    if expected_bytes is not None and downloaded != expected_bytes:
        raise CollectorError(
            f"QM9 下载大小不完整：期望 {expected_bytes} 字节，实际 {downloaded} 字节"
        )
    os.replace(partial, destination)


def validate_tar_member(member: tarfile.TarInfo, destination: Path) -> None:
    if member.issym() or member.islnk():
        raise CollectorError(f"QM9 压缩包包含不允许的链接：{member.name}")
    root = destination.resolve()
    target = (destination / member.name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CollectorError(f"QM9 压缩包包含越界路径：{member.name}") from exc


def extract_qm9_archive(archive: Path, destination: Path) -> tuple[int, int]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted_now = 0
    skipped_existing = 0
    with tarfile.open(archive, mode="r:bz2") as bundle:
        for index, member in enumerate(bundle, start=1):
            if STOP_REQUESTED:
                raise CollectorError("采集任务已请求停止；已解压文件将保留")
            validate_tar_member(member, destination)
            target = destination / member.name
            if member.isfile() and target.exists() and target.stat().st_size == member.size:
                skipped_existing += 1
                continue
            bundle.extract(member, path=destination)
            if member.isfile():
                extracted_now += 1
            if index % 5000 == 0:
                logging.info(
                    "QM9 解压进度：已处理 %s 个条目，本次新增 %s 个文件",
                    index,
                    extracted_now,
                )
    return extracted_now, skipped_existing


def collect_qm9(args: argparse.Namespace, client: HttpClient, root: Path) -> dict[str, Any]:
    qm9_dir = root / "qm9"
    raw_dir = qm9_dir / "raw"
    extract_dir = qm9_dir / "extracted"
    archive = raw_dir / "dsgdb9nsd.xyz.tar.bz2"
    state_path = qm9_dir / "state.json"
    manifest_path = qm9_dir / "manifest.json"
    state = {
        "collector_version": VERSION,
        "dataset": "QM9",
        "status": "running",
        "pid": os.getpid(),
        "source_url": args.qm9_url,
        "archive_path": str(archive.relative_to(root)),
        "updated_at": utc_now(),
    }
    atomic_write_json(state_path, state)
    require_free_disk(root, args.min_free_gb)

    if args.qm9_force_download:
        archive.unlink(missing_ok=True)
        archive.with_suffix(archive.suffix + ".part").unlink(missing_ok=True)

    if not archive.exists():
        logging.info("开始下载 QM9 原始压缩包：%s", args.qm9_url)
        download_with_resume(client, args.qm9_url, archive)
    else:
        logging.info("QM9 原始压缩包已存在，跳过下载：%s", archive)

    try:
        with tarfile.open(archive, mode="r:bz2") as bundle:
            first_member = next(iter(bundle), None)
            if first_member is None:
                raise CollectorError("QM9 压缩包为空")
    except (tarfile.TarError, OSError) as exc:
        raise CollectorError(f"QM9 压缩包校验失败：{exc}") from exc

    archive_sha256 = sha256_file(archive)
    extracted_now = 0
    skipped_existing = 0
    if args.qm9_extract:
        require_free_disk(root, args.min_free_gb)
        logging.info("开始解压 QM9；重复执行会跳过已完成文件")
        extracted_now, skipped_existing = extract_qm9_archive(archive, extract_dir)

    xyz_count = (
        sum(1 for item in extract_dir.rglob("*.xyz") if item.is_file())
        if extract_dir.exists()
        else 0
    )
    manifest = {
        "collector_version": VERSION,
        "dataset": "QM9",
        "source": "Figshare collection 978904",
        "source_url": args.qm9_url,
        "archive": str(archive.relative_to(root)),
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": archive_sha256,
        "extract_enabled": args.qm9_extract,
        "extracted_xyz_count": xyz_count,
        "extracted_now": extracted_now,
        "skipped_existing": skipped_existing,
        "completed_at": utc_now(),
    }
    atomic_write_json(manifest_path, manifest)
    state.update(
        {
            "status": "completed",
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": archive_sha256,
            "extracted_xyz_count": xyz_count,
            "updated_at": utc_now(),
        }
    )
    atomic_write_json(state_path, state)
    logging.info(
        "QM9 完成：archive=%.1f MiB，XYZ=%s",
        archive.stat().st_size / (1024**2),
        xyz_count,
    )
    return manifest


def chunks(values: Iterable[int], size: int) -> Iterator[list[int]]:
    batch: list[int] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def pubchem_property_url(cids: Sequence[int]) -> str:
    cid_text = quote(",".join(str(cid) for cid in cids), safe=",")
    properties = ",".join(PUBCHEM_PROPERTIES)
    return f"{PUBCHEM_PUG_BASE}/compound/cid/{cid_text}/property/{properties}/JSON"


def fetch_pubchem_properties(
    client: HttpClient,
    cids: Sequence[int],
) -> tuple[list[dict[str, Any]], list[int]]:
    if not cids:
        return [], []
    url = pubchem_property_url(cids)
    try:
        response = client.get(url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectorError(f"PubChem 返回了无效 JSON：{url}") from exc
        finally:
            response.close()
        records = payload.get("PropertyTable", {}).get("Properties", [])
        if not isinstance(records, list):
            raise CollectorError("PubChem 属性响应缺少 PropertyTable.Properties")
        found = {int(record["CID"]) for record in records if "CID" in record}
        missing = [cid for cid in cids if cid not in found]
        return records, missing
    except HttpRequestError as exc:
        if exc.status_code not in {400, 404}:
            raise
        if len(cids) == 1:
            return [], [int(cids[0])]
        middle = len(cids) // 2
        left_records, left_missing = fetch_pubchem_properties(client, cids[:middle])
        right_records, right_missing = fetch_pubchem_properties(client, cids[middle:])
        return left_records + right_records, left_missing + right_missing


def fetch_pubchem_sdf(client: HttpClient, cids: Sequence[int], record_type: str) -> bytes:
    cid_text = quote(",".join(str(cid) for cid in cids), safe=",")
    url = (
        f"{PUBCHEM_PUG_BASE}/compound/cid/{cid_text}/record/SDF"
        f"?record_type={record_type}"
    )
    response = client.get(url)
    try:
        return response.content
    finally:
        response.close()


def save_gzip_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as handle:
            handle.write(content)
        raw_handle.flush()
        os.fsync(raw_handle.fileno())
    os.replace(temporary, path)


def load_existing_pubchem_cids(path: Path) -> set[int]:
    result: set[int] = set()
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                result.add(int(record["CID"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                logging.warning("忽略 records.jsonl 第 %s 行的无效记录", line_number)
    return result


def load_known_missing_pubchem_cids(path: Path) -> set[int]:
    result: set[int] = set()
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("error") == "CID not found" and "cid" in record:
                    result.add(int(record["cid"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return result


def collect_pubchem(
    args: argparse.Namespace,
    client: HttpClient,
    root: Path,
) -> dict[str, Any]:
    pubchem_dir = root / "pubchem"
    records_path = pubchem_dir / "records.jsonl"
    errors_path = pubchem_dir / "errors.jsonl"
    batch_manifest_path = pubchem_dir / "batch_manifest.jsonl"
    state_path = pubchem_dir / "state.json"
    sdf_dir = pubchem_dir / "sdf"
    start_cid = args.pubchem_start_cid
    end_cid = start_cid + args.pubchem_count - 1
    requested = range(start_cid, end_cid + 1)
    existing = load_existing_pubchem_cids(records_path)
    known_missing = load_known_missing_pubchem_cids(errors_path)
    pending_count = sum(
        1 for cid in requested if cid not in existing and cid not in known_missing
    )

    state = {
        "collector_version": VERSION,
        "dataset": "PubChem Compound",
        "status": "running",
        "pid": os.getpid(),
        "requested_start_cid": start_cid,
        "requested_end_cid": end_cid,
        "requested_cid_count": args.pubchem_count,
        "already_present_in_range": sum(1 for cid in requested if cid in existing),
        "known_missing_in_range": sum(1 for cid in requested if cid in known_missing),
        "pending_cid_count": pending_count,
        "download_sdf": args.pubchem_sdf,
        "record_type": args.pubchem_record_type,
        "updated_at": utc_now(),
    }
    atomic_write_json(state_path, state)
    require_free_disk(root, args.min_free_gb)

    collected = 0
    missing_total = 0
    failed_batches = 0
    pending_cids = (
        cid for cid in requested if cid not in existing and cid not in known_missing
    )
    for batch_index, batch in enumerate(
        chunks(pending_cids, args.pubchem_batch_size), start=1
    ):
        if STOP_REQUESTED:
            raise CollectorError("采集任务已请求停止")
        require_free_disk(root, args.min_free_gb)
        logging.info(
            "PubChem 批次 %s：CID %s—%s（%s个）",
            batch_index,
            batch[0],
            batch[-1],
            len(batch),
        )
        try:
            properties, missing = fetch_pubchem_properties(client, batch)
            retrieved_at = utc_now()
            normalized = []
            for record in properties:
                item = dict(record)
                item["dataset"] = "PubChem Compound"
                item["retrieved_at"] = retrieved_at
                item["source_url"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{item['CID']}"
                normalized.append(item)
            sdf_record: dict[str, Any] | None = None
            valid_cids = sorted(int(item["CID"]) for item in normalized)
            if args.pubchem_sdf and valid_cids:
                sdf_path = sdf_dir / (
                    f"cid_{batch[0]:09d}_{batch[-1]:09d}_{args.pubchem_record_type}.sdf.gz"
                )
                if not sdf_path.exists():
                    sdf_content = fetch_pubchem_sdf(
                        client, valid_cids, args.pubchem_record_type
                    )
                    save_gzip_atomic(sdf_path, sdf_content)
                sdf_record = {
                    "path": str(sdf_path.relative_to(root)),
                    "bytes": sdf_path.stat().st_size,
                    "sha256": sha256_file(sdf_path),
                    "record_type": args.pubchem_record_type,
                }

            # 属性与 SDF 作为一个批次提交：SDF 失败时不先写入 records.jsonl，
            # 这样下次执行仍会重试该批次，而不会因为 CID 已存在而永久跳过 SDF。
            append_jsonl(records_path, normalized)
            collected += len(normalized)
            missing_total += len(missing)
            if missing:
                append_jsonl(
                    errors_path,
                    (
                        {
                            "dataset": "PubChem Compound",
                            "cid": cid,
                            "error": "CID not found",
                            "recorded_at": retrieved_at,
                        }
                        for cid in missing
                    ),
                )

            batch_manifest = {
                "dataset": "PubChem Compound",
                "requested_cid_start": batch[0],
                "requested_cid_end": batch[-1],
                "requested_count": len(batch),
                "retrieved_count": len(normalized),
                "missing_cids": missing,
                "sdf": sdf_record,
                "completed_at": utc_now(),
            }
            append_jsonl(batch_manifest_path, [batch_manifest])
        except Exception as exc:
            failed_batches += 1
            append_jsonl(
                errors_path,
                [
                    {
                        "dataset": "PubChem Compound",
                        "cid_start": batch[0],
                        "cid_end": batch[-1],
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "recorded_at": utc_now(),
                    }
                ],
            )
            logging.exception("PubChem 批次失败，记录错误后继续")

        state.update(
            {
                "status": "running",
                "last_processed_cid": batch[-1],
                "collected_in_run": collected,
                "missing_in_run": missing_total,
                "failed_batches": failed_batches,
                "updated_at": utc_now(),
            }
        )
        atomic_write_json(state_path, state)

    final_existing = load_existing_pubchem_cids(records_path)
    total_in_range = sum(1 for cid in requested if cid in final_existing)
    summary = {
        "collector_version": VERSION,
        "dataset": "PubChem Compound",
        "requested_start_cid": start_cid,
        "requested_end_cid": end_cid,
        "requested_cid_count": args.pubchem_count,
        "records_in_range": total_in_range,
        "collected_in_run": collected,
        "missing_in_run": missing_total,
        "failed_batches": failed_batches,
        "download_sdf": args.pubchem_sdf,
        "record_type": args.pubchem_record_type,
        "records_path": str(records_path.relative_to(root)),
        "completed_at": utc_now(),
    }
    state.update(summary)
    state["status"] = "completed" if failed_batches == 0 else "completed_with_errors"
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    logging.info(
        "PubChem 完成：区间 CID %s—%s，已有/新增有效记录 %s/%s，缺失 %s，失败批次 %s",
        start_cid,
        end_cid,
        total_in_range,
        collected,
        missing_total,
        failed_batches,
    )
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="独立采集 QM9 与 PubChem 公开化学数据，不影响论文 collector。"
    )
    parser.add_argument(
        "--source",
        choices=("qm9", "pubchem", "all"),
        default="all",
        help="采集来源；all 按 QM9、PubChem 顺序执行（默认：all）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("chemistry_dataset/public_datasets"),
        help="独立输出根目录",
    )
    parser.add_argument(
        "--contact-email",
        default=os.getenv("COLLECTOR_CONTACT_EMAIL", ""),
        help="写入礼貌性 User-Agent；默认读取 COLLECTOR_CONTACT_EMAIL",
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--min-free-gb", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true", help="只打印执行计划")

    qm9 = parser.add_argument_group("QM9")
    qm9.add_argument("--qm9-url", default=QM9_ARCHIVE_URL)
    qm9.add_argument(
        "--qm9-extract",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="下载后解压 XYZ（默认开启，可用 --no-qm9-extract 关闭）",
    )
    qm9.add_argument("--qm9-force-download", action="store_true")

    pubchem = parser.add_argument_group("PubChem")
    pubchem.add_argument("--pubchem-start-cid", type=int, default=1)
    pubchem.add_argument(
        "--pubchem-count",
        type=int,
        default=1000,
        help="从 start-cid 起扫描的连续 CID 数（默认：1000）",
    )
    pubchem.add_argument("--pubchem-batch-size", type=int, default=100)
    pubchem.add_argument(
        "--pubchem-sdf",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="同时保存压缩 SDF（默认开启，可用 --no-pubchem-sdf 关闭）",
    )
    pubchem.add_argument(
        "--pubchem-record-type", choices=("2d", "3d"), default="2d"
    )
    pubchem.add_argument(
        "--pubchem-request-interval",
        type=float,
        default=0.25,
        help="PUG REST 请求最小间隔秒数（默认：0.25）",
    )
    args = parser.parse_args(argv)

    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds 必须大于0")
    if args.max_retries < 0:
        parser.error("--max-retries 不能小于0")
    if args.min_free_gb < 0:
        parser.error("--min-free-gb 不能小于0")
    if args.pubchem_start_cid <= 0:
        parser.error("--pubchem-start-cid 必须大于0")
    if args.pubchem_count <= 0:
        parser.error("--pubchem-count 必须大于0")
    if not 1 <= args.pubchem_batch_size <= 100:
        parser.error("--pubchem-batch-size 必须在1到100之间")
    if args.pubchem_request_interval < 0:
        parser.error("--pubchem-request-interval 不能小于0")
    return args


def configure_logging(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_handler = logging.FileHandler(
        root / "public_dataset_collector.log", encoding="utf-8"
    )
    handlers.append(file_handler)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "collector_version": VERSION,
        "source": args.source,
        "out": str(args.out),
        "independent_from_paper_collector": True,
    }
    if args.source in {"qm9", "all"}:
        plan["qm9"] = {
            "url": args.qm9_url,
            "extract": args.qm9_extract,
        }
    if args.source in {"pubchem", "all"}:
        plan["pubchem"] = {
            "start_cid": args.pubchem_start_cid,
            "end_cid": args.pubchem_start_cid + args.pubchem_count - 1,
            "batch_size": args.pubchem_batch_size,
            "download_sdf": args.pubchem_sdf,
            "record_type": args.pubchem_record_type,
        }
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.out.resolve()
    if args.dry_run:
        print(json.dumps(dry_run_plan(args), ensure_ascii=False, indent=2))
        return 0

    configure_logging(root)
    install_signal_handlers()
    runtime_path = root / "runtime_state.json"
    lock_path = root / ".public_dataset_collector.lock"
    overall_state: dict[str, Any] = {
        "collector_version": VERSION,
        "pid": os.getpid(),
        "source": args.source,
        "status": "starting",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "out": str(root),
    }
    try:
        with process_lock(lock_path):
            atomic_write_json(runtime_path, overall_state)
            overall_state["status"] = "running"
            atomic_write_json(runtime_path, overall_state)
            client = HttpClient(
                contact_email=args.contact_email,
                timeout_seconds=args.timeout_seconds,
                max_retries=args.max_retries,
                request_interval_seconds=(
                    args.pubchem_request_interval
                    if args.source in {"pubchem", "all"}
                    else 0.0
                ),
            )
            results: dict[str, Any] = {}
            try:
                if args.source in {"qm9", "all"}:
                    results["qm9"] = collect_qm9(args, client, root)
                if args.source in {"pubchem", "all"} and not STOP_REQUESTED:
                    results["pubchem"] = collect_pubchem(args, client, root)
            finally:
                client.close()

            overall_state.update(
                {
                    "status": "stopped" if STOP_REQUESTED else "completed",
                    "results": results,
                    "updated_at": utc_now(),
                }
            )
            atomic_write_json(runtime_path, overall_state)
            return 130 if STOP_REQUESTED else 0
    except CollectorLockError as exc:
        logging.error("%s", exc)
        return 1
    except Exception as exc:
        overall_state.update(
            {
                "status": "stopped" if STOP_REQUESTED else "failed",
                "error": type(exc).__name__,
                "message": str(exc),
                "updated_at": utc_now(),
            }
        )
        atomic_write_json(runtime_path, overall_state)
        if STOP_REQUESTED:
            logging.warning("公开数据集采集已停止：%s", exc)
            return 130
        logging.exception("公开数据集采集失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
