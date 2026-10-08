"""One cumulative Markdown per category and distinct-post recency ranking."""
from __future__ import annotations
import json
import logging
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from .config import AppConfig
from .runtime import now, iso_date

CATEGORY_DIRS = {"java_backend":"Java后端","agent_ai":"Agent开发","other":"其他"}
DISCLAIMER = "内容来源于网络公开面经，版权归原作者所有，仅供个人学习。"
logger = logging.getLogger(__name__)

def _publication_date(row) -> str | None:
    """Return only the original post's publication date."""
    value = row["publish_time"] if "publish_time" in row.keys() else ""
    return iso_date(value)

def md(value) -> str:
    return re.sub(r"([\\`*_{}\[\]<>|])",r"\\\1",str(value or "").replace("\n"," ").replace("\r"," "))

def _group_company_posts(rows) -> list[dict]:
    groups = defaultdict(dict)
    for row in rows:
        posts = groups[row["company"] or "未知公司"]
        post = posts.setdefault(row["source_url"],{
            "title":row["post_title"] or "未命名面经", "source_url":row["source_url"],
            "publish_date":_publication_date(row) or "", "rounds":defaultdict(list),
        })
        lines = [f"- **{md(row['question'])}** `{md(row['q_type'])}`"]
        for follow in json.loads(row["follow_ups"]):
            lines.append(f"  - 追问：{md(follow)}")
        post["rounds"][(row["round_name"] or "未标注轮次",row["round_date"] or "未明确")].append("\n".join(lines))
    return [{"company":company,"posts":[
        post | {"rounds":[{"round_name":name,"date":day,"block":"\n".join(items)} for (name,day),items in sorted(post["rounds"].items())]}
        for post in sorted(posts.values(),key=lambda p:(p["publish_date"],p["source_url"]),reverse=True)
    ]} for company,posts in sorted(groups.items())]

def rank_questions(rows, cfg: AppConfig, today: date | None = None) -> list[dict]:
    today = today or now().date()
    since = today-timedelta(days=cfg.output.recency_days-1)
    groups = defaultdict(dict)
    for row in rows:
        published_date = _publication_date(row)
        if not published_date:
            continue
        day = date.fromisoformat(published_date)
        if not since<=day<=today:
            continue
        # One source URL contributes once, even across versions/rounds.
        old = groups[row["hash"]].get(row["source_url"])
        if old is None or row["publish_time"] > old["publish_time"]:
            groups[row["hash"]][row["source_url"]] = row
    ranked = []
    for h,posts in groups.items():
        if len(posts)<cfg.output.high_frequency_min_posts:
            continue
        values = list(posts.values())
        first = values[0]
        ranked.append({"hash":h,"question":first["question"],"q_type":first["q_type"],
            "companies":"、".join(sorted({r["company"] or "未知公司" for r in values})),
            "position_category":"、".join(sorted({CATEGORY_DIRS.get(r["position_category"],"其他") for r in values})),
            "times_seen":len(posts),"last_seen":max(_publication_date(r) for r in values),
            "score":sum(2**(-((today-date.fromisoformat(_publication_date(r))).days)/cfg.output.half_life_days) for r in values),
            "sources":[{"url":url,"title":r["post_title"]} for url,r in sorted(posts.items())]})
    ranked.sort(key=lambda r:(-r["score"],-r["times_seen"],r["hash"]))
    return ranked[:cfg.output.top_limit]

def _write(path: Path, content: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_suffix(path.suffix+".tmp")
    temp.write_text(content,encoding="utf-8")
    os.replace(temp,path)

def render_all(cfg: AppConfig,store,metrics: dict | None = None) -> list[Path]:
    env = Environment(loader=FileSystemLoader(cfg.resolve(cfg.output.template_dir)),undefined=StrictUndefined,autoescape=False)
    env.filters["md"] = md
    rows = store.questions_for_render()
    category_groups = defaultdict(list)
    for row in rows:
        published_date = _publication_date(row)
        if published_date and published_date <= now().date().isoformat():
            category_groups[row["position_category"]].append(row)
    for category in CATEGORY_DIRS:
        category_groups.setdefault(category,[])
    root = cfg.resolve(cfg.output.output_dir)
    written = []
    for category,items in sorted(category_groups.items()):
        dirname = CATEGORY_DIRS.get(category,"其他")
        path = root/dirname/"面经汇总.md"
        _write(path,env.get_template("summary.md.j2").render(title=f"{dirname} 面经汇总",generated_at=now().isoformat(timespec="seconds"),count=len(items),companies=sorted({r["company"] or "未知公司" for r in items}),groups=_group_company_posts(items),disclaimer=DISCLAIMER))
        written.append(path)
    # Preserve old monthly output outside the category folders. Only migrate
    # recognized generated files, after new summaries have been written.
    archived = 0
    archive_root = cfg.resolve(cfg.output.db_path).parent/"output-history"/now().strftime('%Y%m%d-%H%M%S-%f')
    for dirname in CATEGORY_DIRS.values():
        for old in (root/dirname).glob('????-??.md'):
            if not re.fullmatch(r'\d{4}-\d{2}',old.stem):
                continue
            if DISCLAIMER not in old.read_text(encoding='utf-8'):
                continue
            target = archive_root/dirname/old.name
            target.parent.mkdir(parents=True,exist_ok=True)
            old.rename(target)
            archived += 1
    path = root/"高频题"/f"近{cfg.output.recency_days}天高频题Top{cfg.output.top_limit}.md"
    ranked = rank_questions(rows,cfg)
    _write(path,env.get_template("top.md.j2").render(title=f"近{cfg.output.recency_days}天高频题 Top{cfg.output.top_limit}",items=ranked,generated_at=now().isoformat(timespec="seconds"),disclaimer=DISCLAIMER,min_posts=cfg.output.high_frequency_min_posts,half_life=cfg.output.half_life_days))
    written.append(path)
    metrics = metrics if metrics is not None else {}
    since = (now().date()-timedelta(days=cfg.output.recency_days-1)).isoformat()
    recent = [r for r in rows if since<=(_publication_date(r) or '')<=now().date().isoformat()]
    sources = defaultdict(set)
    for row in recent:
        sources[row['hash']].add(row['source_url'])
    eligible = sum(len(urls)>=cfg.output.high_frequency_min_posts for urls in sources.values())
    metrics.update(input_occurrences=len(rows),summary_occurrences=sum(len(v) for v in category_groups.values()),
                   summary_posts=len({r['source_url'] for items in category_groups.values() for r in items}),
                   archived_monthly_files=archived,
                   ranking_days=cfg.output.recency_days,ranking_recent_occurrences=len(recent),
                   ranking_unique_questions=len(sources),ranking_below_min_posts=len(sources)-eligible,
                   ranking_eligible=eligible,ranking_top_limit_omitted=max(0,eligible-len(ranked)),
                   ranking_rendered=len(ranked),files=len(written))
    logger.info("渲染统计：%s",json.dumps(metrics,ensure_ascii=False,sort_keys=True))
    return written

def main() -> int:
    import sys
    from .__main__ import main as cli
    return cli(["render",*sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(main())
