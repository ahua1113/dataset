"""通用辅助函数：标准化 DOI、标题、链接和时间，并输出结构化日志。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ",
                  (title or "").casefold())).strip()


# 统一 DOI 表示形式，使 URL 格式与裸 DOI 的去重结果一致。
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


# 追加结构化错误记录，便于按阶段和期刊聚合分析。
def log_error(path: Path, category: str, message: str, **details) -> None:
    append_jsonl(path, {"category": category, "error": str(message), "at": now_utc(), **details})


# 每条记录占一行，保留中文与非 ASCII 元数据。
def append_jsonl(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(item, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())

