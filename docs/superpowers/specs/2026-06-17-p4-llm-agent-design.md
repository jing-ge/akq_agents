# P4 LLM Agent 层 — 设计文档（v2，oracle review 后收敛）

- 项目：akq-agents
- 阶段：P4（共 P1–P6 六阶段中的第四阶段）
- 日期：2026-06-17
- 状态：待 plan
- 依赖：P1（DataRepository / DataHealth）、P2（events / job_runs / events.kind 规范）、P3（FactorRegistry / portfolio_snapshots / attribution.json）

> **v2 收敛说明**（oracle review 后）：
> - **只做 Anthropic 协议**（`/anthropic/v1/messages`），砍 OpenAI fallback；ToolUse 双协议适配是最大坑，等真出问题再加。
> - **AnalystAgent 去掉 ToolUse**：盘后调用时 P3 已把 portfolio + attribution + DataHealth 全塞进 `context.state`，AnalystAgent 直接拿数据"写文章"，不需要工具循环。**仅 ChatAgent 用 ToolUse**。
> - **砍掉 cost/budget 体系**：网关 local proxy 没有计费信息，`cost_usd` 字段长期 NULL，相关 CLI 砍掉。
> - **砍掉工具级 rate_limit**：单用户本地系统无意义；`max_iterations` 已经是天然护栏。
> - **砍掉 prompt 版本号**：单用户没 A/B 需求，prompt 改了就改了，git 即版本控制。
> - **明确"工具 read-only" vs "Orchestrator 写 chat_messages"边界**：工具是 LLM 可调对象，必须 read-only；Orchestrator 写 `llm_calls` / `chat_messages` 是基础设施层，不受 read-only 约束。
> - **砍掉关键词黑名单扫描**：误伤率高、易绕过；改为仅在 prompt 强约束 + 报告头部固定免责声明。
> - **`chat_sessions` 合并到 `chat_messages`**（session_id 隐式分组），少一张表。
> - **首批工具砍到 4 个**（get_data_health / list_factors / get_portfolio_snapshot / query_events），覆盖 80% 问答场景；其他按需扩。

---

## §1 目标与边界

### 目标

把"机械计算 + 模板渲染"的 Advisor / Report 升级为 LLM 驱动：

- **AnalystAgent（盘后离线，无 ToolUse）**：读 `context.state` 中已就绪的 portfolio + attribution + DataHealth + 近期 events，调 LLM 生成 markdown 盘后简评，写 `reports/YYYY-MM-DD/analyst_brief.md`。
- **ChatAgent（CLI 交互式，带 ToolUse）**：用户问"今天为什么选 600519？"等问题；LLM 通过 ToolUse 调取 4 个只读工具回答。
- 工具调用层（Tools）：只读访问 Repository / FactorRegistry / portfolio_snapshots / events。
- LLM 网关：本地 `http://127.0.0.1:18931`，**只走 Anthropic 协议**（默认模型 `Claude-Opus-4.7`）。

### 在做什么（P4 范围）

- `LLMClient` 协议 + `GatewayLLMClient`（仅 Anthropic 协议）。
- `Tools` 注册表：所有 LLM 可调工具显式注册，统一 JSON Schema；**启动期强制校验 read_only=True**。
- `AnalystAgent`：盘后被 `batch.post_close` 调用，写 markdown 报告；**不使用 ToolUse**。
- `ChatAgent`：CLI 入口 `akq-agents chat`；支持多轮、自动 ToolUse、对话记录写 sqlite。
- 提示词：放在 `src/akq_agents/agents/prompts/`，git 管理；**不做版本号**。
- 调用记账：`llm_calls` 表（tokens / latency / model / status），**无 cost 字段**。
- 安全：所有工具都是 **read-only**；LLM 不能写任何业务表；报告头部固定免责声明。

### 不在做什么

