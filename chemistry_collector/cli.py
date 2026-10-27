"""命令行入口：解析参数、验证运行配置，并分派采集、审计和统计任务。"""
import argparse
import os
from pathlib import Path

from .client import Client, SourceError
from .constants import CATEGORY_CHOICES, ROOT
from .disciplines import load_disciplines, write_reports as write_v6_3_reports
from .feasibility import collect_papers
from .models import load_journals
from .papers import collect_papers_v6_3
from .policy import apply_journal_policies, load_journal_policies
from .reports import select_journals
from .resources import audit_existing, collect_other
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
    parser.add_argument("--out", type=Path, default=ROOT / "chemistry_dataset" / "v6_3_papers")
    parser.add_argument("--journals-file", type=Path,
                        default=ROOT / "config" / "journals_v6_3.json")
    parser.add_argument("--disciplines-file", type=Path,
                        default=ROOT / "config" / "disciplines_v6_3.json")
    parser.add_argument(
        "--journal-policy-file",
        type=Path,
        default=ROOT / "config" / "journal_policy_v6_3.json",
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
        help="explicitly re-enable journals disabled by the V6.3 runtime policy",
    )
    parser.add_argument("--min-journals-per-discipline", type=int, default=3)
    parser.add_argument("--max-journal-share", type=float, default=0.40)
    parser.add_argument("--max-review-share", type=float, default=0.30)
    parser.add_argument(
        "--allow-preprint-fallback", action=argparse.BooleanOptionalAction,
        default=True,
        help="allow DOI-linked arXiv/ChemRxiv copy after journal identity is verified",
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
        "--domain-failure-threshold",
        type=int,
        default=5,
        help="open a host circuit after this many consecutive 401/403/429/robots failures",
    )
    parser.add_argument("--books-csv", type=Path, default=ROOT / "config" / "textbook_seeds.csv")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--stats-only", action="store_true",
                        help="rebuild V6.3 discipline/source reports without network requests")
    args = parser.parse_args()
    cats = [x.strip() for x in args.categories.split(",") if x.strip()]
    if not cats or not set(cats).issubset(CATEGORY_CHOICES):
        parser.error(f"Unknown categories; allowed: {sorted(CATEGORY_CHOICES)}")
    if any(x <= 0 for x in (args.limit, args.per_journal_target,
                             args.max_file_mb, args.max_pdf_mirrors,
                             args.max_pages_per_journal, args.per_page, args.max_attempts,
                             args.min_journals_per_discipline,
                             args.domain_failure_threshold)):
        parser.error("Numeric limits must all be greater than zero")
    if not (0 < args.max_journal_share <= 1):
        parser.error("--max-journal-share must be in (0, 1]")
    if not (0 <= args.max_review_share <= 1):
        parser.error("--max-review-share must be in [0, 1]")
    if args.per_page > 100:
        parser.error("OpenAlex --per-page must be <= 100")
    args.out.mkdir(parents=True, exist_ok=True)
    # 在发起网络请求前校验期刊白名单，尽早暴露配置错误。
    journals = load_journals(args.journals_file) if "papers" in cats else []
    if args.stats_only:
        if "papers" not in cats:
            parser.error("--stats-only requires --categories papers")
        selected, skipped = apply_journal_policies(
            select_journals(journals, args.journal_stage),
            load_journal_policies(args.journal_policy_file),
            args.include_low_yield_journals,
        )
        for journal, policy in skipped:
            print(f"[POLICY-SKIP] {journal.name}: {policy.reason}", flush=True)
        disciplines = load_disciplines(args.disciplines_file)
        records = [
            row for row in load_existing_records(args.out, category="papers")
            if row.get("journal") in {journal.name for journal in selected}
        ]
        report = write_v6_3_reports(
            args.out, records, disciplines, args.min_journals_per_discipline,
            args.max_journal_share, args.max_review_share, "local_audit",
        )
        print(
            f"[V6.3] disciplines_met={report['disciplines_meeting_target']}/"
            f"{report['discipline_count']} counted={report['total_counted_papers']}"
        )
        return
    if args.audit_only:
        audit_existing(args.out, cats, journals)
        return
    client = Client(
        args.contact,
        openalex_api_key=args.openalex_api_key,
        domain_failure_threshold=args.domain_failure_threshold,
    )
    for category in cats:
        target_label = (
            f"per_journal_target={args.per_journal_target}"
            if category == "papers" and args.feasibility_only
            else ("discipline_quotas" if category == "papers" else f"target={args.limit}")
        )
        print(f"\n=== V6.3 {category} {target_label} ===", flush=True)
        try:
            if category == "papers":
                if args.feasibility_only:
                    collect_papers(client, args, args.out)
                else:
                    collect_papers_v6_3(client, args, args.out)
            else:
                collect_other(client, args, args.out, category)
        except (SourceError, RuntimeError, OSError, ValueError) as exc:
            log_error(args.out / "errors.jsonl", category, str(exc), phase="collection")
            print(f"[ERROR] {category}: {exc}", flush=True)
            raise

