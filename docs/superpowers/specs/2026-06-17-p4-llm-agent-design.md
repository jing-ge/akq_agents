# P4 LLM Agent 层 — 设计文档

- 项目：akq-agents
- 阶段：P4（共 P1–P6 六阶段中的第四阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（DataRepository / DataHealth）、P2（events / job_runs）、P3（FactorRegistry / portfolio_snapshots / attribution.json）

---

## §1 目标与边界

### 目标

把"机械计算 + 模板渲染"的 Advisor / Report 升级为**LLM 驱动的 AnalystAgent + ChatAgent**：

- **AnalystAgent（盘后离线）**：读取当日 portfolio + attribution + factor_metrics + universe，调 LLM 生成"今日盘后简评 + 风险提示 + 个股点评"，写 `reports/YYYY-MM-DD/analyst_brief.md`。
- **ChatAgent（盘中/任意时间，交互式）**：CLI 或 Web 起一个对话会话；用户问"今天为什么选 600519？"、"momentum_20 这周表现怎么样？"、"昨天为什么没买 000001？"，LLM 通过 **ToolUse** 调取系统内数据回答。
- 工具调用层（Tools）：只读访问 Repository / FactorRegistry / portfolio_snapshots / events / job_runs。严格只读、严格 schema。
- LLM 网关抽象：走本地 `http://127.0.0.1:18931`（多 provider 网关，已就绪），统一 `LLMClient` 接口，默认模型 `Claude-Opus-4.7`，可切换。

### 在做什么（P4 范围）

- `LLMClient` 协议 + `GatewayLLMClient`（127.0.0.1:18931）；默认走 `/anthropic/v1/messages`（带 ToolUse），fallback `/v1/chat/completions`（OpenAI 兼容）。
- `Tools` 注册表：所有 LLM 可调工具显式注册，统一 JSON Schema；调用都过 RBAC（只读）。
- `AnalystAgent`：盘后被 `batch.post_close` 调用，写 markdown 报告。
- `ChatAgent`：CLI 入口 `akq-agents chat`；支持多轮、自动 ToolUse、对话记录写 sqlite。
- 提示工程：system prompt + few-shot examples 放在 `src/akq_agents/agents/prompts/`，版本化。
- 成本与限流：每次 LLM 调用记 `llm_calls` 表（tokens / latency / cost / model），全局并发限流。
- 安全：所有工具都是 **read-only**；LLM 不能下单、不能改库、不能调用 AKShare 直接拉数据（必须经 Repository）。

### 不在做什么

- ❌ Agent 自动下单 / 触发交易 — **永远禁止**。
- ❌ LLM 直接写库 / 改配置。
- ❌ 训练 / 微调本地模型。
- ❌ 多 Agent 协同（autogen / crewAI 风格）— YAGNI，AnalystAgent 与 ChatAgent 是两个独立流程。
- ❌ 向量库 / RAG — 当前数据规模小，结构化查询就够；如需要留到 P4.5。
- ❌ Web 聊天 UI（P5）。
- ❌ 多模态（图像 / 文档解读）。

---

## §2 架构

### 整体拓扑

```
┌──────────────────────────────────────────────────────────────────┐
│  AnalystAgent (offline, called by batch.post_close)                │
│  ChatAgent    (interactive, CLI / future Web)                       │
└────┬──────────────────────────────────┬──────────────────────────┘
     │                                  │
     ▼                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  LLMOrchestrator                                                    │
│  - prompt assembly (system + few-shot + user)                       │
│  - tool loop: model 想调工具 → ToolRegistry.invoke → 把结果塞回上下文 │
│  - max_iterations 防失控（默认 6）                                   │
│  - 成本记账 → llm_calls 表                                          │
└────┬──────────────────────────────────┬──────────────────────────┘
     │                                  │
     ▼                                  ▼
┌────────────────────┐         ┌────────────────────────────────────┐
│   LLMClient        │         │   ToolRegistry (read-only)         │
│   - GatewayClient  │         │   - get_data_health                │
│     (127.0.0.1:    │         │   - list_factors                   │
│      18931)        │         │   - get_factor_metric              │
│   - 路由：          │         │   - get_portfolio_snapshot         │
│     /anthropic/... │         │   - explain_portfolio              │
│     /v1/chat/...   │         │   - query_events                   │
│   - 重试 + 限频    │         │   - get_ohlcv_summary              │
│   - 流式可选       │         │   - get_universe_summary           │
└────────────────────┘         └────────────────────────────────────┘
```

