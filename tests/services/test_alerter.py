"""M17 Alerter 测试: 3 个 check 规则 + cooldown + macOS notify 隔离。

mock macOS subprocess 避免真发通知。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from akq_agents.orchestrator.state_store import SchedulerStateStore
from akq_agents.services.alerter import Alerter, _is_builtin_factor_name


# ---------- helpers ----------

def _setup_db(tmp_path: Path) -> tuple[Path, SchedulerStateStore, Alerter]:
    db = tmp_path / "meta.db"
    # 触发表创建
    store = SchedulerStateStore(db)
    # 也需要 portfolio_nav 和 factor_metrics 表 (alerter 查它们)
    with sqlite3.connect(db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_nav (
                as_of_date TEXT PRIMARY KEY,
                nav_gross REAL, nav_net REAL,
                daily_return_net REAL,
                turnover REAL, cost REAL,
                benchmark_nav REAL, benchmark_return REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS factor_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT NOT NULL,
                factor_version INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                ic_mean REAL, ic_std REAL, ir REAL, t_stat REAL,
                status TEXT, reason TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS factor_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT UNIQUE NOT NULL,
                recipe_json TEXT, direction TEXT, status TEXT,
                ic_mean REAL, ic_std REAL, ir REAL, t_stat REAL,
                max_abs_corr REAL, reason TEXT,
                created_at TEXT, evaluated_at TEXT,
                shadow_started_at TEXT, oos_observations INTEGER, oos_ir REAL
            )
        """)
        con.commit()
        # M19: 塞一行今日 factor_metrics, 避免新加的 _check_factor_metrics_freshness 触发
        # alert.factor_metrics.empty / .stale 干扰原本只测 NAV / refresh / decay 的用例。
        # 用例如果要专门测 freshness, 应该单独清空这条数据。
        from datetime import date as _date
        con = sqlite3.connect(db)
        try:
            con.execute(
                "INSERT INTO factor_metrics (factor_name, factor_version, as_of_date, "
                "window_days, ic_mean, ic_std, ir, t_stat, status, reason) "
                "VALUES (?, 1, ?, 60, 0.01, 0.05, 0.20, 1.5, 'active', NULL)",
                ("momentum_5", _date.today().isoformat()),
            )
            con.commit()
        finally:
            con.close()
    # M20: 给 backup_freshness check 造一个今日备份 manifest, 避免它干扰原本只测
    # NAV / refresh / decay 的 fixture
    backup_dir = db.parent / "backup"
    backup_dir.mkdir(exist_ok=True)
    from datetime import date as _date
    (backup_dir / "LAST_BACKUP").write_text(
        f"date={_date.today().strftime('%Y%m%d')}\n"
        f"ts={_date.today().isoformat()}\n"
        f"path={backup_dir}\n"
    )
    alerter = Alerter(
        meta_db_path=db,
        state_store=store,
        nav_max_abs_daily_return=0.15,
        refresh_max_consecutive_failed=2,
        factor_decay_min_abs_ir=0.05,
    )
    return db, store, alerter


# ---------- _check_nav ----------

def test_check_nav_triggers_on_abnormal_daily_return(tmp_path: Path) -> None:
    """NAV 单日 -20% 应该触发 alert.nav.abnormal."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO portfolio_nav (as_of_date, nav_net, daily_return_net) "
            "VALUES (?, ?, ?)",
            ("2026-06-23", 0.80, -0.20),
        )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert out["n_alerts"] == 1
    assert "alert.nav.abnormal" in out["alerts"]
    # 应该写了 event
    events = store.list_events(limit=10)
    assert any(e.kind == "alert.nav.abnormal" for e in events)


def test_check_nav_no_alert_when_normal(tmp_path: Path) -> None:
    """NAV +5% 在阈值内，不触发."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO portfolio_nav (as_of_date, nav_net, daily_return_net) "
            "VALUES (?, ?, ?)",
            ("2026-06-23", 1.05, 0.05),
        )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert out["n_alerts"] == 0


# ---------- _check_data_refresh ----------

def test_check_refresh_triggers_on_consecutive_failed(tmp_path: Path) -> None:
    """data.refresh_daily 最近 2 次 failed 应该触发."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        for d in ("2026-06-22", "2026-06-23"):
            con.execute(
                "INSERT INTO job_runs (job_id, partition, status, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("data.refresh_daily", d, "failed", d + "T16:00:00", d + "T16:01:00"),
            )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.data.refresh_failed" in out["alerts"]


def test_check_refresh_no_alert_when_mixed(tmp_path: Path) -> None:
    """最近 2 次有 1 次 ok 不触发."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        for d, status in [("2026-06-22", "failed"), ("2026-06-23", "ok")]:
            con.execute(
                "INSERT INTO job_runs (job_id, partition, status, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("data.refresh_daily", d, status, d + "T16:00:00", d + "T16:01:00"),
            )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.data.refresh_failed" not in out.get("alerts", [])


# ---------- _check_factor_decay ----------

