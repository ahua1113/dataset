"""PubChem PUG REST 属性与 SDF 批量采集。"""

from __future__ import annotations

import argparse
import gzip
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
        records = payload.get("PropertyTable", {}).get("Properties", [])
        if not isinstance(records, list):
            raise CollectorError("PubChem 属性响应缺少 PropertyTable.Properties")
        found = {int(record["CID"]) for record in records if "CID" in record}
        return records, [cid for cid in cids if cid not in found]
    except HttpRequestError as exc:
        if exc.status_code not in {400, 404}:
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


def load_recorded_cids(path: Path) -> set[int]:
    result: set[int] = set()
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                result.add(int(json.loads(line)["CID"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                logging.warning("忽略 records.jsonl 第 %s 行的无效记录", line_number)
    return result


def load_known_missing_cids(path: Path) -> set[int]:
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
            properties, missing = fetch_properties(client, batch)
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
                    save_gzip_atomic(
                        sdf_path,
                        fetch_sdf(client, valid_cids, args.pubchem_record_type),
                    )
                sdf_record = {
                    "path": str(sdf_path.relative_to(root)),
                    "bytes": sdf_path.stat().st_size,
                    "sha256": sha256_file(sdf_path),
                    "record_type": args.pubchem_record_type,
                }

            # SDF 成功后再提交属性，避免失败后 CID 被误判为已完成。
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
                        "sdf": sdf_record,
                        "completed_at": utc_now(),
                    }
                ],
            )
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
