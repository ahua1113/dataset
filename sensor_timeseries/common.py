"""传感器数据源共享的下载、校验和原子写入能力。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urljoin, urlsplit


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}


class SensorHttpError(RuntimeError):
    """不包含凭据和临时签名 URL 的 HTTP 错误。"""


def checked_get(
    session,
    url: str,
    *,
    auth_headers: dict[str, str] | None = None,
    credential_host: str | None = None,
    stream: bool = False,
    max_retries: int = 5,
):
    """GET 请求，处理限流、服务端错误和跨域重定向凭据隔离。"""

    current_url = url
    redirects = 0
    attempt = 0
    while True:
        parsed = urlsplit(current_url)
        if parsed.scheme != "https":
            raise ValueError("数据源链接必须使用 HTTPS")
        headers = auth_headers if parsed.netloc == credential_host else None
        response = session.get(
            current_url,
            headers=headers,
            stream=stream,
            timeout=(15, 120),
            allow_redirects=False,
        )
        if response.status_code in REDIRECT_STATUS:
            redirects += 1
            if redirects > 8 or not response.headers.get("Location"):
                response.close()
                raise SensorHttpError("数据源重定向异常")
            current_url = urljoin(current_url, response.headers["Location"])
            response.close()
            continue
        if response.status_code == 200:
            return response
        if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
            retry_after = response.headers.get("Retry-After") or response.headers.get(
                "RetryAfter"
            )
            try:
                delay = float(retry_after) if retry_after else min(30.0, 2.0**attempt)
            except ValueError:
                delay = min(30.0, 2.0**attempt)
            response.close()
            time.sleep(delay)
            attempt += 1
            continue
        message = response.text[:500].replace("\n", " ").strip()
        status = response.status_code
        response.close()
        detail = f"，响应：{message}" if message else ""
        raise SensorHttpError(f"数据源返回 HTTP {status}{detail}")


def file_digest(path: Path, algorithm: str = "md5") -> str:
    checksum = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def download_verified(
    session,
    url: str,
    target: Path,
    *,
    expected_size: int,
    expected_md5: str,
    auth_headers: dict[str, str] | None = None,
    credential_host: str | None = None,
) -> None:
    if (
        target.is_file()
        and target.stat().st_size == expected_size
        and file_digest(target) == expected_md5.lower()
    ):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        partial = Path(temporary.name)
    try:
        with checked_get(
            session,
            url,
            auth_headers=auth_headers,
            credential_host=credential_host,
            stream=True,
        ) as response, partial.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
        if (
            partial.stat().st_size != expected_size
            or file_digest(partial) != expected_md5.lower()
        ):
            raise ValueError(f"文件校验失败：{target.name}")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
