# P1 数据层硬化 — 设计文档

- 项目：akq-agents
- 阶段：P1（共 P1–P6 六阶段中的第一阶段）
- 日期：2026-06-17
- 状态：待 plan
- 后续阶段（仅作为上下文，本 spec 不涉及实现）：
  - P2 调度守护（盘中密集 + 盘后深算 + 崩溃恢复）
  - P3 多因子组合机（因子注册表 v2、组合优化、归因）
  - P4 LLM Agent 层（AnalystAgent + ChatAgent + ToolUse，走本地 Anthropic 网关）
  - P5 Web 控制台（FastAPI + React + ECharts）
  - P6 工程加固（测试/CI/日志/告警/容器化/文档）

---

## §1 目标与边界

### 目标

把数据层从"5 只样本股 + 13 个内联因子"升级为支撑**全 A 股、24 小时长跑**的数据基础设施。
本阶段交付的对象是后续 P2–P5 都将依赖的"数据底座"。

### 在做什么（P1 范围）

- 全 A 股股票池动态获取与过滤（剔除 ST/退市/上市未满 180 日/停牌）
- 交易日历感知（非交易日自动暂停拉取，Agent 分析继续）
- 行情接入与字段标准化（财务/估值接口预留，实现延后）
- 本地数据缓存（Parquet 分区 + SQLite 索引），增量更新与限频保护
- 数据质量检查（缺失值、异常值、单调性）与异常上报
- `FactorAgent` 改造为"通过 Repository 读数据"，不再直接调 AKShare

### 不在做什么（明确越界）

- ❌ 新因子（P3）
- ❌ 调度（P2）
- ❌ LLM Agent（P4）
- ❌ Web 控制台（P5）
- ❌ 分钟级数据（未来某 P）
- ❌ 多市场（港股/美股）
- ❌ 数据回放/replay 引擎
- ❌ 财务/估值数据的真实拉取（schemas/接口在 P1 预留，实际拉取实现延后到 P1.5 或并入 P3）

---

## §2 数据层架构

### 分层结构

```
┌──────────────────────────────────────────────────────────────┐
│  上游数据源（AKShare）                                          │
│  - stock_zh_a_spot_em（全市场行情快照）                          │
│  - stock_zh_a_hist（个股日线）                                  │
│  - tool_trade_date_hist_sina（交易日历）                        │
│  - stock_zh_a_st_em（ST 列表）                                  │
│  - stock_individual_info_em（停牌/上市日期）                    │
│  - stock_financial_em（财务指标，按需，P1 不实现）                │
└────────────────────┬─────────────────────────────────────────┘
                     │ httpx + 限频 + 重试 + 异常分类
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  AKShareGateway（services/data/akshare_gateway.py）             │
│  - 单一出口、统一限频（QPS 配置驱动）                              │
│  - 重试策略（指数退避，最多 3 次）                                 │
│  - 错误分类：网络/字段缺失/接口变更/限流                            │
│  - 字段映射层（隔离 AKShare 列名变化）                             │
└────────────────────┬─────────────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  DataRepository（services/data/repository.py）                  │
│  - 标准化：pydantic schema                                       │
│  - 缓存写入：Parquet 按 (table, date) 分区                       │
│  - 索引维护：SQLite 元数据表                                      │
│  - 增量识别：基于 last_updated + 交易日历                          │
└────────────────────┬─────────────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  UniverseManager（services/data/universe.py）                  │
│  - 全 A 股动态股票池                                              │
│  - 过滤器链：ST/退市/上市未满180日/停牌/价格异常                    │
│  - 输出"今日可交易股票池"快照（带原因码）                          │
└────────────────────┬─────────────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  对外接口（被 FactorAgent、BacktestAgent 调用）                   │
│  - get_universe(date) → UniverseSnapshot                        │
│  - get_ohlcv(symbols, start, end) → DataFrame                   │
│  - is_trading_day(date) → bool                                  │
│  - quality_report() → DataHealth                                │
└──────────────────────────────────────────────────────────────┘
```

### 存储布局

