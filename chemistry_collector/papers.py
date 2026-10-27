"""按化学学科配额采集论文：调度期刊、尝试开放获取副本并持续更新统计。"""
from pathlib import Path

import requests

from .client import Client, SourceError
from .discovery import journal_article_iterator, unpaywall_pdf_locations
from .disciplines import (
    all_targets_met as discipline_targets_met,
    load_disciplines, merge_locations, quota_decision, quota_snapshot,
    write_reports as write_v6_3_reports,
)
from .models import load_journals
from .policy import apply_journal_policies, load_journal_policies, policy_for
from .reports import select_journals
from .storage import download_file, identity_keys, load_existing_records, paper_audit
from .utils import append_jsonl, log_error

# 以学科缺口为停止条件，期刊仅作为论文来源与溯源维度。
def collect_papers_v6_3(client: Client, args, root: Path) -> None:
    """按主学科配额采集论文；期刊仅是来源，不设置平均采集配额。"""
    # 根据来源阶段和低收益策略筛选实际参与调度的期刊。
    all_journals = load_journals(args.journals_file)
    stage_journals = select_journals(all_journals, args.journal_stage)
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
    disciplines = load_disciplines(args.disciplines_file)
    discipline_map = {discipline.id: discipline for discipline in disciplines}
    allowed = {journal.name for journal in journals}
    history = load_existing_records(root, category="papers")
    quota_records = [
        record for record in history
        if record.get("journal") in allowed
        and record.get("primary_discipline") in discipline_map
    ]
    ignored_legacy = sum(
        record.get("journal") in allowed
        and record.get("primary_discipline") not in discipline_map
        for record in history
    )
    if ignored_legacy:
        print(
            f"[WARN] {ignored_legacy} legacy papers have no V6.3 primary discipline "
            "and are not counted toward quotas.",
            flush=True,
        )

    seen_keys: set[str] = set()
    seen_sha: set[str] = set()
    for record in history:
        seen_keys.update(identity_keys(record))
        seen_sha.add(str(record.get("sha256") or "").lower())

    # 先根据有效历史记录恢复学科配额，避免重复下载。
    snapshot = quota_snapshot(quota_records, disciplines)
    mode = "metadata_preview" if args.dry_run or not args.download else "pdf_download"
    write_v6_3_reports(
        root, quota_records, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share, mode,
    )
    print(
        f"[V6.3] journals={len(journals)} stage={args.journal_stage}; "
        f"disciplines={len(disciplines)}; counted={sum(snapshot['counts'].values())}",
        flush=True,
    )
    if discipline_targets_met(
        snapshot, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share,
    ):
        print("[V6.3] Every discipline quota already meets all constraints.", flush=True)
        return

    errors = root / "errors.jsonl"
    candidates_csv = root / "papers_v6_3_candidates.csv"
    excluded_csv = root / "papers_v6_3_filtered_out.csv"
    states = [{
        "journal": journal,
        "iterator": iter(journal_article_iterator(
            client, journal, args.per_page, args.max_pages_per_journal, errors,
            disciplines=disciplines,
            allow_preprints=args.allow_preprint_fallback,
        )),
        "active": True,
        "tried": 0,
        "accepted": 0,
        "download_attempts": 0,
        "max_download_attempts": policy_for(
            policies, journal
        ).max_download_attempts,
        "downloaded": 0,
        "error": "",
    } for journal in journals]
    tried = 0
    previewed = 0
    successes = 0

    # 轮询各期刊，保证下载请求受全局次数和单刊预算双重限制。
    while tried < args.max_attempts and not discipline_targets_met(
        snapshot, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share,
    ):
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
            try:
                item = next(state["iterator"])
            except StopIteration:
                state["active"] = False
                continue
            except (SourceError, requests.RequestException, ValueError, KeyError) as exc:
                state["active"] = False
                state["error"] = str(exc)
                log_error(errors, "papers", str(exc), journal=journal.name, phase="discovery")
                print(f"[WARN] {journal.name} discovery stopped: {exc}", flush=True)
                continue
            made_progress = True
            tried += 1
            state["tried"] += 1

            matching = seen_keys.intersection(identity_keys(item))
            if matching:
                paper_audit(excluded_csv, item, "duplicate_metadata", sorted(matching)[0])
                continue
            eligible, reason = quota_decision(
                item, snapshot, discipline_map,
                args.max_journal_share, args.max_review_share,
                args.min_journals_per_discipline,
            )
            if not eligible:
                paper_audit(excluded_csv, item, "discipline_quota_rejected", reason)
                continue

            # 合并多个开放获取元数据来源，并按候选位置优先级依次尝试。
            if args.use_unpaywall:
                item["pdf_locations"] = merge_locations(
                    item.get("pdf_locations") or [],
                    unpaywall_pdf_locations(
                        client, item["doi"], journal,
                        allow_preprints=args.allow_preprint_fallback,
                    ),
                )
            if not item.get("pdf_locations"):
                paper_audit(
                    excluded_csv, item, "no_oa_pdf_after_fallback",
                    "OpenAlex and Unpaywall returned no acceptable HTTPS PDF",
                )
                continue

            state["accepted"] += 1
            fallback_chain = [{
                "rank": location.get("fallback_rank"),
                "metadata_provider": location.get("metadata_provider"),
                "source_type": location.get("source_type"),
                "version": location.get("version"),
                "host": location.get("oa_host"),
                "license": location.get("license"),
                "url": location.get("url"),
            } for location in item["pdf_locations"][:args.max_pdf_mirrors]]

            if args.dry_run or not args.download:
                preview = {
                    **{key: value for key, value in item.items() if key != "pdf_locations"},
                    "status": "eligible_not_downloaded",
                    "oa_fallback_chain": fallback_chain,
                    "oa_pdf_source": item["pdf_locations"][0].get("source_type"),
                    "oa_metadata_provider": item["pdf_locations"][0].get("metadata_provider"),
                    "license_note": item["pdf_locations"][0].get("license"),
                    "license_verification": item["pdf_locations"][0].get("license_status"),
                }
                quota_records.append(preview)
                snapshot = quota_snapshot(quota_records, disciplines)
                seen_keys.update(identity_keys(item))
                previewed += 1
                paper_audit(candidates_csv, preview, "eligible_not_downloaded")
                print(
                    f"[PREVIEW] {item['primary_discipline_name']} "
                    f"{snapshot['counts'][item['primary_discipline']]}/"
                    f"{discipline_map[item['primary_discipline']].target} | "
                    f"{journal.name} | {item['title'][:70]}",
                    flush=True,
                )
                continue

            downloaded = False
            last_error = "No OA fallback location succeeded"
            # 同一论文最多尝试指定数量的合法公开全文地址。
            for location in item["pdf_locations"][:args.max_pdf_mirrors]:
                if state["download_attempts"] >= state["max_download_attempts"]:
                    last_error = "journal download-attempt budget exhausted"
                    state["active"] = False
                    break
                url = location["url"]
                state["download_attempts"] += 1
                record_item = {
                    **item,
                    "oa_version": location["version"],
                    "oa_pdf_host": location["oa_host"],
                    "oa_pdf_source_page": location["landing_page"],
                    "oa_pdf_source": location["source_type"],
                    "oa_metadata_provider": location["metadata_provider"],
                    "source_fallback_rank": location["fallback_rank"],
                    "oa_fallback_chain": fallback_chain,
                    "oa_verification_level": "issn_journal_identity+oa_resolver+file_signature",
                    "license_note": location["license"],
                    "license_verification": location["license_status"],
                    "download_url": url,
                }
                try:
                    status, record = download_file(
                        client, record_item, root, url=url, ext=".pdf",
                        max_file_mb=args.max_file_mb, seen_sha=seen_sha,
                    )
                    if status == "sha_duplicate":
                        paper_audit(
                            excluded_csv, record_item, "duplicate_sha256",
                            "Identical PDF bytes already saved",
                        )
                        downloaded = True
                        break
                    assert record is not None
                    append_jsonl(root / "manifest.jsonl", record)
                    seen_keys.update(identity_keys(record))
                    seen_sha.add(record["sha256"])
                    quota_records.append(record)
                    snapshot = quota_snapshot(quota_records, disciplines)
                    successes += 1
                    state["downloaded"] += 1
                    write_v6_3_reports(
                        root, quota_records, disciplines,
                        args.min_journals_per_discipline,
                        args.max_journal_share, args.max_review_share, mode,
                    )
                    paper_audit(candidates_csv, record, "downloaded")
                    print(
                        f"[papers] {item['primary_discipline_name']} "
                        f"{snapshot['counts'][item['primary_discipline']]}/"
                        f"{discipline_map[item['primary_discipline']].target} | "
                        f"{journal.name} | source={location['source_type']} | "
                        f"{record['title'][:58]}",
                        flush=True,
                    )
                    downloaded = True
                    break
                except (SourceError, requests.RequestException, OSError, ValueError) as exc:
                    last_error = str(exc)
                    log_error(
                        errors, "papers", last_error, journal=journal.name,
                        doi=item["doi"], download_url=url,
                        source_type=location.get("source_type"), phase="download",
                    )
                    print(
                        f"[FALLBACK] {journal.name} {location['source_type']}: {last_error}",
                        flush=True,
                    )
            if not downloaded:
                paper_audit(excluded_csv, item, "download_failed", last_error)
        if not made_progress:
            break

    report = write_v6_3_reports(
        root, quota_records, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share, mode,
    )
    print(
        f"[V6.3] tried={tried} previewed={previewed} new_downloads={successes}; "
        f"disciplines_met={report['disciplines_meeting_target']}/{len(disciplines)}",
        flush=True,
    )
    for row in report["disciplines"]:
        print(
            f"  {row['discipline']}: {row['count']}/{row['target']} "
            f"gap={row['gap']} journals={row['unique_journals']} "
            f"target_met={row['target_met']}",
            flush=True,
        )
    if not report["all_disciplines_meet_target"]:
        print(
            "[WARN] Some discipline quotas remain unmet. See "
            "discipline_stats_v6_3.json; no count was fabricated.",
            flush=True,
        )

