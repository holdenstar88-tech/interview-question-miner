"""Regressions for public pagination, retries, original-post dates and diagnostics."""
import json
import sqlite3
from datetime import timedelta
from unittest.mock import patch, Mock
from types import SimpleNamespace

import pytest

from test_pipeline import cfg, store, seed, result, payload, response, TODAY
from pipeline.config import load_config
from pipeline.collect import PublicCollector, RawPost, collect_incremental, parse_article
from pipeline.extract import Extractor, ExtractFailed, coarse_filter_pass, _parse_json_loose, _exception_chain
from pipeline.http import RetryablePage, SkipPage, SourceUnavailable, PublicHTTP, RequestLimitReached


def test_source_request_cap_override_and_default(cfg):
    nowcoder = PublicCollector(cfg, 'nowcoder')
    csdn = PublicCollector(cfg, 'csdn')
    try:
        assert nowcoder.http.max_requests == 320
        assert csdn.http.max_requests == cfg.collect.max_requests_per_source == 40
        nowcoder.http.max_requests = 2
        with patch.object(nowcoder.http.session, 'get', return_value=response('ok')) as get, patch('pipeline.http.time.sleep'):
            nowcoder.http._send('https://www.nowcoder.com/discuss/1')
            nowcoder.http._send('https://www.nowcoder.com/discuss/2')
            with pytest.raises(RequestLimitReached):
                nowcoder.http._send('https://www.nowcoder.com/discuss/3')
        assert get.call_count == nowcoder.http.requests == 2
    finally:
        nowcoder.http.close()
        csdn.http.close()


def test_invalid_source_request_cap_rejected(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('sources:\n  nowcoder:\n    max_requests_per_run: 0\n', encoding='utf-8')
    with pytest.raises(ValueError, match='来源 HTTP 请求上限'):
        load_config(path)


def test_unprocessed_discovery_survives_next_run(cfg, store):
    urls = ['https://www.nowcoder.com/discuss/1', 'https://www.nowcoder.com/discuss/2']
    post = lambda url: RawPost(url, url.rsplit('/', 1)[-1], 'Java面经', '', TODAY,
                               now().isoformat(), 'Java 面试 Redis 如何持久化？' * 3)
    with patch.object(PublicCollector, 'discover', return_value=urls), \
         patch.object(PublicCollector, 'fetch', side_effect=[post(urls[0]), RequestLimitReached('local cap')]):
        first = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=first) == 1
    assert first['sources']['nowcoder']['unprocessed_candidates'] == 1
    assert store.conn.execute('SELECT status FROM candidate_queue WHERE url=?', (urls[1],)).fetchone()[0] == 'pending'
    with patch.object(PublicCollector, 'discover', return_value=[]), \
         patch.object(PublicCollector, 'fetch', return_value=post(urls[1])) as fetch:
        second = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=second) == 1
    fetch.assert_called_once_with(urls[1])
    assert second['sources']['nowcoder']['unprocessed_candidates'] == 0


def test_nowcoder_queue_is_drained_before_discovery(cfg, store):
    url = 'https://www.nowcoder.com/discuss/queued-first'
    post = RawPost(url, 'queued-first', 'Java面经', '', TODAY, now().isoformat(),
                   'Java 面试 Redis 如何持久化？' * 3)
    store.remember_candidates('nowcoder', [url])
    with patch.object(PublicCollector, 'discover', side_effect=AssertionError('discovery must be deferred')), \
         patch.object(PublicCollector, 'fetch', return_value=post) as fetch:
        metrics = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=metrics) == 1
    fetch.assert_called_once_with(url)
    assert metrics['sources']['nowcoder']['discovery_deferred'] is True
    assert metrics['sources']['nowcoder']['candidate_queue_after']['pending'] == 0


