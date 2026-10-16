#!/usr/bin/env python3
"""Chemistry original-file collector: arXiv, MIT OCW and US EPA SW-846.

Examples:
    python collector.py --categories papers,videos,teaching,methods --limit 100
    python collector.py --categories papers --limit 100 --download

No paywalls, streaming extraction, HTML->PDF, transcodes or chemical-entity scraping.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Dict, Iterable
from urllib.parse import urljoin, urlparse, urlencode
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ARXIV_API = 'https://export.arxiv.org/api/query'
EPA_INDEX = 'https://www.epa.gov/hw-sw846/sw-846-compendium'

MIT_COURSES = [
    'https://ocw.mit.edu/courses/5-111sc-principles-of-chemical-science-fall-2014/',
    'https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/',
    'https://ocw.mit.edu/courses/5-112-principles-of-chemical-science-fall-2005/',
    'https://ocw.mit.edu/courses/5-60-thermodynamics-kinetics-spring-2008/',

    # 新增加的化学课程
    'https://ocw.mit.edu/courses/5-61-physical-chemistry-fall-2017/',
    'https://ocw.mit.edu/courses/5-310-laboratory-chemistry-fall-2019/',
    'https://ocw.mit.edu/courses/3-091sc-introduction-to-solid-state-chemistry-fall-2010/',
]
MIME = {
    '.pdf': 'application/pdf',
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
CATEGORIES = {'papers', 'videos', 'teaching', 'methods', 'textbooks'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def jsonl_append(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False) + '\n')


def key_for(url: str):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:18]


def acceptable_link(url, suffix):
    p = urlparse(url)
    return p.scheme == 'https' and p.path.lower().endswith(suffix)


class Client:
    def __init__(self, contact: str, respect_robots=True):
        self.session = requests.Session()
        self.ua = f'AcademicChemistrySourceCollector/1.0 (contact: {contact})'
        self.session.headers.update({'User-Agent': self.ua})
        retries = Retry(total=3, backoff_factor=1.2,
                        status_forcelist=[429, 500, 502, 503, 504],
                        respect_retry_after_header=True)
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.respect_robots = respect_robots
        self.robots: Dict[str, RobotFileParser] = {}
        self.last_hit: Dict[str, float] = {}

    def robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin not in self.robots:
            robot_url = origin + '/robots.txt'
            try:
                r = self.session.get(robot_url, timeout=(10, 25))
                if r.status_code == 404:
                    # A missing robots.txt imposes no extra exclusion.
                    rp = RobotFileParser()
                    rp.parse([])
                elif r.ok:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
                else:
                    raise RuntimeError(f'robots status {r.status_code}')
                self.robots[origin] = rp
            except requests.RequestException as exc:
                raise RuntimeError(f'Cannot verify {robot_url}: {exc}') from exc
        return self.robots[origin].can_fetch(self.ua, url)

    def get(self, url: str, stream=False):
        if not url.startswith('https://'):
            raise ValueError('Only HTTPS sources are supported')
        parsed = urlparse(url)
        # The documented arXiv Search API is a programmatic interface, not
        # a generic website crawl. Its own terms expressly allow API access.
        # Only exempt this exact HTTPS endpoint from the generic robots check;
        # every other URL, including arXiv PDF URLs, is still checked.
        is_official_arxiv_api = (
            parsed.scheme == 'https'
            and parsed.netloc == 'export.arxiv.org'
            and parsed.path == '/api/query'
        )
        if not is_official_arxiv_api and not self.robots_allowed(url):
            raise PermissionError(f'Disallowed by robots.txt: {url}')
        hostname = parsed.netloc.lower()
        if is_official_arxiv_api:
            gap = 3.5  # Official arXiv API: at most one request per three seconds.
        elif hostname in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org'):
            gap = 15.5  # Respect arXiv's generic robots.txt crawl-delay for PDFs.
        else:
            gap = 1.2
        delta = time.monotonic() - self.last_hit.get(hostname, 0)
        if delta < gap:
            time.sleep(gap - delta)
        self.last_hit[hostname] = time.monotonic()
        resp = self.session.get(url, timeout=(20, 120), stream=stream)
        resp.raise_for_status()
        return resp

    def html(self, url):
        response = self.get(url)
        return BeautifulSoup(response.content, 'html.parser')


def paper_candidates(client: Client, wanted: int) -> Iterable[dict]:
    # arXiv chem-ph is a physical chemistry research category, not all chemistry.
    fetched = 0
    while fetched < max(wanted * 2, 150):
        batch_size = min(100, max(wanted * 2, 150) - fetched)
        params = {
            'search_query': 'cat:physics.chem-ph',
            'start': fetched,
            'max_results': batch_size,
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
        }
        request_url = ARXIV_API + '?' + urlencode(params)
        data = client.get(request_url).content
        root = ET.fromstring(data)
        atom = {'a': 'http://www.w3.org/2005/Atom',
                'ar': 'http://arxiv.org/schemas/atom'}
        entries = root.findall('a:entry', atom)
        if not entries:
            break
        for e in entries:
            title = ' '.join((e.findtext('a:title', '', atom)).split())
            page = e.findtext('a:id', '', atom)
            published = e.findtext('a:published', '', atom)
            doi = e.findtext('ar:doi', '', atom)
            link = next((n.attrib.get('href', '') for n in e.findall('a:link', atom)
                         if n.attrib.get('title') == 'pdf' or n.attrib.get('type') == 'application/pdf'), None)
            if not link:
                continue
            if link.startswith('http://'):
                link = 'https://' + link[len('http://'):]
            if not urlparse(link).path.lower().endswith('.pdf'):
                link = link.rstrip('/') + '.pdf'
            yield {
                'category': 'papers', 'resource_kind': 'research_article',
                'title': title, 'source_page': page, 'download_url': link,
                'publisher': 'arXiv', 'license_note': 'CHECK_ARTICLE_SPECIFIC_LICENSE',
                'published_at': published, 'doi': doi,
            }
        fetched += len(entries)
        if len(entries) < batch_size:
            break


def mit_candidates(client: Client, category: str) -> Iterable[dict]:
    endpoint = 'lecture-videos' if category == 'videos' else 'lecture-notes'
    suffix = '.mp4' if category == 'videos' else '.pdf'
    visited_files = set()
    for root in MIT_COURSES:
        # The download page is important: the MP4 links point directly to archive.org.
        pages = [urljoin(root, 'resources/' + endpoint + '/'),
                 urljoin(root, 'download/')]
        for page in pages:
            try:
                soup = client.html(page)
            except (requests.RequestException, PermissionError, RuntimeError) as exc:
                print(f'[WARN] MIT source page {page}: {exc}', flush=True)
                continue
            for anchor in soup.select('a[href]'):
                url = urljoin(page, anchor.get('href', ''))
                if not acceptable_link(url, suffix):
                    continue
                host = urlparse(url).hostname or ''
                if category == 'videos' and not host.endswith('archive.org'):
                    continue
                if category == 'teaching' and not host.endswith('ocw.mit.edu'):
                    continue
                if url in visited_files:
                    continue
                visited_files.add(url)
                stem = Path(urlparse(url).path).stem
                label = anchor.get_text(' ', strip=True)
                yield {
                    'category': category,
                    'resource_kind': 'course_video' if category == 'videos' else 'lecture_notes',
                    'title': f'{root.rstrip("/").split("/")[-1]} - {stem} - {label}'[:240],
                    'source_page': page, 'download_url': url,
                    'publisher': 'MIT OpenCourseWare',
                    'license_note': 'MIT_OCW_CC_BY_NC_SA_4_0_CHECK_THIRD_PARTY_NOTICES',
                    'published_at': '', 'doi': '',
                }


def method_candidates(client: Client) -> Iterable[dict]:
    soup = client.html(EPA_INDEX)
    seen_pages = set()
    for a in soup.select('a[href]'):
        page = urljoin(EPA_INDEX, a.get('href', ''))
        p = urlparse(page)
        if p.hostname != 'www.epa.gov' or '/hw-sw846/sw-846-test-method-' not in p.path:
            continue
        if page in seen_pages:
            continue
        seen_pages.add(page)
        try:
            detail = client.html(page)
        except (requests.RequestException, PermissionError, RuntimeError) as exc:
            print(f'[WARN] EPA method page {page}: {exc}', flush=True)
            continue
        name = detail.h1.get_text(' ', strip=True) if detail.h1 else a.get_text(' ', strip=True)
        for anchor in detail.select('a[href]'):
            pdf = urljoin(page, anchor.get('href', ''))
            if acceptable_link(pdf, '.pdf') and (urlparse(pdf).hostname or '').endswith('epa.gov'):
                yield {
                    'category': 'methods', 'resource_kind': 'analytical_test_method',
                    'title': name, 'source_page': page, 'download_url': pdf,
                    'publisher': 'US EPA',
                    'license_note': 'US_GOV_WORK_CHECK_THIRD_PARTY_MATERIAL_AND_CURRENT_STATUS',
                    'published_at': '', 'doi': '',
                }
                # An individual method may include reference PDFs; keep only its primary file.
                break


def textbook_candidates(seed_file: Path) -> Iterable[dict]:
    """Optional actual full books: manually vetted, official direct-PDF URLs only."""
    if not seed_file.exists():
        print(f'[INFO] No optional book seed file at {seed_file}', flush=True)
        return
    with seed_file.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            url = (row.get('download_url') or '').strip()
            if not acceptable_link(url, '.pdf'):
                continue
            yield {
                'category': 'textbooks', 'resource_kind': 'whole_textbook',
                'title': row.get('title', ''), 'source_page': row.get('source_page', ''),
                'download_url': url, 'publisher': row.get('publisher', ''),
                'license_note': row.get('license_note', 'LICENSE_REVIEW_REQUIRED'),
                'published_at': row.get('published_at', ''), 'doi': '',
            }


def signature_ok(path: Path, ext: str) -> bool:
    with path.open('rb') as f:
        header = f.read(16)
    if ext == '.pdf':
        return header.startswith(b'%PDF-')
    if ext == '.mp4':
        return header[4:8] == b'ftyp'
    if ext == '.webm':
        return header.startswith(b'\x1a\x45\xdf\xa3')
    if ext == '.docx':
        return header.startswith(b'PK\x03\x04')
    if ext == '.doc':
        return header.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')
    return False


def download_one(client: Client, item: dict, root: Path, max_mb: int) -> dict:
    url = item['download_url']
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in MIME:
        raise ValueError('Unsupported source extension: ' + ext)
    folder = root / 'raw' / item['category']
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (item['category'] + '_' + key_for(url) + ext)
    part = target.with_suffix(target.suffix + '.part')
    max_bytes = max_mb * 1024 * 1024
    sha = hashlib.sha256()
    total = 0
    try:
        with client.get(url, stream=True) as resp:
            n = int(resp.headers.get('Content-Length', '0'))
            if n > max_bytes:
                raise ValueError(f'Over {max_mb}MB per-file limit')
            ct = resp.headers.get('Content-Type', '').split(';')[0].lower()
            if ct.startswith('text/html') or ct.startswith('application/json'):
                raise ValueError('Got an HTML/JSON error page, not an original media file')
            with part.open('wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f'Exceeded {max_mb}MB per-file limit')
                    f.write(chunk)
                    sha.update(chunk)
        if not signature_ok(part, ext):
            raise ValueError(f'File signature does not match {ext}')
        part.replace(target)
        return {
            **item, 'file': str(target.relative_to(root)), 'bytes': total,
            'sha256': sha.hexdigest(), 'mime_expected': MIME[ext],
            'collected_at': utc_now(), 'status': 'downloaded',
        }
    except Exception:
        part.unlink(missing_ok=True)
        raise


def previous_success(manifest: Path, root: Path):
    done = {}
    if manifest.exists():
        for line in manifest.read_text(encoding='utf-8').splitlines():
            try:
                item = json.loads(line)
                if item.get('status') == 'downloaded' and (root / item['file']).is_file():
                    done[item['download_url']] = item
            except (ValueError, KeyError):
                pass
    return done


def save_csv(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ['category', 'resource_kind', 'title', 'publisher', 'source_page',
            'download_url', 'license_note', 'published_at', 'doi']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--categories', default='papers,videos,teaching,methods',
                    help='papers,videos,teaching,methods,textbooks')
    ap.add_argument('--limit', type=int, default=100, help='successful files PER category')
    ap.add_argument('--download', action='store_true',
                    help='Without this flag, only discover URLs and write candidates.csv')
    ap.add_argument('--out', type=Path, default=Path('chemistry_dataset'))
    ap.add_argument('--contact', default='your-email@example.org',
                    help='Use a real contact email in your crawler User-Agent')
    ap.add_argument('--max-file-mb', type=int, default=500)
    ap.add_argument('--books-csv', type=Path, default=Path('textbook_seeds.csv'))
    args = ap.parse_args()
    cats = [x.strip() for x in args.categories.split(',') if x.strip()]
    unknown = set(cats) - CATEGORIES
    if unknown:
        ap.error('Unknown categories: ' + ','.join(unknown))
    if args.limit < 1:
        ap.error('--limit must be >= 1')
    client = Client(contact=args.contact)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / 'manifest.jsonl'
    successes = previous_success(manifest, args.out)
    errors_file = args.out / 'errors.jsonl'
    discovery_errors = 0

    for cat in cats:
        print(f'\n=== {cat} | target={args.limit} ===', flush=True)
        try:
            if cat == 'papers':
                iterator = paper_candidates(client, args.limit)
            elif cat in ('videos', 'teaching'):
                iterator = mit_candidates(client, cat)
            elif cat == 'methods':
                iterator = method_candidates(client)
            else:
                iterator = textbook_candidates(args.books_csv)
            completed = sum(1 for v in successes.values() if v['category'] == cat)
            candidates = []
            seen_urls = set()
            for item in iterator:
                url = item['download_url']
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append(item)
                if args.download:
                    if completed >= args.limit:
                        break
                    if url in successes:
                        continue
                    try:
                        record = download_one(client, item, args.out, args.max_file_mb)
                        jsonl_append(manifest, record)
                        successes[url] = record
                        completed += 1
                        print(f'[{cat}] {completed}/{args.limit}: {record["file"]} ({record["bytes"]:,} bytes)', flush=True)
                    except Exception as exc:
                        jsonl_append(errors_file, {
                            'category': cat, 'download_url': url,
                            'error': str(exc), 'at': utc_now(),
                        })
                        print(f'[SKIP] {url}: {exc}', flush=True)
                elif len(candidates) >= args.limit + 30:
                    break
            save_csv(args.out / f'{cat}_candidates.csv', candidates)
            print(f'[{cat}] Candidate URLs: {len(candidates)}; verified downloaded files: {completed}', flush=True)
            if args.download and completed < args.limit:
                print(f'[WARN] Only {completed}/{args.limit} successful files: no fabricated placeholders.', flush=True)
        except Exception as exc:
            discovery_errors += 1
            jsonl_append(errors_file, {'category': cat, 'phase': 'discovery',
                                       'error': str(exc), 'at': utc_now()})
            print(f'[ERROR] discovery failed for {cat}: {exc}', flush=True)
    print('\nDone. raw/ stores unchanged source bytes; manifest.jsonl holds provenance & SHA256.', flush=True)
    if discovery_errors:
        raise SystemExit(1)  # Signal a failed discovery to PyCharm / automation.


if __name__ == '__main__':
    main()
