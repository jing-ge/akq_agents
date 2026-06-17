# P3 多因子组合机 — 设计文档

- 项目：akq-agents
- 阶段：P3（共 P1–P6 六阶段中的第三阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（DataRepository / UniverseSnapshot / OHLCV）、P2（JobRunner / batch.deep_research / events）

---

## §1 目标与边界

### 目标

把当前"9 个内联因子 + 简单加权"的玩具版升级为一个**可注册、可回测、可组合、可归因**的多因子组合机：

- 因子注册表 v2：每个因子一个类，声明输入需求与计算窗口；新增因子无需改 workflow。
- 横截面标准化 + 行业中性化：避免市值/行业过度暴露。
- 滚动 IC / IR 评估：每周（盘后 deep_research）滚动评估因子的预测力，自动失能表现退化的因子。
- 组合优化：从加权打分升级到带约束的均值-方差或风险平价（二选一，优先 MV）。
- 归因报告：日报里能说清"今天为什么选这些股 → 哪几个因子贡献了多少分"。

### 在做什么（P3 范围）

- 抽象 `Factor` 协议 + `FactorRegistry`（注册、查询、版本号）。
- 实现一组生产级因子：动量 5/20/60、反转 5、波动率 20、流动性、ROE、PB/PE 分位、5 日成交额、规模。
- `Preprocessor`：去极值（MAD）、Z-Score 标准化、行业中性化（GICS 一级或自定义 sector）。
- `FactorEvaluator`：滚动 IC（rolling 60d）、IR、衰减曲线、t 统计；写 `factor_metrics` 表。
- `PortfolioOptimizer`：mean-variance with constraints（权重上下限、行业暴露上限、turnover 上限）；fallback 到 inverse-vol 等权。
- `Attributor`：组合权重 → 因子贡献分解；写入日报 payload，可被 P5 渲染。
- 新增 job：`batch.deep_research`（P2 已占位），周末跑因子有效性滚动评估。
- CLI：`akq-agents factors list | inspect <name> | metrics --window 60`、`akq-agents portfolio explain --date YYYY-MM-DD`。

### 不在做什么

- ❌ 机器学习因子（XGBoost / Lasso 选因子）— 留给 P3.5 或 P4。
- ❌ 多空对冲（仅做多 + 现金）。
- ❌ 期权 / 期货 / ETF 组合 — 仅 A 股股票。
- ❌ 实时风控盯盘（盘中只读快照）。
- ❌ 调仓执行（仅输出目标仓位）。
- ❌ 自动调参 / 超参搜索。

---

## §2 架构

### 分层结构

```
┌────────────────────────────────────────────────────────────────┐
│  DataRepository (P1)                                            │
└────────┬────────────────────────────────────────────────────────┘
         │ get_ohlcv / get_universe (P1 接口承诺，幂等只读)
         ▼
┌────────────────────────────────────────────────────────────────┐
│  FactorRegistry                                                  │
│  - register(factor: Factor)                                      │
│  - get(name) / list_active() / list_all()                        │
│  - 注册自动校验：name 唯一、声明 lookback_days、声明 inputs       │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  FactorEngine.compute(universe, date, factors)                  │
│  - 拉数据（按 union(lookback)）                                  │
│  - 并行计算每个 factor（threadpool；CPU bound 用 numpy）         │
│  - 返回 wide DataFrame: index=symbol, columns=factor_name        │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  Preprocessor                                                   │
│  - winsorize (MAD ±3)                                            │
│  - z-score 横截面                                                │
│  - neutralize by industry / cap                                  │
│  - 输出：标准化后的因子 DataFrame                                 │
└────────┬────────────────────────────────────────────────────────┘
         ├──► FactorEvaluator (滚动 IC / IR)                       │
         │       └── writes meta.db.factor_metrics                  │
         ▼
┌────────────────────────────────────────────────────────────────┐
│  CompositeScorer                                                │
│  - 权重来源：factor_metrics（IR 加权）or 配置硬权重               │
│  - 失能机制：metrics 表 status='inactive' 的因子权重置 0          │
│  - 输出：每只股票的综合分（pd.Series）                            │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  PortfolioOptimizer                                             │
│  - 约束：min/max single weight, 行业上限, turnover 上限           │
│  - 主算法：mean-variance with covariance shrinkage               │
│  - Fallback：inverse-vol top-N 等权                              │
│  - 输出：target_weights (DataFrame: symbol, weight, prev_weight)  │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  Attributor                                                     │
│  - 给定 target_weights + factor_exposure → 每个因子贡献 (PnL or score) │
│  - 输出 attribution.json，写入 reports/                          │
└────────────────────────────────────────────────────────────────┘
```

