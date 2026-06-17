# 下一步接入建议

## 1. 真实 AKShare 数据

建议优先补充：
- 日线行情
- 指数成分股
- 财务指标
- 估值指标
- 资金流向
- 宏观和情绪代理变量

## 2. 因子库扩展

当前已实现：
- 动量
- 低波
- 价值占位
- 质量占位
- 流动性

下一步建议加入：
- ROE
- PB/PE 分位数
- 60 日动量
- 5 日反转
- 行业中性化处理

## 3. akquant 接入点

建议把真实回测补到：
- `src/akq_agents/services/backtest_service.py`

统一输出：
- 年化收益
- 夏普
- 最大回撤
- 胜率
- 综合评分

## 4. 本地验证

当前如果环境还没装依赖，先执行：

```bash
pip install -r requirements.txt
```

再执行：

```bash
python scripts/run_once.py
```

运行后检查：
- `runtime_state.yaml`
- `akq_agents.db`

## 5. 生产化建议

- 用 `systemd` 或 `supervisor` 守护进程
- 增加异常告警
- 增加数据质量校验
- 增加风控与交易执行模块
