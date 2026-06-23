# LLM 闭环 + UX 完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LLM 真正闭环参与因子决策（不只是提建议）；补全归因/单日异动诊断 UX；让 trade_list → holdings 真正闭环。

**Architecture:**
- 复用既有数据（portfolio_snapshots.top_factors_json / factor_metrics / paper_track_perf / trade_list_cohorts），主要工作在 API + UI + 调度
- LLM 走既有 chat 型号配置（不动 llm.yaml）
- 4 个方向有依赖：先 shadow 收割（数据基础）→ 归因页（数据可视化）→ LLM postmortem（决策闭环）→ trade_list 闭环（独立但放最后）

**Tech Stack:** 既有栈，无新依赖

---

## 方向总览

| # | 方向 | 关键产出 | 时间 |
|---|---|---|---|
| A | Shadow 因子收割机 + 战况看板 | shadow 池可视化，手动 demote 长期不达标的 | 1.5h |
| B | 组合归因 + 单日异动诊断页面 | /research 页加"今日异动"卡片，复用 top_factors_json | 2h |
| C | LLM 升级为研究助理（factor postmortem） | 新 chat tool `factor_postmortem` + 新 daemon job 每周一次 | 2.5h |
| D | Trade-list → Holdings 闭环 | mark_executed 同时更新 holdings + 一键全执行按钮 | 1.5h |

**总耗时：** ~7.5 小时（10 个 task）

---

## 关键现场事实（必读，避免 plan 与实际不符）

1. **`PortfolioSnapshotStore.write`** 写每行 `top_factors_json`（每只票最近一次的因子贡献排名 + contribution 数值）。**已完整存在**，但前端没消费。
2. **shadow 池现状**：13 个 shadow 因子，其中 10 个是 6/23 16:30 LLM 提议刚 accept 的（OOS 才几小时）；1 个 auto_ 因子 2 天 OOS IR=-0.38。
3. **DiscoveryEngine 阈值**：`shadow_min_oos_days=20`, `shadow_min_oos_ir=0.15`，已实现 promote 逻辑（但**没有 demote 逻辑**——长期不达标的 shadow 因子永远卡着）。
4. **`/api/trading/today-list/{symbol}/mark-executed`** endpoint 已存在但**只标 executed=1，不写 holdings**。
5. **`HoldingsStore.upsert`** 已存在（trade_list.py:126 附近）。
6. **`factor_metrics` 数据稀疏**：每因子最多 4 行历史。LLM postmortem 需要更长历史 → 短期内 LLM 看到的数据偏少，但**先把功能搭起来**，数据会自然累积。

---

## 方向 A：Shadow 因子收割机 + 战况看板

### Task A1：discovery 加 shadow demote 逻辑

**Goal**：让长期不达标的 shadow 因子（OOS > 60 天但 |IR| < 0.10）自动 demote 到 rejected。

**Files:**
- Modify: `src/akq_agents/services/factors/discovery.py:540-660`（`_promote_shadows` 函数）
- Modify: `src/akq_agents/services/factors/discovery.py:183`（`DiscoveryThresholds`）
- Test: `tests/factors/test_discovery_promote.py`（新建或追加）

**Step 1: 加阈值**

`discovery.py:183` `DiscoveryThresholds` 加：

```python
    # M15-A: 长期不达标 shadow 自动 demote
    shadow_max_days: int = 60         # OOS 满 60 天后必须出结果
    shadow_min_keep_ir: float = 0.10  # 不到这个 IR 就 demote
```

**Step 2: 改 `_promote_shadows`**

在 `if oos_ir is not None and abs(oos_ir) >= self.th.shadow_min_oos_ir:` 的 else 分支（line ~660 附近），加：

