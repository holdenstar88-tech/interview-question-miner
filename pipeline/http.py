"""Anonymous, rate-limited HTTP with robots, redirect and access checks."""
from __future__ import annotations
import time
import logging
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import requests
from .config import CollectConfig

logger = logging.getLogger(__name__)

class SkipPage(RuntimeError):
    """This resource is not accessible under the configured collection rules."""

class SourceUnavailable(SkipPage):
    """Stop this source for this run; do not hammer an unavailable host."""

class RequestLimitReached(SourceUnavailable):
    """Local request allowance exhausted, rather than a remote failure."""

class RetryablePage(SkipPage):
    """Incomplete public response; retry with a persistent per-URL limit."""

class PublicHTTP:
    def __init__(self, cfg: CollectConfig, hosts: list[str], max_requests: int | None = None):
        self.cfg = cfg
        self.max_requests = max_requests if max_requests is not None else cfg.max_requests_per_source
        self.hosts = set(hosts)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"})
        self.robots: dict[str, RobotFileParser] = {}
        self.last_request = 0.0
        self.requests = 0
        self.page_requests = 0
        self.retry_requests = 0
        self.status_counts = {}

    def close(self):
        self.session.close()

    def _validate_url(self, url: str):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts or parsed.username or parsed.password or parsed.port not in (None,443):
            raise SkipPage("URL 不在该来源允许的 HTTPS 主机列表中")
        return parsed

    def _send(self, url: str, delay: float = 0):
        self._validate_url(url)
        for attempt in range(self.cfg.max_retries + 1):
            if self.requests >= self.max_requests:
                raise RequestLimitReached("该来源已达本次 HTTP 请求上限")
            wait = max(5,self.cfg.request_interval,delay) - (time.monotonic()-self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.requests += 1
            self.page_requests += int(urlsplit(url).path != "/robots.txt")
            self.retry_requests += int(attempt > 0)
            self.last_request = time.monotonic()
            try:
                # Never attach user cookies or use alternate identities/proxies.
                self.session.cookies.clear()
                response = self.session.get(url,timeout=self.cfg.timeout,allow_redirects=False,stream=True)
                try:
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > self.cfg.max_response_bytes:
                            raise SkipPage("响应过大，停止下载")
                    response._content = bytes(data)
                    response._content_consumed = True
                finally:
                    response.close()
                response.encoding = response.encoding if response.encoding and response.encoding.lower() != "iso-8859-1" else "utf-8"
                status = str(response.status_code)
                self.status_counts[status] = self.status_counts.get(status,0) + 1
                if response.status_code in (401,403,407,412,429,451):
                    raise SourceUnavailable(f"HTTP {response.status_code} 限制访问；停止该来源，不绕过验证")
                if response.status_code >= 500:
                    # Temporary upstream failures are retried with the bounded
                    # retry budget. The request cap still applies to every try.
                    if attempt == self.cfg.max_retries:
                        raise SourceUnavailable(f"HTTP {response.status_code} 服务器错误，重试耗尽")
                    logger.warning("HTTP 重试 host=%s status=%d attempt=%d/%d wait=%ss",urlsplit(url).hostname,response.status_code,attempt+1,self.cfg.max_retries+1,self.cfg.retry_backoff * 2**attempt)
                    time.sleep(self.cfg.retry_backoff * 2**attempt)
                    continue
                return response
            except requests.RequestException as exc:
                if attempt == self.cfg.max_retries:
                    raise SourceUnavailable(f"请求失败，重试耗尽（{type(exc).__name__}）") from exc
                logger.warning("HTTP 重试 host=%s type=%s attempt=%d/%d",urlsplit(url).hostname,type(exc).__name__,attempt+1,self.cfg.max_retries+1)
                time.sleep(self.cfg.retry_backoff * 2**attempt)
        raise SourceUnavailable("请求失败")

    def _rules(self, url: str) -> RobotFileParser:
        parsed = self._validate_url(url)
        root = f"https://{parsed.netloc}"
        if root not in self.robots:
            response = self._send(root + "/robots.txt")
            # Fail closed: a missing, redirected or unreadable robots policy
            # is not treated as permission to collect.
            if response.status_code != 200 or "<html" in response.text.lower() or "user-agent" not in response.text.lower():
                raise SourceUnavailable("无法确认 robots.txt，暂停此来源")
            rp = RobotFileParser()
            rp.parse(response.text.splitlines())
            self.robots[root] = rp
        return self.robots[root]

    def get(self, url: str):
        for _ in range(4):
            rules = self._rules(url)
            if not rules.can_fetch(self.cfg.user_agent,url):
                raise SkipPage("robots.txt 禁止访问")
            delay = rules.crawl_delay(self.cfg.user_agent) or rules.crawl_delay("*") or 0
            rate = rules.request_rate(self.cfg.user_agent) or rules.request_rate("*")
            if rate and rate.requests:
                delay = max(delay,rate.seconds/rate.requests)
            response = self._send(url,delay)
            if response.is_redirect:
                target = urljoin(url,response.headers.get("Location",""))
                self._validate_url(target)
                if any(x in urlsplit(target).path.lower() for x in ("login","passport","captcha","verify","payment")):
                    raise SkipPage("重定向至登录/验证/支付页面")
                url = target
                continue
            if response.status_code != 200:
                raise SkipPage(f"HTTP {response.status_code}")
            lowered = response.text.lower()
            if any(x in lowered for x in ("验证后继续访问", "访问过于频繁", "安全验证", "captcha-container", "window._waf", "document.cookie=", "document.cookie =")):
                raise SourceUnavailable("页面要求访问验证，停止该来源")
            return response
        raise SkipPage("重定向次数过多")