### 存储扩展（追加到 P1 的 `meta.db`）

```sql
CREATE TABLE IF NOT EXISTS factor_metrics (
  id INTEGER PRIMARY KEY,
  factor_name TEXT NOT NULL,
  as_of_date TEXT NOT NULL,        -- 计算这次 metric 时的截止日期
  window_days INTEGER NOT NULL,    -- 60d / 120d / 250d
  ic_mean REAL,
  ic_std REAL,
  ir REAL,                          -- ic_mean / ic_std
  t_stat REAL,
  decay_2 REAL,                     -- IC at lag-2
  decay_5 REAL,                     -- IC at lag-5
  decay_10 REAL,
  status TEXT NOT NULL,             -- active | watch | inactive
  reason TEXT,                      -- 失能原因
  UNIQUE(factor_name, as_of_date, window_days)
);

CREATE INDEX IF NOT EXISTS idx_factor_metrics_active
  ON factor_metrics(status, as_of_date);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  id INTEGER PRIMARY KEY,
  as_of_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  weight REAL NOT NULL,             -- 目标仓位
  prev_weight REAL,                 -- 上一交易日仓位（用于 turnover）
  composite_score REAL,
  contribution_json TEXT,           -- 各因子贡献 dict
  UNIQUE(as_of_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_date ON portfolio_snapshots(as_of_date);
```

### 行业映射数据

- AKShare 提供 `stock_board_industry_name_em` + `stock_board_industry_cons_em`（一级行业大约 100 个，可聚到 28 个申万一级）。
- P3 内部只用 28 个一级行业；缺数据 fail-closed（视为单独行业，避免影响其他股的中性化）。
- 行业映射缓存到 `data/parquet/industry_map/date=YYYY-MM-DD/part.parquet`（每周更新一次，足够）。

### 配置示例（`config/portfolio.yaml`，新增）

```yaml
portfolio:
  preprocessing:
    winsorize_mad_k: 3.0
    zscore: true
    neutralize_by: ["industry", "log_market_cap"]
  evaluation:
    rolling_windows: [60, 120]
    decay_lags: [2, 5, 10]
    deactivate_if:
      ir_below: 0.1
      consecutive_periods: 4    # 连续 4 次 rolling 都低于 → inactive
  composite:
    weighting: "ir"             # ir | equal | config
    min_active_factors: 3       # 不足 3 个 active factor 时退化为 equal weight
  optimizer:
    method: "mean_variance"     # mean_variance | inverse_vol
    risk_aversion: 5.0
    max_single_weight: 0.05
    min_single_weight: 0.0
    max_industry_weight: 0.30
    max_turnover: 0.40          # 单日换手上限
    cov_estimation: "ledoit_wolf"
  attribution:
    enable: true
    top_k_contributors: 5
```

---

## §3 数据流与时序

### 流程 1：盘后 batch.post_close（每个交易日）

```
P2 batch.post_close
  → DataRepository.refresh_daily(today)   # P1
  → FactorEngine.compute(universe, today, registry.list_active())
       - 拉每个 factor 所需 lookback（取 max）
       - 并行计算因子值
  → Preprocessor.transform(factor_df, industry_map)
  → CompositeScorer.score(processed_df)
       - 权重来自最近一次 factor_metrics（active 因子按 IR 归一）
  → PortfolioOptimizer.solve(score, cov, prev_weights, constraints)
  → Attributor.explain(weights, factor_exposure)
  → 写入：
      - meta.db.portfolio_snapshots
      - reports/YYYY-MM-DD/attribution.json
      - context.state["portfolio"] / ["attribution"]
  → events(kind="portfolio.ready", payload=summary)
```

### 流程 2：周末 batch.deep_research（每周日 22:00）