### LLM 网关现状（已就绪）

本地代理 `http://127.0.0.1:18931`：

```json
{
  "status": "ok",
  "models": ["GPT-5.4", "DeepSeek-V4-Pro", "GLM-5.1",
             "Gemini-3.1-Pro-Preview", "Claude-Opus-4.7",
             "GPT-5.5-joybuilder"],
  "routes": {
    "Claude-Opus-4.7":    {"upstream":"anthropic","path":"/anthropic/v1/messages"},
    "GPT-5.4":            {"upstream":"openai",   "path":"/v1/chat/completions"},
    "Gemini-3.1-Pro-Preview":{"upstream":"gemini","path":"/v1/responses"},
    ...
  }
}
```

设计决策：
- **默认模型**：`Claude-Opus-4.7`（强 ToolUse、强中文），走 `/anthropic/v1/messages`。
- **fallback 模型**：`GPT-5.4`（兼容 OpenAI ToolCalls），走 `/v1/chat/completions`。
- 两套协议在 `LLMClient` 内部隔离，对上层 `LLMOrchestrator` 暴露统一 `chat(messages, tools=[...]) -> Message`。

### 存储扩展（追加 `meta.db`）

```sql
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  agent TEXT NOT NULL,             -- "analyst" | "chat"
  session_id TEXT,
  model TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms INTEGER,
  cost_usd REAL,                   -- 占位字段，无法判断时填 NULL
  tool_calls INTEGER DEFAULT 0,
  status TEXT NOT NULL,            -- ok | failed | truncated
  reason_code TEXT,                -- TIMEOUT | UPSTREAM_ERROR | RATE_LIMITED
  error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);

CREATE TABLE IF NOT EXISTS chat_sessions (
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  last_active_at TEXT NOT NULL,
  user_label TEXT,                 -- e.g. CLI 启动时的标签
  model TEXT NOT NULL,
  status TEXT NOT NULL             -- active | closed
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,              -- user | assistant | tool
  content TEXT NOT NULL,           -- markdown / json
  tool_name TEXT,                  -- role=tool 时填
  tool_args TEXT,                  -- JSON
  tokens INTEGER,
  FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_sid ON chat_messages(session_id, ts);
```

### 配置示例（`config/llm.yaml`，新增）

```yaml
llm:
  gateway:
    base_url: "http://127.0.0.1:18931"
    timeout_s: 60
    max_retries: 2
    qps_limit: 2          # 全局并发；超出直接 sleep
  default_model: "Claude-Opus-4.7"
  fallback_model: "GPT-5.4"
  analyst:
    enabled: true
    model: "Claude-Opus-4.7"
    max_tokens: 4000
    temperature: 0.2
    prompt_version: "v1"
  chat:
    enabled: true
    model: "Claude-Opus-4.7"
    max_tokens: 2000
    temperature: 0.4
    max_iterations: 6     # ToolUse 循环上限
    prompt_version: "v1"
  cost:
    log_calls: true
    daily_budget_usd: 10  # 占位；超出仅告警不阻断
  safety:
    tools_read_only: true # 不允许写工具注册
    refuse_trade_orders: true
```

---

## §3 数据流与时序

### 流程 1：AnalystAgent（盘后离线）

```
P2 batch.post_close（已含 P3 portfolio pipeline）
  → 最后一步：AnalystAgent.run(context)
        - 读 context["portfolio"], ["attribution"], ["data_health"]
        - 调 ToolRegistry.preflight_check（验证关键数据可读）
        - 构造 prompt:
            system: 你是量化研究员…
            assistant_context: 今日组合 + 归因 + 数据健康 + 近 7 天 events
            user: "请用 5 段以内写一份盘后简评。结构：数据状态/组合概览/重点持仓/风险点/明日关注。"
        - llm.chat(model=Claude-Opus-4.7, tools=[get_factor_metric, get_ohlcv_summary, ...])
        - LLM 循环 ToolUse ≤ 6 次
        - 收尾：写 reports/YYYY-MM-DD/analyst_brief.md
        - 写 llm_calls + events(kind="analyst.brief.ready")
  失败路径：
    - LLM 超时 / 网关 5xx → 退化到现有模板渲染（AdvisorAgent），不阻断 batch
    - tool 调用异常 → 记 llm_calls.tool_calls + status=truncated；交付 LLM 已有内容
```

