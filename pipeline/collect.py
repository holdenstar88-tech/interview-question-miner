"""Public HTML/RSS/sitemap collection; Nowcoder first, CSDN and Juejin supplemental."""
from __future__ import annotations
import hashlib
import json
import logging
import re
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup
from .config import AppConfig
from .http import PublicHTTP, SkipPage, SourceUnavailable, RetryablePage, RequestLimitReached
from .runtime import now, LOCAL_TZ, iso_date

logger = logging.getLogger(__name__)
ARTICLE_PATTERNS = {
    "nowcoder": re.compile(r"^/(?:discuss/\d+|feed/main/detail/[\w-]+)$"),
    "csdn": re.compile(r"^/[^/]+/article/details/\d+$"),
    "juejin": re.compile(r"^/post/\d+$"),
}
CONTENT_SELECTORS = {
    "nowcoder": ["div.nc-slate-editor-content", ".post-topic-des", ".feed-content-text"],
    "csdn": ["#content_views"],
    "juejin": [".article-content", ".markdown-body"],
}

@dataclass
class RawPost:
    url: str
    post_id: str
    title: str
    author: str
    publish_time: str | None
    crawl_time: str
    content_raw: str
    source: str = "nowcoder"
    date_rule_version: int = 2

    def to_dict(self) -> dict:
        return asdict(self)

def canonical_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme,p.netloc,p.path.rstrip("/"),"",""))

def post_id_from_url(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/",1)[-1]

def save_raw(raw_dir: Path, post: RawPost) -> Path:
    """Immutable snapshots; a different publication timestamp gets a suffix."""
    from .store import fingerprint
    day = iso_date(post.crawl_time) or now().date().isoformat()
    path = raw_dir / post.source / day / f"{post.post_id}.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("url") == post.url and old.get("publish_time") == post.publish_time:
            return path
        path = path.with_name(f"{post.post_id}-{fingerprint(post.url,post.publish_time or '')[:12]}.json")
    if not path.exists():
        # Complete temp write followed by rename; CLI holds the process lock.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(post.to_dict(),ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.rename(path)
    return path

def _timestamp(value) -> str | None:
    if isinstance(value,(int,float)) or isinstance(value,str) and value.isdigit():
        try:
            stamp = float(value)
            if stamp > 10000000000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp,LOCAL_TZ).isoformat(timespec="seconds")
        except (ValueError,OverflowError,OSError):
            return None
    return str(value) if isinstance(value,str) and iso_date(value) else None


def _nowcoder_record(soup: BeautifulSoup, url: str) -> dict:
    """Only the current SSR post, never comments or similarRecommend records."""
    post_id = post_id_from_url(url)
    for tag in soup.select("script:not([src])"):
        script = tag.get_text()
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*",script)
        if not match:
            continue
        try:
            data = json.JSONDecoder().raw_decode(script[match.end():].lstrip())[0]
        except (ValueError,TypeError):
            continue
        if not isinstance(data,dict) or not isinstance(data.get("prefetchData"),dict):
            continue
        for entry in data["prefetchData"].values():
            if not isinstance(entry,dict) or str(entry.get("contentId","")) != post_id:
                continue
            common = entry.get("ssrCommonData") or {}
            record = common.get("contentData") if isinstance(common,dict) else None
            if isinstance(record,dict) and post_id in {str(record.get(k,"")) for k in ("id","uuid")}:
                return record
    return {}


def _published(soup: BeautifulSoup, html: str, source: str, url: str = "") -> str | None:
    if source == "nowcoder":
        record = _nowcoder_record(soup,url)
        for key in ("createdAt","createTime"):
            value = _timestamp(record.get(key))
            if value:
                return value
        return None
    for selector in ('meta[property="article:published_time"]','meta[itemprop="datePublished"]','meta[name="publishdate"]'):
        tag = soup.select_one(selector)
        if tag:
            value = tag.get("content") or tag.get("datetime")
            if iso_date(value):
                return str(value)
    # Explicit article JSON-LD only, not a regex across comments/recommendations.
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.get_text())
        except ValueError:
            continue
        if isinstance(data,dict) and data.get("@type") in ("Article","BlogPosting","DiscussionForumPosting"):
            if data.get("url") and canonical_url(str(data["url"])) != canonical_url(url):
                continue
            if iso_date(data.get("datePublished")):
                return data["datePublished"]
    for selector in ([".article-info-box .time"] if source == "csdn" else [".article-meta-box time", ".article-info .time"]):
        tag = soup.select_one(selector)
        match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})",tag.get_text() if tag else "")
        if match:
            value = f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}"
            if iso_date(value):
                return value
    return None