- ❌ Agent 自动下单 / 触发交易 — **永远禁止**。
- ❌ LLM 直接写业务表 / 改配置（仅 `llm_calls` / `chat_messages` 是基础设施写入）。
- ❌ OpenAI / Gemini 协议适配（YAGNI，先 Anthropic）。
- ❌ AnalystAgent 内嵌 ToolUse（数据已在 context，没必要）。
- ❌ Cost 计算 / Budget 限额 / `llm cost` CLI。
- ❌ 工具级 rate limit。
- ❌ Prompt 版本管理 / 切换。
- ❌ 关键词黑名单扫描（误伤多）。
- ❌ 训练 / 微调本地模型。
- ❌ 多 Agent 协同（autogen / crewAI 风格）。
- ❌ 向量库 / RAG。
- ❌ Web 聊天 UI（P5）。
- ❌ 多模态（图像 / 文档解读）。

---

## §2 架构

### 整体拓扑

```
┌──────────────────────────────────────────────────────────────────┐
│  AnalystAgent (offline; 无 ToolUse)                                 │
│       由 P2 batch.post_close 末尾调用                                │
│  ChatAgent    (interactive; 带 ToolUse)                              │
│       由 CLI `akq-agents chat` 启动                                  │
└────┬──────────────────────────────────┬──────────────────────────┘
     │                                  │
     ▼                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  LLMOrchestrator                                                    │
│  - prompt assembly (system + user)                                  │
│  - ChatAgent only: tool loop (model 想调工具 → ToolRegistry.invoke) │
│  - max_iterations 防失控（默认 6）                                   │
│  - 写 llm_calls + chat_messages（这两张表写入是基础设施，不受 read_only 约束）│
└────┬──────────────────────────────────┬──────────────────────────┘
     │                                  │
     ▼                                  ▼
┌────────────────────┐         ┌────────────────────────────────────┐
│   LLMClient        │         │   ToolRegistry (read-only enforced) │
│   - GatewayClient  │         │   首批 4 个工具：                    │
│     (127.0.0.1:    │         │   - get_data_health                 │
│      18931)        │         │   - list_factors                    │
│   - 仅 Anthropic   │         │   - get_portfolio_snapshot          │
│     /anthropic/... │         │   - query_events                    │
│   - 重试 + 限频    │         │                                      │
└────────────────────┘         └────────────────────────────────────┘
```

### LLM 网关（已就绪，确认）

本地代理 `http://127.0.0.1:18931`，多 provider；**P4 仅使用 Anthropic 路径**：

```
POST http://127.0.0.1:18931/anthropic/v1/messages
Body: Anthropic Messages API 标准格式（含 tools / tool_use / tool_result）
默认模型: Claude-Opus-4.7
```

### 存储扩展（追加 `meta.db`）

```sql
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  agent TEXT NOT NULL,             -- "analyst" | "chat"
  session_id TEXT,                 -- analyst 用 "analyst:<date>"
  model TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms INTEGER,
  tool_calls INTEGER DEFAULT 0,
  status TEXT NOT NULL,            -- ok | failed | truncated
  reason_code TEXT,                -- TIMEOUT | UPSTREAM_ERROR | RATE_LIMITED | TOOL_LOOP_EXCEEDED
  error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);

-- 单表存所有对话消息（session 信息隐式分组）
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,              -- system | user | assistant | tool
  content TEXT NOT NULL,           -- markdown / json string
  tool_name TEXT,                  -- role=tool 或 role=assistant(tool_use) 时填
  tool_args TEXT,                  -- JSON; tool_use 入参
  tool_result TEXT,                -- JSON; role=tool 时填，是工具返回
  tokens INTEGER                   -- 该条消息的近似 token 数（可选）
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_sid_ts ON chat_messages(session_id, ts);
```

> **session 元信息**（创建时间、模型）以"session 的第一条 role='system' message"的 ts 隐式表达；不另起 `chat_sessions` 表。

### 配置示例（`config/llm.yaml`，新增）

```yaml
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
    # 上下文裁剪上限（避免 4000 标的 portfolio 全塞）：
    context_top_holdings: 20      # 仅塞 top 20 持仓
    context_events_count: 10      # 仅塞最近 10 条 events (level >= info)
  chat:
    enabled: true
    model: "Claude-Opus-4.7"
    max_tokens: 2000
    temperature: 0.4
    max_iterations: 6             # ToolUse 循环上限
    history_window: 20            # 对话历史保留最近 N 条进入 prompt
  safety:
    disclaimer_header: "本报告由 LLM 生成，仅供研究参考，不构成投资建议；系统不执行任何交易指令。"
```

---

## §3 数据流与时序