### 流程 2：ChatAgent（CLI 交互）

```
akq-agents chat [--model Claude-Opus-4.7] [--session <id>]
  → 启动 REPL：
        - 新建 chat_sessions 行 或 加载已有
        - 渲染 system prompt（注入：可用工具清单 + 数据范围说明 + 时间）
        - 显示提示："当前数据日期：2026-06-17｜组合：N 只｜可问：因子/组合/风险/事件"
  user >  今天为什么选 600519？
  → LLMOrchestrator.handle(user_msg)
        loop iter <= max_iterations:
            assistant_msg = llm.chat(history + tools)
            if assistant_msg.tool_calls:
                for tc in tool_calls:
                    result = ToolRegistry.invoke(tc.name, tc.arguments)
                    history += tool_msg(result)
            else:
                break
        return assistant_msg.content
  user >  最近 30 天 events 里有几个 batch.failed？
  → 调 query_events(kind="*.failed", since=now-30d) → LLM 总结
  user >  /quit
  → 关闭 session，写 status='closed'
```

### 流程 3：ToolUse 安全模型

每个工具有 5 个属性：
```python
@dataclass
class ToolSpec:
    name: str                  # e.g. "get_factor_metric"
    description: str           # LLM 可见
    json_schema: dict          # 入参 JSON Schema
    read_only: Literal[True]   # P4 强制
    handler: Callable[[dict], dict]
    rate_limit_per_session: int = 30   # 单 session 每分钟上限
```

工具调用前置检查：
1. `read_only=True`（启动时校验注册表，违反则 daemon 拒启）。
2. 入参 schema 校验失败 → 返回 `{"error": "INVALID_ARGUMENTS"}` 给 LLM，不抛异常。
3. 单 session 工具调用计数超限 → 返回 `{"error": "RATE_LIMITED"}`。
4. 工具执行异常 → 返回 `{"error": "INTERNAL", "message": "..."}`，不泄露 traceback 给 LLM。

### 流程 4：核心工具清单

| 工具名 | 描述 | 入参 | 返回 |
|---|---|---|---|
| `get_data_health` | 当前数据层健康状态 | `{}` | `DataHealth` dict |
| `list_factors` | 列出所有因子及 status | `{"only_active": bool}` | `[{name, status, last_ir, ...}]` |
| `get_factor_metric` | 单因子最近 N 期 metrics | `{"name", "window_days", "limit"}` | `[{as_of_date, ic, ir, ...}]` |
| `get_portfolio_snapshot` | 某日组合快照 | `{"date"}` | `[{symbol, weight, score, contribution}]` |
| `explain_portfolio` | 某日组合归因摘要 | `{"date"}` | `attribution.json` payload |
| `query_events` | 事件查询 | `{"kind", "since", "until", "limit"}` | `[Event]` |
| `get_ohlcv_summary` | 单股近 N 日 OHLCV 摘要（非全量，避免 token 爆炸） | `{"symbol", "days"}` | `{last_close, return_5d, return_20d, vol_20d, max_dd_60d}` |
| `get_universe_summary` | 某日 universe 大小 + 排除原因分布 | `{"date"}` | `{total, included, excluded_by_reason}` |
| `get_job_runs` | 最近调度任务 | `{"job_id", "limit"}` | `[JobRun]` |

**总 token 控制**：每个工具返回内置 `truncate_to_tokens` 限制（默认 1500 tokens）；超长用统计摘要替代原始数据。

### 流程 5：失败与降级

