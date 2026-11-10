"""PubChem PUG REST 属性与 SDF 批量采集。"""

from __future__ import annotations

import argparse
import gzip
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote

from public_datasets import VERSION
from public_datasets.common import (
    HttpRequestError,
    append_jsonl,
    atomic_write_json,
    require_free_disk,
    sha256_file,
    stop_requested,
    utc_now,
    CollectorError,
)
from public_datasets.http import HttpClient


PUG_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
SDF_CID_TAG = b"> <PUBCHEM_COMPOUND_CID>"
PROPERTIES = (
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


def chunks(values: Iterable[int], size: int) -> Iterator[list[int]]:
    batch: list[int] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def property_url(cids: Sequence[int]) -> str:
    cid_text = quote(",".join(str(cid) for cid in cids), safe=",")
    return f"{PUG_BASE}/compound/cid/{cid_text}/property/{','.join(PROPERTIES)}/JSON"


def fetch_properties(
    client: HttpClient,
    cids: Sequence[int],
) -> tuple[list[dict[str, Any]], list[int]]:
    if not cids:
        return [], []
    url = property_url(cids)
    try:
        response = client.get(url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectorError(f"PubChem 返回了无效 JSON：{url}") from exc
        finally:
            response.close()
        table = payload.get("PropertyTable") if isinstance(payload, dict) else None
        records = table.get("Properties") if isinstance(table, dict) else None
        if not isinstance(records, list):
            raise CollectorError("PubChem 属性响应缺少 PropertyTable.Properties")
        try:
            found = {int(record["CID"]) for record in records}
        except (KeyError, TypeError, ValueError) as exc:
            raise CollectorError("PubChem 属性记录缺少有效 CID") from exc
        if len(found) != len(records) or not found.issubset(cids):
            raise CollectorError("PubChem 属性响应包含重复或未请求的 CID")
        return records, [cid for cid in cids if cid not in found]
    except HttpRequestError as exc:
        if exc.status_code != 404:
            raise
        if len(cids) == 1:
            return [], [int(cids[0])]
        middle = len(cids) // 2
        left_records, left_missing = fetch_properties(client, cids[:middle])
        right_records, right_missing = fetch_properties(client, cids[middle:])
        return left_records + right_records, left_missing + right_missing


def fetch_sdf(client: HttpClient, cids: Sequence[int], record_type: str) -> bytes:
    cid_text = quote(",".join(str(cid) for cid in cids), safe=",")
    url = f"{PUG_BASE}/compound/cid/{cid_text}/record/SDF?record_type={record_type}"
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


def sdf_cids(lines: Iterable[bytes]) -> set[int]:
    """Read the CID of every complete record in a PubChem SDF."""
    cids: set[int] = set()
    cid = None
    in_record = False
    lines = iter(lines)
    for raw_line in lines:
        line = raw_line.strip()
        if line == b"$$$$":
            if cid is None or cid in cids:
                raise CollectorError("PubChem SDF 记录缺少 CID 或 CID 重复")
            cids.add(cid)
            cid = None
            in_record = False
        elif line:
            in_record = True
            if line == SDF_CID_TAG:
                if cid is not None:
                    raise CollectorError("PubChem SDF 记录含有多个 CID 字段")
                cid = int(next(lines, b"").strip())
    if in_record or not cids:
        raise CollectorError("PubChem SDF 没有完整记录")
    return cids


def fetch_sdf_batch(
    client: HttpClient, cids: Sequence[int], record_type: str
) -> tuple[bytes, list[int], list[int]]:
    """Fetch structures and confirm omitted 3D CIDs individually."""
    try:
        content = fetch_sdf(client, cids, record_type)
    except HttpRequestError as exc:
        if record_type != "3d" or exc.status_code != 404:
            raise
        if len(cids) == 1:
            return b"", [], list(cids)
        content = b""
        found: set[int] = set()
    else:
        found = sdf_cids(content.splitlines())
        if found == set(cids):
            return content, list(cids), []
        if record_type != "3d" or not found.issubset(cids):
            raise CollectorError("PubChem SDF 返回的 CID 与请求不一致")

    parts = [content] if content else []
    missing: list[int] = []
    for cid in cids:
        if cid in found:
            continue
        try:
            record = fetch_sdf(client, [cid], record_type)
        except HttpRequestError as exc:
            if exc.status_code != 404:
                raise
            missing.append(cid)
            continue
        if sdf_cids(record.splitlines()) != {cid}:
            raise CollectorError("PubChem SDF 返回的 CID 与请求不一致")
        parts.append(record)
        found.add(cid)
    combined = b"".join(part.rstrip(b"\r\n") + b"\n" for part in parts)
    return combined, [cid for cid in cids if cid in found], missing


def load_sdf_cids(
    manifest_path: Path, root: Path, requested: range, record_type: str
) -> set[int]:
    """Recover verified SDF coverage from both current and legacy manifests."""
    covered: set[int] = set()
    if not manifest_path.exists():
        return covered
    sdf_dir = (root / "pubchem" / "sdf").resolve()
    with manifest_path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                batch = json.loads(line)
                sdf = batch["sdf"]
                if not sdf or sdf["record_type"] != record_type:
                    continue
                if (
                    batch["requested_cid_end"] < requested.start
                    or batch["requested_cid_start"] >= requested.stop
                ):
                    continue
                # Existing manifests may use the path separator of another platform.
                path = (root / sdf["path"].replace("\\", "/")).resolve()
                if not path.is_relative_to(sdf_dir) or sha256_file(path) != sdf["sha256"]:
                    raise CollectorError("PubChem SDF 路径或 SHA-256 与清单不一致")
                with gzip.open(path, "rb") as records:
                    covered.update(cid for cid in sdf_cids(records) if cid in requested)
            except (OSError, EOFError, ValueError, KeyError, TypeError, CollectorError) as exc:
                logging.warning("忽略 PubChem SDF 清单第 %s 行：%s", line_number, exc)
    return covered


def load_recorded_cids(path: Path) -> set[int]:
    result: set[int] = set()
    if not path.exists():
        return result
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                result.add(int(json.loads(line)["CID"]))
            except (KeyError, TypeError, ValueError):
                logging.warning("忽略 records.jsonl 第 %s 行的无效记录", line_number)
    return result


def load_known_missing_cids(path: Path, record_type: str | None = None) -> set[int]:
    result: set[int] = set()
    if not path.exists():
        return result
    error = "CID not found" if record_type is None else "SDF not found"
    with path.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if (
                    isinstance(record, dict)
                    and record.get("error") == error
                    and record.get("record_type") == record_type
                    and record.get("verified_missing") is True
                ):
                    result.add(int(record["cid"]))
            except (KeyError, TypeError, ValueError):
                continue
    return result


def collect(args: argparse.Namespace, client: HttpClient, root: Path) -> dict[str, Any]:
    pubchem_dir = root / "pubchem"
    records_path = pubchem_dir / "records.jsonl"
    errors_path = pubchem_dir / "errors.jsonl"
    batch_manifest_path = pubchem_dir / "batch_manifest.jsonl"
    state_path = pubchem_dir / "state.json"
    sdf_dir = pubchem_dir / "sdf"
    start_cid = args.pubchem_start_cid
    end_cid = start_cid + args.pubchem_count - 1
    requested = range(start_cid, end_cid + 1)
    existing = load_recorded_cids(records_path)
    known_missing = load_known_missing_cids(errors_path)
    record_type = args.pubchem_record_type
    known_missing_sdf = (
        load_known_missing_cids(errors_path, record_type)
        if args.pubchem_sdf and record_type == "3d" else set()
    )
    stored_sdf = (
        load_sdf_cids(batch_manifest_path, root, requested, record_type)
        if args.pubchem_sdf else set()
    )

    def needs_collection(cid: int) -> bool:
        if cid in existing:
            return args.pubchem_sdf and cid not in stored_sdf and cid not in known_missing_sdf
        return cid not in known_missing

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
        "pending_cid_count": sum(needs_collection(cid) for cid in requested),
        "download_sdf": args.pubchem_sdf,
        "record_type": args.pubchem_record_type,
        "updated_at": utc_now(),
    }
    atomic_write_json(state_path, state)
    require_free_disk(root, args.min_free_gb)

    collected = 0
    collected_sdf = 0
    missing_sdf_total = 0
    missing_total = 0
    failed_batches = 0
    pending_cids = (cid for cid in requested if needs_collection(cid))
    for batch_index, batch in enumerate(
        chunks(pending_cids, args.pubchem_batch_size), start=1
    ):
        if stop_requested():
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
            properties, missing = fetch_properties(
                client, [cid for cid in batch if cid not in existing]
            )
            retrieved_at = utc_now()
            normalized = []
            for record in properties:
                item = dict(record)
                item["dataset"] = "PubChem Compound"
                item["retrieved_at"] = retrieved_at
                item["source_url"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{item['CID']}"
                normalized.append(item)

            sdf_record: dict[str, Any] | None = None
            target_sdf_cids: list[int] = []
            missing_sdf: list[int] = []
            if args.pubchem_sdf:
                new_cids = {int(item["CID"]) for item in normalized}
                target_sdf_cids = [
                    cid for cid in batch
                    if (cid in existing or cid in new_cids) and cid not in stored_sdf
                    and cid not in known_missing_sdf
                ]
            if target_sdf_cids:
                content, target_sdf_cids, missing_sdf = fetch_sdf_batch(
                    client, target_sdf_cids, record_type
                )
            if target_sdf_cids:
                stem = f"cid_{batch[0]:09d}_{batch[-1]:09d}"
                sdf_path = sdf_dir / f"{stem}_{record_type}.sdf.gz"
                if sdf_path.exists():
                    digest = sha256(content).hexdigest()[:16]
                    sdf_path = sdf_dir / f"{stem}_{digest}_{record_type}.sdf.gz"
                save_gzip_atomic(sdf_path, content)
                sdf_record = {
                    "path": str(sdf_path.relative_to(root)),
                    "bytes": sdf_path.stat().st_size,
                    "sha256": sha256_file(sdf_path),
                    "record_type": record_type,
                    "cids": target_sdf_cids,
                }

            # SDF 成功后再提交属性，避免失败后 CID 被误判为已完成。
            if normalized or not records_path.exists():
                append_jsonl(records_path, normalized)
            if missing:
                append_jsonl(
                    errors_path,
                    (
                        {
                            "dataset": "PubChem Compound",
                            "cid": cid,
                            "error": "CID not found",
                            "verified_missing": True,
                            "recorded_at": retrieved_at,
                        }
                        for cid in missing
                    ),
                )
            if missing_sdf:
                append_jsonl(
                    errors_path,
                    (
                        {
                            "dataset": "PubChem Compound",
                            "cid": cid,
                            "error": "SDF not found",
                            "record_type": record_type,
                            "verified_missing": True,
                            "recorded_at": retrieved_at,
                        }
                        for cid in missing_sdf
                    ),
                )
            append_jsonl(
                batch_manifest_path,
                [
                    {
                        "dataset": "PubChem Compound",
                        "requested_cid_start": batch[0],
                        "requested_cid_end": batch[-1],
                        "requested_count": len(batch),
                        "retrieved_count": len(normalized),
                        "missing_cids": missing,
                        "sdf_missing_cids": missing_sdf,
                        "sdf": sdf_record,
                        "completed_at": utc_now(),
                    }
                ],
            )
            collected += len(normalized)
            missing_total += len(missing)
            collected_sdf += len(target_sdf_cids)
            missing_sdf_total += len(missing_sdf)
            stored_sdf.update(target_sdf_cids)
            known_missing_sdf.update(missing_sdf)
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
                "last_processed_cid": batch[-1],
                "collected_in_run": collected,
                "sdf_collected_in_run": collected_sdf,
                "sdf_missing_in_run": missing_sdf_total,
                "missing_in_run": missing_total,
                "failed_batches": failed_batches,
                "updated_at": utc_now(),
            }
        )
        atomic_write_json(state_path, state)

    final_existing = load_recorded_cids(records_path)
    summary = {
        "collector_version": VERSION,
        "dataset": "PubChem Compound",
        "requested_start_cid": start_cid,
        "requested_end_cid": end_cid,
        "requested_cid_count": args.pubchem_count,
        "records_in_range": sum(1 for cid in requested if cid in final_existing),
        "collected_in_run": collected,
        "sdf_collected_in_run": collected_sdf,
        "sdf_records_in_range": len(stored_sdf) if args.pubchem_sdf else None,
        "sdf_missing_in_run": missing_sdf_total,
        "sdf_known_missing_in_range": sum(cid in requested for cid in known_missing_sdf),
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
        "PubChem 完成：CID %s—%s，有效记录 %s，本次新增 %s，失败批次 %s",
        start_cid,
        end_cid,
        summary["records_in_range"],
        collected,
        failed_batches,
    )
    return summary
