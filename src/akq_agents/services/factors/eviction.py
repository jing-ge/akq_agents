"""M19: 因子池淘汰 — 量化打分 + 排序删低分。

用户需求: "做优质因子，定时淘汰探索一看就很差的因子"。

## 设计原则 (一视同仁, 不给 builtin/accepted 绝对保护)

factor_score = 0.5 * |EWMA_30d_IR|
             + 0.3 * |t_stat| / 3.0    (clipped 到 [0, 1])
             + 0.2 * status_weight     (builtin=1, accepted=0.9, shadow=0.5, demoted=0.1, rejected=0.0)

排序按 score 升序, 淘汰策略:
- 硬上限: 总盘 > max_pool_size → 删到 max_pool_size
- 软淘汰: score < min_score 也删 (即使没超上限)

两层情境约束 (不是"永久保护"):
1. created_at 距今 < new_factor_grace_days (默认 14 天) — 给新因子 backfill + 观察
2. 出现在最近 portfolio_snapshots.top_factors_json — "今天还在产生权重"
   (明天掉出 top 50 就可以淘了, 跟"是否 builtin"无关)

物理删除 factor_proposals + factor_metrics + factor.evicted event 记账。
（M19 review P0-2: 已改成软删除 — 标 evicted_at 不动 factor_metrics, 保留
portfolio_snapshots 老快照的归因可解释性. 读路径过滤 evicted_at IS NULL.）
不动 registry (内存) — daemon restart 后 restore_accepted_factors 会自动同步。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


_BUILTIN_PREFIXES = ("momentum_", "reversal_", "volatility_", "amount_", "log_amount_")


@dataclass
class EvictionConfig:
    """淘汰规则参数 (system.yaml/scheduler.yaml 可调)."""

    max_pool_size: int = 300            # 总盘硬上限
    min_score: float = 0.05             # 软淘汰阈值
    new_factor_grace_days: int = 14     # 新因子保护期
    score_weight_ir: float = 0.5
    score_weight_t_stat: float = 0.3
    score_weight_status: float = 0.2
    ewma_window_days: int = 30
    dry_run: bool = False


@dataclass
class FactorScore:
    factor_name: str
    score: float
    ewma_ir: float
    t_stat: float | None
    status_w: float
    status: str
    created_at: str
    in_use: bool          # 出现在最近 portfolio_snapshots top_factors_json
    protected_by: str | None  # "new_grace" | "in_use" | None
    reason: str           # 排序后给出的淘汰理由 (low_score / over_pool_size / protected)


def _status_weight(status: str, name: str) -> float:
    if any(name.startswith(p) for p in _BUILTIN_PREFIXES):
        return 1.0
    return {
        "accepted": 0.9,
        "shadow": 0.5,
        "demoted": 0.1,
        "rejected": 0.0,
    }.get(status, 0.0)


def _ewma_abs_ir(metrics_ir_series: list[float | None], half_life: int = 30) -> float:
    """EWMA(half_life=30) 的 |IR|. 负 IR 截到 0 (反向无用就视为 0).

    metrics_ir_series: list of IR values DESC by date (最新在前). 与 composite.py:_ewma_abs_ir 一致.
    """
    import math
    vals = [max(float(v), 0.0) for v in metrics_ir_series if v is not None]
    if not vals:
        return 0.0
    weights = [math.pow(0.5, i / half_life) for i in range(len(vals))]
    wsum = sum(weights)
    if wsum <= 0:
        return 0.0
    return sum(w * v for w, v in zip(weights, vals)) / wsum


def _read_recent_top_factors(conn) -> set[str]:
    """读最近 portfolio_snapshots 出现过的因子 (top_factors_json 提到的 name 集合)."""
    row = conn.execute(
        "SELECT MAX(as_of_date) FROM portfolio_snapshots"
    ).fetchone()
    if not row or not row[0]:
        return set()
    latest_date = row[0]
    rows = conn.execute(
        "SELECT top_factors_json FROM portfolio_snapshots WHERE as_of_date=?",
        (latest_date,),
    ).fetchall()
    names: set[str] = set()
    for (raw,) in rows:
        if not raw:
            continue
        try:
            for item in json.loads(raw):
                n = item.get("name")
                if n:
                    names.add(n)
        except Exception:  # noqa: BLE001
            continue
    return names


def compute_factor_scores(
    *,
    meta_db_path: Path,
    cfg: EvictionConfig | None = None,
) -> list[FactorScore]:
    """计算所有因子的 score, 返回按 score 升序排列的列表 (最差在前)."""
    cfg = cfg or EvictionConfig()
    cutoff_iso = (datetime.now() - timedelta(days=cfg.new_factor_grace_days)).isoformat()

    out: list[FactorScore] = []
    with open_meta_db(meta_db_path) as conn:
        in_use = _read_recent_top_factors(conn)

        rows = conn.execute(
            """
            SELECT factor_name, status, ir, t_stat, created_at
            FROM factor_proposals
            WHERE evicted_at IS NULL
            """
        ).fetchall()

        for name, status, ir, t_stat, created_at in rows:
            # 拉最近 ewma_window_days × 2 期的 IR 历史 (给 EWMA 留余量)
            limit_n = cfg.ewma_window_days * 2
            ir_rows = conn.execute(
                "SELECT ir FROM factor_metrics WHERE factor_name=? "
                "ORDER BY as_of_date DESC LIMIT ?",
                (name, limit_n),
            ).fetchall()
            ir_series = [r[0] for r in ir_rows]
            ewma = _ewma_abs_ir(ir_series, half_life=cfg.ewma_window_days)
            t_norm = min(abs(t_stat) / 3.0, 1.0) if t_stat is not None else 0.0
            sw = _status_weight(status, name)
            score = (
                cfg.score_weight_ir * ewma
                + cfg.score_weight_t_stat * t_norm
                + cfg.score_weight_status * sw
            )

            # 情境保护 (软, 非绝对) — 只保护"还没观察够"的活因子,
            # rejected/demoted 已经是评估结论, 不再保护期内
            protected_by = None
            if status in ("shadow", "llm_suggested", "accepted") and created_at and created_at >= cutoff_iso:
                protected_by = "new_grace"
            elif name in in_use:
                protected_by = "in_use"

            out.append(FactorScore(
                factor_name=name,
                score=round(score, 4),
                ewma_ir=round(ewma, 4),
                t_stat=t_stat,
                status_w=sw,
                status=status,
                created_at=created_at or "",
                in_use=(name in in_use),
                protected_by=protected_by,
                reason="",  # 排序时填
            ))

    # 也把 builtin 加进来 (factor_proposals 里没记录, 但有 factor_metrics 数据)
    out.extend(_load_builtin_scores(meta_db_path, cfg, in_use, cutoff_iso))

    out.sort(key=lambda fs: fs.score)
    return out


def _load_builtin_scores(
    meta_db_path: Path,
    cfg: EvictionConfig,
    in_use: set[str],
    cutoff_iso: str,
) -> list[FactorScore]:
    """builtin 因子 (momentum_*/reversal_*/...) 不在 factor_proposals, 单独算分."""
    out: list[FactorScore] = []
    limit_n = cfg.ewma_window_days * 2
    with open_meta_db(meta_db_path) as conn:
        rows = conn.execute(
            """
            SELECT factor_name, MIN(as_of_date) AS first_at
            FROM factor_metrics
            WHERE """
            + " OR ".join(f"factor_name LIKE '{p}%'" for p in _BUILTIN_PREFIXES)
            + " GROUP BY factor_name"
        ).fetchall()
        for name, first_at in rows:
            ir_rows = conn.execute(
                "SELECT ir, t_stat FROM factor_metrics WHERE factor_name=? "
                "ORDER BY as_of_date DESC LIMIT ?",
                (name, limit_n),
            ).fetchall()
            if not ir_rows:
                continue
            ir_series = [r[0] for r in ir_rows]
            ewma = _ewma_abs_ir(ir_series, half_life=cfg.ewma_window_days)
            latest_t = ir_rows[0][1] if ir_rows else None
            t_norm = min(abs(latest_t) / 3.0, 1.0) if latest_t is not None else 0.0
            sw = _status_weight("accepted", name)  # builtin → 1.0 (内部走 prefix 判定)
            score = (
                cfg.score_weight_ir * ewma
                + cfg.score_weight_t_stat * t_norm
                + cfg.score_weight_status * sw
            )
            # builtin 因子不走 new_grace (它们本来就是系统起手因子, 没有"新旧"概念)
            # 只看 in_use 保护 — 还在产权重就留着
            protected_by = "in_use" if name in in_use else None
            out.append(FactorScore(
                factor_name=name,
                score=round(score, 4),
                ewma_ir=round(ewma, 4),
                t_stat=latest_t,
                status_w=sw,
                status="builtin",
                created_at=first_at or "",
                in_use=(name in in_use),
                protected_by=protected_by,
                reason="",
            ))
    return out


def select_victims(
    scores: list[FactorScore],
    *,
    cfg: EvictionConfig | None = None,
) -> list[FactorScore]:
    """从 scores (按 score 升序) 选出该淘汰的因子.

    规则:
    - protected_by 非 None → 跳过 (但仍然给 reason 用于日志)
    - score < cfg.min_score → 淘汰 (reason='low_score')
    - 剩下: 如果总盘 > cfg.max_pool_size → 删超出部分 (最低 score 的优先, reason='over_pool_size')
    """
    cfg = cfg or EvictionConfig()
    victims: list[FactorScore] = []
    pool_count = len(scores)
    for fs in scores:
        if fs.protected_by is not None:
            fs.reason = f"protected:{fs.protected_by}"
            continue
        if fs.score < cfg.min_score:
            fs.reason = f"low_score:{fs.score:.3f}<{cfg.min_score}"
            victims.append(fs)
        elif pool_count - len(victims) > cfg.max_pool_size:
            fs.reason = "over_pool_size:rank_bottom"
            victims.append(fs)
        else:
            fs.reason = "kept"
            # 排序是升序 — 一旦剩余 ≤ max_pool_size 且 score ≥ min_score, 后面的都更高分, 全留
            break
    return victims


def evict_factors(
    *,
    meta_db_path: Path,
    state_store: Any | None = None,
    cfg: EvictionConfig | None = None,
) -> dict[str, Any]:
    """执行一次淘汰. 返回 stats {evicted, kept, dry_run, max_pool_size, min_score, victims:[...]}.

    Args:
        meta_db_path: data/meta.db
        state_store: 可选, SchedulerStateStore 用于写 events.factor.evicted
        cfg: EvictionConfig (dry_run=True 时只统计不删)
    """
    cfg = cfg or EvictionConfig()
    scores = compute_factor_scores(meta_db_path=meta_db_path, cfg=cfg)
    victims = select_victims(scores, cfg=cfg)

    victim_payload = [
        {
            "name": v.factor_name,
            "score": v.score,
            "ewma_ir": v.ewma_ir,
            "t_stat": v.t_stat,
            "status": v.status,
            "in_use": v.in_use,
            "created_at": v.created_at,
            "reason": v.reason,
        }
        for v in victims
    ]

    if not cfg.dry_run and victims:
        _soft_delete(meta_db_path, [v.factor_name for v in victims])

    stats = {
        "dry_run": cfg.dry_run,
        "pool_total_before": len(scores),
        "pool_total_after": len(scores) - (0 if cfg.dry_run else len(victims)),
        "victims_n": len(victims),
        "max_pool_size": cfg.max_pool_size,
        "min_score": cfg.min_score,
        "victims": victim_payload[:50],  # event 里 top 50 即可
    }

    if state_store is not None:
        try:
            state_store.write_event(
                level="info",
                kind="factor.evicted" if not cfg.dry_run else "factor.eviction_dry_run",
                source="eviction",
                payload=stats,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("write factor.evicted event failed: %s", exc)

    logger.info(
        "eviction: dry_run=%s pool=%d→%d, victims=%d",
        cfg.dry_run, stats["pool_total_before"], stats["pool_total_after"], stats["victims_n"],
    )
    return stats


def _soft_delete(meta_db_path: Path, names: list[str]) -> None:
    """M19 review P0-2: 软删除 — 标 evicted_at 而非物理 DELETE.

    不动 factor_metrics 历史行, 让 portfolio_snapshots 老快照引用的因子仍可解释
    (用户查 30 天前的 attribution 不会断链).

    factor_proposals 行保留, 通过 evicted_at IS NULL 过滤. restore_accepted_factors
    / compute_factor_scores / list_active 等读路径需要 (并已经) 过滤掉 evicted。
    """
    if not names:
        return
    ts = datetime.now().isoformat(timespec="seconds")
    placeholders = ",".join(["?"] * len(names))
    with open_meta_db(meta_db_path) as conn:
        conn.execute(
            f"UPDATE factor_proposals SET evicted_at=? WHERE factor_name IN ({placeholders}) AND evicted_at IS NULL",
            (ts, *names),
        )
        conn.commit()


def _physical_delete(meta_db_path: Path, names: list[str]) -> None:
    """物理删 factor_proposals + factor_metrics (按 factor_name).

    M19 review P0-2: 已不被 evict_factors 默认使用 (改成软删除). 保留供未来
    "彻底清理超老 evicted 因子" cron 调用; 调用方需自己确认 portfolio_snapshots
    引用已迁移。
    """
    if not names:
        return
    placeholders = ",".join(["?"] * len(names))
    with open_meta_db(meta_db_path) as conn:
        conn.execute(
            f"DELETE FROM factor_metrics WHERE factor_name IN ({placeholders})",
            names,
        )
        conn.execute(
            f"DELETE FROM factor_proposals WHERE factor_name IN ({placeholders})",
            names,
        )
        conn.commit()