def test_check_factor_decay_triggers_for_accepted(tmp_path: Path) -> None:
    """accepted 因子近 30 天平均 |IR| < 0.05 → alert.factor.decayed."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        # 注册一个 accepted 因子
        con.execute(
            "INSERT INTO factor_proposals (factor_name, status, created_at) "
            "VALUES (?, ?, ?)",
            ("auto_test_factor_x", "accepted", "2026-06-01T00:00:00"),
        )
        # 给它写 5 条 metrics 全部 |IR| 很小
        today_str = date.today().isoformat()
        for i in range(5):
            con.execute(
                "INSERT INTO factor_metrics (factor_name, factor_version, as_of_date, "
                "window_days, ic_mean, ic_std, ir, t_stat, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("auto_test_factor_x", 1, today_str, 60, 0.001, 0.5, 0.02, 0.5, "ok"),
            )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.factor.decayed" in out["alerts"]


# ---------- cooldown ----------

def test_cooldown_prevents_repeat_macos_notify(tmp_path: Path) -> None:
    """cooldown 内同 kind+payload 只发一次 macOS notify, 但都写 event."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO portfolio_nav (as_of_date, nav_net, daily_return_net) "
            "VALUES (?, ?, ?)",
            ("2026-06-23", 0.80, -0.20),
        )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run") as mock_run:
        alerter.run_check()  # 第 1 次
        alerter.run_check()  # 第 2 次同 NAV 数据
    # macOS notify (osascript) 应该只被调一次
    if sys.platform == "darwin":
        assert mock_run.call_count == 1, f"expected 1 osascript call, got {mock_run.call_count}"
    # events 应该写了 2 次（每次 run_check 都写 event，方便审计）
    events = [e for e in store.list_events(limit=10) if e.kind == "alert.nav.abnormal"]
    assert len(events) == 2


# ---------- _check_factor_metrics_freshness (M19) ----------

def test_check_factor_metrics_empty_triggers(tmp_path: Path) -> None:
    """M19: factor_metrics 表为空时触发 alert.factor_metrics.empty."""
    db, store, alerter = _setup_db(tmp_path)
    # 清掉 _setup_db 默认塞的那行
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM factor_metrics")
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.factor_metrics.empty" in out["alerts"]


def test_check_factor_metrics_stale_triggers(tmp_path: Path) -> None:
    """M19: factor_metrics 最近一行 > 阈值天数时触发 alert.factor_metrics.stale."""
    db, store, alerter = _setup_db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM factor_metrics")
        # 塞一行 10 天前的, 默认阈值 3 天 → 必然触发
        old = (date.today() - timedelta(days=10)).isoformat()
        con.execute(
            "INSERT INTO factor_metrics (factor_name, factor_version, as_of_date, "
            "window_days, ic_mean, ic_std, ir, t_stat, status, reason) "
            "VALUES ('momentum_5', 1, ?, 60, 0.01, 0.05, 0.20, 1.5, 'active', NULL)",
            (old,),
        )
        con.commit()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.factor_metrics.stale" in out["alerts"]


def test_check_factor_metrics_fresh_no_alert(tmp_path: Path) -> None:
    """M19: 今日有写入 → 不触发 freshness 告警."""
    db, store, alerter = _setup_db(tmp_path)  # fixture 已塞今日数据
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.factor_metrics.stale" not in out["alerts"]
    assert "alert.factor_metrics.empty" not in out["alerts"]


# ---------- _check_backup_freshness (M20) ----------

def test_check_backup_missing_triggers(tmp_path: Path) -> None:
    """M20: 没有 LAST_BACKUP 文件 → alert.backup.missing."""
    db, store, alerter = _setup_db(tmp_path)
    # 删掉 _setup_db 创的 LAST_BACKUP
    (db.parent / "backup" / "LAST_BACKUP").unlink()
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.backup.missing" in out["alerts"]


def test_check_backup_stale_triggers(tmp_path: Path) -> None:
    """M20: 备份 > 10 天前 → alert.backup.stale."""
    db, store, alerter = _setup_db(tmp_path)
    old = (date.today() - timedelta(days=15)).strftime("%Y%m%d")
    (db.parent / "backup" / "LAST_BACKUP").write_text(
        f"date={old}\nts={old}\npath={db.parent}\n"
    )
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.backup.stale" in out["alerts"]


def test_check_backup_fresh_no_alert(tmp_path: Path) -> None:
    """M20: 今日备份 → 不触发任何 backup alert."""
    db, store, alerter = _setup_db(tmp_path)  # fixture 已塞今日 LAST_BACKUP
    with patch("akq_agents.services.alerter.subprocess.run"):
        out = alerter.run_check()
    assert "alert.backup.missing" not in out["alerts"]
    assert "alert.backup.stale" not in out["alerts"]


# ---------- helpers ----------

def test_is_builtin_factor_name() -> None:
    assert _is_builtin_factor_name("momentum_5")
    assert _is_builtin_factor_name("amount_20")
    assert not _is_builtin_factor_name("auto_rolling_std_close_20_long_abc123")
    assert not _is_builtin_factor_name("llm_zscore_amount_30_short_def456")