```
LLM 超时（>60s）→ 重试 1 次 → 仍超时
  AnalystAgent: 走 AdvisorAgent 模板渲染（沿用现有）
  ChatAgent:    回复 "LLM 网关暂时不可用，请稍后重试或切换模型 (/model GPT-5.4)"

工具执行异常
  返回 {"error": "INTERNAL"} 给 LLM；记 events(kind="llm.tool.error")
  LLM 通常会道歉并尝试别的方式

LLM 试图调用未注册工具
  返回 {"error": "TOOL_NOT_FOUND"}；记 events(kind="llm.unknown_tool", payload={tool})

LLM 输出含 "买入/卖出/下单/委托" 等关键词（safety.refuse_trade_orders=true）
  在 AnalystAgent 输出后做关键词扫描；命中则追加风险提示且 events(kind="llm.trade_intent_blocked")
```

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── agents/
│   ├── analyst_agent.py            ← 新增
│   ├── chat_agent.py               ← 新增
│   └── prompts/                     ← 新增
│       ├── analyst_v1.md
│       ├── chat_system_v1.md
│       └── few_shot/
│           ├── factor_question.json
│           └── portfolio_question.json
├── services/
│   └── llm/                         ← 新增子包
│       ├── __init__.py
│       ├── client.py                # LLMClient protocol + GatewayLLMClient
│       ├── orchestrator.py          # LLMOrchestrator (tool loop)
│       ├── tools/                   # ToolRegistry + 各工具实现
│       │   ├── __init__.py
│       │   ├── registry.py
│       │   ├── data_tools.py        # get_data_health, get_universe_summary, get_ohlcv_summary
│       │   ├── factor_tools.py      # list_factors, get_factor_metric
│       │   ├── portfolio_tools.py   # get_portfolio_snapshot, explain_portfolio
│       │   └── ops_tools.py         # query_events, get_job_runs
│       ├── store.py                 # llm_calls / chat_sessions / chat_messages 读写
│       ├── safety.py                # refuse_trade_orders 扫描
│       └── pricing.py               # token → cost_usd 占位映射
├── models/
│   └── llm_config.py                # LLMConfig (pydantic)
├── orchestrator/
│   └── jobs/
│       └── batch_post_close.py      # 末尾注入 AnalystAgent.run
└── cli/
    └── app.py                       # 新增子命令：chat / llm
                                       - chat                       (REPL)
                                       - llm calls --last N
                                       - llm sessions --status active
                                       - llm cost --since YYYY-MM-DD
```

### LLMClient 协议

```python
@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]    # Anthropic 风格 blocks
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None

class LLMClient(Protocol):
    def chat(self, *,
             model: str,
             messages: list[LLMMessage],
             tools: list[ToolSpec] | None = None,
             max_tokens: int,
             temperature: float,
             timeout_s: int) -> LLMResponse:
        """统一接口；内部根据 model→upstream 路由到 anthropic / openai / gemini 协议"""

class GatewayLLMClient:
    """实现：根据 base_url + /healthz 获取路由表，按模型选择协议适配器"""
    def __init__(self, cfg: LLMGatewayConfig): ...
```

### LLMOrchestrator

```python
class LLMOrchestrator:
    def __init__(self, client: LLMClient, tools: ToolRegistry, store: LLMStore,
                 safety: SafetyChecker, max_iterations: int = 6): ...

    def run(self, *,
            agent: Literal["analyst", "chat"],
            session_id: str,
            system_prompt: str,
            user_message: str,
            history: list[LLMMessage] | None = None,
            model: str,
            max_tokens: int,
            temperature: float) -> OrchestratorResult:
        """
        - 拼装 messages（system + history + user）
        - 工具循环 ≤ max_iterations
        - 把每轮 input/output 写 llm_calls
        - 终止条件：(a) no tool_calls in response, (b) iter > max
        """
```

### Tool 注册

```python
class ToolRegistry:
    def register(self, spec: ToolSpec) -> None:
        assert spec.read_only is True, "P4 only allows read-only tools"
        self._tools[spec.name] = spec

    def invoke(self, name: str, args: dict, *, session_id: str) -> dict:
        """schema validate → handler → truncate → 返回"""
