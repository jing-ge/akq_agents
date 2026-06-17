from __future__ import annotations

from datetime import datetime
from typing import Iterable, List

import pandas as pd

from akq_agents.models.domain import FactorScore, MarketSnapshot


class FactorLibrary:
    def compute_factor_scores(self, snapshots: Iterable[MarketSnapshot]) -> List[FactorScore]:
        rows = []
        for snapshot in snapshots:
            row = {
                "symbol": snapshot.symbol,
                "close": snapshot.close,
                "volume": snapshot.volume,
                **snapshot.extras,
            }
            rows.append(row)

        if not rows:
            return []

        frame = pd.DataFrame(rows)
        now = datetime.now()
        for column in [
            "momentum_5",
            "momentum_20",
            "momentum_60",
            "reversal_5",
            "volatility_20",
            "turnover_ratio",
            "value_score",
            "quality_score",
            "size_score",
        ]:
            if column not in frame.columns:
                frame[column] = 0.0

        frame["liquidity_score"] = frame["volume"].astype(float) / 1_000_000.0
        frame["trend_score"] = 0.2 * frame["momentum_5"] + 0.5 * frame["momentum_20"] + 0.3 * frame["momentum_60"]
        frame["stability_score"] = -frame["volatility_20"]

        factor_scores: List[FactorScore] = []
        for _, row in frame.iterrows():
            factor_scores.extend(
                [
                    FactorScore(symbol=row["symbol"], factor_name="momentum", value=float(row["momentum_20"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="trend", value=float(row["trend_score"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="reversal", value=float(row["reversal_5"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="value", value=float(row["value_score"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="quality", value=float(row["quality_score"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="size", value=float(row["size_score"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="low_volatility", value=float(row["stability_score"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="liquidity", value=float(row["liquidity_score"]), timestamp=now),
                    FactorScore(symbol=row["symbol"], factor_name="turnover", value=float(row["turnover_ratio"]), timestamp=now),
                ]
            )
        return factor_scores
