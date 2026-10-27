"""论文数据目录合并、去重与量化分析。"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import statistics

from .disciplines import Discipline, classify_work, normalize_license
from .utils import normalize_doi, normalized_id, normalized_title


@dataclass
class PaperEntry:
    record: dict
    source_root: Path
    source_file: Path | None


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_source_file(root: Path, value: object) -> Path | None:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    relative = PurePosixPath(text)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return root.joinpath(*relative.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_paper_roots(dataset_root: Path, canonical_root: Path) -> list[Path]:
    """发现包含论文 manifest 或 raw/papers 的数据目录。"""
    dataset_root = dataset_root.resolve()
    canonical_root = canonical_root.resolve()
    roots: set[Path] = set()
    if not dataset_root.is_dir():
        return []
    for manifest in dataset_root.rglob("manifest.jsonl"):
        root = manifest.parent.resolve()
        if root != canonical_root and canonical_root not in root.parents:
            roots.add(root)
    for raw_papers in dataset_root.rglob("raw/papers"):
        if not raw_papers.is_dir():
            continue
        root = raw_papers.parent.parent.resolve()
        if root != canonical_root and canonical_root not in root.parents:
            roots.add(root)
    return sorted(roots, key=lambda path: str(path).casefold())


def load_paper_entries(source_roots: list[Path]) -> tuple[list[PaperEntry], list[str]]:
    """读取全部论文记录，并把未被 manifest 引用的 PDF 作为孤立文件纳入。"""
    entries: list[PaperEntry] = []
    warnings: list[str] = []
    for source_root in source_roots:
        root = source_root.resolve()
        referenced: set[Path] = set()
        manifest = root / "manifest.jsonl"
        if manifest.is_file():
            for line_number, raw in enumerate(
                manifest.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except ValueError as exc:
                    warnings.append(f"{manifest}:{line_number}: invalid JSON: {exc}")
                    continue
                if (record.get("category") != "papers"
                        or record.get("status") != "downloaded"):
                    continue
                source_file = _safe_source_file(root, record.get("file"))
                if source_file is not None:
                    referenced.add(source_file.resolve())
                entries.append(PaperEntry(dict(record), root, source_file))
        raw_dir = root / "raw" / "papers"
        if raw_dir.is_dir():
            for pdf in sorted(path for path in raw_dir.iterdir()
                              if path.is_file() and path.suffix.casefold() == ".pdf"):
                if pdf.resolve() in referenced:
                    continue
                digest = _sha256(pdf)
                entries.append(PaperEntry({
                    "category": "papers",
                    "resource_kind": "research_article",
                    "title": "",
                    "status": "downloaded",
                    "file": pdf.relative_to(root).as_posix(),
                    "bytes": pdf.stat().st_size,
                    "sha256": digest,
                    "metadata_status": "orphan_pdf",
                }, root, pdf))
                warnings.append(f"orphan PDF included without article metadata: {pdf}")
    return entries, warnings


def _identity_keys(record: dict) -> list[str]:
    keys: list[str] = []
    sha = str(record.get("sha256") or "").strip().casefold()
    if len(sha) == 64:
        keys.append("sha:" + sha)
    doi = normalize_doi(str(record.get("doi") or ""))
    if doi:
        keys.append("doi:" + doi)
    openalex = normalized_id(str(record.get("openalex_id") or ""))
    if openalex:
        keys.append("openalex:" + openalex)
    if not keys:
        title = normalized_title(str(record.get("title") or ""))
        if title:
            keys.append("title:" + title)
    return keys or ["anonymous:" + hashlib.sha256(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()]


def _quality(entry: PaperEntry) -> tuple[int, int, str]:
    record = entry.record
    file_present = bool(entry.source_file and entry.source_file.is_file())
    fields = sum(bool(record.get(key)) for key in (
        "doi", "openalex_id", "journal", "primary_discipline",
        "published_at", "license_note", "oa_pdf_source",
    ))
    return (100 if file_present else 0, fields, str(record.get("collected_at") or ""))


def _fill_missing(winner: dict, other: dict) -> dict:
    merged = dict(winner)
    for key, value in other.items():
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    return merged


def deduplicate_entries(entries: list[PaperEntry]) -> tuple[list[PaperEntry], int]:
    """按 SHA、DOI、OpenAlex ID 依次关联重复记录，并保留信息更完整的一条。"""
    groups: list[list[PaperEntry]] = []
    key_to_group: dict[str, int] = {}
    for entry in entries:
        matching = sorted({key_to_group[key] for key in _identity_keys(entry.record)
                           if key in key_to_group})
        if not matching:
            group_index = len(groups)
            groups.append([entry])
        else:
            group_index = matching[0]
            groups[group_index].append(entry)
            for extra_index in reversed(matching[1:]):
                groups[group_index].extend(groups[extra_index])
                groups[extra_index] = []
                for key, value in list(key_to_group.items()):
                    if value == extra_index:
                        key_to_group[key] = group_index
        for member in groups[group_index]:
            for key in _identity_keys(member.record):
                key_to_group[key] = group_index

    unique: list[PaperEntry] = []
    for group in groups:
        if not group:
            continue
        ranked = sorted(group, key=_quality, reverse=True)
        selected = ranked[0]
        record = dict(selected.record)
        for alternative in ranked[1:]:
            record = _fill_missing(record, alternative.record)
        record["merged_record_count"] = len(group)
        unique.append(PaperEntry(record, selected.source_root, selected.source_file))
    return unique, len(entries) - len(unique)


def consolidate_entries(entries: list[PaperEntry], canonical_root: Path,
                        *, move_files: bool = False) -> tuple[list[dict], dict]:
    """将存在的 PDF 汇总到唯一 raw/papers 目录，并原子写入统一 manifest。"""
    canonical_root = canonical_root.resolve()
    raw_dir = canonical_root / "raw" / "papers"
    raw_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    copied = moved = missing = 0
    for entry in entries:
        record = dict(entry.record)
        source = entry.source_file
        if source is None or not source.is_file():
            record["consolidation_file_status"] = "source_file_missing"
            missing += 1
            records.append(record)
            continue
        with source.open("rb") as stream:
            signature = stream.read(5)
        if source.suffix.casefold() != ".pdf" or signature != b"%PDF-":
            record["consolidation_file_status"] = "invalid_pdf_file"
            missing += 1
            records.append(record)
            continue
        actual_sha = _sha256(source)
        recorded_sha = str(record.get("sha256") or "").casefold()
        if recorded_sha and recorded_sha != actual_sha:
            record["original_recorded_sha256"] = recorded_sha
            record["consolidation_warning"] = "recorded_sha256_mismatch"
        target = raw_dir / f"papers_{actual_sha[:20]}.pdf"
        if target.exists() and _sha256(target) != actual_sha:
            raise RuntimeError(f"Canonical filename collision: {target}")
        if not target.exists():
            if move_files:
                shutil.move(str(source), str(target))
                moved += 1
            else:
                shutil.copy2(source, target)
                copied += 1
        record.update({
            "file": target.relative_to(canonical_root).as_posix(),
            "bytes": target.stat().st_size,
            "sha256": actual_sha,
            "status": "downloaded",
            "consolidation_file_status": "verified_local_pdf",
        })
        records.append(record)
    records.sort(key=lambda row: (
        str(row.get("journal") or "").casefold(),
        str(row.get("published_at") or ""),
        str(row.get("title") or "").casefold(),
    ))
    canonical_root.mkdir(parents=True, exist_ok=True)
    manifest = canonical_root / "manifest.jsonl"
    temporary = manifest.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
        encoding="utf-8",
    )
    temporary.replace(manifest)
    return records, {"copied": copied, "moved": moved, "missing": missing}


def classify_missing_disciplines(records: list[dict],
                                 disciplines: list[Discipline]) -> list[dict]:
    normalized: list[dict] = []
    valid_ids = {discipline.id for discipline in disciplines}
    for original in records:
        record = dict(original)
        if record.get("primary_discipline") in valid_ids:
            record["analysis_discipline_origin"] = "manifest"
        else:
            result = classify_work(
                record, str(record.get("journal_group") or ""), disciplines
            )
            record.update(result)
            record["analysis_discipline_origin"] = "reclassified_available_metadata"
        normalized.append(record)
    return normalized


def _counter_rows(counter: Counter, key_name: str, total: int) -> list[dict]:
    return [{
        key_name: key,
        "count": count,
        "percentage": round(100 * count / total, 2) if total else 0.0,
    } for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else ["value", "count", "percentage"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _date_bucket(value: object, width: int) -> str:
    text = str(value or "").strip()
    if len(text) < width:
        return "unknown"
    candidate = text[:width]
    try:
        datetime.strptime(candidate, "%Y" if width == 4 else "%Y-%m")
        return candidate
    except ValueError:
        return "unknown"


def _size_bucket(value: object) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "unknown"
    mib = size / (1024 * 1024)
    if size <= 0:
        return "unknown"
    if mib < 1:
        return "<1 MiB"
    if mib < 5:
        return "1-5 MiB"
    if mib < 10:
        return "5-10 MiB"
    if mib < 25:
        return "10-25 MiB"
    if mib < 50:
        return "25-50 MiB"
    return ">=50 MiB"


def _license_bucket(record: dict) -> str:
    label, _status = normalize_license(str(record.get("license_note") or ""))
    return label


def _matrix(records: list[dict], row_key: str, column_key: str) -> tuple[list[dict], list[str]]:
    columns = sorted({str(row.get(column_key) or "unknown") for row in records})
    values: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        values[str(record.get(row_key) or "unknown")][
            str(record.get(column_key) or "unknown")
        ] += 1
    rows = [{row_key: key, **{column: values[key][column] for column in columns}}
            for key in sorted(values)]
    return rows, [row_key, *columns]


def write_quantitative_analysis(records: list[dict], output_dir: Path,
                                *, source_roots: list[Path],
                                duplicate_records: int,
                                warnings: list[str]) -> dict:
    """在同一目录输出总览、分组统计、交叉矩阵及论文级标准化清单。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(records)
    present_statuses = {"verified_local_pdf", "local_pdf_present"}
    present = sum(str(row.get("consolidation_file_status") or "") in
                  present_statuses for row in records)
    sizes = [int(row.get("bytes") or 0) for row in records if int(row.get("bytes") or 0) > 0]
    dates = sorted(str(row.get("published_at")) for row in records
                   if str(row.get("published_at") or "").strip())
    counters = {
        "by_journal.csv": ("journal", Counter(str(r.get("journal") or "unknown") for r in records)),
        "by_discipline.csv": ("discipline", Counter(str(r.get("primary_discipline_name") or
                                                       r.get("primary_discipline") or "未分类") for r in records)),
        "by_publication_year.csv": ("publication_year", Counter(_date_bucket(r.get("published_at"), 4) for r in records)),
        "by_publication_month.csv": ("publication_month", Counter(_date_bucket(r.get("published_at"), 7) for r in records)),
        "by_publisher.csv": ("publisher", Counter(str(r.get("publisher") or "unknown") for r in records)),
        "by_oa_status.csv": ("oa_status", Counter(str(r.get("oa_status") or "unknown") for r in records)),
        "by_pdf_source.csv": ("oa_pdf_source", Counter(str(r.get("oa_pdf_source") or "unknown") for r in records)),
        "by_oa_version.csv": ("oa_version", Counter(str(r.get("oa_version") or "unknown") for r in records)),
        "by_license.csv": ("license", Counter(_license_bucket(r) for r in records)),
        "by_collector_version.csv": ("collector_version", Counter(str(r.get("collector_version") or "unknown") for r in records)),
        "by_discipline_origin.csv": ("discipline_origin", Counter(str(r.get("analysis_discipline_origin") or "unknown") for r in records)),
        "by_resource_kind.csv": ("resource_kind", Counter(str(r.get("resource_kind") or "unknown") for r in records)),
        "by_file_size_bucket.csv": ("file_size_bucket", Counter(_size_bucket(r.get("bytes")) for r in records)),
    }
    grouped: dict[str, list[dict]] = {}
    for filename, (key_name, counter) in counters.items():
        rows = _counter_rows(counter, key_name, total)
        grouped[filename.removesuffix(".csv")] = rows
        _write_csv(output_dir / filename, rows)

    journal_matrix, journal_fields = _matrix(records, "primary_discipline_name", "journal")
    _write_csv(output_dir / "discipline_journal_matrix.csv", journal_matrix, journal_fields)
    matrix_records = [{**record, "publication_year": _date_bucket(record.get("published_at"), 4)}
                      for record in records]
    year_matrix, year_fields = _matrix(
        matrix_records, "primary_discipline_name", "publication_year"
    )
    _write_csv(output_dir / "discipline_year_matrix.csv", year_matrix, year_fields)

    inventory_fields = [
        "title", "journal", "journal_issn", "primary_discipline",
        "primary_discipline_name", "analysis_discipline_origin", "published_at",
        "doi", "openalex_id", "publisher", "oa_status", "oa_pdf_source",
        "oa_version", "license_note", "license_verification", "bytes", "sha256",
        "file", "consolidation_file_status", "collector_version",
    ]
    _write_csv(output_dir / "papers_inventory.csv", records, inventory_fields)

    quality = [
        {"metric": "unique_papers", "count": total},
        {"metric": "duplicate_records_removed", "count": duplicate_records},
        {"metric": "local_pdf_verified", "count": present},
        {"metric": "local_pdf_not_verified_or_analysis_only", "count": total - present},
        {"metric": "missing_doi", "count": sum(not normalize_doi(str(r.get("doi") or "")) for r in records)},
        {"metric": "missing_publication_date", "count": sum(_date_bucket(r.get("published_at"), 4) == "unknown" for r in records)},
        {"metric": "unclassified_discipline", "count": sum(r.get("primary_discipline") == "unclassified" for r in records)},
        {"metric": "missing_license", "count": sum(str(r.get("license_note") or "").lower() in {"", "unknown", "check_article_specific_license"} for r in records)},
        {"metric": "warnings", "count": len(warnings)},
    ]
    for row in quality:
        row["percentage"] = round(100 * row["count"] / total, 2) if total else 0.0
    _write_csv(output_dir / "data_quality.csv", quality,
               ["metric", "count", "percentage"])

    summary = {
        "generated_at": _now_utc(),
        "source_roots": [path.name for path in source_roots],
        "unique_papers": total,
        "duplicate_records_removed": duplicate_records,
        "journal_count": len(counters["by_journal.csv"][1]),
        "discipline_count_including_unclassified": len(counters["by_discipline.csv"][1]),
        "publication_date_min": dates[0] if dates else "",
        "publication_date_max": dates[-1] if dates else "",
        "total_pdf_bytes_reported": sum(sizes),
        "mean_pdf_bytes": round(statistics.mean(sizes), 2) if sizes else 0,
        "median_pdf_bytes": round(statistics.median(sizes), 2) if sizes else 0,
        "local_pdf_verified": present,
        "analysis_only_or_unverified": total - present,
        "warnings": warnings,
        "tables": grouped,
    }
    (output_dir / "quantitative_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        "# 论文量化分析\n\n"
        f"生成时间：{summary['generated_at']}\n\n"
        f"输入目录：{', '.join(summary['source_roots'])}\n\n"
        f"- 去重后论文：{total}\n"
        f"- 合并重复记录：{duplicate_records}\n"
        f"- 期刊数：{summary['journal_count']}\n"
        f"- 已核验本地 PDF：{present}\n"
        f"- 仅元数据或未核验文件：{total - present}\n\n"
        "所有分组统计、交叉矩阵、质量指标和论文级清单均位于本目录。\n\n"
        + ("当前快照仅能核对 manifest 元数据，未在 Git 仓库中找到 PDF 实体；"
           "请在本地源数据目录重新运行工具以完成文件核验。\n"
           if present == 0 and total else ""),
        encoding="utf-8",
    )
    return summary


def cleanup_source_roots(source_roots: list[Path], dataset_root: Path,
                         canonical_root: Path) -> list[Path]:
    """仅删除数据根目录下的直接子目录，拒绝宽泛或越界路径。"""
    dataset_root = dataset_root.resolve()
    canonical_root = canonical_root.resolve()
    removed: list[Path] = []
    for source in source_roots:
        resolved = source.resolve()
        if resolved == canonical_root:
            continue
        if resolved.parent != dataset_root:
            raise RuntimeError(f"Refusing to delete non-direct dataset child: {resolved}")
        raw_root = resolved / "raw"
        if raw_root.is_dir():
            unexpected = [path for path in raw_root.iterdir() if path.name != "papers"]
            if unexpected:
                raise RuntimeError(
                    f"Refusing to delete source with non-paper raw data: {resolved}"
                )
        if resolved.is_dir():
            shutil.rmtree(resolved)
            removed.append(resolved)
    return removed
