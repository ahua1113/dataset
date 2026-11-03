"""带节流、重试和断点下载能力的共享 HTTP 客户端。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import time

try:
    import requests
except ModuleNotFoundError:  # 允许未安装依赖时使用 --help / --dry-run
    requests = None  # type: ignore[assignment]

from public_datasets import VERSION
from public_datasets.common import CollectorError, HttpRequestError, stop_requested


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    def __init__(
        self,
        *,
        contact_email: str,
        timeout_seconds: float,
        max_retries: int,
        request_interval_seconds: float,
    ) -> None:
        if requests is None:
            raise CollectorError(
                "缺少 requests 依赖，请先运行：python -m pip install -r requirements.txt"
            )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.request_interval_seconds = request_interval_seconds
        self.last_request_at = 0.0
        self.session = requests.Session()
        contact = contact_email.strip() or "not-provided"
        self.session.headers.update(
            {
                "User-Agent": (
                    f"chemistry-public-dataset-collector/{VERSION} "
                    f"(contact: {contact})"
                ),
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def close(self) -> None:
        self.session.close()

    def _pace(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        acceptable_status: set[int] | None = None,
    ):
        acceptable = acceptable_status or {200}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if stop_requested():
                raise CollectorError("采集任务已请求停止")
            self._pace()
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    stream=stream,
                    timeout=(15.0, self.timeout_seconds),
                )
                self.last_request_at = time.monotonic()
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(60.0, 2.0**attempt)
                logging.warning("网络请求失败，%.1f 秒后重试：%s", delay, exc)
                time.sleep(delay)
                continue

            if response.status_code in acceptable:
                throttle = response.headers.get("X-Throttling-Control", "")
                if "Red" in throttle:
                    time.sleep(5.0)
                elif "Yellow" in throttle:
                    time.sleep(1.0)
                return response

            message = response.text[:400].replace("\n", " ")
            if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                response.close()
                raise HttpRequestError(response.status_code, message, url)

            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(60.0, 2.0**attempt)
            except ValueError:
                delay = min(60.0, 2.0**attempt)
            status_code = response.status_code
            response.close()
            logging.warning("HTTP %s，%.1f 秒后重试", status_code, delay)
            time.sleep(delay)

        raise CollectorError(f"网络请求最终失败：{url}；{last_error}")


def download_with_resume(
    client: HttpClient,
    url: str,
    destination: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    existing = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else None
    response = client.get(url, headers=headers, stream=True, acceptable_status={200, 206})

    append = existing > 0 and response.status_code == 206
    if existing and not append:
        logging.warning("下载端不接受断点续传，将从头下载：%s", destination.name)
        existing = 0
    mode = "ab" if append else "wb"
    expected = response.headers.get("Content-Length")
    expected_bytes = int(expected) + existing if expected and expected.isdigit() else None
    downloaded = existing
    last_report = time.monotonic()

    with partial.open(mode) as handle:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if stop_requested():
                response.close()
                raise CollectorError("采集任务已请求停止；断点文件已保留")
            if not chunk:
                continue
            handle.write(chunk)
            downloaded += len(chunk)
            if time.monotonic() - last_report >= 10:
                if expected_bytes:
                    logging.info(
                        "下载进度：%.1f%%（%.1f MiB）",
                        downloaded * 100 / expected_bytes,
                        downloaded / (1024**2),
                    )
                else:
                    logging.info("已下载 %.1f MiB", downloaded / (1024**2))
                last_report = time.monotonic()
        handle.flush()
        os.fsync(handle.fileno())
    response.close()

    if expected_bytes is not None and downloaded != expected_bytes:
        raise CollectorError(
            f"下载大小不完整：期望 {expected_bytes} 字节，实际 {downloaded} 字节"
        )
    os.replace(partial, destination)
