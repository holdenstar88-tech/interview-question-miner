"""Regressions for daily queue progress and publication rechecks of history."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from test_pipeline import cfg, store, seed, result
from pipeline.collect import PublicCollector, RawPost, collect_incremental
from pipeline.http import RetryablePage
from pipeline.runtime import now
from pipeline.store import Store
from pipeline.render import render_all


def test_daily_retry_backlog_must_not_stop_discovery_forever(cfg, store):
    cfg.collect.page_attempts_per_day = 1
    url = 'https://www.nowcoder.com/discuss/123'
    store.remember_candidates('nowcoder', [url])
    start = now()
    with patch.object(PublicCollector, 'discover', return_value=[]) as discover, \
         patch.object(PublicCollector, 'fetch', side_effect=RetryablePage('missing publication date')) as fetch:
        for offset in range(4):
            day = start + timedelta(days=offset)
            with patch('pipeline.store.now', return_value=day), patch('pipeline.collect.now', return_value=day):
                collect_incremental(cfg, sources=['nowcoder'])
        assert fetch.call_count == 4
        assert discover.call_count > 0, 'One persistent parse failure suppressed all discovery for four daily runs'


@pytest.mark.parametrize('corrected', [False, True])
def test_migrated_historical_post_can_finish_publication_recheck(cfg, corrected):
    day = (now().date() - timedelta(days=90)).isoformat()
    path = cfg.resolve(cfg.output.db_path)
    url = 'https://www.nowcoder.com/discuss/123'
    with Store(path) as st:
        fp = seed(cfg, st, '123', published=day)
        st.save_extraction(fp, result(), cfg.dedup)
        st.conn.executescript('ALTER TABLE posts DROP COLUMN publication_verified; '
                             'ALTER TABLE fetch_attempts DROP COLUMN attempt_count; '
                             'ALTER TABLE fetch_attempts DROP COLUMN next_retry_at; PRAGMA user_version=2;')
    with Store(path) as st:
        assert st.get_post(fp)['publication_verified'] == 0
    verified_day = (now().date() - timedelta(days=91)).isoformat() if corrected else day
    post = RawPost(url, '123', 'Java面经', '', verified_day, now().isoformat(), 'Redis如何持久化？' * 10)
    with patch.object(PublicCollector, 'discover', return_value=[]), \
         patch.object(PublicCollector, 'fetch', return_value=post):
        collect_incremental(cfg, sources=['nowcoder'])
    with Store(path) as st:
        st.bind_raw_root(cfg.resolve(cfg.output.raw_dir))
        assert st.candidate_queue_summary('nowcoder', 1)['pending'] == 0
        assert st.status_summary()['publication_unverified'] == 0
        if corrected:
            assert st.get_post(fp)['publication_verified'] == 0
            # Preserve the original snapshot and extraction while the corrected
            # snapshot follows the normal extraction path.
            assert st.get_post(fp)['extraction_json']
            pending = st.posts_by_status('raw')
            assert len(pending) == 1 and pending[0]['publish_time'] == verified_day
            assert st.get_post_content(pending[0]['fingerprint']) == post.content_raw
            st.save_extraction(pending[0]['fingerprint'], result(), cfg.dedup)
        else:
            assert st.get_post(fp)['publication_verified'] == 1
        assert len(st.questions_for_render()) == 1
        summary = next(p for p in render_all(cfg, st) if 'Java后端' in str(p))
        assert url in summary.read_text(encoding='utf-8')
        assert verified_day in summary.read_text(encoding='utf-8')


def test_small_batch_can_collect_new_post_despite_large_retry_backlog(cfg, store):
    cfg.collect.page_attempts_per_day = 1
    cfg.collect.max_candidates_per_source = 1
    start = now()
    stale = [f'https://www.nowcoder.com/discuss/{i}' for i in range(3)]
    with patch('pipeline.store.now', return_value=start - timedelta(days=1)):
        for url in stale:
            store.record_fetch(url, 'nowcoder', 'retryable', 'missing date')
    fresh = 'https://www.nowcoder.com/discuss/999'
    post = RawPost(fresh, '999', 'Java面经', '', start.date().isoformat(), start.isoformat(), 'Redis如何持久化？' * 10)
    with patch.object(PublicCollector, 'discover', return_value=[fresh]) as discover, \
         patch.object(PublicCollector, 'fetch', return_value=post) as fetch:
        assert collect_incremental(cfg, sources=['nowcoder']) == 1
    discover.assert_called_once()
    fetch.assert_called_once_with(fresh)
    assert store.candidate_queue_summary('nowcoder', 1)['pending'] == len(stale)


def test_failed_retry_moves_behind_other_eligible_retries(cfg, store):
    start = now()
    urls = [f'https://www.nowcoder.com/discuss/{i}' for i in range(3)]
    with patch('pipeline.store.now', return_value=start - timedelta(days=1)):
        for url in urls:
            store.record_fetch(url, 'nowcoder', 'retryable', 'missing date')
    assert store.retry_candidates('nowcoder', 1, 1) == urls[:1]
    store.record_fetch(urls[0], 'nowcoder', 'retryable', 'missing date')
    with patch('pipeline.store.now', return_value=start + timedelta(days=1)):
        assert store.retry_candidates('nowcoder', 1, 3) == urls[1:] + urls[:1]


@pytest.mark.parametrize('historical,offset', [(False, -90), (False, 1), (True, 1)])
def test_history_recheck_does_not_admit_new_old_posts_or_future_dates(cfg, store, historical, offset):
    url = 'https://www.nowcoder.com/discuss/123'
    if historical:
        fp = seed(cfg, store, '123', published=(now().date() - timedelta(days=90)).isoformat())
        with store.conn:
            store.conn.execute('UPDATE posts SET publication_verified=0 WHERE fingerprint=?', (fp,))
    store.remember_candidates('nowcoder', [url])
    day = (now().date() + timedelta(days=offset)).isoformat()
    post = RawPost(url, '123', 'Java面经', '', day, now().isoformat(), 'Redis如何持久化？' * 10)
    with patch.object(PublicCollector, 'fetch', return_value=post):
        assert collect_incremental(cfg, sources=['nowcoder']) == 0
    assert not store.has_post(url, day)
    assert store.questions_for_render() == []
    assert store.conn.execute('SELECT reason FROM fetch_attempts WHERE url=?', (url,)).fetchone()[0] == 'outside_time_window'
