"""Paper discovery, OA-location resolution and metadata filtering."""
import re
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

from .client import Client, SourceError
from .constants import EXCLUDED_ARTICLE_TITLES, OPENALEX_SOURCES, OPENALEX_WORKS, UNPAYWALL_API
from .disciplines import (
    Discipline, classify_work, is_review_work, openalex_locations,
    unpaywall_locations,
)
from .models import Journal
from .utils import is_https, log_error, normalize_doi, normalized_id

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

