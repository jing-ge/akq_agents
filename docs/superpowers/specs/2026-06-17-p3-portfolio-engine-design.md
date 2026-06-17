# P3 多因子组合机 — 设计文档（v2，oracle review 后收敛）

- 项目：akq-agents
- 阶段：P3（共 P1–P6 六阶段中的第三阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（DataRepository / UniverseSnapshot / OHLCV / meta.db WAL）、P2（JobRunner / batch.deep_research / events 表 + 命名规范 / events.kind enum）

> **v2 收敛说明**（oracle review 后）：
> - **拆成 P3a + P3b 两期**：P3a 先把端到端跑通（FactorRegistry + Preprocessor 简化版 + inverse_vol top-N 等权 + 基础 Attributor），P3b 再上 cvxpy MV + IR 失能闭环 + 行业中性化。验收承诺仅覆盖 P3a；P3b 列为后续工作。
> - **限制组合 universe**：组合 universe ⊂ 数据 universe，固定取 top 500 by 流动性（20 日成交额均值），避免 4000 维 QP 跑不动。
> - **`factor_metrics` 加 `factor_version` 字段** 解决 P5 join 需求。
> - **`portfolio_snapshots` 扩字段** 直接镜像 P5 渲染需要的 `name` / `industry` / `top_factors_json`；P5 无需现场组装。
> - **首日无 metrics 时退化到 equal weight**（不抛异常）。
> - **归因数学重定义**：行业中性化后因子非正交，简单 `exposure × weight` 求和 ≠ 综合分。改为定义"贡献"= z-score × factor_weight（线性归因，承诺误差 ≤ 1%，不强求精确等式）。

---

## §1 目标与边界

### 目标

把"9 个内联因子 + 简单加权"的玩具版升级为一个**可注册、可回测、可组合、可归因**的多因子组合机。

### P3a 范围（必交付，验收覆盖）

- 抽象 `Factor` 协议 + `FactorRegistry`（注册、查询、版本号 `factor_version`）。
- 实现一组价格类因子：动量 5/20/60、反转 5、波动率 20、流动性（amount_20_rank）、规模（log_market_cap）。
- `Preprocessor` 简化版：去极值（MAD ±3）+ 横截面 Z-Score（**不做行业 / 市值中性化**，留 P3b）。
- `FactorEvaluator`：滚动 IC（rolling 60d）、IR、t 统计；写 `factor_metrics` 表。**仅用于可观测**，P3a 的 CompositeScorer **不读 metrics 做权重**。
- `CompositeScorer`：所有 active 因子 **equal weight**（按 direction 调号后 z-score 平均）。
- `PortfolioOptimizer` P3a 简化版：**inverse_vol top-N 等权**（仅一种算法），不引入 cvxpy。
- `Attributor`：定义 `contribution_i = z_i × factor_weight_i`，作为线性归因；输出 attribution dict 并镜像写入 `portfolio_snapshots.top_factors_json`。
- **组合 universe 限制**：固定取数据 universe 中 top 500 by 流动性。
- 新增 job：`batch.deep_research`（P2 已占位），周日 22:00 跑 factor_metrics 滚动更新。
- CLI：`akq-agents factors list | inspect <name>`、`akq-agents portfolio explain --date YYYY-MM-DD`。

### P3b 范围（后续工作，不在本期验收）

- 行业中性化（neutralize by industry + log_market_cap，OLS 取残差）。
- 财务类因子（ROE / PB / PE 分位）— 依赖财务数据补齐（P1.5）。
- `PortfolioOptimizer` 升级为 cvxpy MV with constraints（行业暴露上限、turnover 上限、协方差 Ledoit-Wolf shrinkage）。
- IR 加权 + 因子失能闭环（factor_metrics → CompositeScorer weights）。
- 归因从线性升级到 Brinson 风格。

### 不在做什么

- ❌ 机器学习因子（XGBoost / Lasso 选因子）。
- ❌ 多空对冲（仅做多 + 现金）。
- ❌ 期权 / 期货 / ETF 组合 — 仅 A 股股票。
- ❌ 实时风控盯盘（盘中只读快照）。
- ❌ 调仓执行（仅输出目标仓位）。
- ❌ 自动调参 / 超参搜索。