### 流程 1：AnalystAgent（盘后离线，**无 ToolUse**）

```
P2 batch.post_close 末尾
  → AnalystAgent.run(context)
        - 提取 context["portfolio"], ["attribution"], ["data_health"]
        - 调 _build_context_payload()：
            * 取 portfolio 中 top 20 by weight（不要全塞）
            * 取 attribution.portfolio_contribution（factor → 贡献）
            * 取 data_health 摘要（universe_size_today, ohlcv_coverage_today, health）
            * 取最近 10 条 events（按 ts desc, level >= info）
        - prompt 拼装：
            system: 你是量化研究员，输出 5 段结构化 markdown ...
                    （含 safety.disclaimer_header）
            user:   今日组合 / 归因 / 数据 / 事件 / 撰写要求
        - llm.chat(model=Claude-Opus-4.7, tools=None)   # 关键：不传 tools
        - 写 reports/YYYY-MM-DD/analyst_brief.md（首行 disclaimer_header）
        - 写 llm_calls(agent="analyst", session_id="analyst:<date>")
        - 写 events(kind="analyst.brief.generated")
  失败路径：
    - LLM 超时 / 网关 5xx / 重试 2 次仍失败
        → 退化到现有 AdvisorAgent 模板渲染，文件中写 disclaimer + "LLM 不可用，使用模板版"
        → llm_calls(status="failed", reason_code="UPSTREAM_ERROR")
        → events(kind="analyst.brief.degraded", level="warning")
        → AnalystAgent.run 返回 {"status": "degraded"}，**不阻塞 batch**
```

### 流程 2：ChatAgent（CLI 交互，**带 ToolUse**）

```
akq-agents chat [--model Claude-Opus-4.7]
  → 启动 REPL：
        - 生成 session_id = f"chat:{uuid4().hex[:8]}"
        - 写一条 chat_messages(role="system", content=load_prompt("chat_system"))
        - events(kind="chat.session.created")
        - 渲染提示语
  user >  今天为什么选 600519？
  → LLMOrchestrator.handle(user_msg)
        - 写 chat_messages(role="user", content=user_msg)
        - 拼装 messages = [system] + recent_history(window=20) + user
        - tools = ToolRegistry.list_anthropic_specs()
        - loop iter <= max_iterations:
            response = llm.chat(messages, tools=tools)
            写 llm_calls(status="ok", tool_calls=len(response.tool_uses))
            写 chat_messages(role="assistant", content=response.text, tool_name=..., tool_args=...)
            if response.stop_reason == "tool_use":
                for tu in response.tool_uses:
                    result = ToolRegistry.invoke(tu.name, tu.input, session_id=session_id)
                    写 chat_messages(role="tool", tool_name=tu.name, tool_result=result)
                    messages.append(tool_result_msg)
            else:
                break
        - 若 iter > max_iterations:
            llm_calls(reason_code="TOOL_LOOP_EXCEEDED")
            返回 "ToolUse 循环上限，已截断"
        - 返回最终 assistant text
  user >  /quit
  → 关闭 REPL；session 不再写新消息（不需要 status='closed' 字段，自动失活）
```

### 流程 3：ToolUse 安全模型

每个工具的注册定义：
```python
@dataclass
class ToolSpec:
    name: str                  # e.g. "get_portfolio_snapshot"
    description: str           # LLM 可见
    json_schema: dict          # 入参 JSON Schema（Anthropic tools 字段格式）
    read_only: Literal[True]   # 启动期强校验
    handler: Callable[[dict], dict]
    truncate_chars: int = 8000  # 返回 JSON 序列化后字节上限，超出截断
```

工具调用前置检查：
1. **启动期**：`ToolRegistry.register()` 拒绝 `read_only != True` 的工具，直接 raise。daemon / chat 启动时执行一次注册，违反则进程拒启。
2. 入参 JSON Schema 校验失败 → 返回 `{"error": "INVALID_ARGUMENTS", "detail": "..."}` 给 LLM，不抛异常。
3. 工具执行异常 → 返回 `{"error": "INTERNAL", "message": "..."}`，不泄露 traceback；写 events(kind="llm.tool.failed")。
4. 工具返回超过 `truncate_chars` → 截断 + `"_truncated": true` 标记。

