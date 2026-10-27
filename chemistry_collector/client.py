"""网络请求客户端：控制访问频率、遵守站点规则，并处理域名级访问熔断。"""
import re
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .constants import COLLECTOR_VERSION
from .utils import domain_matches, hostname, is_https

class SourceError(RuntimeError):
    pass


# 请求客户端不处理登录认证；连续访问失败的域名会暂时停止下载请求。
class Client:
    """No login, session cookies, paywall workaround or other auth workflow."""

    def __init__(
        self,
        contact: str = "",
        respect_robots: bool = True,
        openalex_api_key: str = "",
        domain_failure_threshold: int = 5,
    ):
        if domain_failure_threshold <= 0:
            raise ValueError("domain_failure_threshold must be greater than zero")
        self.session = requests.Session()
        agent = f"AcademicChemistrySourceCollector/{COLLECTOR_VERSION}"
        if contact.strip():
            agent += f" (contact: {contact.strip()})"
        self.session.headers.update({"User-Agent": agent, "Accept": "application/json, application/pdf, */*"})
        self.respect_robots = respect_robots
        self.contact = contact.strip()
        self.openalex_api_key = openalex_api_key.strip()
        self.robot_parsers: dict[str, RobotFileParser] = {}
        self.last_request: dict[str, float] = {}
        self.domain_failure_threshold = domain_failure_threshold
        self.domain_failures: dict[str, int] = {}
        self.circuit_open_hosts: set[str] = set()

    # 累计受限请求次数，到达阈值后打开该域名的熔断开关。
    def record_access_failure(self, host: str) -> None:
        if not host:
            return
        failures = self.domain_failures.get(host, 0) + 1
        self.domain_failures[host] = failures
        if failures >= self.domain_failure_threshold:
            self.circuit_open_hosts.add(host)

    def record_access_success(self, host: str) -> None:
        self.domain_failures.pop(host, None)

    def wait(self, url: str) -> None:
        host = hostname(url)
        gap = 1.0
        delta = time.monotonic() - self.last_request.get(host, 0.0)
        if delta < gap:
            time.sleep(gap - delta)
        self.last_request[host] = time.monotonic()

    # 先检查站点抓取规则，再决定是否访问实际资源。
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robot_parsers:
            robots_url = origin + "/robots.txt"
            try:
                response = self.session.get(robots_url, timeout=(8, 15))
                if response.status_code == 404:
                    parser = RobotFileParser()
                    parser.parse([])
                elif response.ok:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                else:
                    raise SourceError(f"robots.txt verification HTTP {response.status_code}: {robots_url}")
                self.robot_parsers[origin] = parser
            except requests.RequestException as exc:
                raise SourceError(f"Cannot verify robots.txt for {origin}: {exc}") from exc
        return self.robot_parsers[origin].can_fetch(self.session.headers["User-Agent"], url)

    # 统一检查 HTTPS、站点限制、响应状态和登录跳转。
    def get(self, url: str, *, api: bool = False, stream: bool = False):
        if not is_https(url):
            raise SourceError("Only HTTPS links are allowed")
        host = hostname(url)
        if not api and host in self.circuit_open_hosts:
            raise SourceError(
                f"Domain circuit open after {self.domain_failure_threshold} "
                f"consecutive access failures: {host}"
            )
        if not api and not self.allowed(url):
            self.record_access_failure(host)
            raise SourceError(f"Disallowed by robots.txt: {url}")
        self.wait(url)
        try:
            headers = {}
            if api and self.openalex_api_key and domain_matches(hostname(url), "openalex.org"):
                headers["Authorization"] = f"Bearer {self.openalex_api_key}"
            response = self.session.get(url, timeout=(10, 40), stream=stream,
                                        allow_redirects=True, headers=headers)
        except requests.RequestException as exc:
            raise SourceError(str(exc)) from exc
        if not is_https(response.url):
            response.close()
            raise SourceError("Download redirects to a non-HTTPS URL")
        if not response.ok:
            status = response.status_code
            response.close()
            if not api and status in {401, 403, 429}:
                self.record_access_failure(host)
            if status == 429 and domain_matches(hostname(url), "openalex.org"):
                key_hint = ("OpenAlex API key budget/rate limit reached"
                            if self.openalex_api_key else
                            "OpenAlex shared rate limit reached; set OPENALEX_API_KEY "
                            "or use --openalex-api-key")
                raise SourceError(f"HTTP 429: {key_hint}")
            raise SourceError(f"HTTP {status} for {url}")
        # 出版社可能将公开全文链接重定向到登录页，遇到这种情况直接拒绝。
        if not api and re.search(r"/(?:login|signin|authenticate)(?:[/?]|$)", urlparse(response.url).path, re.I):
            response.close()
            self.record_access_failure(host)
            raise SourceError("Redirected to an authentication page")
        if not api:
            self.record_access_success(host)
        return response

    def json(self, url: str) -> dict:
        with self.get(url, api=True) as response:
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Response is not a JSON object")
                return result
            except ValueError as exc:
                raise SourceError(f"Invalid API JSON: {exc}") from exc

    def html(self, url: str) -> BeautifulSoup:
        with self.get(url) as response:
            return BeautifulSoup(response.content, "html.parser")