def parse_article(url: str, html: str, source: str) -> RawPost:
    """Read only rendered public article content, never hidden paid payloads."""
    soup = BeautifulSoup(html,"lxml")
    page_text = soup.get_text(" ",strip=True)
    pay_markers = ("付费后可阅读", "付费解锁", "购买后阅读", "购买专栏解锁", "开通VIP后", "订阅后可阅读", "会员专享文章", "登录后继续阅读", "登录后查看全文", "登录后可查看", "扫码登录后", "VIP专享文章")
    if any(marker.lower() in page_text.lower() for marker in pay_markers) or soup.select_one(".blog-tags-box .isblogvip, .hide-article-box, .article-paywall, [data-paywall='true']"):
        raise SkipPage("付费/会员/登录限制，整篇跳过")
    if re.search(r'"(?:is_pay|is_paid|isCharge|isVip|need_pay)"\s*:\s*(?:true|1)\b',html,re.I):
        raise SkipPage("付费内容标记，整篇跳过")
    content_el = next((soup.select_one(selector) for selector in CONTENT_SELECTORS[source] if soup.select_one(selector)),None)
    if not content_el:
        raise RetryablePage("公开正文缺失；不以搜索摘要代替全文")
    for tag in content_el.select("script,style,nav,.advertisement,.recommend-box"):
        tag.decompose()
    content = content_el.get_text("\n",strip=True)
    if len(content) < 30:
        raise RetryablePage("正文过短或不完整")
    h1 = soup.select_one("h1")
    meta = soup.select_one('meta[property="og:title"]')
    title = h1.get_text(" ",strip=True) if h1 else (meta.get("content","") if meta else "")
    author_tag = soup.select_one('meta[name="author"]')
    author = author_tag.get("content","") if author_tag else ""
    if not author:
        author_el = soup.select_one(".user-name, #uid, .author-name, .name")
        author = author_el.get_text(" ",strip=True) if author_el else ""
    published = _published(soup,html,source,url)
    if not published:
        raise RetryablePage("缺少可靠发布日期；不能按采集时间假定为新帖")
    return RawPost(canonical_url(url),post_id_from_url(url),title,author,published,now().isoformat(timespec="seconds"),content,source)