```python
            else:
                # M15-A: 时长足够但 IR 仍低 → 检查是否该 demote
                if len(oos_dates) >= self.th.shadow_max_days and (
                    oos_ir is None or abs(oos_ir) < self.th.shadow_min_keep_ir
                ):
                    p.status = "demoted"
                    p.reason = (
                        f"demoted_after_{len(oos_dates)}d_oos_ir={oos_ir:.3f}"
                        if oos_ir is not None
                        else f"demoted_after_{len(oos_dates)}d_oos_ir=None"
                    )
                    p.ir = oos_ir
                    p.oos_observations = len(oos_dates)
                    p.oos_ir = oos_ir
                    p.evaluated_at = now_iso()
                    self.proposal_store.upsert(p)
                    stats.demoted = (stats.demoted or 0) + 1
                else:
                    # 仍在观察期 → 更新 oos_observations / oos_ir，不改 status
                    p.oos_observations = len(oos_dates)
                    p.oos_ir = oos_ir
                    p.evaluated_at = now_iso()
                    self.proposal_store.upsert(p)
```

⚠️ 注意：先 grep 现有 `_promote_shadows` 看实际写法，按现有缩进/分支结构插入。`DiscoveryStats` 是不是有 `demoted` 字段——grep 一下：`grep -n 'class DiscoveryStats\|demoted' src/akq_agents/services/factors/discovery.py`。

**Step 3: 写 regression test**

```python
def test_promote_shadows_demotes_long_underperforming(tmp_path):
    """OOS 60 天 + IR < 0.10 → 自动 demote 到 rejected。"""
    # 构造一个 shadow proposal，shadow_started_at = 70 天前
    # mock evaluator 返回低 IR
    # 调 _promote_shadows
    # 断言 status='demoted'
    ...
```

**Step 4: Commit**

```bash
git commit -m "feat(factors): A1 — shadow 因子 60d OOS 不达标自动 demote

之前 shadow → accepted promote 逻辑完整，但 demote 缺失。结果：长期边缘
因子永远卡在 shadow 池里（13 个 shadow 中部分已观察 60+ 天 IR 仍 < 0.10）。

新增阈值: shadow_max_days=60, shadow_min_keep_ir=0.10。
新增分支: oos_days >= 60 且 |ir| < 0.10 → status='demoted'。"
```

---

### Task A2：/research 页加 Shadow 战况看板

**Goal**：在"自动发现因子流水"和"LLM 因子建议"卡片之间加一个 shadow 池战况表。

**Files:**
- Modify: `src/akq_agents/web/api/research.py`（新增 endpoint）
- Modify: `src/akq_agents/web/templates/research.html.j2`（新增卡片）

**Step 1: 后端 endpoint**

```python
@router.get("/factors/shadow-stats")
async def shadow_stats() -> dict[str, Any]:
    """Shadow 因子战况：每个 shadow 已观察天数、当前 OOS IR、距离 promote/demote 阈值还差多少。"""
    svc: ServiceContainer = get_services()
    store = svc.proposal_store
    if store is None:
        return {"shadows": [], "n": 0}
    rows = store.list_shadow()
    out = []
    from datetime import datetime as _dt
    now = _dt.now()
    for r in rows:
        shadow_d = _dt.fromisoformat(r.shadow_started_at) if r.shadow_started_at else None
        days_observed = (now - shadow_d).days if shadow_d else None
        out.append({
            "factor_name": r.factor_name,
            "direction": r.direction,
            "shadow_started_at": r.shadow_started_at,
            "days_observed": days_observed,
            "oos_observations": r.oos_observations,
            "oos_ir": r.oos_ir,
            "ir": r.ir,
            "is_llm": r.factor_name.startswith("llm_"),
            # 状态判定: 待评估(<20d) / 达标(IR≥0.15) / 边缘(0.10≤IR<0.15) / 该 demote(>60d且IR<0.10)
            "verdict": _shadow_verdict(r.oos_observations, r.oos_ir),
        })
    return {"shadows": out, "n": len(out)}


def _shadow_verdict(oos_days, oos_ir):
    if not oos_days or oos_days < 20:
        return "evaluating"
    if oos_ir is None:
        return "no_data"
    if abs(oos_ir) >= 0.15:
        return "promote_eligible"
    if oos_days >= 60 and abs(oos_ir) < 0.10:
        return "should_demote"
    return "edge"
```

**Step 2: 前端 UI 卡片**

在 `research.html.j2` 找到 "LLM 因子构建建议" 卡片之前，插入：

