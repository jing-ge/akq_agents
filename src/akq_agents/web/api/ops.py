"""Ops endpoints：/api/ops/health|job-runs|events。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Query

from akq_agents.web.deps import ServiceContainer, get_services

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    """聚合：DataHealth + DaemonState + today_batch + scheduler_events_24h_by_level。"""
    svc: ServiceContainer = get_services()

    # 修复: 整个 health 聚合会做多次同步阻塞 IO (quality_report 查 parquet/meta.db、
    # daemon_state_file.read() 读文件、get_job_run / events_count 查 SQLite)。放在 async
    # endpoint 里直接跑会阻塞 event loop, 用 asyncio.to_thread 挪到线程池。
    def _compute() -> dict[str, Any]:
        data_health = None
        if svc.repo is not None:
            try:
                data_health = svc.repo.quality_report().model_dump(mode="json")
                # L-7: coverage > 100% 时（spot 接口含新股 > universe）夹到 1.0 + 标注
                if data_health and "ohlcv_coverage_today" in data_health:
                    cov = data_health["ohlcv_coverage_today"]
                    if cov is not None and cov > 1.0:
                        data_health["ohlcv_coverage_today_raw"] = cov
                        data_health["ohlcv_coverage_today"] = 1.0
                        data_health["coverage_note"] = "今日 spot 拉取数 > universe（含未入池新股）"
                # P0-followup: 给 UI 多塞两个字段
                #   today_refresh_status: PENDING / IN_PROGRESS / OK / RETRY / SKIPPED_NON_TRADING_DAY
                #   next_refresh_attempt: 下一次 data.refresh_daily cron 时刻 (ISO 字符串)
                _attach_refresh_info(svc, data_health)
            except Exception as exc:  # noqa: BLE001
                data_health = {"error": str(exc)[:200]}

        daemon: dict[str, Any] = {"state": None, "is_alive": False}
        if svc.daemon_state_file is not None:
            state = svc.daemon_state_file.read()
            daemon = {
                "state": state.to_dict() if state else None,
                "is_alive": svc.daemon_state_file.is_alive(max_age_s=600),
            }

        today_batch: dict[str, Any] | None = None
        if svc.sched_store is not None:
            from datetime import date as _date

            run = svc.sched_store.get_job_run("batch.post_close", _date.today().isoformat())
            if run is not None:
                today_batch = {
                    "status": run.status,
                    "reason_code": run.reason_code,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "duration_ms": run.duration_ms,
                }

        scheduler_events_24h_by_level: dict[str, int] = {"info": 0, "warning": 0, "error": 0}
        if svc.sched_store is not None:
            scheduler_events_24h_by_level = svc.sched_store.events_count_24h_by_level()

        return {
            "data_health": data_health,
            "daemon": daemon,
            "today_batch": today_batch,
            "scheduler_events_24h_by_level": scheduler_events_24h_by_level,
        }

    return await asyncio.to_thread(_compute)


@router.get("/job-runs")
async def job_runs(
    limit: int = Query(default=50, ge=1, le=500),
    job_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        return {"runs": [], "n": 0}
    runs = svc.sched_store.list_recent_runs(limit=limit, job_id=job_id, status=status)
    return {
        "runs": [
            {
                "id": r.id,
                "job_id": r.job_id,
                "partition": r.partition,
                "status": r.status,
                "reason_code": r.reason_code,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "duration_ms": r.duration_ms,
                "payload": json.loads(r.payload_json) if r.payload_json else None,
            }
            for r in runs
        ],
        "n": len(runs),
    }


@router.get("/events")
async def events(
    limit: int = Query(default=50, ge=1, le=500),
    level_min: str | None = Query(default=None),
    kind_prefix: str | None = Query(default=None),
) -> dict[str, Any]:
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        return {"events": [], "n": 0}
    rows = svc.sched_store.list_events(limit=limit, level_min=level_min, kind_prefix=kind_prefix)
    return {
        "events": [
            {
                "id": e.id,
                "ts": e.ts,
                "level": e.level,
                "kind": e.kind,
                "source": e.source,
                "payload": json.loads(e.payload_json) if e.payload_json else None,
            }
            for e in rows
        ],
        "n": len(rows),
    }


# ============================================================
# M11: 任务详情 + 子步骤 + 日志
# ============================================================


@router.get("/job-runs/{job_id}/{partition}/detail")
async def job_run_detail(job_id: str, partition: str) -> dict[str, Any]:
    """某 job 某 partition 的详细执行轨迹（含 job_steps 子步骤）。"""
    svc: ServiceContainer = get_services()
    if svc.sched_store is None:
        return {"error": "no_state_store"}
    run = svc.sched_store.get_job_run(job_id, partition)
    if run is None:
        return {"error": "not_found", "job_id": job_id, "partition": partition}

    # 读 job_steps
    steps = []
    if svc.repo is not None:
        from akq_agents.orchestrator.step_recorder import StepReader

        reader = StepReader(svc.repo.meta_db_path)
        steps = reader.list_steps(job_id, partition)

    return {
        "job_id": job_id,
        "partition": partition,
        "status": run.status,
        "reason_code": run.reason_code,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": run.duration_ms,
        "payload": json.loads(run.payload_json) if run.payload_json else None,
        "steps": steps,
        "n_steps": len(steps),
    }


@router.get("/data-freshness")
async def data_freshness() -> dict[str, Any]:
    """L-3: 一次性返回各核心表的最新数据日，让 UI 顶部能展示「系统状态」。"""
    from datetime import date as _date
    svc: ServiceContainer = get_services()
    if svc.repo is None or svc.workflow is None:
        return {"error": "not_ready"}

    def _max(table: str, col: str) -> str | None:
        try:
            from akq_agents.services.data.repository import open_meta_db
            with open_meta_db(svc.repo.meta_db_path) as conn:
                row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
            return row[0] if row else None
        except Exception:
            return None

    today_str = _date.today().isoformat()
    def stale(d: str | None) -> bool:
        return d is None or d < today_str

    # 修复: 下面 8+ 次 _max / _count_table 都是同步 SQLite 查询, 放 async endpoint 里
    # 会阻塞 event loop。整块挪到线程池执行。
    def _compute() -> dict[str, Any]:
        out = {
            "today": today_str,
            "ohlcv_latest": _max("refresh_state", "target_date"),
            "portfolio_snapshots_latest": _max("portfolio_snapshots", "as_of_date"),
            "portfolio_nav_latest": _max("portfolio_nav", "as_of_date"),
            "trade_list_latest": _max("trade_list_cohorts", "cohort_date"),
            "paper_trades_latest": _max("paper_trades", "cohort_date"),
            "paper_perf_latest": _max("paper_track_perf", "as_of_date"),
            "factor_metrics_latest": _max("factor_metrics", "as_of_date"),
            "factor_proposals_count": _count_table(svc.repo, "factor_proposals"),
            "holdings_count": _count_table(svc.repo, "holdings"),
        }
        out["all_fresh"] = all(not stale(out[k]) for k in [
            "ohlcv_latest", "portfolio_snapshots_latest", "trade_list_latest",
        ])
        return out

    return await asyncio.to_thread(_compute)


def _count_table(repo, table: str) -> int:
    from akq_agents.services.data.repository import open_meta_db
    try:
        with open_meta_db(repo.meta_db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _attach_refresh_info(svc: ServiceContainer, data_health: dict[str, Any]) -> None:
    """给 data_health 补 today_refresh_status + next_refresh_attempt。

    - today_refresh_status: PENDING / IN_PROGRESS / OK / RETRY / SKIPPED_NON_TRADING_DAY
    - next_refresh_attempt: 下一次 cron 触发 ISO 时间；非交易日找下一交易日的 first_try

    依赖：repo.is_trading_day + SchedulerConfig（一次性 yaml 解析，~ms 级）
    全程 try/except 包住，失败时不影响主流程。
    """
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    try:
        from akq_agents.bootstrap import load_scheduler_config

        repo = svc.repo
        if repo is None:
            return
        cfg = load_scheduler_config().jobs.data_refresh
        now = _dt.now()
        today = _date.today()

        # 1) today_refresh_status
        if not repo.is_trading_day(today):
            data_health["today_refresh_status"] = "SKIPPED_NON_TRADING_DAY"
        else:
            # 看 refresh_state 表里今天有没有 status='ok'
            from akq_agents.services.data.repository import open_meta_db

            with open_meta_db(repo.meta_db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM refresh_state WHERE target_date = ?",
                    (today.isoformat(),),
                ).fetchone()
            if row and row[0] == "ok":
                data_health["today_refresh_status"] = "OK"
            elif row and row[0] == "partial":
                data_health["today_refresh_status"] = "RETRY"
            elif now.hour < cfg.first_try_hour or (
                now.hour == cfg.first_try_hour and now.minute < cfg.first_try_minute
            ):
                data_health["today_refresh_status"] = "PENDING"
            else:
                # 调度窗口内还没出 ok 行 → 仍在重试
                data_health["today_refresh_status"] = "RETRY"

        # 2) next_refresh_attempt
        triggers: list[tuple[int, int]] = []
        base_min = cfg.first_try_hour * 60 + cfg.first_try_minute
        # 复用 data_refresh.register 同样的窗口生成逻辑
        for i in range(((cfg.stop_hour - cfg.first_try_hour) * 60 - cfg.first_try_minute)
                       // cfg.retry_interval_minutes + 1):
            total = base_min + i * cfg.retry_interval_minutes
            h, m = divmod(total, 60)
            if h >= cfg.stop_hour:
                break
            triggers.append((h, m))

        nxt: _dt | None = None
        if repo.is_trading_day(today):
            now_min = now.hour * 60 + now.minute
            for h, m in triggers:
                if h * 60 + m >= now_min:
                    nxt = _dt.combine(today, _dt.min.time()).replace(hour=h, minute=m)
                    break
        if nxt is None:
            # 找下一个交易日，最多看 10 天
            d = today + _td(days=1)
            for _ in range(10):
                if repo.is_trading_day(d):
                    nxt = _dt.combine(d, _dt.min.time()).replace(
                        hour=cfg.first_try_hour, minute=cfg.first_try_minute
                    )
                    break
                d = d + _td(days=1)
        if nxt is not None:
            data_health["next_refresh_attempt"] = nxt.isoformat(timespec="minutes")
    except Exception as exc:  # noqa: BLE001
        # 不影响主响应，给个 debug 字段
        data_health["refresh_info_error"] = str(exc)[:200]


@router.get("/logs")
async def get_logs(
    source: str = Query(default="daemon", regex="^(daemon|web|akquant_backtest)$"),
    lines: int = Query(default=200, ge=10, le=2000),
    grep: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    """读物理日志文件尾部 N 行，可选 grep 过滤。"""
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[4]
    log_path = {
        "daemon": project_root / "data" / "daemon.log",
        "web": project_root / "data" / "web.log",
        # 回测 / 因子重算的独立分流文件 (attach_named_handler 落地),
        # 而不是 6/17 的 bootstrap.log 死文件。
        "akquant_backtest": project_root / "data" / "backtest.log",
    }.get(source)
    if log_path is None or not log_path.exists():
        return {"source": source, "lines": [], "exists": False}

    try:
        # 修复: 读日志文件 (seek + read ~200KB) 是同步阻塞 IO, 放 async endpoint 会阻塞
        # event loop。用 asyncio.to_thread 挪到线程池。
        def _read_tail() -> tuple[str, int]:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                fsize = f.tell()
                read_size = min(fsize, 200 * 1024)
                f.seek(fsize - read_size)
                data = f.read().decode("utf-8", errors="replace")
            return data, fsize

        chunk, file_size = await asyncio.to_thread(_read_tail)
        all_lines = chunk.splitlines()
    except Exception as exc:  # noqa: BLE001
        return {"source": source, "error": str(exc)[:200], "lines": []}

    if grep:
        all_lines = [ln for ln in all_lines if grep.lower() in ln.lower()]
    tail = all_lines[-lines:]

    # 解析成结构化字段: {ts, level, logger, msg}, 供前端按列渲染。
    # traceback 续行 / 第三方裸输出解析不出级别时, level="" 归到上一条消息体。
    from akq_agents.logging_setup import parse_log_line

    entries = [parse_log_line(ln) for ln in tail]
    return {
        "source": source,
        "path": str(log_path),
        "file_size": file_size,
        "total_lines_returned": len(tail),
        "lines": tail,
        "entries": entries,
        "exists": True,
    }