### 流程 4：首批 4 个工具

| 工具名 | 描述 | 入参 | 返回 |
|---|---|---|---|
| `get_data_health` | 当前数据层健康状态 | `{}` | `DataHealth` dict (P1 schema) |
| `list_factors` | 列出所有因子 | `{}` | `[{name, factor_version, direction, lookback_days, last_ic?, last_ir?, last_evaluated?}]` |
| `get_portfolio_snapshot` | 某日组合快照 | `{"date": "YYYY-MM-DD"}` | `{"rows": [{symbol, name, industry, weight, prev_weight, composite_score, top_factors_json (解析后)}], "summary": "..."}` |
| `query_events` | 事件查询 | `{"kind_prefix"?: str, "since"?: ISO, "until"?: ISO, "limit": 50}` | `[{ts, level, kind, source, payload}]` |

**总 token 控制**：每个工具内部按 §3 流程 3 的 `truncate_chars` 截断，避免 LLM 上下文爆炸。

### 流程 5：失败与降级

```
LLM 超时（>60s）→ 重试 1 次 → 仍超时
  AnalystAgent: 走 AdvisorAgent 模板渲染（沿用现有），返回 degraded
  ChatAgent:    在 REPL 回复 "LLM 网关暂时不可用，请稍后重试"

工具执行异常
  返回 {"error": "INTERNAL"} 给 LLM；events(kind="llm.tool.failed")
  LLM 通常会道歉并尝试别的方式（或直接停止 tool loop）

LLM 试图调用未注册工具
  返回 {"error": "TOOL_NOT_FOUND"}；events(kind="llm.tool.unknown")
```

> **不做关键词黑名单扫描**。报告头部 `disclaimer_header` 已声明"不构成投资建议、系统不执行交易"。

---

## §4 模块与接口

### 文件清单

```
src/akq_agents/
├── agents/
│   ├── analyst_agent.py            ← 新增（不用 ToolUse）
│   ├── chat_agent.py               ← 新增（CLI REPL）
│   └── prompts/                     ← 新增
│       ├── analyst.md               # AnalystAgent system prompt（git 管理，无版本号）
│       └── chat_system.md           # ChatAgent system prompt
├── services/
│   └── llm/                         ← 新增子包
│       ├── __init__.py
│       ├── client.py                # LLMClient protocol + GatewayLLMClient (Anthropic only)
│       ├── orchestrator.py          # LLMOrchestrator (analyst 单次 / chat tool loop)
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── registry.py          # ToolRegistry，启动期强校验 read_only
│       │   ├── data_tools.py        # get_data_health
│       │   ├── factor_tools.py      # list_factors
│       │   ├── portfolio_tools.py   # get_portfolio_snapshot
│       │   └── ops_tools.py         # query_events
│       └── store.py                 # llm_calls / chat_messages 读写
├── models/
│   └── llm_config.py                # LLMConfig (pydantic)
├── orchestrator/
│   └── jobs/
│       └── batch_post_close.py      # 末尾注入 AnalystAgent.run（失败不阻断）
└── cli/
    └── app.py                       # 新增子命令：chat / llm
                                       - chat                          (REPL)
                                       - llm calls --last N            (调用历史)
                                       - llm sessions --last N         (会话列表：按 session_id 聚合)
```

> 砍掉的 CLI：`llm cost`（无 cost 数据）、`llm sessions --status`（无 status 字段）。

### LLMClient 协议

```python
@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]    # Anthropic blocks: text / tool_use / tool_result
    # tool_use_id（在 ContentBlock 上），不在外层

@dataclass
class LLMResponse:
    text: str                         # 平铺的 assistant 文本
    tool_uses: list[ToolUseRequest]   # 本轮提出的工具调用
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]
    prompt_tokens: int
    completion_tokens: int

class LLMClient(Protocol):
    def chat(self, *,
             model: str,
             system: str,
             messages: list[LLMMessage],
             tools: list[dict] | None = None,    # Anthropic tools schema
             max_tokens: int,
             temperature: float,
             timeout_s: int) -> LLMResponse: ...

class GatewayLLMClient:
    """仅实现 Anthropic 协议；POST {base_url}/anthropic/v1/messages"""
    def __init__(self, cfg: LLMGatewayConfig): ...
```