```
P2 batch.deep_research
  → 对 registry.list_all() 每个因子：
       - 拉过去 250d OHLCV + 因子值历史
       - 算 rolling IC / IR / t-stat / decay
       - 与配置阈值比较 → 决定 status (active / watch / inactive)
  → 写 meta.db.factor_metrics（追加一行 per factor）
  → 触发因子激活/失能变更 → events(kind="factor.deactivated", payload=...)
```

**关键约束**：
- deep_research 跑得慢没关系（不影响盘后日报，weights 用上一次 metrics）。
- 每次写一行，永不 update；用 `as_of_date` 区分版本；CompositeScorer 读 `MAX(as_of_date)`。

### 流程 3：归因报告（被 AdvisorAgent / ReportAgent 调用）

```
Attributor.explain(weights, factor_exposure):
  # weights: Series, factor_exposure: DataFrame [symbol x factor]
  # 每只股票的"得分贡献"按因子分解（z-score × ir_weight）
  per_stock = (factor_exposure * factor_weights).sum(axis=1)
  per_factor = (factor_exposure.T @ weights) * factor_weights
  return {
    "as_of_date": ...,
    "factor_contribution": per_factor.to_dict(),
    "top_picks": [{symbol, score, top_factors: [...]} ...]
  }
```

### 流程 4：CLI 检视

```
akq-agents factors list
  → 表格：name | status | last_ic | last_ir | last_evaluated

akq-agents factors inspect momentum_20 --window 60
  → 历史 IC 序列 / IR 走势（文本 sparkline）

akq-agents portfolio explain --date 2026-06-17
  → 读 portfolio_snapshots + attribution.json
  → 文本输出："今日组合 N 只，行业暴露 top3 ...，单因子贡献 top5 ..."
```

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── services/
│   ├── factor_service.py              ← 重写：薄壳，只是 FactorRegistry 出口
│   ├── factors/                        ← 新增子包
│   │   ├── __init__.py
│   │   ├── base.py                     # Factor protocol + FactorRegistry
│   │   ├── momentum.py                 # Momentum5/20/60
│   │   ├── reversal.py                 # Reversal5
│   │   ├── volatility.py               # Volatility20
│   │   ├── liquidity.py                # Turnover5, AmountRank
│   │   ├── value.py                    # PB / PE 分位 (依赖 P3.5 财务数据，先写空壳 + 报 missing_data)
│   │   ├── quality.py                  # ROE / 毛利率（同上）
│   │   ├── size.py                     # log_market_cap
│   │   └── _engine.py                  # FactorEngine: 并行计算
│   ├── portfolio/
│   │   ├── __init__.py
│   │   ├── preprocessor.py
│   │   ├── evaluator.py
│   │   ├── composite.py
│   │   ├── optimizer.py
│   │   ├── attributor.py
│   │   └── industry_map.py
│   └── ...
├── models/
│   └── portfolio_config.py             ← PortfolioConfig (pydantic)
├── orchestrator/
│   └── jobs/
│       ├── batch_post_close.py         ← 注入 portfolio pipeline
│       └── batch_deep_research.py      ← P2 占位 → P3 实现
└── cli/
    └── app.py                          ← 新增子命令：factors / portfolio
```

### Factor 协议

```python
class Factor(Protocol):
    name: str                       # 全局唯一
    version: int                    # 实现版本，>= 1
    inputs: list[Literal["ohlcv", "industry", "financials"]]
    lookback_days: int              # 计算需要的 OHLCV 回溯天数
    direction: Literal["long", "short"]   # 数值越大越好(long) 还是越小越好(short)

    def compute(self, ohlcv: pd.DataFrame, *,
                industry_map: pd.Series | None = None,
                financials: pd.DataFrame | None = None) -> pd.Series:
        """返回 index=symbol, values=因子原始值。允许 NaN（缺数据），
        下游 Preprocessor 会处理。"""

class FactorRegistry:
    def register(self, factor: Factor) -> None: ...
    def get(self, name: str) -> Factor: ...
    def list_all(self) -> list[Factor]: ...
    def list_active(self, as_of_date: date) -> list[Factor]:
        """读 factor_metrics 最近一次 status='active' 的子集"""
