# P1 数据层使用指南

> 对应设计文档：`docs/superpowers/specs/2026-06-17-p1-data-layer-design.md`

## 1. 模块总览

```
src/akq_agents/services/data/
├── __init__.py              # 对外导出：DataRepository / UniverseManager 等
├── schemas.py               # 标准 pydantic schema（OHLCVBar / UniverseSnapshot / DataHealth / RefreshResult）
├── exceptions.py            # 异常类（FetchError / DataNotReady / QualityCheckFailed）
├── akshare_gateway.py       # AKShare 单一出口：限频 + 重试 + 字段映射
├── calendar.py              # 交易日历（bisect 加速）
├── universe.py              # 全 A 股股票池 + 过滤器链
├── quality.py               # QualityGate 入库校验
├── repository.py            # 缓存读写主入口（Parquet + sqlite meta）
└── retry_worker.py          # fetch_errors 重试 worker
```

入口配置：`config/data.yaml`
入口模型：`src/akq_agents/models/data_config.py:DataConfig`

## 2. 存储布局

```
data/
├── parquet/
│   ├── ohlcv/date=YYYY-MM-DD/part.parquet     # 一日全市场日线
│   ├── universe/date=YYYY-MM-DD/snap.parquet  # 当日股票池快照
│   └── (P1 不写) financials/
└── meta.db                                      # sqlite 元数据
```

sqlite 表（由 `DataRepository.__init__` 幂等建表）：
- `fetch_log`：每次 AKShare 调用记录（success/fail/timing）
- `fetch_errors`：失败明细，含 `reason_code` 和 `resolved` 状态
- `data_quality_log`：QualityGate 每次校验结果
- `refresh_state`：每日刷新结果（断点续传依据）

## 3. CLI 速查

需要先准备好 `config/data.yaml`（若缺失，所有 `data` 子命令立即 stderr+exit 1）。

```bash
# 健康检查（不联网，安全）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data status

# 增量刷新某一天（默认今天；需要联网）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data refresh --date 2026-06-17

# 全量回填历史（需要联网，可能几小时）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data bootstrap --lookback 250

# 查询单股缓存（不联网）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data inspect 600519
```

## 4. 字段映射约定

AKShare 中文列 → 数据层内部英文列（在 `akshare_gateway.py` 统一映射）：

| AKShare 列 | 内部列 | 接口来源 |
|---|---|---|
| 日期 | date | stock_zh_a_hist |
| 开盘 / 收盘 / 最高 / 最低 | open / close / high / low | stock_zh_a_hist |
| 成交量 / 成交额 | volume / amount | stock_zh_a_hist / spot |
| 代码 | symbol | stock_zh_a_spot_em |
| 名称 | name | stock_zh_a_spot_em |
| 最新价 | price | stock_zh_a_spot_em |
| 换手率 | turnover_ratio | stock_zh_a_spot_em |

关键列缺失 → `FetchError(reason_code="SCHEMA_DRIFT")`，提示上游 AKShare 字段变更。

## 5. 过滤规则（UniverseManager）

按 `config/data.yaml -> data.universe` 配置启用，**filter 链按顺序短路**，excluded 中保留**第一个**失败原因：

| Filter | reason_code | 默认启用 | 说明 |
|---|---|---|---|
| STFilter | ST | ✓ | 来自 `ak.stock_zh_a_st_em` |
| ListingAgeFilter | LISTING_TOO_NEW | ✓ | 上市天数 < `min_listing_days` (默认 180) |
| SuspendedFilter | SUSPENDED | ✓ | `ak.stock_individual_info_em` 的停牌字段 |
| PriceRangeFilter | PRICE_OUT_OF_RANGE | ✓ | close 不在 `[min_price, max_price]` |

字段缺失 fail-closed：filter 拿不到关键字段就视为"不通过"，避免脏数据漏过。

## 6. 故障排查

### `data status` 返回 `health: FAILED`
- `data/meta.db` 不存在 / `refresh_state` 表为空 → 没跑过 `data refresh` / `data bootstrap`
- coverage_today=0：今日 universe 未生成 → 跑一次 `data refresh`

### `data refresh` 没有写 parquet
检查 `meta.db.data_quality_log`：
```bash
sqlite3 data/meta.db "SELECT * FROM data_quality_log ORDER BY id DESC LIMIT 5"
```
通常 `row_count` 失败 = `universe_size` 太小（全市场不应少于 4000）。

### 大量 `fetch_errors`
```bash
sqlite3 data/meta.db "SELECT reason_code, COUNT(*) FROM fetch_errors WHERE resolved=0 GROUP BY reason_code"
```
- 大量 `RATE_LIMITED` → 调小 `akshare.qps`（默认 5，可降到 3）
- 大量 `SCHEMA_DRIFT` → AKShare 升级、字段映射需要更新
- 大量 `NETWORK` → 检查机器网络出口

`RetryWorker.run_once()` 可主动重试，单独 PyTHONPATH 跑（CLI 未暴露，P2 阶段会接到调度器）。

### `FactorAgent` 跑 run-once 返回 `skipped`
说明 spec A6 验收路径被触发：repository 注入但缓存还没准备好。先跑：
```bash
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data refresh
```

## 7. 与后续阶段的接口承诺（P2/P3/P4/P5 依赖）

详见 spec §附录 B。最关键 5 条：
1. `DataRepository.get_ohlcv` 幂等只读
2. `DataRepository.refresh_daily` 单日重复调用幂等
3. `DataHealth` schema 稳定（P5 Web 控制台直接渲染）
4. `UniverseSnapshot.excluded` 字段稳定
5. `meta.db.fetch_errors` 表结构稳定（P6 告警系统消费）

## 8. 验收快查

| Spec | 状态 | 验证方式 |
|---|---|---|
| A6 缺数据返回 skipped 不报错 | ✅ | `tests/data/test_factor_agent_integration.py` |
| A7 硬编码路径=0 | ✅ | 见 spec §5 验收命令；本仓库无 `Documents/A` 字面量 |
| B1 覆盖率 ≥ 80% | ✅ 94% | `pytest --cov=akq_agents.services.data` |
| B2 ruff 0 warnings | ✅ | `ruff check src/ tests/` |
| B4 .gitignore 完整 | ✅ | 已包含 data/, *.db, runtime_state.yaml 等 |

A1/A2 (bootstrap/refresh 实跑) 和 C1/C2/C3 (性能) 因为需要真实联网拉数，建议在你方便的时段单独验证。
