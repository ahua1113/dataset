"""期刊配置模型：加载基础及扩展白名单，并校验期刊标识和采集状态。"""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .constants import CONFIG_WHITELIST_SIZES, WHITELIST_SIZE
from .utils import is_https


@dataclass(frozen=True)
# 期刊元数据集中建模，候选期刊和已验证期刊共用同一结构。
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


# 加载当前期刊列表，并检查总数、重复项及必要字段。
def load_journals(path: Path) -> list[Journal]:
    if not path.is_file():
        raise FileNotFoundError(f"Journal whitelist missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
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

