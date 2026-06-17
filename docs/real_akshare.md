# 真实 AKShare 接入说明

## 目标

将当前 mock 数据切换为真实 A 股行情数据，并逐步补充估值、财务与行业因子。

## 当前实现位置

- `/Users/fengbojing1/Documents/A/src/akq_agents/services/akshare_service.py:1`

## 当前已支持

- 逐只股票调用 `stock_zh_a_hist`
- 拉取指定回看窗口日线数据
- 计算基础 `momentum_20`
- 计算基础 `volatility_20`

## 切换方式

把配置里的：

```yaml
services:
  use_mock_data: false
```

并确保环境安装：

```bash
/opt/anaconda3/envs/ab/bin/python -m pip install akshare
```

## 下一步建议补充的真实因子

- 价值：PE、PB、股息率
- 质量：ROE、净利率、现金流质量
- 成长：营收增速、利润增速
- 情绪：资金流、热度、换手率
- 行业：行业相对强弱、行业中性化

## 注意事项

- AKShare 接口存在字段变化风险，建议增加字段映射层
- 对停牌、ST、退市风险标的需要额外过滤
- 真正实盘前建议缓存历史数据，避免高频重复请求
