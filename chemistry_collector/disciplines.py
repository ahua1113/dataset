"""学科分类、开放获取来源排序、配额判定与统计报告的纯逻辑模块。"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _is_https(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


@dataclass(frozen=True)
class Discipline:
    id: str
    name: str
    target: int
    keywords: tuple[str, ...]
    journal_groups: tuple[str, ...]


def load_disciplines(path: Path) -> list[Discipline]:
    if not path.is_file():
        raise FileNotFoundError(f"Discipline config missing: {path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("Discipline config must be a non-empty JSON array")
    result: list[Discipline] = []
    ids: set[str] = set()
    names: set[str] = set()
    for row in rows:
        discipline = Discipline(
            id=str(row["id"]).strip(),
            name=str(row["name"]).strip(),
            target=int(row["target"]),
            keywords=tuple(str(x).strip() for x in row.get("keywords", []) if str(x).strip()),
            journal_groups=tuple(str(x).strip() for x in row.get("journal_groups", []) if str(x).strip()),
        )
        if not re.fullmatch(r"[a-z][a-z0-9_]+", discipline.id):
            raise ValueError(f"Invalid discipline id: {discipline.id}")
        if discipline.id in ids or discipline.name in names:
            raise ValueError(f"Duplicate discipline: {discipline.id}/{discipline.name}")
        if discipline.target <= 0 or not discipline.keywords:
            raise ValueError(f"Discipline requires target>0 and keywords: {discipline.id}")
        ids.add(discipline.id)
        names.add(discipline.name)
        result.append(discipline)
    return result


def reconstruct_abstract(inverted_index: object) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    tokens: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and position >= 0:
                tokens.append((position, str(word)))
    return " ".join(word for _, word in sorted(tokens))


def _topic_text(work: dict) -> str:
    values: list[str] = []
    for topic in work.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        for key in ("display_name",):
            values.append(str(topic.get(key) or ""))
        for key in ("subfield", "field", "domain"):
            nested = topic.get(key) or {}
            if isinstance(nested, dict):
                values.append(str(nested.get("display_name") or ""))
    for keyword in work.get("keywords") or []:
        if isinstance(keyword, dict):
            values.append(str(keyword.get("display_name") or ""))
        else:
            values.append(str(keyword))
    return " ".join(values)


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.casefold().strip()
    if not phrase:
        return False
    if len(phrase) <= 4 and re.fullmatch(r"[a-z0-9+-]+", phrase):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))
    return phrase in text


# 依据标题、主题、摘要和期刊分组评分，选取唯一主学科。
def classify_work(work: dict, journal_group: str,
                  disciplines: list[Discipline]) -> dict:
    """为每篇论文分配唯一主学科，次学科不参与配额计数。"""
    title = " ".join(str(work.get("title") or "").split()).casefold()
    abstract = reconstruct_abstract(work.get("abstract_inverted_index")).casefold()
    topics = _topic_text(work).casefold()
    scored: list[tuple[int, str, Discipline, list[str]]] = []
    for discipline in disciplines:
        score = 0
        evidence: list[str] = []
        for keyword in discipline.keywords:
            hit = False
            if _contains(title, keyword):
                score += 5
                evidence.append(f"title:{keyword}")
                hit = True
            if _contains(topics, keyword):
                score += 3
                evidence.append(f"topic:{keyword}")
                hit = True
            if _contains(abstract, keyword):
                score += 1
                if not hit:
                    evidence.append(f"abstract:{keyword}")
        if journal_group in discipline.journal_groups:
            score += 2
            evidence.append(f"journal_group:{journal_group}")
        scored.append((score, discipline.id, discipline, evidence))
    scored.sort(key=lambda row: (-row[0], row[1]))
    best_score, _, best, best_evidence = scored[0]
    if best_score <= 0:
        return {
            "primary_discipline": "unclassified",
            "primary_discipline_name": "未分类",
            "secondary_disciplines": [],
            "discipline_score": 0,
            "discipline_evidence": [],
            "discipline_classifier": "v6.4_weighted_keywords",
        }
    secondary_threshold = max(2, math.ceil(best_score * 0.35))
    secondary = [row[2].id for row in scored[1:]
                 if row[0] >= secondary_threshold][:4]
    return {
        "primary_discipline": best.id,
        "primary_discipline_name": best.name,
        "secondary_disciplines": secondary,
        "discipline_score": best_score,
        "discipline_evidence": best_evidence[:12],
        "discipline_classifier": "v6.4_weighted_keywords",
    }


def is_review_work(work: dict) -> bool:
    if str(work.get("type") or "").casefold() == "review":
        return True
    title = str(work.get("title") or "")
    return bool(re.search(r"\b(?:systematic review|meta-analysis|review article)\b", title, re.I))


OPEN_LICENSE_ALIASES = {
    "cc-by": "CC-BY",
    "cc-by-sa": "CC-BY-SA",
    "cc-by-nc": "CC-BY-NC",
    "cc-by-nc-sa": "CC-BY-NC-SA",
    "cc-by-nd": "CC-BY-ND",
    "cc-by-nc-nd": "CC-BY-NC-ND",
    "cc0": "CC0",
    "public-domain": "PUBLIC-DOMAIN",
}


# 仅在许可证标识可以识别时归一化，未知许可保留待核验状态。
def normalize_license(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    folded = raw.casefold().replace("_", "-")
    for key, label in sorted(
        OPEN_LICENSE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if key in folded:
            return label, "verified_open_license"
    if "creativecommons.org/licenses/by/" in folded:
        return "CC-BY", "verified_open_license"
    if "creativecommons.org/publicdomain" in folded:
        return "CC0/PUBLIC-DOMAIN", "verified_open_license"
    if raw:
        return raw, "reported_license_manual_review"
    return "CHECK_ARTICLE_SPECIFIC_LICENSE", "license_missing_manual_review"


PREPRINT_SOURCE_DOMAINS = {
    "arxiv.org": "arxiv",
    "chemrxiv.org": "chemrxiv",
    "biorxiv.org": "biorxiv",
    "medrxiv.org": "medrxiv",
    "researchsquare.com": "research_square",
}
PREPRINT_SOURCE_TYPES = frozenset(PREPRINT_SOURCE_DOMAINS.values())


def is_preprint_source(source_type: str) -> bool:
    """判断规范化后的OA来源类型是否属于预印本平台。"""
    return source_type in PREPRINT_SOURCE_TYPES


def classify_source(url: str, landing_page: str, publisher_hosts: tuple[str, ...],
                    source_type: str = "") -> str:
    hosts = {_host(url), _host(landing_page)} - {""}
    if any(any(_domain_matches(host, domain) for domain in publisher_hosts) for host in hosts):
        return "publisher"
    for domain, preprint_type in PREPRINT_SOURCE_DOMAINS.items():
        if any(_domain_matches(host, domain) for host in hosts):
            return preprint_type
    if any(_domain_matches(host, domain) for host in hosts
           for domain in ("ncbi.nlm.nih.gov", "europepmc.org")):
        return "pmc_europepmc"
    if source_type.casefold() == "repository":
        return "institutional_repository"
    return "other_oa_repository"


SOURCE_PRIORITY = {
    "publisher": 0,
    "arxiv": 1,
    "chemrxiv": 2,
    "biorxiv": 3,
    "medrxiv": 4,
    "research_square": 5,
    "pmc_europepmc": 6,
    "institutional_repository": 7,
    "other_oa_repository": 8,
}


# 从元数据提供的多个位置筛选符合条件的全文下载链接。
def openalex_locations(work: dict, publisher_hosts: tuple[str, ...],
                       source_id: str, allow_preprints: bool = True) -> list[dict]:
    locations = [work.get("primary_location"), work.get("best_oa_location")]
    locations.extend(work.get("locations") or [])
    selected: list[dict] = []
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, dict) or not location.get("is_oa"):
            continue
        url = str(location.get("pdf_url") or "").strip()
        if not _is_https(url) or url in seen:
            continue
        landing = str(location.get("landing_page_url") or "").strip()
        source = location.get("source") or {}
        source_kind = classify_source(
            url, landing, publisher_hosts, str(source.get("type") or "")
        )
        if is_preprint_source(source_kind) and not allow_preprints:
            continue
        version = str(location.get("version") or "unknown")
        if is_preprint_source(source_kind):
            version = "submittedVersion"
        publisher_source = str(source.get("id") or "").rstrip("/").split("/")[-1].casefold() == source_id.casefold()
        if source_kind == "publisher":
            publisher_source = True
        if (source_kind != "publisher" and not is_preprint_source(source_kind)
                and version not in {"publishedVersion", "acceptedVersion"}):
            continue
        license_note, license_status = normalize_license(str(location.get("license") or ""))
        seen.add(url)
        selected.append({
            "url": url,
            "version": version,
            "landing_page": landing,
            "license": license_note,
            "license_status": license_status,
            "oa_host": _host(url),
            "publisher_pdf": publisher_source,
            "repository_fallback": not publisher_source,
            "source_type": source_kind,
            "metadata_provider": "openalex",
        })
    return rank_locations(selected)


def unpaywall_locations(data: dict, publisher_hosts: tuple[str, ...],
                        allow_preprints: bool = True) -> list[dict]:
    raw_locations = [data.get("best_oa_location")]
    raw_locations.extend(data.get("oa_locations") or [])
    selected: list[dict] = []
    seen: set[str] = set()
    for location in raw_locations:
        if not isinstance(location, dict):
            continue
        url = str(location.get("url_for_pdf") or "").strip()
        if not _is_https(url) or url in seen:
            continue
        landing = str(location.get("url_for_landing_page") or "").strip()
        host_type = str(location.get("host_type") or "")
        source_kind = classify_source(url, landing, publisher_hosts, host_type)
        if is_preprint_source(source_kind) and not allow_preprints:
            continue
        version = str(location.get("version") or "unknown")
        if is_preprint_source(source_kind):
            version = "submittedVersion"
        if (source_kind != "publisher" and not is_preprint_source(source_kind)
                and version not in {"publishedVersion", "acceptedVersion"}):
            continue
        license_note, license_status = normalize_license(str(location.get("license") or ""))
        seen.add(url)
        selected.append({
            "url": url,
            "version": version,
            "landing_page": landing,
            "license": license_note,
            "license_status": license_status,
            "oa_host": _host(url),
            "publisher_pdf": source_kind == "publisher",
            "repository_fallback": source_kind != "publisher",
            "source_type": source_kind,
            "metadata_provider": "unpaywall",
        })
    return rank_locations(selected)


def rank_locations(locations: list[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for location in locations:
        url = str(location.get("url") or "")
        if url and url not in unique:
            unique[url] = location
    ranked = sorted(unique.values(), key=lambda loc: (
        SOURCE_PRIORITY.get(str(loc.get("source_type")), 99),
        str(loc.get("version")) != "publishedVersion",
        str(loc.get("version")) != "acceptedVersion",
        str(loc.get("metadata_provider")) != "openalex",
        str(loc.get("url")),
    ))
    for index, location in enumerate(ranked, 1):
        location["fallback_rank"] = index
    return ranked


def merge_locations(*groups: list[dict]) -> list[dict]:
    combined: list[dict] = []
    for group in groups:
        combined.extend(group)
    return rank_locations(combined)


def _record_key(record: dict) -> str:
    doi = str(record.get("doi") or "").casefold().strip()
    if doi:
        return "doi:" + doi
    openalex = str(record.get("openalex_id") or "").rstrip("/").split("/")[-1].casefold()
    if openalex:
        return "openalex:" + openalex
    sha = str(record.get("sha256") or "").casefold()
    return "sha:" + sha


# 以唯一论文为单位汇总各主学科的数量、期刊分布与综述数量。
def quota_snapshot(records: list[dict], disciplines: list[Discipline]) -> dict:
    discipline_ids = {discipline.id for discipline in disciplines}
    counts = Counter()
    reviews = Counter()
    journals: dict[str, Counter] = defaultdict(Counter)
    seen: set[str] = set()
    for record in records:
        key = _record_key(record)
        if key in seen:
            continue
        seen.add(key)
        discipline_id = str(record.get("primary_discipline") or "")
        if discipline_id not in discipline_ids:
            continue
        counts[discipline_id] += 1
        journals[discipline_id][str(record.get("journal") or "UNKNOWN")] += 1
        if bool(record.get("is_review")) or record.get("resource_kind") == "review_article":
            reviews[discipline_id] += 1
    return {"counts": counts, "reviews": reviews, "journals": journals}


# 下载前检查学科目标、期刊集中度与综述占比等约束。
def quota_decision(item: dict, snapshot: dict, discipline_map: dict[str, Discipline],
                   max_journal_share: float, max_review_share: float,
                   min_journals: int = 1) -> tuple[bool, str]:
    discipline_id = str(item.get("primary_discipline") or "")
    if discipline_id not in discipline_map:
        return False, "unclassified"
    discipline = discipline_map[discipline_id]
    count = snapshot["counts"][discipline_id]
    journal_name = str(item.get("journal") or "UNKNOWN")
    journal_counts = snapshot["journals"][discipline_id]
    unique_journals = sum(value > 0 for value in journal_counts.values())
    largest = max(journal_counts.values(), default=0)
    largest_share = largest / count if count else 0.0
    review_share = snapshot["reviews"][discipline_id] / count if count else 0.0
    if count >= discipline.target:
        if unique_journals < min_journals:
            if journal_name in journal_counts:
                return False, "discipline_needs_new_journal_for_diversity"
            return True, "quota_repair_journal_diversity"
        if largest_share > max_journal_share:
            dominant = {name for name, value in journal_counts.items() if value == largest}
            if journal_name in dominant:
                return False, "discipline_dominant_journal_share_too_high"
            return True, "quota_repair_journal_share"
        if review_share > max_review_share:
            if item.get("is_review"):
                return False, "discipline_review_share_too_high"
            return True, "quota_repair_review_share"
        return False, "discipline_target_already_met"
    journal_cap = max(1, math.floor(discipline.target * max_journal_share))
    journal_count = journal_counts[journal_name]
    if journal_count >= journal_cap:
        return False, "discipline_single_journal_cap_reached"
    review_cap = max(0, math.floor(discipline.target * max_review_share))
    if item.get("is_review") and snapshot["reviews"][discipline_id] >= review_cap:
        return False, "discipline_review_cap_reached"
    return True, "quota_eligible"


def all_targets_met(snapshot: dict, disciplines: list[Discipline],
                    min_journals: int, max_journal_share: float,
                    max_review_share: float) -> bool:
    for discipline in disciplines:
        count = snapshot["counts"][discipline.id]
        if count < discipline.target:
            return False
        active_journals = sum(value > 0 for value in snapshot["journals"][discipline.id].values())
        if active_journals < min_journals:
            return False
        if count and max(snapshot["journals"][discipline.id].values(), default=0) / count > max_journal_share:
            return False
        if count and snapshot["reviews"][discipline.id] / count > max_review_share:
            return False
    return True


# 汇总学科覆盖和来源统计，并写出供审计使用的报告。
def write_reports(root: Path, records: list[dict], disciplines: list[Discipline],
                  min_journals: int, max_journal_share: float,
                  max_review_share: float, mode: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    snapshot = quota_snapshot(records, disciplines)
    rows: list[dict] = []
    for discipline in disciplines:
        count = snapshot["counts"][discipline.id]
        journal_counts = snapshot["journals"][discipline.id]
        unique_journals = sum(value > 0 for value in journal_counts.values())
        largest = max(journal_counts.values(), default=0)
        review_count = snapshot["reviews"][discipline.id]
        largest_share = largest / count if count else 0.0
        review_share = review_count / count if count else 0.0
        target_met = (
            count >= discipline.target
            and unique_journals >= min_journals
            and largest_share <= max_journal_share
            and review_share <= max_review_share
        )
        rows.append({
            "discipline_id": discipline.id,
            "discipline": discipline.name,
            "count": count,
            "target": discipline.target,
            "gap": max(0, discipline.target - count),
            "unique_journals": unique_journals,
            "min_journals": min_journals,
            "largest_journal_share": round(largest_share, 4),
            "max_journal_share": max_journal_share,
            "review_count": review_count,
            "review_share": round(review_share, 4),
            "max_review_share": max_review_share,
            "target_met": target_met,
        })
    report = {
        "version": "V6.4",
        "updated_at": _now_utc(),
        "mode": mode,
        "counting_rule": "one primary discipline per unique DOI/OpenAlex work; secondary disciplines do not count",
        "all_disciplines_meet_target": all(row["target_met"] for row in rows),
        "disciplines_meeting_target": sum(row["target_met"] for row in rows),
        "discipline_count": len(rows),
        "total_counted_papers": sum(row["count"] for row in rows),
        "disciplines": rows,
    }
    json_path = root / "discipline_stats_v6_4.json"
    json_tmp = json_path.with_suffix(".json.tmp")
    json_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json_tmp.replace(json_path)

    return report
