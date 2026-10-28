#!/usr/bin/env python3
"""合并本地论文目录，并将全部量化统计写入一个分析目录。"""
import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chemistry_collector.analysis import (
    classify_missing_disciplines,
    cleanup_source_roots,
    consolidate_entries,
    deduplicate_entries,
    discover_paper_roots,
    load_paper_entries,
    write_quantitative_analysis,
)
from chemistry_collector.disciplines import load_disciplines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path,
                        default=ROOT / "chemistry_dataset")
    parser.add_argument("--canonical-root", type=Path,
                        default=ROOT / "chemistry_dataset" / "papers")
    parser.add_argument("--analysis-dir", type=Path,
                        default=ROOT / "chemistry_dataset" / "quantitative_analysis")
    parser.add_argument("--disciplines-file", type=Path,
                        default=ROOT / "config" / "disciplines_v6_4.json")
    parser.add_argument("--inputs", type=Path, nargs="*",
                        help="source dataset folders; defaults to automatic discovery")
    parser.add_argument("--consolidate", action="store_true",
                        help="copy verified PDFs and write the canonical manifest")
    parser.add_argument("--move-files", action="store_true",
                        help="move instead of copy; requires --consolidate")
    parser.add_argument("--cleanup-sources", action="store_true",
                        help="delete source folders after successful consolidation")
    parser.add_argument("--yes", action="store_true",
                        help="confirm destructive --cleanup-sources operation")
    args = parser.parse_args()

    if args.move_files and not args.consolidate:
        parser.error("--move-files requires --consolidate")
    if args.cleanup_sources and not (args.consolidate and args.yes):
        parser.error("--cleanup-sources requires --consolidate --yes")

    source_roots = [path.resolve() for path in args.inputs] if args.inputs else (
        discover_paper_roots(args.dataset_root, args.canonical_root)
    )
    if not source_roots:
        parser.error("No paper dataset folders were found")
    entries, warnings = load_paper_entries(source_roots)
    unique, duplicate_count = deduplicate_entries(entries)
    disciplines = load_disciplines(args.disciplines_file)
    if args.consolidate:
        records, consolidation = consolidate_entries(
            unique, args.canonical_root, move_files=args.move_files,
            disciplines=disciplines,
        )
    else:
        records = []
        for entry in unique:
            record = dict(entry.record)
            record["consolidation_file_status"] = (
                "local_pdf_present"
                if entry.source_file and entry.source_file.is_file()
                else "source_file_missing"
            )
            records.append(record)
        consolidation = {"copied": 0, "moved": 0, "missing": sum(
            not entry.source_file or not entry.source_file.is_file()
            for entry in unique
        )}

    records = classify_missing_disciplines(records, disciplines)
    summary = write_quantitative_analysis(
        records, args.analysis_dir, source_roots=source_roots,
        duplicate_records=duplicate_count, warnings=warnings,
        disciplines=disciplines,
    )
    removed = []
    if args.cleanup_sources:
        removed = cleanup_source_roots(
            source_roots, args.dataset_root, args.canonical_root
        )
    print(
        f"[ANALYSIS] papers={summary['unique_papers']} "
        f"duplicates={duplicate_count} journals={summary['journal_count']}"
    )
    print(
        f"[CONSOLIDATE] copied={consolidation['copied']} "
        f"moved={consolidation['moved']} missing={consolidation['missing']}"
    )
    print(f"[OUTPUT] {args.analysis_dir.resolve()}")
    if removed:
        print("[REMOVED] " + ", ".join(str(path) for path in removed))


if __name__ == "__main__":
    main()
