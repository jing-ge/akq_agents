# 架构清理 + Critical Bug 修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 系统性修复 oracle review 发现的 5 个 Critical bug + 5 个 Important 问题，让数字第一次值得相信、代码量减 30%、流程统一。

**Architecture:**
- 删 老库 `akq_agents.db` + 6 个装饰品 agent（DataAgent/FactorAgent/BacktestAgent/ResearchAgent/RiskAgent/AdvisorAgent）+ 关联 service / cli / config
- 修 portfolio_snapshots 重复入库导致 weight sum > 1（C3 root cause）
- 修 paper_track_perf 缺价 fallback 冷热路径不对称（C4）
- web 触发统一走 JobRunner 写 job_runs（C5）
- web + daemon 同进程合并（I1，删 web/daemon 双进程）
- yaml/default 对齐（I4）+ silent fallback 加 events 记账（I5）+ 文档对齐（N1/N2/N5）

**Tech Stack:** 既有栈，无新依赖

**关键 root cause（必读）：**

oracle review 标的"NAV 单日 +56%"实际根因不在 backtester，而在 **`PortfolioSnapshotStore.write`**：
- `INSERT ... ON CONFLICT(as_of_date, symbol) DO UPDATE` 只更新已存在 symbol，**不删旧的不在 new 集合的 symbol**
- 如果同一天调用两次 `write`，第二次的 symbol 集合不同 → 旧 symbol 残留 + 新 symbol 写入 = **weight sum > 1**
- 6/17 sum=1.5363（85 行）、6/18 sum=1.5169（92 行）——其它 187 天全部 sum=1.0
- 6/17/6/18 那天用户在调试 m13/m14，多次 web 手动 trigger（C5：trigger 绕过 JobRunner 幂等）→ 重复入库

修法：`write` 前先删除 `WHERE as_of_date=?`（"replace 当日所有"语义）。

---

## 任务总览

| # | Task | Critical/Important | 时间 |
|---|---|---|---|
| 1 | 修 portfolio_snapshots 重复入库（C3 root cause） + 修复历史脏数据 | 🔴 Critical | 1.5h |
| 2 | 修 paper_track_perf 缺价 fallback 冷热对称（C4） | 🔴 Critical | 1h |
| 3 | web 触发统一走 JobRunner（C5） | 🔴 Critical | 1.5h |
| 4 | 删老库 + SQLiteStore + cli query/analyze + workflow._persist_state（C1） | 🔴 Critical | 2h |
| 5 | 砍 6 个装饰品 agent + workflow 简化（C2） | 🔴 Critical | 2h |
| 6 | scheduler.yaml 与 default 对齐（I4） | 🟠 Important | 30min |
| 7 | silent fallback 加 events 记账（I5，关键 4 处） | 🟠 Important | 1.5h |
| 8 | web + daemon 同进程合并（I1） | 🟠 Important | 3h |
| 9 | 文档与代码对齐（N1+N2+N5） | 🟡 Nice-to-have | 1h |
| 10 | 端到端验证 + 金丝雀回测测试 | — | 1h |

**预估总耗时：** 14-15 小时（按这个量级，建议分 2-3 个 session）

---

## 关键设计决策

1. **C3 修法选择**：在 `write()` 入口先 `DELETE WHERE as_of_date=?`，而**不**改 ON CONFLICT 语义。原因：
   - 删 ON CONFLICT 子句会影响测试 mock
   - "replace 当日全部" 是更直觉的语义（把当日 snapshot 当幂等单元）
   - 任何重跑场景下都正确（不依赖调用方记得清理）

2. **历史脏数据处理**：6/17、6/18 两天的 weight sum 重新归一化到 1.0（直接 SQL UPDATE），不删数据。这两天的 nav_net 错误会在下一轮 `rebuild_full_history()` 自动修正。

3. **删老 agent 边界**：保留 `AnalystAgent`（仍在用，跑 LLM 报告）+ `PortfolioAgent`（核心业务）+ `ReportAgent`（生成 markdown）。删的是 6 个无人消费的装饰品 + `factor_service.FactorLibrary` + `services/storage.py`。

4. **I1 进程合并方案**：删 daemon 子进程，把 APScheduler 直接挂在 web 进程的 FastAPI lifespan 里。`start.sh` 简化成只起 web。`watch` 模式废弃。

5. **测试策略**：每个 task 走 TDD（先写失败测试 → 实现 → 通过）。Task 10 加金丝雀测试：mock 5 日序列已知收益 +5%，确保 backtester 输出 = +5%。

---

（详细 task 步骤见后续章节）

---

## Task 1：修 portfolio_snapshots 重复入库（C3 root cause）

**Goal**：让 `PortfolioSnapshotStore.write()` 是"replace 当日全部"语义，杜绝同日多次调用导致 weight sum > 1。

**Files:**
- Modify: `src/akq_agents/services/portfolio/snapshot_store.py:103-119`
- Modify: `tests/portfolio/` 下相关测试（找到后追加 1 个 regression test）
- Manual SQL: 修复 6/17、6/18 历史 weight 归一化

### Step 1: 找到现有测试

```bash
grep -rn 'PortfolioSnapshotStore\|snapshot_store' tests/ 2>/dev/null | grep -v __pycache__ | head
```

### Step 2: 追加 regression test（先 RED）

文件：`tests/portfolio/test_snapshot_store.py`（新建或追加）