---

## §2 架构

### 分层结构（P3a）

```
┌────────────────────────────────────────────────────────────────┐
│  DataRepository (P1)                                            │
└────────┬────────────────────────────────────────────────────────┘
         │ get_ohlcv / get_universe (P1 接口承诺，幂等只读)
         ▼
┌────────────────────────────────────────────────────────────────┐
│  CombinedUniverseBuilder                                        │
│  - 输入：data universe (P1，~5000 标的)                          │
│  - 计算每只股票 20 日成交额均值                                   │
│  - 输出 portfolio universe：top 500 by amount_20_mean             │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  FactorRegistry                                                  │
│  - register(factor: Factor)  factor.factor_version >= 1          │
│  - get(name) / list_all() / list_active(as_of_date)              │
│    P3a: list_active 直接返回 list_all（不读 metrics）            │
│    P3b: 读 factor_metrics 最近一次 status='active' 子集           │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  FactorEngine.compute(universe, date, factors)                  │
│  - 拉数据（按 max(factor.lookback_days)）                        │
│  - 并行计算每个 factor（threadpool）                              │
│  - 返回 wide DataFrame: index=symbol, columns=factor_name        │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  Preprocessor (P3a 简化版)                                       │
│  - winsorize (MAD ±3)                                            │
│  - z-score 横截面                                                │
│  - direction 统一为 'long'（short 因子内部反号）                  │
│  - 输出：标准化后的因子 DataFrame                                 │
└────────┬────────────────────────────────────────────────────────┘
         ├──► FactorEvaluator (滚动 IC / IR)  仅可观测                │
         │       └── writes meta.db.factor_metrics                  │
         ▼
┌────────────────────────────────────────────────────────────────┐
│  CompositeScorer (P3a: equal weight)                            │
│  - composite_score = mean(z_i for i in active factors)           │
│  - 失能机制：P3a 没有（list_active = list_all）                   │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  PortfolioOptimizer (P3a: inverse_vol top-N 等权)               │
│  - 取 composite_score top N（配置）                              │
│  - 权重 = (1 / vol_20) 归一化                                    │
│  - max_single_weight 截断（超出则向其余股转移）                   │
└────────┬────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────────────────────────────────────────────┐
│  Attributor (P3a: 线性归因)                                     │
│  - contribution_i = z_i × factor_weight_i                        │
│  - 输出 attribution.json + 顶层 dict + top_factors_json         │
└────────────────────────────────────────────────────────────────┘
```

### 存储扩展（追加到 P1 的 `meta.db`）

```sql
CREATE TABLE IF NOT EXISTS factor_metrics (
  id INTEGER PRIMARY KEY,
  factor_name TEXT NOT NULL,
  factor_version INTEGER NOT NULL,  -- 与 Factor.factor_version 绑定，版本变了换行
  as_of_date TEXT NOT NULL,
  window_days INTEGER NOT NULL,     -- 60d (P3a 仅一个窗口；P3b 再加 120/250)
  ic_mean REAL,
  ic_std REAL,
  ir REAL,                          -- ic_mean / ic_std
  t_stat REAL,
  status TEXT NOT NULL,             -- P3a 永远 'active'；P3b 起会出现 'inactive'
  reason TEXT,
  UNIQUE(factor_name, factor_version, as_of_date, window_days)
);

CREATE INDEX IF NOT EXISTS idx_factor_metrics_lookup
  ON factor_metrics(factor_name, factor_version, as_of_date);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  id INTEGER PRIMARY KEY,
  as_of_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT,                        -- 镜像 P1 universe 中文名（避免 P5 join）
  industry TEXT,                    -- P3a 留空字符串；P3b 接入行业映射后填
  weight REAL NOT NULL,             -- 目标仓位 [0, max_single_weight]
  prev_weight REAL,                 -- 上一交易日仓位；新股 / 无历史填 0
  composite_score REAL,
  top_factors_json TEXT,            -- e.g. '[{"name":"momentum_20","contribution":0.42}, ...]'
  UNIQUE(as_of_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_date ON portfolio_snapshots(as_of_date);
```

