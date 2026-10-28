#!/usr/bin/env python3
"""按 PDF SHA-256 从旧 manifest 恢复孤立论文元数据并重建 V6.4 量化报告。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chemistry_collector.analysis import (
    PaperEntry,
    classify_missing_disciplines,
    deduplicate_entries,
    write_quantitative_analysis,
)
from chemistry_collector.disciplines import load_disciplines


STORAGE_FIELDS = {
    "category", "status", "file", "bytes", "sha256", "mime_expected",
    "consolidation_file_status", "original_recorded_sha256",
    "consolidation_warning", "merged_record_count",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jsonl_text(text: str, source: str) -> list[dict]:
    records: list[dict] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"旧清单 JSON 无效：{source}:{line_number}: {exc}"
            ) from exc
        if record.get("category") == "papers" and record.get("sha256"):
            records.append(record)
    return records


def load_jsonl(path: Path) -> list[dict]:
    """读取论文 JSONL；当前 manifest 必须完整有效。"""
    if not path.is_file():
        raise FileNotFoundError(f"找不到 manifest：{path}")
    return _load_jsonl_text(path.read_text(encoding="utf-8"), str(path))


def load_git_manifest(repo: Path, git_ref: str, git_path: str) -> list[dict]:
    """从本地 Git 历史读取旧清单，不需要恢复或改写工作区文件。"""
    try:
        result = subprocess.run(
            ["git", "show", f"{git_ref}:{git_path}"],
            cwd=repo, check=True, capture_output=True, text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git show failed").strip()
        raise RuntimeError(
            f"无法读取 Git 历史清单 {git_ref}:{git_path}: {detail}"
        ) from exc
    return _load_jsonl_text(result.stdout, f"{git_ref}:{git_path}")


def _has_article_metadata(record: dict) -> bool:
    return bool(
        str(record.get("title") or "").strip()
        and (record.get("doi") or record.get("openalex_id"))
        and record.get("journal")
    )


def recover_records(current: list[dict], legacy: list[tuple[str, dict]]) -> tuple[list[dict], dict]:
    """只按相同 SHA-256 合并，不根据文件名或模糊标题猜测论文身份。"""
    legacy_by_sha: dict[str, tuple[str, dict]] = {}
    for source, record in legacy:
        digest = str(record.get("sha256") or "").casefold()
        if digest and (digest not in legacy_by_sha
                       or _has_article_metadata(record)):
            legacy_by_sha[digest] = (source, record)

    recovered = already_complete = unmatched = 0
    output: list[dict] = []
    recovered_at = _now_utc()
    for current_record in current:
        row = dict(current_record)
        if _has_article_metadata(row):
            already_complete += 1
            output.append(row)
            continue
        digest = str(row.get("sha256") or "").casefold()
        match = legacy_by_sha.get(digest)
        if match is None or not _has_article_metadata(match[1]):
            unmatched += 1
            output.append(row)
            continue
        source, old = match
        merged = dict(old)
        for key in STORAGE_FIELDS:
            if key in row:
                merged[key] = row[key]
        for key, value in row.items():
            if key not in merged or merged[key] in (None, "", [], {}):
                merged[key] = value
        merged.update({
            "metadata_status": "recovered_from_legacy_manifest",
            "metadata_recovered_at": recovered_at,
            "metadata_recovery_key": "sha256",
            "metadata_recovery_source": source,
        })
        recovered += 1
        output.append(merged)
    return output, {
        "records": len(current),
        "already_complete": already_complete,
        "recovered": recovered,
        "unmatched": unmatched,
        "legacy_unique_sha256": len(legacy_by_sha),
    }


def _write_manifest(path: Path, records: list[dict], backup: bool) -> None:
    if backup:
        backup_path = path.with_name(path.stem + ".before_v6_4_recovery.jsonl")
        if not backup_path.exists():
            shutil.copy2(path, backup_path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path,
        default=ROOT / "chemistry_dataset" / "papers" / "manifest.jsonl",
    )
    parser.add_argument(
        "--legacy-manifest", type=Path, action="append", default=[],
        help="可重复指定旧 manifest 文件",
    )
    parser.add_argument("--git-ref", help="从本地 Git 历史读取旧 manifest 的提交")
    parser.add_argument(
        "--git-path", action="append", default=[],
        help="与 --git-ref 配合，可重复指定仓库内的旧 manifest 路径",
    )
    parser.add_argument(
        "--disciplines-file", type=Path,
        default=ROOT / "config" / "disciplines_v6_4.json",
    )
    parser.add_argument(
        "--analysis-dir", type=Path,
        default=ROOT / "chemistry_dataset" / "quantitative_analysis",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()

    if bool(args.git_ref) != bool(args.git_path):
        parser.error("--git-ref and at least one --git-path must be used together")
    if not args.legacy_manifest and not args.git_ref:
        parser.error("specify --legacy-manifest or --git-ref with --git-path")

    legacy: list[tuple[str, dict]] = []
    for path in args.legacy_manifest:
        for record in load_jsonl(path):
            legacy.append((str(path), record))
    if args.git_ref:
        for git_path in args.git_path:
            source = f"{args.git_ref}:{git_path}"
            for record in load_git_manifest(ROOT, args.git_ref, git_path):
                legacy.append((source, record))

    current = load_jsonl(args.manifest)
    recovered, stats = recover_records(current, legacy)
    disciplines = load_disciplines(args.disciplines_file)
    classified = classify_missing_disciplines(recovered, disciplines)
    manifest_root = args.manifest.parent
    unique_entries, duplicate_records = deduplicate_entries([
        PaperEntry(
            record, manifest_root,
            manifest_root / Path(str(record.get("file") or "")),
        )
        for record in classified
    ])
    classified = [entry.record for entry in unique_entries]
    stats["duplicate_records_removed"] = duplicate_records
    print(
        "[RECOVERY] " + " ".join(f"{key}={value}" for key, value in stats.items()),
        flush=True,
    )
    if args.dry_run:
        return
    _write_manifest(args.manifest, classified, args.backup)
    warnings = [
        f"仍有 {stats['unmatched']} 条孤立 PDF 记录无法从指定旧清单恢复元数据"
    ] if stats["unmatched"] else []
    write_quantitative_analysis(
        classified, args.analysis_dir, source_roots=[args.manifest.parent],
        duplicate_records=duplicate_records, warnings=warnings,
        disciplines=disciplines,
    )
    print(f"[OUTPUT] {args.manifest.resolve()}")
    print(f"[ANALYSIS] {args.analysis_dir.resolve()}")


if __name__ == "__main__":
    main()
