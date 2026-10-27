"""逐期刊开放获取可行性测试流程，兼容早期按期刊计数的运行方式。"""
from pathlib import Path
from typing import Iterator

import requests

from .client import Client, SourceError
from .constants import COLLECTOR_VERSION
from .discovery import journal_article_iterator, unpaywall_pdf_locations
from .disciplines import load_disciplines, merge_locations
from .models import Journal, load_journals
from .policy import apply_journal_policies, load_journal_policies, policy_for
from .reports import (
    print_journal_stats, refresh_journal_stats, save_feasibility_report,
    save_journal_stats, select_journals,
)
from .storage import (
    download_file, identity_keys, load_existing_records, paper_audit,
)
from .utils import append_jsonl, log_error

# 可行性测试以每刊目标篇数为准，与正式的按学科配额模式分离。
def collect_papers(client: Client, args, root: Path) -> None:
    stage_journals = select_journals(
        load_journals(args.journals_file), getattr(args, "journal_stage", "all")
    )
    policies = load_journal_policies(getattr(args, "journal_policy_file", None))
    journals, skipped = apply_journal_policies(
        stage_journals,
        policies,
        getattr(args, "include_low_yield_journals", False),
    )
    for journal, policy in skipped:
        print(f"[POLICY-SKIP] {journal.name}: {policy.reason}", flush=True)
    if not journals:
        raise ValueError("Journal runtime policy disabled every selected journal")
    disciplines = (
        load_disciplines(args.disciplines_file)
        if getattr(args, "disciplines_file", None) else None
    )
    allowed = {journal.name for journal in journals}
    all_history = load_existing_records(root, category="papers")
    old = [row for row in all_history if row.get("journal") in allowed]
    seen_keys = set()
    seen_sha = set()
    for row in all_history:
        seen_keys.update(identity_keys(row))
        seen_sha.add(str(row["sha256"]).lower())
    per_journal_target = args.per_journal_target
    counts = save_journal_stats(root, journals, old, per_journal_target)
    total = sum(counts.values())
    print(
        f"[V{COLLECTOR_VERSION}] {len(journals)} whitelist journals; old_valid={total}; "
        f"per_journal_target={per_journal_target}",
        flush=True,
    )
    if all(count >= per_journal_target for count in counts.values()) and not args.dry_run:
        print(f"[V{COLLECTOR_VERSION}] Every journal target already reached; nothing to download.", flush=True)
        states = [{"journal": journal, "tried": 0, "previewed": 0,
                   "download_attempts": 0, "success": 0, "error": ""}
                  for journal in journals]
        save_feasibility_report(root, states, counts, per_journal_target, False)
        print_journal_stats(counts)
        return

    errors = root / "errors.jsonl"
    version_suffix = COLLECTOR_VERSION.replace(".", "_")
    candidates_csv = root / f"papers_v{version_suffix}_candidates.csv"
    excluded_csv = root / f"papers_v{version_suffix}_filtered_out.csv"
    # 兼容未启用学科分类及预印本兜底的旧版测试调用。
    def legacy_iterator(journal: Journal) -> Iterator[dict]:
        if disciplines is None and not getattr(args, "allow_preprint_fallback", False):
            return journal_article_iterator(
                client, journal, args.per_page, args.max_pages_per_journal, errors
            )
        return journal_article_iterator(
            client, journal, args.per_page, args.max_pages_per_journal, errors,
            disciplines=disciplines,
            allow_preprints=getattr(args, "allow_preprint_fallback", False),
        )

    states = [
        {"journal": j, "iterator": iter(legacy_iterator(j)),
         "active": args.dry_run or counts[j.name] < per_journal_target,
         "success": 0, "tried": 0, "previewed": 0,
         "download_attempts": 0,
         "max_download_attempts": policy_for(policies, j).max_download_attempts,
         "error": ""}
        for j in journals
    ]
    successes = 0
    tried = 0
    previewed = 0

    # 模拟预览和真实下载采用不同的期刊达标判断口径。
    def all_targets_met() -> bool:
        if args.dry_run or not args.download:
            return all(state["previewed"] >= per_journal_target for state in states)
        return all(counts[state["journal"].name] >= per_journal_target for state in states)

    while tried < args.max_attempts and not all_targets_met():
        made_progress = False
        for state in states:
            if not state["active"]:
                continue
            journal = state["journal"]
            if (
                args.download
                and state["download_attempts"] >= state["max_download_attempts"]
            ):
                state["active"] = False
                state["error"] = "journal download-attempt budget exhausted"
                print(
                    f"[POLICY-STOP] {journal.name}: "
                    f"{state['download_attempts']} PDF attempts",
                    flush=True,
                )
                continue
            if ((args.dry_run or not args.download)
                    and state["previewed"] >= per_journal_target):
                state["active"] = False
                continue
            if (not args.dry_run and args.download
                    and counts[journal.name] >= per_journal_target):
                state["active"] = False
                continue
            try:
                item = next(state["iterator"])
            except StopIteration:
                state["active"] = False
                continue
            except (SourceError, requests.RequestException, ValueError, KeyError) as exc:
                state["active"] = False
                state["error"] = str(exc)
                print(f"[WARN] {journal.name} discovery stopped: {exc}", flush=True)
                log_error(errors, "papers", str(exc), journal=journal.name, phase="discovery")
                continue
            made_progress = True
            tried += 1
            state["tried"] += 1
            matching = seen_keys.intersection(identity_keys(item))
            if matching:
                paper_audit(excluded_csv, item, "duplicate_metadata", sorted(matching)[0])
                continue
            if getattr(args, "use_unpaywall", False):
                item["pdf_locations"] = merge_locations(
                    item["pdf_locations"],
                    unpaywall_pdf_locations(
                        client, item["doi"], journal,
                        allow_preprints=getattr(args, "allow_preprint_fallback", False),
                    ),
                )
            if not item.get("pdf_locations"):
                paper_audit(
                    excluded_csv, item, "no_oa_pdf_after_fallback",
                    "OpenAlex and Unpaywall returned no acceptable HTTPS PDF",
                )
                continue
            if args.dry_run or not args.download:
                previewed += 1
                state["previewed"] += 1
                seen_keys.update(identity_keys(item))
                paper_audit(candidates_csv, item, "eligible_not_downloaded")
                print(
                    f"[PREVIEW] {journal.name} "
                    f"{state['previewed']}/{per_journal_target}: {item['title'][:75]}"
                )
                continue
            downloaded = False
            last_error = "No candidate PDF succeeded"
            for location in item["pdf_locations"][:args.max_pdf_mirrors]:
                if state["download_attempts"] >= state["max_download_attempts"]:
                    last_error = "journal download-attempt budget exhausted"
                    state["active"] = False
                    break
                url = location["url"]
                state["download_attempts"] += 1
                record_item = {**item,
                               "oa_version": location["version"],
                               "oa_pdf_host": location["oa_host"],
                               "oa_pdf_source_page": location["landing_page"],
                               "oa_pdf_source": location.get("source_type", "publisher" if location["publisher_pdf"] else "OA_repository"),
                               "oa_metadata_provider": location.get("metadata_provider", "openalex"),
                               "source_fallback_rank": location.get("fallback_rank", 1),
                               "oa_verification_level": "openalex_journal_identity+oa_location+file_signature",
                               "license_note": location["license"] or "CHECK_ARTICLE_SPECIFIC_LICENSE",
                               "license_verification": location.get("license_status", "manual_review_required"),
                               "download_url": url}
                try:
                    state_str, record = download_file(
                        client, record_item, root, url=url, ext=".pdf",
                        max_file_mb=args.max_file_mb, seen_sha=seen_sha)
                    if state_str == "sha_duplicate":
                        paper_audit(excluded_csv, record_item, "duplicate_sha256", "Identical PDF bytes already saved")
                        downloaded = True  # 已是重复论文，无需继续尝试其他下载位置
                        break
                    assert record is not None
                    append_jsonl(root / "manifest.jsonl", record)
                    seen_keys.update(identity_keys(record))
                    seen_sha.add(record["sha256"])
                    total += 1
                    successes += 1
                    state["success"] += 1
                    counts[journal.name] += 1
                    old.append(record)
                    save_journal_stats(root, journals, old, per_journal_target)
                    print(f"[JOURNAL STATS] {journal.name}={counts[journal.name]} | TOTAL={total}", flush=True)
                    paper_audit(candidates_csv, record, "downloaded")
                    print(
                        f"[papers] {journal.name} {counts[journal.name]}/{per_journal_target} "
                        f"| total={total} | {record['title'][:78]}",
                        flush=True,
                    )
                    downloaded = True
                    break
                except (SourceError, requests.RequestException, OSError, ValueError) as exc:
                    last_error = str(exc)
                    log_error(errors, "papers", last_error, journal=journal.name,
                              doi=item["doi"], download_url=url, phase="download")
                    print(f"[SKIP] {journal.name}: {last_error}", flush=True)
            if not downloaded:
                paper_audit(excluded_csv, item, "download_failed", last_error)
        if not made_progress:
            break
    counts = refresh_journal_stats(root, journals, per_journal_target)
    save_feasibility_report(root, states, counts, per_journal_target,
                            args.dry_run or not args.download)
    print_journal_stats(counts)
    print(
        f"[V{COLLECTOR_VERSION}] valid_pdf_total={sum(counts.values())} "
        f"new_downloads={successes} previewed={previewed}",
        flush=True,
    )
    for state in states:
        print(
            f"  {state['journal'].name}: tried={state['tried']} "
            f"previewed={state['previewed']} downloaded={state['success']} "
            f"valid_local={counts[state['journal'].name]}"
        )
    unmet = [name for name, count in counts.items() if count < per_journal_target]
    if not args.dry_run and args.download and unmet:
        print(
            f"[WARN] Per-journal target not reached for {len(unmet)} journals: "
            + ", ".join(unmet)
            + ". No PDF count was fabricated.",
            flush=True,
        )
    preview_unmet = [state["journal"].name for state in states
                     if state["previewed"] < per_journal_target]
    if (args.dry_run or not args.download) and preview_unmet:
        print(
            f"[WARN] Metadata feasibility target not reached for {len(preview_unmet)} journals: "
            + ", ".join(preview_unmet),
            flush=True,
        )

