"""生成期刊可行性、有效本地论文数量及来源测试统计报告。"""
import csv
import json
from pathlib import Path

from .constants import COLLECTOR_VERSION
from .models import Journal
from .storage import load_existing_records
from .utils import now_utc

# 根据已落盘且去重的有效文件重建每刊统计，不使用推测数量。
def save_journal_stats(root: Path, journals: list[Journal], records: list[dict],
                       per_journal_target: int = 20) -> dict[str, int]:
    """从已下载清单重建每刊数量，并使用 SHA-256 消除重复文件。"""
    counts = {journal.name: 0 for journal in journals}
    seen_sha: set[str] = set()
    for record in records:
        if record.get("journal") not in counts:
            continue
        digest = str(record.get("sha256") or "").lower()
        if digest in seen_sha:
            continue
        seen_sha.add(digest)
        counts[record["journal"]] += 1

    updated_at = now_utc()
    total = sum(counts.values())
    rows = [{
        "journal": journal.name,
        "issn": journal.issn,
        "publisher": journal.publisher,
        "count": counts[journal.name],
        "target": per_journal_target,
        "target_met": counts[journal.name] >= per_journal_target,
        "percentage": round(100 * counts[journal.name] / total, 2) if total else 0.0,
    } for journal in journals]
    report = {
        "version": f"V{COLLECTOR_VERSION}",
        "updated_at": updated_at,
        "counting_rule": "unique SHA-256, status=downloaded, manifest record and local file exist with matching recorded size",
        "per_journal_target": per_journal_target,
        "journals_meeting_target": sum(row["target_met"] for row in rows),
        "total_papers": total,
        "journals": rows,
    }
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "journal_stats.json"
    json_tmp = root / "journal_stats.json.tmp"
    json_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json_tmp.replace(json_path)

    csv_path = root / "journal_stats.csv"
    csv_tmp = root / "journal_stats.csv.tmp"
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["journal", "issn", "publisher", "count", "target",
                        "target_met", "percentage"],
        )
        writer.writeheader()
        writer.writerows(rows)
    csv_tmp.replace(csv_path)
    return counts


def refresh_journal_stats(root: Path, journals: list[Journal],
                          per_journal_target: int = 20) -> dict[str, int]:
    """仅根据清单及本地实际文件更新统计，不执行网络请求。"""
    allowed = {j.name for j in journals}
    records = [r for r in load_existing_records(root, category="papers")
               if r.get("journal") in allowed]
    return save_journal_stats(root, journals, records, per_journal_target)


# 记录每刊候选、下载尝试和目标完成情况，便于横向比较。
def save_feasibility_report(root: Path, states: list[dict], counts: dict[str, int],
                            per_journal_target: int, dry_run: bool) -> None:
    """保存逐期刊可行性测试结果，不使用全局总数推算单刊成功量。"""
    rows = []
    for state in states:
        journal = state["journal"]
        observed = state["previewed"] if dry_run else counts[journal.name]
        rows.append({
            "journal": journal.name,
            "issn": journal.issn,
            "group": journal.group,
            "publisher": journal.publisher,
            "mode": "metadata_preview" if dry_run else "pdf_download",
            "eligible_candidates": state["previewed"] if dry_run else state["tried"],
            "download_attempts": state["download_attempts"] if not dry_run else 0,
            "circuit_skips": state.get("circuit_skips", 0) if not dry_run else 0,
            "new_downloads": state["success"],
            "valid_local_pdfs": counts[journal.name],
            "target": per_journal_target,
            "target_met": observed >= per_journal_target,
            "discovery_error": state.get("error", ""),
        })
    report = {
        "version": f"V{COLLECTOR_VERSION}",
        "updated_at": now_utc(),
        "mode": "metadata_preview" if dry_run else "pdf_download",
        "target_definition": (
            "eligible OpenAlex OA article candidates per journal"
            if dry_run else "valid unique downloaded PDFs per journal"
        ),
        "per_journal_target": per_journal_target,
        "journals_tested": len(rows),
        "journals_meeting_target": sum(row["target_met"] for row in rows),
        "all_journals_meet_target": all(row["target_met"] for row in rows),
        "journals": rows,
    }
    root.mkdir(parents=True, exist_ok=True)
    suffix = COLLECTOR_VERSION.replace(".", "_")
    json_path = root / f"journal_feasibility_v{suffix}.json"
    json_tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    json_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    json_tmp.replace(json_path)
    csv_path = root / f"journal_feasibility_v{suffix}.csv"
    csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    csv_tmp.replace(csv_path)


def print_journal_stats(counts: dict[str, int]) -> None:
    print("[JOURNAL STATS] successfully downloaded, unique PDFs:", flush=True)
    for journal, count in counts.items():
        print(f"  {journal}: {count}", flush=True)
    print(f"  TOTAL: {sum(counts.values())}", flush=True)


# 按照已验证、候选或全部三个阶段选择期刊。
def select_journals(journals: list[Journal], stage: str) -> list[Journal]:
    if stage == "all":
        return journals
    selected = [journal for journal in journals if journal.status == stage]
    if not selected:
        raise ValueError(f"No journals selected for stage={stage!r}")
    return selected