```
data/
├── parquet/
│   ├── ohlcv/                    # 日线，按日分区
│   │   ├── date=2026-06-17/part.parquet
│   │   └── date=2026-06-16/...
│   ├── financials/               # 财务（P1 不写入，目录占位）
│   │   └── report_date=2026Q1/
│   ├── universe/                 # 每日股票池快照
│   │   └── date=2026-06-17/
│   └── trading_calendar.parquet
├── meta.db                        # SQLite，存数据状态/拉取记录/质量异常
└── cache/                         # 临时缓存（限频窗口、原始响应）
```

**选型理由**：

- Parquet：列存、压缩比高、Pandas/Polars 原生读、按日分区只读需要的天。
- SQLite：存"做过什么"的元数据；嵌入、轻、ACID；不存大表。
- 不引入 DuckDB / Clickhouse / Polars / Dask：单机够用，YAGNI。

### 关键设计点

1. **限频**：`AKShareGateway` 内置 token bucket，默认 5 QPS，可配置。所有 AKShare 调用必须走 Gateway。
2. **增量原则**：日线"今日收盘后只拉今日"，启动时若发现历史缺失才补齐。
3. **质量门**：每次拉取后做 3 项检查 — 行数 ≥ `min_universe_size`、关键字段非空率 > 99%、收盘价 0.5–2000；任一失败标记"脏数据"且不进缓存。
4. **错误恢复**：Gateway 层捕获所有 AKShare 异常 → 标准化为 `FetchError(symbol, reason_code)` → 写 `meta.db.fetch_errors` → 调用方决定重试或跳过。
5. **路径修复**：`bootstrap.py` 及所有引用清除 `/Users/fengbojing1/Documents/A/...` 硬编码，统一使用 `BASE_DIR` 或配置。

### 配置示例（`config/data.yaml`，新增）

```yaml
data:
  base_dir: "./data"
  universe:
    market: cn
    include_st: false
    include_new: false
    min_listing_days: 180
  akshare:
    qps: 5
    max_retries: 3
    timeout_s: 30
  cache:
    ohlcv_lookback_days: 250
    financials_lookback_quarters: 8
  quality:
    min_universe_size: 4000
    max_null_rate: 0.01
```

---

## §3 数据流与时序

### 流程 1：每日盘后增量更新（17:00 触发；调度具体形式在 P2）

```
17:00 触发
  → TradingCalendar.is_trading_day()  ── 非交易日 → 跳过、记日志
  → UniverseManager.refresh()         拉全市场快照 → 应用过滤器链 → 写 universe 分区
  → Repository.fetch_ohlcv_incremental
       - 比对 meta.db 已拉记录
       - 仅拉今日缺失标的，限频 5 QPS
       - 失败入 retry_queue
  → QualityGate.check_daily()
       - 行数 ≥ min_universe_size
       - 关键字段 null 率 < 0.01
       - 异常写 meta.db.data_quality_log
  → 通过 → 标记 fetch_log.status=ok，触发事件 data_ready_for_date
```

### 流程 2：盘中按需读取（FactorAgent 调用）

```
FactorAgent.run()
  → repo.get_ohlcv(symbols, start, end)
     - Parquet 命中 → 直接返回 DataFrame
     - 未命中且在拉取队列 → 返回部分结果或抛 DataNotReady（视策略）
     - 未命中且不在队列 → 触发后台 fill_gap 任务 + 抛 DataNotReady
       （FactorAgent 收到后跳过本轮，下次循环再算）
```

**关键约束**：**Agent 只读不拉**。所有 AKShare 请求由后台增量任务发起，避免盘中卡顿与限频冲突。

### 流程 3：冷启动（首次部署 / 数据完全缺失）

```
cli: akq-agents data bootstrap
  → 拉交易日历（近 5 年）
  → 拉全市场股票列表 + 过滤
  → 按日倒序回填近 250 个交易日
       - 限频 5 QPS
       - 进度条 + 断点续传
       - 失败写 retry_queue
```

预估：全 A 股 ~5300 只 × 250 天，按 5 QPS 计算约 4–6 小时一次性回填。

### 流程 4：错误与重试

```
AKShareGateway.fetch(symbol)
  HTTP 200 + 字段正常 → 返回
  HTTP 429 / 超时   → 指数退避重试 ≤ 3 次 → 仍失败 → FetchError(RATE_LIMITED)
  字段缺失 / 变更   → FetchError(SCHEMA_DRIFT)
  其他异常          → FetchError(UNKNOWN)
  调用方 → 写 meta.db.fetch_errors → 入 retry_queue → 下个周期 RetryWorker 处理
       连续失败 3 个调度周期 → 升级告警（告警通道在 P6 落地）
```