def test_discovered_candidate_survives_interruption(cfg, store):
    urls = ['https://www.nowcoder.com/discuss/1', 'https://www.nowcoder.com/discuss/2']
    post = RawPost(urls[1], '2', 'Java面经', '', TODAY, now().isoformat(),
                   'Java 面试 Redis 如何持久化？' * 3)
    with patch.object(PublicCollector, 'discover', return_value=urls), \
         patch.object(PublicCollector, 'fetch', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            collect_incremental(cfg, sources=['nowcoder'])

    assert [row[0] for row in store.conn.execute(
        "SELECT url FROM candidate_queue WHERE source='nowcoder' AND status='pending' ORDER BY url"
    )] == urls
    with patch.object(PublicCollector, 'discover', return_value=[]), \
         patch.object(PublicCollector, 'fetch', side_effect=lambda url: post if url == urls[1] else SkipPage('test skip')) as fetch:
        metrics = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=metrics) == 1

    assert [call.args[0] for call in fetch.call_args_list] == urls
    assert metrics['sources']['nowcoder']['retry_candidates'] == 2
    assert metrics['sources']['nowcoder']['unprocessed_candidates'] == 0


def test_discovery_page_and_candidates_resume_after_request_cap(cfg, store):
    first = 'https://www.nowcoder.com/discuss/1'
    second = 'https://www.nowcoder.com/discuss/2'
    next_page = 'https://www.nowcoder.com/?page=2'
    page_one = ('<a href="/discuss/1">Java 面经</a><ul class="el-pager">'
                '<li class="active"><a href="/?page=1">1</a></li>'
                '<li><a href="/?page=2">2</a></li></ul>')

    store.remember_candidates('nowcoder', [first])
    store.save_discovery_page('nowcoder', cfg.sources['nowcoder'].discovery_urls[0], [], [next_page])

    def fetched(_collector, url):
        return RawPost(url, url.rsplit('/', 1)[-1], 'Java面经', '', TODAY,
                       now().isoformat(), 'Java 面试 Redis 如何持久化？' * 3)

    with patch.object(PublicCollector, 'discover', side_effect=AssertionError('queued work goes first')), \
         patch.object(PublicCollector, 'fetch', autospec=True, side_effect=fetched):
        metrics = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=metrics) == 1
    assert metrics['sources']['nowcoder']['discovery_deferred'] is True
    assert store.pending_discovery_pages('nowcoder') == [next_page]

    def resumed_get(_http, url):
        if url == next_page:
            return response('<a href="/discuss/2">Java 面经</a>', url=url)
        return response('<h1>empty public list</h1>', url=url)

    with patch.object(PublicHTTP, 'get', autospec=True, side_effect=resumed_get), \
         patch.object(PublicCollector, 'fetch', autospec=True, side_effect=fetched) as fetch, \
         patch('pipeline.collect.time.sleep'):
        metrics = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=metrics) == 1
    assert {call.args[1] for call in fetch.call_args_list} == {second}
    assert store.pending_discovery_pages('nowcoder') == []
    assert metrics['sources']['nowcoder']['candidate_queue_after']['pending'] == 0


def test_candidates_beyond_run_batch_remain_queued(cfg, store):
    cfg.collect.max_candidates_per_source = 1
    urls = [f'https://www.nowcoder.com/discuss/{i}' for i in (1, 2, 3)]
    html = ''.join(f'<a href="/discuss/{i}">Java 面经</a>' for i in (1, 2, 3))

    def fetched(_collector, url):
        return RawPost(url, url.rsplit('/', 1)[-1], 'Java面经', '', TODAY,
                       now().isoformat(), 'Java 面试 Redis 如何持久化？' * 3)

    with patch.object(PublicHTTP, 'get', return_value=response(html)), \
         patch.object(PublicCollector, 'fetch', autospec=True, side_effect=fetched):
        metrics = {}
        assert collect_incremental(cfg, sources=['nowcoder'], metrics=metrics) == 1
    assert metrics['sources']['nowcoder']['candidates_returned'] == 1
    assert metrics['sources']['nowcoder']['candidate_queue_after']['pending'] == 2
    with patch.object(PublicCollector, 'discover', return_value=[]), \
         patch.object(PublicCollector, 'fetch', autospec=True, side_effect=fetched):
        assert collect_incremental(cfg, sources=['nowcoder']) == 1
        assert collect_incremental(cfg, sources=['nowcoder']) == 1
    assert store.candidate_queue_summary('nowcoder', 3)['pending'] == 0
    assert {row[0] for row in store.conn.execute('SELECT url FROM candidate_queue')} == set(urls)
    with patch('pipeline.store.now', return_value=now() + timedelta(days=1)), \
         patch.object(PublicCollector, 'discover', return_value=urls), \
         patch.object(PublicCollector, 'fetch') as fetch:
        assert collect_incremental(cfg, sources=['nowcoder']) == 0
    fetch.assert_not_called()


