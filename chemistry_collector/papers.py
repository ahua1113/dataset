"""按学科配额或论文总量采集：调度期刊、下载开放全文并更新统计。"""
from dataclasses import replace
from pathlib import Path

import requests

from .client import Client, SourceError
from .analysis import (
    PaperEntry,
    classify_missing_disciplines,
    deduplicate_entries,
    write_quantitative_analysis,
)
from .discovery import journal_article_iterator, unpaywall_pdf_locations
from .disciplines import (
    all_targets_met as discipline_targets_met,
    load_disciplines, merge_locations, quota_decision, quota_snapshot,
    write_reports,
)
from .models import load_journals
from .policy import apply_journal_policies, load_journal_policies, policy_for
from .reports import select_journals
from .runtime import free_disk_gib
from .storage import download_file, identity_keys, load_existing_records, paper_audit
from .utils import append_jsonl, log_error


def _max_total_papers(args) -> int:
    """Return the cumulative paper target; zero keeps discipline-quota mode."""
    return int(getattr(args, "max_total_papers", 0) or 0)


def _collection_target_reached(records: list[dict], snapshot: dict,
                               disciplines: list, args) -> bool:
    """Use the cumulative valid-paper count when total-target mode is enabled."""
    total_target = _max_total_papers(args)
    if total_target:
        return len(records) >= total_target
    return discipline_targets_met(
        snapshot, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share,
    )


def _candidate_decision(item: dict, snapshot: dict, discipline_map: dict,
                        args) -> tuple[bool, str]:
    """Total-target mode accepts classified and unclassified OA candidates."""
    if _max_total_papers(args):
        return True, "total_target_eligible"
    return quota_decision(
        item, snapshot, discipline_map,
        args.max_journal_share, args.max_review_share,
        args.min_journals_per_discipline,
    )


def _annotate_collection_report(report: dict, records: list[dict], args) -> dict:
    """Expose the active stopping rule to the continuous runner and operators."""
    total_target = _max_total_papers(args)
    report["collection_mode"] = (
        "total_paper_target" if total_target else "discipline_quotas"
    )
    report["total_papers"] = len(records)
    report["max_total_papers"] = total_target
    report["total_target_reached"] = bool(
        total_target and len(records) >= total_target
    )
    return report

