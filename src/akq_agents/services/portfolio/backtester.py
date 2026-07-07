"""组合净值回测（M7-A）：用历史 portfolio_snapshots 重放出每日扣费 NAV。

设计原则（YAGNI）：
- 用 portfolio_snapshots 表里**全部历史 rebalance 日**当成 rebalance 节点；
- 每日按当前持仓 close 收益更新 NAV；
- 遇到 rebalance 日：先把当前 NAV 按今日 close 算市值 → 应用新权重 → 扣 turnover × cost；
- 停牌 / 价格缺失：那只股票当日按零收益处理（vfwd = 0）。

数据库表：portfolio_nav
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_PORTFOLIO_NAV_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_nav (
  as_of_date TEXT PRIMARY KEY,
  nav_gross REAL NOT NULL,
  nav_net REAL NOT NULL,
  daily_return_net REAL,
  turnover REAL,
  cost REAL,
  benchmark_nav REAL,
  benchmark_return REAL
);
"""


@dataclass
class BacktestConfig:
    commission: float = 0.0003   # 单边手续费
    slippage: float = 0.0005     # 单边滑点
    benchmark_symbol: str = "000300"
    # 涨跌停不可成交阈值 (一字板代理): 当日 close 相对前一交易日涨跌幅达此比例视为
    # 涨/跌停, 涨停禁买 / 跌停禁卖. A 股主板 ±10%, 取 0.095 留缓冲. 设 None 关闭该约束.
    price_limit_pct: float | None = 0.095


@dataclass
class BacktestResult:
    nav: pd.DataFrame
    summary: dict