def test_empty_saved_discovery_page_stays_pending(cfg, store):
    cfg.sources['nowcoder'].discovery_urls = []
    page = 'https://www.nowcoder.com/?page=2'
    store.save_discovery_page('nowcoder', 'https://www.nowcoder.com/discuss', [], [page])
    collector = PublicCollector(cfg, 'nowcoder')
    collector.store = store
    try:
        with patch.object(collector.http, 'get', return_value=response('<h1>empty</h1>', url=page)) as get, \
             patch('pipeline.collect.time.sleep'):
            assert collector.discover() == []
        assert get.call_count == 2
        assert store.pending_discovery_pages('nowcoder') == [page]
        with patch.object(collector.http, 'get', return_value=response('<a href="/discuss/3">Java 面经</a>', url=page)):
            assert collector.discover() == ['https://www.nowcoder.com/discuss/3']
        assert store.pending_discovery_pages('nowcoder') == []
    finally:
        collector.http.close()


def test_rediscovered_page_returns_to_pending_frontier(store):
    entry = 'https://www.nowcoder.com/discuss'
    page = 'https://www.nowcoder.com/?page=2'
    store.save_discovery_page('nowcoder', page, [('https://www.nowcoder.com/discuss/1', 1)], [])
    assert store.pending_discovery_pages('nowcoder') == []
    store.save_discovery_page('nowcoder', entry, [], [page])
    assert store.pending_discovery_pages('nowcoder') == [page]


def test_source_failure_deferred_until_next_day(cfg, store):
    url = 'https://www.nowcoder.com/discuss/123'
    store.record_fetch(url, 'nowcoder', 'source_unavailable', 'HTTP 521 server error')
    assert store.retry_candidates('nowcoder', 3, 80) == []
    assert store.candidate_queue_summary('nowcoder', 3)['deferred_today'] == 1
    with patch('pipeline.store.now', return_value=now() + timedelta(days=1)):
        assert store.retry_candidates('nowcoder', 3, 80) == [url]
        assert store.candidate_queue_summary('nowcoder', 3)['ready'] == 1


def test_v3_candidate_queue_migration_backfills_and_backs_up(cfg):
    path = cfg.resolve(cfg.output.db_path)
    with Store(path) as st:
        st.record_fetch('https://www.nowcoder.com/discuss/1', 'nowcoder', 'retryable', 'body missing')
        st.record_fetch('https://www.nowcoder.com/discuss/2', 'nowcoder', 'collected')
        st.conn.executescript('DROP TABLE candidate_queue; DROP TABLE discovery_pages; PRAGMA user_version=3;')
    with Store(path) as st:
        assert st.conn.execute('PRAGMA user_version').fetchone()[0] == 4
        assert st.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert st.conn.execute("SELECT status FROM candidate_queue WHERE url LIKE '%/1'").fetchone()[0] == 'pending'
        assert st.conn.execute("SELECT status FROM candidate_queue WHERE url LIKE '%/2'").fetchone()[0] == 'done'
    assert len(list(path.parent.glob('*.pre-v4-*.db'))) == 1


@pytest.mark.parametrize('wrapper',[
    '{}', '\ufeff{}', '```json\n{}\n```',
    '<think>private reasoning with {{braces}}</think>\n{}',
    '<think>private reasoning</think>\n```JSON\n{}\n```',
])
def test_closed_relay_wrappers_keep_schema_validation(cfg,wrapper):
    e=Extractor(cfg)
    try:
        with patch.object(e,'_chat',return_value=(wrapper.format(payload()),20)):
            assert e._extract_with_retry('private prompt').is_interview_post
    finally:
        e.close()


