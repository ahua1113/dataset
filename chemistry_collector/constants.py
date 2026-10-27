"""集中维护采集器版本、来源地址、文件类型和文章过滤规则。"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
# 版本与请求地址集中维护，避免多个模块重复定义。
COLLECTOR_VERSION = "6.3"
WHITELIST_SIZE = 42
CONFIG_WHITELIST_SIZES = {
    "journals_v6_3.json": 42,
}
OPENALEX_SOURCES = "https://api.openalex.org/sources"
OPENALEX_WORKS = "https://api.openalex.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
EPA_INDEX = "https://www.epa.gov/hw-sw846/sw-846-compendium"
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
MIT_CHEMISTRY_SLUGS = {root.rstrip("/").split("/")[-1] for root in MIT_COURSES}
MIME = {
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