```html
<!-- M15-A: Shadow 战况看板 -->
<div class="card">
  <h2>Shadow 因子战况
    <span class="small">OOS 观察中，等待 promote / demote 判决</span>
  </h2>
  <div class="toolbar">
    <button onclick="loadShadowStats()">刷新</button>
    <span class="small" style="color:var(--fg-mute)">阈值: OOS ≥ 20 天 + |IR| ≥ 0.15 → promote；OOS ≥ 60 天 + |IR| < 0.10 → demote</span>
  </div>
  <div id="shadow-stats-table" class="small"></div>
</div>
<script>
async function loadShadowStats() {
  const r = await fetch('/api/research/factors/shadow-stats');
  const d = await r.json();
  document.getElementById('shadow-stats-table').innerHTML = renderShadowStats(d.shadows || []);
}
function renderShadowStats(rows) {
  if (!rows.length) return '<div class="small" style="color:var(--fg-mute)">暂无 shadow 因子</div>';
  let html = '<table class="data-table"><thead><tr><th>因子名</th><th>来源</th><th>方向</th><th>观察天数</th><th>OOS IR</th><th>判定</th></tr></thead><tbody>';
  const verdictMap = {
    'evaluating': '<span class="metric-dim">评估中</span>',
    'no_data': '<span class="metric-warn">无 OOS 数据</span>',
    'promote_eligible': '<span class="status-ok">达标</span>',
    'edge': '<span class="metric-warn">边缘</span>',
    'should_demote': '<span class="status-error">该 demote</span>',
  };
  for (const s of rows) {
    const ir = s.oos_ir != null ? s.oos_ir.toFixed(3) : '—';
    const days = s.days_observed != null ? s.days_observed : '—';
    const source = s.is_llm ? 'LLM' : 'auto';
    html += `<tr>
      <td><code>${s.factor_name}</code></td>
      <td>${source}</td>
      <td>${s.direction}</td>
      <td>${days}</td>
      <td>${ir}</td>
      <td>${verdictMap[s.verdict] || s.verdict}</td>
    </tr>`;
  }
  return html + '</tbody></table>';
}
window.addEventListener('load', loadShadowStats);
</script>
```

**Step 3: Commit**

```bash
git commit -m "feat(web): A2 — /research 页加 Shadow 战况看板

显示 shadow 池中每个因子的 OOS 观察天数、当前 IR、判定状态
(评估中 / 达标 / 边缘 / 该 demote)。让 LLM brainstorm 闭环可见。"
```

---

## 方向 B：归因 + 单日异动诊断页面

### Task B1：当日组合 PnL 分解 endpoint

**Goal**：根据 portfolio_snapshots + ohlcv 算出当日组合的 PnL 分解（个股贡献 bps + 因子贡献）。

**Files:**
- Modify: `src/akq_agents/web/api/research.py`（新增 endpoint）

**Step 1: endpoint 实现**

