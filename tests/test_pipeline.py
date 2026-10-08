"""Offline acceptance regressions: no real network or LLM calls."""
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import pytest
import requests
from pipeline.config import load_config, DedupConfig
from pipeline.collect import RawPost, save_raw, parse_article, PublicCollector, collect_incremental, recover_raw
from pipeline.store import Store
from pipeline.models import ExtractionResult
from pipeline.extract import Extractor, _merge, _chunk_content, sanitize_result, extract_pass, apply_review
from pipeline.budget import TokenBudget, BudgetPaused
from pipeline.http import PublicHTTP, SkipPage, SourceUnavailable
from pipeline.render import render_all, rank_questions
from pipeline.runtime import now, pipeline_lock

ROOT = Path(__file__).resolve().parents[1]
TODAY = now().date().isoformat()

@pytest.fixture
def cfg(tmp_path,monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY",raising=False)
    monkeypatch.delenv("LLM_API_KEY",raising=False)
    cfg = load_config(ROOT/"config.example.yaml")
    cfg.base_dir = tmp_path
    cfg.output.template_dir = str(ROOT/"templates")
    cfg.llm.api_key = "offline-test"
    cfg.llm.retry_backoff = 0
    # Reliability tests cover the three-attempt queue behavior explicitly;
    # production config uses one page attempt per day to avoid same-run churn.
    cfg.collect.page_attempts_per_day = 3
    return cfg

@pytest.fixture
def store(cfg):
    with Store(cfg.resolve(cfg.output.db_path)) as store:
        store.bind_raw_root(cfg.resolve(cfg.output.raw_dir))
        yield store

def seed(cfg,store,name="a",published=TODAY,content="Redis如何持久化？\n如何解决丢失？",status="to_extract"):
    post = RawPost(f"https://www.nowcoder.com/discuss/{name}",name,"Java面经","作者",published,now().isoformat(),content)
    path = save_raw(cfg.resolve(cfg.output.raw_dir),post)
    return store.upsert_post(post,status,path)

def result(company="甲公司",question="Redis如何持久化？",confidence=.9):
    return ExtractionResult(post_url="",company=company,department="",position="Java后端",position_category="java_backend",rounds=[{"round_name":"一面","date":None,"questions":[{"question":question,"type":"八股","follow_ups":["如何解决丢失？"],"original_text":question}]}],summary="Redis",confidence=confidence,is_interview_post=True)

def payload(value=None):
    data = asdict(value or result())
    data.pop("post_url")
    return json.dumps(data,ensure_ascii=False)

def test_distinct_sources_idempotent_and_followups_retained(cfg,store):
    a,b = seed(cfg,store,"1"),seed(cfg,store,"2")
    store.save_extraction(a,result(),cfg.dedup)
    bresult = result("乙公司")
    bresult.rounds.append({"round_name":"二面","date":None,"questions":[dict(bresult.rounds[0]["questions"][0],follow_ups=["乙公司不同追问"])]})
    for _ in range(2):
        store.save_extraction(b,bresult,cfg.dedup)
    assert store.conn.execute("SELECT times_seen FROM questions").fetchone()[0]==2
    assert len(store.questions_for_render())==3
    assert {r["company"] for r in store.questions_for_render()}=={"甲公司","乙公司"}
    assert any("乙公司不同追问" in r["follow_ups"] for r in store.questions_for_render())

def test_all_time_dedup(cfg,store):
    a=seed(cfg,store,"1",published="2021-01-01")
    b=seed(cfg,store,"2")
    store.save_extraction(a,result(),cfg.dedup)
    store.save_extraction(b,result(),cfg.dedup)
    assert store.status_summary()["questions"]==1

def test_transaction_interruption_rolls_back(cfg,store):
    a=seed(cfg,store)
    store.conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON occurrences WHEN NEW.question='第二题' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    value=result()
    value.rounds[0]["questions"].append(dict(value.rounds[0]["questions"][0],question="第二题"))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_extraction(a,value,cfg.dedup)
    assert store.status_summary()["questions"]==0
    assert store.status_summary()["occurrences"]==0
    assert store.get_post(a)["status"]=="to_extract"
    store.conn.execute("DROP TRIGGER fail_second")
    store.save_extraction(a,value,cfg.dedup)
    assert store.status_summary()["occurrences"]==2

def test_versions_distinct_fingerprints_one_frequency(cfg,store):
    a=seed(cfg,store,"1",published=TODAY)
    b=seed(cfg,store,"1",published=TODAY+"T10:00:00+08:00")
    assert a!=b
    for fp in (a,b):store.save_extraction(fp,result(),cfg.dedup)
    assert store.conn.execute("SELECT times_seen FROM questions").fetchone()[0]==1
    assert len(store.questions_for_render())==1

def test_high_frequency_and_decay(cfg,store):
    for i in range(3):
        a=seed(cfg,store,str(i))
        store.save_extraction(a,result(),cfg.dedup)
    old=seed(cfg,store,"old",published="2021-01-01")
    store.save_extraction(old,result(),cfg.dedup)
    ranks=rank_questions(store.questions_for_render(),cfg)
    assert len(ranks)==1 and ranks[0]["times_seen"]==3
    assert ranks[0]["score"]==3
    assert len(ranks[0]["sources"])==3
    store.set_status(a,"pending_review")
    assert rank_questions(store.questions_for_render(),cfg)==[]

def test_summary_includes_history_without_monthly_files(cfg,store):
    a=seed(cfg,store,"1",published="2021-01-01")
    store.save_extraction(a,result(),cfg.dedup)
    render_all(cfg,store)
    root=cfg.resolve(cfg.output.output_dir)
    assert "Redis" in (root/"Java后端"/"面经汇总.md").read_text(encoding="utf-8")
    assert "2021-01-01" in (root/"Java后端"/"面经汇总.md").read_text(encoding="utf-8")
    assert list((root/"Java后端").glob("????-??.md"))==[]
    assert "暂无满足高频条件" in (root/"高频题"/"近60天高频题Top50.md").read_text(encoding="utf-8")

def test_date_unknown_evidence_and_smalltalk(cfg):
    value=result(question="Redis如何持久化？")
    value.rounds[0]["date"]="2024-08-11"
    value.rounds[0]["questions"].append(dict(value.rounds[0]["questions"][0],question="请先做一个自我介绍"))
    sanitize_result(value,"0811面经\nRedis如何持久化？",cfg)
    assert value.rounds[0]["date"] is None
    assert len(value.rounds[0]["questions"])==1
    value.rounds[0]["questions"][0]["original_text"]="原文没有说的话"
    sanitize_result(value,"Redis如何持久化？",cfg)
    assert value.confidence<.6

def test_zero_confidence_and_noninterview_chunk():
    base=ExtractionResult(post_url="",is_interview_post=False)
    _merge(base,result(confidence=0))
    _merge(base,ExtractionResult(post_url="",is_interview_post=False))
    _merge(base,result(confidence=.9))
    assert base.confidence==0 and base.is_interview_post

def test_chunks_are_bounded_and_lossless():
    text="a"*40+"\n"+"短段落\n"*8
    chunks=_chunk_content(text,8)
    assert max(map(len,chunks))<=8
    assert "".join(chunks)==text

def test_schema_retry_and_failed_status(cfg,store):
    fp=seed(cfg,store)
    with patch.object(Extractor,"_chat",return_value=("bad JSON",1)) as chat:
        extract_pass(cfg,store)
    assert chat.call_count==3
    assert store.get_post(fp)["status"]=="extract_failed"
    store.set_status(fp,"to_extract")
    with patch.object(Extractor,"_chat",side_effect=[("bad",1),(payload(),1)]) as chat:
        extract_pass(cfg,store)
    assert chat.call_count==2
    assert store.get_post(fp)["status"]=="extracted"

@pytest.mark.parametrize("interview,confidence,status",[(False,.9,"discarded"),(True,.3,"pending_review")])
def test_result_routing(cfg,store,interview,confidence,status):
    fp=seed(cfg,store)
    value=result(confidence=confidence)
    value.is_interview_post=interview
    with patch.object(Extractor,"_chat",return_value=(payload(value),1)):
        extract_pass(cfg,store)
    assert store.get_post(fp)["status"]==status

def test_review_apply_idempotent(cfg,store):
    fp=seed(cfg,store)
    with patch.object(Extractor,"_chat",return_value=(payload(result(confidence=.3)),1)):
        extract_pass(cfg,store)
    path=cfg.resolve(cfg.output.pending_dir)/f"{fp}.json"
    data=json.loads(path.read_text(encoding="utf-8"))
    data["result"]["confidence"]=.9
    path.write_text(json.dumps(data),encoding="utf-8")
    apply_review(cfg,store,path)
    assert store.get_post(fp)["status"]=="extracted"
    with pytest.raises(ValueError):apply_review(cfg,store,path)

def test_budget_reservation_persistent_and_unknown(cfg):
    path=cfg.base_dir/"usage.json"
    b=TokenBudget(path,10)
    ident=b.reserve(8,"post",0,0)
    b.settle(ident,None)
    with pytest.raises(BudgetPaused):TokenBudget(path,10).reserve(3,"post",1,0)
    b.settle(ident,2)
    assert TokenBudget(path,10).used_today==2

def test_budget_checked_before_every_retry(cfg):
    e=Extractor(cfg)
    original=e.budget.reserve
    calls=0
    def reserve(*args):
        nonlocal calls
        calls+=1
        if calls>1:raise BudgetPaused("test")
        return original(*args)
    with patch.object(e.budget,"reserve",side_effect=reserve),patch.object(e,"_chat",return_value=("bad",1)) as chat:
        with pytest.raises(BudgetPaused):e._extract_with_retry("prompt")
    assert chat.call_count==1
    e.close()

def test_chunk_checkpoint_resumes_without_rebilling(cfg):
    cfg.llm.chunk_chars=5
    e=Extractor(cfg)
    with patch.object(e,"_extract_with_retry",side_effect=[result(),BudgetPaused("stop")]) as chat:
        with pytest.raises(BudgetPaused):e.extract("title","abcdefghijklmno","test")
    with patch.object(e,"_extract_with_retry",return_value=result()) as chat:
        e.extract("title","abcdefghijklmno","test")
        assert chat.call_count==2
    e.close()

def response(text,status=200,url="https://www.nowcoder.com/discuss/1",headers=None):
    r=requests.Response()
    r.status_code=status
    r._content=text.encode()
    r._content_consumed=True
    r.encoding="utf-8"
    r.url=url
    if headers:r.headers.update(headers)
    return r

def test_robots_denial_never_fetches_article(cfg):
    h=PublicHTTP(cfg.collect,["www.nowcoder.com"])
    with patch.object(h,"_send",return_value=response("User-agent: *\nDisallow: /discuss")) as send:
        with pytest.raises(SkipPage):h.get("https://www.nowcoder.com/discuss/1")
        assert send.call_count==1
    h.close()

def test_missing_robots_fail_closed(cfg):
    h=PublicHTTP(cfg.collect,["www.nowcoder.com"])
    with patch.object(h,"_send",return_value=response("",404)) as send:
        with pytest.raises(SourceUnavailable):h.get("https://www.nowcoder.com/discuss/1")
        assert send.call_count==1
    h.close()

def test_redirect_rechecks_robots(cfg):
    h=PublicHTTP(cfg.collect,["www.nowcoder.com"])
    with patch.object(h,"_send",side_effect=[response("User-agent: *\nDisallow: /search"),response("",302,headers={"Location":"/search"})]) as send:
        with pytest.raises(SkipPage):h.get("https://www.nowcoder.com/discuss/1")
        assert send.call_count==2
    h.close()

def test_network_retry_limit_and_access_denial(cfg):
    h=PublicHTTP(cfg.collect,["www.nowcoder.com"])
    with patch("pipeline.http.time.sleep"),patch.object(h.session,"get",side_effect=requests.ConnectionError()) as get:
        with pytest.raises(SourceUnavailable):h._send("https://www.nowcoder.com/discuss/1")
        assert get.call_count==4
    with patch("pipeline.http.time.sleep"),patch.object(h.session,"get",return_value=response("denied",403)) as get:
        with pytest.raises(SourceUnavailable):h._send("https://www.nowcoder.com/discuss/1")
        assert get.call_count==1
    h.close()

@pytest.mark.parametrize("source,selector,url",[("nowcoder","class='nc-slate-editor-content'","https://www.nowcoder.com/discuss/1"),("csdn","id='content_views'","https://blog.csdn.net/a/article/details/1"),("juejin","class='article-content'","https://juejin.cn/post/1")])
def test_public_parsers_and_paywall(source,selector,url):
    html=f'<h1>Java面经</h1><meta property="article:published_time" content="{TODAY}"><div {selector}>'+('Redis如何持久化？这里是面试官的追问。'*3)+'</div>'
    if source=='nowcoder':
        html += '<script>window.__INITIAL_STATE__='+json.dumps({'prefetchData':{'2':{'contentId':'1','ssrCommonData':{'contentData':{'id':'1','createTime':TODAY}}}}})+'</script>'
    assert parse_article(url,html,source).publish_time==TODAY
    with pytest.raises(SkipPage):parse_article(url,html+"付费后可阅读",source)
    with pytest.raises(SkipPage):parse_article(url,"<h1>只有摘要</h1>",source)

def test_daily_limits_time_window_and_failure_isolation(cfg,store):
    cfg.collect.daily_limit=2
    cfg.sources["csdn"].enabled=False
    cfg.sources["juejin"].enabled=False
    urls=["https://www.nowcoder.com/discuss/1","https://www.nowcoder.com/discuss/2","https://www.nowcoder.com/discuss/3"]
    def fetch(self,url):
        published="2021-01-01" if url.endswith("1") else TODAY
        return RawPost(url,url[-1],"Java面经","",published,now().isoformat(),"正文"*30)
    metrics={}
    with patch.object(PublicCollector,"discover",return_value=urls),patch.object(PublicCollector,"fetch",fetch):
        assert collect_incremental(cfg,7,metrics=metrics)==2
        assert collect_incremental(cfg,7)==0
    assert store.collected_today()==2
    assert not store.has_post(urls[0])
    assert metrics["days"]==7 and metrics["collected"]==2
    assert metrics["sources"]["nowcoder"]["outside_time_window"]==1
    assert metrics["sources"]["nowcoder"]["candidates_returned"]==3

def test_sixty_day_collection_window_includes_day_60_but_not_day_61(cfg,store):
    from datetime import date
    today=date.fromisoformat(TODAY)
    oldest_included=(today-timedelta(days=59)).isoformat()
    oldest_excluded=(today-timedelta(days=60)).isoformat()
    urls=[f"https://www.nowcoder.com/discuss/{i}" for i in range(1,4)]
    published={urls[0]:oldest_included,urls[1]:oldest_excluded,urls[2]:(today+timedelta(days=1)).isoformat()}
    def fetch(self,url):
        return RawPost(url,url.rsplit("/",1)[-1],"Java面经","",published[url],now().isoformat(),"面试正文"*20)
    cfg.sources["csdn"].enabled=False
    cfg.sources["juejin"].enabled=False
    cfg.sources["github"].enabled=False
    metrics={}
    with patch.object(PublicCollector,"discover",return_value=urls),patch.object(PublicCollector,"fetch",fetch):
        assert collect_incremental(cfg,60,metrics=metrics)==1
    assert metrics["window_start"]==oldest_included
    assert metrics["sources"]["nowcoder"]["collected"]==1
    assert metrics["sources"]["nowcoder"]["outside_time_window"]==2

def test_raw_immutable_and_orphan_recovery(cfg,store):
    post=RawPost("https://www.nowcoder.com/discuss/1","1","Java面经","",TODAY,now().isoformat(),"original")
    path=save_raw(cfg.resolve(cfg.output.raw_dir),post)
    before=path.read_bytes()
    post.content_raw="changed"
    save_raw(cfg.resolve(cfg.output.raw_dir),post)
    assert path.read_bytes()==before
    assert recover_raw(cfg,store)==1
    assert recover_raw(cfg,store)==0

def test_process_lock(cfg):
    lock=cfg.base_dir/"pipeline.lock"
    with pipeline_lock(lock):
        with pytest.raises(RuntimeError):
            with pipeline_lock(lock):pass
    with pipeline_lock(lock):pass

@pytest.mark.parametrize("command",[["pipeline","collect"],["pipeline","extract"],["pipeline","render"],["pipeline.collect"],["pipeline.extract"],["pipeline.render"],["pipeline","run"]])
def test_cli_entrypoints_clean_install(cfg,command):
    data=asdict(cfg)
    data.pop("base_dir")
    data["llm"]["api_key"]=""
    for source in data["sources"].values():source["enabled"]=False
    path=cfg.base_dir/"config.yaml"
    path.write_text(json.dumps(data),encoding="utf-8")
    proc=subprocess.run([sys.executable,"-m",*command,"--config",str(path)],cwd=ROOT,capture_output=True)
    assert proc.returncode==0,proc.stderr.decode(errors="replace")
    if command[-1]=="run":
        with Store(cfg.resolve(cfg.output.db_path)) as run_store:
            detail=json.loads(run_store.conn.execute("SELECT detail FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])
        assert detail["window_days"]==60
        assert detail["collection"]["days"]==60
        assert detail["outputs"]
        assert detail["exit_code"]==0

def test_migration_backup_queues_old_extraction(cfg):
    path=cfg.resolve(cfg.output.db_path)
    path.parent.mkdir(parents=True)
    conn=sqlite3.connect(path)
    conn.executescript("CREATE TABLE posts(url TEXT PRIMARY KEY,post_id TEXT,title TEXT,author TEXT,publish_time TEXT,crawl_time TEXT,source TEXT,status TEXT); CREATE TABLE questions(id INTEGER,question TEXT);")
    conn.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,?,?)",("https://www.nowcoder.com/discuss/1","1","Java面经","",TODAY,TODAY,"nowcoder","extracted"))
    conn.execute("INSERT INTO questions VALUES(1,'legacy')")
    conn.commit()
    conn.close()
    with Store(path) as store:
        assert store.conn.execute("SELECT count(*) FROM posts WHERE status='to_extract'").fetchone()[0]==1
        assert store.posts_by_status("to_extract")==[]  # Original post date must be rechecked first.
        assert len(store.retry_candidates('nowcoder',3,80))==1
        assert store.conn.execute("SELECT count(*) FROM legacy_questions").fetchone()[0]==1
    assert len(list(path.parent.glob("*.pre-v2-*.db")))==1

def test_source_failure_does_not_block_other_source(cfg,store):
    cfg.sources["nowcoder"].enabled=True
    cfg.sources["csdn"].enabled=True
    cfg.sources["juejin"].enabled=False
    url="https://blog.csdn.net/test/article/details/123"
    def discover(self):
        if self.source=="nowcoder":raise SourceUnavailable("denied")
        return [url]
    def fetch(self,url):
        return RawPost(url,"123","Java面经","",TODAY,now().isoformat(),"正文"*30,"csdn")
    with patch.object(PublicCollector,"discover",discover),patch.object(PublicCollector,"fetch",fetch):
        assert collect_incremental(cfg,7)==1
    assert store.status_summary()["sources"]=={"csdn":1}
    assert store.conn.execute("SELECT outcome FROM fetch_attempts WHERE url='source:nowcoder'").fetchone()[0]=="source_unavailable"

def test_sitemap_discovery_bounds_and_robots(cfg):
    cfg.sources["juejin"].discovery_urls=["https://juejin.cn/sitemap/posts/index1.xml"]
    cfg.collect.max_candidates_per_source=2
    c=PublicCollector(cfg,"juejin")
    xml='<?xml version="1.0"?><urlset>'+''.join(f'<url><loc>https://juejin.cn/post/{i}</loc></url>' for i in [1,2,3])+'</urlset>'
    with patch.object(c.http,"get",return_value=response(xml)):
        assert c.discover()==["https://juejin.cn/post/3","https://juejin.cn/post/2"]
    c.http.close()

def test_sitemap_lastmod_only_prioritizes_candidates(cfg):
    cfg.collect.max_candidates_per_source=1
    c=PublicCollector(cfg,"juejin")
    xml=f'''<?xml version="1.0"?><urlset>
      <url><loc>https://juejin.cn/post/101</loc><lastmod>{TODAY}T00:00:00Z</lastmod></url>
      <url><loc>https://juejin.cn/post/999</loc><lastmod>2020-01-01T00:00:00Z</lastmod></url>
      <url><loc>https://juejin.cn/post/1000</loc></url>
    </urlset>'''
    with patch.object(c.http,"get",return_value=response(xml)):
        assert c.discover()==["https://juejin.cn/post/101"]
    c.http.close()

def test_discovery_does_not_discard_articles_without_interview_words_in_title(cfg):
    c=PublicCollector(cfg,"nowcoder")
    html='<html><a href="https://www.nowcoder.com/discuss/101">Java 技术总结</a></html>'
    with patch.object(c.http,"get",return_value=response(html)):
        assert c.discover()==["https://www.nowcoder.com/discuss/101"]
    assert c.discovery_stats["article_links"]==1
    assert c.discovery_stats["candidates_returned"]==1
    c.http.close()

def test_missing_date_is_never_assumed_today():
    html='<h1>Java面经</h1><div id="content_views">'+"面试内容"*30+'</div>'
    with pytest.raises(SkipPage,match="发布日期"):
        parse_article("https://blog.csdn.net/a/article/details/1",html,"csdn")

def test_github_public_api_only_and_file_dates(cfg):
    import base64
    from pipeline.github import GitHubCollector
    cfg.sources["github"].repositories=["public/interviews"]
    cfg.sources["github"].max_discovery_pages=1
    c=GitHubCollector(cfg,7)
    url="https://github.com/public/interviews/blob/main/interview.md"
    with patch.object(c,"_api",side_effect=[
        [{"type":"file","name":"interview.md","path":"interview.md","html_url":url}],
        {"type":"file","encoding":"base64","size":50,"content":base64.b64encode("# Java面经\nRedis如何持久化".encode()).decode()},
        [{"commit":{"committer":{"date":TODAY+"T10:00:00Z"}}}],
    ]) as api:
        assert c.discover()==[url]
        post=c.fetch(url)
        assert post.source=="github" and post.publish_time.startswith(TODAY)
        assert all(call.args[0].startswith("/repos/public/interviews/") for call in api.call_args_list)
    with pytest.raises(SkipPage):c._api("/login")
    c.http.close()

def test_schema_missing_evidence_retries(cfg):
    value=json.loads(payload())
    del value["rounds"][0]["questions"][0]["original_text"]
    e=Extractor(cfg)
    with patch.object(e,"_chat",side_effect=[(json.dumps(value),1),(payload(),1)]) as chat:
        e._extract_with_retry("prompt")
        assert chat.call_count==2
    e.close()

def test_reasoning_effort_uses_completion_tokens_without_temperature(cfg):
    from types import SimpleNamespace
    from unittest.mock import Mock
    cfg.llm.api_key="offline-test"
    cfg.llm.reasoning_effort="high"
    e=Extractor(cfg)
    fake=Mock()
    fake.chat.completions.create.return_value=SimpleNamespace(
        usage=SimpleNamespace(total_tokens=12),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    e.client.close()
    e.client=fake
    assert e._chat("probe")==("{}",12)
    request=fake.chat.completions.create.call_args.kwargs
    assert request["reasoning_effort"]=="high"
    assert request["max_completion_tokens"]==cfg.llm.max_tokens
    assert "temperature" not in request and "max_tokens" not in request
    e.close()

def test_generic_llm_api_key_env_precedes_legacy_and_config(cfg,monkeypatch):
    monkeypatch.setenv("LLM_API_KEY","generic-env-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY","legacy-env-key")
    assert load_config(ROOT/"config.example.yaml").llm.api_key=="generic-env-key"

def test_budget_day_rollover(cfg):
    from datetime import datetime
    import pipeline.budget as budget_module
    current=now()
    path=cfg.base_dir/"usage.json"
    b=TokenBudget(path,10)
    b.reserve(10,"post",0,0)
    with patch.object(budget_module,"now",return_value=current+timedelta(days=1)):
        next_day=TokenBudget(path,10)
        assert next_day.used_today==0
        next_day.reserve(10,"next-post",0,0)
    assert path.with_name(f"usage-{current.date().isoformat()}.json").exists()

def test_recent_questions_outrank_more_old_occurrences(cfg):
    rows=[]
    for key,count,age in [("recent",3,0),("older",5,14)]:
        for i in range(count):
            rows.append({"hash":key,"source_url":f"https://example.test/{key}/{i}","publish_time":(now().date()-timedelta(days=age)).isoformat(),"question":key,"q_type":"八股","company":"公司","position_category":"java_backend","post_title":"面经"})
    ranks=rank_questions(rows,cfg)
    assert [r["question"] for r in ranks]==["recent","older"]
    assert ranks[1]["score"]==2.5

def test_frequency_ranking_uses_sixty_day_window(cfg):
    from datetime import date
    today=date.fromisoformat(TODAY)
    rows=[]
    for key,age in [("day-60",59),("day-61",60)]:
        for i in range(3):
            rows.append({"hash":key,"source_url":f"https://example.test/{key}/{i}","publish_time":(today-timedelta(days=age)).isoformat(),"question":key,"q_type":"八股","company":"公司","position_category":"java_backend","post_title":"面经"})
    assert [row["question"] for row in rank_questions(rows,cfg)]==["day-60"]

def test_revalidate_removes_smalltalk_without_llm(cfg,store):
    from pipeline.extract import revalidate
    fp=seed(cfg,store,content="个人介绍\nRedis如何持久化？")
    value=result()
    value.rounds[0]["questions"].append(dict(value.rounds[0]["questions"][0],question="个人介绍",original_text="个人介绍"))
    store.save_extraction(fp,value,cfg.dedup)
    with patch.object(Extractor,"_chat",side_effect=AssertionError("must not call LLM")):
        revalidate(cfg,store)
    assert [r["question"] for r in store.questions_for_render()]==["Redis如何持久化？"]

def test_minimum_request_interval_cannot_be_disabled(cfg):
    data=asdict(cfg)
    data.pop("base_dir")
    data["collect"]["request_interval"]=0
    path=cfg.base_dir/"invalid.yaml"
    path.write_text(json.dumps(data),encoding="utf-8")
    with pytest.raises(ValueError,match="至少 5 秒"):load_config(path)

@pytest.mark.skipif(sys.platform!="win32",reason="Windows batch entry point")
def test_batch_first_launch_and_exit_code(cfg,monkeypatch):
    import shutil
    import os
    # The temporary project has no .venv; exercise fallback with the same
    # dependency-equipped interpreter that is running this offline suite.
    monkeypatch.setenv("PATH",str(Path(sys.executable).parent)+os.pathsep+os.environ.get("PATH",""))
    shutil.copytree(ROOT/"pipeline",cfg.base_dir/"pipeline",ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT/"run_daily.bat",cfg.base_dir/"run_daily.bat")
    data=asdict(cfg)
    data.pop("base_dir")
    data["llm"]["api_key"]=""
    for source in data["sources"].values():source["enabled"]=False
    path=cfg.base_dir/"config.yaml"
    path.write_text(json.dumps(data),encoding="utf-8")
    cmd=["cmd.exe","/d","/c",str(cfg.base_dir/"run_daily.bat"),"scheduled"]
    proc=subprocess.run(cmd,cwd=cfg.base_dir,capture_output=True)
    assert proc.returncode==0,proc.stderr.decode(errors="replace")
    assert (cfg.base_dir/"logs"/"run_daily.log").exists()
    data["collect"]["request_interval"]=0
    path.write_text(json.dumps(data),encoding="utf-8")
    proc=subprocess.run(cmd,cwd=cfg.base_dir,capture_output=True)
    assert proc.returncode==1
