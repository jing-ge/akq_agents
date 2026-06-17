# P4 LLM Agent 层使用指南

> 对应设计文档：`docs/superpowers/specs/2026-06-17-p4-llm-agent-design.md`

## 1. 模块总览

```
src/akq_agents/
├── agents/
│   ├── analyst_agent.py        # 盘后 LLM 简评（无 ToolUse）
│   ├── chat_agent.py           # CLI REPL（带 ToolUse loop）
│   └── prompts/
│       ├── analyst.md          # AnalystAgent system prompt
│       └── chat_system.md      # ChatAgent system prompt
├── models/
│   └── llm_config.py           # LLMConfig 加载 config/llm.yaml
└── services/
    └── llm/
        ├── __init__.py
        ├── client.py           # LLMClient protocol + GatewayLLMClient (Anthropic only)
        ├── orchestrator.py     # LLMOrchestrator (analyst 单次 / chat tool loop)
        ├── store.py            # llm_calls + chat_messages 表读写
        └── tools/
            ├── registry.py     # ToolRegistry + ToolSpec (启动期强校验 read_only)
            └── builtin.py      # 4 个 builtin 工具构造器
```

入口配置：`config/llm.yaml`  
入口模型：`src/akq_agents/models/llm_config.py:LLMConfig`

## 2. 设计要点（spec v2 收敛后）

- **只支持 Anthropic 协议**（POST `127.0.0.1:18931/anthropic/v1/messages`）；OpenAI fallback 已砍
- **AnalystAgent 不使用 ToolUse**：盘后 context 已含 portfolio + attribution + DataHealth + events，直接拿数据写文章
- **ChatAgent 使用 ToolUse**：4 个只读工具
- **启动期强校验 read_only=True**：违反 read-only 的工具直接 raise（spec A5 验收）
- **基础设施层（Orchestrator 写 llm_calls / chat_messages）不受 read-only 约束**
- 砍掉：cost/budget/rate-limit 体系、prompt 版本号、关键词黑名单扫描

## 3. CLI 速查

```bash
# 启动 ChatAgent REPL
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app chat

# 看最近 LLM 调用
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app llm calls --last 20

# 仅看 analyst 调用
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app llm calls --agent analyst

# 看最近 chat sessions
PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python -m akq_agents.cli.app llm sessions
```

## 4. 数据流

### AnalystAgent（盘后自动调用）

```
batch.post_close
  → ... (P3 portfolio pipeline) ...
  → AdvisorAgent
  → AnalystAgent  ← 这里
      ├ 读 context.state（portfolio / attribution / data_health）
      ├ 取 top 20 持仓 + 近 10 条 events → payload
      ├ 调 LLMOrchestrator.run_analyst（**不传 tools**）
      ├ 成功 → 写 reports/<date>/analyst_brief.md，首行 disclaimer
      └ 失败 → 走模板版本，status='degraded'，**不阻塞 batch**
  → ReportAgent
```

### ChatAgent（CLI 交互）

```
akq-agents chat
  → 生成 session_id
  → 写 system message
  → 写 events(kind=chat.session.created)
  → REPL：
      user > 今天数据怎么样？
      → run_chat_turn：
            tool loop:
              LLM 返回 tool_use(get_data_health) →
                ToolRegistry.invoke(get_data_health) →
                返回结果作为 tool_result block →
                next LLM iteration →
              LLM 返回 end_turn 文本
            (最多 max_iterations=6 轮)
      ← 助手输出
      /quit 退出
```

## 5. 4 个 builtin 工具

| 工具名 | 描述 | 入参 |
|---|---|---|
| `get_data_health` | P1 DataHealth | `{}` |
| `list_factors` | 所有因子 + 最近 metrics | `{}` |
| `get_portfolio_snapshot` | 某日组合快照 | `{"date": "YYYY-MM-DD"}` |
| `query_events` | 调度器事件流 | `{kind_prefix?, since?, level_min?, limit?}` |

所有工具：
- **read-only**（启动期强校验，违反则进程拒启）
- **truncate_chars=8000**（序列化后超长自动截断，标记 `_truncated`）
- **JSON Schema 入参校验**（缺 required / 类型不对 → 返回 `INVALID_ARGUMENTS` 给 LLM，让它自己改）

## 6. 表 DDL

```sql
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  agent TEXT NOT NULL,             -- analyst | chat
  session_id TEXT,
  model TEXT NOT NULL,
  prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER,
  tool_calls INTEGER DEFAULT 0,
  status TEXT NOT NULL,            -- ok | failed | truncated
  reason_code TEXT,                -- TIMEOUT | UPSTREAM_ERROR | RATE_LIMITED | TOOL_LOOP_EXCEEDED
  error_msg TEXT
);

CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,              -- system | user | assistant | tool
  content TEXT NOT NULL,
  tool_name TEXT, tool_args TEXT, tool_result TEXT,
  tokens INTEGER
);
```

