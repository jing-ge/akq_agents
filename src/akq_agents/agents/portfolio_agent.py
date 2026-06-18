"""PortfolioAgent v2（P3a 接入版）：负责组合 pipeline 的 7 个步骤。

Step 1: 取当日数据 universe（P1 已就绪）
Step 2: 对 ohlcv 做 vol_20 计算
Step 3: 用 CombinedUniverseBuilder 取 portfolio_universe top 500
Step 4: FactorEngine.compute → raw factor values
Step 5: Preprocessor.transform → z-scored factor values
Step 6: CompositeScorer.score → composite_score
Step 7: PortfolioOptimizer.solve → target_weights
Step 8: Attributor.explain → attribution
Step 9: PortfolioSnapshotStore.write → 持久化

如果 services dict 不含 P3 组件（向后兼容旧 workflow），退化为旧版基于 factor_scores
的加权逻辑。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta

import pandas as pd

from akq_agents.agents.base import AgentContext, BaseAgent
from akq_agents.models.domain import PortfolioRecommendation
from akq_agents.services.data.exceptions import DataNotReady

logger = logging.getLogger(__name__)


def _compute_vol_20(ohlcv: pd.DataFrame) -> pd.Series:
    """从 long-format ohlcv 计算每只股票过去 20 日日收益率 std。"""
    if ohlcv.empty:
        return pd.Series(dtype=float, name="vol_20")
    close = ohlcv.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    if len(close) < 21:
        return pd.Series(dtype=float, name="vol_20")
    returns = close.pct_change().iloc[-20:]
    vol = pd.Series(returns.std(ddof=1))
    vol.name = "vol_20"
    return vol


class PortfolioAgent(BaseAgent):
    name = "portfolio-agent"

    def __init__(
        self,
        top_n_symbols: int = 50,
        *,
        services: dict | None = None,
    ) -> None:
        """初始化。

        Args:
            top_n_symbols: 旧版兼容参数；新版从 OptimizerConfig.top_n 读取
            services: 注入的服务字典；包含以下 keys 时启用 P3 pipeline：
                - data_repository (P1)
                - factor_registry (P3)
                - factor_engine (P3)
                - preprocessor (P3)
                - composite_scorer (P3)
                - portfolio_optimizer (P3)
                - attributor (P3)
                - portfolio_snapshot_store (P3)
                缺任一 → 退化到旧逻辑
        """
        self.top_n_symbols = top_n_symbols
        self._services = services or {}

    def run(self, context: AgentContext):
        if self._has_p3_pipeline():
            return self._run_p3(context)
        return self._run_legacy(context)

    def _has_p3_pipeline(self) -> bool:
        required = {
            "data_repository",
            "factor_registry",
            "factor_engine",
            "preprocessor",
            "composite_scorer",
            "portfolio_optimizer",
            "attributor",
            "portfolio_snapshot_store",
        }
        return required.issubset(self._services.keys())

    def _run_p3(self, context: AgentContext) -> dict:
        repo = self._services["data_repository"]
        registry = self._services["factor_registry"]
        engine = self._services["factor_engine"]
        prep = self._services["preprocessor"]
        scorer = self._services["composite_scorer"]
        opt = self._services["portfolio_optimizer"]
        attr = self._services["attributor"]
        store = self._services["portfolio_snapshot_store"]

        today = context.state.get("today")
        if today is None:
            today = date.today()
        elif isinstance(today, str):
            today = date.fromisoformat(today)

        try:
            full_universe = repo.get_universe(today)
        except DataNotReady as exc:
            logger.warning("portfolio: universe not ready for %s: %s", today, exc.missing)
            return {"status": "skipped", "reason": "data_not_ready", "portfolio_size": 0}

        # 拉取最近 max(lookback) 天 OHLCV（factor lookback_days 最大值，且至少 80 给 momentum_60 + 余量）
        max_lookback = max((f.lookback_days for f in registry.list_all()), default=80)
        start = today - timedelta(days=max_lookback * 2)  # 多取保险，过滤交易日后才够
        try:
            ohlcv = repo.get_ohlcv(full_universe.symbols, start, today)
        except DataNotReady as exc:
            # 严格读要求每日齐全；缓存里有些 symbol 历史缺失属正常。
            # 改走宽容路径：直接扫 parquet 区间，缺什么用什么，下游因子按 lookback 自然过滤。
            logger.info(
                "portfolio: strict get_ohlcv missing %d symbols, fall back to loose read",
                len(exc.missing),
            )
            ohlcv = repo.get_ohlcv_loose(list(full_universe.symbols) + ["000300"], start, today)
            if ohlcv.empty:
                return {"status": "skipped", "reason": "ohlcv_not_ready", "portfolio_size": 0}

        # Step 3: top 500 by amount_20
        from akq_agents.services.portfolio.combined_universe import build_portfolio_universe

        portfolio_universe = build_portfolio_universe(
            full_universe_symbols=full_universe.symbols,
            ohlcv=ohlcv,
            top_n=500,
            window=20,
        )

        # M7-B: 硬风控过滤（新股 / 停牌 / 极价 / 低流动性）
        risk_filter = self._services.get("risk_filter")
        if risk_filter is not None:
            sub_ohlcv_for_filter = ohlcv[ohlcv["symbol"].isin(list(portfolio_universe))]
            rf_result = risk_filter.apply(
                candidate_symbols=portfolio_universe,
                ohlcv=sub_ohlcv_for_filter,
                as_of_date=today,
            )
            if rf_result.excluded:
                logger.info(
                    "risk_filter excluded %d/%d symbols. by_reason=%s",
                    len(rf_result.excluded), len(portfolio_universe),
                    rf_result.excluded_count_by_reason,
                )
            portfolio_universe = rf_result.kept
            context.state["risk_filter_excluded"] = rf_result.excluded_count_by_reason
        if not portfolio_universe:
            return {"status": "skipped", "reason": "empty_portfolio_universe", "portfolio_size": 0}

        # 把 ohlcv 限制到 portfolio_universe
        sub_ohlcv = ohlcv[ohlcv["symbol"].isin(set(portfolio_universe))]

        # Step 4-5: factors + preprocess
        factors = registry.list_active(today)
        raw = engine.compute(sub_ohlcv, factors)
        directions = registry.factor_directions()
        z = prep.transform(raw, directions)

        # Step 6: composite_score
        composite = scorer.score(z)

        # Step 7: optimizer
        vol_20 = _compute_vol_20(sub_ohlcv)
        prev_weights = store.read_prev_weights(today)
        # M9-C: 行业映射（code 和 name）
        ind_store = self._services.get("industry_map_store")
        industry_code_map = ind_store.load() if ind_store is not None else {}
        industry_name_map = ind_store.load_names() if ind_store is not None else {}
        weights = opt.solve(composite, vol_20, prev_weights, industry_map=industry_code_map)

        if weights.empty:
            return {"status": "skipped", "reason": "optimizer_empty", "portfolio_size": 0}

        # Step 8: attribution
        attribution = attr.explain(
            weights=weights,
            factor_z=z,
            factor_weights=scorer.factor_weights(),
            as_of_date=today,
        )

        # Step 9: persist
        store.write(
            as_of_date=today,
            weights=weights,
            composite_score=composite,
            attribution=attribution,
            prev_weights=prev_weights,
            name_map={},  # P1 universe 暂无 name 字段
            industry_map=industry_name_map,  # M9-C: 真实申万一级行业名
        )

        # M7-A: 写完 snapshot 后增量重算 NAV（若 backtester 已装配）
        backtester = self._services.get("portfolio_backtester")
        if backtester is not None:
            try:
                backtester.rebuild_full_history()
            except Exception as exc:  # noqa: BLE001
                logger.warning("portfolio_backtester rebuild failed: %s", exc)

        # P0-2: Paper Trading 冻结当日 cohort + 估值所有历史 cohort（前向证据）
        # today_close 在 paper trading 和 trade list 都用，提前算好
        today_close: dict[str, float] = {}
        if not sub_ohlcv.empty:
            last_day_df = sub_ohlcv[sub_ohlcv["date"] == today]
            if last_day_df.empty:
                last_day_df = sub_ohlcv[sub_ohlcv["date"] == sub_ohlcv["date"].max()]
            for _, row in last_day_df.iterrows():
                today_close[str(row["symbol"])] = float(row["close"])
        if not ohlcv.empty:
            bench_rows = ohlcv[(ohlcv["symbol"] == "000300") & (ohlcv["date"] == today)]
            if bench_rows.empty:
                bench_rows = ohlcv[(ohlcv["symbol"] == "000300")].tail(1)
            if not bench_rows.empty:
                today_close["000300"] = float(bench_rows["close"].iloc[-1])

        paper = self._services.get("paper_trading_store")
        if paper is not None:
            try:
                weights_dict = {str(s): float(w) for s, w in weights.items()}
                # 修复 oracle #1：传 cohort_close_lookup 让 benchmark 收益有得算
                # 修复 oracle #2：缺价票退化用最近 ohlcv close（停牌也算合理估值）
                def _cohort_lookup(symbol: str, d):
                    """从 ohlcv parquet 查某 symbol 在某 cohort_date 的 close。"""
                    if repo is None:
                        return None
                    try:
                        from datetime import timedelta as _td
                        import pyarrow.dataset as ds
                        ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
                        if ohlcv_dir is None or not ohlcv_dir.exists():
                            return None
                        start = (d - _td(days=14)).isoformat()
                        end = d.isoformat()
                        dataset = ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
                        table = dataset.to_table(
                            filter=(ds.field("date") >= start)
                                   & (ds.field("date") <= end)
                                   & (ds.field("symbol") == str(symbol)),
                            columns=["date", "close"],
                        )
                        df = table.to_pandas()
                        if df.empty:
                            return None
                        df = df.sort_values("date")
                        return float(df.iloc[-1]["close"])
                    except Exception:
                        return None

                # 冻结当日（含停牌票 fallback）
                paper.freeze_today_cohort(today, weights_dict, today_close, fallback_lookup=_cohort_lookup)
                # 估值历史（含 benchmark lookup）
                paper.update_track_perf(today, today_close, cohort_close_lookup=_cohort_lookup)
            except Exception as exc:  # noqa: BLE001
                logger.warning("paper_trading update failed: %s", exc)

        # P0-1: 生成今日交易清单（权重 → BUY/SELL/HOLD 具体股数）
        try:
            self._generate_trade_list(
                today=today,
                weights=weights,
                composite=composite,
                today_close_map=today_close,
                industry_name_map=industry_name_map,
                prev_weights=prev_weights,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trade_list generation failed: %s", exc)

        # Compute turnover
        turnover = self._compute_turnover(weights, prev_weights)

        # context.state for downstream agents
        context.state["portfolio"] = [
            {"symbol": str(sym), "weight": float(w), "score": float(composite.get(sym, 0.0))}
            for sym, w in weights.items()
        ]
        context.state["attribution"] = attribution.dict()
        context.state["portfolio_turnover"] = turnover

        return {
            "status": "ok",
            "portfolio_size": len(weights),
            "turnover": turnover,
            "as_of_date": today.isoformat(),
        }

    def _generate_trade_list(
        self,
        *,
        today,
        weights,
        composite,
        today_close_map: dict,
        industry_name_map: dict,
        prev_weights,
    ) -> None:
        """P0-1: 生成今日交易清单（BUY/SELL/HOLD + 具体股数）。"""
        holdings_store = self._services.get("holdings_store")
        tl_store = self._services.get("trade_list_store")
        tl_cfg = self._services.get("trade_list_config")
        if holdings_store is None or tl_store is None:
            return

        from akq_agents.services.portfolio.trade_list import generate_trade_list

        weights_dict = {str(s): float(w) for s, w in weights.items()}
        composite_dict = {str(s): float(v) for s, v in composite.items()} if composite is not None else {}
        holdings_dict = holdings_store.as_dict()
        prev_weights_dict = {str(s): float(w) for s, w in prev_weights.items()} if prev_weights is not None and not prev_weights.empty else {}

        # 补充：对 holdings 里的 symbol 但 today_close 没有的，现场从 parquet 查最近 close
        close_map = dict(today_close_map or {})
        missing = [s for s in holdings_dict if s not in close_map]
        if missing:
            try:
                close_map.update(self._lookup_close_for_symbols(missing, today))
            except Exception as exc:  # noqa: BLE001
                logger.warning("close lookup failed for missing holdings: %s", exc)

        items = generate_trade_list(
            cohort_date=today,
            target_weights=weights_dict,
            current_close=close_map,
            holdings=holdings_dict,
            composite_scores=composite_dict,
            industry_map=industry_name_map or {},
            yesterday_weights=prev_weights_dict,
            cfg=tl_cfg,
        )
        tl_store.upsert_cohort(today, items)

    def _lookup_close_for_symbols(self, symbols: list[str], today) -> dict[str, float]:
        """为某些 symbol 现场查 ohlcv 最近 close。"""
        repo = self._services.get("data_repository")
        if repo is None or not symbols:
            return {}
        import pyarrow.dataset as ds
        from datetime import timedelta
        ohlcv_dir = getattr(repo, "_ohlcv_dir", None)
        if ohlcv_dir is None or not ohlcv_dir.exists():
            return {}
        start = (today - timedelta(days=14)).isoformat()
        end = today.isoformat()
        dataset = ds.dataset(ohlcv_dir, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start)
                   & (ds.field("date") <= end)
                   & ds.field("symbol").isin(list(symbols)),
            columns=["date", "symbol", "close"],
        )
        df = table.to_pandas()
        if df.empty:
            return {}
        df = df.sort_values(["symbol", "date"])
        # 每只 symbol 取最新一天的 close
        latest = df.groupby("symbol").tail(1)
        return {str(r["symbol"]): float(r["close"]) for _, r in latest.iterrows()}

    @staticmethod
    def _compute_turnover(weights: pd.Series, prev_weights: pd.Series) -> float:
        if prev_weights.empty:
            return 1.0  # 首日完全建仓
        all_symbols = set(weights.index) | set(prev_weights.index)
        s = 0.0
        for sym in all_symbols:
            w_today_val = weights.get(sym, 0.0)
            w_prev_val = prev_weights.get(sym, 0.0)
            w_today = float(w_today_val) if w_today_val is not None else 0.0
            w_prev = float(w_prev_val) if w_prev_val is not None else 0.0
            s += abs(w_today - w_prev)
        return s / 2.0

    def _run_legacy(self, context: AgentContext) -> dict:
        """旧版逻辑：基于 factor_scores 简单加权。保持向后兼容。"""
        selected_factors = {item["factor_name"] for item in context.state.get("selected_factors", [])}
        factor_scores = context.state.get("factor_scores", [])

        total_scores: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[str]] = defaultdict(list)
        for item in factor_scores:
            if item["factor_name"] not in selected_factors:
                continue
            total_scores[item["symbol"]] += item["value"]
            reasons[item["symbol"]].append(f"{item['factor_name']}={item['value']:.4f}")

        ranked = sorted(total_scores.items(), key=lambda pair: pair[1], reverse=True)[: self.top_n_symbols]
        total = sum(score for _, score in ranked) or 1.0

        recommendations = [
            PortfolioRecommendation(
                symbol=symbol,
                weight=score / total,
                score=score,
                reasons=reasons[symbol],
            )
            for symbol, score in ranked
        ]
        context.state["portfolio"] = [asdict(item) for item in recommendations]
        return {"portfolio": recommendations, "portfolio_size": len(recommendations)}