```python
@router.get("/daily-attribution")
async def daily_attribution(date: str = Query(...)) -> dict[str, Any]:
    """当日组合 PnL 分解。

    返回:
    - top_contributors: 涨幅 top 5 票 + 贡献 bps
    - top_draggers: 跌幅 top 5 票 + 贡献 bps
    - factor_contribution: 按因子聚合的 contribution 排名（来自 top_factors_json）
    - total_return / benchmark_return / excess

    数据源:
    - portfolio_snapshots (as_of_date, symbol, weight, top_factors_json)
    - portfolio_nav (当日 daily_return_net, benchmark_return)
    - ohlcv (close prev_day, close today → 个股日收益)
    """
    svc: ServiceContainer = get_services()
    if svc.portfolio_store is None or svc.repo is None:
        raise HTTPException(503, "stores not ready")
    try:
        d = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, f"invalid date: {date}")  # noqa: B904

    # 1) 拉当日 snapshot
    rows = svc.portfolio_store.read_snapshot(d)
    if not rows:
        raise HTTPException(404, {"error": "no_snapshot", "date": date})

    # 2) 拉个股当日 close + 前一交易日 close
    symbols = [r.symbol for r in rows]
    today_close, prev_close = _load_close_pair(svc.repo, symbols, d)

    # 3) 算个股贡献 bps = (close_t / close_{t-1} - 1) * prev_weight * 10000
    contribs = []
    for r in rows:
        c_t = today_close.get(r.symbol)
        c_p = prev_close.get(r.symbol)
        prev_w = float(r.prev_weight or 0.0)
        if c_t and c_p and prev_w > 0:
            ret = c_t / c_p - 1
            bps = ret * prev_w * 10000
            contribs.append({
                "symbol": r.symbol, "name": r.name, "industry": r.industry,
                "prev_weight": prev_w, "ret_pct": ret, "contrib_bps": bps,
            })
    contribs.sort(key=lambda x: x["contrib_bps"], reverse=True)
    top_contributors = contribs[:5]
    top_draggers = contribs[-5:][::-1]  # 倒序

    # 4) 因子贡献聚合（按 top_factors_json）
    factor_total: dict[str, float] = {}
    for r in rows:
        try:
            top_factors = json.loads(r.top_factors_json or "[]")
            for f in top_factors:
                name = f.get("name")
                c = f.get("contribution", 0.0)
                factor_total[name] = factor_total.get(name, 0.0) + c
        except Exception:
            continue
    factor_rank = sorted(factor_total.items(), key=lambda kv: abs(kv[1]), reverse=True)[:8]

    return {
        "date": date,
        "n_holdings": len(rows),
        "top_contributors": top_contributors,
        "top_draggers": top_draggers,
        "factor_contribution": [{"name": n, "contribution": v} for n, v in factor_rank],
    }


def _load_close_pair(repo, symbols, d):
    """从 ohlcv parquet 拉 (today_close, prev_trading_day_close) dict."""
    from datetime import timedelta as _td
    import pyarrow.dataset as ds
    ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
    if ohlcv_dir is None or not ohlcv_dir.exists():
        return {}, {}
    start = (d - _td(days=10)).isoformat()
    end = d.isoformat()
    dataset = ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
    table = dataset.to_table(
        filter=(ds.field("date") >= start) & (ds.field("date") <= end)
               & ds.field("symbol").isin(list(symbols)),
        columns=["date", "symbol", "close"],
    )
    df = table.to_pandas()
    if df.empty:
        return {}, {}
    df["date"] = df["date"].astype(str)
    today_str = d.isoformat()
    today = df[df["date"] == today_str].set_index("symbol")["close"].to_dict()
    # prev: 取 < today 的最大 date
    prev_df = df[df["date"] < today_str]
    if prev_df.empty:
        return today, {}
    latest_prev = prev_df["date"].max()
    prev = prev_df[prev_df["date"] == latest_prev].set_index("symbol")["close"].to_dict()
    return {str(k): float(v) for k, v in today.items()}, {str(k): float(v) for k, v in prev.items()}
```

**Step 2: Commit**

```bash
git commit -m "feat(web): B1 — /api/research/daily-attribution endpoint

当日组合 PnL 分解: top 5 涨/跌票 + 因子贡献排名。
数据全部复用既有 portfolio_snapshots.top_factors_json + ohlcv，零数据库
schema 变化。"
```

---

### Task B2：前端归因页 UI

**Goal**：在 /research 页加"今日异动诊断"卡片，调用 B1 endpoint 渲染表格 + 图。

**Files:**
- Modify: `src/akq_agents/web/templates/research.html.j2`

**Step 1: UI 卡片**

放在"今日组合"卡片之后：