> **session 元信息**（创建时间）以 session 内第一条 role='system' message 的 ts 隐式表达；不另起 chat_sessions 表（spec v2 收敛）。

## 7. 配置

```yaml
# config/llm.yaml
llm:
  gateway:
    base_url: "http://127.0.0.1:18931"
    anthropic_path: "/anthropic/v1/messages"
    timeout_s: 60
    max_retries: 2
  default_model: "Claude-Opus-4.7"
  analyst:
    enabled: true
    model: "Claude-Opus-4.7"
    max_tokens: 4000
    temperature: 0.2
    context_top_holdings: 20        # top 20 持仓 → prompt
    context_events_count: 10
  chat:
    enabled: true
    model: "Claude-Opus-4.7"
    max_tokens: 2000
    temperature: 0.4
    max_iterations: 6                # ToolUse 循环上限
    history_window: 20
  safety:
    disclaimer_header: "本报告由 LLM 生成，仅供研究参考，不构成投资建议；系统不执行任何交易指令。"
```

## 8. 安全模型

1. **工具只读**：启动期强校验 `read_only=True`；任何 write 工具直接 raise，进程拒启
2. **报告头部 disclaimer**：分析师简评首行硬注入 disclaimer
3. **LLM 无 trade 工具**：4 个 builtin 工具中没有任何"下单/委托"工具；LLM 无法触发交易
4. **traceback 不外泄**：工具异常返回 `{"error": "INTERNAL", "message": "..."}`，traceback 仅写 server log
5. **token 上限**：max_tokens 强制 + 单工具 truncate_chars + max_iterations 兜底

> v1 中的"关键词黑名单扫描"已砍（误伤率高、易绕过）；仅靠 prompt 强约束 + disclaimer。

## 9. 故障排查

### `chat` REPL 启动失败 "LLM 未装配"
- 检查 `config/data.yaml` 是否存在（P4 依赖 P1 装配）
- 检查 `config/llm.yaml`；无则用默认

### 工具调用都返回 `INVALID_ARGUMENTS`
- 看 detail 字段；常见是 LLM 没传 required 字段
- prompt 里描述工具签名是否清晰

### `llm calls --last 10` 大量 `status='failed' reason_code='UPSTREAM_ERROR'`
- 检查本地代理 `http://127.0.0.1:18931/` 是否在跑
- 看 `error_msg` 字段；常见网络层问题

### `llm calls` 大量 `truncated reason_code='TOOL_LOOP_EXCEEDED'`
- LLM 进入工具调用死循环；检查 prompt 是否清晰、工具描述是否准确
- 可临时调小 `chat.max_iterations`

### AnalystAgent 大量 `status='degraded'`
- LLM 网关不稳定；批次任务不阻塞，但报告内容是模板版（events kind `analyst.brief.degraded`）

## 10. 验收快查

| Spec | 状态 | 验证方式 |
|---|---|---|
| A1 chat 多轮对话含 ToolUse | ✅ | `tests/llm/test_llm.py::test_orchestrator_chat_tool_loop_completes` |
| A2 AnalystAgent 写 markdown 首行 disclaimer | ✅ | `test_analyst_agent_writes_report` |
| A3 LLM down → degraded、batch 不挂 | ✅ | `test_analyst_agent_fallback_on_llm_failure` |
| A4 工具异常返回结构化 error | ✅ | `test_registry_invoke_handler_exception_returns_internal` |
| A5 启动期 read_only=False 直接 raise | ✅ | `test_registry_rejects_non_read_only` |
| A6 AnalystAgent **不传 tools** | ✅ | `test_orchestrator_analyst_no_tools_passed` |
| A7 `llm calls` 无 cost 列 | ✅ | LLMCall dataclass 无 cost 字段 |
| A9 max_iterations 截断 | ✅ | `test_orchestrator_chat_max_iterations_truncates` |
| A10 上下文裁剪 top 20 | ✅ | `test_analyst_agent_truncates_top_holdings` |
| A11 events.kind 符合 P2 附录 C | ✅ | `chat.session.created` 已注册 |
| B1 覆盖率 ≥ 75% | ✅ 81% | `pytest --cov=akq_agents.services.llm` |
| B2 ruff 0 warnings | ✅ | `ruff check src/ tests/` |
| B4 无写工具 | ✅ | grep `read_only=False` 命中数 = 0 |
| B5 prompt 文件非空 | ✅ | `analyst.md` + `chat_system.md` |

## 11. 与 P1/P2/P3 接口承诺 / 与 P5 接口承诺

详见 spec 附录 A / B。关键：
- 依赖 P1/P2/P3：read-only 消费 DataHealth / events / job_runs / factor_metrics / portfolio_snapshots
- 承诺给 P5：`llm_calls` / `chat_messages` schema 稳定；`LLMOrchestrator.run_chat_turn` 是 Web Chat 后端的同一入口；不另起 LLM 链路
- **P5 SSE 实现策略**：P4 不实现 streaming，P5 接受"等完整响应再 send 一次 SSE done"（spec 附录 B §4 已明文承诺）
