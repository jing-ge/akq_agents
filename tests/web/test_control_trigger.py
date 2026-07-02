"""Web /api/control/jobs/*/trigger 手动触发路径回归测试 (M23 重写).

历史 bug (2026-07-01): 之前 force_full=True 走 web 进程 ``svc.job_runner.submit()``
把 batch.deep_research 8 worker 池放进 web 进程. 双 manual 并发直接把 web 进程 CPU
吃到 800%+, data-freshness 端点 5s+ 不返回, 整站不可达.

M23 重写后: web 端点**永远不跑业务**, 只写 pending_triggers + job_runs.status='pending'
立即返回 202. daemon 周期任务 manual_trigger_picker 5s 扫一次 claim 起来在 daemon 进程
跑. web 进程零 CPU 消耗, 整站免疫 web 卡死.

测试覆盖:
- 写入 pending_triggers 表 (有 trigger_id, status=pending, job_id/partition/payload 正确)
- 写入 job_runs.status='pending' (partition 匹配, payload 包含 trigger_id)
- 返回 202 + poll_url (前端轮询目标)
- 并发防护: 同 job_id 已有 pending/running 任务时 409 拒绝
- fast 模式 (force_full=false) 也走异步 (之前 sync 阻塞 web event loop 也是隐患)
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def container_with_sched(assets):
    """基础 conftest 已创建 sched_store (含 pending_triggers 表). 触发器无需更多 mock.
    唯一额外要求: workflow + discovery_engine 在 batch.post_close / factor.discovery 路径
    会被 readiness check, 触发时如果缺则 503. 测试主要跑 batch.deep_research (它有
    fallback 不强依赖 workflow)."""
    return assets["container"]


def _read_pending_triggers(db_path) -> list[dict[str, Any]]:
    import sqlite3
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT id, job_id, partition, payload_json, status, requested_at "
        "FROM pending_triggers ORDER BY id"
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0], "job_id": r[1], "partition": r[2],
            "payload_json": r[3], "status": r[4], "requested_at": r[5],
        }
        for r in rows
    ]


def _read_job_runs(db_path, job_id: str) -> list[tuple]:
    import sqlite3
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT job_id, partition, status, payload_json "
        "FROM job_runs WHERE job_id=? ORDER BY id",
        (job_id,),
    ).fetchall()
    con.close()
    return rows


def test_batch_deep_research_trigger_queues_pending_not_runs(
    client, assets, container_with_sched,
) -> None:
    """回归: web 触发 batch.deep_research 必须走 pending_triggers 异步通道, 不在 web 跑业务.

    验证:
    1. 立即返回 202 (status=accepted, reason_code=ASYNC_QUEUED)
    2. 写一行 pending_triggers (job_id=batch.deep_research, payload.mode='fast')
    3. 写一行 job_runs (status='pending', partition 匹配)
    4. 端到端耗时 < 200ms (CPU 零消耗)
    """
    import time
    db = assets["db"]

    t0 = time.monotonic()
    r = client.post("/api/control/jobs/batch.deep_research/trigger?force_full=false")
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["reason_code"] == "ASYNC_QUEUED"
    assert body["payload"]["job_id"] == "batch.deep_research"
    assert "poll_url" in body["payload"]
    assert "manual-" in body["payload"]["partition"]
    assert elapsed_ms < 200, f"web 端点应 < 200ms, 实际 {elapsed_ms:.0f}ms"

    # pending_triggers 表里有 1 行
    rows = _read_pending_triggers(db)
    assert len(rows) == 1
    assert rows[0]["job_id"] == "batch.deep_research"
    assert rows[0]["status"] == "pending"
    import json
    payload = json.loads(rows[0]["payload_json"])
    assert payload == {"mode": "fast"}

    # job_runs 表里有 1 行 status='pending'
    runs = _read_job_runs(db, "batch.deep_research")
    assert len(runs) == 1
    assert runs[0][1] == rows[0]["partition"]
    assert runs[0][2] == "pending"


def test_batch_deep_research_force_full_passes_full_mode(
    client, assets, container_with_sched,
) -> None:
    """force_full=true 必须透传到 pending_triggers.payload.mode='full'."""
    db = assets["db"]
    r = client.post("/api/control/jobs/batch.deep_research/trigger?force_full=true")
    assert r.status_code == 200
    rows = _read_pending_triggers(db)
    import json
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["mode"] == "full"


def test_factor_discovery_trigger_passes_n_candidates(
    client, assets, container_with_sched,
) -> None:
    """factor.discovery trigger 透传 n_candidates 到 pending_triggers.payload."""
    db = assets["db"]
    r = client.post("/api/control/jobs/factor.discovery/trigger?n_candidates=15")
    assert r.status_code == 200
    rows = _read_pending_triggers(db)
    import json
    assert len(rows) == 1
    assert rows[0]["job_id"] == "factor.discovery"
    assert json.loads(rows[0]["payload_json"]) == {"n_candidates": 15}


def test_factor_eviction_trigger_passes_dry_run(
    client, assets, container_with_sched,
) -> None:
    """factor.eviction trigger 透传 dry_run 到 pending_triggers.payload.

    dry_run=true 和 dry_run=false 是不同语义 (前者只列名单, 后者真删), 互斥: 不能让
    一个在跑时再触发. 测试用两个不同 job_id (batch.deep_research + factor.eviction)
    模拟两个独立 dry_run 参数, 都该 200.
    """
    db = assets["db"]
    r1 = client.post("/api/control/jobs/factor.eviction/trigger?dry_run=true")
    r2 = client.post("/api/control/jobs/batch.deep_research/trigger?force_full=true")
    assert r1.status_code == 200
    assert r2.status_code == 200
    rows = _read_pending_triggers(db)
    import json
    assert len(rows) == 2
    # factor.eviction 是第一个 trigger
    assert rows[0]["job_id"] == "factor.eviction"
    assert json.loads(rows[0]["payload_json"]) == {"dry_run": True}
    # batch.deep_research 是第二个, payload 是 mode=full (跟 dry_run 无关)
    assert rows[1]["job_id"] == "batch.deep_research"
    assert json.loads(rows[1]["payload_json"]) == {"mode": "full"}


def test_concurrent_trigger_same_job_409(
    client, assets, container_with_sched,
) -> None:
    """回归: 同一 job 已有 pending/running 任务时, 二次 trigger 必须 409 拒绝, 防止用户
    连点导致 N 行排队, picker 单线程串行执行浪费 daemon 时间."""
    db = assets["db"]
    r1 = client.post("/api/control/jobs/batch.deep_research/trigger?force_full=true")
    assert r1.status_code == 200
    rows = _read_pending_triggers(db)
    # 模拟 picker 已 claim 第一行 (status=claimed) — 此时第二次 trigger 必须拒
    import sqlite3
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE pending_triggers SET status='claimed', claimed_at='2026-07-01T18:00:00', claimed_by='manual.trigger_picker' WHERE id=?",
        (rows[0]["id"],),
    )
    con.commit()
    con.close()

    r2 = client.post("/api/control/jobs/batch.deep_research/trigger?force_full=true")
    assert r2.status_code == 409, f"同 job 已有 claimed 行, 二次 trigger 应 409; 实际 {r2.status_code}: {r2.text}"
    # pending_triggers 应仍只有 1 行 (第二条没插入)
    rows_after = _read_pending_triggers(db)
    assert len(rows_after) == 1


def test_concurrent_trigger_running_in_job_runs_409(
    client, assets, container_with_sched,
) -> None:
    """即使 pending_triggers 全是 ok/failed, 只要 job_runs 有 status='running' 同行
    (例如 cron 正在跑), 也得 409 拒绝 — 防用户手动 trigger 跟 cron 撞, 抢同一 partition
    写入 (虽然 manual-xxxxxx partition 不会撞, 但 8 worker 池 + cron 8 worker 池
    同时跑 = 16 worker 抢 daemon CPU, 也得防)."""
    db = assets["db"]
    # 模拟 cron 跑中的 batch.deep_research 行 (status='running', partition='2026-07-01')
    import sqlite3
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO job_runs (job_id, partition, status, started_at) VALUES (?, ?, 'running', '2026-07-01T17:00:00')",
        ("batch.deep_research", "2026-07-01"),
    )
    con.commit()
    con.close()

    r = client.post("/api/control/jobs/batch.deep_research/trigger?force_full=true")
    assert r.status_code == 409, f"cron 正在跑应 409; 实际 {r.status_code}: {r.text}"


def test_unknown_job_404(client, container_with_sched) -> None:
    """未支持的 job 仍然 404."""
    r = client.post("/api/control/jobs/nonexistent/trigger")
    assert r.status_code == 404