> **顶层归因摘要** 写入 `reports/YYYY-MM-DD/attribution.json`，**字段镜像** `portfolio_snapshots.top_factors_json` 聚合后的结果（同一数据两种载体）；任何阶段读其一即可，不会产生口径分歧。

### 配置示例（`config/portfolio.yaml`，新增；P3a 仅用部分字段）

```yaml
portfolio:
  combined_universe:
    method: "top_amount_20"
    top_n: 500
  preprocessing:
    winsorize_mad_k: 3.0
    zscore: true
    # neutralize_by: 留空，P3b 启用
  evaluation:
    rolling_window: 60          # P3a 仅一个窗口
    # P3b: rolling_windows: [60, 120], decay_lags: [2,5,10], deactivate rules
  composite:
    weighting: "equal"          # P3a 仅 equal；P3b 加 ir
  optimizer:
    method: "inverse_vol"       # P3a 仅一种；P3b 加 mean_variance
    top_n: 50
    max_single_weight: 0.05
    # P3b: min_single_weight / max_industry_weight / max_turnover / cov_estimation
  attribution:
    top_k_contributors: 5
```

---

## §3 数据流与时序

### 流程 1：盘后 batch.post_close（由 P2 调用，每个交易日）

```
P2 batch.post_close
  → DataRepository.refresh_daily(today)   # P1
  → CombinedUniverseBuilder.build(today)  → portfolio_universe (~500 标的)
  → FactorEngine.compute(portfolio_universe, today, registry.list_active(today))
  → Preprocessor.transform(factor_df)
  → CompositeScorer.score(processed_df)
       - 首次跑 / factor_metrics 表为空 → equal weight（log warning + events 'factor.metric.bootstrap'）
  → PortfolioOptimizer.solve(score, vol_20, prev_weights)
       - top N by score
       - weight ∝ 1/vol_20，归一化到 sum=1
       - 单股权重超 max_single_weight → 截断 + 多余权重比例转移到其他持仓
  → Attributor.explain(weights, processed_factor_df, factor_weights)
  → 写入：
      - meta.db.portfolio_snapshots（含 name/top_factors_json/prev_weight）
      - reports/YYYY-MM-DD/attribution.json
      - context.state["portfolio"] / ["attribution"]
  → events(kind="portfolio.snapshot.generated", payload={n: 50, turnover: 0.18})
```

### 流程 2：周日 batch.deep_research（每周日 22:00；P3a 也跑）

```
P2 batch.deep_research
  → 对 registry.list_all() 每个因子：
       - 拉过去 (60+lookback) 日 OHLCV + 因子值历史
       - 算 rolling IC (window=60) / IR / t-stat
       - status 永远写 'active'（P3a；P3b 起判定 inactive）
  → 写 meta.db.factor_metrics（追加一行 per factor per window，按 factor_version）
  → events(kind="factor.metric.evaluated", payload={n_factors: N, window: 60})
```

**关键约束**：
- deep_research 跑得慢没关系（不影响盘后日报）。
- 每次写新行，永不 update；用 `(factor_name, factor_version, as_of_date)` 区分版本。
- 若某 factor 升级了 `factor_version`，旧版本 metrics 不被删除（P5 可回看历史）；新版本会从下一次 deep_research 开始累积新数据。

### 流程 3：首日 / 空 metrics 路径

```
CompositeScorer.score(processed_df):
  if cfg.composite.weighting == "equal":
      return processed_df.mean(axis=1)
  # P3b 起：
  if metrics 表中本 factor_version 无任何数据:
      events("factor.metric.bootstrap", level="warning")
      return processed_df.mean(axis=1)
  weights = read_latest_ir_weights(...)
  return (processed_df * weights).sum(axis=1)
```

### 流程 4：新股 / 退市的 prev_weight 处理

```
Optimizer.solve(...):
  prev = repo_read_yesterday_weights()  # Dict[symbol -> weight]
  for sym in today_universe:
      prev_w = prev.get(sym, 0.0)       # 新股 → 0
  # 退市股（昨天有今天无）：
  #   - 直接不进 today_universe；prev_w 进入 turnover 计算但不进入 portfolio_snapshots
  turnover = sum(|w_today - w_prev|) / 2
  # turnover 仅作为 portfolio.snapshot.generated 的 payload 字段一起写出，不单独发事件
  events("portfolio.snapshot.generated", payload={"n": len(today_weights), "turnover": turnover, ...})
```

