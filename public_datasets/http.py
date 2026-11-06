"""带节流和重试能力的共享 HTTP 客户端。"""

from __future__ import annotations

import logging
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
