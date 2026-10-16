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
from collections import Counter
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
    'https://ocw.mit.edu/courses/5-61-physical-chemistry-fall-2017/',
    'https://ocw.mit.edu/courses/5-310-laboratory-chemistry-fall-2019/',
    'https://ocw.mit.edu/courses/5-07sc-biological-chemistry-i-fall-2013/',
]
MIME = {
    '.pdf': 'application/pdf',
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
CATEGORIES = {'papers', 'videos', 'teaching', 'methods', 'textbooks'}


class ArxivRateLimitError(RuntimeError):
    """arXiv API is rate-limiting the client (HTTP 429)."""



# Conservative source whitelist. All listed OCW courses are chemistry-related;
# keep cross-disciplinary engineering / physics courses out of strict mode.
MIT_CHEMISTRY_SLUGS = {
    '5-111sc-principles-of-chemical-science-fall-2014': 'general_chemistry',
    '5-111-principles-of-chemical-science-fall-2008': 'general_chemistry',
    '5-112-principles-of-chemical-science-fall-2005': 'general_chemistry',
    '5-60-thermodynamics-kinetics-spring-2008': 'physical_chemistry',
    '5-61-physical-chemistry-fall-2017': 'physical_chemistry',
    '5-310-laboratory-chemistry-fall-2019': 'laboratory_chemistry',
    '5-07sc-biological-chemistry-i-fall-2013': 'biochemistry',
}

# Metadata filter, NOT a claim of full-text or video-frame verification.
# Use domain-specific signals; generic words ("study", "energy", "model") do not count.
CHEMISTRY_SIGNAL = re.compile(
    r'\b(?:chemistry|chemical(?:s|ly)?|molecul(?:e|es|ar)|reaction(?:s)?|'
    r'cataly\w*|spectroscop\w*|chromatograph\w*|electrochem\w*|'
    r'photochem\w*|thermochem\w*|solvat\w*|solvent\w*|'
    r'adsorp\w*|desorp\w*|organometal\w*|polymer\w*|'
    r'chemical\s+bond\w*|hydrogen\s+bond\w*|'
    r'quantum\s+chem\w*|ionic\s+liquid\w*|'
    r'ligand\w*|biochem\w*|protein\w*|'
    r'reagent\w*|reaction\s+kinetic\w*|'
    r'photodissociat\w*|ferrocene|esterificat\w*|'
    r'mass\s+spectrometr\w*|nuclear\s+magnetic\s+resonance|NMR|'
    r'acid.base|redox|electrolyt\w*|ioniz\w*|'
    r'chemical\s+equilibri\w*|crystal\s+field)\b',
    re.IGNORECASE
)
VIDEO_NON_CHEMICAL_TOPIC = re.compile(
    r'\b(?:writing\s+up\s+the\s+lab\s+report|ellen\s+swallow\s+richards|'
    r'how\s+to\s+use\s+this\s+course|administrative\s+announcements)\b',
    re.IGNORECASE
)
CHEMISTRY_BOOK_TITLE = re.compile(
    r'\b(?:chemistry|chemical|biochemistry|chemistry-related|'
    r'organic\s+chem|inorganic\s+chem|physical\s+chem|analytical\s+chem)\b',
    re.IGNORECASE
)


def course_slug(url: str) -> str:
    m = re.search(r'/courses/([^/]+)/', urlparse(url).path)
    return m.group(1).lower() if m else ''


def classify_candidate(item: dict, legacy_arxiv_query: str = '') -> tuple[str, str]:
    """Classify based on available metadata, without claiming PDF-content verification.

    accepted: official arXiv primary category physics.chem-ph (or other original
              accepted source categories for non-paper resources).
    likely_chemistry: positive but not independently verified evidence, e.g.
              old chem-ph search provenance or chemistry-specific title/abstract.
    rejected: explicit contrary subject information, or disallowed video course.
    needs_review: no usable evidence.
    """
    cat = item.get('category', '')
    title = (item.get('title') or '').strip()
    if cat == 'papers':
        primary = (item.get('primary_category') or '').strip()
        abstract = (item.get('abstract') or '').strip()
        words = title + ' ' + abstract
        has_chemistry_terms = bool(CHEMISTRY_SIGNAL.search(words))
        if primary == 'physics.chem-ph':
            # An arXiv primary category is useful positive discipline evidence.
            # Requiring a narrow keyword list here wrongly rejects many papers.
            return 'accepted', 'official arXiv primary_category=physics.chem-ph (metadata only)'
        if primary:
            if has_chemistry_terms:
                return ('likely_chemistry',
                        f'arXiv primary category={primary}, but chemistry terms occur in title/abstract')
            return 'rejected', f'arXiv primary category={primary}; no chemistry terms found'
        if has_chemistry_terms:
            return ('likely_chemistry',
                    'chemistry-specific terms in stored title/abstract; primary category not verified')
        if legacy_arxiv_query == 'physics.chem-ph' and title:
            return ('likely_chemistry',
                    'historical collector queried cat:physics.chem-ph; primary category not verified')
        return 'needs_review', 'no verified arXiv primary category or usable chemistry evidence'
    if cat in ('videos', 'teaching'):
        slug = course_slug(item.get('source_page', ''))
        if slug not in MIT_CHEMISTRY_SLUGS:
            return 'rejected', 'not a whitelisted MIT chemistry course'
        if VIDEO_NON_CHEMICAL_TOPIC.search(title):
            return 'rejected', 'clearly non-chemistry lesson topic'
        return 'accepted', 'MIT chemistry course (course-level evidence only)'
    if cat == 'methods':
        page = item.get('source_page', '')
        if '/hw-sw846/sw-846-test-method-' not in page:
            return 'needs_review', 'outside EPA SW-846 method catalog'
        return 'accepted', 'EPA SW-846 chemical analytical methods (source-level evidence)'
    if cat == 'textbooks':
        if CHEMISTRY_BOOK_TITLE.search(title):
            return 'accepted', 'chemical subject shown in textbook title'
        return 'needs_review', 'book title insufficient to establish chemistry subject'
    return 'needs_review', 'unknown category'


def arxiv_id_from_url(url: str) -> str:
    path = urlparse(url).path
    for prefix in ('/abs/', '/pdf/'):
        if prefix in path:
            result = path.split(prefix, 1)[1].strip('/').removesuffix('.pdf')
            return result
    return ''


def arxiv_id_base(arxiv_id: str) -> str:
    return re.sub(r'v\d+$', '', arxiv_id.strip(), flags=re.IGNORECASE)


def arxiv_entry_metadata(entry, namespaces: dict) -> dict:
    title = ' '.join(entry.findtext('a:title', '', namespaces).split())
    abstract = ' '.join(entry.findtext('a:summary', '', namespaces).split())
    primary_node = entry.find('ar:primary_category', namespaces)
    primary = (primary_node.attrib.get('term', '') if primary_node is not None else '')
    page = entry.findtext('a:id', '', namespaces)
    return {'title': title, 'abstract': abstract, 'primary_category': primary,
            'source_page': page, 'arxiv_id': arxiv_id_from_url(page)}


def arxiv_metadata_by_ids(client: Client, ids: list[str]) -> dict[str, dict]:
    """Resolve historical records via arXiv API without hammering on 429.

    This function is only used by explicit audit mode. A rate limit or
    temporary outage returns partial metadata and never modifies raw files
    or the existing manifest.
    """
    results = {}
    ns = {'a': 'http://www.w3.org/2005/Atom',
          'ar': 'http://arxiv.org/schemas/atom'}
    for start in range(0, len(ids), 10):
        batch = ids[start:start + 10]
        query_url = ARXIV_API + '?' + urlencode({
            'id_list': ','.join(batch),
            'max_results': len(batch)
        })
        try:
            response = client.get_arxiv_api(query_url)
            root = ET.fromstring(response.content)
        except ArxivRateLimitError as exc:
            print(f'[WARN] arXiv audit stopped by rate limit: {exc}. '
                  'Using local evidence for the remaining records.', flush=True)
            break
        except (requests.RequestException, ET.ParseError, RuntimeError) as exc:
            print('[WARN] arXiv metadata batch unavailable; preserving '
                  f'previous batches/local evidence: {exc}', flush=True)
            break
        for entry in root.findall('a:entry', ns):
            data = arxiv_entry_metadata(entry, ns)
            if data['arxiv_id']:
                results[arxiv_id_base(data['arxiv_id'])] = data
    return results


def local_paper_metadata(root: Path) -> dict[str, dict]:
    """Find cached paper metadata without modifying existing CSVs or PDFs."""
    lookup = {}
    for filename in ('papers_candidates.csv', 'papers_chemistry_candidates.csv'):
        path = root / filename
        if not path.is_file():
            continue
        with path.open(encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                rid = arxiv_id_base(row.get('arxiv_id', '') or arxiv_id_from_url(
                    row.get('source_page', '') or row.get('download_url', '')))
                if not rid:
                    continue
                rec = lookup.setdefault(rid, {})
                for field in ('title', 'abstract', 'primary_category', 'search_query'):
                    if row.get(field) and not rec.get(field):
                        rec[field] = row[field]
    return lookup


def audit_existing(client: Client | None, records: dict, categories: list[str],
                   root: Path, report_path: Path, *, offline: bool = False,
                   legacy_arxiv_query: str = '') -> dict[str, str]:
    """Review old files using local evidence first; never modify originals.

    Network failure must not erase local evidence. Only unresolved paper IDs
    are looked up; --audit-offline avoids all network access.
    """
    relevant = [r for r in records.values() if r.get('category') in categories]
    cached = local_paper_metadata(root)
    prepared = []
    unresolved = set()
    for record in relevant:
        item = dict(record)
        source = 'manifest'
        if item.get('category') == 'papers':
            rid = arxiv_id_base(item.get('arxiv_id', '') or arxiv_id_from_url(
                item.get('source_page', '') or item.get('download_url', '')))
            if rid in cached:
                for k, v in cached[rid].items():
                    if v and not item.get(k):
                        item[k] = v
                        source = 'manifest+local_candidates'
            origin = (item.get('search_query') or '').replace('cat:', '').strip()
            legacy = origin or legacy_arxiv_query
            if rid and not item.get('primary_category') and not offline:
                unresolved.add(rid)
            prepared.append((record, item, source, legacy, rid))
        else:
            prepared.append((record, item, source, '', ''))
    remote = {}
    if unresolved and not offline:
        try:
            remote = arxiv_metadata_by_ids(client, sorted(unresolved))
        except Exception as exc:
            print(f'[WARN] arXiv remote metadata unavailable; using local evidence: {exc}', flush=True)
    if offline:
        print('[INFO] Offline audit: no API requests, source files will not be opened or changed.', flush=True)
    rows = []
    verdicts = {}
    for rec, item, metadata_source, original_query, rid in prepared:
        if rid in remote:
            for k, v in remote[rid].items():
                if v:
                    item[k] = v
            metadata_source = 'official_arxiv_api'
        status, evidence = classify_candidate(item, original_query)
        verdicts[rec['download_url']] = status
        rows.append({
            'category': rec.get('category', ''),
            'file': rec.get('file', ''),
            'title': item.get('title', ''),
            'source_page': rec.get('source_page', ''),
            'download_url': rec.get('download_url', ''),
            'verdict': status,
            'evidence': evidence,
            'primary_category': item.get('primary_category', ''),
            'search_provenance': ('cat:' + original_query) if original_query else '',
            'metadata_source': metadata_source,
            'reviewed_at': utc_now(),
        })
    save_csv_generic(report_path, rows,
                     ['category', 'file', 'title', 'source_page', 'download_url',
                      'verdict', 'evidence', 'primary_category',
                      'search_provenance', 'metadata_source', 'reviewed_at'])
    counter = Counter(verdicts.values())
    print(f'[AUDIT] Source files and manifest untouched. {len(rows)} inspected: '
          f'{counter.get("accepted", 0)} accepted, '
          f'{counter.get("likely_chemistry", 0)} likely_chemistry, '
          f'{counter.get("rejected", 0)} rejected, '
          f'{counter.get("needs_review", 0)} needs_review. '
          f'Report: {report_path}', flush=True)
    return verdicts


def save_csv_generic(path: Path, data: list[dict], columns: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data)


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
        # Do not automatically hammer arXiv on HTTP 429. Ordinary sources
        # retry only transient 5xx responses; arXiv uses a dedicated method.
        retries = Retry(
            total=2, connect=2, read=2, status=2,
            backoff_factor=1.5,
            status_forcelist=[500, 502, 503, 504],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
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

    def _throttle(self, hostname: str, gap: float):
        delta = time.monotonic() - self.last_hit.get(hostname, 0)
        if delta < gap:
            time.sleep(gap - delta)
        self.last_hit[hostname] = time.monotonic()

    def get_arxiv_api(self, url: str, stream=False):
        """Call arXiv API conservatively and honor HTTP 429."""
        parsed = urlparse(url)
        if parsed.netloc != 'export.arxiv.org' or parsed.path != '/api/query':
            raise ValueError('Unexpected arXiv API URL')

        max_attempts = 4
        for attempt in range(max_attempts):
            self._throttle('export.arxiv.org', 4.0)
            try:
                resp = self.session.get(url, timeout=(20, 120), stream=stream)
            except requests.RequestException:
                if attempt == max_attempts - 1:
                    raise
                wait = min(60.0, 8.0 * (2 ** attempt))
                print(f'[WARN] arXiv API connection error; waiting {wait:.0f}s '
                      f'before retry {attempt + 2}/{max_attempts}.', flush=True)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get('Retry-After', '').strip()
                try:
                    wait = float(retry_after) if retry_after else min(
                        120.0, 15.0 * (2 ** attempt)
                    )
                except ValueError:
                    wait = min(120.0, 15.0 * (2 ** attempt))

                if attempt == max_attempts - 1:
                    raise ArxivRateLimitError(
                        f'HTTP 429 after {max_attempts} attempts; '
                        f'last suggested wait={wait:.0f}s'
                    )

                print(f'[WARN] arXiv API returned HTTP 429; waiting '
                      f'{wait:.0f}s before retry {attempt + 2}/{max_attempts}.',
                      flush=True)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        raise ArxivRateLimitError('arXiv API rate limit')

    def get(self, url: str, stream=False):
        if not url.startswith('https://'):
            raise ValueError('Only HTTPS sources are supported')
        parsed = urlparse(url)

        is_official_arxiv_api = (
            parsed.netloc == 'export.arxiv.org'
            and parsed.path == '/api/query'
        )
        if is_official_arxiv_api:
            return self.get_arxiv_api(url, stream=stream)

        if not self.robots_allowed(url):
            raise PermissionError(f'Disallowed by robots.txt: {url}')

        hostname = parsed.netloc.lower()
        gap = 15.5 if hostname in (
            'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'
        ) else 1.2
        self._throttle(hostname, gap)

        resp = self.session.get(url, timeout=(20, 120), stream=stream)
        if resp.status_code == 429:
            retry_after = resp.headers.get('Retry-After', '').strip()
            suffix = f' (Retry-After={retry_after})' if retry_after else ''
            raise RuntimeError(
                f'HTTP 429 from {hostname}; retry later{suffix}'
            )
        resp.raise_for_status()
        return resp

    def html(self, url):
        response = self.get(url)
        return BeautifulSoup(response.content, 'html.parser')


def paper_candidates(client: Client, wanted: int) -> Iterable[dict]:
    """Discover chemistry papers without turning an arXiv 429 into failure.

    A temporary arXiv limit stops discovery cleanly. Any files downloaded
    before the stop remain in raw/ and manifest.jsonl. Re-running later will
    skip existing URLs and continue searching from the newest results.
    """
    fetched = 0
    search_pool = min(max(wanted * 6, 150), 1800)
    batch_size = 50

    while fetched < search_pool:
        batch_size = min(batch_size, search_pool - fetched)
        params = {
            'search_query': 'cat:physics.chem-ph',
            'start': fetched,
            'max_results': batch_size,
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
        }
        request_url = ARXIV_API + '?' + urlencode(params)

        try:
            data = client.get_arxiv_api(request_url).content
        except ArxivRateLimitError as exc:
            print(f'[WARN] arXiv discovery paused: {exc}. '
                  'Existing files are preserved; re-run later to continue.',
                  flush=True)
            return
        except requests.RequestException as exc:
            print(f'[WARN] arXiv discovery temporarily unavailable: {exc}. '
                  'Existing files are preserved; re-run later.', flush=True)
            return

        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            print(f'[WARN] arXiv returned invalid XML: {exc}. '
                  'Discovery stopped; existing files are preserved.', flush=True)
            return

        atom = {'a': 'http://www.w3.org/2005/Atom',
                'ar': 'http://arxiv.org/schemas/atom'}
        entries = root.findall('a:entry', atom)
        if not entries:
            return

        for e in entries:
            title = ' '.join(e.findtext('a:title', '', atom).split())
            page = e.findtext('a:id', '', atom)
            published = e.findtext('a:published', '', atom)
            doi = e.findtext('ar:doi', '', atom)
            link = next(
                (n.attrib.get('href', '') for n in e.findall('a:link', atom)
                 if n.attrib.get('title') == 'pdf'
                 or n.attrib.get('type') == 'application/pdf'),
                None
            )
            if not link:
                continue
            if link.startswith('http://'):
                link = 'https://' + link[len('http://'):]
            if not urlparse(link).path.lower().endswith('.pdf'):
                link = link.rstrip('/') + '.pdf'

            meta = arxiv_entry_metadata(e, atom)
            yield {
                'category': 'papers',
                'resource_kind': 'research_article',
                'title': title,
                'source_page': page,
                'download_url': link,
                'publisher': 'arXiv',
                'license_note': 'CHECK_ARTICLE_SPECIFIC_LICENSE',
                'published_at': published,
                'doi': doi,
                'primary_category': meta['primary_category'],
                'abstract': meta['abstract'],
                'arxiv_id': meta['arxiv_id'],
                'search_query': 'cat:physics.chem-ph',
            }

        fetched += len(entries)
        if len(entries) < batch_size:
            return


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
            'download_url', 'license_note', 'published_at', 'doi',
            'primary_category', 'abstract', 'arxiv_id', 'search_query', 'discipline',
            'discipline_evidence', 'discipline_verification_level']
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
    ap.add_argument('--audit-only', action='store_true',
                    help='Audit existing downloaded files only; do not discover or download')
    ap.add_argument('--audit-offline', action='store_true',
                    help='Audit using existing manifest/candidates only; no arXiv API')
    ap.add_argument('--legacy-arxiv-query', choices=['physics.chem-ph'],
                    default='physics.chem-ph',
                    help='Local provenance used for legacy arXiv papers without primary_category')
    ap.add_argument('--audit-existing', action='store_true',
                    help='Audit existing files before collection; may call arXiv API')
    ap.add_argument('--no-audit', action='store_true',
                    help='Compatibility flag; normal collection is already audit-free')
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
    # IMPORTANT: normal collection does NOT audit historical files online.
    # This prevents an arXiv 429 from blocking an otherwise valid collection.
    # Use --audit-only or --audit-existing when you explicitly want an audit.
    if args.audit_only:
        reviewed = audit_existing(
            client, successes, cats, args.out,
            args.out / 'chemistry_audit.csv',
            offline=args.audit_offline,
            legacy_arxiv_query=args.legacy_arxiv_query
        )
    elif args.audit_existing and not args.no_audit:
        reviewed = audit_existing(
            client, successes, cats, args.out,
            args.out / 'chemistry_audit.csv',
            offline=args.audit_offline,
            legacy_arxiv_query=args.legacy_arxiv_query
        )
    else:
        reviewed = {}

    if args.audit_only:
        return

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
            # A probable legacy record still occupies storage and counts toward
            # the requested file count, but it is NOT marked 'accepted'. This
            # prevents silently re-downloading dozens of extra PDFs after an
            # arXiv metadata service outage. Read chemistry_audit.csv to see
            # the accepted/likely distinction.
            def local_status(record):
                """Classify existing records using local provenance only."""
                rcat = record.get('category', '')

                if rcat == 'papers':
                    primary = (record.get('primary_category') or '').strip()
                    if primary == 'physics.chem-ph':
                        return 'accepted'

                    query = (record.get('search_query') or '').strip()
                    if query == 'cat:physics.chem-ph':
                        return 'likely_chemistry'

                    # Backward compatibility for the original project, whose
                    # paper manifest may not have persisted search_query.
                    if args.legacy_arxiv_query == 'physics.chem-ph':
                        page = record.get('source_page', '')
                        if ('arxiv.org' in page or 'export.arxiv.org' in page):
                            if arxiv_id_from_url(page):
                                return 'likely_chemistry'
                    return ''

                if rcat in ('videos', 'teaching'):
                    return (
                        'accepted'
                        if course_slug(record.get('source_page', ''))
                        in MIT_CHEMISTRY_SLUGS
                        else ''
                    )

                if rcat == 'methods':
                    return (
                        'accepted'
                        if '/hw-sw846/sw-846-test-method-' in
                        record.get('source_page', '')
                        else ''
                    )

                if rcat == 'textbooks':
                    return (
                        'accepted'
                        if CHEMISTRY_BOOK_TITLE.search(record.get('title', ''))
                        else ''
                    )

                return ''

            local_statuses = {
                url: (reviewed.get(url) or local_status(record))
                for url, record in successes.items()
            }

            completed = sum(
                1 for url, v in successes.items()
                if v['category'] == cat
                and local_statuses.get(url) in ('accepted', 'likely_chemistry')
            )
            probable_count = sum(
                1 for url, v in successes.items()
                if v['category'] == cat
                and local_statuses.get(url) == 'likely_chemistry'
            )
            if probable_count:
                print(f'[{cat}] {probable_count} old files are LIKELY chemistry '
                      '(not verified by primary category). They count toward '
                      'the file-count cap, not toward independently verified '
                      'chemistry totals.', flush=True)
            if args.download and completed >= args.limit:
                print(f'[{cat}] Existing chemistry-relevant file count '
                      f'{completed} already reaches target {args.limit}; '
                      'no new discovery/download is necessary.', flush=True)
                continue
            candidates = []
            rejected = []
            seen_urls = set()
            for item in iterator:
                url = item['download_url']
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                status, evidence = classify_candidate(item)
                if status != 'accepted':
                    rejected.append({**item, 'filter_status': status,
                                     'filter_reason': evidence})
                    continue
                item = {**item, 'discipline': 'Chemistry',
                        'discipline_evidence': evidence,
                        'discipline_verification_level':
                        'course_metadata' if cat in ('videos', 'teaching')
                        else 'article_metadata' if cat == 'papers'
                        else 'source_metadata'}
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
            # Different filenames preserve pre-existing *_candidates.csv.
            save_csv(args.out / f'{cat}_chemistry_candidates.csv', candidates)
            save_csv_generic(args.out / f'{cat}_filtered_out.csv', rejected,
                             ['category', 'title', 'source_page', 'download_url',
                              'filter_status', 'filter_reason', 'primary_category'])
            print(f'[{cat}] Strict chemistry candidates: {len(candidates)}; '
                  f'excluded/review: {len(rejected)}; '
                  f'accepted completed files: {completed}', flush=True)
            if args.download and completed < args.limit:
                print(f'[WARN] Only {completed}/{args.limit} successful files: no fabricated placeholders.', flush=True)
        except Exception as exc:
            discovery_errors += 1
            jsonl_append(errors_file, {'category': cat, 'phase': 'discovery',
                                       'error': str(exc), 'at': utc_now()})
            print(f'[ERROR] discovery failed for {cat}: {exc}', flush=True)
    print('\nDone. Original raw files and existing manifest records preserved; strict reports saved separately.', flush=True)
    if discovery_errors:
        raise SystemExit(1)  # Signal a failed discovery to PyCharm / automation.


if __name__ == '__main__':
    main()