class PortfolioBacktester:
    """从 portfolio_snapshots + ohlcv 重放出扣费 NAV。"""

    def __init__(
        self,
        meta_db_path: Path,
        ohlcv_dir: Path,
        cfg: BacktestConfig | None = None,
    ) -> None:
        self._db = Path(meta_db_path)
        self._ohlcv_dir = Path(ohlcv_dir)
        self._cfg = cfg or BacktestConfig()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with open_meta_db(self._db) as conn:
            conn.execute(_PORTFOLIO_NAV_SCHEMA)
            conn.commit()

    # ------------------------------------------------------------------

    def rebuild_full_history(self) -> BacktestResult:
        """从 portfolio_snapshots 全部历史重新算 NAV，覆盖写表。"""
        snapshot_dates = self._list_snapshot_dates()
        if len(snapshot_dates) < 1:
            return BacktestResult(nav=pd.DataFrame(), summary={"reason": "no_snapshots"})

        weights_by_date = self._load_all_weights(snapshot_dates)
        symbols = sorted({s for d in weights_by_date.values() for s in d})

        start = date.fromisoformat(snapshot_dates[0])
        end = date.today()
        close = self._load_close(symbols + [self._cfg.benchmark_symbol], start, end)
        if close.empty:
            return BacktestResult(nav=pd.DataFrame(), summary={"reason": "no_close_data"})

        nav_df = self._replay(weights_by_date, close)
        self._upsert_nav(nav_df)
        summary = self._summarize(nav_df)
        return BacktestResult(nav=nav_df, summary=summary)

    def read_nav(self) -> pd.DataFrame:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                """
                SELECT as_of_date, nav_gross, nav_net, daily_return_net,
                       turnover, cost, benchmark_nav, benchmark_return
                FROM portfolio_nav ORDER BY as_of_date
                """
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=[
            "as_of_date", "nav_gross", "nav_net", "daily_return_net",
            "turnover", "cost", "benchmark_nav", "benchmark_return",
        ])

    # ------------------------------------------------------------------
    # M19: 单因子回测公开 API
    # ------------------------------------------------------------------

    def backtest_in_memory(
        self,
        weights_by_date: dict[str, dict[str, float]],
        start: date | None = None,
        end: date | None = None,
    ) -> BacktestResult:
        """跑一次内存回测 (不写 portfolio_nav 表), 复用 _replay 的 NAV 算法.

        Args:
            weights_by_date: {as_of_date_iso: {symbol: weight}} — 每个 rebalance 日的目标权重
            start / end: 拉 close 的区间; 默认从最早 snapshot_date 到 today

        Returns:
            BacktestResult(nav=DataFrame, summary=dict). 不写表, 调用方自己处理结果。

        典型用法 (单因子回测): 生成"如果只用某因子打分跑组合"的虚拟 weights → 拿 NAV 曲线
        vs benchmark, 评估单因子的赚钱能力。
        """
        if not weights_by_date:
            return BacktestResult(nav=pd.DataFrame(), summary={"reason": "no_weights"})

        snapshot_dates = sorted(weights_by_date.keys())
        symbols = sorted({s for d in weights_by_date.values() for s in d})

        start = start or date.fromisoformat(snapshot_dates[0])
        end = end or date.today()
        close = self._load_close(symbols + [self._cfg.benchmark_symbol], start, end)
        if close.empty:
            return BacktestResult(nav=pd.DataFrame(), summary={"reason": "no_close_data"})

        nav_df = self._replay(weights_by_date, close)
        summary = self._summarize(nav_df)
        return BacktestResult(nav=nav_df, summary=summary)

    # ------------------------------------------------------------------

    def _list_snapshot_dates(self) -> list[str]:
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT DISTINCT as_of_date FROM portfolio_snapshots ORDER BY as_of_date ASC"
            ).fetchall()
        return [r[0] for r in rows]

    def _load_all_weights(self, snapshot_dates: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        with open_meta_db(self._db) as conn:
            for d in snapshot_dates:
                rows = conn.execute(
                    "SELECT symbol, weight FROM portfolio_snapshots WHERE as_of_date = ?",
                    (d,),
                ).fetchall()
                out[d] = {sym: float(w) for sym, w in rows}
        return out

    def _load_close(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        """读 close wide table，宽容缺失。"""
        import pyarrow.dataset as ds

        if not self._ohlcv_dir.exists() or not symbols:
            return pd.DataFrame()
        dataset = ds.dataset(self._ohlcv_dir, format="parquet", partitioning="hive")
        table = dataset.to_table(
            filter=(ds.field("date") >= start.isoformat())
            & (ds.field("date") <= end.isoformat())
            & ds.field("symbol").isin(list(symbols)),
            columns=["date", "symbol", "close"],
        )
        frame = table.to_pandas()
        if frame.empty:
            return pd.DataFrame()
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        wide = frame.pivot_table(
            index="date", columns="symbol", values="close", aggfunc="last"
        ).sort_index()
        return wide

    # ------------------------------------------------------------------

    def _replay(
        self,
        weights_by_date: dict[str, dict[str, float]],
        close: pd.DataFrame,
    ) -> pd.DataFrame:
        """逐日重放 NAV。

        简单模型：
        - 状态 = {symbol: shares}（持仓单位数）
        - 每日按 close 计算 mv = Σ shares * price，作为 nav_net
        - 在 rebalance 日，把 nav_net 按新权重重新分配 → 新 shares；同时扣 turnover 成本

        **前视偏差防护 (T+1 成交)**：因子信号在 snapshot_date (T 日) 收盘后才产生，
        实盘无法用 T 日收盘价成交。因此 rebalance 顺延到 snapshot_date 的**下一个**
        交易日 (T+1)，用 T+1 收盘价建仓。这样「T 日信号 → T+1 成交」，消除
        「T 日收盘信号用 T 日收盘价成交」的前视偏差。

        **涨跌停不可成交**：A 股一字板无法成交。用 close 相对前一交易日 close 的
        涨跌幅代理：当日涨幅 >= +limit 时禁买入 (该 symbol 目标权重顺延到下次)，
        跌幅 <= -limit 时禁卖出 (维持原持仓)。limit 由 cfg.price_limit_pct 控制。
        """
        cfg = self._cfg
        trading_days = list(close.index)
        if not trading_days:
            return pd.DataFrame()
        bench = close.get(cfg.benchmark_symbol)

        # snapshot_date → 真实 rebalance 交易日 (T+1 成交, 防前视偏差)
        # 取 **严格 > snapshot_date** 的最近交易日: 信号在 T 日收盘后产生, T+1 才成交.
        rebalance_map: dict[date, dict[str, float]] = {}
        for ds_str, w_dict in weights_by_date.items():
            sd = date.fromisoformat(ds_str)
            real_td = next((td for td in trading_days if td > sd), None)
            if real_td is None:
                continue
            # 同一交易日多个 snapshot 取最后一个（用 max snapshot_date）
            if real_td in rebalance_map:
                # 比较：用更晚的 snapshot
                existing_sd = next(
                    (date.fromisoformat(s) for s, w in weights_by_date.items() if w is rebalance_map[real_td]),
                    None,
                )
                if existing_sd and sd <= existing_sd:
                    continue
            rebalance_map[real_td] = w_dict

        # 从第一个 rebalance 日开始
        rb_days = sorted(rebalance_map.keys())
        if not rb_days:
            return pd.DataFrame()
        first_pos = trading_days.index(rb_days[0])
        sim_days = trading_days[first_pos:]

        nav_net = 1.0
        nav_gross = 1.0
        prev_weights: dict[str, float] = {}
        shares: dict[str, float] = {}
        prev_mv = 1.0

        records = []
        bench_first = None
        for td in sim_days:
            today_close = close.loc[td] if td in close.index else None

            # 1) 盯市：按今日 close 计算持仓市值（未扣费的真实收益）
            raw_return = 0.0
            if today_close is not None and shares:
                mv = 0.0
                for sym, sh in shares.items():
                    px = today_close.get(sym)
                    if px is None or pd.isna(px) or px <= 0:
                        # 停牌：保持昨日估值（不变化），从最近有效价格估值
                        last_px = _last_valid_px_before(close, sym, td)
                        if last_px is not None:
                            mv += sh * float(last_px)
                    else:
                        mv += sh * float(px)
                raw_return = (mv / prev_mv - 1.0) if prev_mv > 0 else 0.0
                nav_net = mv
            # gross 永远只反映未扣费收益（与 net 分离才能看出 cost 影响）
            nav_gross = nav_gross * (1.0 + raw_return)

            # 2) 如果今天是 rebalance 日，应用新权重 + 扣 turnover 成本（只影响 net）
            turnover_today = 0.0
            cost_today = 0.0
            if td in rebalance_map:
                new_weights = rebalance_map[td]
                # turnover = 0.5 × Σ|w_new - w_old| 单边换手率
                all_syms = set(prev_weights) | set(new_weights)
                turnover_today = 0.5 * sum(
                    abs(new_weights.get(s, 0.0) - prev_weights.get(s, 0.0)) for s in all_syms
                )
                # 双边成本：买入 + 卖出 两边都付，所以乘 2
                # commission/slippage 是单边费率（见 BacktestConfig 注释）
                cost_today = 2.0 * turnover_today * (cfg.commission + cfg.slippage)
                nav_net = nav_net * (1.0 - cost_today)
                # 重新建立 shares (涨停禁买 / 跌停禁卖: 受限 symbol 维持 rebalance 前持仓)
                prev_shares = dict(shares)
                limited = _price_limited_symbols(close, td, cfg.price_limit_pct)
                shares = {}
                if today_close is not None:
                    for sym, w in new_weights.items():
                        px = today_close.get(sym)
                        if px is None or pd.isna(px) or px <= 0:
                            continue
                        target_sh = (w * nav_net) / float(px)
                        held = prev_shares.get(sym, 0.0)
                        state = limited.get(sym)
                        # 涨停禁买: 若要增持 (target>held) 则维持原持仓
                        if state == "up" and target_sh > held:
                            if held > 0:
                                shares[sym] = held
                            continue
                        # 跌停禁卖: 若要减持 (target<held) 则维持原持仓
                        if state == "down" and target_sh < held:
                            if held > 0:
                                shares[sym] = held
                            continue
                        shares[sym] = target_sh
                    # 跌停禁卖的补充: 原持仓里 new_weights 未提及 (目标清零) 但当日跌停的,
                    # 也无法卖出, 维持原持仓
                    for sym, held in prev_shares.items():
                        if sym in shares or held <= 0:
                            continue
                        if limited.get(sym) == "down":
                            shares[sym] = held
                prev_weights = dict(new_weights)

            # daily_return_net: 在扣 cost 之后基于 nav_net 算，这样 cost 反映在当日 return
            daily_return_net = (nav_net / prev_mv - 1.0) if prev_mv > 0 else 0.0

            # 3) benchmark
            bench_ret = None
            bench_nav = None
            if bench is not None and td in bench.index:
                cur = bench.loc[td]
                if bench_first is None and pd.notna(cur) and cur > 0:
                    bench_first = float(cur)
                if bench_first is not None and pd.notna(cur) and cur > 0:
                    bench_nav = float(cur) / bench_first
                # daily return
                idx_pos = list(bench.index).index(td)
                if idx_pos > 0:
                    prev_b = bench.iloc[idx_pos - 1]
                    if pd.notna(prev_b) and prev_b > 0 and pd.notna(cur):
                        bench_ret = float(cur / prev_b - 1.0)

            records.append({
                "as_of_date": td.isoformat(),
                "nav_gross": float(nav_gross),
                "nav_net": float(nav_net),
                "daily_return_net": float(daily_return_net),
                "turnover": float(turnover_today),
                "cost": float(cost_today),
                "benchmark_nav": bench_nav,
                "benchmark_return": bench_ret,
            })
            prev_mv = nav_net

        return pd.DataFrame(records)

    # ------------------------------------------------------------------

    def _upsert_nav(self, nav_df: pd.DataFrame) -> None:
        if nav_df.empty:
            return
        rows = []
        for _, r in nav_df.iterrows():
            rows.append((
                r["as_of_date"], float(r["nav_gross"]), float(r["nav_net"]),
                _f(r["daily_return_net"]), _f(r["turnover"]), _f(r["cost"]),
                _f(r["benchmark_nav"]), _f(r["benchmark_return"]),
            ))
        with open_meta_db(self._db) as conn:
            conn.executemany(
                """
                INSERT INTO portfolio_nav
                    (as_of_date, nav_gross, nav_net, daily_return_net,
                     turnover, cost, benchmark_nav, benchmark_return)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(as_of_date) DO UPDATE SET
                    nav_gross=excluded.nav_gross,
                    nav_net=excluded.nav_net,
                    daily_return_net=excluded.daily_return_net,
                    turnover=excluded.turnover,
                    cost=excluded.cost,
                    benchmark_nav=excluded.benchmark_nav,
                    benchmark_return=excluded.benchmark_return
                """,
                rows,
            )
            conn.commit()

    @staticmethod
    def _summarize(nav_df: pd.DataFrame) -> dict:
        if nav_df.empty or len(nav_df) < 2:
            return {"n_days": int(len(nav_df))}
        nav_net = nav_df["nav_net"].dropna()
        ret = nav_df["daily_return_net"].dropna()
        if len(nav_net) < 2:
            return {"n_days": int(len(nav_net))}
        total_ret = float(nav_net.iloc[-1] - 1.0)
        n = len(nav_net)
        ann_ret = float(nav_net.iloc[-1] ** (252.0 / n) - 1.0) if nav_net.iloc[-1] > 0 else 0.0
        sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0.0
        cummax = nav_net.cummax()
        max_dd = float((nav_net / cummax - 1.0).min())
        total_cost = float(nav_df["cost"].sum())
        avg_turnover = float(nav_df.loc[nav_df["turnover"] > 0, "turnover"].mean()) if (nav_df["turnover"] > 0).any() else 0.0
        # M19 B7: bench_total 和 excess 必须用同一天的 nav vs benchmark, 否则尾部 NULL
        # 会让 excess 虚高 (nav 涨到 t, benchmark 停在 t-N, excess 多算了 N 天 nav 涨幅).
        bench_aligned = nav_df[nav_df["benchmark_nav"].notna()]
        if not bench_aligned.empty:
            last_aligned = bench_aligned.iloc[-1]
            bench_total = float(last_aligned["benchmark_nav"] - 1.0)
            # 用同一天的 nav_net (而不是全表末日的 nav_net) 算超额
            nav_at_bench_end = float(last_aligned["nav_net"])
            excess = nav_at_bench_end - 1.0 - bench_total
            excess_aligned_date = str(last_aligned["as_of_date"])
        else:
            bench_total = None
            excess = None
            excess_aligned_date = None
        return {
            "n_days": int(n),
            "total_return_net": total_ret,
            "annualized_return_net": ann_ret,
            "sharpe_net": sharpe,
            "max_drawdown": max_dd,
            "total_cost": total_cost,
            "avg_turnover_per_rebalance": avg_turnover,
            "benchmark_total_return": bench_total,
            "excess_return": excess,
            # 透明披露: 超额对齐到的日期 (benchmark 末日)
            "excess_aligned_to": excess_aligned_date,
        }


def _f(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _last_valid_px_before(close: pd.DataFrame, sym: str, td: date):
    """从 close[sym] 取 td 之前最近的非 NaN/正价格。"""
    if sym not in close.columns:
        return None
    col = close[sym].loc[:td].dropna()
    col = col[col > 0]
    if col.empty:
        return None
    return col.iloc[-1]


def _price_limited_symbols(
    close: pd.DataFrame, td: date, limit_pct: float | None
) -> dict[str, str]:
    """判定 td 当日哪些 symbol 涨停 / 跌停 (一字板代理)。

    用 td 当日 close 相对 **td 之前最近有效交易日 close** 的涨跌幅近似:
    涨幅 >= +limit_pct → "up" (涨停, 禁买); 跌幅 <= -limit_pct → "down" (跌停, 禁卖)。
    limit_pct 为 None 时返回空 (关闭该约束)。

    注意这是近似: 真实涨跌停应比对官方前收盘价且判一字板 (high==low),
    但回测仅加载了 close, 用相邻 close 涨跌幅代理已能挡住绝大多数不可成交场景。
    """
    if limit_pct is None or td not in close.index:
        return {}
    today = close.loc[td]
    prev_pos = close.index.get_loc(td)
    if prev_pos <= 0:
        return {}
    out: dict[str, str] = {}
    for sym in close.columns:
        px = today.get(sym)
        if px is None or pd.isna(px) or px <= 0:
            continue
        prev_px = _last_valid_px_before(close, sym, close.index[prev_pos - 1])
        if prev_px is None or prev_px <= 0:
            continue
        chg = float(px) / float(prev_px) - 1.0
        if chg >= limit_pct:
            out[sym] = "up"
        elif chg <= -limit_pct:
            out[sym] = "down"
    return out