@pytest.mark.parametrize('text',[
    '<think>unclosed '+payload(), 'Explanation: '+payload(),
    payload()+' trailing text', payload()+payload(),
    '```json\n'+payload(), '<think>closed</think>[]',
])
def test_ambiguous_or_incomplete_wrappers_rejected(text):
    assert _parse_json_loose(text) is None


def test_connection_cause_is_safe():
    inner=OSError(10054,'private key and body')
    outer=RuntimeError('private prompt')
    outer.__cause__=inner
    assert _exception_chain(outer)==[{'type':'RuntimeError'},{'type':type(inner).__name__,'errno':10054}]


@pytest.mark.parametrize('during_discovery',[False,True])
def test_request_cap_is_not_server_or_article_failure(cfg,store,during_discovery):
    metrics={}
    url='https://www.nowcoder.com/discuss/limit'
    discovery={'side_effect':RequestLimitReached('local cap')} if during_discovery else {'return_value':[url]}
    with patch.object(PublicCollector,'discover',**discovery),patch.object(PublicCollector,'fetch',side_effect=RequestLimitReached('local cap')):
        collect_incremental(cfg,sources=['nowcoder'],metrics=metrics)
    s=metrics['sources']['nowcoder']
    assert s['status']=='request_limit_reached'
    assert s['article_failures']==s['discovery_failures']==0
    assert s['failures']=={}
    assert not store.attempted_today(url)
    if not during_discovery:
        assert s['unprocessed_candidates']==1


def test_cli_days_override_also_changes_ranking(cfg,store):
    from dataclasses import asdict
    from pipeline.__main__ import main
    for i in range(3):
        fp=seed(cfg,store,str(i),published=(now().date()-timedelta(days=7)).isoformat())
        store.save_extraction(fp,result(),cfg.dedup)
    for source in cfg.sources.values():
        source.enabled=False
    data=asdict(cfg);data.pop('base_dir')
    path=cfg.base_dir/'config.yaml';path.write_text(json.dumps(data),encoding='utf-8')
    assert main(['run','--config',str(path),'--days','7'])==0
    detail=json.loads(store.conn.execute('SELECT detail FROM runs ORDER BY id DESC').fetchone()[0])
    assert detail['window_days']==detail['ranking_days']==detail['collection']['days']==7
    top=cfg.resolve(cfg.output.output_dir)/'高频题'/'近7天高频题Top50.md'
    assert top.exists() and 'https://www.nowcoder.com/discuss/' not in top.read_text(encoding='utf-8')
    assert detail['rendering']['summary_posts']==3
    assert detail['rendering']['ranking_recent_occurrences']==0


def test_render_statistics_explain_distinct_post_threshold(cfg,store):
    for i in range(3):
        fp=seed(cfg,store,str(i))
        store.save_extraction(fp,result(),cfg.dedup)
    fp=seed(cfg,store,'rare')
    store.save_extraction(fp,result(question='只在一个原帖出现的问题'),cfg.dedup)
    metrics={}
    render_all(cfg,store,metrics)
    assert metrics['summary_posts']==4
    assert metrics['ranking_recent_occurrences']==4
    assert metrics['ranking_unique_questions']==2
    assert metrics['ranking_below_min_posts']==metrics['ranking_rendered']==1
from pipeline.render import render_all, rank_questions
from pipeline.runtime import now
from pipeline.store import Store
from pipeline.quality import filter_question


@pytest.mark.parametrize('position',['测试开发','测开工程师','QA Engineer','SDET','Software Test Engineer'])
def test_excludes_testing_roles_even_with_development_questions(cfg,position):
    from pipeline.extract import sanitize_result
    value=result();value.position=position
    notes=sanitize_result(value,'Redis如何持久化？',cfg)
    assert not value.is_interview_post and not value.rounds
    assert '岗位' in notes[0]


