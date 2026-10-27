"""Small, dependency-free normalization and JSONL helpers."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

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

