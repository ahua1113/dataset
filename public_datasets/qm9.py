"""QM9 Figshare 原始数据下载、校验和安全解压。"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import tarfile
from typing import Any

from public_datasets import VERSION
from public_datasets.common import (
    CollectorError,
    atomic_write_json,
    require_free_disk,
    sha256_file,
    stop_requested,
    utc_now,
)
from public_datasets.http import HttpClient, download_with_resume


QM9_ARCHIVE_URL = "https://ndownloader.figshare.com/files/3195389"


def validate_tar_member(member: tarfile.TarInfo, destination: Path) -> None:
    if member.issym() or member.islnk():
        raise CollectorError(f"QM9 压缩包包含不允许的链接：{member.name}")
    root = destination.resolve()
    target = (destination / member.name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CollectorError(f"QM9 压缩包包含越界路径：{member.name}") from exc


def extract_archive(archive: Path, destination: Path) -> tuple[int, int]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted_now = 0
    skipped_existing = 0
    with tarfile.open(archive, mode="r:bz2") as bundle:
        for index, member in enumerate(bundle, start=1):
            if stop_requested():
                raise CollectorError("采集任务已请求停止；已解压文件将保留")
            validate_tar_member(member, destination)
            target = destination / member.name
            if member.isfile() and target.exists() and target.stat().st_size == member.size:
                skipped_existing += 1
                continue
            try:
                bundle.extract(member, path=destination, filter="data")
            except TypeError:  # Python 3.9—3.11 尚不支持 filter 参数
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


def collect(args: argparse.Namespace, client: HttpClient, root: Path) -> dict[str, Any]:
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
            if next(iter(bundle), None) is None:
                raise CollectorError("QM9 压缩包为空")
    except (tarfile.TarError, OSError) as exc:
        raise CollectorError(f"QM9 压缩包校验失败：{exc}") from exc

    archive_sha256 = sha256_file(archive)
    extracted_now = 0
    skipped_existing = 0
    if args.qm9_extract:
        require_free_disk(root, args.min_free_gb)
        logging.info("开始解压 QM9；重复执行会跳过已完成文件")
        extracted_now, skipped_existing = extract_archive(archive, extract_dir)

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