```python
"""PortfolioSnapshotStore C3 regression: 同日多次 write 不应导致 weight sum > 1。"""
from datetime import date
from pathlib import Path

import pandas as pd

from akq_agents.services.portfolio.snapshot_store import PortfolioSnapshotStore


def test_write_replaces_all_rows_for_same_date(tmp_path: Path) -> None:
    """C3 root cause regression: 同 as_of_date 第二次 write 应替换而非累加。

    bug 历史: 6/17/6/18 出现 weight sum=1.5 因为 ON CONFLICT DO UPDATE 只更新
    已存在 symbol，旧 symbol 残留 + 新 symbol 写入 → 累加。
    """
    store = PortfolioSnapshotStore(tmp_path / "meta.db")

    # 第一次：50 只票，权重 0.02 each → sum=1.0
    weights1 = pd.Series({f"00000{i}": 0.02 for i in range(1, 51)}, name="weight")
    composite1 = pd.Series({s: 1.0 for s in weights1.index}, name="composite_score")
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=weights1,
        composite_score=composite1,
        prev_weights=None,
        industry_map={s: "电子" for s in weights1.index},
        name_map={s: f"票{s}" for s in weights1.index},
        top_factors_per_symbol={s: [] for s in weights1.index},
    )

    # 第二次：完全不同的 30 只票，权重 ~0.033 each → sum=1.0
    weights2 = pd.Series({f"99000{i}": 0.0333 for i in range(1, 31)}, name="weight")
    composite2 = pd.Series({s: 1.0 for s in weights2.index}, name="composite_score")
    store.write(
        as_of_date=date(2026, 6, 17),
        weights=weights2,
        composite_score=composite2,
        prev_weights=None,
        industry_map={s: "电力" for s in weights2.index},
        name_map={s: f"票{s}" for s in weights2.index},
        top_factors_per_symbol={s: [] for s in weights2.index},
    )

    # 应只剩 30 行（第二次的全部），不是 50+30=80
    rows = store.read_snapshot(date(2026, 6, 17))
    assert len(rows) == 30, f"期望 30 行（replace 语义），实际 {len(rows)}"
    total_weight = sum(float(r.weight) for r in rows)
    assert abs(total_weight - 1.0) < 1e-6, f"权重总和应为 1.0，实际 {total_weight:.4f}"
    # 确认全是第二次写的 symbol
    syms = {r.symbol for r in rows}
    assert syms == set(weights2.index), "应该完全替换为第二次的 symbol 集合"
```