### 流程 5：归因（线性，明确数学）

定义：
- `z_{s,f}`：股票 s 因子 f 的 z-score（Preprocessor 输出）
- `w_f`：CompositeScorer 中因子 f 的权重（P3a equal=1/N_factors）
- `comp_s = Σ_f z_{s,f} × w_f`（**精确等于** CompositeScorer 输出，P3a 因不做行业中性化所以确实是线性可分解）
- **`contribution_{s,f} = z_{s,f} × w_f`** 单股贡献
- **`portfolio_contribution_f = Σ_s W_s × z_{s,f} × w_f`** 组合层贡献（W_s 是最终持仓权重）
- **`top_factors_json[s] = top_k(contribution_{s,*}, k=5)`** 写入快照

承诺：**P3a 验收要求 |Σ_f contribution_{s,f} − comp_s| < 1e-6**；P3b 引入行业中性化后，等式不再成立，承诺改为 ≤ 1%。

### 流程 6：CLI 检视

```
akq-agents factors list
  → 表格：name | version | direction | lookback | (last_ic | last_ir | last_evaluated)
  P3a: last_* 来自 factor_metrics 最新行；首次跑前为空，显示 '-'

akq-agents factors inspect momentum_20
  → 历史 IC 序列 / IR 走势（文本 sparkline，最近 30 个 as_of_date）

akq-agents portfolio explain --date 2026-06-17
  → 读 portfolio_snapshots + attribution.json
  → 文本输出："今日组合 N 只，turnover X%，顶层因子贡献 top5..."
```

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── services/
│   ├── factor_service.py              ← 重写：薄壳，仅 FactorRegistry 出口
│   ├── factors/                        ← 新增子包
│   │   ├── __init__.py
│   │   ├── base.py                     # Factor protocol + FactorRegistry
│   │   ├── momentum.py                 # Momentum5/20/60
│   │   ├── reversal.py                 # Reversal5
│   │   ├── volatility.py               # Volatility20
│   │   ├── liquidity.py                # Amount20Rank
│   │   ├── size.py                     # LogMarketCap
│   │   └── _engine.py                  # FactorEngine: 并行计算
│   ├── portfolio/
│   │   ├── __init__.py
│   │   ├── combined_universe.py        # top_amount_20 selector
│   │   ├── preprocessor.py             # P3a 简化版
│   │   ├── evaluator.py                # rolling IC/IR；写 factor_metrics
│   │   ├── composite.py                # equal weight scorer
│   │   ├── optimizer.py                # inverse_vol top-N
│   │   ├── attributor.py               # 线性归因
│   │   └── snapshot_store.py           # portfolio_snapshots 读写
│   └── ...
├── models/
│   └── portfolio_config.py             ← PortfolioConfig (pydantic)
├── orchestrator/
│   └── jobs/
│       ├── batch_post_close.py         ← 注入 portfolio pipeline（替换原 PortfolioAgent）
│       └── batch_deep_research.py      ← P2 占位 → P3a 实现
└── cli/
    └── app.py                          ← 新增子命令：factors / portfolio
```

### Factor 协议

```python
class Factor(Protocol):
    name: str                       # 全局唯一
    factor_version: int             # 实现版本，>= 1；改算法时 +1
    inputs: list[Literal["ohlcv", "industry", "financials"]]
    lookback_days: int              # 计算需要的 OHLCV 回溯天数
    direction: Literal["long", "short"]   # 数值越大越好(long) 还是越小越好(short)

    def compute(self, ohlcv: pd.DataFrame) -> pd.Series:
        """返回 index=symbol, values=因子原始值。允许 NaN（缺数据），
        下游 Preprocessor 会处理。
        P3a: 仅依赖 ohlcv；P3b 起可声明 inputs=["industry","financials"]，
        engine 会在调用时注入对应数据。"""

class FactorRegistry:
    def register(self, factor: Factor) -> None: ...
    def get(self, name: str) -> Factor: ...
    def list_all(self) -> list[Factor]: ...
    def list_active(self, as_of_date: date) -> list[Factor]:
        """P3a: 直接返回 list_all();
           P3b: 读 factor_metrics 最近一次 status='active' 的子集，
                若 metrics 为空 → 退化为 list_all() + events('factor.metric.bootstrap')"""