def test_developer_role_keeps_specific_testing_knowledge(cfg):
    from pipeline.extract import sanitize_result
    value=result(question='Java服务如何设计集成测试？')
    sanitize_result(value,'Java服务如何设计集成测试？',cfg)
    assert value.is_interview_post and len(value.rounds[0]['questions'])==1


@pytest.mark.parametrize('question',[
    '简单询问多 Agent 项目的相关情况','期望薪资是多少？','是否需要提前实习？',
    '讲解实习项目','Agent项目的背景是什么？','简历开发中的Agent项目有哪些技术难点？',
    '你是否使用过Go？','你对Redis有了解吗？',
])
def test_reject_vague_or_nontechnical_interview_items(question):
    assert filter_question({'question':question,'follow_ups':[]}) is None


@pytest.mark.parametrize('question',[
    'TCP三次握手','Redis缓存击穿如何处理？','电话号码的字母组合',
    '如果给了一份技术文档，用户需要文档中引用的某个文档的内容，这时候怎么处理？',
    '项目如何防治Prompt注入和越狱攻击？',
])
def test_keep_concrete_technical_questions(question):
    assert filter_question({'question':question,'follow_ups':[]})['question']==question


def test_generic_lead_in_retains_concrete_followup():
    q=filter_question({'question':'你的测试Agent项目有什么亮点或难点？',
                       'follow_ups':['AI生成脚本时UI元素定位不稳定，如何解决？']})
    assert q['question']=='AI生成脚本时UI元素定位不稳定，如何解决？'


def test_revalidation_discards_empty_post_and_removes_active_questions(cfg,store):
    from pipeline.extract import revalidate
    fp=seed(cfg,store,content='简单询问多Agent项目的相关情况')
    value=result(question='简单询问多Agent项目的相关情况')
    value.rounds[0]['questions'][0]['follow_ups']=[]
    store.save_extraction(fp,value,cfg.dedup)
    revalidate(cfg,store)
    assert store.get_post(fp)['status']=='discarded'
    assert store.questions_for_render()==[]
    assert store.status_summary()['questions']==0
    assert store.get_post_content(fp)


def test_summary_archives_old_months_once_and_keeps_dates(cfg,store):
    from pipeline.render import DISCLAIMER
    root=cfg.resolve(cfg.output.output_dir)/'Java后端';root.mkdir(parents=True)
    old=root/'2026-08.md';old.write_text(DISCLAIMER+' old content',encoding='utf-8')
    for i,day in enumerate(['2026-08-01','2026-09-01']):
        fp=seed(cfg,store,str(i),published=day)
        value=result();value.rounds[0]['date']='2026-07-30'
        store.save_extraction(fp,value,cfg.dedup)
    render_all(cfg,store)
    text=(root/'面经汇总.md').read_text(encoding='utf-8')
    assert '2026-08-01' in text and '2026-09-01' in text
    assert '面试日期：2026-07-30' in text
    assert text.count('[原帖](')==2
    assert [p.name for p in root.glob('*.md')]==['面经汇总.md']
    archive=cfg.resolve(cfg.output.db_path).parent/'output-history'
    assert len(list(archive.rglob('2026-08.md')))==1
    render_all(cfg,store)
    assert len(list(archive.rglob('2026-08.md')))==1


def nowcoder_html(record=None, post_id="123", extra="", entry_id=None):
    state = {"prefetchData":{"2":{"contentId":entry_id or post_id,"ssrCommonData":{
        "similarRecommend":[{"contentData":{"id":"999","createTime":1234567890000}}],
        "commentListFirst":{"createTime":1234567890000},
        "contentData":record or {"id":post_id},
    }}}}
    return (f'<html><h1>Java面经</h1><div class="nc-slate-editor-content">'+"面试内容 Redis问题"*10+
            '</div><script>window.__INITIAL_STATE__='+json.dumps(state)+';</script>'+extra+'</html>')


