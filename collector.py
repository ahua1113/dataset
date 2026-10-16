#!/usr/bin/env python3
"""
Chemistry Source Collector V5.1.

V5.1 fixes OpenAlex journal filtering and strengthens balanced multi-source
round-robin scheduling. A source that returns HTTP 429 is disabled only for
the current run. Existing raw files and manifest records are preserved.

Paper sources:
  arxiv   -> arXiv physics.chem-ph
  nature  -> Nature Portfolio journals via OpenAlex
  science -> Science family / Science Advances via OpenAlex

Dedup across paper sources:
  DOI -> arXiv ID -> OpenAlex ID -> normalized title -> URL
After download, SHA-256 is checked as a final content-level duplicate guard.

Examples:
  python collector.py --categories papers --limit 120 --download
  python collector.py --categories papers --limit 120 --download \
      --paper-sources arxiv,nature,science \
      --source-targets arxiv=40,nature=40,science=40
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Iterable
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
OPENALEX_SOURCES_API = "https://api.openalex.org/sources"
EPA_INDEX = "https://www.epa.gov/hw-sw846/sw-846-compendium"

CATEGORIES = {"papers", "videos", "teaching", "methods", "textbooks"}

PAPER_SOURCES = {
    "nature": {
        "label": "Nature Portfolio",
        "publisher": "Nature Portfolio",
        "journals": [
            ("Nature Chemistry", "1755-4349",
             "https://www.nature.com/nchem/", "chemistry_core"),
            ("Communications Chemistry", "2399-3669",
             "https://www.nature.com/commschem/", "chemistry_core"),
            ("Nature Catalysis", "2520-1158",
             "https://www.nature.com/natcatal/", "chemistry_core"),
            ("Nature Synthesis", "2731-0582",
             "https://www.nature.com/natsynth/", "chemistry_core"),
            ("Nature Chemical Biology", "1552-4469",
             "https://www.nature.com/nchembio/", "chemistry_core"),
            ("Nature Communications", "2041-1723",
             "https://www.nature.com/ncomms/", "chemistry_broad"),
        ],
    },
    "science": {
        "label": "Science family",
        "publisher": "AAAS / Science family",
        "journals": [
            ("Science Advances", "2375-2548",
             "https://www.science.org/journal/sciadv", "chemistry_broad"),
        ],
    },
}

OA_BROAD_SEARCH = (
    "chemistry OR chemical OR molecular OR catalysis OR spectroscopy "
    "OR synthesis OR electrochemistry OR polymer OR materials "
    "OR biochemistry OR reaction OR ligand OR solvent"
)

CHEMISTRY_SIGNAL = re.compile(
    r"\b(?:chemistry|chemical(?:s|ly)?|molecul(?:e|es|ar)|"
    r"reaction(?:s)?|cataly\w*|spectroscop\w*|chromatograph\w*|"
    r"electrochem\w*|photochem\w*|thermochem\w*|solvat\w*|solvent\w*|"
    r"adsorp\w*|desorp\w*|organometal\w*|polymer\w*|"
    r"chemical\s+bond\w*|hydrogen\s+bond\w*|quantum\s+chem\w*|"
    r"ionic\s+liquid\w*|ligand\w*|biochem\w*|reagent\w*|"
    r"reaction\s+kinetic\w*|photodissociat\w*|ferrocene|"
    r"esterificat\w*|mass\s+spectrometr\w*|nuclear\s+magnetic\s+resonance|"
    r"\bNMR\b|acid[\s.-]*base|redox|electrolyt\w*|ioniz\w*|"
    r"chemical\s+equilibri\w*|crystal\s+field)\b",
    re.IGNORECASE,
)

CHEMISTRY_BOOK_TITLE = re.compile(
    r"\b(?:chemistry|chemical|biochemistry|organic\s+chem|"
    r"inorganic\s+chem|physical\s+chem|analytical\s+chem)\b",
    re.IGNORECASE,
)

VIDEO_NON_CHEMICAL_TOPIC = re.compile(
    r"\b(?:writing\s+up\s+the\s+lab\s+report|"
    r"ellen\s+swallow\s+richards|how\s+to\s+use\s+this\s+course|"
    r"administrative\s+announcements)\b",
    re.IGNORECASE,
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
    "5-111sc-principles-of-chemical-science-fall-2014": "general_chemistry",
    "5-111-principles-of-chemical-science-fall-2008": "general_chemistry",
    "5-112-principles-of-chemical-science-fall-2005": "general_chemistry",
    "5-60-thermodynamics-kinetics-spring-2008": "physical_chemistry",
    "5-61-physical-chemistry-fall-2017": "physical_chemistry",
    "5-310-laboratory-chemistry-fall-2019": "laboratory_chemistry",
    "5-07sc-biological-chemistry-i-fall-2013": "biochemistry",
}

MIME = {
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class SourceTemporarilyUnavailable(RuntimeError):
    """The source is temporarily unavailable, typically due to HTTP 429."""
    pass


class SourceQueryFailed(RuntimeError):
    """The source endpoint rejected or could not satisfy the query."""
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://doi\.org/", "", value)
    return value.rstrip(" .;,")


def normalize_title(value: str) -> str:
    value = (value or "").casefold().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def arxiv_id_from_url(url: str) -> str:
    path = urlparse(url).path
    for prefix in ("/abs/", "/pdf/"):
        if prefix in path:
            return path.split(prefix, 1)[1].strip("/").removesuffix(".pdf")
    return ""


def arxiv_id_base(value: str) -> str:
    return re.sub(r"v\d+$", "", (value or "").strip(), flags=re.I)


def course_slug(url: str) -> str:
    match = re.search(r"/courses/([^/]+)/", urlparse(url).path)
    return match.group(1).lower() if match else ""


def key_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:18]


def acceptable_link(url: str, suffix: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and p.path.lower().endswith(suffix)


def reconstruct_abstract(work: dict) -> str:
    inv = work.get("abstract_inverted_index") or {}
    if not isinstance(inv, dict):
        return ""
    tokens = []
    for word, positions in inv.items():
        if isinstance(positions, list):
            for pos in positions:
                try:
                    tokens.append((int(pos), str(word)))
                except (TypeError, ValueError):
                    pass
    tokens.sort(key=lambda x: x[0])
    return " ".join(word for _, word in tokens)


class Client:
    def __init__(self, contact: str, respect_robots: bool = True):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"AcademicChemistrySourceCollector/5.0 (contact: {contact})"
        })
        retry = Retry(
            total=2, connect=2, read=2, status=2,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.respect_robots = respect_robots
        self.robots: dict[str, RobotFileParser] = {}
        self.last_hit: dict[str, float] = {}
        self.openalex_source_cache: dict[str, dict] = {}

    def _throttle(self, host: str, gap: float) -> None:
        delta = time.monotonic() - self.last_hit.get(host, 0.0)
        if delta < gap:
            time.sleep(gap - delta)
        self.last_hit[host] = time.monotonic()

    def robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            robot_url = origin + "/robots.txt"
            try:
                r = self.session.get(robot_url, timeout=(10, 20))
                if r.status_code == 404:
                    rp = RobotFileParser()
                    rp.parse([])
                elif r.ok:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
                else:
                    raise RuntimeError(f"robots status {r.status_code}")
                self.robots[origin] = rp
            except requests.RequestException as exc:
                raise RuntimeError(f"Cannot verify {robot_url}: {exc}") from exc
        return self.robots[origin].can_fetch(
            self.session.headers["User-Agent"], url
        )

    def get(self, url: str, *, stream: bool = False, api_name: str = ""):
        if not url.startswith("https://"):
            raise ValueError("Only HTTPS sources are supported")
        host = urlparse(url).netloc.lower()

        if api_name:
            # API discovery must fail fast. In particular, do not sleep for
            # long periods after HTTP 429: the scheduler can pause only the
            # affected source and continue with the others.
            self._throttle(host, 1.0)
            for attempt in range(2):
                try:
                    response = self.session.get(
                        url, timeout=(12, 45), stream=stream
                    )
                except requests.RequestException as exc:
                    if attempt == 0:
                        print(
                            f"[WARN] {api_name} connection error; retry once in 1s.",
                            flush=True,
                        )
                        time.sleep(1)
                        continue
                    raise SourceTemporarilyUnavailable(
                        f"{api_name} connection failure: {exc}"
                    ) from exc

                if response.status_code == 429:
                    response.close()
                    raise SourceTemporarilyUnavailable(
                        f"{api_name} HTTP 429; no long retry in this run"
                    )

                if 400 <= response.status_code < 500:
                    body = response.text[:240].replace("\n", " ")
                    response.close()
                    raise SourceQueryFailed(
                        f"{api_name} HTTP {response.status_code}: {body}"
                    )

                response.raise_for_status()
                return response

            raise SourceTemporarilyUnavailable(f"{api_name} unavailable")

        if not self.robots_allowed(url):
            raise PermissionError(f"Disallowed by robots.txt: {url}")

        gap = 4.0 if "arxiv.org" in host else 1.2
        self._throttle(host, gap)
        response = self.session.get(url, timeout=(20, 120), stream=stream)
        if response.status_code == 429:
            raise SourceTemporarilyUnavailable(f"{host} HTTP 429")
        response.raise_for_status()
        return response

    def json(self, url: str, api_name: str) -> dict:
        return self.get(url, api_name=api_name).json()

    def html(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.get(url).content, "html.parser")


def arxiv_entry_metadata(entry, ns: dict) -> dict:
    title = " ".join(entry.findtext("a:title", "", ns).split())
    abstract = " ".join(entry.findtext("a:summary", "", ns).split())
    node = entry.find("ar:primary_category", ns)
    primary = node.attrib.get("term", "") if node is not None else ""
    page = entry.findtext("a:id", "", ns)
    doi = entry.findtext("ar:doi", "", ns) or ""
    return {
        "title": title,
        "abstract": abstract,
        "primary_category": primary,
        "source_page": page,
        "arxiv_id": arxiv_id_from_url(page),
        "doi": doi,
    }


def arxiv_candidates(client: Client, pool_size: int) -> Iterable[dict]:
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "ar": "http://arxiv.org/schemas/atom",
    }
    start = 0
    batch = 25

    while start < pool_size:
        size = min(batch, pool_size - start)
        params = {
            "search_query": "cat:physics.chem-ph",
            "start": start,
            "max_results": size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = ARXIV_API + "?" + urlencode(params)

        try:
            root = ET.fromstring(client.get(url, api_name="arXiv").content)
        except SourceTemporarilyUnavailable as exc:
            raise SourceTemporarilyUnavailable(str(exc))
        except (requests.RequestException, ET.ParseError) as exc:
            raise SourceTemporarilyUnavailable(f"arXiv discovery error: {exc}")

        entries = root.findall("a:entry", ns)
        if not entries:
            return

        for entry in entries:
            meta = arxiv_entry_metadata(entry, ns)
            pdf = ""
            for link in entry.findall("a:link", ns):
                href = link.attrib.get("href", "")
                if (
                    link.attrib.get("title") == "pdf"
                    or link.attrib.get("type") == "application/pdf"
                ):
                    pdf = href
                    break
            if not pdf:
                continue
            if pdf.startswith("http://"):
                pdf = "https://" + pdf[7:]
            if not pdf.lower().endswith(".pdf"):
                pdf = pdf.rstrip("/") + ".pdf"

            yield {
                "category": "papers",
                "resource_kind": "research_article",
                "title": meta["title"],
                "source_page": meta["source_page"],
                "download_url": pdf,
                "publisher": "arXiv",
                "journal": "",
                "journal_issn": "",
                "license_note": "CHECK_ARTICLE_SPECIFIC_LICENSE",
                "published_at": entry.findtext("a:published", "", ns),
                "doi": meta["doi"],
                "primary_category": meta["primary_category"],
                "abstract": meta["abstract"],
                "arxiv_id": meta["arxiv_id"],
                "search_query": "cat:physics.chem-ph",
                "oa_status": "",
                "oa_verified": True,
                "oa_pdf_source": "arXiv public PDF endpoint",
                "oa_verification_level": "arxiv_public_pdf_endpoint",
                "openalex_id": "",
                "discipline": "Chemistry",
                "discipline_evidence": (
                    "official arXiv primary_category=physics.chem-ph"
                ),
                "discipline_verification_level": "source_category",
                "source_group": "arxiv",
            }

        start += len(entries)
        if len(entries) < size:
            return


def openalex_pdf_url(work: dict) -> str:
    best = work.get("best_oa_location") or {}
    pdf = (best.get("pdf_url") or "").strip()
    if pdf:
        return pdf
    for loc in work.get("locations") or []:
        if loc and loc.get("is_oa") and loc.get("pdf_url"):
            return str(loc["pdf_url"]).strip()
    return ""


def publisher_is_chemistry(work: dict, journal: dict) -> tuple[bool, str]:
    title = work.get("title", "") or ""
    abstract = reconstruct_abstract(work)
    topic_text = openalex_topic_text(work)
    text = f"{title} {abstract} {topic_text}"

    if journal["scope"] == "chemistry_core":
        return True, (
            f'ISSN-verified chemistry journal: {journal["name"]}'
        )

    hits = CHEMISTRY_SIGNAL.findall(text)
    if hits:
        return True, (
            f'broad journal {journal["name"]} + chemistry metadata signal '
            f"({len(hits)} match(es))"
        )

    return False, (
        f'broad journal {journal["name"]} without chemistry signal '
        "in title/abstract/OpenAlex topic metadata"
    )


def openalex_resolve_source(
    client: Client,
    issn: str,
    journal_name: str,
) -> dict:
    """
    Resolve a journal ISSN to its OpenAlex Source entity.

    Important: ISSN is a valid identifier for the /sources endpoint, but
    primary_location.source.issn is not a supported Works filter. The
    correct two-step route is ISSN -> Source ID -> Works filter by
    primary_location.source.id.
    """
    issn = (issn or "").strip()
    if not issn:
        raise SourceQueryFailed(
            f"{journal_name}: empty ISSN in PAPER_SOURCES"
        )

    cached = client.openalex_source_cache.get(issn)
    if cached:
        return cached

    url = f"{OPENALEX_SOURCES_API}/issn:{issn}"
    try:
        data = client.json(url, "OpenAlex Sources")
    except SourceTemporarilyUnavailable:
        raise
    except (SourceQueryFailed, requests.RequestException, ValueError) as exc:
        raise SourceQueryFailed(
            f"{journal_name} ISSN {issn} source lookup failed: {exc}"
        ) from exc

    source_id = str(data.get("id") or "").rstrip("/").split("/")[-1]
    if not source_id.startswith("S"):
        raise SourceQueryFailed(
            f"{journal_name} ISSN {issn}: OpenAlex returned no valid Source ID"
        )

    resolved = {
        "id": source_id,
        "display_name": data.get("display_name") or journal_name,
        "issn": data.get("issn") or [],
        "issn_l": data.get("issn_l") or "",
        "type": data.get("type") or "",
        "is_oa": bool(data.get("is_oa")),
    }
    client.openalex_source_cache[issn] = resolved

    print(
        f"[OpenAlex] {journal_name} ISSN={issn} -> source_id={source_id}",
        flush=True,
    )
    return resolved


def openalex_topic_text(work: dict) -> str:
    parts = []

    primary_topic = work.get("primary_topic")
    if primary_topic:
        parts.append(json.dumps(primary_topic, ensure_ascii=False))

    topics = work.get("topics") or []
    if topics:
        parts.append(json.dumps(topics, ensure_ascii=False))

    return " ".join(parts)


def openalex_source_candidates(
    client: Client,
    source_group: str,
    per_journal: int = 60,
) -> Iterable[dict]:
    """
    Discover OA PDF works from a configured journal family.

    Each journal is independently resolved and queried. A bad query for one
    journal does not make the entire source group look "exhausted".
    If OpenAlex is rate-limited (429), the whole source group is paused for
    the current run because repeated calls would only make the situation worse.
    """
    cfg = PAPER_SOURCES[source_group]
    successful_queries = 0
    failed_queries = 0

    for name, issn, journal_url, scope in cfg["journals"]:
        try:
            source = openalex_resolve_source(client, issn, name)
        except SourceTemporarilyUnavailable:
            raise
        except SourceQueryFailed as exc:
            failed_queries += 1
            print(
                f"[WARN] OpenAlex source lookup failed for {name}: {exc}; "
                "trying the next configured journal.",
                flush=True,
            )
            continue

        source_id = source["id"]

        params = {
            "filter": (
                f"primary_location.source.id:{source_id},"
                "open_access.is_oa:true,has_content.pdf:true,type:article"
            ),
            "per_page": min(100, max(10, per_journal)),

            # OpenAlex 当前 API 的降序写法
            "sort": "publication_date:desc",
            "select": (
                "id,doi,title,publication_date,type,open_access,"
                "best_oa_location,locations,abstract_inverted_index,"
                "primary_location,primary_topic,topics"
            ),
        }
        if scope == "chemistry_broad":
            params["search"] = OA_BROAD_SEARCH

        url = OPENALEX_API + "?" + urlencode(params)

        try:
            data = client.json(url, "OpenAlex Works")
        except SourceTemporarilyUnavailable:
            raise
        except SourceQueryFailed as exc:
            failed_queries += 1
            print(
                f"[WARN] OpenAlex works query failed for {name}: {exc}; "
                "trying the next configured journal.",
                flush=True,
            )
            continue
        except (requests.RequestException, ValueError) as exc:
            failed_queries += 1
            print(
                f"[WARN] OpenAlex works response invalid for {name}: {exc}; "
                "trying the next configured journal.",
                flush=True,
            )
            continue

        successful_queries += 1
        results = data.get("results", [])
        print(
            f"[OpenAlex] {name}: {len(results)} OA/PDF article candidates",
            flush=True,
        )

        for work in results:
            journal = {
                "name": name,
                "issn": issn,
                "url": journal_url,
                "scope": scope,
            }
            ok, evidence = publisher_is_chemistry(work, journal)
            if not ok:
                continue

            pdf = openalex_pdf_url(work)
            if not pdf or not acceptable_link(pdf, ".pdf"):
                continue

            oa = work.get("open_access") or {}
            best = work.get("best_oa_location") or {}
            source_page = best.get("landing_page_url") or ""
            doi = normalize_doi(work.get("doi", ""))

            if not source_page and doi:
                source_page = "https://doi.org/" + doi
            if not source_page:
                source_page = journal_url

            license_name = best.get("license") or ""

            yield {
                "category": "papers",
                "resource_kind": "research_article",
                "title": " ".join((work.get("title") or "").split()),
                "source_page": source_page,
                "download_url": pdf,
                "publisher": cfg["publisher"],
                "journal": name,
                "journal_issn": issn,
                "license_note": (
                    license_name
                    or "OA_REPORTED_BY_OPENALEX_CHECK_ARTICLE_LICENSE"
                ),
                "published_at": work.get("publication_date") or "",
                "doi": doi,
                "primary_category": "",
                "abstract": reconstruct_abstract(work),
                "arxiv_id": "",
                "search_query": (
                    f"OpenAlex primary_location.source.id:{source_id}"
                ),
                "oa_status": oa.get("oa_status", ""),
                "oa_verified": bool(oa.get("is_oa")) and bool(pdf),
                "oa_pdf_source": "OpenAlex best_oa_location.pdf_url",
                "oa_verification_level": (
                    "openalex_oa+direct_pdf"
                    + ("+license_metadata" if license_name else "")
                ),
                "openalex_id": work.get("id", ""),
                "openalex_source_id": source_id,
                "discipline": "Chemistry",
                "discipline_evidence": evidence,
                "discipline_verification_level": (
                    "issn->openalex_source_id+journal_scope+metadata+oa_pdf"
                ),
                "source_group": source_group,
            }

    if successful_queries == 0 and failed_queries > 0:
        raise SourceQueryFailed(
            f"{source_group}: all configured OpenAlex journal queries failed"
        )


def paper_identity_keys(item: dict) -> list[str]:
    """Return all known identity keys; using all keys catches cross-source copies."""
    keys = []

    doi = normalize_doi(item.get("doi", ""))
    if doi:
        keys.append("doi:" + doi)

    aid = arxiv_id_base(item.get("arxiv_id", ""))
    if aid:
        keys.append("arxiv:" + aid)

    oid = (item.get("openalex_id") or "").strip().lower()
    if oid:
        keys.append("openalex:" + oid)

    title = normalize_title(item.get("title", ""))
    if title:
        keys.append("title:" + title)

    url = (item.get("download_url") or "").strip().lower()
    if url:
        keys.append("url:" + url)

    return keys


def load_manifest(manifest: Path, root: Path) -> list[dict]:
    records = []
    if not manifest.exists():
        return records

    for line in manifest.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            rel = item.get("file", "")
            if (
                item.get("status") == "downloaded"
                and rel
                and (root / rel).is_file()
            ):
                records.append(item)
        except (ValueError, OSError):
            continue
    return records


def build_global_dedup(records: list[dict]) -> set[str]:
    index = set()
    for record in records:
        index.update(paper_identity_keys(record))
        sha = (record.get("sha256") or "").strip().lower()
        if sha:
            index.add("sha256:" + sha)
    return index


def classify_candidate(item: dict) -> tuple[str, str]:
    cat = item.get("category", "")
    title = item.get("title", "")
    abstract = item.get("abstract", "")

    if cat == "papers":
        primary = (item.get("primary_category") or "").strip()
        if primary == "physics.chem-ph":
            return "accepted", (
                "official arXiv primary_category=physics.chem-ph"
            )

        chemistry_journals = {
            name
            for cfg in PAPER_SOURCES.values()
            for name, _, _, scope in cfg["journals"]
            if scope == "chemistry_core"
        }
        if item.get("journal") in chemistry_journals:
            return "accepted", (
                f'ISSN-verified chemistry journal: {item["journal"]}'
            )

        if item.get("oa_verified") and CHEMISTRY_SIGNAL.search(
            f"{title} {abstract}"
        ):
            return "accepted", (
                "OA metadata + chemistry signal in title/abstract"
            )

        # Backward compatibility with your existing first-generation arXiv
        # collection. If an old record came from the chem-ph query but its
        # primary category was not persisted, call it likely_chemistry rather
        # than pretending it is verified or rejecting it.
        if (
            item.get("search_query") == "cat:physics.chem-ph"
            or item.get("source_group") == "arxiv"
            or (
                "arxiv.org" in item.get("source_page", "")
                and arxiv_id_from_url(item.get("source_page", ""))
            )
        ):
            return "likely_chemistry", (
                "historical arXiv chemistry source evidence; "
                "primary_category not independently verified"
            )

        return "rejected", "insufficient chemistry verification evidence"

    if cat in ("videos", "teaching"):
        slug = course_slug(item.get("source_page", ""))
        if slug not in MIT_CHEMISTRY_SLUGS:
            return "rejected", "not a whitelisted MIT chemistry course"
        if VIDEO_NON_CHEMICAL_TOPIC.search(title):
            return "rejected", "clearly non-chemistry lesson topic"
        return "accepted", "MIT chemistry course-level evidence"

    if cat == "methods":
        if "/hw-sw846/sw-846-test-method-" in item.get("source_page", ""):
            return "accepted", "EPA SW-846 analytical method source"
        return "needs_review", "outside EPA SW-846 method catalog"

    if cat == "textbooks":
        if CHEMISTRY_BOOK_TITLE.search(title):
            return "accepted", "chemistry shown in textbook title"
        return "needs_review", "title does not establish chemistry subject"

    return "needs_review", "unknown category"


def parse_source_targets(spec: str, sources: list[str], total: int) -> dict[str, int]:
    if not spec.strip():
        base, remainder = divmod(total, len(sources))
        return {
            s: base + (1 if i < remainder else 0)
            for i, s in enumerate(sources)
        }

    result = {s: 0 for s in sources}
    fixed = set()

    for token in spec.split(","):
        if "=" not in token:
            raise ValueError(f"Bad --source-targets item: {token}")
        source, raw = token.split("=", 1)
        source = source.strip()
        if source not in result:
            raise ValueError(f"Source {source} not in --paper-sources")
        result[source] = max(0, int(raw))
        fixed.add(source)

    specified = sum(result.values())
    if specified > total:
        raise ValueError("--source-targets sum exceeds remaining target")

    remaining = total - specified
    free = [s for s in sources if s not in fixed]

    if free:
        base, remainder = divmod(remaining, len(free))
        for i, source in enumerate(free):
            result[source] = base + (1 if i < remainder else 0)
    elif remaining:
        # All sources were explicitly listed but their sum is below the target.
        # Distribute the remainder to the currently smallest quota.
        for _ in range(remaining):
            source = min(sources, key=lambda s: result[s])
            result[source] += 1

    return result


def redistribute_quota(
    quotas: Counter,
    sources: list[str],
    unavailable_source: str,
    unused: int,
    disabled: set[str],
    exhausted: set[str],
) -> None:
    if unused <= 0:
        return

    receivers = [
        s for s in sources
        if s != unavailable_source
        and s not in disabled
        and s not in exhausted
    ]
    if not receivers:
        return

    # Give one slot at a time to the source with the smallest current
    # remaining quota. This keeps redistribution as balanced as possible.
    for _ in range(unused):
        receiver = min(
            receivers,
            key=lambda s: (
                quotas[s],
                sources.index(s),
            ),
        )
        quotas[receiver] += 1


def balanced_paper_candidates(
    client: Client,
    sources: list[str],
    total_needed: int,
    source_targets: dict[str, int],
    dedup_keys: set[str],
) -> Iterable[dict]:
    """
    True round-robin paper scheduler with global deduplication.

    - Sources take turns instead of repeatedly favoring the first source.
    - Duplicates do not consume quota.
    - Only the affected source is disabled on a temporary/API failure.
    - Unused quota is redistributed among the remaining sources.
    - HTTP 400 is reported as a query failure, not as "source exhausted".
    """
    iterators = {}

    if "arxiv" in sources:
        iterators["arxiv"] = iter(
            arxiv_candidates(client, pool_size=max(100, total_needed * 5))
        )
    if "nature" in sources:
        iterators["nature"] = iter(
            openalex_source_candidates(client, "nature", per_journal=60)
        )
    if "science" in sources:
        iterators["science"] = iter(
            openalex_source_candidates(client, "science", per_journal=80)
        )

    counts = Counter()
    quotas = Counter(source_targets)
    disabled = set()
    exhausted = set()
    emitted = 0
    cursor = 0

    print(
        "[papers] initial balanced quotas: "
        + ", ".join(f"{s}={quotas[s]}" for s in sources),
        flush=True,
    )

    while emitted < total_needed:
        active = [
            s for s in sources
            if s not in disabled
            and s not in exhausted
            and quotas[s] > counts[s]
        ]
        if not active:
            break

        # Strictly rotate through the configured source order. Duplicates
        # therefore cannot cause arXiv to monopolize discovery.
        chosen = None
        for step in range(len(sources)):
            idx = (cursor + step) % len(sources)
            source = sources[idx]
            if source in active:
                chosen = source
                cursor = (idx + 1) % len(sources)
                break

        if chosen is None:
            break

        source = chosen

        try:
            item = next(iterators[source])
        except StopIteration:
            unused = quotas[source] - counts[source]
            exhausted.add(source)
            quotas[source] = counts[source]
            print(
                f"[papers] source exhausted: {source}; "
                f"redistributing unused quota={unused}.",
                flush=True,
            )
            redistribute_quota(
                quotas, sources, source, unused, disabled, exhausted
            )
            continue
        except SourceTemporarilyUnavailable as exc:
            unused = quotas[source] - counts[source]
            disabled.add(source)
            quotas[source] = counts[source]
            print(
                f"[WARN] {source} temporarily unavailable: {exc}. "
                f"Pausing only this source; redistributing quota={unused}.",
                flush=True,
            )
            redistribute_quota(
                quotas, sources, source, unused, disabled, exhausted
            )
            continue
        except SourceQueryFailed as exc:
            unused = quotas[source] - counts[source]
            disabled.add(source)
            quotas[source] = counts[source]
            print(
                f"[WARN] {source} query failed: {exc}. "
                f"Pausing only this source; redistributing quota={unused}.",
                flush=True,
            )
            redistribute_quota(
                quotas, sources, source, unused, disabled, exhausted
            )
            continue
        except Exception as exc:
            unused = quotas[source] - counts[source]
            disabled.add(source)
            quotas[source] = counts[source]
            print(
                f"[WARN] {source} discovery error: {exc}. "
                f"Pausing only this source; redistributing quota={unused}.",
                flush=True,
            )
            redistribute_quota(
                quotas, sources, source, unused, disabled, exhausted
            )
            continue

        keys = paper_identity_keys(item)
        duplicate_key = next(
            (key for key in keys if key in dedup_keys),
            None,
        )
        if duplicate_key:
            print(
                f'[DUP] {source}: {item.get("title", "")[:90]} '
                f'| {duplicate_key}',
                flush=True,
            )
            continue

        dedup_keys.update(keys)
        counts[source] += 1
        emitted += 1
        yield item

    print(
        "[papers] scheduler emitted: "
        + ", ".join(f"{s}={counts[s]}" for s in sources),
        flush=True,
    )


def mit_candidates(client: Client, category: str) -> Iterable[dict]:
    endpoint = "lecture-videos" if category == "videos" else "lecture-notes"
    suffix = ".mp4" if category == "videos" else ".pdf"
    visited = set()

    for root in MIT_COURSES:
        pages = [
            urljoin(root, "resources/" + endpoint + "/"),
            urljoin(root, "download/"),
        ]

        for page in pages:
            try:
                soup = client.html(page)
            except Exception as exc:
                print(f"[WARN] MIT source page {page}: {exc}", flush=True)
                continue

            for anchor in soup.select("a[href]"):
                url = urljoin(page, anchor.get("href", ""))

                if not acceptable_link(url, suffix) or url in visited:
                    continue

                host = urlparse(url).hostname or ""
                if category == "videos" and not host.endswith("archive.org"):
                    continue
                if category == "teaching" and not host.endswith("ocw.mit.edu"):
                    continue

                visited.add(url)
                stem = Path(urlparse(url).path).stem
                label = anchor.get_text(" ", strip=True)

                yield {
                    "category": category,
                    "resource_kind": (
                        "course_video"
                        if category == "videos"
                        else "lecture_notes"
                    ),
                    "title": (
                        f'{root.rstrip("/").split("/")[-1]} - '
                        f"{stem} - {label}"
                    )[:240],
                    "source_page": page,
                    "download_url": url,
                    "publisher": "MIT OpenCourseWare",
                    "license_note": (
                        "MIT_OCW_CC_BY_NC_SA_4_0_CHECK_THIRD_PARTY_NOTICES"
                    ),
                    "published_at": "",
                    "doi": "",
                }


def method_candidates(client: Client) -> Iterable[dict]:
    soup = client.html(EPA_INDEX)
    seen_pages = set()

    for anchor in soup.select("a[href]"):
        page = urljoin(EPA_INDEX, anchor.get("href", ""))
        p = urlparse(page)

        if (
            p.hostname != "www.epa.gov"
            or "/hw-sw846/sw-846-test-method-" not in p.path
            or page in seen_pages
        ):
            continue

        seen_pages.add(page)

        try:
            detail = client.html(page)
        except Exception as exc:
            print(f"[WARN] EPA method page {page}: {exc}", flush=True)
            continue

        name = (
            detail.h1.get_text(" ", strip=True)
            if detail.h1
            else anchor.get_text(" ", strip=True)
        )

        for a in detail.select("a[href]"):
            pdf = urljoin(page, a.get("href", ""))
            if (
                acceptable_link(pdf, ".pdf")
                and (urlparse(pdf).hostname or "").endswith("epa.gov")
            ):
                yield {
                    "category": "methods",
                    "resource_kind": "analytical_test_method",
                    "title": name,
                    "source_page": page,
                    "download_url": pdf,
                    "publisher": "US EPA",
                    "license_note": (
                        "US_GOV_WORK_CHECK_THIRD_PARTY_MATERIAL_AND_CURRENT_STATUS"
                    ),
                    "published_at": "",
                    "doi": "",
                }
                break


def textbook_candidates(seed_file: Path) -> Iterable[dict]:
    if not seed_file.exists():
        print(
            f"[INFO] No optional book seed file at {seed_file}",
            flush=True,
        )
        return

    with seed_file.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url = (row.get("download_url") or "").strip()
            if not acceptable_link(url, ".pdf"):
                continue

            yield {
                "category": "textbooks",
                "resource_kind": "whole_textbook",
                "title": row.get("title", ""),
                "source_page": row.get("source_page", ""),
                "download_url": url,
                "publisher": row.get("publisher", ""),
                "license_note": row.get(
                    "license_note", "LICENSE_REVIEW_REQUIRED"
                ),
                "published_at": row.get("published_at", ""),
                "doi": "",
            }


def signature_ok(path: Path, ext: str) -> bool:
    header = path.read_bytes()[:16]

    if ext == ".pdf":
        return header.startswith(b"%PDF-")
    if ext == ".mp4":
        return header[4:8] == b"ftyp"
    if ext == ".webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    if ext == ".docx":
        return header.startswith(b"PK\x03\x04")
    if ext == ".doc":
        return header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    return False


def download_one(
    client: Client,
    item: dict,
    root: Path,
    max_mb: int,
) -> dict:
    url = item["download_url"]
    ext = Path(urlparse(url).path).suffix.lower()

    if ext not in MIME:
        raise ValueError("Unsupported source extension: " + ext)

    folder = root / "raw" / item["category"]
    folder.mkdir(parents=True, exist_ok=True)

    target = folder / f'{item["category"]}_{key_for(url)}{ext}'
    part = target.with_suffix(target.suffix + ".part")
    max_bytes = max_mb * 1024 * 1024

    sha = hashlib.sha256()
    total = 0

    try:
        with client.get(url, stream=True) as response:
            length = int(response.headers.get("Content-Length", "0") or 0)
            if length > max_bytes:
                raise ValueError(f"Over {max_mb}MB per-file limit")

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0]
                .lower()
            )
            if content_type in {"text/html", "application/json"}:
                raise ValueError(
                    "Got HTML/JSON instead of an original media file"
                )

            with part.open("wb") as f:
                for chunk in response.iter_content(
                    chunk_size=1024 * 256
                ):
                    if not chunk:
                        continue

                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(
                            f"Exceeded {max_mb}MB per-file limit"
                        )

                    f.write(chunk)
                    sha.update(chunk)

        if not signature_ok(part, ext):
            raise ValueError(f"File signature does not match {ext}")

        part.replace(target)

        return {
            **item,
            "file": str(target.relative_to(root)),
            "bytes": total,
            "sha256": sha.hexdigest(),
            "mime_expected": MIME[ext],
            "collected_at": utc_now(),
            "status": "downloaded",
        }

    except Exception:
        part.unlink(missing_ok=True)
        raise


def save_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def save_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    columns = [
        "category", "resource_kind", "title", "publisher",
        "source_page", "download_url", "license_note", "published_at",
        "doi", "primary_category", "abstract", "arxiv_id",
        "search_query", "discipline", "discipline_evidence",
        "discipline_verification_level", "journal", "journal_issn",
        "oa_status", "oa_verified", "oa_pdf_source", "openalex_id",
        "openalex_source_id",
        "oa_verification_level", "source_group", "filter_status",
        "filter_reason", "duplicate_key",
    ]

    with path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        writer = csv.DictWriter(
            f, fieldnames=columns, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def collect_papers(client: Client, args, root: Path) -> None:
    manifest = root / "manifest.jsonl"
    errors = root / "errors.jsonl"

    old = load_manifest(manifest, root)
    old_papers = [r for r in old if r.get("category") == "papers"]

    # Backward compatibility with your existing arXiv collection:
    # legacy arXiv files are retained and count toward the file target.
    chemistry_old = []
    for record in old_papers:
        status, _ = classify_candidate(record)
        if status == "accepted":
            chemistry_old.append(record)
        elif (
            record.get("primary_category") == "physics.chem-ph"
            or record.get("search_query") == "cat:physics.chem-ph"
            or "arxiv.org" in record.get("source_page", "")
        ):
            chemistry_old.append(record)

    print(
        f"[papers] existing chemistry-relevant files: {len(chemistry_old)}",
        flush=True,
    )

    if len(chemistry_old) >= args.limit:
        print(
            f"[papers] existing files already reach target {args.limit}; "
            "no new discovery/download is necessary.",
            flush=True,
        )
        return

    sources = [
        x.strip()
        for x in args.paper_sources.split(",")
        if x.strip()
    ]
    allowed = {"arxiv", "nature", "science"}

    if not sources:
        raise ValueError("No paper source configured")

    unknown = set(sources) - allowed
    if unknown:
        raise ValueError(
            "Unknown --paper-sources: " + ",".join(sorted(unknown))
        )

    need = args.limit - len(chemistry_old)
    quotas = parse_source_targets(
        args.source_targets, sources, need
    )

    print(
        f"[papers] target={args.limit}; need_new={need}",
        flush=True,
    )
    print(
        "[papers] source quotas: "
        + ", ".join(f"{s}={quotas[s]}" for s in sources),
        flush=True,
    )

    dedup = build_global_dedup(old)
    candidates = []
    filtered = []
    duplicates = []

    for item in balanced_paper_candidates(
        client,
        sources,
        need,
        quotas,
        dedup,
    ):
        status, reason = classify_candidate(item)

        if status != "accepted":
            filtered.append({
                **item,
                "filter_status": status,
                "filter_reason": reason,
            })
            continue

        item = {
            **item,
            "discipline": "Chemistry",
            "discipline_evidence": reason,
            "discipline_verification_level": (
                "source_category_or_journal+metadata+oa_pdf"
            ),
        }
        candidates.append(item)

        if not args.download:
            continue

        try:
            record = download_one(
                client, item, root, args.max_file_mb
            )

            sha_key = "sha256:" + record["sha256"].lower()

            if sha_key in dedup:
                duplicate_path = root / record["file"]
                duplicate_path.unlink(missing_ok=True)

                duplicates.append({
                    **item,
                    "filter_status": "duplicate",
                    "filter_reason": "SHA-256 already exists",
                    "duplicate_key": sha_key,
                })
                continue

            save_jsonl(manifest, record)
            old.append(record)
            dedup.add(sha_key)

            # Reserve all identity keys for the newly accepted work.
            dedup.update(paper_identity_keys(record))

            total_papers = sum(
                1 for r in old if r.get("category") == "papers"
            )

            print(
                f'[papers] {total_papers}/{args.limit}: '
                f'{record.get("source_group", "")} | '
                f'{record.get("journal", "") or "arXiv"} | '
                f'{record.get("title", "")[:100]}',
                flush=True,
            )

            if total_papers >= args.limit:
                break

        except Exception as exc:
            save_jsonl(errors, {
                "category": "papers",
                "source_group": item.get("source_group", ""),
                "download_url": item.get("download_url", ""),
                "error": str(exc),
                "at": utc_now(),
            })
            print(
                f'[SKIP] {item.get("download_url")}: {exc}',
                flush=True,
            )

    save_csv(
        root / "papers_v5_candidates.csv",
        candidates,
    )
    save_csv(
        root / "papers_v5_filtered_out.csv",
        filtered + duplicates,
    )

    final_records = load_manifest(manifest, root)
    final = sum(
        1 for r in final_records if r.get("category") == "papers"
    )

    by_source = Counter(
        r.get("source_group", "unknown")
        for r in final_records
        if r.get("category") == "papers"
    )

    print(
        f"[papers] V5 result: {final}/{args.limit}; "
        f"source distribution: "
        + ", ".join(
            f"{s}={by_source.get(s, 0)}"
            for s in sources
        ),
        flush=True,
    )


def collect_other(
    client: Client,
    args,
    root: Path,
    category: str,
) -> None:
    manifest = root / "manifest.jsonl"
    errors = root / "errors.jsonl"

    old = load_manifest(manifest, root)
    completed = sum(
        1 for r in old if r.get("category") == category
    )

    if completed >= args.limit:
        print(
            f"[{category}] existing files={completed} "
            f"already reach target={args.limit}.",
            flush=True,
        )
        return

    if category in ("videos", "teaching"):
        iterator = mit_candidates(client, category)
    elif category == "methods":
        iterator = method_candidates(client)
    elif category == "textbooks":
        iterator = textbook_candidates(args.books_csv)
    else:
        raise ValueError(category)

    seen = {
        r.get("download_url")
        for r in old
        if r.get("category") == category
    }
    candidates = []

    for item in iterator:
        url = item.get("download_url", "")
        if not url or url in seen:
            continue
        seen.add(url)

        status, reason = classify_candidate(item)
        if status != "accepted":
            candidates.append({
                **item,
                "filter_status": status,
                "filter_reason": reason,
            })
            continue

        item = {
            **item,
            "discipline": "Chemistry",
            "discipline_evidence": reason,
            "discipline_verification_level": "source_metadata",
        }
        candidates.append(item)

        if not args.download:
            continue

        try:
            record = download_one(
                client, item, root, args.max_file_mb
            )
            save_jsonl(manifest, record)
            completed += 1

            print(
                f'[{category}] {completed}/{args.limit}: '
                f'{record["file"]} ({record["bytes"]:,} bytes)',
                flush=True,
            )

            if completed >= args.limit:
                break

        except Exception as exc:
            save_jsonl(errors, {
                "category": category,
                "download_url": url,
                "error": str(exc),
                "at": utc_now(),
            })
            print(f"[SKIP] {url}: {exc}", flush=True)

    save_csv(
        root / f"{category}_v5_candidates.csv",
        candidates,
    )


def audit_existing(root: Path) -> None:
    records = load_manifest(
        root / "manifest.jsonl", root
    )
    rows = []

    for record in records:
        status, evidence = classify_candidate(record)
        rows.append({
            **record,
            "audit_verdict": status,
            "audit_evidence": evidence,
            "audited_at": utc_now(),
        })

    save_csv(
        root / "chemistry_audit_v5.csv",
        rows,
    )

    counter = Counter(
        row["audit_verdict"] for row in rows
    )

    print(
        f"[AUDIT] {len(rows)} inspected: "
        f"accepted={counter['accepted']}, "
        f"rejected={counter['rejected']}, "
        f"needs_review={counter['needs_review']}.",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--categories",
        default="papers,videos,teaching,methods",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="successful files per category",
    )
    parser.add_argument(
        "--download",
        action="store_true",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("chemistry_dataset"),
    )
    parser.add_argument(
        "--contact",
        default="your-email@example.org",
    )
    parser.add_argument(
        "--paper-sources",
        default="arxiv,nature,science",
        help="comma-separated: arxiv,nature,science",
    )
    parser.add_argument(
        "--source-targets",
        default="",
        help="e.g. arxiv=7,nature=7,science=6",
    )
    parser.add_argument(
        "--max-file-mb",
        type=int,
        default=500,
    )
    parser.add_argument(
        "--books-csv",
        type=Path,
        default=Path("textbook_seeds.csv"),
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
    )

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be >= 1")

    categories = [
        x.strip()
        for x in args.categories.split(",")
        if x.strip()
    ]

    unknown = set(categories) - CATEGORIES
    if unknown:
        parser.error(
            "Unknown categories: "
            + ",".join(sorted(unknown))
        )

    args.out.mkdir(
        parents=True,
        exist_ok=True,
    )

    client = Client(args.contact)

    if args.audit_only:
        audit_existing(args.out)
        return

    for category in categories:
        print(
            f"\\n=== {category} | target={args.limit} ===",
            flush=True,
        )

        try:
            if category == "papers":
                collect_papers(
                    client, args, args.out
                )
            else:
                collect_other(
                    client, args, args.out, category
                )
        except Exception as exc:
            save_jsonl(
                args.out / "errors.jsonl",
                {
                    "category": category,
                    "phase": "collection",
                    "error": str(exc),
                    "at": utc_now(),
                },
            )
            print(
                f"[ERROR] {category}: {exc}",
                flush=True,
            )

    print(
        "\\nDone. Existing raw files and manifest records "
        "are preserved. V5 uses balanced paper-source "
        "scheduling and global deduplication.",
        flush=True,
    )


if __name__ == "__main__":
    main()