`meta.db.fetch_errors` 表结构：

```sql
CREATE TABLE fetch_errors (
  id INTEGER PRIMARY KEY,
  ts TEXT,
  symbol TEXT,
  endpoint TEXT,
  reason_code TEXT,    -- RATE_LIMITED / SCHEMA_DRIFT / NETWORK / UNKNOWN
  message TEXT,
  retry_count INTEGER,
  resolved INTEGER DEFAULT 0
);
```

### 流程 5：数据健康检查（CLI / Web 调用）

```python
repo.quality_report() → DataHealth {
  "last_full_refresh": "2026-06-17T17:03:21",
  "universe_size_today": 5234,
  "ohlcv_coverage_today": 0.998,
  "financials_freshness_days": 12,
  "pending_retries": 23,
  "unresolved_errors_24h": 3,
  "health": "OK" | "DEGRADED" | "FAILED"
}
```

---

## §4 模块与接口

### 文件清单与职责

```
src/akq_agents/
├── services/
│   ├── data/                           ← 新增子包
│   │   ├── __init__.py                 # 对外只导出 Repository、UniverseManager
│   │   ├── akshare_gateway.py          # AKShare 单一出口 + 限频 + 重试 + 字段映射
│   │   ├── repository.py               # 缓存读写 + 增量识别
│   │   ├── universe.py                 # 股票池 + 过滤器链
│   │   ├── calendar.py                 # 交易日历
│   │   ├── quality.py                  # QualityGate
│   │   ├── retry_worker.py             # 失败重试后台 worker
│   │   ├── exceptions.py               # FetchError / DataNotReady / QualityCheckFailed
│   │   └── schemas.py                  # pydantic 标准化 schema
│   ├── akshare_service.py              ← 标记 deprecated，仅保留 mock 供过渡
│   ├── backtest_service.py             # P1 不动
│   └── ...
├── models/
│   └── data_config.py                  ← 新增：DataConfig (pydantic)
└── cli/
    └── app.py                          ← 新增子命令 data:
                                          - data bootstrap
                                          - data refresh
                                          - data status
                                          - data inspect <symbol>
```

### Schema（services/data/schemas.py）

```python
class OHLCVBar(BaseModel):
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    turnover: float | None

class UniverseSnapshot(BaseModel):
    date: date
    symbols: list[str]
    excluded: dict[str, str]   # symbol → reason_code

class DataHealth(BaseModel):
    last_full_refresh: datetime | None
    universe_size_today: int
    ohlcv_coverage_today: float
    financials_freshness_days: int
    pending_retries: int
    unresolved_errors_24h: int
    health: Literal["OK", "DEGRADED", "FAILED"]
```

### Gateway 接口

```python
class AKShareGateway:
    def __init__(self, qps: int, timeout_s: int, max_retries: int): ...
    def fetch_spot(self) -> pd.DataFrame: ...
    def fetch_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...
    def fetch_trading_dates(self) -> list[date]: ...
    def fetch_st_list(self) -> list[str]: ...
    def fetch_individual_info(self, symbol: str) -> dict: ...
    # 异常：FetchError(symbol, reason_code, message)
```

### Repository 接口

```python
class DataRepository:
    def __init__(self, config: DataConfig, gateway: AKShareGateway): ...

    # 读：只读缓存，不触发拉取
    def get_ohlcv(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        """缺数据时抛 DataNotReady，由调用方决定跳过或等待"""
    def get_universe(self, d: date) -> UniverseSnapshot: ...
    def is_trading_day(self, d: date) -> bool: ...

    # 写：增量/全量（由调度器或 CLI 调用，Agent 不直接调）
    def refresh_daily(self, d: date) -> RefreshResult: ...
    def bootstrap_history(self, lookback_days: int, progress_cb=None) -> None: ...

    # 监控
    def quality_report(self) -> DataHealth: ...
    def pending_retries(self) -> int: ...
```

### UniverseManager 接口