def test_public_html_pagination_follows_next_page_and_has_bound(cfg):
    c = PublicCollector(cfg,"nowcoder")
    def page(url):
        number = 1 if url.endswith('/discuss') else int(url.split('page=')[1].split('&')[0])
        html = f'<a href="/discuss/{number}">技术总结</a><ul class="el-pager">'
        for n in [number,number+1,100]:
            html += f'<li class="{"active" if n==number else "number"}"><a href="/?type=recommend&page={n}">{n}</a></li>'
        return response(html+'</ul>',url=url)
    with patch.object(c.http,"get",side_effect=page) as get:
        assert set(c.discover()) == {f'https://www.nowcoder.com/discuss/{i}' for i in (1,2,3)}
    assert get.call_count == 3
    assert c.discovery_stats['discovery_pages'] == 3
    assert c.discovery_stats['discovery_stop'] == 'page_limit'
    assert 'page=2' in get.call_args_list[1].args[0]
    assert 'page=3' in get.call_args_list[2].args[0]
    c.http.close()


def test_pagination_cycles_and_foreign_hosts_do_not_expand(cfg):
    c=PublicCollector(cfg,'nowcoder')
    html='<div class="pagination"><a href="/discuss">1</a><a href="https://evil.test/?page=2">2</a></div>'
    with patch.object(c.http,'get',return_value=response(html)) as get:
        assert c.discover()==[]
        assert get.call_count==1
    c.http.close()


@pytest.mark.parametrize('recovers',[True,False])
def test_empty_discovery_shell_retries_once_then_stops(cfg,recovers):
    c=PublicCollector(cfg,'nowcoder')
    cfg.sources['nowcoder'].max_discovery_pages=1
    final='<a href="/discuss/123">Java 面经</a>' if recovers else '<h1>app shell</h1>'
    with patch.object(c.http,'get',side_effect=[response('<h1>app shell</h1>'),response(final)]) as get,patch('pipeline.collect.time.sleep') as sleep:
        urls=c.discover()
    assert get.call_count==2 and sleep.call_count==1
    assert bool(urls)==recovers
    assert c.discovery_stats['discovery_retries']==1
    assert c.discovery_stats['empty_discovery_responses']==(1 if recovers else 2)
    c.http.close()


@pytest.mark.parametrize('field',['createdAt','createTime'])
def test_only_matching_original_post_timestamp_is_used(field):
    stamp=int(now().timestamp()*1000)
    html=nowcoder_html({'id':'123',field:stamp,'editTime':1234567890000},extra='<time datetime="2001-01-01"></time>')
    assert parse_article('https://www.nowcoder.com/discuss/123',html,'nowcoder').publish_time[:10]==TODAY


@pytest.mark.parametrize('record,entry',[
    ({'id':'123','editTime':1790000000000},'123'),
    ({'id':'999','createTime':1790000000000},'123'),
    ({'id':'123','createTime':1790000000000},'999'),
])
def test_comments_recommendations_edit_times_and_wrong_post_cannot_supply_date(record,entry):
    html=nowcoder_html(record,entry_id=entry,extra=f'<div class="comment"><time datetime="{TODAY}"></time></div>')
    with pytest.raises(RetryablePage,match='发布日期'):
        parse_article('https://www.nowcoder.com/discuss/123',html,'nowcoder')


def test_feed_uses_created_at_for_matching_uuid():
    stamp=int(now().timestamp()*1000)
    html=nowcoder_html({'id':45,'uuid':'abc123','createdAt':stamp},post_id='abc123')
    assert parse_article('https://www.nowcoder.com/feed/main/detail/abc123',html,'nowcoder').publish_time[:10]==TODAY