```

启动时 `services/llm/tools/__init__.py` 注册所有工具；进程内单例。

### AnalystAgent 集成

```python
# agents/analyst_agent.py
class AnalystAgent(BaseAgent):
    name = "analyst-agent"
    def __init__(self, orchestrator: LLMOrchestrator, cfg: AnalystConfig,
                 reports_dir: Path, repo: DataRepository): ...

    def run(self, context: AgentContext) -> dict:
        if not self.cfg.enabled:
            return {"status": "skipped", "reason": "disabled"}
        prompt = load_prompt("analyst", self.cfg.prompt_version)
        ctx_payload = self._build_context_payload(context)
        try:
            result = self.orchestrator.run(
                agent="analyst",
                session_id=f"analyst:{date.today().isoformat()}",
                system_prompt=prompt.system,
                user_message=prompt.user_template.format(**ctx_payload),
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
            )
        except LLMGatewayError as e:
            logger.warning("LLM 不可用，退化到模板", exc_info=e)
            return {"status": "degraded", "reason": "llm_unavailable"}
        report_path = self.reports_dir / date.today().isoformat() / "analyst_brief.md"
        report_path.write_text(result.content)
        return {"status": "ok", "path": str(report_path)}
```

### ChatAgent（CLI REPL）

```python
class ChatAgent:
    def __init__(self, orchestrator: LLMOrchestrator, cfg: ChatConfig, store: LLMStore): ...

    def repl(self) -> None:
        session = self.store.new_session(model=self.cfg.model)
        print_banner(session)
        history = [LLMMessage(role="system", content=load_prompt("chat", self.cfg.prompt_version).system)]
        while True:
            user_input = input("> ").strip()
            if user_input in {"/quit", "/exit"}: break
            if user_input.startswith("/model "):
                self.cfg.model = user_input.split()[1]; continue
            history.append(LLMMessage(role="user", content=user_input))
            result = self.orchestrator.run(
                agent="chat", session_id=session.session_id,
                system_prompt=history[0].content,
                user_message=user_input,
                history=history,
                model=self.cfg.model, ...
            )
            history.extend(result.new_messages)
            print(result.content)
        self.store.close_session(session.session_id)
```

### 关键边界

- ❌ 任何工具都不能写库（启动期 read_only 强制校验）。
- ❌ LLM 不能直接调 AKShare（无 fetch_* 工具）；想要数据必须通过 Repository / 已缓存数据。
- ❌ AnalystAgent 失败不阻塞 batch（degraded 状态）。
- ❌ 没有 vector store / RAG（YAGNI）。
- ❌ 不缓存 LLM 响应（每次请求重新算；如需缓存留到 P4.5）。
- ❌ 不实现 streaming UI（CLI 先按整条返回；P5 Web 再做 SSE）。

### 测试策略

```
tests/llm/
├── test_gateway_client.py        # mock 127.0.0.1:18931；anthropic + openai 路径
├── test_orchestrator.py          # tool loop、max_iterations、错误降级
├── test_tools_*.py               # 每个工具入参 schema + 返回 truncation
├── test_safety.py                # trade_intent 关键词扫描
├── test_analyst_agent.py         # mock LLM → 写 report
├── test_chat_agent.py            # mock LLM REPL 走通 3 轮
└── fixtures/
    ├── gateway_anthropic_resp.json
    └── gateway_openai_resp.json
```

目标覆盖率：**`services/llm/` ≥ 75%**（IO 多，比 P3 略低）。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents chat` 启动后能完成 3 轮对话，至少 1 轮含 ToolUse | 人工验证 |
| A2 | `batch.post_close` 跑完后写出 `reports/YYYY-MM-DD/analyst_brief.md`，含非空 5 段结构 | 文件检查 |
| A3 | LLM 网关 down 时 AnalystAgent 返回 `degraded`，batch 不失败 | mock 网关 5xx |
| A4 | 工具执行异常返回结构化 `{"error": ...}`，LLM 看到后继续 | 单测 |
| A5 | 启动时尝试注册 write 工具直接 raise；daemon 拒启 | 单测 |
| A6 | LLM 输出含"建议买入/下单" → safety 追加风险提示 + events 记录 | 集成测 |
| A7 | `llm calls --last 10` 输出表格含 model/tokens/latency/cost | CLI 验证 |
| A8 | `/model GPT-5.4` 切换模型后继续对话不丢历史 | 人工验证 |
| A9 | 工具单 session 60 秒内 30 次以上调用返回 `RATE_LIMITED` | 单测 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/llm/` 覆盖率 ≥ 75% | `pytest --cov` |
| B2 | `ruff check` 零警告 | CI |
| B3 | 所有工具有 description + JSON Schema | review |
| B4 | prompt 文件版本化（analyst_v1.md），更换版本配置即可切换 | 配置 + 文件 |
| B5 | 无任何工具有写权限（grep 检测：`read_only=False` 命中数 = 0） | grep |

### C. 性能 & 成本验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | AnalystAgent 一次生成 ≤ 90 秒（不含 ToolUse 最多 6 轮） | events 时间统计 |
| C2 | ChatAgent 单轮回复 P95 ≤ 15s | llm_calls 表统计 |
| C3 | 单日 LLM 总成本 ≤ 配置 `daily_budget_usd`（占位字段，无价时 NULL） | llm_calls 表 sum |
| C4 | LLM 失败不影响盘后 batch 总耗时 > +5 分钟 | 实测 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/llm_agents.md`：架构、工具清单、prompt 版本管理、故障排查、安全模型 |
| D2 | `docs/prompts.md`：每个 prompt 文件说明与变量字典 |
| D3 | README 增加 `chat` / `llm calls` / `llm sessions` 命令 |