```python
class UniverseManager:
    def __init__(self, gateway: AKShareGateway, filters: list[UniverseFilter]): ...
    def build_snapshot(self, d: date) -> UniverseSnapshot: ...

class UniverseFilter(Protocol):
    name: str
    reason_code: str
    def keep(self, symbol_info: dict) -> bool: ...

# 内置过滤器
class STFilter: ...                     # ST / *ST / 退市风险
class ListingAgeFilter: ...             # 上市天数 < min_listing_days
class SuspendedFilter: ...              # 停牌
class PriceRangeFilter: ...             # 价格 < 1 或 > 1000
```

### 异常分类（services/data/exceptions.py）

```python
class DataError(Exception): ...

class FetchError(DataError):
    reason_code: Literal["RATE_LIMITED", "SCHEMA_DRIFT", "NETWORK", "UNKNOWN"]
    symbol: str | None

class DataNotReady(DataError):
    missing: dict[str, list[date]]

class QualityCheckFailed(DataError):
    checks: dict[str, bool]
```

### Agent 侧典型调用

```python
class FactorAgent(BaseAgent):
    def __init__(self, repo: DataRepository, factor_library: FactorLibrary):
        self.repo = repo
        self.factors = factor_library

    def run(self, context: AgentContext) -> dict:
        today = context.state["today"]
        try:
            universe = self.repo.get_universe(today)
            df = self.repo.get_ohlcv(universe.symbols, today - 250d, today)
        except DataNotReady as e:
            logger.warning(f"data not ready, skip: {e.missing}")
            return {"status": "skipped", "reason": "data_not_ready"}

        scores = self.factors.compute_all(df)
        context.state["factor_scores"] = scores.to_dict()
        return {"status": "ok", "rows": len(scores)}
```

### 关键边界

- ❌ 不缓存盘中实时分钟级行情。
- ❌ Repository 不知道因子/回测，只管数据。
- ❌ AKShareGateway 不做业务逻辑（无过滤、无标准化），只做"拉 + 限频 + 重试 + 字段映射"。
- ❌ 不引入 DuckDB / Polars / Dask。
- ❌ 财务/估值真实拉取延后；P1 只在 schema 与目录占位。

### 测试策略（tests/data/）

```
tests/data/
├── test_akshare_gateway.py      # mock AKShare 响应；验证限频、重试、错误分类
├── test_repository.py            # tmp dir + 假 Parquet；增量/缺数据/健康
├── test_universe_filters.py      # 每个过滤器独立单测
├── test_calendar.py              # 交易日历边界
└── fixtures/
    ├── akshare_spot_sample.csv
    └── akshare_hist_sample.csv
```

目标覆盖率：**P1 新增的 `services/data/` 子包 ≥ 80%**（不强求其他层）。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents data bootstrap` 跑通全 A 股 250 日回填 | `data/parquet/ohlcv/` 存在 250 个 `date=` 分区，每分区行数 ≥ 4000 |
| A2 | `akq-agents data refresh` 第二次跑零增量 | 日志显示 "0 new fetched, 0 retried" |
| A3 | `akq-agents data status` 输出结构化 health JSON | JSON 完整包含 `DataHealth` 字段；`health` ∈ {OK, DEGRADED, FAILED} |
| A4 | ST/上市未满 180 日/停牌不出现在 `get_universe()` 结果 | 抽样验证 3 只已知 ST 股不在结果中 |
| A5 | 非交易日调 `refresh_daily` 立即返回 `skipped` | mock 时间为周日测试 |
| A6 | `FactorAgent` 改造后通过 Repository 读数据，缺数据返回 `skipped` 不报错 | 集成测试：删除一天 parquet，跑 FactorAgent → skipped |
| A7 | 路径硬编码全部清除：`grep -rn "/Users/fengbojing1/Documents/A"` 命中数为 0 | 命令直接验证 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/data/` 覆盖率 ≥ 80% | `pytest --cov=akq_agents.services.data --cov-report=term-missing` |
| B2 | `ruff check src/ tests/` 零警告 | CI/本地命令 |
| B3 | 关键接口含 docstring + 类型标注 | 人工 review |
| B4 | `.gitignore` 完整：含 `__pycache__/`, `*.pyc`, `.DS_Store`, `data/`, `*.db`, `runtime_state.yaml`, `reports/`, `.omc/state/` 等 | 文件检查 |
| B5 | git 仓库已初始化首个 commit；分支模型 `main` + feature branch | `git log --oneline` 有提交 |

