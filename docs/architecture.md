# 系统设计

> ⚠️ **历史设计稿（已过时）**：本文档描述的是项目早期设想的 6-Agent 流水线
> （DataAgent → FactorAgent → BacktestAgent → ResearchAgent → PortfolioAgent → AdvisorAgent），
> 这些装饰性 Agent 在 m15 里程碑已被删除。当前**实际架构**是 web + daemon 双进程
> + SQLite/Parquet 存储，实际存在的 Agent 只有 `BaseAgent` / `AnalystAgent` /
> `PortfolioAgent` / `ChatAgent` / `StockAnalystAgent`。
> **以 README.md「系统架构」章节为准**，下文仅作历史归档参考。

## 目标

构建一个长期运行的多 Agent 量化研究系统，使其可以：
- 持续拉取数据
- 自动生成和更新因子
- 持续回测和评估
- 组合优选
- 每日输出操盘建议

## 工作流

```text
AKShare -> DataAgent -> FactorAgent -> BacktestAgent -> ResearchAgent -> PortfolioAgent -> AdvisorAgent
```

## Agent 职责

### DataAgent
- 获取 OHLCV、行业、估值、财务、资金流、情绪等数据
- 标准化数据格式
- 将结果交给因子层

### FactorAgent
- 维护候选因子注册表
- 计算因子值
- 输出每个标的的综合因子快照

### BacktestAgent
- 将单因子/因子组合映射为可回测策略
- 跑历史回测
- 产出关键绩效指标

### ResearchAgent
- 根据阈值筛选有效因子
- 淘汰表现不稳定的因子
- 记录因子研究结论

### PortfolioAgent
- 对通过筛选的因子加权组合
- 形成标的评分与推荐仓位

### AdvisorAgent
- 综合研究和组合结果
- 输出日级建议与风险提示

## 部署建议

### 最小可用版本
- Python + APScheduler
- YAML 状态文件
- 本地日志

### 生产版本
- Postgres 存储
- Redis 队列
- Celery / Prefect / Airflow 调度
- FastAPI + 前端看板
- LLM 总结报告与消息推送