```

### Preprocessor 接口

```python
class Preprocessor:
    def __init__(self, cfg: PreprocessingConfig): ...
    def transform(self, factor_df: pd.DataFrame,
                  industry: pd.Series | None,
                  log_market_cap: pd.Series | None) -> pd.DataFrame:
        """对每个因子列：去极值 → 行业 + 市值回归取残差 → z-score"""
```

### PortfolioOptimizer 接口

```python
class PortfolioOptimizer:
    def __init__(self, cfg: OptimizerConfig): ...
    def solve(self,
              composite_score: pd.Series,
              cov: pd.DataFrame,
              prev_weights: pd.Series | None,
              industry: pd.Series | None) -> pd.Series:
        """返回 target_weights，index=symbol, sum<=1.
        约束求解失败 → 退化为 inverse_vol top-N 等权"""

# 实现用 cvxpy（约束 QP）；fallback 不依赖外部求解器
```

### Attributor 接口

```python
class Attributor:
    def explain(self,
                weights: pd.Series,
                factor_exposure: pd.DataFrame,
                factor_weights: pd.Series) -> dict:
        """返回 §3 流程 3 描述的 attribution dict"""
```

### Agent 集成（仅 1 处改动）

```python
# agents/portfolio_agent.py（替换现有的简单加权逻辑）
class PortfolioAgent(BaseAgent):
    def __init__(self, services: dict, top_n_symbols: int): ...
    def run(self, context):
        registry = services["factor_registry"]
        engine   = services["factor_engine"]
        prep     = services["preprocessor"]
        scorer   = services["composite_scorer"]
        opt      = services["portfolio_optimizer"]
        attr     = services["attributor"]
        repo     = services["data_repository"]

        today = context.state["today"]
        universe = repo.get_universe(today).symbols
        raw = engine.compute(universe, today, registry.list_active(today))
        pre = prep.transform(raw, ...)
        comp = scorer.score(pre)
        weights = opt.solve(comp, ...)
        attribution = attr.explain(weights, pre, scorer.weights())
        context.state["portfolio"] = weights_to_records(weights)
        context.state["attribution"] = attribution
        return {"status": "ok", "n": len(weights)}
```

### 关键边界

- ❌ Factor 内部不读 sqlite / 不写文件；只接收输入、返回 Series。
- ❌ Optimizer 不知道因子；只接收 score + cov + constraints。
- ❌ Attributor 不读 db；只做矩阵运算。
- ❌ 财务因子（value/quality）在 P3 仅占位（无财务数据 → 返回全 NaN + 记 events `factor.missing_data`），实际拉取等 P1.5。

### 测试策略（`tests/portfolio/` + `tests/factors/`）

```
tests/factors/
├── test_registry.py              # 注册、唯一性、active 过滤
├── test_momentum.py / test_volatility.py / ...   # 每个 factor 独立 fixture 校验
└── test_engine.py                # 并行计算正确性、缺数据 NaN 处理

tests/portfolio/
├── test_preprocessor.py          # winsorize / zscore / neutralize 数值校验
├── test_evaluator.py             # rolling IC 计算 + 失能判定
├── test_composite.py             # IR 加权、失能后退化路径
├── test_optimizer.py             # 约束满足、cvxpy 求解失败 → fallback
├── test_attributor.py            # 贡献分解之和 == 综合分
└── fixtures/                     # 小规模 50 标的样本数据
```

目标覆盖率：**`services/factors/` + `services/portfolio/` ≥ 80%**。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | 新增 1 个 factor 只改 1 个文件 + 1 行注册 | 演示新增 `amihud_illiquidity` |
| A2 | `factor_metrics` 表在跑过 1 次 deep_research 后有 N (#factors) 行 | sqlite 查询 |
| A3 | 强制把某因子 IR 设为 -1（mock），下次 deep_research 后该因子 `status='inactive'` | 集成测 |
| A4 | 失能因子在 CompositeScorer 中权重为 0 | 单测 |
| A5 | Optimizer 输出满足所有约束：单股权重 ≤ max、行业权重 ≤ max、turnover ≤ max | 单测 + 端到端 |
| A6 | Optimizer 求解 infeasible 时退化到 inverse_vol，不抛异常 | 单测（构造不可解约束） |
| A7 | `attribution.factor_contribution` 求和 ≈ portfolio 综合分（误差 < 1e-6） | 单测 |
| A8 | `akq-agents portfolio explain --date <today>` 输出可读归因报告 | CLI 验证 |
| A9 | 财务因子缺数据时整个 pipeline 不崩，记 events 并继续 | 集成测 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/factors/` + `tests/portfolio/` 覆盖率 ≥ 80% | `pytest --cov` |
| B2 | `ruff check` 零警告 | CI |
| B3 | 每个 factor 顶部 docstring 含公式 + 引用 | review |
| B4 | `portfolio_snapshots` / `factor_metrics` 表 DDL 写入文档 | docs/portfolio.md |

