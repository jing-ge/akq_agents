"""tests for services/factors/eviction.py."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from akq_agents.services.factors.eviction import (
    EvictionConfig,
    compute_factor_scores,
    evict_factors,
    select_victims,
)


def _setup_db(tmp_path: Path) -> Path:
    """建一个最小 meta.db, 塞几个不同状态的因子."""
    db = tmp_path / "meta.db"
    now = datetime.now()
    old = (now - timedelta(days=30)).isoformat(timespec="seconds")
    new = (now - timedelta(days=5)).isoformat(timespec="seconds")

    with sqlite3.connect(db) as con:
        con.execute("""
            CREATE TABLE factor_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT UNIQUE NOT NULL,
                recipe_json TEXT, direction TEXT, status TEXT,
                ic_mean REAL, ic_std REAL, ir REAL, t_stat REAL,
                max_abs_corr REAL, reason TEXT,
                created_at TEXT, evaluated_at TEXT,
                shadow_started_at TEXT, oos_observations INTEGER, oos_ir REAL
            )
        """)
        con.execute("""
            CREATE TABLE factor_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT NOT NULL,
                factor_version INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                ic_mean REAL, ic_std REAL, ir REAL, t_stat REAL,
                status TEXT, reason TEXT,
                UNIQUE(factor_name, factor_version, as_of_date, window_days)
            )
        """)
        con.execute("""
            CREATE TABLE portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of_date TEXT, symbol TEXT, name TEXT, industry TEXT,
                weight REAL, prev_weight REAL, composite_score REAL,
                top_factors_json TEXT,
                UNIQUE(as_of_date, symbol)
            )
        """)

        # 1) 高分活因子 (shadow IR 高, 应保留)
        con.execute(
            "INSERT INTO factor_proposals (factor_name, recipe_json, direction, status, ir, t_stat, created_at) "
            "VALUES ('llm_good_xyz', '{}', 'long', 'shadow', 0.3, 2.5, ?)",
            (old,),
        )
        con.execute(
            "INSERT INTO factor_metrics VALUES (NULL, 'llm_good_xyz', 1, '2026-06-25', 90, 0.04, 0.1, 0.3, 2.5, 'active', NULL)",
        )

        # 2) 低分 rejected (老的, 应被淘汰)
        con.execute(
            "INSERT INTO factor_proposals (factor_name, recipe_json, direction, status, ir, t_stat, created_at) "
            "VALUES ('auto_bad_old', '{}', 'long', 'rejected', 0.01, 0.1, ?)",
            (old,),
        )

        # 3) 低分但新 (rejected 状态不享受 new_grace, 应被淘汰)
        con.execute(
            "INSERT INTO factor_proposals (factor_name, recipe_json, direction, status, ir, t_stat, created_at) "
            "VALUES ('auto_bad_new', '{}', 'long', 'rejected', 0.01, 0.1, ?)",
            (new,),
        )

        # 4) 低分 shadow 但新 (享受 new_grace, 应保留)
        con.execute(
            "INSERT INTO factor_proposals (factor_name, recipe_json, direction, status, ir, t_stat, created_at) "
            "VALUES ('llm_grace_new', '{}', 'long', 'shadow', 0.01, 0.1, ?)",
            (new,),
        )

        # 5) 低分 shadow 但 in_use (出现在最近 portfolio_snapshots, 应保留)
        con.execute(
            "INSERT INTO factor_proposals (factor_name, recipe_json, direction, status, ir, t_stat, created_at) "
            "VALUES ('llm_in_use_low', '{}', 'long', 'shadow', 0.01, 0.1, ?)",
            (old,),
        )
        con.execute(
            "INSERT INTO portfolio_snapshots VALUES (NULL, '2026-06-25', '600519', '茅台', '酒', 0.05, 0.05, 0.5, "
            "'[{\"name\":\"llm_in_use_low\",\"contribution\":0.1}]')",
        )

        con.commit()
    return db


# ---------- compute_factor_scores ----------

def test_compute_factor_scores_basic(tmp_path: Path) -> None:
    """5 个 fixture 因子都被算到 score."""
    db = _setup_db(tmp_path)
    scores = compute_factor_scores(meta_db_path=db, cfg=EvictionConfig())
    assert len(scores) == 5
    names = {s.factor_name for s in scores}
    assert names == {"llm_good_xyz", "auto_bad_old", "auto_bad_new", "llm_grace_new", "llm_in_use_low"}


def test_score_high_for_good_factor(tmp_path: Path) -> None:
    """ir=0.3 t=2.5 shadow → score 应该 > 0.2."""
    db = _setup_db(tmp_path)
    scores = compute_factor_scores(meta_db_path=db, cfg=EvictionConfig())
    good = next(s for s in scores if s.factor_name == "llm_good_xyz")
    assert good.score > 0.2, f"good factor score too low: {good.score}"


def test_score_low_for_bad_factor(tmp_path: Path) -> None:
    """ir=0.01 t=0.1 rejected → score < 0.05."""
    db = _setup_db(tmp_path)
    scores = compute_factor_scores(meta_db_path=db, cfg=EvictionConfig())
    bad = next(s for s in scores if s.factor_name == "auto_bad_old")
    assert bad.score < 0.05, f"bad factor score too high: {bad.score}"


# ---------- protection ----------

def test_new_grace_protects_only_active_status(tmp_path: Path) -> None:
    """new_grace 只保护 shadow/llm_suggested/accepted, 不保护 rejected/demoted."""
    db = _setup_db(tmp_path)
    scores = compute_factor_scores(meta_db_path=db, cfg=EvictionConfig())
    s_map = {s.factor_name: s for s in scores}
    assert s_map["llm_grace_new"].protected_by == "new_grace"  # shadow + new → 保护
    assert s_map["auto_bad_new"].protected_by is None  # rejected + new → 不保护


def test_in_use_protects(tmp_path: Path) -> None:
    """出现在最近 portfolio_snapshots.top_factors_json 的因子被 in_use 保护."""
    db = _setup_db(tmp_path)
    scores = compute_factor_scores(meta_db_path=db, cfg=EvictionConfig())
    s_map = {s.factor_name: s for s in scores}
    assert s_map["llm_in_use_low"].in_use is True
    assert s_map["llm_in_use_low"].protected_by == "in_use"


# ---------- select_victims ----------

def test_victims_exclude_protected(tmp_path: Path) -> None:
    """select_victims 跳过 protected_by 非 None 的."""
    db = _setup_db(tmp_path)
    scores = compute_factor_scores(meta_db_path=db, cfg=EvictionConfig())
    victims = select_victims(scores, cfg=EvictionConfig())
    victim_names = {v.factor_name for v in victims}
    # 受保护的都不应该出现在 victims
    assert "llm_grace_new" not in victim_names
    assert "llm_in_use_low" not in victim_names
    assert "llm_good_xyz" not in victim_names  # 高分也不被淘
    # 低分 + 无保护的应被淘
    assert "auto_bad_old" in victim_names
    assert "auto_bad_new" in victim_names


# ---------- evict_factors (端到端) ----------

def test_evict_dry_run_does_not_delete(tmp_path: Path) -> None:
    """dry_run=True 时数据不动."""
    db = _setup_db(tmp_path)
    cfg = EvictionConfig(dry_run=True)
    result = evict_factors(meta_db_path=db, cfg=cfg)
    assert result["dry_run"] is True
    assert result["victims_n"] >= 2  # 至少 auto_bad_old / auto_bad_new
    # db 行数不变
    with sqlite3.connect(db) as con:
        n = con.execute("SELECT COUNT(*) FROM factor_proposals").fetchone()[0]
    assert n == 5


def test_evict_real_deletes(tmp_path: Path) -> None:
    """dry_run=False 真删除."""
    db = _setup_db(tmp_path)
    cfg = EvictionConfig(dry_run=False)
    result = evict_factors(meta_db_path=db, cfg=cfg)
    assert result["dry_run"] is False
    assert result["victims_n"] >= 2
    with sqlite3.connect(db) as con:
        names = {r[0] for r in con.execute("SELECT factor_name FROM factor_proposals").fetchall()}
    assert "auto_bad_old" not in names
    assert "auto_bad_new" not in names
    # 受保护的因子还在
    assert "llm_good_xyz" in names
    assert "llm_grace_new" in names
    assert "llm_in_use_low" in names


def test_max_pool_size_caps_pool(tmp_path: Path) -> None:
    """max_pool_size 强制总盘上限. 设极小值 (2) 验证."""
    db = _setup_db(tmp_path)
    # 5 个因子, 设上限 2 → 应该删 3 个 (但受保护的优先, 实际删 2 个 unprotected)
    cfg = EvictionConfig(dry_run=True, max_pool_size=2, min_score=0.0)  # min_score=0 不靠它淘
    result = evict_factors(meta_db_path=db, cfg=cfg)
    # 5 个里 3 个受保护 (good_xyz/grace_new/in_use_low), 上限 2 → 2 个 unprotected 被淘 + 1 个 unprotected 即使超盘也按 over_pool_size 删
    # 实际: bot_2 unprotected → low_score=0 不触发, 走 over_pool_size 删到 max_pool_size
    assert result["victims_n"] >= 2
