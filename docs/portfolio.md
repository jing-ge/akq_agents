# P3a 多因子组合机使用指南

> 对应设计文档：`docs/superpowers/specs/2026-06-17-p3-portfolio-engine-design.md`

## 1. 模块总览

```
src/akq_agents/services/
├── factors/                  # 因子库（注册表 + 协议 + 6+ 因子 + Engine）
│   ├── __init__.py           # 出口：build_default_registry()
│   ├── base.py               # Factor protocol + FactorRegistry
│   ├── engine.py             # FactorEngine.compute(ohlcv, factors) → wide DF
│   ├── momentum.py           # momentum_5/20/60
│   ├── reversal.py           # reversal_5
│   ├── volatility.py         # volatility_20
│   ├── liquidity.py          # amount_20 (流动性)
│   └── size.py               # log_amount_20 (规模代理)
└── portfolio/                # 组合 pipeline
    ├── __init__.py
    ├── combined_universe.py  # top 500 by amount_20 流动性筛选
    ├── preprocessor.py       # MAD 去极值 + z-score + direction 反号
    ├── evaluator.py          # 滚动 IC/IR/t-stat → factor_metrics 表
    ├── composite.py          # CompositeScorer (P3a equal weight)
    ├── optimizer.py          # PortfolioOptimizer (inverse_vol top-N + max_single_weight cap)
    ├── attributor.py         # 线性归因 z_{s,f} × w_f
    └── snapshot_store.py     # portfolio_snapshots 表读写
```

`PortfolioAgent`（agents/portfolio_agent.py）串起所有 7 个步骤。`batch.deep_research`
（orchestrator/jobs/batch_deep_research.py）每周日 22:00 自动跑因子有效性评估。

## 2. 组合 Pipeline（7 个步骤）

```
DataRepository.get_universe(today)   ← P1 全 A 股 universe（~5000）
       │
       ▼
build_portfolio_universe(top_n=500, window=20)   ← 取流动性 top 500
       │
       ▼
FactorEngine.compute(sub_ohlcv, factors)         ← 7 个价格类因子
       │
       ▼
Preprocessor.transform                            ← winsorize MAD + zscore + short 反号
       │
       ▼
CompositeScorer.score                             ← P3a equal weight 合成
       │
       ▼
PortfolioOptimizer.solve                          ← top 50 by score, weight ∝ 1/vol_20
       │
       ▼
Attributor.explain                                ← contribution = z_{s,f} × w_f
       │
       ▼
PortfolioSnapshotStore.write                      ← 写 portfolio_snapshots 表
```

## 3. 配置

P3a 暂未引入独立 `config/portfolio.yaml`；使用 spec 中默认值（hardcode）：

- 组合 universe: top 500 by amount_20 (window=20)
- Preprocessor: winsorize MAD k=3.0, zscore=true
- Optimizer: top_n=50, max_single_weight=0.05, min_vol=1e-4
- Composite: equal weight
- FactorEvaluator: rolling window 60d
- Attribution: top_k_contributors=5

P3b 引入 `config/portfolio.yaml` 时这些参数才会配置化。

## 4. CLI 速查

```bash
# 列出所有因子 + 最近 metrics
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app factors list

# 看某因子历史 metrics
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app factors inspect momentum_5

# 看某日组合快照（默认今日）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app portfolio explain --date 2026-06-17
```

## 5. 表 DDL（追加到 `data/meta.db`）

```sql
CREATE TABLE factor_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factor_name TEXT NOT NULL,
  factor_version INTEGER NOT NULL,        -- 跟 Factor.factor_version 一致；升级算法时换行
  as_of_date TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  ic_mean REAL, ic_std REAL, ir REAL, t_stat REAL,
  status TEXT NOT NULL,                   -- 'active' (P3a 永远)；P3b 起会出现 'inactive'
  reason TEXT,
  UNIQUE(factor_name, factor_version, as_of_date, window_days)
);

CREATE TABLE portfolio_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT,                              -- 镜像 P1 universe 中文名
  industry TEXT,                          -- P3a 留空，P3b 接入行业映射后填
  weight REAL NOT NULL,
  prev_weight REAL,                       -- 上一交易日仓位；新股填 0
  composite_score REAL,
  top_factors_json TEXT,                  -- 已 dump 的 [{name, contribution}, ...]
  UNIQUE(as_of_date, symbol)
);
```

> **稳定承诺**：P5 Web 直接 `SELECT * FROM portfolio_snapshots`，**不 join 其他表**；P3 已镜像所需字段（spec 附录 B §1）。

## 6. 因子目录（v1）

| name | factor_version | direction | lookback_days | 公式 |
|---|---|---|---|---|
| momentum_5 | 1 | long | 10 | close[t]/close[t-5] - 1 |
| momentum_20 | 1 | long | 30 | close[t]/close[t-20] - 1 |
| momentum_60 | 1 | long | 80 | close[t]/close[t-60] - 1 |
| reversal_5 | 1 | long | 10 | -(close[t]/close[t-5] - 1)（内部反号→输出"越大越好"） |
| volatility_20 | 1 | short | 30 | std(returns[-20:]) （越小越好） |
| amount_20 | 1 | long | 20 | mean(amount[-20:]) （流动性） |
| log_amount_20 | 1 | short | 20 | log1p(mean(amount[-20:])) （规模代理，越小越好 = 小盘偏好） |

