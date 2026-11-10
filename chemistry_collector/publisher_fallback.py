"""Find the advertised PDF on an open publisher article page."""
import re
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .client import SourceError
from .disciplines import normalize_license
from .utils import domain_matches, is_https, normalize_doi


def publisher_url_allowed(url, domains):
    try:
        parsed = urlsplit(url)
        return (
            is_https(url) and parsed.port in (None, 443)
            and any(domain_matches(parsed.hostname or "", domain) for domain in domains)
        )
    except ValueError:
        return False


def publisher_pdf_locations(client, item, journal):
    doi = normalize_doi(item["doi"])
    if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return []
    domains = journal.publisher_hosts
    resolver = "https://doi.org/" + quote(doi, safe="/")
    landing = item["source_page"]
    if not publisher_url_allowed(landing, domains):
        landing = resolver

    def allowed(url):
        return publisher_url_allowed(url, domains)

    with client.get(landing, stream=True, url_allowed=lambda url: url == resolver or allowed(url)) as response:
        page = response.url
        if not allowed(page):
            return []
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > 4 * 1024 * 1024:
                raise SourceError("Publisher article HTML exceeds 4 MiB")

    soup = BeautifulSoup(bytes(body), "html.parser")
    declared = {
        normalize_doi(tag.get("content", ""))
        for tag in soup.select('meta[name="citation_doi"], meta[property="citation_doi"]')
    }
    if declared != {doi}:
        return []

    # The build kit prefers citation metadata and checks article identity on PDF anchors.
    links = [
        tag["content"]
        for tag in soup.select('meta[name="citation_pdf_url"], meta[property="citation_pdf_url"]')
        if tag.get("content")
    ]
    suffix = doi.split("/", 1)[1]
    page_path = unquote(urlsplit(page).path).casefold()
    coordinates = re.match(r"^/([^/]+)/article/([^/]+)/([^/]+)/([^/]+)/", page_path)
    pii = re.search(r"/pii/([a-z0-9]+)/?$", page_path)
    for tag in soup.select('a[href], link[type="application/pdf"][href]'):
        url = urljoin(page, tag["href"])
        if not allowed(url):
            continue
        path = unquote(urlsplit(url).path).casefold()
        pdf_coordinates = re.match(r"^/([^/]+)/article-pdf/([^/]+)/([^/]+)/([^/]+)/", path)
        same_article = any(
            re.search(rf"(?:^|/){re.escape(identifier)}(?:\.pdf)?/?$", path)
            for identifier in (doi, suffix)
        )
        if coordinates and pdf_coordinates:
            same_article = (
                coordinates.groups()[:3] == pdf_coordinates.groups()[:3]
                and coordinates[4].split("-")[0] == pdf_coordinates[4].split("-")[0]
            )
        if pii and re.search(rf"/pii/{pii[1]}/(?:pdfft|pdf)/?$", path):
            links.append(url)
        elif same_article and (
            path.endswith(".pdf") or "/article-pdf/" in path
            or re.search(r"/doi/(?:pdf|epdf|pdfdirect)/", path)
        ):
            links.append(url)

    license_note, license_status = normalize_license(item["publisher_oa_license"])
    locations = {}
    for link in links:
        url = urljoin(page, link)
        if not allowed(url):
            continue
        path = unquote(urlsplit(url).path).casefold()
        if any(part in path for part in ("/suppl", "/suppinfo", "/esm/", "/asset/", "/toc/")):
            continue
        locations[url] = {
            "url": url, "landing_page": page, "oa_host": urlsplit(url).hostname,
            "source_type": "publisher", "metadata_provider": "publisher_landing_page",
            "version": "publishedVersion", "license": license_note,
            "license_status": license_status,
        }
    if domain_matches(urlsplit(page).hostname or "", "onlinelibrary.wiley.com"):
        return sorted(locations.values(), key=lambda entry: (
            "/pdfdirect/" not in urlsplit(entry["url"]).path
        ))
    return list(locations.values())
