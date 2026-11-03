"""公开数据集采集器命令行参数与任务编排。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence

from public_datasets import VERSION
from public_datasets import pubchem, qm9
from public_datasets.common import (
    CollectorLockError,
    atomic_write_json,
    configure_logging,
    install_signal_handlers,
    process_lock,
    reset_stop_request,
    stop_requested,
    utc_now,
)
from public_datasets.http import HttpClient


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

    qm9_group = parser.add_argument_group("QM9")
    qm9_group.add_argument("--qm9-url", default=qm9.QM9_ARCHIVE_URL)
    qm9_group.add_argument(
        "--qm9-extract",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="下载后解压 XYZ（默认开启，可用 --no-qm9-extract 关闭）",
    )
    qm9_group.add_argument("--qm9-force-download", action="store_true")

    pubchem_group = parser.add_argument_group("PubChem")
    pubchem_group.add_argument("--pubchem-start-cid", type=int, default=1)
    pubchem_group.add_argument(
        "--pubchem-count",
        type=int,
        default=1000,
        help="从 start-cid 起扫描的连续 CID 数（默认：1000）",
    )
    pubchem_group.add_argument("--pubchem-batch-size", type=int, default=100)
    pubchem_group.add_argument(
        "--pubchem-sdf",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="同时保存压缩 SDF（默认开启，可用 --no-pubchem-sdf 关闭）",
    )
    pubchem_group.add_argument(
        "--pubchem-record-type", choices=("2d", "3d"), default="2d"
    )
    pubchem_group.add_argument(
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


def dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "collector_version": VERSION,
        "source": args.source,
        "out": str(args.out),
        "independent_from_paper_collector": True,
    }
    if args.source in {"qm9", "all"}:
        plan["qm9"] = {"url": args.qm9_url, "extract": args.qm9_extract}
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
    reset_stop_request()
    install_signal_handlers()
    runtime_path = root / "runtime_state.json"
    lock_path = root / ".public_dataset_collector.lock"
    state: dict[str, Any] = {
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
            atomic_write_json(runtime_path, state)
            state["status"] = "running"
            atomic_write_json(runtime_path, state)
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
                    results["qm9"] = qm9.collect(args, client, root)
                if args.source in {"pubchem", "all"} and not stop_requested():
                    results["pubchem"] = pubchem.collect(args, client, root)
            finally:
                client.close()

            state.update(
                {
                    "status": "stopped" if stop_requested() else "completed",
                    "results": results,
                    "updated_at": utc_now(),
                }
            )
            atomic_write_json(runtime_path, state)
            return 130 if stop_requested() else 0
    except CollectorLockError as exc:
        logging.error("%s", exc)
        return 1
    except Exception as exc:
        state.update(
            {
                "status": "stopped" if stop_requested() else "failed",
                "error": type(exc).__name__,
                "message": str(exc),
                "updated_at": utc_now(),
            }
        )
        atomic_write_json(runtime_path, state)
        if stop_requested():
            logging.warning("公开数据集采集已停止：%s", exc)
            return 130
        logging.exception("公开数据集采集失败")
        return 1
