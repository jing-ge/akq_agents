"""Attributor：线性归因 contribution_{s,f} = z_{s,f} × factor_weight_f。

P3a 数学（spec §3 流程 5）：
- comp_s = Σ_f z_{s,f} × w_f （precisely equals CompositeScorer 输出，因为 P3a 等权）
- contribution_{s,f} = z_{s,f} × w_f
- portfolio_contribution_f = Σ_s W_s × z_{s,f} × w_f
- top_factors_json[s] = top_k(|contribution_{s,*}|, k=5)

A7 验收承诺：|Σ_f contribution_{s,f} − composite_score_s| < 1e-6 for all s。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd


@dataclass
class AttributionResult:
    as_of_date: str
    portfolio_contribution: dict[str, float]  # factor_name → 组合层贡献
    per_stock: dict[str, list[dict[str, Any]]]  # symbol → [{name, contribution}, ...]
    summary: str
    extras: dict[str, Any] = field(default_factory=dict)

    def dict(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "portfolio_contribution": self.portfolio_contribution,
            "per_stock": self.per_stock,
            "summary": self.summary,
            **self.extras,
        }


class Attributor:
    """线性归因。"""

    def explain(
        self,
        *,
        weights: pd.Series,                   # 持仓权重 W_s，index=symbol
        factor_z: pd.DataFrame,               # Preprocessor 输出，index=symbol, columns=factor
        factor_weights: pd.Series,            # 因子权重 w_f
        as_of_date: date,
        top_k: int = 5,
    ) -> AttributionResult:
        if weights.empty or factor_z.empty:
            return AttributionResult(
                as_of_date=as_of_date.isoformat(),
                portfolio_contribution={},
                per_stock={},
                summary="组合为空",
            )

        # 对齐 weights.index 到 factor_z.index
        common = weights.index.intersection(factor_z.index)
        w_aligned = pd.Series(weights).loc[common]
        z_aligned = factor_z.loc[common].fillna(0.0)

        # contribution matrix: rows=symbol, cols=factor
        # contribution_{s,f} = z_{s,f} × w_f
        contrib = z_aligned.mul(factor_weights, axis=1)

        # portfolio_contribution_f = Σ_s W_s × contrib_{s,f}
        port_contrib = (contrib.T @ w_aligned).to_dict()

        # per_stock: 每股取 top_k |contribution|
        per_stock: dict[str, list[dict[str, Any]]] = {}
        for sym in contrib.index:
            row = contrib.loc[sym].sort_values(key=lambda x: x.abs(), ascending=False)
            top = row.head(top_k)
            per_stock[str(sym)] = [
                {"name": str(name), "contribution": float(value)}
                for name, value in top.items()
            ]

        # 一句话 summary：top 3 factor 贡献
        top_factor_contrib = sorted(
            port_contrib.items(), key=lambda kv: abs(kv[1]), reverse=True
        )[:3]
        summary = "组合 top 因子贡献：" + ", ".join(
            f"{name} {value:+.4f}" for name, value in top_factor_contrib
        ) if top_factor_contrib else "无显著因子贡献"

        return AttributionResult(
            as_of_date=as_of_date.isoformat(),
            portfolio_contribution={str(k): float(v) for k, v in port_contrib.items()},
            per_stock=per_stock,
            summary=summary,
        )