```html
<div class="card">
  <h2>今日异动诊断
    <span class="small">个股贡献 bps + 因子贡献分解</span>
  </h2>
  <div class="toolbar">
    <label>日期：<input type="date" id="attr-date" value="" /></label>
    <button class="primary" onclick="loadDailyAttribution()">分析</button>
  </div>
  <div id="daily-attr-output"></div>
</div>

<script>
async function loadDailyAttribution() {
  const dateInput = document.getElementById('attr-date');
  const date = dateInput.value || new Date().toISOString().slice(0, 10);
  const r = await fetch(`/api/research/daily-attribution?date=${date}`);
  const out = document.getElementById('daily-attr-output');
  if (!r.ok) {
    const d = await r.json();
    out.innerHTML = '<div class="status-error small">' + (d.detail || JSON.stringify(d)) + '</div>';
    return;
  }
  const d = await r.json();
  out.innerHTML = renderDailyAttr(d);
}

function renderDailyAttr(d) {
  let html = `<p class="small">${d.date} · ${d.n_holdings} 只持仓</p>`;
  html += '<div class="grid grid-2">';
  // top contributors
  html += '<div><h3 class="small">📈 Top 5 贡献（涨）</h3><table class="data-table"><thead><tr><th>票</th><th>权重</th><th>涨跌</th><th>贡献 bps</th></tr></thead><tbody>';
  for (const r of d.top_contributors || []) {
    html += `<tr><td><code>${r.symbol}</code> ${r.name || ''}</td><td>${(r.prev_weight*100).toFixed(2)}%</td><td class="status-ok">${(r.ret_pct*100).toFixed(2)}%</td><td class="status-ok"><b>+${r.contrib_bps.toFixed(1)}</b></td></tr>`;
  }
  html += '</tbody></table></div>';
  // top draggers
  html += '<div><h3 class="small">📉 Top 5 拖累（跌）</h3><table class="data-table"><thead><tr><th>票</th><th>权重</th><th>涨跌</th><th>贡献 bps</th></tr></thead><tbody>';
  for (const r of d.top_draggers || []) {
    html += `<tr><td><code>${r.symbol}</code> ${r.name || ''}</td><td>${(r.prev_weight*100).toFixed(2)}%</td><td class="status-error">${(r.ret_pct*100).toFixed(2)}%</td><td class="status-error"><b>${r.contrib_bps.toFixed(1)}</b></td></tr>`;
  }
  html += '</tbody></table></div>';
  html += '</div>';
  // factor contribution
  html += '<h3 class="small" style="margin-top:16px;">🎯 因子贡献（汇总）</h3><table class="data-table"><thead><tr><th>因子</th><th>贡献</th></tr></thead><tbody>';
  for (const f of d.factor_contribution || []) {
    const sign = f.contribution >= 0 ? 'status-ok' : 'status-error';
    html += `<tr><td>${f.name}</td><td class="${sign}">${f.contribution.toFixed(3)}</td></tr>`;
  }
  html += '</tbody></table>';
  return html;
}

// 默认填入今天日期
window.addEventListener('load', () => {
  const today = new Date().toISOString().slice(0, 10);
  const di = document.getElementById('attr-date');
  if (di) di.value = today;
});
</script>
```

**Step 2: 烟雾测试**

```bash
./start.sh stop && sleep 2 && ./start.sh up && sleep 5
curl -sf "http://127.0.0.1:8765/api/research/daily-attribution?date=2026-06-23" | python -m json.tool | head -30
```

**Step 3: Commit**

```bash
git commit -m "feat(web): B2 — /research 页今日异动诊断卡片

显示 top 5 贡献/拖累票 + 因子贡献排名。
默认填今天日期，可手动改任意历史日期。"
```

---

## 方向 C：LLM 升级为研究助理

### Task C1：新 chat tool `factor_postmortem`

**Goal**：让 LLM 通过 chat 查询某个因子的近 N 天 IC/IR 历史，并能给出"是否该 demote / 该等"建议。

**Files:**
- Modify: `src/akq_agents/services/llm/tools/builtin.py`（新增 build_factor_postmortem）
- Modify: `src/akq_agents/services/llm/tools/registry.py`（注册）

**Step 1: 实现 tool**