### C. 性能验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | FactorEngine 计算 4000 标的 × 10 factor × 60d lookback ≤ 60s | 实测 |
| C2 | Optimizer 4000 标的 MV 求解 ≤ 30s（cvxpy ECOS） | 实测 |
| C3 | Preprocessor 整套 ≤ 10s | 实测 |
| C4 | 端到端 batch.post_close（含 portfolio pipeline）≤ 90 分钟 | events 表 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/portfolio.md`：架构、因子列表、配置项、归因报告示例、故障排查 |
| D2 | `docs/factor_registry.md`：如何新增一个因子（含模板） |
| D3 | README 增加 `factors list` / `portfolio explain` 命令示例 |

### 里程碑参考

- M3.1 FactorRegistry + 协议 + 5 个价格类因子（2 天）
- M3.2 Preprocessor + IndustryMap（1–2 天）
- M3.3 FactorEvaluator + factor_metrics 表（1 天）
- M3.4 CompositeScorer + PortfolioOptimizer (cvxpy + fallback)（2 天）
- M3.5 Attributor + ReportAgent 集成（1 天）
- M3.6 接入 batch.deep_research（P2 占位 job）+ CLI（1 天）
- M3.7 端到端联调 + 性能调优（1–2 天）

**预估总工时：9–12 工作日。**

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| cvxpy 在大规模问题求解慢 / 失败 | 组合无解 | fallback inverse_vol；可选 ECOS/OSQP；problem size 时降权 universe |
| 财务数据缺失 | value/quality 因子常空 | P3 先标 inactive；等 P1.5 拉财务后再激活 |
| 行业分类口径不一致 | 中性化偏差 | 固定到申万一级 28 个；缺映射的股入 'other' bucket |
| IR 评估窗口短期 noise 大 | 频繁失能/激活 | `consecutive_periods` 平滑 + watch 状态做缓冲 |
| 协方差矩阵不正定 | MV 无解 | Ledoit-Wolf shrinkage + nearest-PSD 投影 fallback |
| Factor 实现版本升级影响历史回测 | 归因可重复性 | `version` 字段写进 `factor_metrics`，metrics 版本绑定因子版本 |

### 越界声明

- ❌ ML / 神经网络因子
- ❌ 多空 / 杠杆 / 衍生品
- ❌ 自动调参
- ❌ 实时风控盯盘（盘中只读快照）
- ❌ 调仓执行（仅给目标仓位）

---

## 附录 A：与 P1/P2 依赖契约

P3 依赖：
1. `DataRepository.get_ohlcv / get_universe` 幂等只读（P1）。
2. `meta.db.fetch_errors` 表稳定（P3 不写、仅 events 引用）。
3. `JobRunner.run()` 注册入口（P2）；`batch.deep_research` job slot（P2 已占位）。
4. `events` 表（P2）：P3 写 `factor.deactivated` / `factor.missing_data` / `portfolio.ready`。

## 附录 B：与后续阶段接口承诺

1. `portfolio_snapshots` 表结构稳定（P5 Web 渲染组合 + 历史回放）。
2. `factor_metrics` 表结构稳定（P5 因子健康度仪表盘；P4 LLM Agent 可读做总结）。
3. `attribution.json` schema 稳定（P4 ChatAgent 解释组合时直接读取）。
4. `FactorRegistry` 单例可被 P4 工具调用层读取（"列出当前所有 active factor"是一个 ToolUse）。
