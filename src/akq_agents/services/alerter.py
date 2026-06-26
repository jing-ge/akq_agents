"""M17 Alerter：定期巡检关键指标，触发条件就写 events + macOS 通知。

设计原则:
- 只查表，不修改任何业务数据
- 每条规则独立，互不依赖
- 触发后写 events.alert.* level=warning/error
- macOS 上 osascript 发 system notification（非阻塞，失败静默）
- 同一 alert kind + payload 24 小时内只通知一次 (避免疲劳)

规则:
- nav.abnormal: portfolio_nav 表里最近一日 |daily_return_net| > 阈值
- data.refresh_failed: data.refresh_daily 连续 N 次 failed
- factor.decayed: accepted 因子最近 30 天平均 |IR| < 阈值
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from akq_agents.orchestrator.state_store import SchedulerStateStore
from akq_agents.services.data.repository import open_meta_db

logger = logging.getLogger(__name__)


class Alerter:
    """单机告警器。daemon job 每 30 分钟调一次 :meth:`run_check`。"""

    def __init__(
        self,
        *,
        meta_db_path: Path,
        state_store: SchedulerStateStore,
        nav_max_abs_daily_return: float = 0.15,
        refresh_max_consecutive_failed: int = 2,
        factor_decay_min_abs_ir: float = 0.05,
        factor_metrics_max_stale_days: int = 3,
        notify_cooldown_hours: int = 24,
    ) -> None:
        self._db = Path(meta_db_path)
        self._store = state_store
        self._nav_thr = nav_max_abs_daily_return
        self._refresh_thr = refresh_max_consecutive_failed
        self._factor_thr = factor_decay_min_abs_ir
        # M19: factor_metrics 表 N 天没有新写入则告警 (防止"砍 job 没人接"再次悄无声息发生)
        self._factor_metrics_stale_thr = factor_metrics_max_stale_days
        self._cooldown = timedelta(hours=notify_cooldown_hours)

    def run_check(self) -> dict[str, Any]:
        """跑一遍所有 check，返回 stats（供 events.alert.check.completed 用）。"""
        alerts: list[dict[str, Any]] = []
        try:
            alerts.extend(self._check_nav())
        except Exception as exc:  # noqa: BLE001
            logger.exception("alerter: check_nav failed: %s", exc)
        try:
            alerts.extend(self._check_data_refresh())
        except Exception as exc:  # noqa: BLE001
            logger.exception("alerter: check_data_refresh failed: %s", exc)
        try:
            alerts.extend(self._check_factor_decay())
        except Exception as exc:  # noqa: BLE001
            logger.exception("alerter: check_factor_decay failed: %s", exc)
        try:
            alerts.extend(self._check_factor_metrics_freshness())
        except Exception as exc:  # noqa: BLE001
            logger.exception("alerter: check_factor_metrics_freshness failed: %s", exc)

        for a in alerts:
            self._maybe_notify(a)
        return {"n_alerts": len(alerts), "alerts": [a["kind"] for a in alerts]}

    # ---------------- check 规则 ----------------

    def _check_nav(self) -> list[dict[str, Any]]:
        """portfolio_nav 表最近一日 |daily_return_net| > 阈值。"""
        out = []
        with open_meta_db(self._db) as conn:
            row = conn.execute(
                "SELECT as_of_date, daily_return_net FROM portfolio_nav "
                "WHERE daily_return_net IS NOT NULL "
                "ORDER BY as_of_date DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return out
        d, ret = row
        if abs(float(ret)) > self._nav_thr:
            out.append({
                "kind": "alert.nav.abnormal",
                "level": "error",
                "title": "NAV 单日异动",
                "body": f"{d}: daily_return = {float(ret)*100:.1f}% (阈值 ±{self._nav_thr*100:.0f}%)",
                "payload": {"as_of_date": d, "daily_return": float(ret), "threshold": self._nav_thr},
            })
        return out

    def _check_data_refresh(self) -> list[dict[str, Any]]:
        """data.refresh_daily 最近 N 次都是 failed → 告警。"""
        out = []
        with open_meta_db(self._db) as conn:
            rows = conn.execute(
                "SELECT partition, status FROM job_runs "
                "WHERE job_id = 'data.refresh_daily' "
                "ORDER BY started_at DESC LIMIT ?",
                (self._refresh_thr,),
            ).fetchall()
        if len(rows) < self._refresh_thr:
            return out  # 数据不足
        if all(r[1] == "failed" for r in rows):
            partitions = [r[0] for r in rows]
            out.append({
                "kind": "alert.data.refresh_failed",
                "level": "error",
                "title": "数据刷新连续失败",
                "body": f"最近 {self._refresh_thr} 次 data.refresh_daily 全部 failed: {partitions}",
                "payload": {"failed_partitions": partitions, "threshold": self._refresh_thr},
            })
        return out

    def _check_factor_metrics_freshness(self) -> list[dict[str, Any]]:
        """M19: factor_metrics 表 N 天没有新写入则告警。

        防"砍 job 没人接"再次悄无声息发生 — 之前砍 FactorAgent 时把"日级 factor_metrics 写入"
        也一起砍了, 整个表停写但 UI/alerter 没人发现, 卡了好几天才发觉。
        """
        out = []
        with open_meta_db(self._db) as conn:
            row = conn.execute(
                "SELECT MAX(as_of_date) FROM factor_metrics"
            ).fetchone()
        last_at = row[0] if row else None
        from datetime import date as _date
        if last_at is None:
            out.append({
                "kind": "alert.factor_metrics.empty",
                "level": "warning",
                "title": "factor_metrics 表为空",
                "body": "从未写入过任何因子 metrics, 检查 batch.deep_research 是否在跑",
                "payload": {"last_at": None},
            })
            return out
        try:
            last_date = _date.fromisoformat(last_at)
        except ValueError:
            return out
        stale_days = (_date.today() - last_date).days
        if stale_days > self._factor_metrics_stale_thr:
            out.append({
                "kind": "alert.factor_metrics.stale",
                "level": "warning",
                "title": "factor_metrics 长时间无新写入",
                "body": (
                    f"最近一次写入是 {last_at} ({stale_days} 天前), 阈值 {self._factor_metrics_stale_thr} 天. "
                    f"batch.deep_research 可能没跑 / 数据 empty / job 被砍"
                ),
                "payload": {
                    "last_at": last_at,
                    "stale_days": stale_days,
                    "threshold": self._factor_metrics_stale_thr,
                },
            })
        return out

    def _check_factor_decay(self) -> list[dict[str, Any]]:
        """accepted 或 builtin 因子最近 30 天 |IR| 平均 < 阈值 → 告警。"""
        out = []
        with open_meta_db(self._db) as conn:
            accepted = {
                r[0] for r in conn.execute(
                    "SELECT factor_name FROM factor_proposals WHERE status = 'accepted'"
                ).fetchall()
            }
            recent = conn.execute(
                "SELECT factor_name, AVG(ABS(ir)) FROM factor_metrics "
                "WHERE ir IS NOT NULL AND as_of_date >= date('now', '-30 days') "
                "GROUP BY factor_name HAVING COUNT(*) >= 5"
            ).fetchall()
        for name, avg_ir in recent:
            # 只对 accepted / builtin 告警；DSL shadow 自己有 demote 机制
            if name not in accepted and not _is_builtin_factor_name(name):
                continue
            if avg_ir < self._factor_thr:
                out.append({
                    "kind": "alert.factor.decayed",
                    "level": "warning",
                    "title": f"因子衰减: {name}",
                    "body": f"最近 30 天平均 |IR| = {avg_ir:.3f} (阈值 {self._factor_thr})",
                    "payload": {"factor_name": name, "avg_abs_ir": float(avg_ir), "threshold": self._factor_thr},
                })
        return out

    # ---------------- notify ----------------

    def _maybe_notify(self, alert: dict[str, Any]) -> None:
        """写 events.alert.*; cooldown 内同 kind+payload 不重复发 macOS notify。"""
        kind = alert["kind"]
        level = alert["level"]
        payload = alert.get("payload", {})

        # 先查 cooldown（在写入前查，避免误把自己算进去）
        already = self._notified_within_cooldown(kind, payload)

        # 写 events（每次都写，让 /ops 看板能看到完整历史）
        try:
            self._store.write_event(
                level=level,
                kind=kind,
                source="alerter",
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            pass

        # macOS notification: cooldown 内不重复
        if not already:
            self._notify_macos(alert["title"], alert["body"])

    def _notified_within_cooldown(self, kind: str, payload: dict[str, Any]) -> bool:
        """cooldown 内是否已经存在同 kind + 同 payload 的 alert event。"""
        try:
            cutoff = (datetime.now() - self._cooldown).isoformat()
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with open_meta_db(self._db) as conn:
                row = conn.execute(
                    "SELECT 1 FROM events WHERE kind = ? AND payload_json = ? AND ts >= ? LIMIT 1",
                    (kind, payload_json, cutoff),
                ).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _notify_macos(title: str, body: str) -> None:
        """调 osascript 发系统通知。失败静默，不影响主流程。"""
        if sys.platform != "darwin":
            return
        try:
            safe_title = title.replace('"', "'")[:120]
            safe_body = body.replace('"', "'")[:240]
            script = f'display notification "{safe_body}" with title "AKQ Agents" subtitle "{safe_title}"'
            subprocess.run(
                ["osascript", "-e", script],
                check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass


_BUILTIN_PREFIXES = ("momentum_", "reversal_", "volatility_", "amount_", "log_amount_")


def _is_builtin_factor_name(name: str) -> bool:
    return any(name.startswith(p) for p in _BUILTIN_PREFIXES)