## 7. 归因数学（spec §3 流程 5）

定义：
- `z_{s,f}` = Preprocessor 输出的 z-score
- `w_f` = CompositeScorer 用的因子权重（P3a equal = 1/N_factors）
- `comp_s = Σ_f z_{s,f} × w_f` = CompositeScorer 输出
- **`contribution_{s,f} = z_{s,f} × w_f`** 单股因子贡献
- **`portfolio_contribution_f = Σ_s W_s × z_{s,f} × w_f`** 组合层因子贡献

**A6 验收等式**：`|Σ_f contribution_{s,f} − comp_s| < 1e-6` for all s（P3a 因不做行业中性化所以严格成立）。

P3b 引入行业 + 市值中性化后，等式不再严格成立；spec 已写明放宽到 ≤ 1%。

## 8. 添加新因子（流程示例）

1. 在 `src/akq_agents/services/factors/<name>.py` 新建 dataclass：
   ```python
   from dataclasses import dataclass
   @dataclass
   class _MyFactor:
       name: str = "my_factor"
       factor_version: int = 1
       lookback_days: int = 30
       direction: str = "long"      # 或 "short"
       inputs: tuple[str, ...] = ("ohlcv",)

       def compute(self, ohlcv):    # 必须有此方法
           # 算法 ...
           return some_series       # index=symbol, name=self.name

   def my_factor():
       return _MyFactor()
   ```
2. 在 `factors/__init__.py:build_default_registry` 里 `reg.register(my_factor())`
3. 加 `tests/factors/test_my_factor.py`（参考 momentum 测试）
4. 下次盘后 / deep_research 自动跑

**factor_version 升级规则**：算法变了就 +1。新版本的 metrics 从下次 deep_research 开始重新累积；P5 Web 按 factor_version 分组渲染。

## 9. 故障排查

### CLI `factors list` 所有 last_* 字段全为 null
说明 `factor_metrics` 表是空的——`batch.deep_research` 还没跑过。手动触发：将 `config/scheduler.yaml` 中 `batch_deep_research.enabled` 改为 true 并重启 daemon；或写一个一次性脚本调 `FactorEvaluator.evaluate(...)`。

### `portfolio explain --date X` 返回 `no_snapshot_for_date`
- 该日 batch.post_close 没跑过（节假日跳过 / daemon 没在跑 / 数据未就绪）
- 检查 `daemon runs --job-id batch.post_close --last 10`

### Optimizer 返回空 weights
- composite_score 全 NaN：检查 factor compute 是否都因 lookback 不足返回 NaN
- vol_20 全部小于 min_vol=1e-4：universe 全是停牌股，需扩 universe

### Optimizer 异常或 portfolio 全只有 1 个 symbol weight=100%
- max_single_weight cap 没生效：检查配置 OptimizerConfig
- 数据问题导致只有 1 个 symbol 通过过滤

## 10. 验收快查

| Spec | 状态 | 验证方式 |
|---|---|---|
| A1 新增因子只改 1 文件 + 1 行 | ✅ | 已演示流程见 §8 |
| A2 deep_research 写 N × 1 行 | ✅ | `tests/portfolio/test_evaluator.py` |
| A3 factor_metrics 每行带 factor_version | ✅ | DDL + `test_factor_version_separates_metrics` |
| A4 空 metrics → equal weight bootstrap | ✅ | CompositeScorer 永远 equal；P3a 不读 metrics |
| A5 Optimizer 满足约束 | ✅ | `test_optimizer_max_single_weight_cap` |
| A6 归因等式 < 1e-6 | ✅ | `test_attribution_sum_equals_composite_a6` |
| A7 portfolio_snapshots 字段齐 | ✅ | `test_snapshot_store.py` |
| A8 portfolio explain CLI | ✅ | `cmd_portfolio_explain` |
| A9 组合 universe = top 500 | ✅ | `test_combined_universe.py` + PortfolioAgent 集成测 |
| A10 events.kind 符合规范 | ✅ | P2 附录 C 已注册 portfolio.snapshot.generated 等 |
| B1 覆盖率 ≥ 80% | ✅ 96% | `pytest --cov` |
| B2 ruff 0 warnings | ✅ | `ruff check src/ tests/` |

## 11. 与 P1 / P2 / P4 / P5 的接口承诺

详见 spec 附录 A / B。关键：
- 依赖 P1：`get_universe / get_ohlcv` 幂等只读；`meta.db` WAL
- 依赖 P2：`batch.deep_research` job slot；`events.kind` 注册表
- 承诺 P5：`portfolio_snapshots` 含 name/industry/top_factors_json，直接 SELECT 渲染
- 承诺 P4：`FactorRegistry` 单例 + `factor_metrics` schema 稳定