### LLMOrchestrator

```python
class LLMOrchestrator:
    def __init__(self, client: LLMClient, tools: ToolRegistry, store: LLMStore,
                 max_iterations: int = 6): ...

    def run_analyst(self, *,
                    session_id: str,
                    system_prompt: str,
                    user_message: str,
                    model: str, max_tokens: int, temperature: float) -> str:
        """单次调用，不带 tools，不循环；返回 markdown 文本。"""

    def run_chat_turn(self, *,
                      session_id: str,
                      system_prompt: str,
                      history: list[LLMMessage],
                      user_message: str,
                      model: str, max_tokens: int, temperature: float) -> str:
        """带 tool loop ≤ max_iterations；每轮写 chat_messages / llm_calls；返回最终文本。"""
```

### ToolRegistry

```python
class ToolRegistry:
    def __init__(self): self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.read_only is not True:
            raise ValueError(f"P4: tool {spec.name} must be read_only=True")
        self._tools[spec.name] = spec

    def list_anthropic_specs(self) -> list[dict]:
        """转成 Anthropic tools schema: [{name, description, input_schema}]"""

    def invoke(self, name: str, args: dict, *, session_id: str) -> dict:
        """schema validate → handler → truncate → 返回 dict（含 _truncated 标记）"""
```

### AnalystAgent

```python
class AnalystAgent(BaseAgent):
    name = "analyst-agent"
    def __init__(self, orchestrator: LLMOrchestrator, cfg: AnalystConfig,
                 reports_dir: Path, fallback_advisor: AdvisorAgent): ...

    def run(self, context: AgentContext) -> dict:
        if not self.cfg.enabled:
            return {"status": "skipped", "reason": "disabled"}
        try:
            payload = self._build_context_payload(context)
            text = self.orchestrator.run_analyst(
                session_id=f"analyst:{date.today().isoformat()}",
                system_prompt=load_prompt("analyst") + "\n\n" + self.cfg.safety.disclaimer_header,
                user_message=self._render_user(payload),
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
            )
        except LLMGatewayError as e:
            logger.warning("LLM 不可用，退化到模板", exc_info=e)
            return self._fallback(context)
        path = self._write_report(text)
        return {"status": "ok", "path": str(path)}

    def _build_context_payload(self, context) -> dict:
        """裁剪上下文，避免 token 爆炸：
        - portfolio: 仅 top N by weight (cfg.context_top_holdings)
        - attribution.portfolio_contribution: 全量（顶层因子贡献只有几个 key）
        - data_health: 取关键字段
        - events: 最近 cfg.context_events_count 条
        """

    def _fallback(self, context) -> dict:
        """LLM 不可用：用现有 AdvisorAgent + 模板生成 markdown；
        events(kind='analyst.brief.degraded', level='warning')"""
```

### ChatAgent

```python
class ChatAgent:
    def __init__(self, orchestrator: LLMOrchestrator, cfg: ChatConfig, store: LLMStore): ...

    def repl(self) -> None:
        session_id = f"chat:{uuid4().hex[:8]}"
        system = load_prompt("chat_system") + "\n\n" + self.cfg.safety.disclaimer_header
        self.store.append_message(session_id, "system", system)
        events.write("chat.session.created", source="chat", payload={"session_id": session_id})
        self._print_banner(session_id)
        while True:
            user_input = input("> ").strip()
            if user_input in {"/quit", "/exit"}: break
            if user_input.startswith("/model "):
                self.cfg.model = user_input.split()[1]; continue
            history = self.store.recent(session_id, limit=self.cfg.history_window)
            try:
                text = self.orchestrator.run_chat_turn(
                    session_id=session_id, system_prompt=system,
                    history=history, user_message=user_input,
                    model=self.cfg.model, max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                )
            except LLMGatewayError:
                print("LLM 网关暂时不可用，请稍后重试"); continue
            print(text)
```

### 关键边界

- ❌ 任何工具都不能写库（启动期 read_only 强制校验，违反则进程拒启）。
- ❌ LLM 不能直接调 AKShare；想要数据必须通过 Repository。
- ❌ AnalystAgent 不使用 ToolUse；上下文由 P3 准备。
- ❌ AnalystAgent 失败不阻塞 P2 batch（degraded 状态，事件落库）。
- ❌ 不实现 OpenAI / Gemini 协议适配。
- ❌ 不缓存 LLM 响应（每次请求重新算）。
- ❌ 不实现 streaming（P5 SSE 直接接受非流式：等完整响应再 send done）。
- ❌ 不做 cost 计算 / budget 限额 / 关键词扫描。

