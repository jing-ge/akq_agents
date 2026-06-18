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
            ohlcv = self._loose_read_ohlcv(repo, full_universe.symbols, start, today)
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
        weights = opt.solve(composite, vol_20, prev_weights)

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
            name_map={},  # P3a 暂无 name 映射（P1 universe 暂无 name 字段；可后续接入）
            industry_map={},  # P3a 不接入行业映射（推迟到 P3b）
        )

        # M7-A: 写完 snapshot 后增量重算 NAV（若 backtester 已装配）
        backtester = self._services.get("portfolio_backtester")
        if backtester is not None:
            try:
                backtester.rebuild_full_history()
            except Exception as exc:  # noqa: BLE001
                logger.warning("portfolio_backtester rebuild failed: %s", exc)

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

    @staticmethod
    def _loose_read_ohlcv(repo, symbols, start: date, end: date) -> pd.DataFrame:
        """绕过 calendar 严格校验，直接读 parquet 区间，缺哪天就缺哪天。"""
        import pyarrow.dataset as ds

        ohlcv_root = getattr(repo, "_ohlcv_dir", None)
        if ohlcv_root is None or not ohlcv_root.exists():
            return pd.DataFrame()
        dataset = ds.dataset(ohlcv_root, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat())
            & (ds.field("date") <= end.isoformat())
            & ds.field("symbol").isin(list(symbols)),
        )
        frame = table.to_pandas()
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

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