def test_temporary_parse_failure_recovers_and_attempts_reconcile(cfg,store):
    url='https://www.nowcoder.com/discuss/123'
    post=RawPost(url,'123','Java面经','',TODAY,now().isoformat(),'正文'*30)
    metrics={}
    with patch.object(PublicCollector,'discover',return_value=[url]),patch.object(PublicCollector,'fetch',side_effect=[RetryablePage('公开正文缺失'),post]) as fetch,patch('pipeline.collect.time.sleep') as sleep:
        assert collect_incremental(cfg,sources=['nowcoder'],metrics=metrics)==1
        assert collect_incremental(cfg,sources=['nowcoder'])==0
    assert fetch.call_count==2
    sleep.assert_called_once_with(30)
    s=metrics['sources']['nowcoder']
    assert s['article_fetch_attempts']==s['collected']+sum(s['skipped'].values())==2
    assert s['retry_pending']==0
    assert store.conn.execute('SELECT attempt_count FROM fetch_attempts WHERE url=?',(url,)).fetchone()[0]==2


def test_daily_retry_cap_survives_new_runs_and_cooldown(cfg,store):
    url='https://www.nowcoder.com/discuss/123'
    store.record_fetch(url,'nowcoder','retryable','公开正文缺失')
    assert store.fetch_block_reason(url)=='retry_cooldown'
    with store.conn:
        store.conn.execute('UPDATE fetch_attempts SET next_retry_at=NULL')
    with patch.object(PublicCollector,'discover',return_value=[]),patch.object(PublicCollector,'fetch',side_effect=RetryablePage('公开正文缺失')) as fetch,patch('pipeline.collect.time.sleep'):
        assert collect_incremental(cfg,sources=['nowcoder'])==0
        assert collect_incremental(cfg,sources=['nowcoder'])==0
    assert fetch.call_count==2  # The persisted first attempt leaves only two today.
    assert store.fetch_block_reason(url)=='retry_limit'
    with patch('pipeline.store.now',return_value=now()+timedelta(days=1)):
        assert not store.attempted_today(url)
        assert store.retry_candidates('nowcoder',3,80)==[url]


def test_permanent_skip_does_not_retry(cfg,store):
    url='https://www.nowcoder.com/discuss/123'
    with patch.object(PublicCollector,'discover',return_value=[url]),patch.object(PublicCollector,'fetch',side_effect=SkipPage('robots.txt 禁止访问')) as fetch:
        collect_incremental(cfg,sources=['nowcoder'])
        collect_incremental(cfg,sources=['nowcoder'])
    assert fetch.call_count==1


def test_article_source_failure_counted_once(cfg,store):
    metrics={}
    with patch.object(PublicCollector,'discover',return_value=['https://www.nowcoder.com/discuss/123']),patch.object(PublicCollector,'fetch',side_effect=SourceUnavailable('HTTP 502 服务器错误，重试耗尽')):
        collect_incremental(cfg,sources=['nowcoder'],metrics=metrics)
    s=metrics['sources']['nowcoder']
    assert sum(s['failures'].values())==s['article_fetch_attempts']==s['article_failures']==1
    assert s['discovery_failures']==0


def test_502_retries_count_real_requests_and_stop(cfg):
    h=PublicHTTP(cfg.collect,['www.nowcoder.com'])
    with patch.object(h.session,'get',return_value=response('',502)),patch('pipeline.http.time.sleep'):
        with pytest.raises(SourceUnavailable):h._send('https://www.nowcoder.com/discuss/123')
    assert h.page_requests==h.requests==4
    assert h.retry_requests==3
    assert h.status_counts=={'502':4}
    h.close()


def test_coarse_reasons_persist_and_no_body_or_key_in_logs(cfg,store,caplog):
    caplog.set_level('INFO')
    a=seed(cfg,store,'1',status='raw',content='本文正文保密标记')
    b=seed(cfg,store,'2',status='raw',content='面试 本文正文保密标记')
    c=seed(cfg,store,'3',status='raw',content='Java 面试 本文正文保密标记')
    with store.conn:
        store.conn.execute("UPDATE posts SET title='普通标题'")
    metrics={}
    assert coarse_filter_pass(cfg,store,metrics)==(1,2)
    assert store.get_post(a)['error']=='未命中面试词'
    assert store.get_post(b)['error']=='未命中岗位词'
    assert store.get_post(c)['status']=='to_extract'
    assert metrics['processed']==metrics['passed']+metrics['filtered']+metrics['missing']==3
    assert '本文正文保密标记' not in caplog.text and cfg.llm.api_key not in caplog.text