### 测试策略

```
tests/llm/
├── test_gateway_client.py        # mock 127.0.0.1:18931 Anthropic 响应；tool_use 解析
├── test_orchestrator_analyst.py  # 单次调用、失败 degrade、disclaimer 注入
├── test_orchestrator_chat.py     # tool loop、max_iterations、ToolUse 多轮
├── test_registry.py              # 启动期 read_only 强制；register(read_only=False) → raise
├── test_tools_*.py               # 每个工具入参 schema + 返回 truncation
├── test_chat_agent.py            # mock LLM REPL 走通 3 轮（user → tool_use → result → final）
└── fixtures/
    └── gateway_anthropic_resp.json
```

目标覆盖率：**`services/llm/` ≥ 75%**（IO 多，比 P3 略低）。

---

## §5 验收标准与里程碑

### A. 功能验收

| # | 条件 | 验证方式 |
|---|---|---|
| A1 | `akq-agents chat` 启动后能完成 3 轮对话，其中至少 1 轮含 ToolUse | 人工验证 |
| A2 | `batch.post_close` 跑完后写出 `reports/YYYY-MM-DD/analyst_brief.md`，首行含 disclaimer | 文件检查 |
| A3 | LLM 网关 down 时 AnalystAgent 返回 `degraded`，batch 不失败，markdown 文件仍写出（模板版） | mock 网关 5xx |
| A4 | 工具执行异常返回结构化 `{"error": ...}`，LLM 看到后继续 | 单测 |
| A5 | 启动时尝试注册 read_only=False 的工具直接 raise，进程拒启 | 单测 |
| A6 | AnalystAgent **不传 tools** 字段给 LLM（grep 验证或 mock 断言） | 单测 |
| A7 | `llm calls --last 10` 输出表格含 model/tokens/latency/status（**无 cost 列**） | CLI 验证 |
| A8 | `/model GPT-5.4` 切换后 REPL 警告"非 Anthropic 模型未支持"（v2 仅 Claude-Opus-4.7） | 人工验证（或全砍掉 /model） |
| A9 | tool loop 超过 max_iterations 自动截断，llm_calls.reason_code='TOOL_LOOP_EXCEEDED' | 单测 |
| A10 | AnalystAgent 上下文裁剪：4000 标的 portfolio 进入 prompt 时只塞 top 20 | 单测 |
| A11 | 所有 `events.kind` 写入符合 P2 附录 C 规范（`analyst.brief.generated/degraded`、`chat.session.created`、`llm.tool.failed/unknown`） | grep + 单测 |

### B. 质量验收

| # | 条件 | 验证方式 |
|---|---|---|
| B1 | `tests/llm/` 覆盖率 ≥ 75% | `pytest --cov` |
| B2 | `ruff check` 零警告 | CI |
| B3 | 所有工具有 description + JSON Schema | review |
| B4 | 无任何工具有写权限（grep 检测：`read_only=False` 或缺失字段命中数 = 0） | grep |
| B5 | prompt 文件存在且非空：`agents/prompts/analyst.md`、`chat_system.md` | 文件检查 |

### C. 性能验收

| # | 条件 | 验证方式 |
|---|---|---|
| C1 | AnalystAgent 一次生成 ≤ 90 秒（单次 LLM 调用，无 tool loop） | events 时间统计 |
| C2 | ChatAgent 单轮回复 P95 ≤ 15s（含最多 6 次 ToolUse） | llm_calls 表 |
| C3 | LLM 失败不影响盘后 batch 总耗时 +5 分钟以上（degrade fallback fast path） | 实测 |
| C4 | 单条 chat 消息 + tool_use blocks 入 sqlite `chat_messages` ≤ 50ms | 实测 |

### D. 文档验收

| # | 条件 |
|---|---|
| D1 | `docs/llm_agents.md`：架构、Anthropic-only 选型说明、工具清单、prompt 文件说明、故障排查、安全模型 |
| D2 | README 增加 `chat` / `llm calls` / `llm sessions` 命令 |
| D3 | 明确说明：本地代理无 cost 数据，因此不做 cost 体系 |

