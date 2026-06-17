# AKShare + akquant 智能 AI 量化 Agent 协作系统

这是一个可扩展的多 Agent 量化研究系统，现已具备可运行的真实研究环境。

## 推荐环境

- conda 环境：`akq310`
- Python：`3.10.20`
- 已安装：`akshare 1.18.64`、`akquant 0.2.45`

解释器路径：

```bash
/opt/anaconda3/envs/akq310/bin/python
```

## 常用命令

> 以下命令假设你已 `cd` 到项目根目录（含 `pyproject.toml` 的目录）。
> 使用 conda 环境 `akq310`，Python 路径见下方。

```bash
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app doctor
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app run-once
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app query --section all --limit 5
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app analyze
```

## 当前能力

- `AKShare` 真实数据路径已可用
- `akquant` 真实回测适配层已接入
- 多 Agent 工作流、日报、数据库、风控、CLI 全部可运行
- **P1 数据层**：全 A 股动态股票池 + Parquet 缓存 + 限频/重试/质量门 + 交易日历感知

## 关键文件

- 配置：`config/system.yaml`、`config/data.yaml`
- 数据层（P1）：`src/akq_agents/services/data/`、`docs/data_layer.md`
- 回测适配：`src/akq_agents/services/backtest_service.py`
- 半实盘说明：`docs/semi_live.md`

## P1 数据层快速上手

```bash
# 健康检查（不联网）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data status

# 全量回填历史（联网；首跑 ~4-6 小时）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data bootstrap --lookback 250

# 增量刷新当日（联网；< 30 分钟）
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data refresh

# 查看单股缓存
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app data inspect 600519
```

详见 `docs/data_layer.md`。

## 开发命令

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/ -q
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/data/ --cov=akq_agents.services.data
/opt/anaconda3/envs/akq310/bin/python -m ruff check src/ tests/
```