### C. 性能验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | 冷启动回填全 A 股 250 日 ≤ 6 小时 | 实测计时 |
| C2 | 增量刷新（仅当日）≤ 30 分钟 | 实测计时 |
| C3 | `get_ohlcv(全市场, 近 60 日)` 内存占用 ≤ 1GB | `memory_profiler` 抽测 |
| C4 | 单次 AKShare 调用平均 ≤ 5 QPS（不被对方限流） | `meta.db.fetch_log` 时间戳统计 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/data_layer.md` 记录：数据源、字段映射、缓存布局、过滤规则、故障排查 |
| D2 | `README.md` 更新：bootstrap / refresh / status 三条命令的快速上手 |
| D3 | 新增模块顶部含模块 docstring 说明职责 |

### 里程碑参考（细化进 plan 阶段）

- M1.1 骨架就位（1–2 天）：data/ 子包、schemas、异常、配置、空实现
- M1.2 Gateway + 限频（1–2 天）：实现、单测、字段映射层
- M1.3 Repository + 缓存（2–3 天）：Parquet 读写、meta.db、增量识别
- M1.4 Universe + Calendar + Quality（1–2 天）：过滤器链、交易日历、质量门
- M1.5 RetryWorker + CLI（1 天）：retry worker、`data` 子命令、健康报告
- M1.6 集成 + FactorAgent 改造（1 天）：替换现有 akshare_service，端到端
- M1.7 文档 + git 初始化 + 测试覆盖收尾（1 天）

**预估总工时：8–12 工作日。**

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| AKShare 字段悄变 | 大面积失败 | 字段映射层 + `SCHEMA_DRIFT` 异常 + 启动 smoke test |
| 限流频发 | 冷启动超 6 小时 | 降到 3 QPS + 重试 + 断点续传 |
| 全市场 DataFrame OOM | 进程崩 | 按需切片 + `lookback_days` 上限 |
| Parquet 碎片化 | 读放大 | 月级 compaction（P2 处理） |
| 历史数据某段缺失 | 因子 NaN | QualityGate 显式标记 + Agent 跳过该日 |

### 越界声明（明确不在 P1 内）

- 因子算法迭代（P3）
- 分钟级数据
- 多市场（港股/美股）
- 数据可视化（P5）
- 数据回放/replay 引擎
- 财务/估值数据**真实**拉取（接口预留，实现延后到 P1.5 或并入 P3）

---

## 附录 A：与现有代码的差异（用于 plan 阶段对照）

| 现状 | P1 后 |
|---|---|
| `services/akshare_service.py` 直接被 Agent 调用 | 由 `services/data/` 子包接管；旧文件标记 deprecated，仅保留 mock |
| 5 只硬编码股票池 | UniverseManager 动态生成全 A 股股票池 |
| 无交易日历感知 | TradingCalendar 强制护栏 |
| 无数据缓存，每次重新计算 | Parquet 分区 + meta.db 增量 |
| 路径硬编码 `/Users/fengbojing1/Documents/A/...` | 统一 `BASE_DIR` / 配置 |
| 无错误重试 | 指数退避 + retry_queue + 升级告警 |
| 无质量门 | QualityGate 强制校验 |
| 无测试 | `tests/data/` 覆盖率 ≥ 80% |
| 无 `.gitignore`，0 commit | `.gitignore` 完整、首个 commit、main 分支 |

## 附录 B：与后续阶段的接口承诺

P2/P3/P4/P5 将依赖以下契约（P1 必须保证）：

1. `DataRepository.get_ohlcv` 是**幂等只读**的。
2. `DataRepository.refresh_daily` 必须可在外部调度器（P2）安全并发调用，单日重复调用为幂等。
3. `DataHealth` schema 是**稳定的**：P5 Web 控制台将直接渲染该 schema。
4. `UniverseSnapshot` 包含 `excluded` 字段，Web 控制台可展示每只股票被排除的原因。
5. `meta.db.fetch_errors` 表结构稳定，P6 告警系统将读取该表。
