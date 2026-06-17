# 半实盘研究阶段说明

当前系统已进入更严格的真实模式：
- 环境名：`akq310`
- Python：`3.10.20`
- 已安装：`akshare 1.18.64`、`akquant 0.2.45`
- 配置：`strict_real_services: true`

## 当前含义

这表示：
- 优先使用真实 `AKShare` 数据
- 优先使用真实 `akquant` 回测
- 如果真实服务调用失败，将直接报错，而不是静默回退

## 推荐解释器

```bash
/opt/anaconda3/envs/akq310/bin/python
```

## 自检

```bash
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app doctor
```

## 单次运行

```bash
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app run-once
```

## 查询与分析

```bash
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app query --section all --limit 5
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app analyze
```

## 当前增强

### AKShare 实时因子增强

已加入或增强：
- `momentum_5`
- `momentum_20`
- `momentum_60`
- `reversal_5`
- `volatility_20`
- `turnover_ratio`
- `amplitude_20`
- `close_to_high`
- `volume_trend`
- `intraday_range`
- `value_score`
- `quality_score`
- `size_score`

实现位置：
- `/Users/fengbojing1/Documents/A/src/akq_agents/services/akshare_service.py:1`

### akquant 真实适配层

已接入：
- `akquant.backtest.run_backtest`
- `BacktestResult.daily_returns`
- `BacktestResult.equity_curve`
- `BacktestResult.metrics`
- `BacktestResult.trades`

实现位置：
- `/Users/fengbojing1/Documents/A/src/akq_agents/services/backtest_service.py:1`

## 下一步建议

- 增加真实财务/估值接口
- 增加交易日历、停牌过滤、涨跌停过滤
- 增加组合持仓延续与调仓约束
- 做 24 小时连续调度验证
