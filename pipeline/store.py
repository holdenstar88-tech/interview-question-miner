"""SQLite versioned posts, canonical questions and per-source occurrences."""
from __future__ import annotations
import hashlib
import json
import logging
import sqlite3
from datetime import timedelta
from dataclasses import asdict
from pathlib import Path
from .config import DedupConfig
from .dedup import is_duplicate, normalize_question, question_hash
from .models import ExtractionResult
from .runtime import now, iso_date

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
 fingerprint TEXT PRIMARY KEY, url TEXT NOT NULL, post_id TEXT NOT NULL,
 title TEXT, author TEXT, publish_time TEXT NOT NULL, crawl_time TEXT NOT NULL,
 source TEXT NOT NULL, raw_path TEXT, status TEXT NOT NULL DEFAULT 'raw',
 company TEXT, position TEXT, position_category TEXT, summary TEXT,
 confidence REAL, extract_time TEXT, error TEXT, extraction_json TEXT,
 publication_verified INTEGER NOT NULL DEFAULT 1,
 UNIQUE(url, publish_time)
);
CREATE TABLE IF NOT EXISTS questions (
 hash TEXT PRIMARY KEY, question TEXT NOT NULL,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, times_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS occurrences (
 id INTEGER PRIMARY KEY, question_hash TEXT NOT NULL REFERENCES questions(hash),
 post_fingerprint TEXT NOT NULL REFERENCES posts(fingerprint),
 company TEXT, position TEXT, position_category TEXT, round_name TEXT NOT NULL,
 round_date TEXT NOT NULL DEFAULT '', question TEXT NOT NULL, q_type TEXT,
 follow_ups TEXT NOT NULL DEFAULT '[]', original_text TEXT, event_date TEXT NOT NULL,
 UNIQUE(question_hash, post_fingerprint, round_name, round_date)
);
CREATE TABLE IF NOT EXISTS fetch_attempts (
 url TEXT PRIMARY KEY, source TEXT NOT NULL, attempted_at TEXT NOT NULL,
 outcome TEXT NOT NULL, reason TEXT, attempt_count INTEGER NOT NULL DEFAULT 1,
 next_retry_at TEXT
);
CREATE TABLE IF NOT EXISTS candidate_queue (
 url TEXT PRIMARY KEY, source TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 priority INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS candidate_queue_source_status ON candidate_queue(source,status,first_seen_at);
CREATE TABLE IF NOT EXISTS discovery_pages (
 url TEXT PRIMARY KEY, source TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS discovery_pages_source_status ON discovery_pages(source,status,first_seen_at);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 command TEXT NOT NULL, status TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS v2_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS v2_posts_url ON posts(url);
CREATE INDEX IF NOT EXISTS v2_occurrences_date ON occurrences(event_date);
CREATE INDEX IF NOT EXISTS v2_occurrences_post ON occurrences(post_fingerprint);
"""

def fingerprint(url: str, publish_time: str) -> str:
    return hashlib.sha256(f"{url}\n{publish_time}".encode()).hexdigest()

class Store:
    """A whole post commits atomically; migration first backs up SQLite."""
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._raw_root: Path | None = None
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if columns and "fingerprint" not in columns:
            self._migrate_legacy()
        else:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if columns and version < 4:
                label = "pre-v3" if version < 3 else "pre-v4"
                backup = self.path.with_name(f"{self.path.stem}.{label}-{now().strftime('%Y%m%d-%H%M%S')}.db")
                with sqlite3.connect(str(backup)) as dest:
                    self.conn.backup(dest)
            self.conn.executescript(SCHEMA)
            if version < 3:
                self.conn.execute("BEGIN IMMEDIATE")
                self._upgrade_fetch_attempts()
            if version < 4:
                self._backfill_candidate_queue()
            self.conn.execute("PRAGMA user_version=4")
            self.conn.commit()

    def _backfill_candidate_queue(self) -> None:
        self.conn.execute("""INSERT OR IGNORE INTO candidate_queue(url,source,first_seen_at,last_seen_at,status)
            SELECT url,source,attempted_at,attempted_at,
              CASE WHEN outcome IN ('discovered','retryable','failed','source_unavailable')
                   THEN 'pending' ELSE 'done' END
            FROM fetch_attempts WHERE source='nowcoder' AND url LIKE 'https://%'""")
        self.conn.execute("""UPDATE candidate_queue SET status='pending' WHERE source='nowcoder'
            AND url IN (SELECT url FROM fetch_attempts WHERE source='nowcoder'
                AND outcome IN ('discovered','retryable','failed','source_unavailable'))""")

    def _upgrade_fetch_attempts(self) -> None:
        post_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "publication_verified" not in post_columns:
            self.conn.execute("ALTER TABLE posts ADD COLUMN publication_verified INTEGER NOT NULL DEFAULT 1")
            self.conn.execute("UPDATE posts SET publication_verified=0 WHERE source='nowcoder'")
            # Old Nowcoder dates may have come from comments/recommendations.
            # Retain raw/extractions, but re-check publication before using them.
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(fetch_attempts)")}
        if "attempt_count" not in columns:
            self.conn.execute("ALTER TABLE fetch_attempts ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1")
        if "next_retry_at" not in columns:
            self.conn.execute("ALTER TABLE fetch_attempts ADD COLUMN next_retry_at TEXT")
        # Existing temporary skips must become eligible again without clearing history.
        self.conn.execute("""UPDATE fetch_attempts SET outcome='retryable' WHERE outcome='skipped' AND
            (reason LIKE '公开正文缺失%' OR reason LIKE '正文过短%' OR reason LIKE '缺少可靠发布日期%')""")
        self.conn.execute("""INSERT INTO fetch_attempts(url,source,attempted_at,outcome,reason,attempt_count)
            SELECT url,source,crawl_time,'retryable','publication_recheck',0 FROM posts WHERE publication_verified=0 GROUP BY url
            ON CONFLICT(url) DO UPDATE SET outcome='retryable',reason='publication_recheck',attempt_count=0,next_retry_at=NULL""")
        self.conn.execute("""UPDATE occurrences SET event_date=(SELECT substr(p.publish_time,1,10)
            FROM posts p WHERE p.fingerprint=post_fingerprint)""")
        self._refresh_question_dates()

    def _refresh_question_dates(self):
        self.conn.execute("""UPDATE questions SET
            first_seen=(SELECT min(substr(p.publish_time,1,10)) FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint WHERE o.question_hash=questions.hash),
            last_seen=(SELECT max(substr(p.publish_time,1,10)) FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint WHERE o.question_hash=questions.hash)
            WHERE EXISTS(SELECT 1 FROM occurrences WHERE question_hash=questions.hash)""")

    def _migrate_legacy(self) -> None:
        backup = self.path.with_name(f"{self.path.stem}.pre-v2-{now().strftime('%Y%m%d-%H%M%S')}.db")
        dest = sqlite3.connect(str(backup))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        posts = [dict(r) for r in self.conn.execute("SELECT * FROM posts")]
        try:
            self.conn.executescript("BEGIN IMMEDIATE; ALTER TABLE questions RENAME TO legacy_questions; ALTER TABLE posts RENAME TO legacy_posts;" + SCHEMA)
            for p in posts:
                fp = fingerprint(p["url"], p["publish_time"] or "")
                status = "to_extract" if p["status"] in ("extracted","pending_review") else p["status"]
                self.conn.execute("""INSERT INTO posts(fingerprint,url,post_id,title,author,publish_time,crawl_time,source,status,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (fp,p["url"],p["post_id"],p["title"],p["author"],p["publish_time"] or "",p["crawl_time"],p["source"],status,"legacy_requires_reextraction"))
            self.conn.execute("UPDATE posts SET publication_verified=0 WHERE source='nowcoder'")
            self._upgrade_fetch_attempts()
            self._backfill_candidate_queue()
            self.conn.execute("PRAGMA user_version=4")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        logger.warning("旧库备份: %s；旧题保存在 legacy_questions，%d 帖迁移后待重新抽取/复核", backup, len(posts))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def has_post(self, url: str, publish_time: str | None = None) -> bool:
        if publish_time is None:
            return self.conn.execute("SELECT 1 FROM posts WHERE url=?", (url,)).fetchone() is not None
        return self.conn.execute("SELECT 1 FROM posts WHERE fingerprint=?", (fingerprint(url,publish_time),)).fetchone() is not None

    def needs_publication_recheck(self, url: str) -> bool:
        return self.conn.execute("""SELECT 1 FROM posts p WHERE p.url=? AND p.publication_verified=0
            AND NOT EXISTS(SELECT 1 FROM posts verified WHERE verified.url=p.url
                AND verified.publication_verified=1) LIMIT 1""", (url,)).fetchone() is not None

    def upsert_post(self, post, status: str = "raw", raw_path: Path | None = None, publication_verified: bool = True) -> str:
        fp = fingerprint(post.url, post.publish_time or "")
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO posts(fingerprint,url,post_id,title,author,publish_time,crawl_time,source,status,raw_path,publication_verified)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (fp,post.url,post.post_id,post.title,post.author,post.publish_time or "",post.crawl_time,post.source,status,str(raw_path) if raw_path else None,int(publication_verified)))
            if publication_verified:
                self.conn.execute("UPDATE posts SET publication_verified=1 WHERE fingerprint=?",(fp,))
        return fp

    def set_status(self, fp: str, status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE posts SET status=?,error=? WHERE fingerprint=?", (status,error,fp))

    def posts_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        if not statuses:
            return []
        return self.conn.execute(f"SELECT * FROM posts WHERE publication_verified=1 AND status IN ({','.join('?' for _ in statuses)}) ORDER BY publish_time DESC",statuses).fetchall()

    def get_post(self, fp: str):
        return self.conn.execute("SELECT * FROM posts WHERE fingerprint=?", (fp,)).fetchone()

    def bind_raw_root(self, raw_dir: Path) -> None:
        self._raw_root = raw_dir

    def get_post_content(self, fp: str) -> str:
        row = self.get_post(fp)
        if row is None or self._raw_root is None:
            return ""
        candidates = [Path(row["raw_path"])] if row["raw_path"] else []
        candidates.extend(sorted(self._raw_root.glob(f"{row['source']}/*/{row['post_id']}*.json")))
        for candidate in candidates:
            if not candidate.resolve().is_relative_to(self._raw_root.resolve()):
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if data.get("url") == row["url"] and (data.get("publish_time") or "") == row["publish_time"]:
                    return data["content_raw"]
            except (OSError, ValueError, KeyError):
                continue
        return ""

    def collected_today(self, source: str | None = None) -> int:
        sql = "SELECT count(*) FROM posts WHERE substr(crawl_time,1,10)=?"
        params = [now().date().isoformat()]
        if source:
            sql += " AND source=?"
            params.append(source)
        return self.conn.execute(sql, params).fetchone()[0]

    def fetch_block_reason(self, url: str, max_attempts: int = 3) -> str | None:
        row = self.conn.execute("SELECT * FROM fetch_attempts WHERE url=?",(url,)).fetchone()
        if row is None or row["attempted_at"][:10] != now().date().isoformat():
            return None
        if row["outcome"] == "discovered":
            return None
        if row["outcome"] != "retryable":
            return "already_attempted"
        if row["attempt_count"] >= max_attempts:
            return "retry_limit"
        if row["next_retry_at"] and row["next_retry_at"] > now().isoformat():
            return "retry_cooldown"
        return None

    def attempted_today(self, url: str, max_attempts: int = 3) -> bool:
        return self.fetch_block_reason(url,max_attempts) is not None

    def attempts_today(self, url: str) -> int:
        row = self.conn.execute("SELECT attempt_count FROM fetch_attempts WHERE url=? AND outcome!='discovered' AND substr(attempted_at,1,10)=?",(url,now().date().isoformat())).fetchone()
        return row[0] if row else 0

    def remember_candidates(self, source: str, urls: list[str]) -> None:
        current = now().isoformat()
        with self.conn:
            if source == "nowcoder":
                self.conn.executemany("""INSERT INTO candidate_queue(url,source,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                    ((url,source,current,current) for url in urls))
            else:
                self.conn.executemany("""INSERT OR IGNORE INTO fetch_attempts
                    (url,source,attempted_at,outcome,reason,attempt_count,next_retry_at)
                    VALUES(?, ?, ?, 'discovered', '', 0, NULL)""",
                    ((url,source,current) for url in urls))

    def pending_discovery_pages(self, source: str) -> list[str]:
        return [row[0] for row in self.conn.execute("""SELECT url FROM discovery_pages
            WHERE source=? AND status='pending' ORDER BY first_seen_at,url""",(source,))]

    def save_discovery_page(self, source: str, url: str, candidates: list[tuple[str,int]],
                            next_pages: list[str], keep_pending_on_empty: bool = True) -> None:
        current = now().isoformat()
        with self.conn:
            self.conn.executemany("""INSERT INTO candidate_queue(url,source,first_seen_at,last_seen_at,priority)
                VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,priority=max(priority,excluded.priority)""",
                ((candidate,source,current,current,priority) for candidate,priority in candidates))
            self.conn.executemany("""INSERT INTO discovery_pages(url,source,first_seen_at,last_seen_at)
                VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,status='pending'""",
                ((page,source,current,current) for page in dict.fromkeys(next_pages)))
            status = "pending" if keep_pending_on_empty and not candidates and not next_pages else "visited"
            self.conn.execute("""INSERT INTO discovery_pages(url,source,first_seen_at,last_seen_at,status)
                VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,status=excluded.status""",(url,source,current,current,status))

    def candidate_queue_summary(self, source: str, max_attempts: int) -> dict[str,int]:
        current = now().isoformat()
        today = current[:10]
        counts = {"pending":0,"ready":0,"retry_limit_today":0,"retry_cooldown":0,
                  "deferred_today":0,"done":0}
        for row in self.conn.execute("""SELECT q.status,a.outcome,a.attempted_at,a.attempt_count,a.next_retry_at
            FROM candidate_queue q LEFT JOIN fetch_attempts a ON a.url=q.url WHERE q.source=?""",(source,)):
            status = row["status"]
            counts[status] += 1
            if status != "pending":
                continue
            if row["outcome"] == "retryable" and row["attempted_at"][:10] == today:
                if row["attempt_count"] >= max_attempts:
                    counts["retry_limit_today"] += 1
                elif row["next_retry_at"] and row["next_retry_at"] > current:
                    counts["retry_cooldown"] += 1
                else:
                    counts["ready"] += 1
            elif row["outcome"] in ("failed","source_unavailable") and row["attempted_at"][:10] == today:
                counts["deferred_today"] += 1
            else:
                counts["ready"] += 1
        counts["discovery_pages_pending"] = self.conn.execute("""SELECT count(*) FROM discovery_pages
            WHERE source=? AND status='pending'""",(source,)).fetchone()[0]
        return counts

    def has_unattempted_candidates(self, source: str) -> bool:
        return self.conn.execute("""SELECT 1 FROM candidate_queue q
            LEFT JOIN fetch_attempts a ON a.url=q.url WHERE q.source=? AND q.status='pending'
            AND (a.url IS NULL OR a.outcome='discovered') LIMIT 1""", (source,)).fetchone() is not None

    def retry_candidates(self, source: str, max_attempts: int, limit: int) -> list[str]:
        if source == "nowcoder":
            return [r[0] for r in self.conn.execute("""SELECT q.url FROM candidate_queue q
                LEFT JOIN fetch_attempts a ON a.url=q.url WHERE q.source=? AND q.status='pending'
                AND (a.url IS NULL OR a.outcome='discovered' OR
                  (a.outcome IN ('failed','source_unavailable') AND substr(a.attempted_at,1,10)<>?) OR
                  (a.outcome='retryable' AND (substr(a.attempted_at,1,10)<>? OR
                    (a.attempt_count<? AND (a.next_retry_at IS NULL OR a.next_retry_at<=?)))))
                ORDER BY CASE WHEN a.url IS NULL OR a.outcome='discovered' THEN 0 ELSE 1 END,
                    COALESCE(a.attempted_at,q.first_seen_at),q.priority DESC,q.url LIMIT ?""",
                (source,now().date().isoformat(),now().date().isoformat(),max_attempts,now().isoformat(),limit))]
        return [r[0] for r in self.conn.execute("""SELECT url FROM fetch_attempts WHERE source=? AND
            (outcome='discovered' OR (outcome='retryable' AND
            (substr(attempted_at,1,10)<>? OR (attempt_count<? AND (next_retry_at IS NULL OR next_retry_at<=?)))))
            ORDER BY attempted_at LIMIT ?""",(source,now().date().isoformat(),max_attempts,now().isoformat(),limit))]

    def record_fetch(self, url: str, source: str, outcome: str, reason: str = "", cooldown: int = 30) -> None:
        with self.conn:
            current = now().isoformat()
            old = self.conn.execute("SELECT attempted_at,attempt_count FROM fetch_attempts WHERE url=?",(url,)).fetchone()
            attempt_count = old["attempt_count"] + 1 if old and old["attempted_at"][:10] == current[:10] else 1
            retry_at = (now()+timedelta(seconds=cooldown)).isoformat() if outcome == "retryable" else None
            self.conn.execute("""INSERT INTO fetch_attempts(url,source,attempted_at,outcome,reason,attempt_count,next_retry_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET source=excluded.source,attempted_at=excluded.attempted_at,
                outcome=excluded.outcome,reason=excluded.reason,attempt_count=excluded.attempt_count,next_retry_at=excluded.next_retry_at""",
                (url,source,current,outcome,reason,attempt_count,retry_at))
            if source == "nowcoder" and "://" in url:
                status = "pending" if outcome in ("retryable","failed","source_unavailable") else "done"
                self.conn.execute("""INSERT INTO candidate_queue(url,source,first_seen_at,last_seen_at,status)
                    VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET status=excluded.status""",
                    (url,source,current,current,status))

    def all_recent_hashes(self, days: int | None = None):
        """All-time dedup; retention windows must not create duplicates."""
        return [(r[0],r[1]) for r in self.conn.execute("SELECT hash,question FROM questions")]

    def save_extraction(self, fp: str, result: ExtractionResult, dedup: DedupConfig) -> int:
        row = self.get_post(fp)
        if row is None:
            raise ValueError("未知帖子")
        if not row["publication_verified"]:
            raise ValueError("原帖发帖时间待复查，暂不进入抽取结果")
        pub_date = iso_date(row["publish_time"])
        if not pub_date:
            raise ValueError("缺少可靠发布日期，不能进入时效统计")
        pairs = self.all_recent_hashes()
        count = 0
        with self.conn:
            self.conn.execute("DELETE FROM occurrences WHERE post_fingerprint=?",(fp,))
            for rnd in result.rounds:
                for q in rnd["questions"]:
                    norm = normalize_question(q["question"],dedup.synonyms)
                    h = is_duplicate(norm,pairs,dedup.threshold,dedup.short_len,dedup.synonyms)
                    if h is None:
                        h = question_hash(q["question"],dedup.synonyms)
                        self.conn.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,0)",(h,q["question"],pub_date,pub_date))
                        pairs.append((h,q["question"]))
                    rd = rnd.get("date") or ""
                    event = pub_date  # Compatibility column: all frequency dates use publication.
                    old = self.conn.execute("SELECT id,follow_ups,original_text FROM occurrences WHERE question_hash=? AND post_fingerprint=? AND round_name=? AND round_date=?",(h,fp,rnd["round_name"],rd)).fetchone()
                    if old:
                        follow = list(dict.fromkeys(json.loads(old["follow_ups"]) + q.get("follow_ups",[])))
                        evidence = old["original_text"] or q.get("original_text", "")
                        self.conn.execute("UPDATE occurrences SET follow_ups=?,original_text=? WHERE id=?",(json.dumps(follow,ensure_ascii=False),evidence,old["id"]))
                    else:
                        self.conn.execute("""INSERT INTO occurrences(question_hash,post_fingerprint,company,position,position_category,round_name,round_date,question,q_type,follow_ups,original_text,event_date)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(h,fp,result.company,result.position,result.position_category,rnd["round_name"],rd,q["question"],q["type"],json.dumps(q.get("follow_ups",[]),ensure_ascii=False),q.get("original_text",""),event))
                        count += 1
            self.conn.execute("UPDATE posts SET status='extracted',company=?,position=?,position_category=?,summary=?,confidence=?,extract_time=?,error=NULL,extraction_json=? WHERE fingerprint=?",(result.company,result.position,result.position_category,result.summary,result.confidence,now().isoformat(),json.dumps(asdict(result),ensure_ascii=False),fp))
            self.conn.execute("DELETE FROM questions WHERE hash NOT IN (SELECT question_hash FROM occurrences)")
            self.conn.execute("""UPDATE questions SET times_seen=(SELECT count(DISTINCT p.url) FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint WHERE o.question_hash=questions.hash),
                first_seen=(SELECT min(substr(p.publish_time,1,10)) FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint WHERE o.question_hash=questions.hash),
                last_seen=(SELECT max(substr(p.publish_time,1,10)) FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint WHERE o.question_hash=questions.hash)""")
        return count

    def questions_for_render(self, category: str | None = None, since: str | None = None):
        sql = """SELECT o.*,o.question_hash AS hash,p.url AS source_url,p.title AS post_title,
            p.publish_time,p.fingerprint FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint
            WHERE p.status='extracted' AND p.publication_verified=1 AND p.publish_time=(SELECT max(p2.publish_time) FROM posts p2 WHERE p2.url=p.url AND p2.status='extracted' AND p2.publication_verified=1)
            AND substr(p.publish_time,1,10)<=?"""
        params = [now().date().isoformat()]
        if category:
            sql += " AND o.position_category=?"
            params.append(category)
        if since:
            sql += " AND substr(p.publish_time,1,10)>=? AND substr(p.publish_time,1,10)<=?"
            params.append(since)
            params.append(now().date().isoformat())
        return self.conn.execute(sql+" ORDER BY o.company,o.round_name,o.id",params).fetchall()

    def status_summary(self) -> dict:
        return {
            "posts": {r[0]:r[1] for r in self.conn.execute("SELECT status,count(*) FROM posts GROUP BY status")},
            "sources": {r[0]:r[1] for r in self.conn.execute("SELECT source,count(*) FROM posts GROUP BY source")},
            "questions": self.conn.execute("SELECT count(*) FROM questions").fetchone()[0],
            "occurrences": self.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0],
            "publication_unverified": self.conn.execute("""SELECT count(DISTINCT p.url) FROM posts p WHERE p.publication_verified=0
                AND NOT EXISTS(SELECT 1 FROM posts verified WHERE verified.url=p.url AND verified.publication_verified=1)""").fetchone()[0],
            "fetch_attempts": {r[0]:r[1] for r in self.conn.execute("SELECT outcome,count(*) FROM fetch_attempts GROUP BY outcome")},
            "filtered_reasons": {r[0] or "未注明":r[1] for r in self.conn.execute("SELECT error,count(*) FROM posts WHERE status='filtered' GROUP BY error")},
        }
