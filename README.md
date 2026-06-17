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

```bash
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app doctor
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app run-once
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app query --section all --limit 5
PYTHONPATH=/Users/fengbojing1/Documents/A/src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app analyze
```

## 当前能力

- `AKShare` 真实数据路径已可用
- `akquant` 真实回测适配层已接入
- 多 Agent 工作流、日报、数据库、风控、CLI 全部可运行

## 关键文件

- 配置：`/Users/fengbojing1/Documents/A/config/system.yaml:1`
- 回测适配：`/Users/fengbojing1/Documents/A/src/akq_agents/services/backtest_service.py:1`
- 半实盘说明：`/Users/fengbojing1/Documents/A/docs/semi_live.md:1`