### Step 3: 跑测试确认 FAIL

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/portfolio/test_snapshot_store.py::test_write_replaces_all_rows_for_same_date -v
```

期望：FAIL（当前 ON CONFLICT 行为）。

### Step 4: 实现修复

修改 `src/akq_agents/services/portfolio/snapshot_store.py:103-119`，在 `executemany INSERT` 之前先 DELETE 当日所有：

```python
        with open_meta_db(self._db) as conn:
            # C3 fix: replace 当日全部行，避免同日多次 write 导致 weight sum > 1
            # （ON CONFLICT 只更新已存在 symbol，新 symbol 累加 → 累计 > 1）
            conn.execute(
                "DELETE FROM portfolio_snapshots WHERE as_of_date = ?",
                (as_of_date.isoformat(),),
            )
            conn.executemany(
                """
                INSERT INTO portfolio_snapshots
                  (as_of_date, symbol, name, industry, weight, prev_weight, composite_score, top_factors_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return len(rows)
```

注意：删 `ON CONFLICT(...) DO UPDATE` 子句，因为 DELETE 已保证无冲突。

### Step 5: 跑测试确认 PASS

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/portfolio/ -v
```

期望：所有测试通过。

### Step 6: 修复历史脏数据 + 重算 NAV

```bash
# 6.1 重新归一化 6/17、6/18 的 weight
/opt/anaconda3/envs/akq310/bin/python << 'PYEOF'
import sqlite3
con = sqlite3.connect('data/meta.db')
for d in ('2026-06-17', '2026-06-18'):
    cur = con.execute(
        "SELECT SUM(weight) FROM portfolio_snapshots WHERE as_of_date = ?", (d,)
    )
    total = cur.fetchone()[0]
    print(f"{d}: 当前 sum={total:.4f}")
    if abs(total - 1.0) > 0.01:
        con.execute(
            "UPDATE portfolio_snapshots SET weight = weight / ? WHERE as_of_date = ?",
            (total, d),
        )
        print(f"  → 归一化到 1.0")
con.commit()
# verify
for d in ('2026-06-17', '2026-06-18'):
    cur = con.execute("SELECT SUM(weight) FROM portfolio_snapshots WHERE as_of_date = ?", (d,))
    print(f"{d}: 修复后 sum={cur.fetchone()[0]:.4f}")
PYEOF

# 6.2 触发 backtester 重算 NAV
curl -sf -X POST http://127.0.0.1:8765/api/control/jobs/batch.post_close/trigger --max-time 600 || echo "（trigger 跑了，超 curl timeout 没关系，看 DB）"

# 6.3 验证 portfolio_nav 单日 return 全部 < 15%
/opt/anaconda3/envs/akq310/bin/python -c "
import sqlite3
con = sqlite3.connect('data/meta.db')
rows = list(con.execute(
    'SELECT as_of_date, daily_return_net FROM portfolio_nav WHERE ABS(daily_return_net) > 0.15'
))
if rows:
    print(f'❌ 仍有 {len(rows)} 天 |daily_return| > 15%:'); print(rows)
else:
    print('✅ 所有日 |daily_return| < 15%，NAV 真实性恢复')
"
```

### Step 7: Commit

```bash
git add src/akq_agents/services/portfolio/snapshot_store.py tests/portfolio/test_snapshot_store.py
git commit -m "fix(portfolio): C3 — snapshot_store.write 改 replace 当日全部语义

之前 ON CONFLICT(as_of_date,symbol) DO UPDATE 只更新已存在 symbol，
旧 symbol 残留 + 新 symbol 累加 → weight sum > 1。

历史症状: 6/17 wsum=1.54（85 行）, 6/18 wsum=1.52（92 行），
导致 portfolio_nav 6/18 +56%, 6/22 +57% 这种伪净值。

修法: write() 入口先 DELETE WHERE as_of_date=?，再 executemany INSERT。
任何重跑场景下都正确，不依赖调用方记得清理。

附 regression test: 同 date 第二次 write 应只剩第二次的 symbol。"
```

---

## Task 2：修 paper_track_perf 缺价 fallback 冷热对称（C4）

**Goal**：让 `update_track_perf` 缺价 fallback 与 `freeze_today_cohort` 一致，都查 cohort_close_lookup 而非用 frozen_price。

**Files:**
- Modify: `src/akq_agents/services/portfolio/paper_trading.py:update_track_perf`
- Modify: `src/akq_agents/agents/portfolio_agent.py`（传 lookup 给 update_track_perf）
- Test: 找现有 paper_trading 测试或新建

### Step 1: 阅读现状

```bash
grep -n 'def update_track_perf\|def freeze_today_cohort\|frozen_price\|fallback_lookup' src/akq_agents/services/portfolio/paper_trading.py | head
```

确认两个函数的 signature 差异。

### Step 2: 写 regression test

文件：`tests/portfolio/test_paper_trading.py`（追加或新建）

```python
def test_update_track_perf_uses_lookup_for_missing_close(tmp_path):
    """C4 regression: 估值缺价时应查最近有效价（lookup），不能直接 fallback frozen_price。

    冷热路径不对称: freeze 时努力查，估值时不查 → 上涨期高估收益。
    """
    from datetime import date
    from akq_agents.services.portfolio.paper_trading import PaperTradingStore

    store = PaperTradingStore(tmp_path / "meta.db")
    cohort_date = date(2026, 1, 1)
    today = date(2026, 6, 23)
    weights = {"000001": 0.5, "000002": 0.5}
    cohort_close = {"000001": 10.0, "000002": 20.0}
    store.freeze_today_cohort(cohort_date, weights, cohort_close)

    # 估值时 today_close 缺 000001（停牌假设）
    today_close = {"000002": 30.0}  # 涨 50%

    # lookup 返回 000001 在 2026-06-22 的最近有效价 11.0（涨 10%）
    def lookup(symbol, d):
        if symbol == "000001" and d == today:
            return 11.0
        return None

    store.update_track_perf(today, today_close, lookup=lookup)

    rows = store.read_track_perf(today)
    perf = next(r for r in rows if r.cohort_date == cohort_date)
    # 期望 return = 0.5*(11/10-1) + 0.5*(30/20-1) = 0.05 + 0.25 = 0.30
    assert abs(perf.return_pct - 0.30) < 0.01, \
        f"期望 30%，实际 {perf.return_pct*100:.1f}%（说明仍用 frozen_price，未查 lookup）"
```

### Step 3: 跑确认 FAIL

### Step 4: 修实现

`paper_trading.py:update_track_perf` 加 `lookup` 参数（与 freeze 同 signature），缺价时优先 lookup，再 fallback frozen_price。`portfolio_agent.py` 调用时复用 `_cohort_lookup` 闭包。

具体代码：找到现有 update_track_perf 函数体，把缺价分支改成：

```python
            close = today_close.get(symbol)
            if close is None or close <= 0:
                # C4: 先查 lookup（与 freeze 路径对称），最后才 fallback frozen_price
                if lookup is not None:
                    looked = lookup(symbol, as_of_date)
                    if looked and looked > 0:
                        close = float(looked)
                if close is None or close <= 0:
                    close = float(frozen_price)  # 最后兜底
```

### Step 5: 跑测试

### Step 6: Commit

```bash
git commit -m "fix(paper_trading): C4 — update_track_perf 缺价 fallback 改用 lookup

之前 freeze 路径努力查最近有效价（cohort_close_lookup），estimate 路径
直接用 frozen_price → 冷热不对称 → 上涨期高估收益。

修法: update_track_perf 增 lookup 参数，缺价时先查 lookup 再 fallback。
portfolio_agent._cohort_lookup 闭包同时传给 freeze 和 update_track_perf。"
```

---

## Task 3：web 触发统一走 JobRunner（C5）

**Goal**：所有 web `/api/control/jobs/*/trigger` 和 `/api/research/factors/brainstorm/run` 都走 JobRunner，写 job_runs + events，统一记账。

**Files:**
- Modify: `src/akq_agents/web/api/control.py`（3 个 trigger endpoint）
- Modify: `src/akq_agents/web/api/research.py`（brainstorm endpoint）
- Modify: `src/akq_agents/web/deps.py`（让 web 进程拿到 JobRunner）
- Test: 改现有 7 个 web/api/research_brainstorm 测试 + 新增 control endpoint 测试

### Step 1: 给 web 进程加 JobRunner

`web/deps.py` 在 `_build_default()` 里加：

```python
        # M15: web 进程也持有 JobRunner，让 trigger endpoint 能写 job_runs
        from akq_agents.orchestrator.job_runner import JobRunner
        from akq_agents.services.data.calendar import TradingCalendar
        calendar = TradingCalendar()
        # web 进程的 JobRunner 与 daemon 共用同一份 sched_store/meta.db，
        # JobRunner 内部 UNIQUE 约束保证两进程不会双写同 (job_id, partition)
        job_runner = JobRunner(
            sched_store,
            is_trading_day=calendar.is_trading_day,
            executor=ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-job"),
        )
```

并在 ServiceContainer 加字段 `job_runner: Any = None`。

### Step 2: 改 control.py 4 个 trigger

把 `batch.post_close / batch.deep_research / factor.discovery / data.refresh` 4 处直接调 `_do(services)` 改成 `runner.run(JOB_ID, partition, lambda: _do(services), timeout_s=...)`。

### Step 3: 改 research.py 的 brainstorm

`/api/research/factors/brainstorm/run`：

```python
    from akq_agents.orchestrator.jobs.factor_brainstorm import run_once_now
    result = run_once_now(svc.job_runner, services, n=n)
    # result 是 JobRunResult；从 payload 拿 stats
    stats = result.payload if result.status == "ok" else {}
    return {"ok": True, "status": result.status, "stats": stats}
```

### Step 4: 改测试 fixture

`tests/web/test_research_brainstorm.py:container_with_brainstorm` 之前 fake_runner 没真触发；改成正确的 mock：让 fake_runner.run 真正调 fn() 并写出 JobRunResult。

### Step 5: 跑测试 + 端到端验证

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/web/ tests/orchestrator/test_factor_brainstorm_job.py -v
# 端到端
./start.sh stop && sleep 2 && ./start.sh up
curl -X POST http://127.0.0.1:8765/api/research/factors/brainstorm/run -d '{"n":2}' -H 'Content-Type: application/json'
# 验证 job_runs 表有新记录
sqlite3 data/meta.db "SELECT job_id, partition, status, started_at FROM job_runs WHERE job_id='factor.brainstorm' ORDER BY started_at DESC LIMIT 3"
```

### Step 6: Commit

```bash
git commit -m "fix(web): C5 — web trigger endpoint 统一走 JobRunner 写 job_runs

之前 4 个 trigger endpoint 直接调 _do(services) 绕过 JobRunner:
- 不写 job_runs / events
- 破坏 (job_id, partition) 幂等性: daemon cron 后用户手动 trigger 会再跑一次
- /ops 看板看不到任何手动操作记录

修法: web 进程在 deps.py 装一个 JobRunner（与 daemon 共用 sched_store
+ meta.db UNIQUE 约束保证两进程不双写）。所有 trigger endpoint 走
runner.run(JOB_ID, partition, ...)，统一记账。"
```

---

## Task 4：删老库 + SQLiteStore + workflow._persist_state（C1）

**Goal**：清理老库（akq_agents.db）+ 5 张废弃表 + 不再被读的写入路径。代码量 -300 行。

**Files to delete:**
- `src/akq_agents/services/storage.py` — `SQLiteStore` 类（160 行）
- `akq_agents.db` — 18MB 物理文件

**Files to modify:**
- `src/akq_agents/orchestrator/workflow.py` — 删 `_persist_state` 方法 + `sqlite_store` 字段（~50 行）
- `src/akq_agents/bootstrap.py` — 删 `SQLiteStore(config.storage.sqlite_path)` 装配
- `src/akq_agents/cli/app.py` — 删 `cmd_query / cmd_analyze / cmd_export / cmd_notify` 4 个命令（~70 行）
- `src/akq_agents/models/config.py:57` — 删 `sqlite_path` 配置项
- `config/system.yaml` — 删 `storage.sqlite_path` 段落
- `runtime_state.yaml` — 物理删除（7MB 老 state）

### Step 1: 确认所有读写位置（再次扫描）

```bash
grep -rn 'SQLiteStore\|sqlite_path\|akq_agents\.db\|_persist_state' src/ tests/ 2>/dev/null | grep -v __pycache__
```

如果有意外的依赖（比如某个 test 还在用 SQLiteStore），先记录下来。

### Step 2: 删测试中的相关用例

如果 `tests/` 里有用 SQLiteStore 的测试（`tests/services/test_storage.py` 等），全删。

### Step 3: 删 storage.py + workflow._persist_state

```bash
rm src/akq_agents/services/storage.py
```

`workflow.py`:
- 删 `from akq_agents.services.storage import ...` import
- 删 `__init__` 里 `self.sqlite_store = ...` 装配
- 删 `_persist_state` 整个方法（~50 行）
- `run_once` 末尾的 `self._persist_state(context.state)` 调用删掉

### Step 4: 删 cli 4 个命令

`cli/app.py`:
- 删 `cmd_query / cmd_analyze / cmd_export / cmd_notify` 4 个函数
- 在 `make_parser()` 里删对应的 `subparsers.add_parser` 注册（4 处）
- 删 `from akq_agents.services.storage import SQLiteStore` import

### Step 5: 删配置项

`models/config.py` 删 `sqlite_path: str = "./akq_agents.db"`。
`config/system.yaml` 删 `storage:` 整段（如果只剩 sqlite_path 一个字段）。

### Step 6: 物理删文件

```bash
rm akq_agents.db runtime_state.yaml
```

### Step 7: 跑全量测试

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q
```

期望：所有还活着的测试 PASS（已经预期会有几个失败的 pre-existing 测试，比如 `test_root_redirects_to_ops` 之前就失败）。

### Step 8: 烟雾测试

```bash
./start.sh stop && sleep 2 && ./start.sh up
sleep 5
curl -sf http://127.0.0.1:8765/ops > /dev/null && echo 'web ok'
curl -sf http://127.0.0.1:8765/api/ops/health | python -m json.tool | head -15
ls -la akq_agents.db 2>&1 | head -1  # 应该 No such file
```

### Step 9: Commit

```bash
git commit -m "refactor: C1 — 删老库 akq_agents.db + SQLiteStore + workflow._persist_state

oracle review 发现:
- 老库 akq_agents.db 18MB，每天 batch 写 ~500 行到 5 张表
  (market_snapshots/factor_scores/backtest_reports/portfolio_recommendations/daily_advices)
- 但只有 cli/app.py:cmd_query|analyze|export|notify 读这些表
- 没有 web/daemon/scheduler 读 → 死写入，磁盘单调增长

删除:
- src/akq_agents/services/storage.py (SQLiteStore 类, 160 行)
- workflow._persist_state 方法 (50 行)
- cli 4 个查询/分析/导出/通知命令 (70 行)
- models/config.py 的 sqlite_path 配置项
- config/system.yaml 的 storage 段
- 物理文件 akq_agents.db + runtime_state.yaml"
```

---

## Task 5：砍 6 个装饰品 agent + workflow 简化（C2）

**Goal**：删 DataAgent/FactorAgent/BacktestAgent/ResearchAgent/RiskAgent/AdvisorAgent + factor_service.FactorLibrary。workflow 砍到 [PortfolioAgent, AnalystAgent, ReportAgent]。

**Files to delete:**
- `src/akq_agents/agents/data_agent.py`
- `src/akq_agents/agents/factor_agent.py`
- `src/akq_agents/agents/backtest_agent.py`
- `src/akq_agents/agents/research_agent.py`
- `src/akq_agents/agents/risk_agent.py`
- `src/akq_agents/agents/advisor_agent.py`
- `src/akq_agents/services/factor_service.py`
- `src/akq_agents/services/akshare_service.py`（如果只被 DataAgent 用）
- 相关测试 `tests/data/test_factor_agent_integration.py` 等

**Files to modify:**
- `src/akq_agents/orchestrator/workflow.py` — 砍 agents 列表
- `src/akq_agents/agents/portfolio_agent.py` — 删 `_run_legacy` 路径（~50 行）
- `src/akq_agents/orchestrator/jobs/batch_post_close.py` — `_do` 简化（不再 `outputs.get("advisor-agent")`）
- `src/akq_agents/web/api/control.py:139` — 删 `outputs.get("advisor-agent")`
- 测试 `tests/orchestrator/test_jobs_post_close.py` — 改 mock workflow.run_once 返回值

### Step 1: 再次扫描死代码引用

```bash
for agent in data_agent factor_agent backtest_agent research_agent risk_agent advisor_agent; do
  echo "--- ${agent} 仍被引用的位置 ---"
  grep -rn "from akq_agents.agents.${agent}" src/ tests/ 2>/dev/null | grep -v __pycache__
done
```

记录下来，按位置一处一处改。

### Step 2: 改 workflow.py

删 imports + `__init__` 里 6 个 agent 的装配 + 让 `self.agents = [PortfolioAgent, AnalystAgent, ReportAgent]`。

注意：`AnalystAgent` 装配可能依赖 `llm_orchestrator / llm_cfg`，要保持。

### Step 3: 删 portfolio_agent 的 `_run_legacy`

`agents/portfolio_agent.py:384`+ 找到 `_run_legacy` 方法删掉。同时删调用它的判断（用 `_run_p3` 单一路径）。

### Step 4: 改 batch_post_close._do

```python
def _do(services: dict[str, Any]) -> dict[str, Any]:
    workflow = services["workflow"]
    recorder = _make_recorder(services)
    outputs = workflow.run_once(recorder=recorder) if recorder else workflow.run_once()
    portfolio_out = outputs.get("portfolio-agent", {}) if isinstance(outputs, dict) else {}
    if isinstance(portfolio_out, dict) and portfolio_out.get("status") == "skipped":
        from datetime import date as _date
        from akq_agents.services.data.exceptions import DataNotReady
        raise DataNotReady({f"portfolio_agent:{portfolio_out.get('reason', 'unknown')}": [_date.today()]})
    analyst_out = outputs.get("analyst-agent", {}) if isinstance(outputs, dict) else {}
    return {
        "agents": list(outputs.keys()) if isinstance(outputs, dict) else [],
        "analyst_chars": len(analyst_out.get("rendered", "")) if isinstance(analyst_out, dict) else 0,
        "portfolio_n": portfolio_out.get("portfolio_size", 0) if isinstance(portfolio_out, dict) else 0,
    }
```

注意把 `advice_rendered_chars` 改成 `analyst_chars`。

### Step 5: 改 control.py:139

```python
return {
    "status": "ok",
    "portfolio_size": ...,
    "analyst": (outputs.get("analyst-agent") or {}).get("rendered", "")[:400],
}
```

### Step 6: 删测试

`tests/data/test_factor_agent_integration.py` 整个删（FactorAgent 不存在了）。

`tests/orchestrator/test_jobs_post_close.py` 把 mock workflow 返回里的 `data-agent / factor-agent / advisor-agent` 改成 `portfolio-agent / analyst-agent`。

`tests/orchestrator/test_daemon_lifecycle.py:48` 同改。

### Step 7: 物理删文件

```bash
rm src/akq_agents/agents/{data_agent,factor_agent,backtest_agent,research_agent,risk_agent,advisor_agent}.py
rm src/akq_agents/services/factor_service.py
# akshare_service.py 视情况删（如果有别处引用就保留）
```

### Step 8: 跑测试

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q 2>&1 | tail
```

### Step 9: 烟雾测试

```bash
./start.sh stop && sleep 2 && ./start.sh up
sleep 5
curl -sf http://127.0.0.1:8765/api/ops/health > /dev/null && echo 'web ok'
# 触发一次 batch.post_close，看是否能跑通
curl -X POST http://127.0.0.1:8765/api/control/jobs/batch.post_close/trigger --max-time 600
sleep 60
sqlite3 data/meta.db "SELECT job_id, partition, status FROM job_runs WHERE job_id='batch.post_close' ORDER BY started_at DESC LIMIT 1"
```

### Step 10: Commit

```bash
git commit -m "refactor: C2 — 砍 6 个装饰品 agent + workflow 简化

oracle review 发现 老 5-agent 链路对真实组合零影响:
- DataAgent → factor_agent.value/quality/size = 0.0 写死
- FactorAgent (legacy) → factor_scores 没人读
- BacktestAgent → backtest_reports 9 月没人读
- ResearchAgent → 读 backtest_reports 筛选，selected_factors 无人用
- RiskAgent → 读 5 只票 factor_scores 当 50 只 P3 portfolio 滤镜
- AdvisorAgent → buy_candidates = portfolio[:2] 这种装饰性建议

实际工作的: PortfolioAgent._run_p3 (FactorEngine + composite_scorer +
optimizer)，自给自足，前 4 agent 写到 state 的全不读。

删除:
- 6 个 agent 文件
- factor_service.FactorLibrary（写死 0 的 9 个因子计算器）
- portfolio_agent._run_legacy 路径
- 关联测试

workflow.agents 砍到 [PortfolioAgent, AnalystAgent, ReportAgent]
代码量 -700 行"
```

---

## Task 6：scheduler.yaml 与 default 对齐（I4）

**Goal**：避免 yaml 丢失导致 default fallback 引发回归（之前 `batch_post_close hour 15→16` 修复就是这种坑）。

**Files:**
- Modify: `src/akq_agents/models/scheduler_config.py` — 改 `BatchJobConfig.hour` 默认值

### Step 1: 改默认值

`scheduler_config.py:20-21`:
```python
class BatchJobConfig(BaseModel):
    enabled: bool = True
    timeout_s: int = 5400
    hour: int = 16    # 改: 必须晚于 data_refresh.first_try_hour=16
    minute: int = 30
    day_of_week: str | None = None
```

注意 `batch_deep_research` default factory 已经是 `hour=22`，不改它。

### Step 2: 跑现有测试

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -k 'scheduler or post_close' -v
```

### Step 3: Commit

```bash
git commit -m "fix(scheduler): I4 — BatchJobConfig.hour default 15 → 16

跟 scheduler.yaml 对齐。如果 yaml 文件丢失或被改名，load_scheduler_config()
会 fallback 到 default → 之前 hour=15 会让 post_close 跑在 data_refresh
(16:00) 之前 → 又踩 6/22 那种 silent failure 坑。

现在 default = 16:30，与 yaml 一致。"
```

---

## Task 7：silent fallback 加 events 记账（I5）

**Goal**：4 个关键 silent fallback 位置加 `sched_store.write_event(level="warning", ...)`，让盲区可观测。

**Files:**
- `src/akq_agents/agents/portfolio_agent.py:207, 263, 276, 327` — paper_trading 等失败 fallback
- `src/akq_agents/services/factors/discovery.py:557, 590` — _promote_shadows 失败
- `src/akq_agents/agents/factor_agent.py:46-48` —— 但 factor_agent 已删，这条作废

实际改 4 处即可。

### Step 1: 找具体行号（grep 现状）

```bash
grep -n 'except Exception\|noqa: BLE001' src/akq_agents/agents/portfolio_agent.py src/akq_agents/services/factors/discovery.py | head -15
```

### Step 2: 改造模式（一致风格）

把：
```python
try:
    paper.update_track_perf(...)
except Exception as exc:
    logger.warning("paper_trading update failed: %s", exc)
```

改成：
```python
try:
    paper.update_track_perf(...)
except Exception as exc:  # noqa: BLE001
    logger.warning("paper_trading update failed: %s", exc)
    # I5: 关键路径 silent fallback 必须写 events 让 ops 看得见
    if "scheduler_state_store" in self._services:
        try:
            self._services["scheduler_state_store"].write_event(
                level="warning",
                kind="paper_trading.update_failed",
                source="portfolio_agent",
                payload={"error": str(exc)[:200]},
            )
        except Exception:
            pass  # events 记账失败不影响主流程
```

注意 `write_event` 的实际 signature 要先 grep 确认（见 `src/akq_agents/orchestrator/state_store.py`）。

### Step 3: 改 4 处

按 Step 1 grep 结果列表定位，每处都加 events 记账（kind 起好名字方便后期 grep）：
- `portfolio_agent.py` paper_trading update / freeze 失败 → kind=`paper_trading.update_failed` / `paper_trading.freeze_failed`
- `discovery.py:_promote_shadows` 失败 → kind=`factor.promote_shadow.failed`
- `discovery.py:_prepare_data` 失败 → kind=`factor.discovery.prepare_failed`

### Step 4: 跑测试

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q
```

### Step 5: Commit

```bash
git commit -m "fix(observability): I5 — silent fallback 关键 4 处加 events 记账

oracle review: noqa: BLE001 32 处 / except Exception 71 处，集中在
portfolio_agent / discovery / paper_trading。出问题不写 events，导致 ops
看板看不见，调试只能翻 daemon.log。

改 4 处关键 silent fallback (其它 silent 是合理 fallback 不动):
- portfolio_agent paper.update_track_perf 失败
- portfolio_agent paper.freeze_today_cohort 失败
- discovery._promote_shadows 失败
- discovery._prepare_data 失败

写 events kind=*.failed level=warning，让 /ops 红条能感知。"
```

---

## Task 8：web + daemon 同进程合并（I1）

**Goal**：删 daemon 子进程模型。APScheduler 直接挂在 web 的 FastAPI lifespan 里。`start.sh` 简化。

**Files to modify:**
- `src/akq_agents/web/app.py` — 加 lifespan + APScheduler 启动/关闭
- `src/akq_agents/web/deps.py` — 装配 daemon services（job_runner / scheduler）
- `src/akq_agents/orchestrator/scheduler.py` — `QuantDaemon` 改成可被 web 使用的 `SchedulerSetup` helper（不强制起进程）
- `src/akq_agents/cli/app.py` — `cmd_daemon_start / cmd_daemon_stop` 删（不再需要）
- `start.sh` — 大幅简化

**Files to delete:**
- 目前 daemon 进程相关的 `data/daemon.pid` 物理文件（运行时可清）

⚠️ 这个 task 影响面大，建议**最后做**或**单独 session 做**，避免上面 7 个 task 中途出问题时调试更难。

### Step 1: FastAPI lifespan 接 APScheduler

`src/akq_agents/web/app.py`:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    from akq_agents.web.deps import get_services
    svc = get_services()
    if svc.scheduler is not None:
        svc.scheduler.start()
        logger.info("APScheduler started in web lifespan")
    yield
    # shutdown
    if svc.scheduler is not None:
        svc.scheduler.shutdown(wait=False)
        logger.info("APScheduler shutdown")

def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    ...
```

### Step 2: deps.py 装配 scheduler

```python
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        # 注册所有 jobs（复用 scheduler.py 的 _register_jobs 逻辑）
        from akq_agents.orchestrator.scheduler import register_all_jobs
        register_all_jobs(scheduler, job_runner, scheduler_config, services)
```

### Step 3: 把 scheduler.py 的 QuantDaemon 拆成两半

- `register_all_jobs(scheduler, runner, cfg, services)` — 纯函数，注册 jobs（web 也调用）
- `QuantDaemon` 类保留作 **CLI 后台运行模式** （`./start.sh daemon`，可能仍有人用），但 web 里不用它

### Step 4: 删 cli daemon 命令

`cli/app.py:cmd_daemon_start / cmd_daemon_stop` 删，及对应 subparser 注册。

### Step 5: 简化 start.sh

```bash
# 新版 start.sh 只起 web，APScheduler 跑在 web 进程内
cmd_up() {
    cmd_web  # 只起 web
    cmd_status
}
# 删 cmd_daemon_start / cmd_watch
```

### Step 6: 删 daemon_state_file 双进程同步

之前 `daemon.pid + daemon_state.json` 是为跨进程通信用的，合并后 web 自己就是 daemon，可以简化（但暂时保留 `/api/ops/health` 的 `daemon.is_alive` 字段，让 UI 不破坏）。

### Step 7: 测试

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q
./start.sh stop && sleep 2 && ./start.sh up
sleep 5
curl -sf http://127.0.0.1:8765/api/ops/health | python -m json.tool | grep -E 'daemon|status'
# 应该看到 daemon.is_alive = True (因为 web 自己就是 daemon)
ps aux | grep -E 'akq_agents' | grep -v grep
# 应该只有一个 python 进程
```

### Step 8: Commit

```bash
git commit -m "refactor: I1 — web + daemon 同进程合并

oracle review: 单机版根本不需要 web/daemon 进程隔离。三套 ServiceContainer
装配 (daemon/web/test) 导致 _proposal_store helper 兼容三种位置。状态共享
只能走 SQLite 跨进程，daemon 加载新 accepted 因子到 registry 后 web 不知道。

合并方案:
- APScheduler 挂在 FastAPI lifespan，web 起即调度起
- ServiceContainer 单一来源
- start.sh 简化为只起 web
- 删 cli daemon_start/stop 命令

代价: web 重启会重新跑 build_workflow ~5-10s，但单机自用可接受。"
```

---

## Task 9：文档与代码对齐（N1+N2+N5）

**Goal**：删过期文档 + 加 spec 历史标注 + 删 enable_llm 失效配置。

**Files:**
- `docs/next_steps.md` — 删（P1 之前的，全过时）
- `docs/superpowers/specs/2026-06-17-*.md` — 头部加一行说明
- `config/system.yaml` — 删 `enable_llm: false`
- `README.md` — 软化"已具备真实研究环境"措辞，提及 N3 holdings 闭环未建立

### Step 1: 删 next_steps.md

```bash
git rm docs/next_steps.md
```

### Step 2: 给 specs 加历史标注

`docs/superpowers/specs/` 下 5 个 P1-P5 spec 文件头部都加：

```markdown
> **历史档案**：本文档是设计期 spec，不再代表当前实现。代码已经过 m1~m14 多轮迭代，具体行为请以代码和 `docs/architecture.md` 为准。
```

### Step 3: 删 enable_llm

`config/system.yaml`:
- 删 `services.enable_llm: false` 这一行（bootstrap 不读它，已失效）

### Step 4: README 软化

加一段：

```markdown
## 当前限制

- **trade_list → holdings 闭环未自动建立**：trade_list_cohorts 每天生成 BUY/SELL 建议，
  但 holdings 表需要用户在 web `/trading` 页手动校准。系统目前是 advisory only。
- **paper trading 仅做事后跟踪**：不下单实盘。
```

### Step 5: Commit

```bash
git commit -m "docs: N1+N2+N5 — 删过期文档 + 标注 spec 历史 + 删 enable_llm

- 删 docs/next_steps.md (P1 之前写的，60 日动量/行业中性化都早实现)
- 5 份 P1-P5 spec 头部加历史档案标注，避免后来者按 spec 找代码
- 删 config/system.yaml services.enable_llm 失效配置
- README 加 当前限制 段，承认 trade_list → holdings 闭环未建立"
```

---

## Task 10：端到端验证 + 金丝雀回测测试

**Goal**：所有改动后，加一个金丝雀测试锁定 backtester 行为，避免 C3 类问题再发生。

**Files:**
- Test: `tests/portfolio/test_backtester_canary.py`（新建）

### Step 1: 写金丝雀测试

```python
"""C3 canary: 已知 5 日序列收益 +5%，确保 backtester 输出 = +5% 而不是 +56%。"""
from datetime import date
from pathlib import Path

import pandas as pd

from akq_agents.services.portfolio.backtester import (
    BacktestConfig, PortfolioBacktester,
)


def test_backtester_canary_known_return(tmp_path: Path) -> None:
    """5 日单只票，每日涨 1%，最终 nav = 1.05^4 ≈ 1.0406。

    防止 C3 那种 backtester 把 +5% 算成 +56% 的灾难重现。
    """
    # mock close 矩阵: 5 个交易日，1 只票每日涨 1%
    dates = pd.bdate_range('2026-01-01', periods=5)
    close = pd.DataFrame({
        '000001': [10.0, 10.1, 10.201, 10.30301, 10.40604],
        '000300': [3000.0, 3000.0, 3000.0, 3000.0, 3000.0],  # benchmark 不动
    }, index=dates)

    # 第一天 100% 持有 000001
    weights_by_date = {
        '2026-01-01': {'000001': 1.0},
    }

    bt = PortfolioBacktester(
        meta_db_path=tmp_path / "meta.db",
        ohlcv_dir=tmp_path / "parquet",
        cfg=BacktestConfig(commission=0.0, slippage=0.0, benchmark_symbol='000300'),
    )
    nav_df = bt._replay(weights_by_date, close)

    assert len(nav_df) == 5
    final_nav = nav_df.iloc[-1]['nav_net']
    expected = 1.01 ** 4  # 4 个 +1% 日
    assert abs(final_nav - expected) < 0.001, \
        f"金丝雀失败: 期望 nav={expected:.4f}, 实际 {final_nav:.4f}"

    # 单日 return 不应该超过 5%
    max_daily = nav_df['daily_return_net'].abs().max()
    assert max_daily < 0.05, f"单日 |return| {max_daily*100:.1f}% > 5%（不正常）"
```

### Step 2: 跑测试

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/portfolio/test_backtester_canary.py -v
```

### Step 3: 跑全量 + 端到端

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q
```

```bash
./start.sh stop && sleep 2 && ./start.sh up
sleep 5
# /research 页验证 trade_list 是今天日期
curl -sf http://127.0.0.1:8765/api/research/trade-list/today-list | python -m json.tool | head -10
# /ops 健康
curl -sf http://127.0.0.1:8765/api/ops/health | python -m json.tool | head -20
# portfolio_nav 不应有单日 > 15%
sqlite3 data/meta.db "SELECT COUNT(*) FROM portfolio_nav WHERE ABS(daily_return_net) > 0.15"
```

期望 0。

### Step 4: Commit

```bash
git commit -m "test(portfolio): 金丝雀回测锁定 backtester 行为

5 日单只票每日 +1% → 期望 nav = 1.01^4 ≈ 1.0406。
防止 C3 那种 +5% 算成 +56% 的灾难重现。"
```

---

## Self-Review

**Spec coverage**：
- ✅ C1 (Task 4)、C2 (Task 5)、C3 (Task 1)、C4 (Task 2)、C5 (Task 3)
- ✅ I1 (Task 8)、I4 (Task 6)、I5 (Task 7)
- ✅ N1+N2+N5 (Task 9)
- ✅ 金丝雀测试 (Task 10)

**Placeholder scan**：
- Task 7 Step 1 只列了 grep 命令但行号不固定，让 implementer 自己定位。这是合理的 "find then act"，不是 placeholder。
- Task 8 是大重构，步骤偏 high-level —— 实施时必要 implementer 自己 read 代码逐步推进。可接受。

**Type consistency**：
- Task 2 给 paper_trading update_track_perf 加 `lookup` 参数，与 freeze_today_cohort 的 `fallback_lookup` 命名不一致。让 Task 2 implementer 选：
  - 用 `lookup` 简短
  - 还是 `fallback_lookup` 与 freeze 完全对齐（推荐后者，对称更清晰）

---

## 范围里没做的（YAGNI）

- ❌ N3 trade_list 一键执行按钮 → 拆到 m16 单独 plan（涉及 holdings 写入语义）
- ❌ 重构 evaluator forward_returns 计算 → 等 C3 修完后看是否还有问题
- ❌ 删 RiskAgent 后 OptimizerConfig.max_industry_weight 是否要继续保留 → 默认就 0.30，保留无害

---

## 执行顺序建议

按 Critical → Important → Nice-to-have，**强烈建议**：

1. **Session 1（4-5h）**：Task 1, 2, 3 — 修 3 个 Critical bug，立即让数字可信
2. **Session 2（5h）**：Task 4, 5 — 删老库 + 砍 6 agent，代码量 -700 行
3. **Session 3（4-5h）**：Task 6, 7, 9, 10 — 小修 + 文档 + 金丝雀
4. **Session 4（3h）**：Task 8 — I1 进程合并（最重，单独跑避免污染前面）

**绝对不要**一口气把 10 个 task 全跑——中途出问题难定位。