```python
def build_factor_postmortem(services: dict[str, Any]) -> ToolSpec:
    evaluator = services.get("factor_evaluator")
    proposal_store = services.get("factor_proposal_store")
    registry = services.get("factor_registry")

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        factor_name = args.get("factor_name", "").strip()
        days = int(args.get("days", 30))
        if not factor_name:
            return {"error": "factor_name required"}
        if evaluator is None:
            return {"error": "factor_evaluator not available"}

        # 1) 历史 IC/IR
        history = evaluator.list_history(factor_name, limit=days)
        ic_series = [{"as_of": m.as_of_date, "ic": m.ic_mean, "ir": m.ir} for m in history]

        # 2) 当前 status (from registry or proposal_store)
        status = "unknown"
        if proposal_store is not None:
            for p in proposal_store.list_recent(limit=200):
                if p.factor_name == factor_name:
                    status = p.status
                    break
        # registry 里说不定有内置因子（无 proposal）
        if status == "unknown" and registry is not None:
            for f in registry.list_all():
                if f.name == factor_name:
                    status = "registered"
                    break

        # 3) 简单趋势指标
        irs = [m["ir"] for m in ic_series if m["ir"] is not None]
        recent_mean = sum(abs(x) for x in irs[:5]) / 5 if len(irs) >= 5 else None
        earlier_mean = sum(abs(x) for x in irs[-5:]) / 5 if len(irs) >= 10 else None
        trend = None
        if recent_mean is not None and earlier_mean is not None:
            if recent_mean < earlier_mean * 0.6:
                trend = "decaying"
            elif recent_mean > earlier_mean * 1.4:
                trend = "improving"
            else:
                trend = "stable"

        return {
            "factor_name": factor_name,
            "status": status,
            "history": ic_series,
            "n_observations": len(ic_series),
            "recent_5d_mean_abs_ir": recent_mean,
            "earlier_5d_mean_abs_ir": earlier_mean,
            "trend": trend,
        }

    return ToolSpec(
        name="factor_postmortem",
        description="""查询某个因子的近 N 天 IC/IR 历史 + 当前 status + 趋势诊断。
用途: 帮你判断一个 shadow 因子该 promote / demote / 继续观察。

入参:
- factor_name: 因子名（如 'momentum_20' 或 'llm_zscore_close_30_long_abc123'）
- days: 看多少天历史，默认 30

返回:
- status: registered / shadow / accepted / rejected / demoted / llm_suggested / unknown
- history: 按日期降序的 [{as_of, ic, ir}]
- recent_5d_mean_abs_ir: 最近 5 个观察日的 |IR| 均值
- earlier_5d_mean_abs_ir: 较早 5 日均值（用于对比）
- trend: decaying / stable / improving / None（数据不足）
""",
        json_schema={
            "type": "object",
            "properties": {
                "factor_name": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
            "required": ["factor_name"],
        },
        handler=handler,
        read_only=True,
    )
```

**Step 2: 注册到 registry**

`registry.py:78` 附近找 `register_default_tools` 函数，加：

```python
    reg.register(build_factor_postmortem(services))
```

**Step 3: 写测试**

```python
def test_factor_postmortem_returns_history(services):
    """factor_postmortem tool 应返回历史 IC/IR + 趋势。"""
    reg = ToolRegistry()
    reg.register(build_factor_postmortem(services))
    out = reg.invoke("factor_postmortem", {"factor_name": "momentum_20", "days": 10}, session_id="t")
    assert "history" in out
    assert "trend" in out
```

**Step 4: Commit**

```bash
git commit -m "feat(llm): C1 — chat tool factor_postmortem

让 LLM 通过 chat 查询因子近 N 天 IC/IR 历史 + 趋势。
返回 status / history / recent vs earlier mean abs IR / trend 标签
(decaying/stable/improving)，让 LLM 能给出 promote/demote 建议。"
```

---

### Task C2：扩展 chat_system prompt 教 LLM 用新 tool

**Goal**：在 chat system prompt 里加示例，教 LLM 看到 shadow 因子时主动调 `factor_postmortem`。

**Files:**
- Modify: `src/akq_agents/agents/prompts/chat_system.md`

**Step 1: 追加 prompt section**

在 chat_system.md 末尾加：

```markdown
## 因子诊断场景

如果用户问"X 因子怎么样"、"这个 shadow 因子能上吗"、"哪些因子在衰减" 这种问题：

1. 优先调 `factor_postmortem(factor_name="X", days=30)` 看历史 + 趋势
2. 根据 status + recent_5d_mean_abs_ir + trend 判断：
   - status='shadow' 且 trend='decaying' → 建议拒绝
   - status='accepted' 且 trend='decaying' → 建议关注，可能需要 demote
   - status='shadow' 且 trend='improving' → 可继续观察
3. 把判断告诉用户，并附 history 的关键数字证据

不要凭空猜，必须基于 tool 返回数据。
```

**Step 2: Commit**

```bash
git commit -m "feat(llm): C2 — chat prompt 教 LLM 用 factor_postmortem 做因子诊断"
```

---

## 方向 D：Trade-list → Holdings 闭环

### Task D1：mark_executed 同时更新 holdings

