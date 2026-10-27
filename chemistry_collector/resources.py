"""MIT OCW, EPA methods and optional textbook collection."""
import csv
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlparse

import requests

from .client import Client, SourceError
from .constants import (
    CHEMISTRY_BOOK_TITLE, EPA_INDEX, MIT_CHEMISTRY_SLUGS, MIT_COURSES,
    VIDEO_NON_CHEMICAL_TOPIC,
)
from .models import Journal
from .storage import download_file, hash_file, load_existing_records
from .utils import append_jsonl, course_slug, domain_matches, hostname, is_https, log_error

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

