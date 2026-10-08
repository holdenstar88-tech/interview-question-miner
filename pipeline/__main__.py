"""Single argument parser for all pipeline entry points."""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path
from .config import load_config, setup_logging
from .runtime import now, pipeline_lock

logger = logging.getLogger(__name__)

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="replace")
    parser = argparse.ArgumentParser(prog="pipeline",description="公开面经采集与 DeepSeek 结构化整理")
    parser.add_argument("command",choices=["run","collect","extract","render","status","retry-failed","review","recover","revalidate"])
    parser.add_argument("--config",default=None)
    parser.add_argument("--days",type=int,default=None,help="采集和高频榜近 N 个自然日（含今天）")
    parser.add_argument("--sources",nargs="+",choices=["nowcoder","csdn","juejin","github"])
    parser.add_argument("--limit",type=int,help="本次最多处理的待抽取帖数")
    parser.add_argument("--review-file",type=Path,help="人工编辑好的 pending JSON")
    parser.add_argument("--verbose",action="store_true")
    args = parser.parse_args(argv)
    if args.days is not None and args.days<=0 or args.limit is not None and args.limit<=0:
        parser.error("--days / --limit 必须为正数")
    if args.command=="review" and args.review_file is None:
        parser.error("review 需要 --review-file")
    try:
        cfg = load_config(args.config)
        if args.days is not None:
            cfg.collect.days = cfg.output.recency_days = args.days
        setup_logging(cfg.resolve(cfg.output.log_dir),args.verbose,cfg.output.log_max_bytes,cfg.output.log_backups)
        from .collect import collect_incremental, recover_raw
        from .extract import coarse_filter_pass, extract_pass, apply_review, revalidate
        from .render import render_all
        from .store import Store
        with pipeline_lock(cfg.resolve(cfg.output.db_path).parent/"pipeline.lock"):
            with Store(cfg.resolve(cfg.output.db_path)) as store:
                store.bind_raw_root(cfg.resolve(cfg.output.raw_dir))
                with store.conn:
                    # A stale 'running' record is evidence of interruption, not success.
                    store.conn.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'",(now().isoformat(),))
                    run_id = store.conn.execute("INSERT INTO runs(started_at,command,status) VALUES(?,?,'running')",(now().isoformat(),args.command)).lastrowid
                exit_code = 0
                run_started = now()
                run_metrics = {"command":args.command,"started_at":run_started.isoformat(timespec="seconds"),
                               "config_path":str(Path(args.config).resolve()) if args.config else str(cfg.base_dir/"config.yaml"),
                               "window_days":cfg.collect.days,"ranking_days":cfg.output.recency_days}
                try:
                    if args.command in ("run","collect"):
                        collect_incremental(cfg,args.days,args.sources,run_metrics.setdefault("collection",{}))
                    if args.command=="recover":
                        logger.info("从完整 raw 恢复 %d 帖",recover_raw(cfg,store))
                    if args.command=="retry-failed":
                        for row in store.posts_by_status("extract_failed"):
                            store.set_status(row["fingerprint"],"to_extract")
                    if args.command in ("run","extract","retry-failed"):
                        coarse_filter_pass(cfg,store,run_metrics.setdefault("coarse_filter",{}))
                        extraction_metrics = {}
                        extract_pass(cfg,store,args.limit,extraction_metrics)
                        run_metrics["extraction"] = extraction_metrics
                        if store.posts_by_status("extract_failed"):
                            exit_code = 1
                    if args.command=="review":
                        apply_review(cfg,store,args.review_file.resolve())
                    if args.command=="revalidate":
                        logger.info("重新检查 %d 篇已存抽取结果（不调用 LLM）",revalidate(cfg,store))
                    if args.command in ("run","render","review","revalidate"):
                        output_paths = render_all(cfg,store,run_metrics.setdefault("rendering",{}))
                        run_metrics["outputs"] = [str(path) for path in output_paths]
                        for path in output_paths:
                            logger.info("已生成: %s",path)
                    summary = store.status_summary()
                    if args.command in ("run","collect"):
                        blocked = store.conn.execute("SELECT DISTINCT source FROM fetch_attempts WHERE outcome IN ('source_unavailable','failed') AND attempted_at >= (SELECT started_at FROM runs WHERE id=?)",(run_id,)).fetchall()
                        if blocked:
                            logger.warning("以下来源本次未完成：%s",", ".join(r[0] for r in blocked))
                            exit_code = max(exit_code,2)
                        if any(s.get("status") in ("partial","empty_discovery","failed","source_unavailable","request_limit_reached","daily_limit_reached","source_limit_reached","collection_limit_reached") for s in run_metrics.get("collection",{}).get("sources",{}).values()):
                            exit_code = max(exit_code,2)
                    if args.command=="status":
                        print(json.dumps(summary,ensure_ascii=False,indent=2))
                    unfinished = bool(store.posts_by_status("to_extract"))
                    if summary["publication_unverified"] and args.command in ("run","extract","retry-failed","render","revalidate"):
                        logger.warning("有 %d 条历史记录的原帖日期尚未验证，暂不用于输出",summary["publication_unverified"])
                        exit_code = max(exit_code,2)
                    if unfinished and args.command in ("run","extract","retry-failed"):
                        exit_code = max(exit_code,2)
                    run_metrics["database_status"] = summary
                    run_metrics["exit_code"] = exit_code
                    run_metrics["finished_at"] = now().isoformat(timespec="seconds")
                    run_metrics["duration_seconds"] = round((now()-run_started).total_seconds(),2)
                    logger.info("运行统计：%s",json.dumps(run_metrics,ensure_ascii=False,sort_keys=True))
                    if summary["posts"].get("pending_review"):
                        logger.warning("有 %d 帖待人工复核：%s",summary["posts"]["pending_review"],cfg.resolve(cfg.output.pending_dir))
                    run_status = "partial" if exit_code or (unfinished and args.command in ("run","extract","retry-failed")) else "completed"
                    with store.conn:
                        store.conn.execute("UPDATE runs SET finished_at=?,status=?,detail=? WHERE id=?",(now().isoformat(),run_status,json.dumps(run_metrics,ensure_ascii=False),run_id))
                except BaseException as exc:
                    run_metrics["finished_at"] = now().isoformat(timespec="seconds")
                    run_metrics["duration_seconds"] = round((now()-run_started).total_seconds(),2)
                    run_metrics["exit_code"] = 1
                    run_metrics["failure"] = type(exc).__name__
                    with store.conn:
                        store.conn.execute("UPDATE runs SET finished_at=?,status='failed',detail=? WHERE id=?",(now().isoformat(),json.dumps(run_metrics,ensure_ascii=False),run_id))
                    raise
                return exit_code
    except Exception as exc:
        # Do not dump HTTP bodies or SDK exceptions that might include credentials.
        logger.error("流水线失败：%s",str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
