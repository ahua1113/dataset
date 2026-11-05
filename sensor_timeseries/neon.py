"""按产品、站点和月份保存 NEON 原始表和来源清单。"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

API = "https://data.neonscience.org/api/v0"
PRODUCTS = {"water-quality": "DP1.20288.001", "groundwater": "DP1.20100.001"}


def get(session, url, token):
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise ValueError("文件链接必须使用 HTTPS")
        headers = {"X-API-Token": token} if parsed.netloc == "data.neonscience.org" else {}
        response = session.get(url, headers=headers, stream=True, timeout=(10, 90),
                               allow_redirects=False)
        if response.is_redirect:
            url = urljoin(url, response.headers["Location"])
            response.close()
            continue
        if response.status_code == 200:
            return response
        response.close()
        raise RuntimeError(f"NEON 请求返回 HTTP {response.status_code}")
    raise RuntimeError("重定向次数过多")


def digest(path):
    checksum = hashlib.md5()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def collect_month(session, token, product, site, month, root):
    code = PRODUCTS[product]
    source = f"{API}/data/{code}/{site}/{month}?package=basic"
    with get(session, source, token) as response:
        data = response.json()["data"]
    directory = root / "neon" / code / site / month
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for entry in data["files"]:
        name = entry["name"]
        if not name.lower().endswith(".csv"):
            continue
        if Path(name).name != name or any(c in name for c in "/\\:"):
            raise ValueError("文件名包含非法路径")
        target = directory / name
        size, checksum = int(entry["size"]), str(entry["md5"]).lower()
        if not (target.is_file() and target.stat().st_size == size and digest(target) == checksum):
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
                partial = Path(tmp.name)
            try:
                with get(session, entry["url"], token) as response, partial.open("wb") as out:
                    for chunk in response.iter_content(1024 * 1024):
                        out.write(chunk)
                if partial.stat().st_size != size or digest(partial) != checksum:
                    raise ValueError(f"文件校验失败：{name}")
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
        files.append({"name": name, "bytes": size, "md5": checksum})
    if not files:
        raise ValueError("此站点和月份没有可下载的 CSV")
    manifest = dict(product=code, site=site, month=month, release=data.get("release"),
                    source=source, files=files)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as tmp:
        json.dump(manifest, tmp, indent=2)
    os.replace(tmp.name, directory / "manifest.json")
    return len(files)