**Goal**：用户点 "✓ 已执行" 时，不光标 executed=1，**同时按 target_shares 更新 holdings_store**。

**Files:**
- Modify: `src/akq_agents/services/portfolio/trade_list.py`（`TradeListStore.mark_executed`）
- Modify: `src/akq_agents/web/api/trading.py:204`（mark_executed endpoint）
- Test: `tests/portfolio/test_trade_list.py`（新建或追加）

**Step 1: 看现状 + 写 regression test 先 RED**

```python
def test_mark_executed_updates_holdings(tmp_path):
    """C4 闭环: mark_executed 应同时把 target_shares 写到 holdings。"""
    from akq_agents.services.portfolio.trade_list import TradeListStore, HoldingsStore
    db = tmp_path / "meta.db"
    tl_store = TradeListStore(db)
    h_store = HoldingsStore(db)
    # 先插一条 trade_list_cohort
    tl_store.upsert_cohort([{
        "cohort_date": "2026-06-23", "symbol": "000001",
        "action": "BUY", "current_shares": 0, "target_shares": 1000,
        "delta_shares": 1000, "target_weight": 0.05, "current_price": 10.0,
        "delta_amount": 10000, "reason": "BUY", "industry": "银行",
        "composite_score": 1.0,
    }])
    # mark_executed 应该更新 holdings
    tl_store.mark_executed(date(2026, 6, 23), "000001", holdings_store=h_store)
    holdings = h_store.as_dict()
    assert holdings.get("000001") == 1000.0
```

**Step 2: 改 `mark_executed`**

```python
def mark_executed(
    self, cohort_date: date, symbol: str,
    *, holdings_store: "HoldingsStore | None" = None,
) -> None:
    """标记某条已执行；可选同时同步 holdings 到 target_shares。"""
    with open_meta_db(self._db) as conn:
        # 拿 target_shares
        row = conn.execute(
            "SELECT target_shares FROM trade_list_cohorts WHERE cohort_date=? AND symbol=?",
            (cohort_date.isoformat(), str(symbol)),
        ).fetchone()
        if row is None:
            return
        target_shares = float(row[0])
        conn.execute(
            "UPDATE trade_list_cohorts SET executed=1 WHERE cohort_date=? AND symbol=?",
            (cohort_date.isoformat(), str(symbol)),
        )
        conn.commit()
    if holdings_store is not None:
        # 同步到 holdings（target=0 时删除）
        if target_shares > 0:
            holdings_store.upsert(symbol, shares=target_shares, note=f"executed {cohort_date.isoformat()}")
        else:
            holdings_store.delete(symbol)
```

**Step 3: 改 endpoint**

```python
@router.post("/today-list/{symbol}/mark-executed")
async def mark_executed(symbol: str, date: str | None = None) -> dict[str, Any]:
    svc = get_services()
    workflow = svc.workflow
    tl_store = workflow.services.get("trade_list_store") if workflow else None
    h_store = workflow.services.get("holdings_store") if workflow else None
    if tl_store is None:
        raise HTTPException(503, "trade_list_store not ready")
    target_date = _date.fromisoformat(date) if date else _date.today()
    tl_store.mark_executed(target_date, symbol, holdings_store=h_store)
    return {"status": "ok"}
```

**Step 4: 跑测试 + 烟雾测试**

```bash
/opt/anaconda3/envs/akq310/bin/python -m pytest tests/portfolio/test_trade_list.py -v
# 端到端
./start.sh stop && sleep 2 && ./start.sh up && sleep 5
# 当前 holdings 应该 0
sqlite3 data/meta.db "SELECT COUNT(*) FROM holdings"
# trigger 一条
curl -X POST "http://127.0.0.1:8765/api/trading/today-list/000001/mark-executed?date=2026-06-23"
# 现在应该有 holdings
sqlite3 data/meta.db "SELECT * FROM holdings"
```

**Step 5: Commit**

```bash
git commit -m "fix(trade_list): D1 — mark_executed 同时更新 holdings

之前 mark_executed 只标 executed=1，holdings 永远为 0，trade_list →
holdings 闭环没建立。

修法: mark_executed 加 holdings_store 可选参数，target_shares > 0 时
upsert，target_shares = 0 时 delete。endpoint 自动注入 holdings_store。

附 regression test 锁定闭环行为。"
```