class PublicCollector:
    def __init__(self, cfg: AppConfig, source: str):
        self.app = cfg
        self.source = source
        self.cfg = cfg.sources[source]
        self.http = PublicHTTP(cfg.collect,self.cfg.hosts,self.cfg.max_requests_per_run)
        self.store = None
        self.discovery_stats = {
            "discovery_pages": 0,
            "discovery_page_skips": {},
            "links_seen": 0,
            "article_links": 0,
            "unique_candidates": 0,
            "candidates_returned": 0,
            "pagination_links": 0,
            "empty_discovery_pages": 0,
            "discovery_pages_attempted": 0,
            "empty_discovery_responses": 0,
            "discovery_retries": 0,
        }

    def _pagination_links(self, soup, base: str) -> list[str]:
        links = []
        for tag in soup.select('.el-pager a[href], .pagination a[href], .pager a[href], a[rel="next"], link[rel="next"]'):
            p = urlsplit(urljoin(base,tag["href"]))
            if p.scheme != "https" or p.hostname not in self.cfg.hosts:
                continue
            if ARTICLE_PATTERNS[self.source].match(p.path.rstrip("/")):
                continue
            # Keep pagination parameters but normalize order and fragments to break cycles.
            links.append(urlunsplit((p.scheme,p.netloc,p.path or "/",urlencode(sorted(parse_qsl(p.query))),"")))
        return list(dict.fromkeys(links))

    @staticmethod
    def _freshness_hint(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(timezone.utc).timestamp()

    def discover(self) -> list[str]:
        """Bounded discovery from allowed lists, RSS/Atom and sitemaps."""
        candidates = list(self.cfg.seed_urls)
        relevance = {}
        freshness = {}
        pending_pages = self.store.pending_discovery_pages(self.source) if self.store and self.source == "nowcoder" else []
        queue = list(dict.fromkeys([*self.cfg.discovery_urls,*pending_pages]))
        visited = set()
        while queue and len(visited) < self.cfg.max_discovery_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            self.discovery_stats["discovery_pages_attempted"] += 1
            try:
                # A 200 app shell is transient too. Retry it once without inventing
                # an undiscovered next-page URL or bypassing normal HTTP checks.
                for attempt in range(2):
                    response = self.http.get(url)
                    is_xml = response.text.lstrip().startswith("<?xml") or "xml" in response.headers.get("Content-Type", "")
                    soup = BeautifulSoup(response.text,"xml" if is_xml else "lxml")
                    article_links = any(ARTICLE_PATTERNS[self.source].match(urlsplit(urljoin(url,a["href"])).path.rstrip("/")) for a in soup.select("a[href]"))
                    if is_xml or article_links or self._pagination_links(soup,response.url or url):
                        break
                    self.discovery_stats["empty_discovery_responses"] += 1
                    if attempt == 0:
                        self.discovery_stats["discovery_retries"] += 1
                        logger.warning("[%s] 发现页为空，%d 秒后最多复查一次 url=%s",self.source,self.app.collect.page_retry_cooldown,url)
                        time.sleep(self.app.collect.page_retry_cooldown)
            except SourceUnavailable:
                raise
            except SkipPage as exc:
                logger.warning("[%s] 发现页跳过 %s: %s",self.source,url,exc)
                reason = str(exc)
                self.discovery_stats["discovery_page_skips"][reason] = self.discovery_stats["discovery_page_skips"].get(reason,0) + 1
                continue
            self.discovery_stats["discovery_pages"] += 1
            links = []
            next_pages = []
            if is_xml:
                for entry in soup.select("url, item, entry"):
                    tag = entry.select_one("loc, link")
                    if tag:
                        href = tag.get("href") or tag.get_text(strip=True)
                        date_tag = entry.select_one("lastmod, pubDate, published, updated")
                        links.append((href,"",self._freshness_hint(date_tag.get_text(strip=True) if date_tag else "")))
            else:
                pages = self._pagination_links(soup,response.url or url)
                self.discovery_stats["pagination_links"] += len(pages)
                # Skip page 1 when /discuss has already rendered that same first page.
                current = soup.select_one('.el-pager .active a[href]')
                current_url = urljoin(response.url or url,current['href']) if current else None
                current_page_text = dict(parse_qsl(urlsplit(current_url).query)).get("page","") if current_url else ""
                current_page = int(current_page_text) if current_page_text.isdigit() else None
                for page in pages:
                    if current_url and dict(parse_qsl(urlsplit(page).query)) == dict(parse_qsl(urlsplit(current_url).query)):
                        continue
                    page_query = dict(parse_qsl(urlsplit(page).query))
                    if current_page is not None and page_query.get("page", "1").isdigit() and int(page_query.get("page", "1")) != current_page + 1:
                        continue
                    if page not in visited and page not in queue:
                        queue.append(page)
                        next_pages.append(page)
                for tag in soup.select("a[href]"):
                    date_tag = tag.select_one("time[datetime]")
                    links.append((urljoin(url,tag["href"]),tag.get_text(" ",strip=True),self._freshness_hint(date_tag.get("datetime") if date_tag else "")))
                if not any(ARTICLE_PATTERNS[self.source].match(urlsplit(link).path.rstrip("/")) for link,_,_ in links):
                    self.discovery_stats["empty_discovery_pages"] += 1
                    logger.warning("[%s] 公开列表没有文章链接（可能仅为动态页面）；请配置允许的 RSS/站点地图或公开文章 seed_urls：%s",self.source,url)
            self.discovery_stats["links_seen"] += len(links)
            page_candidates = []
            for link,title,hint in links:
                p = urlsplit(link)
                if p.hostname not in self.cfg.hosts or p.scheme != "https":
                    continue
                if p.path.endswith(".xml") and link not in visited and link not in queue:
                    queue.append(link)
                elif ARTICLE_PATTERNS[self.source].match(p.path.rstrip("/")):
                    self.discovery_stats["article_links"] += 1
                    key = canonical_url(link)
                    candidates.append(key)
                    normalized = title.lower()
                    score = sum(w.lower() in normalized for w in self.app.filter.position_words)
                    score += 2 * sum(w.lower() in normalized for w in self.app.collect.keywords)
                    page_candidates.append((key,score))
                    relevance[key] = max(relevance.get(key,0),score)
                    freshness[key] = max(freshness.get(key,0.0),hint)
            if self.store and self.source == "nowcoder":
                self.store.save_discovery_page(self.source,url,page_candidates,next_pages,
                                               keep_pending_on_empty=url not in self.cfg.discovery_urls)
        candidates = list(dict.fromkeys(candidates))
        seeds = set(self.cfg.seed_urls)
        self.discovery_stats["unique_candidates"] = len(candidates)
        candidates.sort(key=lambda u: (u in seeds,freshness.get(u,0.0),relevance.get(u,0),post_id_from_url(u)),reverse=True)
        candidates = candidates[:self.app.collect.max_candidates_per_source]
        self.discovery_stats["candidates_returned"] = len(candidates)
        self.discovery_stats["discovery_stop"] = "page_limit" if queue else "queue_exhausted"
        return candidates

    def fetch(self, url: str) -> RawPost:
        if not ARTICLE_PATTERNS[self.source].match(urlsplit(url).path.rstrip("/")):
            raise SkipPage("不是该来源文章 URL")
        response = self.http.get(url)
        return parse_article(response.url or url,response.text,self.source)

def recover_raw(cfg: AppConfig, store) -> int:
    """Recover a complete immutable raw file written before a DB commit."""
    recovered = 0
    for path in cfg.resolve(cfg.output.raw_dir).glob("*/*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            post = RawPost(**{k:data[k] for k in RawPost.__dataclass_fields__ if k in data})
            if post.source not in cfg.sources or not iso_date(post.publish_time):
                continue
            if not store.has_post(post.url,post.publish_time):
                verified = post.source != "nowcoder" or data.get("date_rule_version") == 2
                store.upsert_post(post,raw_path=path,publication_verified=verified)
                if not verified:
                    store.record_fetch(post.url,post.source,"retryable","publication_recheck",cooldown=0)
                recovered += 1
        except (ValueError,KeyError,TypeError,OSError):
            logger.warning("跳过无效 raw 文件: %s",path.name)
    return recovered

def collect_incremental(cfg: AppConfig, days: int | None = None, sources: list[str] | None = None,
                        metrics: dict | None = None) -> int:
    from .store import Store
    days = days if days is not None else cfg.collect.days
    if days <= 0:
        raise ValueError("days 必须为正数")
    since = now().date() - timedelta(days=days-1)
    window_end = now().date()
    total = 0
    metrics = metrics if metrics is not None else {}
    metrics.update({"window_start": since.isoformat(), "window_end": window_end.isoformat(), "days": days, "sources": {}})
    with Store(cfg.resolve(cfg.output.db_path)) as store:
        recover_raw(cfg,store)
        for source in (sources or list(cfg.sources)):
            if not cfg.sources[source].enabled:
                metrics["sources"][source] = {"status":"disabled"}
                continue
            if source == "github":
                from .github import GitHubCollector
                collector = GitHubCollector(cfg,days)
            else:
                collector = PublicCollector(cfg,source)
                if source == "nowcoder":
                    collector.store = store
            source_stats = {"status":"running", "discovery_pages":0, "links_seen":0,
                            "article_links":0, "unique_candidates":0, "candidates_returned":0,
                            "article_fetch_attempts":0, "article_page_requests":0,
                            "retryable_attempts":0, "retry_pending":0,
                            "retry_limit":0, "retry_cooldown":0, "unprocessed_candidates":0,
                            "article_failures":0, "discovery_failures":0, "articles_started":0,
                            "already_attempted":0, "collected":0,
                            "seen":0, "outside_time_window":0, "skipped":{}, "failures":{}}
            metrics["sources"][source] = source_stats
            if source == "nowcoder":
                source_stats["candidate_queue_before"] = store.candidate_queue_summary(source,cfg.collect.page_attempts_per_day)
            article_failure_recorded = False
            discovery_completed = False
            try:
                if store.collected_today() >= cfg.collect.daily_limit:
                    source_stats["status"] = "daily_limit_reached"
                    break
                if store.collected_today(source) >= cfg.sources[source].max_posts:
                    source_stats["status"] = "source_limit_reached"
                    continue
                # Drain never-attempted candidates before opening more discovery
                # pages. Failed retries alone must not suppress new discovery on
                # every daily run; new candidates also take priority in the batch.
                retries = store.retry_candidates(source,cfg.collect.page_attempts_per_day,cfg.collect.max_candidates_per_source)
                defer_discovery = source == "nowcoder" and store.has_unattempted_candidates(source)
                if defer_discovery:
                    candidates = retries
                    source_stats["discovery_deferred"] = True
                else:
                    candidates = collector.discover()
                    discovery_completed = True
                    discovery_stats = getattr(collector,"discovery_stats",{})
                    source_stats.update(discovery_stats)
                    source_stats["unique_candidates"] = max(discovery_stats.get("unique_candidates",0),len(candidates))
                    source_stats["candidates_returned"] = len(candidates)
                    store.remember_candidates(source,candidates)
                    retries = store.retry_candidates(source,cfg.collect.page_attempts_per_day,cfg.collect.max_candidates_per_source)
                candidates = retries if source == "nowcoder" else list(dict.fromkeys(retries+candidates))[:cfg.collect.max_candidates_per_source]
                source_stats["retry_candidates"] = len(retries)
                source_stats["queued_candidates"] = len(candidates)
                for index,url in enumerate(candidates):
                    source_stats["unprocessed_candidates"] = len(candidates)-index
                    if store.collected_today() >= cfg.collect.daily_limit or store.collected_today(source) >= cfg.sources[source].max_posts:
                        source_stats["status"] = "collection_limit_reached"
                        break
                    source_stats["unprocessed_candidates"] -= 1
                    blocked = store.fetch_block_reason(url,cfg.collect.page_attempts_per_day)
                    if blocked:
                        source_stats[blocked] += 1
                        continue
                    pages_before = getattr(collector.http,"page_requests",0)
                    source_stats["articles_started"] += 1
                    try:
                        post = None
                        for attempt in range(store.attempts_today(url),cfg.collect.page_attempts_per_day):
                            source_stats["article_fetch_attempts"] += 1
                            try:
                                post = collector.fetch(url)
                                break
                            except RetryablePage as exc:
                                store.record_fetch(url,source,"retryable",str(exc),cfg.collect.page_retry_cooldown)
                                source_stats["retryable_attempts"] += 1
                                reason = str(exc)
                                source_stats["skipped"][reason] = source_stats["skipped"].get(reason,0) + 1
                                if attempt + 1 >= cfg.collect.page_attempts_per_day:
                                    logger.warning("[%s] 暂时无法解析，今日重试次数已用尽 url=%s",source,url)
                                    post = None
                                    break
                                wait = cfg.collect.page_retry_cooldown
                                logger.info("[%s] 页面可能暂时不完整，%d 秒后第 %d/%d 次复查",source,wait,attempt+2,cfg.collect.page_attempts_per_day)
                                time.sleep(wait)
                        if post is None:
                            source_stats["retry_pending"] += 1
                            continue
                        day = datetime.fromisoformat(post.publish_time.replace("Z","+00:00")).date()
                        historical_recheck = source == "nowcoder" and store.needs_publication_recheck(post.url)
                        # The collection window limits new posts, not verification
                        # of retained history. Corrected dates get a new immutable
                        # snapshot and normal extraction; future dates stay invalid.
                        if day > window_end or (day < since and not historical_recheck):
                            store.record_fetch(url,source,"skipped","outside_time_window")
                            source_stats["outside_time_window"] += 1
                            continue
                        if store.has_post(post.url,post.publish_time):
                            store.upsert_post(post)  # Mark an existing snapshot's date verified.
                            store.record_fetch(url,source,"seen")
                            source_stats["seen"] += 1
                            continue
                        path = save_raw(cfg.resolve(cfg.output.raw_dir),post)
                        store.upsert_post(post,raw_path=path)
                        store.record_fetch(url,source,"collected")
                        total += 1
                        source_stats["collected"] += 1
                        logger.info("[%s] 已采集 %s",source,post.title)
                    except RequestLimitReached:
                        # A local cap is not an article failure. Keep it eligible.
                        source_stats["unprocessed_candidates"] += 1
                        raise
                    except SourceUnavailable as exc:
                        store.record_fetch(url,source,"source_unavailable",str(exc))
                        article_failure_recorded = True
                        source_stats["article_failures"] += 1
                        reason = str(exc).split("；",1)[0]
                        source_stats["failures"][reason] = source_stats["failures"].get(reason,0) + 1
                        source_stats["status"] = "source_unavailable"
                        raise
                    except (SkipPage,ValueError) as exc:
                        store.record_fetch(url,source,"skipped",str(exc))
                        reason = str(exc)
                        source_stats["skipped"][reason] = source_stats["skipped"].get(reason,0) + 1
                        logger.warning("[%s] 跳过 %s: %s",source,url,exc)
                    except Exception as exc:
                        store.record_fetch(url,source,"failed",type(exc).__name__)
                        source_stats["article_failures"] += 1
                        source_stats["failures"][type(exc).__name__] = source_stats["failures"].get(type(exc).__name__,0)+1
                        logger.error("[%s] 文章失败 url=%s type=%s",source,url,type(exc).__name__)
                    finally:
                        source_stats["article_page_requests"] += getattr(collector.http,"page_requests",pages_before)-pages_before
            except RequestLimitReached:
                source_stats["status"] = "request_limit_reached"
                source_stats["stop_reason"] = "http_request_limit"
                logger.info("[%s] 达到本次 HTTP 请求上限，保留剩余候选",source)
            except (SourceUnavailable,SkipPage) as exc:
                logger.warning("[%s] 本次暂停: %s",source,exc)
                if not article_failure_recorded:
                    source_stats["discovery_failures"] += 1
                    store.record_fetch(f"source:{source}",source,"source_unavailable",str(exc))
                    reason = str(exc).split("；",1)[0]
                    source_stats["failures"][reason] = source_stats["failures"].get(reason,0) + 1
                if source_stats["status"] == "running":
                    source_stats["status"] = "source_unavailable"
            except Exception as exc:
                logger.error("[%s] 来源失败，不影响其他来源: %s",source,type(exc).__name__)
                store.record_fetch(f"source:{source}",source,"failed",type(exc).__name__)
                source_stats["failures"][type(exc).__name__] = source_stats["failures"].get(type(exc).__name__,0) + 1
                source_stats["status"] = "failed"
            finally:
                if source == "nowcoder":
                    source_stats["candidate_queue_after"] = store.candidate_queue_summary(source,cfg.collect.page_attempts_per_day)
                if not discovery_completed:
                    source_stats.update(getattr(collector,"discovery_stats",{}))
                source_stats["http_requests"] = collector.http.requests
                source_stats["http_retries"] = collector.http.retry_requests
                source_stats["http_statuses"] = collector.http.status_counts
                source_stats["discovery_page_requests"] = collector.http.page_requests-source_stats["article_page_requests"]
                if source_stats["status"] == "running":
                    source_stats["status"] = "partial" if source_stats["retry_pending"] or source_stats["article_failures"] or source_stats["retry_limit"] or source_stats["retry_cooldown"] or source_stats.get("empty_discovery_pages") else ("empty_discovery" if not source_stats.get("queued_candidates") else "completed")
                logger.info("采集来源统计 [%s]: %s",source,json.dumps(source_stats,ensure_ascii=False,sort_keys=True))
                collector.http.close()
    metrics["collected"] = total
    logger.info("采集完成: 新增 %d 帖",total)
    return total

def main() -> int:
    import sys
    from .__main__ import main as cli
    return cli(["collect",*sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(main())