```

### Preprocessor 接口（P3a）

```python
class Preprocessor:
    def __init__(self, cfg: PreprocessingConfig): ...
    def transform(self, factor_df: pd.DataFrame,
                  directions: dict[str, Literal["long","short"]]) -> pd.DataFrame:
        """对每个因子列：
        1) winsorize MAD ±k
        2) z-score 横截面
        3) direction='short' → 内部反号，使输出统一'越大越好'
        返回 index=symbol, columns=factor_name (NaN 保留)"""
```

### PortfolioOptimizer 接口（P3a：inverse_vol top-N）

```python
class PortfolioOptimizer:
    def __init__(self, cfg: OptimizerConfig): ...
    def solve(self,
              composite_score: pd.Series,
              vol_20: pd.Series,
              prev_weights: pd.Series | None) -> pd.Series:
        """P3a:
        - 取 score top N（配置 top_n=50）
        - weight ∝ 1/vol_20，归一化使 sum=1
        - 任一权重 > max_single_weight → 截断；多余权重按比例转移到剩余
        - 返回 Series(symbol → weight), sum=1
        P3b: 升级为 cvxpy MV with constraints；若 infeasible 退化到 inverse_vol 并写 events"""
```

### Attributor 接口

```python
class Attributor:
    def explain(self,
                weights: pd.Series,                  # 组合权重
                factor_z: pd.DataFrame,              # Preprocessor 输出
                factor_weights: pd.Series,           # CompositeScorer 用的因子权重
                top_k: int = 5) -> AttributionResult:
        """返回结构：
        {
          "as_of_date": ...,
          "portfolio_contribution": {factor_name: float},    # 组合层贡献
          "per_stock": {symbol: [{"name":..., "contribution":...}]},  # 每股 top_k 因子
          "summary": "..."   # 一句话总结，喂给 P4 AnalystAgent
        }
        P3a 验收：sum over factors of contribution_{s,f} 与 composite_score 误差 < 1e-6"""
```

### Snapshot 写入（确保 P5 渲染所需字段齐备）

```python
class PortfolioSnapshotStore:
    def write(self,
              as_of_date: date,
              weights: pd.Series,
              prev_weights: pd.Series,
              composite_score: pd.Series,
              attribution: AttributionResult,
              name_map: dict[str, str]) -> None:
        """对 weights.index 的每只股票，写一行 portfolio_snapshots，含：
        - name: name_map.get(symbol, '')   # 从 P1 universe 取
        - industry: ''（P3a 留空，P3b 接入行业映射后填）
        - top_factors_json: json.dumps(attribution.per_stock[symbol])
        """
```

### Agent 集成

```python
# 替换现有 agents/portfolio_agent.py
class PortfolioAgent(BaseAgent):
    def __init__(self, services: dict, cfg: PortfolioConfig): ...
    def run(self, context):
        repo = services["data_repository"]
        registry = services["factor_registry"]
        engine, prep, scorer, opt, attr, store = (
            services["factor_engine"], services["preprocessor"],
            services["composite_scorer"], services["portfolio_optimizer"],
            services["attributor"], services["portfolio_snapshot_store"],
        )
        today = context.state["today"]
        try:
            full_universe = repo.get_universe(today)
            portfolio_universe = build_combined_universe(repo, full_universe, today, cfg)
        except DataNotReady as e:
            return {"status": "skipped", "reason": "data_not_ready"}
        raw   = engine.compute(portfolio_universe, today, registry.list_active(today))
        z     = prep.transform(raw, factor_directions(registry))
        comp  = scorer.score(z)
        vol20 = repo.compute_vol(portfolio_universe, today, 20)
        prev  = store.read_prev_weights(today)
        w     = opt.solve(comp, vol20, prev)
        attribution = attr.explain(w, z, scorer.factor_weights(), top_k=cfg.attribution.top_k_contributors)
        store.write(today, w, prev, comp, attribution, name_map=repo.symbol_name_map())
        context.state["portfolio"] = w.reset_index().rename(columns={"index":"symbol", 0:"weight"}).to_dict("records")
        context.state["attribution"] = attribution.dict()
        return {"status": "ok", "n": len(w)}