def test_publication_date_overrides_conflicting_interview_date_everywhere(cfg,store):
    for i in range(3):
        fp=seed(cfg,store,str(i))
        value=result();value.rounds[0]['date']='2021-01-01'
        store.save_extraction(fp,value,cfg.dedup)
    rows=store.questions_for_render(since=TODAY)
    assert len(rows)==3
    assert len(rank_questions(rows,cfg))==1
    assert {r['event_date'] for r in rows}=={TODAY}
    assert store.conn.execute('SELECT first_seen,last_seen FROM questions').fetchone()[:]==(TODAY,TODAY)
    paths=render_all(cfg,store)
    text=next(p for p in paths if 'Java后端' in str(p)).read_text(encoding='utf-8')
    assert TODAY in text and 'https://www.nowcoder.com/discuss/0' in text
    assert '面试日期：2021-01-01' in text
    assert not rank_questions([dict(r,publish_time='2001-01-01',event_date=TODAY) for r in rows],cfg)
    assert not rank_questions([{k:v for k,v in dict(r).items() if k!='publish_time'} for r in rows],cfg)


def test_v2_migration_backs_up_and_requires_old_nowcoder_date_recheck(cfg):
    path=cfg.resolve(cfg.output.db_path)
    with Store(path) as st:
        fp=seed(cfg,st,'123')
        st.save_extraction(fp,result(),cfg.dedup)
        st.record_fetch('https://www.nowcoder.com/discuss/456','nowcoder','skipped','公开正文缺失')
        st.conn.executescript('ALTER TABLE posts DROP COLUMN publication_verified; ALTER TABLE fetch_attempts DROP COLUMN attempt_count; ALTER TABLE fetch_attempts DROP COLUMN next_retry_at; PRAGMA user_version=2;')
    with Store(path) as st:
        assert st.get_post(fp)['publication_verified']==0
        assert st.questions_for_render()==[]
        assert len(st.retry_candidates('nowcoder',3,80))==2
        assert st.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert len(list(path.parent.glob('*.pre-v3-*.db')))==1


def test_corrected_post_date_does_not_leave_url_pending_forever(cfg,store):
    fp=seed(cfg,store,'123',published='2021-01-01')
    with store.conn:
        store.conn.execute('UPDATE posts SET publication_verified=0 WHERE fingerprint=?',(fp,))
    assert store.status_summary()['publication_unverified']==1
    corrected=seed(cfg,store,'123',published=TODAY)
    store.save_extraction(corrected,result(),cfg.dedup)
    assert store.status_summary()['publication_unverified']==0
    assert len(store.questions_for_render())==1
    assert store.get_post(fp)['publication_verified']==0


def test_model_metadata_json_failure_and_schema_logs_are_safe(cfg,caplog):
    caplog.set_level('INFO')
    e=Extractor(cfg)
    fake=Mock()
    responses=[SimpleNamespace(usage=SimpleNamespace(total_tokens=12),choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='秘密正文'))]),
        SimpleNamespace(usage=SimpleNamespace(total_tokens=12),choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='秘密正文'))]),
        SimpleNamespace(usage=SimpleNamespace(total_tokens=12),choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=payload()))])]
    e.client.close();e.client=fake
    fake.chat.completions.create.side_effect=responses
    assert e._extract_with_retry('完整提示词保密').is_interview_post
    assert fake.chat.completions.create.call_count==3
    assert 'finish_reason' in caplog.text and 'pos=0' in caplog.text
    assert '秘密正文' not in caplog.text and '完整提示词保密' not in caplog.text and cfg.llm.api_key not in caplog.text
    bad=json.loads(payload());bad['company']=['秘密正文']
    with patch.object(e,'_chat',return_value=(json.dumps(bad),1)):
        with pytest.raises(ExtractFailed) as ex:e._extract_with_retry('prompt')
    assert '秘密正文' not in str(ex.value) and '秘密正文' not in caplog.text
    e.close()