### 里程碑参考

- M4.1 LLMClient + Gateway 适配（Anthropic + OpenAI 两条协议）（1–2 天）
- M4.2 ToolRegistry + 8 个核心工具（每工具单测）（2 天）
- M4.3 LLMOrchestrator + tool loop + llm_calls 表（1–2 天）
- M4.4 SafetyChecker + 关键词扫描 + prompt 版本管理（0.5–1 天）
- M4.5 AnalystAgent + analyst_v1 prompt + 集成到 batch（1 天）
- M4.6 ChatAgent + CLI REPL + chat_sessions 表（1–2 天）
- M4.7 端到端联调 + 真实 LLM 多轮验证（1 天）

**预估总工时：7–10 工作日。**

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| LLM 网关不稳定 | AnalystAgent 频繁 degraded | 重试 + 模板降级；可切换 fallback model |
| ToolUse 协议两套（anthropic / openai）差异 | 适配复杂 | 内部统一抽象 ToolCall；分协议 adapter 单测覆盖 |
| LLM 给出错误结论 | 误导用户 | 报告头部固定免责声明；safety scanner 防交易指令 |
| Token 爆炸 | 成本飙升 | 每工具返回截断；max_iterations 上限；max_tokens 强制 |
| prompt 漂移导致回归 | 报告质量下降 | prompt 版本化 + 关键 prompt 有 fixture 黄金样例对比 |
| 多 session 状态泄漏 | 安全风险 | session_id 隔离；工具调用注入 session_id 计费 |

### 越界声明

- ❌ 自动下单 / 触发交易
- ❌ LLM 写库 / 改配置
- ❌ 训练 / 微调
- ❌ 多 Agent 协同框架
- ❌ 向量库 / RAG（P4.5 起再说）
- ❌ Web 聊天 UI（P5）

---

## 附录 A：与 P1/P2/P3 依赖契约

P4 仅作为 **read-only consumer**，依赖：
1. `DataRepository.get_ohlcv / get_universe / quality_report`（P1，幂等只读）。
2. `DataHealth` schema（P1）。
3. `meta.db.events` + `job_runs` 表（P2，schema 稳定）。
4. `FactorRegistry` 单例 + `factor_metrics` 表（P3）。
5. `portfolio_snapshots` 表 + `attribution.json` schema（P3）。

P4 不修改上述任何资源，全部走读接口。

## 附录 B：与后续阶段（P5）接口承诺

1. `llm_calls` 表 schema 稳定（P5 渲染成本曲线 + 历史调用）。
2. `chat_sessions` / `chat_messages` 表 schema 稳定（P5 渲染对话历史，未来支持 Web 续聊）。
3. `LLMOrchestrator` 是 P5 Web Chat 后端的同一入口；不应在 P4 之外另起一个聊天链路。
4. Prompt 文件结构稳定（`prompts/<name>_v<n>.md`）；P5 控制台支持选 prompt 版本。
5. 工具列表通过 `ToolRegistry.list_specs()` 暴露 JSON Schema，P5 可渲染"模型可用工具"页面。