```

### 关键边界

- ❌ Factor 内部不读 sqlite / 不写文件；只接收输入、返回 Series。
- ❌ Optimizer 不知道因子；只接收 score + vol + prev_weights。
- ❌ Attributor 不读 db；只做矩阵运算。
- ❌ 财务因子（value/quality）P3 **不实现**；移到 P3b。
- ❌ P3a 的 CompositeScorer 不读 `factor_metrics`（仅 evaluator 写入用于可观测）。
- ❌ Optimizer 不会触发 cvxpy（P3b 才引入）。

### 测试策略

```
tests/factors/
├── test_registry.py              # 注册、唯一性、factor_version 区分
├── test_momentum.py              # 数值校验（fixture 50 标的 30 日）
├── test_volatility.py
├── test_liquidity.py
├── test_size.py
└── test_engine.py                # 并行计算正确性、缺数据 NaN 处理

tests/portfolio/
├── test_combined_universe.py     # top_amount_20 选股、边界（amount NaN）
├── test_preprocessor.py          # winsorize / zscore / direction 反号
├── test_evaluator.py             # rolling IC 数值校验 + factor_version 写入
├── test_composite.py             # equal weight；缺 metrics 时 bootstrap warn
├── test_optimizer.py             # top-N + inverse_vol + max_single_weight 截断
├── test_attributor.py            # contribution 求和 == composite_score（误差 1e-6）
├── test_snapshot_store.py        # write 含 name/top_factors_json/prev_weight
└── fixtures/                     # 50 标的样本 OHLCV
```

目标覆盖率：**`services/factors/` + `services/portfolio/` ≥ 80%**。

---

## §5 验收标准与里程碑

### A. 功能验收（P3a）

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | 新增 1 个 factor 只改 1 个文件 + 1 行注册 | 演示新增 `amihud_illiquidity` |
| A2 | `factor_metrics` 表跑过 1 次 deep_research 后有 N (#factors) × 1 (window=60) 行 | sqlite 查询 |
| A3 | `factor_metrics` 表的每行 `factor_version` 字段非空且与 Factor 实现版本一致 | 单测 |
| A4 | 首次跑 / `factor_metrics` 为空 → CompositeScorer 返回 equal weight 结果，不抛异常，写 events 'factor.metric.bootstrap' | 集成测 |
| A5 | Optimizer 输出满足约束：单股权重 ≤ max_single_weight、weights.sum() ≈ 1.0、len ≤ top_n | 单测 + 端到端 |
| A6 | 归因数学等式：`|Σ_f contribution_{s,f} − composite_score_s| < 1e-6` for all s | 单测 |
| A7 | `portfolio_snapshots` 行含完整字段：name 非空、top_factors_json 是合法 JSON 数组、prev_weight 新股为 0、退市不在表中 | 集成测 |
| A8 | `akq-agents portfolio explain --date <today>` 输出可读归因报告 | CLI 验证 |
| A9 | 组合 universe = top 500 by amount_20，对 4000+ 标的输入只跑 500 标的的下游计算 | 性能 + 单测 |
| A10 | `events.kind` 全部符合 P2 附录 C 规范（如 `portfolio.snapshot.generated`） | grep + 单测 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/factors/` + `tests/portfolio/` 覆盖率 ≥ 80% | `pytest --cov` |
| B2 | `ruff check` 零警告 | CI |
| B3 | 每个 factor 顶部 docstring 含公式 + 引用 | review |
| B4 | `portfolio_snapshots` / `factor_metrics` 表 DDL 写入文档 | docs/portfolio.md |