---

### Task D2：/trading 页加"全部执行"批量按钮

**Goal**：用户每天 trade_list 有 50+ 条，逐条点 ✓ 累。加一个"全部一键执行"按钮。

**Files:**
- Modify: `src/akq_agents/web/api/trading.py`（新 endpoint）
- Modify: `src/akq_agents/web/templates/research.html.j2` 或 trading 页（看 trade_list 实际显示在哪个 template）

**Step 1: 找 trade_list 现在显示在哪**

```bash
grep -rn 'trade-list\|today-list\|交易清单' src/akq_agents/web/templates/ | head
```

**Step 2: batch endpoint**

```python
@router.post("/today-list/mark-all-executed")
async def mark_all_executed(date: str | None = None) -> dict[str, Any]:
    """一键执行：把指定日期 trade_list 全部 mark executed + 同步 holdings。"""
    svc = get_services()
    workflow = svc.workflow
    tl_store = workflow.services.get("trade_list_store") if workflow else None
    h_store = workflow.services.get("holdings_store") if workflow else None
    if tl_store is None:
        raise HTTPException(503, "trade_list_store not ready")
    target_date = _date.fromisoformat(date) if date else _date.today()
    items = tl_store.list_cohort(target_date)
    n = 0
    for it in items:
        if it.action == "HOLD":
            continue  # HOLD 不动 holdings
        tl_store.mark_executed(target_date, it.symbol, holdings_store=h_store)
        n += 1
    return {"status": "ok", "executed": n}
```

**Step 3: UI 加按钮 + 二次确认**

按 Step 1 找到的位置，在 trade list 表格上方加：

```html
<button class="primary" onclick="markAllExecuted()" title="把今天 trade_list 里所有 BUY/SELL 一键标记已执行 + 更新 holdings">
  📦 全部执行 (BUY/SELL)
</button>

<script>
async function markAllExecuted() {
  if (!confirm('确认把今日 trade_list 里所有 BUY/SELL 全部标记已执行？这会更新 holdings 表。')) return;
  const r = await fetch('/api/trading/today-list/mark-all-executed', {method: 'POST'});
  const d = await r.json();
  alert(`已执行 ${d.executed} 条`);
  location.reload();
}
</script>
```

**Step 4: Commit**

```bash
git commit -m "feat(trading): D2 — trade_list 一键全执行按钮

50 条/日逐个点 ✓ 太累。加一键执行 BUY/SELL（HOLD 不动）。
带二次确认 alert，避免误操作 holdings。"
```

---

## Self-Review

**Spec coverage:**
- ✅ A: Shadow 收割机 + 战况看板（A1 demote 逻辑、A2 可视化）
- ✅ B: 归因 + 单日异动诊断（B1 endpoint、B2 UI）
- ✅ C: LLM 升级研究助理（C1 新 tool、C2 prompt 教学）
- ✅ D: Trade-list 闭环（D1 mark_executed 写 holdings、D2 一键执行）

**Placeholder scan:**
- D2 Step 1 让 implementer 自己 grep 找 UI 位置（合理 "find then act"）

**Type consistency:**
- A1 + A2 共享 `oos_observations` / `oos_ir` 字段（factor_proposals 已有列）
- D1 + D2 共享 `mark_executed(... holdings_store=...)` signature

---

## 范围外（YAGNI）

- ❌ LLM postmortem daemon job（C 方向只先做 chat tool；定期跑等用户实际有需求再加）
- ❌ holdings 历史变更轨迹表（D2 只更新当前 holdings；如果将来要审计可以加 holdings_history 表）
- ❌ Shadow 战况按 industry / direction 分组 sub-view（A2 一张大表已够）

---

## 执行建议

按依赖顺序：

1. **Session 1（3h）**：A1 → A2 → B1 → B2 — shadow 看板 + 归因页（独立、低风险）
2. **Session 2（2.5h）**：C1 → C2 — LLM 新 tool + prompt（需要 A、B 数据完整）
3. **Session 3（1.5h）**：D1 → D2 — trade_list 闭环（独立，最后做）

**绝对不要** 8 个 task 一口气跑——中途出问题难定位。