### 里程碑参考

- M4.1 LLMClient + Gateway Anthropic 适配（1 天，砍 OpenAI 后缩短）
- M4.2 ToolRegistry + 4 个核心工具（每工具单测）（1.5 天）
- M4.3 LLMOrchestrator + analyst path + chat tool loop + llm_calls/chat_messages 表（1.5 天）
- M4.4 prompt 文件 + AnalystAgent + 集成到 batch（1 天）
- M4.5 ChatAgent + CLI REPL（1 天）
- M4.6 端到端联调 + 真实 LLM 多轮验证（0.5–1 天）

**预估总工时：5–6 工作日**（v1 的 7–10 天因砍 OpenAI fallback / cost / rate_limit / 版本化 prompt / AnalystAgent ToolUse / 关键词扫描 / 砍 5 个工具而缩短）。

### 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 网关 Anthropic 路径不稳定 | AnalystAgent 频繁 degrade | 重试 + 模板降级；events.kind='analyst.brief.degraded' 可观测 |
| Anthropic tool_use 协议在多轮 + parallel tool_use 上的边界 | ChatAgent 卡 loop | max_iterations=6 强制兜底；单测覆盖多轮 |
| LLM 给出错误结论 | 误导用户 | 报告头部固定免责声明；prompt 强约束"不给买卖建议" |
| Token 爆炸 | latency / 上下文超限 | AnalystAgent 上下文裁剪（top 20 / events 10）；工具返回 truncate |
| 单用户切多 session 状态混淆 | chat 历史串到别的 session | session_id 隔离；recent_history 严格按 session_id 过滤 |
| `meta.db` 多进程并发写 chat_messages | 写阻塞 | P1 已承诺 WAL + busy_timeout=5000 |

### 越界声明

- ❌ 自动下单 / 触发交易
- ❌ LLM 写业务表 / 改配置
- ❌ 训练 / 微调
- ❌ 多 Agent 协同框架
- ❌ 向量库 / RAG（P4.5 起再说）
- ❌ Web 聊天 UI（P5）
- ❌ 多协议适配（仅 Anthropic）
- ❌ Cost 计算 / Budget 限额 / 关键词黑名单扫描

---

## 附录 A：与 P1/P2/P3 依赖契约

P4 作为 **read-only consumer**（除 llm_calls / chat_messages 这两张 P4 自有基础设施表），依赖：
1. `DataRepository.get_ohlcv / get_universe / quality_report`（P1，幂等只读）。
2. `DataHealth` schema（P1）。
3. `meta.db` WAL + busy_timeout（P1 附录 B §6）。
4. `meta.db.events` + `job_runs` 表（P2，schema 稳定，命名规范见 P2 附录 C）。
5. `FactorRegistry` 单例 + `factor_metrics` 表（P3，含 `factor_version` 字段）。
6. `portfolio_snapshots` 表（P3，含 `name` / `industry` / `top_factors_json` 字段）+ `attribution.json` 镜像。

P4 不修改上述任何资源，全部走读接口。

## 附录 B：与 P5 接口承诺

1. `llm_calls` 表 schema 稳定（P5 可渲染历史调用统计）。
2. `chat_messages` 表 schema 稳定（P5 Web Chat 后端拉历史 + 可恢复对话）。
3. `LLMOrchestrator.run_chat_turn()` 是 P5 Web Chat 后端的同一入口；P5 不另起一套 LLM 链路。
4. **P5 SSE 实现策略**：由于 P4 不实现 streaming，P5 直接接受"非流式 = 等完整响应后 send 一次 SSE done"；这一点在 P5 spec 中明文承诺。
5. Prompt 文件路径稳定（`src/akq_agents/agents/prompts/<name>.md`）；P5 不动 prompt（只读展示 ok）。
6. 工具列表通过 `ToolRegistry.list_anthropic_specs()` 暴露 schema，P5 可展示"模型可用工具"页面。
7. **`events.kind` 仅写入 P2 附录 C 已枚举的 analyst.* / chat.* / llm.* 集合**；新增 kind 必须先回 P2 附录 C 注册。