### C. 性能验收（P3a）

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | FactorEngine 计算 500 标的 × 6 factor × 60d lookback ≤ 15s | 实测 |
| C2 | Optimizer 500 标的 inverse_vol top-50 ≤ 1s | 实测 |
| C3 | Preprocessor 整套 500 标的 ≤ 2s | 实测 |
| C4 | 端到端 batch.post_close 包含 portfolio pipeline ≤ 30 分钟（不含 P4 AnalystAgent） | events 表 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/portfolio.md`：架构、因子列表、配置项、归因报告示例、故障排查、P3a vs P3b 差异 |
| D2 | `docs/factor_registry.md`：如何新增一个因子（含模板，含 factor_version 升级流程） |
| D3 | README 增加 `factors list` / `portfolio explain` 命令示例 |

### 里程碑参考（P3a）

- M3.1 FactorRegistry + 协议 + 6 个价格类因子（2 天）
- M3.2 CombinedUniverseBuilder + Preprocessor 简化版（1 天）
- M3.3 FactorEvaluator + factor_metrics 表（含 factor_version）（1 天）
- M3.4 CompositeScorer (equal) + PortfolioOptimizer (inverse_vol top-N) + Attributor 线性归因（2 天）
- M3.5 PortfolioSnapshotStore（含 name/top_factors_json）+ PortfolioAgent 重写（1 天）
- M3.6 接入 batch.deep_research + CLI factors/portfolio 子命令（1 天）

**预估总工时：6–7 工作日**（v1 的 9–12 天因拆 P3b、砍 cvxpy/行业中性化/财务因子/IR 失能/多窗口而缩短）。

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| `factor_version` 升级导致历史 metrics 断层 | metrics 累积慢 | 接受；新版本 metrics 重新累积；P5 渲染时按 version 分组 |
| 流动性 top 500 选股漏掉重要股 | 组合代表性 | top 500 已覆盖 ~95% 成交额；接受；P3b 可配置调大 |
| `vol_20` 含停牌日 → 异常低波 | inverse_vol 给停牌股过高权重 | Optimizer 内部对 vol < 1e-4 的标的 reject |
| factor_metrics 写入失败（disk full） | 下次跑 P3 仍 fallback equal weight | 接受；事件落 stderr，不阻塞 batch |
| Attributor 线性等式在 P3b 行业中性化后不成立 | 跨版本验收失败 | 已在 §3 流程 5 明文承诺：P3a < 1e-6，P3b ≤ 1% |
| 全市场 portfolio_universe.amount 数据缺失 | top 500 选股失败 | fallback 到字典序 top 500 + 写 events 警告 |

### 越界声明（P3 整体）

- ❌ ML / 神经网络因子
- ❌ 多空 / 杠杆 / 衍生品
- ❌ 自动调参
- ❌ 实时风控盯盘（盘中只读快照）
- ❌ 调仓执行（仅给目标仓位）
- ❌ cvxpy / 行业中性化 / 财务因子 / IR 失能闭环（推迟到 P3b）

---

## 附录 A：与 P1/P2 依赖契约

P3 依赖：
1. `DataRepository.get_ohlcv / get_universe` 幂等只读（P1）。
2. `DataRepository.compute_vol(symbols, date, window)` — **P1 已提供或 P3 内部基于 get_ohlcv 计算**（P3 实施期决定，但不修改 P1 接口签名）。
3. `meta.db` WAL + busy_timeout（P1 附录 B §6）。
4. `meta.db.fetch_errors` 表稳定（P3 不写、仅 events 引用）。
5. `JobRunner.run()` 注册入口（P2）；`batch.deep_research` job slot（P2 已占位）。
6. `events` 表 + `events.kind` 命名规范（P2 附录 C）。

## 附录 B：与后续阶段接口承诺

1. `portfolio_snapshots` 表结构稳定（含 name/industry/top_factors_json）：P5 直接 SELECT 渲染，**不需要 join P1 universe 或行业表**。
2. `factor_metrics` 表结构稳定（含 factor_version）：P4 LLM tool 与 P5 渲染均按 `(factor_name, factor_version, as_of_date)` 取最新行。
3. `reports/YYYY-MM-DD/attribution.json` schema 稳定，与 portfolio_snapshots.top_factors_json 聚合后字段一致；任一阶段读其一即可。
4. `FactorRegistry` 单例可被 P4 工具调用层读取（"列出当前所有 active factor"是一个 ToolUse）。
5. P3a 的"组合 universe = top 500 by amount_20"承诺稳定（P5 渲染时可说明此口径）。
6. **`events.kind` 仅写入 P2 附录 C 已枚举的 portfolio.* / factor.* 集合**；新增 kind 必须先回 P2 附录 C 注册。