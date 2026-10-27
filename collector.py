#!/usr/bin/env python3
"""Chemistry Source Collector V6.3 OA-only discipline quota stage.

Papers: validated and candidate chemistry journals, discipline-level quotas,
        OpenAlex + Unpaywall OA locations, traceable fallback metadata,
        global metadata and SHA-256 deduplication.
No publisher-login/paywall bypassing logic.
MIT-OCW videos/teaching and optional methods/textbooks remain usable.

Quick start:
    python collector.py --categories papers --dry-run
    python collector.py --categories papers --download
    python collector.py --categories papers --stats-only
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from v6_3_core import (
    Discipline,
    all_targets_met as discipline_targets_met,
    classify_work,
    is_review_work,
    load_disciplines,
    merge_locations,
    openalex_locations,
    quota_decision,
    quota_snapshot,
    unpaywall_locations,
    write_reports as write_v6_3_reports,
)

ROOT = Path(__file__).resolve().parent
COLLECTOR_VERSION = "6.3"
WHITELIST_SIZE = 42
CONFIG_WHITELIST_SIZES = {
    "journals_v6.json": 10,
    "journals_v6_1.json": 20,
    "journals_v6_2.json": 34,
    "journals_v6_3.json": 42,
}
OPENALEX_SOURCES = "https://api.openalex.org/sources"
OPENALEX_WORKS = "https://api.openalex.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
EPA_INDEX = "https://www.epa.gov/hw-sw846/sw-846-compendium"

# Paper identity always comes from an ISSN-verified journal. Public copies may
# come from the publisher, PMC/Europe PMC, arXiv, ChemRxiv or a repository.
CATEGORY_CHOICES = {"papers", "videos", "teaching", "methods", "textbooks"}
EXCLUDED_ARTICLE_TITLES = re.compile(
    r"^\s*(?:publisher(?:'s|’s)?\s+correction|author\s+correction|"
    r"correction\s+(?:to|for)\b|erratum\b|retraction\b|"
    r"expression\s+of\s+concern\b|editorial\b|book\s+review\b)", re.I,
)
VIDEO_NON_CHEMICAL_TOPIC = re.compile(
    r"\b(?:writing\s+up\s+the\s+lab\s+report|"
    r"ellen\s+swallow\s+richards|how\s+to\s+use\s+this\s+course|"
    r"administrative\s+announcements)\b", re.I,
)
CHEMISTRY_BOOK_TITLE = re.compile(
    r"\b(?:chemistry|chemical|biochemistry|organic\s+chem|"
    r"inorganic\s+chem|physical\s+chem|analytical\s+chem)\b", re.I,
)
MIT_COURSES = [
    "https://ocw.mit.edu/courses/5-111sc-principles-of-chemical-science-fall-2014/",
    "https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/",
    "https://ocw.mit.edu/courses/5-112-principles-of-chemical-science-fall-2005/",
    "https://ocw.mit.edu/courses/5-60-thermodynamics-kinetics-spring-2008/",
    "https://ocw.mit.edu/courses/5-61-physical-chemistry-fall-2017/",
    "https://ocw.mit.edu/courses/5-310-laboratory-chemistry-fall-2019/",
    "https://ocw.mit.edu/courses/5-07sc-biological-chemistry-i-fall-2013/",
]
MIT_CHEMISTRY_SLUGS = {
    root.rstrip("/").split("/")[-1] for root in MIT_COURSES
}
MIME = {".pdf": "application/pdf", ".mp4": "video/mp4", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ",
                  (title or "").casefold())).strip()


def normalize_doi(doi: str) -> str:
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (doi or "").strip().lower()).rstrip(" .;,")


def normalized_id(value: str) -> str:
    return (value or "").strip().rstrip("/").split("/")[-1].lower()


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def course_slug(url: str) -> str:
    m = re.search(r"/courses/([^/]+)/", urlparse(url).path, re.I)
    return m.group(1).lower() if m else ""


def is_https(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and bool(p.netloc) and not p.username and not p.password


def log_error(path: Path, category: str, message: str, **details) -> None:
    append_jsonl(path, {"category": category, "error": str(message), "at": now_utc(), **details})


def append_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(item, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class Journal:
    name: str
    issn: str
    publisher: str
    website: str
    group: str
    origin: str
    publisher_hosts: tuple[str, ...]
    status: str = "validated"
    feasibility_target: int = 0


def load_journals(path: Path) -> list[Journal]:
    if not path.is_file():
        raise FileNotFoundError(f"Journal whitelist missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        base_name = str(data.get("extends") or "").strip()
        additions = data.get("journals")
        if not base_name or Path(base_name).name != base_name:
            raise ValueError("Extended journal config requires a safe local 'extends' filename")
        if not isinstance(additions, list):
            raise ValueError("Extended journal config requires a 'journals' array")
        base_path = path.parent / base_name
        base_data = json.loads(base_path.read_text(encoding="utf-8"))
        if not isinstance(base_data, list):
            raise ValueError("Base journal whitelist must be a JSON array")
        data = base_data + additions
    expected_size = CONFIG_WHITELIST_SIZES.get(path.name)
    if not isinstance(data, list) or not data:
        raise ValueError("Journal whitelist must be a non-empty JSON array")
    if expected_size is not None and len(data) != expected_size:
        raise ValueError(
            f"{path.name} journal whitelist must contain exactly {expected_size} journals"
        )
    if expected_size is None and len(data) > WHITELIST_SIZE:
        raise ValueError(f"Custom journal whitelist cannot exceed {WHITELIST_SIZE} journals")
    result = []
    names, issns = set(), set()
    for row in data:
        journal = Journal(
            name=str(row["name"]).strip(),
            issn=str(row["issn"]).strip(),
            publisher=str(row["publisher"]).strip(),
            website=str(row["website"]).strip(),
            group=str(row["group"]).strip(),
            origin=str(row["origin"]).strip(),
            publisher_hosts=tuple(str(x).lower() for x in row["publisher_hosts"]),
            status=str(row.get("status") or "validated").strip().lower(),
            feasibility_target=int(row.get("feasibility_target") or 0),
        )
        if not re.fullmatch(r"\d{4}-[\dX]{4}", journal.issn, re.I):
            raise ValueError(f"Invalid ISSN: {journal.name}: {journal.issn}")
        if not journal.name or not is_https(journal.website) or not journal.publisher_hosts:
            raise ValueError(f"Invalid whitelist entry: {journal.name}")
        if journal.status not in {"validated", "candidate"}:
            raise ValueError(f"Invalid journal status: {journal.name}: {journal.status}")
        if journal.feasibility_target < 0:
            raise ValueError(f"Invalid feasibility target: {journal.name}")
        if journal.name.casefold() in names or journal.issn in issns:
            raise ValueError(f"Duplicate whitelist journal/ISSN: {journal.name}")
        names.add(journal.name.casefold())
        issns.add(journal.issn)
        result.append(journal)
    return result


class SourceError(RuntimeError):
    pass


class Client:
    """No login, session cookies, paywall workaround or other auth workflow."""

    def __init__(self, contact: str = "", respect_robots: bool = True,
                 openalex_api_key: str = ""):
        self.session = requests.Session()
        agent = f"AcademicChemistrySourceCollector/{COLLECTOR_VERSION}"
        if contact.strip():
            agent += f" (contact: {contact.strip()})"
        self.session.headers.update({"User-Agent": agent, "Accept": "application/json, application/pdf, */*"})
        self.respect_robots = respect_robots
        self.contact = contact.strip()
        self.openalex_api_key = openalex_api_key.strip()
        self.robot_parsers: dict[str, RobotFileParser] = {}
        self.last_request: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = hostname(url)
        gap = 1.0
        delta = time.monotonic() - self.last_request.get(host, 0.0)
        if delta < gap:
            time.sleep(gap - delta)
        self.last_request[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robot_parsers:
            robots_url = origin + "/robots.txt"
            try:
                response = self.session.get(robots_url, timeout=(8, 15))
                if response.status_code == 404:
                    parser = RobotFileParser()
                    parser.parse([])
                elif response.ok:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                else:
                    raise SourceError(f"robots.txt verification HTTP {response.status_code}: {robots_url}")
                self.robot_parsers[origin] = parser
            except requests.RequestException as exc:
                raise SourceError(f"Cannot verify robots.txt for {origin}: {exc}") from exc
        return self.robot_parsers[origin].can_fetch(self.session.headers["User-Agent"], url)

    def get(self, url: str, *, api: bool = False, stream: bool = False):
        if not is_https(url):
            raise SourceError("Only HTTPS links are allowed")
        if not api and not self.allowed(url):
            raise SourceError(f"Disallowed by robots.txt: {url}")
        self.wait(url)
        try:
            headers = {}
            if api and self.openalex_api_key and domain_matches(hostname(url), "openalex.org"):
                headers["Authorization"] = f"Bearer {self.openalex_api_key}"
            response = self.session.get(url, timeout=(10, 40), stream=stream,
                                        allow_redirects=True, headers=headers)
        except requests.RequestException as exc:
            raise SourceError(str(exc)) from exc
        if not is_https(response.url):
            response.close()
            raise SourceError("Download redirects to a non-HTTPS URL")
        if not response.ok:
            status = response.status_code
            response.close()
            if status == 429 and domain_matches(hostname(url), "openalex.org"):
                key_hint = ("OpenAlex API key budget/rate limit reached"
                            if self.openalex_api_key else
                            "OpenAlex shared rate limit reached; set OPENALEX_API_KEY "
                            "or use --openalex-api-key")
                raise SourceError(f"HTTP 429: {key_hint}")
            raise SourceError(f"HTTP {status} for {url}")
        # Publishers can redirect an OA URL to a login / access wall.
        if not api and re.search(r"/(?:login|signin|authenticate)(?:[/?]|$)", urlparse(response.url).path, re.I):
            response.close()
            raise SourceError("Redirected to an authentication page")
        return response

    def json(self, url: str) -> dict:
        with self.get(url, api=True) as response:
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Response is not a JSON object")
                return result
            except ValueError as exc:
                raise SourceError(f"Invalid API JSON: {exc}") from exc

    def html(self, url: str) -> BeautifulSoup:
        with self.get(url) as response:
            return BeautifulSoup(response.content, "html.parser")


def resolve_openalex_source(client: Client, journal: Journal) -> str:
    data = client.json(f"{OPENALEX_SOURCES}/issn:{journal.issn}")
    issn_set = set(data.get("issn") or [])
    if data.get("issn_l"):
        issn_set.add(data["issn_l"])
    if journal.issn not in issn_set:
        raise SourceError(f"ISSN mismatch for {journal.name}: OpenAlex returned {sorted(issn_set)}")
    source_id = normalized_id(str(data.get("id") or "")).upper()
    if not re.fullmatch(r"S\d+", source_id):
        raise SourceError(f"Invalid OpenAlex Source ID for {journal.name}: {source_id}")
    return source_id


def candidate_pdf_locations(work: dict, journal: Journal, source_id: str,
                            allow_preprints: bool = False) -> list[dict]:
    """Publisher OA PDF first; repository accepted/published copy as fallback.

    A preprint-hosted PDF is *not* a journal-published PDF. Exclude known
    preprint servers even if OpenAlex links that version to the journal work.
    """
    return openalex_locations(
        work, journal.publisher_hosts, source_id, allow_preprints=allow_preprints
    )


def unpaywall_pdf_locations(client: Client, doi: str, journal: Journal,
                            allow_preprints: bool = True) -> list[dict]:
    """Resolve extra legal OA copies. Unpaywall requires a contact email."""
    if not client.contact or "@" not in client.contact:
        return []
    url = f"{UNPAYWALL_API}/{doi}?" + urlencode({"email": client.contact})
    try:
        data = client.json(url)
    except SourceError as exc:
        print(f"[WARN] Unpaywall lookup failed for {doi}: {exc}", flush=True)
        return []
    if not data.get("is_oa"):
        return []
    return unpaywall_locations(
        data, journal.publisher_hosts, allow_preprints=allow_preprints
    )


def article_candidate(work: dict, journal: Journal, source_id: str,
                      disciplines: list[Discipline] | None = None,
                      allow_preprints: bool = False) -> tuple[dict | None, str]:
    if work.get("type") not in {"article", "review"}:
        return None, "not an article or review"
    if work.get("is_retracted") or work.get("is_paratext"):
        return None, "retracted or paratext"
    title = " ".join(str(work.get("title") or "").split())
    if not title or EXCLUDED_ARTICLE_TITLES.search(title):
        return None, "correction/editorial/non-research title"
    if not (work.get("open_access") or {}).get("is_oa"):
        return None, "not OA according to OpenAlex"
    primary = work.get("primary_location") or {}
    actual_source = normalized_id(str((primary.get("source") or {}).get("id") or ""))
    if actual_source != source_id.lower():
        return None, "primary journal source does not match whitelist ISSN"
    doi = normalize_doi(str(work.get("doi") or ""))
    if not doi:
        return None, "missing DOI; cannot verify published article identity"
    locations = candidate_pdf_locations(
        work, journal, source_id, allow_preprints=allow_preprints
    )
    if not locations and disciplines is None:
        return None, "OA work has no acceptable HTTPS PDF location"
    landing = str(primary.get("landing_page_url") or "").strip()
    article_page = landing if is_https(landing) else "https://doi.org/" + doi
    oa = work.get("open_access") or {}
    review = is_review_work(work)
    classification = (
        classify_work(work, journal.group, disciplines)
        if disciplines else {
            "primary_discipline": "chemistry",
            "primary_discipline_name": "Chemistry",
            "secondary_disciplines": [],
            "discipline_score": 1,
            "discipline_evidence": [f"ISSN-verified journal whitelist: {journal.name}"],
            "discipline_classifier": "legacy_journal_whitelist",
        }
    )
    return {
        "category": "papers", "resource_kind": "review_article" if review else "research_article",
        "title": title, "journal": journal.name, "journal_issn": journal.issn,
        "journal_group": journal.group, "journal_whitelist_origin": journal.origin,
        "publisher": journal.publisher, "source_group": "v6_3_oa_journals",
        "journal_status": journal.status,
        "source_page": article_page, "doi": doi,
        "openalex_id": str(work.get("id") or ""),
        "openalex_source_id": source_id,
        "published_at": str(work.get("publication_date") or ""),
        "oa_verified": True, "oa_status": str(oa.get("oa_status") or ""),
        **classification,
        "is_review": review,
        "discipline_verification_level": "source_issn+primary_journal_id+metadata_classifier",
        "article_filter": "type=article, OA=true, non-retracted, DOI present",
        "pdf_locations": locations,
    }, "accepted"


def journal_article_iterator(client: Client, journal: Journal, per_page: int, pages: int,
                             error_path: Path, disciplines: list[Discipline] | None = None,
                             allow_preprints: bool = False) -> Iterator[dict]:
    source_id = resolve_openalex_source(client, journal)
    print(f"[OpenAlex] {journal.name}: ISSN={journal.issn} -> {source_id}", flush=True)
    for page in range(1, pages + 1):
        params = {
            "filter": (f"primary_location.source.id:{source_id},"
                       "open_access.is_oa:true,type:article|review,is_retracted:false"),
            "sort": "publication_date:desc",
            "page": page,
            "per_page": per_page,
            "select": ("id,doi,title,type,publication_date,open_access,"
                       "primary_location,best_oa_location,locations,"
                       "is_retracted,is_paratext,abstract_inverted_index,topics,keywords"),
        }
        url = OPENALEX_WORKS + "?" + urlencode(params)
        data = client.json(url)
        results = data.get("results")
        if not isinstance(results, list):
            raise SourceError(f"Invalid OpenAlex works response: {journal.name}, page={page}")
        qualified = 0
        for work in results:
            try:
                candidate, reason = article_candidate(
                    work, journal, source_id, disciplines,
                    allow_preprints=allow_preprints,
                )
                if candidate:
                    qualified += 1
                    yield candidate
            except (TypeError, AttributeError, ValueError) as exc:
                log_error(error_path, "papers", f"Bad article metadata: {exc}",
                          journal=journal.name, openalex_id=work.get("id", "") if isinstance(work, dict) else "")
        print(f"[DISCOVER] {journal.name} page={page}: works={len(results)} eligible_pdf={qualified}", flush=True)
        if len(results) < per_page:
            break


def identity_keys(item: dict) -> set[str]:
    keys = set()
    doi = normalize_doi(str(item.get("doi") or ""))
    if doi:
        keys.add("doi:" + doi)
    openalex = normalized_id(str(item.get("openalex_id") or ""))
    if openalex:
        keys.add("openalex:" + openalex)
    title = normalized_title(str(item.get("title") or ""))
    if title:
        keys.add("title:" + title)
    url = str(item.get("download_url") or "").strip().lower()
    if url:
        keys.add("url:" + url)
    for location in item.get("pdf_locations") or []:
        candidate_url = str(location.get("url") or "").strip().lower()
        if candidate_url:
            keys.add("url:" + candidate_url)
    return keys


def load_existing_records(root: Path, *, category: str,
                          permitted_journals: set[str] | None = None) -> list[dict]:
    manifest = root / "manifest.jsonl"
    if not manifest.is_file():
        return []
    records = []
    for line_num, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(f"Bad manifest JSON on line {line_num}: {exc}") from exc
        if row.get("category") != category or row.get("status") != "downloaded":
            continue
        if permitted_journals is not None and row.get("journal") not in permitted_journals:
            raise RuntimeError(
                f"Existing manifest contains a non-whitelist journal record: {row.get('journal')!r}. "
                "Use an empty output folder; the program will NOT erase legacy data."
            )
        rel = Path(str(row.get("file") or ""))
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(f"Unsafe manifest path on line {line_num}: {rel}")
        path = root / rel
        if not path.is_file() or path.stat().st_size != row.get("bytes"):
            print(f"[WARN] Missing/size-mismatched old file, not counted: {rel}", flush=True)
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256") or ""), re.I):
            raise RuntimeError(f"Manifest missing valid SHA-256 on line {line_num}")
        records.append(row)
    return records


def file_signature_ok(first_bytes: bytes, ext: str) -> bool:
    if ext == ".pdf":
        return first_bytes.startswith(b"%PDF-")
    if ext == ".mp4":
        return len(first_bytes) >= 8 and first_bytes[4:8] == b"ftyp"
    if ext == ".docx":
        return first_bytes.startswith(b"PK\x03\x04")
    return False


def download_file(client: Client, item: dict, root: Path, *, url: str,
                  ext: str, max_file_mb: int, seen_sha: set[str]) -> tuple[str, dict | None]:
    """Stream to .part; verify magic bytes; SHA check before atomic rename.

    Returns downloaded/sha_duplicate and the new record (if downloaded).
    """
    if not is_https(url):
        raise SourceError(f"Non-HTTPS download: {url}")
    folder = root / "raw" / item["category"]
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{item['category']}_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}{ext}"
    target = folder / name
    tmp = folder / (name + ".part")
    limit = max_file_mb * 1024 * 1024
    total = 0
    sha = hashlib.sha256()
    first = bytearray()
    try:
        with client.get(url, stream=True) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower().strip()
            if content_type in {"text/html", "application/json", "text/plain", "application/xml"}:
                raise SourceError(f"Not a PDF/media response: {content_type}")
            clen = int(response.headers.get("Content-Length", "0") or 0)
            if clen > limit:
                raise SourceError(f"File exceeds {max_file_mb} MB limit")
            with tmp.open("wb") as dest:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    if len(first) < 16:
                        first.extend(chunk[:16 - len(first)])
                    total += len(chunk)
                    if total > limit:
                        raise SourceError(f"File exceeds {max_file_mb} MB limit")
                    dest.write(chunk)
                    sha.update(chunk)
        if total < 1024 or not file_signature_ok(bytes(first), ext):
            raise SourceError(f"Downloaded content is not a valid {ext} file signature")
        digest = sha.hexdigest()
        if digest in seen_sha:
            return "sha_duplicate", None
        if target.exists():
            # Avoid replacing an unexpected existing file with the same
            # URL-derived name; allow recovery of a verified orphan file if
            # the previous run wrote it but crashed before manifest append.
            existing_digest = hash_file(target)
            if existing_digest != digest:
                raise SourceError(f"Output filename collision with different file: {target}")
        else:
            tmp.replace(target)
        record = {k: v for k, v in item.items() if k != "pdf_locations"}
        record.update({
            "download_url": url, "file": str(target.relative_to(root)),
            "bytes": total, "sha256": digest,
            "mime_expected": MIME[ext], "collected_at": now_utc(),
            "status": "downloaded", "collector_version": COLLECTOR_VERSION,
        })
        return "downloaded", record
    finally:
        tmp.unlink(missing_ok=True)


def hash_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def append_csv(path: Path, item: dict, fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(item)


PAPER_AUDIT_COLUMNS = [
    "at", "journal", "journal_issn", "title", "doi", "openalex_id",
    "source_page", "download_url", "oa_status", "oa_version", "oa_pdf_host",
    "primary_discipline", "primary_discipline_name", "oa_pdf_source",
    "oa_metadata_provider", "license_note", "license_verification",
    "filter_status", "filter_reason",
]


def paper_audit(path: Path, item: dict, status: str, reason: str = "") -> None:
    append_csv(path, {"at": now_utc(), **item, "filter_status": status,
                      "filter_reason": reason}, PAPER_AUDIT_COLUMNS)


def save_journal_stats(root: Path, journals: list[Journal], records: list[dict],
                       per_journal_target: int = 20) -> dict[str, int]:
    """Rebuild per-journal counts from downloaded manifest files; deduplicate by SHA-256."""
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
    """Rebuild from manifest and real files, without network requests."""
    allowed = {j.name for j in journals}
    records = [r for r in load_existing_records(root, category="papers")
               if r.get("journal") in allowed]
    return save_journal_stats(root, journals, records, per_journal_target)


def save_feasibility_report(root: Path, states: list[dict], counts: dict[str, int],
                            per_journal_target: int, dry_run: bool) -> None:
    """Persist a per-journal source test; never infer success from a global total."""
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


def select_journals(journals: list[Journal], stage: str) -> list[Journal]:
    if stage == "all":
        return journals
    selected = [journal for journal in journals if journal.status == stage]
    if not selected:
        raise ValueError(f"No journals selected for stage={stage!r}")
    return selected


def collect_papers(client: Client, args, root: Path) -> None:
    journals = select_journals(
        load_journals(args.journals_file), getattr(args, "journal_stage", "all")
    )
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
         "download_attempts": 0, "error": ""}
        for j in journals
    ]
    successes = 0
    tried = 0
    previewed = 0

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
                        downloaded = True  # duplicate work; do not try further locations
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


def collect_papers_v6_3(client: Client, args, root: Path) -> None:
    """Collect by discipline quota; journals are sources, never quota buckets."""
    all_journals = load_journals(args.journals_file)
    journals = select_journals(all_journals, args.journal_stage)
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
        "downloaded": 0,
        "error": "",
    } for journal in journals]
    tried = 0
    previewed = 0
    successes = 0

    while tried < args.max_attempts and not discipline_targets_met(
        snapshot, disciplines, args.min_journals_per_discipline,
        args.max_journal_share, args.max_review_share,
    ):
        made_progress = False
        for state in states:
            if not state["active"]:
                continue
            journal = state["journal"]
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
            for location in item["pdf_locations"][:args.max_pdf_mirrors]:
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


def mit_candidates(client: Client, category: str) -> Iterator[dict]:
    endpoint, suffix = ("lecture-videos", ".mp4") if category == "videos" else ("lecture-notes", ".pdf")
    visited = set()
    for course in MIT_COURSES:
        for page in (urljoin(course, "resources/" + endpoint + "/"),
                     urljoin(course, "download/")):
            try:
                soup = client.html(page)
            except SourceError as exc:
                print(f"[WARN] MIT page: {exc}", flush=True)
                continue
            for anchor in soup.select("a[href]"):
                url = urljoin(page, anchor.get("href", ""))
                if not is_https(url) or not urlparse(url).path.lower().endswith(suffix) or url in visited:
                    continue
                host = hostname(url)
                if category == "videos" and not domain_matches(host, "archive.org"):
                    continue
                if category == "teaching" and not domain_matches(host, "ocw.mit.edu"):
                    continue
                visited.add(url)
                stem = Path(urlparse(url).path).stem
                title = f"{course.rstrip('/').split('/')[-1]} - {stem} - {anchor.get_text(' ', strip=True)}"[:240]
                if course_slug(page) not in MIT_CHEMISTRY_SLUGS or VIDEO_NON_CHEMICAL_TOPIC.search(title):
                    continue
                yield {
                    "category": category,
                    "resource_kind": "course_video" if category == "videos" else "lecture_notes",
                    "title": title, "source_page": page, "download_url": url,
                    "publisher": "MIT OpenCourseWare",
                    "license_note": "MIT_OCW_LICENSE_CHECK_THIRD_PARTY_MATERIAL",
                    "published_at": "", "doi": "",
                    "discipline": "Chemistry",
                    "discipline_evidence": "MIT chemistry course-level whitelist",
                }


def method_candidates(client: Client) -> Iterator[dict]:
    soup = client.html(EPA_INDEX)
    seen_pages = set()
    for anchor in soup.select("a[href]"):
        page = urljoin(EPA_INDEX, anchor.get("href", ""))
        if hostname(page) != "www.epa.gov" or "/hw-sw846/sw-846-test-method-" not in urlparse(page).path:
            continue
        if page in seen_pages:
            continue
        seen_pages.add(page)
        try:
            detail = client.html(page)
        except SourceError as exc:
            print(f"[WARN] EPA page: {exc}")
            continue
        title = detail.h1.get_text(" ", strip=True) if detail.h1 else anchor.get_text(" ", strip=True)
        for link in detail.select("a[href]"):
            url = urljoin(page, link.get("href", ""))
            if is_https(url) and urlparse(url).path.lower().endswith(".pdf") and domain_matches(hostname(url), "epa.gov"):
                yield {"category": "methods", "resource_kind": "analytical_test_method",
                       "title": title, "source_page": page, "download_url": url,
                       "publisher": "US EPA", "license_note": "US_GOV_WORK_CHECK_THIRD_PARTY_MATERIAL",
                       "published_at": "", "doi": ""}
                break


def textbook_candidates(csv_path: Path) -> Iterator[dict]:
    if not csv_path.exists():
        print(f"[INFO] Optional textbook seeds absent: {csv_path}")
        return
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url = str(row.get("download_url") or "").strip()
            title = str(row.get("title") or "").strip()
            if not is_https(url) or not urlparse(url).path.lower().endswith(".pdf") or not CHEMISTRY_BOOK_TITLE.search(title):
                continue
            yield {"category": "textbooks", "resource_kind": "whole_textbook",
                   "title": title, "source_page": row.get("source_page", ""),
                   "download_url": url, "publisher": row.get("publisher", ""),
                   "license_note": row.get("license_note", "CHECK_BOOK_LICENSE"),
                   "published_at": row.get("published_at", ""), "doi": ""}


def collect_other(client: Client, args, root: Path, category: str) -> None:
    old = load_existing_records(root, category=category)
    total = len(old)
    if total >= args.limit:
        print(f"[{category}] Target already reached: {total}/{args.limit}")
        return
    seen_url = {r.get("download_url") for r in old}
    seen_sha = {str(r["sha256"]).lower() for r in old}
    if category in {"videos", "teaching"}:
        candidates = mit_candidates(client, category)
        ext = ".mp4" if category == "videos" else ".pdf"
    elif category == "methods":
        candidates, ext = method_candidates(client), ".pdf"
    elif category == "textbooks":
        candidates, ext = textbook_candidates(args.books_csv), ".pdf"
    else:
        raise ValueError(f"Unsupported category: {category}")
    for item in candidates:
        if total >= args.limit:
            break
        url = item["download_url"]
        if url in seen_url:
            continue
        seen_url.add(url)
        if not args.download or args.dry_run:
            print(f"[PREVIEW] {category}: {item['title'][:90]}")
            continue
        try:
            status, record = download_file(client, item, root, url=url, ext=ext,
                                           max_file_mb=args.max_file_mb, seen_sha=seen_sha)
            if status == "sha_duplicate":
                print(f"[DUP-SHA] {category}: {url}")
                continue
            assert record is not None
            append_jsonl(root / "manifest.jsonl", record)
            seen_sha.add(record["sha256"])
            total += 1
            print(f"[{category}] {total}/{args.limit}: {record['file']}")
        except (SourceError, requests.RequestException, OSError, ValueError) as exc:
            log_error(root / "errors.jsonl", category, str(exc), download_url=url)
            print(f"[SKIP] {category}: {exc}")


def audit_existing(root: Path, categories: list[str], journals: list[Journal]) -> None:
    for category in categories:
        valid = load_existing_records(root, category=category)
        if category == "papers":
            valid = [r for r in valid if r.get("journal") in {j.name for j in journals}]
        errors = 0
        for row in valid:
            if hash_file(root / row["file"]) != str(row["sha256"]).lower():
                errors += 1
                print(f"[AUDIT-SHA-MISMATCH] {row['file']}")
        print(f"[AUDIT] category={category}, checked={len(valid)}, SHA mismatches={errors}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories", default="papers", help="papers,videos,teaching,methods,textbooks")
    parser.add_argument("--limit", type=int, default=100,
                        help="cumulative successful files for non-paper categories")
    parser.add_argument("--per-journal-target", type=int, default=10,
                        help="candidate-journal feasibility target; only used with --feasibility-only")
    parser.add_argument("--download", action="store_true", help="download originals (otherwise only preview)")
    parser.add_argument("--dry-run", action="store_true", help="preview eligible papers only")
    parser.add_argument("--out", type=Path, default=ROOT / "chemistry_dataset" / "papers_dataset")
    parser.add_argument("--journals-file", type=Path,
                        default=ROOT / "config" / "journals_v6_3.json")
    parser.add_argument("--disciplines-file", type=Path,
                        default=ROOT / "config" / "disciplines_v6_3.json")
    parser.add_argument("--journal-stage", choices=("validated", "candidate", "all"),
                        default="validated",
                        help="validated=34 proven journals; candidate=8 new OA journals; all=both")
    parser.add_argument("--feasibility-only", action="store_true",
                        help="use per-journal target instead of discipline quotas")
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
    parser.add_argument("--contact", default="", help="optional polite contact email for User-Agent")
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
                             args.min_journals_per_discipline)):
        parser.error("Numeric limits must all be greater than zero")
    if not (0 < args.max_journal_share <= 1):
        parser.error("--max-journal-share must be in (0, 1]")
    if not (0 <= args.max_review_share <= 1):
        parser.error("--max-review-share must be in [0, 1]")
    if args.per_page > 100:
        parser.error("OpenAlex --per-page must be <= 100")
    args.out.mkdir(parents=True, exist_ok=True)
    # Fail early on whitelist problems even before any network traffic.
    journals = load_journals(args.journals_file) if "papers" in cats else []
    if args.stats_only:
        if "papers" not in cats:
            parser.error("--stats-only requires --categories papers")
        selected = select_journals(journals, args.journal_stage)
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
    client = Client(args.contact, openalex_api_key=args.openalex_api_key)
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


if __name__ == "__main__":
    main()
