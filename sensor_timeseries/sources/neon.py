"""按产品、站点和月份保存 NEON 原始表和来源清单。"""

from __future__ import annotations

from pathlib import Path

from sensor_timeseries.common import atomic_write_json, checked_get, download_verified


API = "https://data.neonscience.org/api/v0"
HOST = "data.neonscience.org"
PRODUCTS = {"water-quality": "DP1.20288.001", "groundwater": "DP1.20100.001"}


def collect_month(session, token: str, product: str, site: str, month: str, root: Path) -> int:
    code = PRODUCTS[product]
    source = f"{API}/data/{code}/{site}/{month}?package=basic"
    auth_headers = {"X-API-Token": token}
    with checked_get(
        session, source, auth_headers=auth_headers, credential_host=HOST
    ) as response:
        data = response.json()["data"]

    # 保持 PR #9 已产生的数据目录兼容，避免升级后重复下载。
    directory = root / "neon" / code / site / month
    files: list[dict[str, object]] = []
    for entry in data["files"]:
        name = entry["name"]
        if not name.lower().endswith(".csv"):
            continue
        if Path(name).name != name or any(character in name for character in "/\\:"):
            raise ValueError("NEON 文件名包含非法路径")
        target = directory / name
        size = int(entry["size"])
        checksum = str(entry["md5"]).lower()
        download_verified(
            session,
            entry["url"],
            target,
            expected_size=size,
            expected_md5=checksum,
            auth_headers=auth_headers,
            credential_host=HOST,
        )
        files.append({"name": name, "bytes": size, "md5": checksum})
    if not files:
        raise ValueError("此站点和月份没有可下载的 CSV")
    atomic_write_json(
        directory / "manifest.json",
        {
            "source": "NEON",
            "product": code,
            "site": site,
            "month": month,
            "release": data.get("release"),
            "source_url": source,
            "files": files,
        },
    )
    return len(files)