def _rolling_disciplines(records: list[dict], disciplines: list,
                         step: int) -> tuple[list, int]:
    """连续模式以最低学科数量为基准，逐轮提升全部学科目标。"""
    base_target = max(discipline.target for discipline in disciplines)
    snapshot = quota_snapshot(records, disciplines)
    minimum_count = min(snapshot["counts"][discipline.id]
                        for discipline in disciplines)
    if minimum_count < base_target:
        round_target = base_target
    else:
        round_target = ((minimum_count // step) + 1) * step
        round_target = max(round_target, base_target)
    return [replace(discipline, target=max(discipline.target, round_target))
            for discipline in disciplines], round_target


def _write_all_reports(root: Path, records: list[dict], disciplines: list,
                       args, mode: str, duplicate_records: int = 0) -> dict:
    """同步更新配额状态与统一量化长表。"""
    report = write_reports(
        root, records, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share, mode,
    )
    analysis_dir = getattr(
        args, "analysis_dir", root.parent / "quantitative_analysis"
    )
    downloaded_records = [
        record for record in records if record.get("status") == "downloaded"
    ]
    write_quantitative_analysis(
        downloaded_records, analysis_dir, source_roots=[root],
        duplicate_records=duplicate_records,
        warnings=[], disciplines=disciplines,
        min_journals=args.min_journals_per_discipline,
        max_journal_share=args.max_journal_share,
        max_review_share=args.max_review_share,
    )
    return report


# 以累计总量或学科缺口为停止条件，期刊仅作为论文来源与溯源维度。
def collect_papers_v6_4(client: Client, args, root: Path) -> dict:
    """按总量或主学科配额采集，并把有效历史论文计入累计目标。"""
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
    loaded_history = load_existing_records(root, category="papers")
    history_entries = [PaperEntry(
        record, root, root / Path(str(record.get("file") or ""))
    ) for record in loaded_history]
    unique_history, duplicate_records = deduplicate_entries(history_entries)
    history = classify_missing_disciplines(
        [entry.record for entry in unique_history], disciplines
    )
    if duplicate_records:
        print(
            f"[V6.4] 历史 manifest 合并 {duplicate_records} 条重复记录；原文件不删除。",
            flush=True,
        )
    for record in history:
        record.setdefault("consolidation_file_status", "local_pdf_present")
    reclassified = sum(
        record.get("analysis_discipline_origin") == "reclassified_available_metadata"
        for record in history
    )
    if reclassified:
        print(
            f"[V6.4] 已按现有元数据重新分类 {reclassified} 篇历史论文并计入统计。",
            flush=True,
        )
    total_target = _max_total_papers(args)
    if getattr(args, "continuous", False) and not total_target:
        disciplines, rolling_target = _rolling_disciplines(
            history, disciplines, args.quota_step
        )
        discipline_map = {discipline.id: discipline for discipline in disciplines}
        print(f"[V6.4] 连续采集轮次目标：每学科 {rolling_target} 篇", flush=True)
    elif total_target:
        print(
            f"[V6.4] 总量优先模式：有效论文达到 {total_target} 篇后停止；"
            "学科分类仅用于统计。",
            flush=True,
        )

    seen_keys: set[str] = set()
    seen_sha: set[str] = set()
    for record in history:
        seen_keys.update(identity_keys(record))
        seen_sha.add(str(record.get("sha256") or "").lower())

    # 先根据全部有效历史记录恢复学科配额，避免重复下载。
    snapshot = quota_snapshot(history, disciplines)
    mode = "metadata_preview" if args.dry_run or not args.download else "pdf_download"
    report = _write_all_reports(
        root, history, disciplines, args, mode, duplicate_records
    )
    print(
        f"[V6.4] journals={len(journals)} stage={args.journal_stage}; "
        f"disciplines={len(disciplines)}; counted={sum(snapshot['counts'].values())}; "
        f"total={len(history)}",
        flush=True,
    )
    if _collection_target_reached(history, snapshot, disciplines, args):
        if total_target:
            print(
                f"[V6.4] 当前有效论文总量 {len(history)} 已达到目标 "
                f"{total_target}。",
                flush=True,
            )
        else:
            print("[V6.4] 当前学科轮次已满足全部约束。", flush=True)
        return _annotate_collection_report(report, history, args)

    errors = root / "errors.jsonl"
    candidates_csv = root / "papers_v6_4_candidates.csv"
    excluded_csv = root / "papers_v6_4_filtered_out.csv"
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
        "circuit_skips": 0,
        "reported_circuit_hosts": set(),
        "max_download_attempts": policy_for(
            policies, journal
        ).max_download_attempts,
        "downloaded": 0,
        "error": "",
    } for journal in journals]
    tried = 0
    previewed = 0
    successes = 0
    stop_event = getattr(args, "stop_event", None)

    def stop_requested() -> bool:
        return bool(stop_event and stop_event.is_set())

    # 轮询各期刊，保证下载请求受全局次数和单刊预算双重限制。
    while (not stop_requested() and tried < args.max_attempts
           and not _collection_target_reached(
               history, snapshot, disciplines, args
           )):
        made_progress = False
        for state in states:
            if stop_requested() or _collection_target_reached(
                history, snapshot, disciplines, args
            ):
                break
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
            eligible, reason = _candidate_decision(
                item, snapshot, discipline_map, args
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
                history.append(preview)
                snapshot = quota_snapshot(history, disciplines)
                seen_keys.update(identity_keys(item))
                previewed += 1
                paper_audit(candidates_csv, preview, "eligible_not_downloaded")
                if total_target:
                    progress = f"total={len(history)}/{total_target}"
                else:
                    discipline_id = item["primary_discipline"]
                    progress = (
                        f"{snapshot['counts'][discipline_id]}/"
                        f"{discipline_map[discipline_id].target}"
                    )
                print(
                    f"[PREVIEW] {item['primary_discipline_name']} {progress} | "
                    f"{journal.name} | {item['title'][:70]}",
                    flush=True,
                )
                continue

            downloaded = False
            last_error = "No OA fallback location succeeded"
            minimum_free = float(getattr(args, "min_free_gb", 0) or 0)
            available_gib = free_disk_gib(root)
            if minimum_free and available_gib < minimum_free:
                raise RuntimeError(
                    f"可用磁盘仅 {available_gib:.2f} GiB，低于 "
                    f"{minimum_free:.2f} GiB 安全阈值"
                )
            # 同一论文最多尝试指定数量的合法公开全文地址。
            for location in item["pdf_locations"][:args.max_pdf_mirrors]:
                if state["download_attempts"] >= state["max_download_attempts"]:
                    last_error = "journal download-attempt budget exhausted"
                    state["active"] = False
                    break
                url = location["url"]
                circuit_is_open = getattr(client, "is_circuit_open", lambda _url: False)
                if circuit_is_open(url):
                    host = location.get("oa_host") or url
                    state["circuit_skips"] += 1
                    last_error = f"domain circuit already open: {host}"
                    if host not in state["reported_circuit_hosts"]:
                        state["reported_circuit_hosts"].add(host)
                        print(
                            f"[CIRCUIT-SKIP] {journal.name}: {host}; "
                            "trying remaining OA mirrors",
                            flush=True,
                        )
                    continue
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
                        seen_keys.update(identity_keys(item))
                        downloaded = True
                        break
                    assert record is not None
                    append_jsonl(root / "manifest.jsonl", record)
                    seen_keys.update(identity_keys(record))
                    seen_sha.add(record["sha256"])
                    record["analysis_discipline_origin"] = "manifest"
                    record["consolidation_file_status"] = "local_pdf_present"
                    history.append(record)
                    snapshot = quota_snapshot(history, disciplines)
                    successes += 1
                    state["downloaded"] += 1
                    if successes % args.report_every == 0:
                        report = _write_all_reports(
                            root, history, disciplines, args, mode,
                            duplicate_records,
                        )
                    paper_audit(candidates_csv, record, "downloaded")
                    if total_target:
                        progress = f"total={len(history)}/{total_target}"
                    else:
                        discipline_id = item["primary_discipline"]
                        progress = (
                            f"{snapshot['counts'][discipline_id]}/"
                            f"{discipline_map[discipline_id].target}"
                        )
                    print(
                        f"[papers] {item['primary_discipline_name']} {progress} | "
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

    report = _write_all_reports(
        root, history, disciplines, args, mode, duplicate_records
    )
    print(
        f"[V6.4] tried={tried} previewed={previewed} new_downloads={successes}; "
        f"total={len(history)}; "
        f"disciplines_met={report['disciplines_meeting_target']}/{len(disciplines)}",
        flush=True,
    )
    for state in states:
        print(
            f"  [SOURCE STATS] {state['journal'].name}: "
            f"http_attempts={state['download_attempts']} "
            f"circuit_skips={state['circuit_skips']} "
            f"downloaded={state['downloaded']}",
            flush=True,
        )
    for row in report["disciplines"]:
        print(
            f"  {row['discipline']}: {row['count']}/{row['target']} "
            f"gap={row['gap']} journals={row['unique_journals']} "
            f"target_met={row['target_met']}",
            flush=True,
        )
    if not total_target and not report["all_disciplines_meet_target"]:
        print(
            "[WARN] Some discipline quotas remain unmet. See "
            "discipline_stats_v6_4.json；程序保留真实缺口。",
            flush=True,
        )
    report["new_downloads"] = successes
    report["attempted_candidates"] = tried
    return _annotate_collection_report(report, history, args)

