"""配置加载:唯一的配置入口,其余模块只从这里拿参数。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class CollectConfig:
    keywords: list[str] = field(default_factory=lambda: ["Java后端 面经", "Agent 面经"])
    request_interval: int = 5
    daily_limit: int = 200
    max_pages_per_keyword: int = 2
    max_retries: int = 3
    cookie: str = field(default="",repr=False)  # legacy setting, deliberately unused
    days: int = 60
    timeout: int = 20
    user_agent: str = "MianjingPipeline/0.2 (personal study; respects robots.txt)"
    max_requests_per_source: int = 40
    max_candidates_per_source: int = 80
    max_response_bytes: int = 12000000
    retry_backoff: int = 5
    page_attempts_per_day: int = 3
    page_retry_cooldown: int = 30


@dataclass
class SourceConfig:
    enabled: bool = True
    hosts: list[str] = field(default_factory=list)
    discovery_urls: list[str] = field(default_factory=list)
    seed_urls: list[str] = field(default_factory=list)
    max_posts: int = 150
    max_discovery_pages: int = 3
    max_requests_per_run: int | None = None
    repositories: list[str] = field(default_factory=list)
    search_query: str = "面经"


def default_sources() -> dict[str, SourceConfig]:
    """Only public HTML listings; prohibited search and private APIs are excluded."""
    return {
        "nowcoder": SourceConfig(hosts=["www.nowcoder.com"], discovery_urls=["https://www.nowcoder.com/discuss"]),
        "csdn": SourceConfig(hosts=["blog.csdn.net"], discovery_urls=["https://blog.csdn.net/"], max_posts=25),
        "juejin": SourceConfig(hosts=["juejin.cn"], discovery_urls=["https://juejin.cn/sitemap/posts/index1.xml"], max_posts=25,max_discovery_pages=1),
        "github": SourceConfig(enabled=False,hosts=["api.github.com"],max_posts=10,max_discovery_pages=2),
    }


@dataclass
class DedupConfig:
    threshold: float = .9
    short_len: int = 30
    synonyms: dict[str, str] = field(default_factory=lambda: {
        "请问": "", "问一下": "", "面试官问": "", "讲一下": "", "说说": "",
    })


@dataclass
class FilterConfig:
    interview_words: list[str] = field(default_factory=list)
    position_words: list[str] = field(default_factory=list)
    excluded_questions: list[str] = field(default_factory=lambda: ["自我介绍", "个人介绍", "介绍自己", "你有什么想问", "有什么要问", "反问环节"])


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = field(default="", repr=False)
    model: str = "deepseek-chat"
    daily_token_budget: int = 500000
    chunk_chars: int = 3000
    max_tokens: int = 8192
    context_tokens: int = 64000
    max_retries: int = 2
    timeout: int = 90
    confidence_threshold: float = .6
    temperature: float = .1
    reasoning_effort: str = ""
    retry_backoff: int = 5


@dataclass
class OutputConfig:
    raw_dir: str = "raw"
    db_path: str = "data/interviews.db"
    output_dir: str = "output"
    pending_dir: str = "output/pending"
    template_dir: str = "templates"
    log_dir: str = "logs"
    high_frequency_min_posts: int = 3
    top_limit: int = 50
    recency_days: int = 60
    half_life_days: float = 14
    log_max_bytes: int = 5000000
    log_backups: int = 3


@dataclass
class AppConfig:
    collect: CollectConfig
    filter: FilterConfig
    llm: LLMConfig
    output: OutputConfig
    base_dir: Path = PROJECT_ROOT
    sources: dict[str, SourceConfig] = field(default_factory=default_sources)
    dedup: DedupConfig = field(default_factory=DedupConfig)

    def resolve(self, relative: str) -> Path:
        """把配置中的相对路径解析为基于项目根目录的绝对路径。"""
        return (self.base_dir / relative).resolve()


def load_config(path: str | Path | None = None) -> AppConfig:
    """读取 config.yaml;文件缺失或字段缺失时使用保守默认值。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        logger.warning("配置文件不存在: %s,使用默认配置", cfg_path)

    collect = CollectConfig(**(data.get("collect") or {}))
    filt = FilterConfig(**(data.get("filter") or {}))
    llm = LLMConfig(**(data.get("llm") or {}))
    output = OutputConfig(**(data.get("output") or {}))
    llm.api_key = os.environ.get("LLM_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "") or llm.api_key
    sources = default_sources()
    for name, source in (data.get("sources") or {}).items():
        if name not in sources:
            raise ValueError(f"不支持的数据源: {name}")
        from dataclasses import asdict
        sources[name] = SourceConfig(**(asdict(sources[name]) | source))
    cfg = AppConfig(collect=collect, filter=filt, llm=llm, output=output,
                    base_dir=cfg_path.resolve().parent, sources=sources,
                    dedup=DedupConfig(**(data.get("dedup") or {})))
    if collect.request_interval < 5:
        raise ValueError("collect.request_interval 必须至少 5 秒")
    if not 0 <= collect.max_retries <= 3 or not 0 <= llm.max_retries <= 2:
        raise ValueError("采集重试最多 3 次，LLM 重试最多 2 次")
    if not 1 <= collect.page_attempts_per_day <= 3 or collect.page_retry_cooldown < 0 or llm.retry_backoff < 0:
        raise ValueError("页面日重试次数必须为 1-3，冷却时间不能为负数")
    if min(collect.days, collect.daily_limit, collect.max_requests_per_source,
           collect.max_candidates_per_source, collect.max_response_bytes,
           collect.timeout, llm.daily_token_budget, llm.chunk_chars,
           llm.max_tokens, llm.timeout, output.half_life_days, output.recency_days,
           output.top_limit) <= 0:
        raise ValueError("限额、时间窗、超时、分块大小必须为正数")
    if llm.context_tokens <= llm.max_tokens or not 0 <= llm.confidence_threshold <= 1:
        raise ValueError("模型上下文/置信度配置无效")
    if llm.reasoning_effort not in ("", "low", "medium", "high"):
        raise ValueError("llm.reasoning_effort 必须为空、low、medium 或 high")
    if not .9 <= cfg.dedup.threshold <= 1 or cfg.dedup.short_len <= 0 or output.high_frequency_min_posts < 3:
        raise ValueError("去重相似阈值至少 .9，高频题至少 3 个不同帖子")
    for source in sources.values():
        if source.max_posts < 0 or source.max_discovery_pages < 0:
            raise ValueError("来源限额不能为负数")
        if source.max_requests_per_run is not None and source.max_requests_per_run <= 0:
            raise ValueError("来源 HTTP 请求上限必须为正数")
    return cfg


def setup_logging(log_dir: Path, verbose: bool = False, max_bytes: int = 5000000, backups: int = 3) -> None:
    """日志同时输出到控制台与 logs/ 目录(UTF-8)。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "pipeline.log", encoding="utf-8", maxBytes=max_bytes, backupCount=backups
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
