"""命令行入口：解析参数、验证运行配置，并分派采集、审计和统计任务。"""
import argparse
import os
from pathlib import Path

from .analysis import (
    PaperEntry,
    classify_missing_disciplines,
    deduplicate_entries,
    write_quantitative_analysis,
)
from .client import Client, SourceError
from .constants import CATEGORY_CHOICES, ROOT
from .disciplines import load_disciplines, write_reports
from .feasibility import collect_papers
from .models import load_journals
from .papers import collect_papers_v6_4
from .policy import apply_journal_policies, load_journal_policies
from .reports import select_journals
from .resources import audit_existing, collect_other
from .runtime import RunLock, free_disk_gib, install_stop_event, write_runtime_state
from .storage import load_existing_records
from .utils import log_error

# 首先统一验证参数，再根据运行模式选择离线统计或联网采集。
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories", default="papers", help="papers,videos,teaching,methods,textbooks")
    parser.add_argument("--limit", type=int, default=100,
                        help="cumulative successful files for non-paper categories")
    parser.add_argument("--per-journal-target", type=int, default=10,
                        help="candidate-journal feasibility target; only used with --feasibility-only")
    parser.add_argument("--download", action="store_true", help="download originals (otherwise only preview)")
    parser.add_argument("--dry-run", action="store_true", help="preview eligible papers only")
    parser.add_argument("--out", type=Path, default=ROOT / "chemistry_dataset" / "papers")
    parser.add_argument(
        "--analysis-dir", type=Path,
        default=ROOT / "chemistry_dataset" / "quantitative_analysis",
        help="统一量化 CSV、论文清单与摘要的输出目录",
    )
    parser.add_argument("--journals-file", type=Path,
                        default=ROOT / "config" / "journals_v6_4.json")
    parser.add_argument("--disciplines-file", type=Path,
                        default=ROOT / "config" / "disciplines_v6_4.json")
    parser.add_argument(
        "--journal-policy-file",
        type=Path,
        default=ROOT / "config" / "journal_policy_v6_4.json",
        help="runtime priorities, attempt budgets and low-yield exclusions",
    )
    parser.add_argument("--journal-stage", choices=("validated", "candidate", "all"),
                        default="validated",
                        help="validated=34 proven journals; candidate=8 new OA journals; all=both")
    parser.add_argument("--feasibility-only", action="store_true",
                        help="use per-journal target instead of discipline quotas")
    parser.add_argument(
        "--include-low-yield-journals",
        action="store_true",
        help="显式重新启用运行策略中默认停用的低收益期刊",
    )
    parser.add_argument("--min-journals-per-discipline", type=int, default=3)
    parser.add_argument("--max-journal-share", type=float, default=0.40)
    parser.add_argument("--max-review-share", type=float, default=0.30)
    parser.add_argument(
        "--allow-preprint-fallback", action=argparse.BooleanOptionalAction,
        default=True,
        help=("allow DOI-linked arXiv/ChemRxiv/bioRxiv/medRxiv/Research Square "
              "copy after journal identity is verified"),
    )
    parser.add_argument(
        "--use-unpaywall", action=argparse.BooleanOptionalAction, default=True,
        help="augment OpenAlex locations via Unpaywall (requires --contact email)",
    )
    parser.add_argument(
        "--contact",
        default=os.environ.get("COLLECTOR_CONTACT_EMAIL", ""),
        help="polite contact email; defaults to COLLECTOR_CONTACT_EMAIL",
    )
    parser.add_argument(
        "--openalex-api-key",
        default=os.environ.get("OPENALEX_API_KEY", ""),
        help="OpenAlex API key; defaults to OPENALEX_API_KEY (never written to output)",
    )
    parser.add_argument("--max-file-mb", type=int, default=500)
    parser.add_argument("--max-pdf-mirrors", type=int, default=3)
    parser.add_argument("--max-pages-per-journal", type=int, default=8)
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--max-attempts", type=int, default=3000)
    parser.add_argument(
        "--max-total-papers", type=int, default=0,
        help=("累计有效论文总量目标；大于0时停用学科配额过滤，"
              "允许未分类论文，并在达到该总量后停止"),
    )
    parser.add_argument(
        "--domain-failure-threshold",
        type=int,
        default=5,
        help="open a host circuit after this many consecutive 401/403/429/robots failures",
    )
    parser.add_argument("--books-csv", type=Path, default=ROOT / "config" / "textbook_seeds.csv")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--stats-only", action="store_true",
                        help="不联网，重建 V6.4 配额状态与统一量化 CSV")
    parser.add_argument(
        "--continuous", action="store_true",
        help="服务器连续模式：每轮重新读取历史数据并提升平衡采集目标",
    )
    parser.add_argument("--cycle-interval-seconds", type=int, default=3600,
                        help="连续模式两轮之间的等待秒数，最少60秒")
    parser.add_argument("--quota-step", type=int, default=20,
                        help="连续模式每轮的全学科目标增长步长")
    parser.add_argument("--report-every", type=int, default=10,
                        help="每成功下载多少篇原子更新一次量化报告")
    parser.add_argument("--min-free-gb", type=float, default=5.0,
                        help="输出磁盘低于该可用空间时暂停新一轮采集")
    parser.add_argument("--max-cycles", type=int, default=0,
                        help="连续模式最多执行轮数；0表示持续运行")
    args = parser.parse_args()
    cats = [x.strip() for x in args.categories.split(",") if x.strip()]
    if not cats or not set(cats).issubset(CATEGORY_CHOICES):
        parser.error(f"Unknown categories; allowed: {sorted(CATEGORY_CHOICES)}")
    if any(x <= 0 for x in (args.limit, args.per_journal_target,
                             args.max_file_mb, args.max_pdf_mirrors,
                             args.max_pages_per_journal, args.per_page, args.max_attempts,
                             args.min_journals_per_discipline,
                             args.domain_failure_threshold, args.quota_step,
                             args.report_every)):
        parser.error("Numeric limits must all be greater than zero")
    if args.cycle_interval_seconds < 60:
        parser.error("--cycle-interval-seconds must be at least 60")
    if args.min_free_gb < 0 or args.max_cycles < 0 or args.max_total_papers < 0:
        parser.error(
            "--min-free-gb, --max-cycles and --max-total-papers cannot be negative"
        )
    if not (0 < args.max_journal_share <= 1):
        parser.error("--max-journal-share must be in (0, 1]")
    if not (0 <= args.max_review_share <= 1):
        parser.error("--max-review-share must be in [0, 1]")
    if args.per_page > 100:
        parser.error("OpenAlex --per-page must be <= 100")
    if args.continuous and (
        cats != ["papers"] or not args.download or args.dry_run
        or args.feasibility_only or args.audit_only or args.stats_only
    ):
        parser.error(
            "--continuous requires only --categories papers and --download; "
            "it cannot be combined with preview, feasibility, audit or stats modes"
        )
    if args.max_total_papers and (
        cats != ["papers"] or args.feasibility_only
        or args.audit_only or args.stats_only
    ):
        parser.error(
            "--max-total-papers requires normal papers collection mode"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    # 在发起网络请求前校验期刊白名单，尽早暴露配置错误。
    journals = load_journals(args.journals_file) if "papers" in cats else []
    if args.stats_only:
        if "papers" not in cats:
            parser.error("--stats-only requires --categories papers")
        _selected, skipped = apply_journal_policies(
            select_journals(journals, args.journal_stage),
            load_journal_policies(args.journal_policy_file),
            args.include_low_yield_journals,
        )
        for journal, policy in skipped:
            print(f"[POLICY-SKIP] {journal.name}: {policy.reason}", flush=True)
        disciplines = load_disciplines(args.disciplines_file)
        loaded_records = load_existing_records(args.out, category="papers")
        unique_entries, duplicate_records = deduplicate_entries([
            PaperEntry(
                record, args.out,
                args.out / Path(str(record.get("file") or "")),
            )
            for record in loaded_records
        ])
        records = classify_missing_disciplines(
            [entry.record for entry in unique_entries], disciplines
        )
        for record in records:
            record.setdefault("consolidation_file_status", "local_pdf_present")
        report = write_reports(
            args.out, records, disciplines, args.min_journals_per_discipline,
            args.max_journal_share, args.max_review_share, "local_audit",
        )
        write_quantitative_analysis(
            records, args.analysis_dir, source_roots=[args.out],
            duplicate_records=duplicate_records, warnings=[], disciplines=disciplines,
            min_journals=args.min_journals_per_discipline,
            max_journal_share=args.max_journal_share,
            max_review_share=args.max_review_share,
        )
        print(
            f"[V6.4] disciplines_met={report['disciplines_meeting_target']}/"
            f"{report['discipline_count']} counted={report['total_counted_papers']}"
        )
        return
    if args.audit_only:
        audit_existing(args.out, cats, journals)
        return
    def run_once() -> dict | None:
        client = Client(
            args.contact,
            openalex_api_key=args.openalex_api_key,
            domain_failure_threshold=args.domain_failure_threshold,
        )
        result = None
        for category in cats:
            target_label = (
                f"per_journal_target={args.per_journal_target}"
                if category == "papers" and args.feasibility_only
                else (
                    f"max_total_papers={args.max_total_papers}"
                    if category == "papers" and args.max_total_papers
                    else ("discipline_quotas" if category == "papers"
                          else f"target={args.limit}")
                )
            )
            print(f"\n=== V6.4 {category} {target_label} ===", flush=True)
            if category == "papers":
                if args.feasibility_only:
                    collect_papers(client, args, args.out)
                else:
                    result = collect_papers_v6_4(client, args, args.out)
            else:
                collect_other(client, args, args.out, category)
        return result

    if not args.continuous:
        try:
            if "papers" in cats and args.download:
                with RunLock(args.out / "collector_v6_4.lock"):
                    run_once()
            else:
                run_once()
        except (SourceError, RuntimeError, OSError, ValueError) as exc:
            log_error(args.out / "errors.jsonl", "runtime", str(exc), phase="collection")
            print(f"[ERROR] {exc}", flush=True)
            raise
        return

    stop_event = install_stop_event()
    args.stop_event = stop_event
    state_path = args.out / "runtime_state_v6_4.json"
    lock_path = args.out / "collector_v6_4.lock"
    cycle = 0
    consecutive_failures = 0
    total_target_reached = False
    last_report: dict = {}
    with RunLock(lock_path):
        write_runtime_state(state_path, status="starting", cycle=cycle)
        while not stop_event.is_set():
            available_gib = free_disk_gib(args.out)
            if available_gib < args.min_free_gb:
                delay = args.cycle_interval_seconds
                write_runtime_state(
                    state_path, status="paused_low_disk", cycle=cycle,
                    free_disk_gib=round(available_gib, 3),
                    required_free_gib=args.min_free_gb,
                    next_attempt_in_seconds=delay,
                )
                print(
                    f"[PAUSE] 可用磁盘仅 {available_gib:.2f} GiB；"
                    f"要求至少 {args.min_free_gb:.2f} GiB。",
                    flush=True,
                )
                stop_event.wait(delay)
                continue

            cycle += 1
            write_runtime_state(
                state_path, status="running", cycle=cycle,
                free_disk_gib=round(available_gib, 3),
            )
            try:
                report = run_once() or {}
                last_report = report
            except (SourceError, RuntimeError, OSError, ValueError) as exc:
                consecutive_failures += 1
                delay = min(
                    args.cycle_interval_seconds * (2 ** min(consecutive_failures, 4)),
                    args.cycle_interval_seconds * 16,
                )
                log_error(
                    args.out / "errors.jsonl", "runtime", str(exc),
                    phase="continuous_cycle", cycle=cycle,
                )
                write_runtime_state(
                    state_path, status="retry_wait", cycle=cycle,
                    consecutive_failures=consecutive_failures,
                    last_error=str(exc), next_attempt_in_seconds=delay,
                )
                print(f"[RETRY] 第 {cycle} 轮失败：{exc}；{delay} 秒后重试。", flush=True)
            else:
                consecutive_failures = 0
                delay = args.cycle_interval_seconds
                total_target_reached = bool(report.get("total_target_reached"))
                write_runtime_state(
                    state_path,
                    status=("target_reached" if total_target_reached else "sleeping"),
                    cycle=cycle,
                    new_downloads=report.get("new_downloads", 0),
                    total_papers=report.get("total_papers", 0),
                    max_total_papers=report.get("max_total_papers", 0),
                    disciplines_meeting_target=report.get(
                        "disciplines_meeting_target", 0
                    ),
                    discipline_count=report.get("discipline_count", 0),
                    next_attempt_in_seconds=(0 if total_target_reached else delay),
                )
            if total_target_reached:
                break
            if args.max_cycles and cycle >= args.max_cycles:
                break
            stop_event.wait(delay)
        write_runtime_state(
            state_path, status="stopped", cycle=cycle,
            stop_reason=(
                "signal" if stop_event.is_set()
                else "max_total_papers" if total_target_reached
                else "max_cycles"
            ),
            total_papers=last_report.get("total_papers", 0),
            max_total_papers=last_report.get("max_total_papers", 0),
        )

