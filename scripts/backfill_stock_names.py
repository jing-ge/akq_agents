"""拉沪深 A 股代码 → 中文简称 → 写入 meta.db.stock_names。

数据源：``AKShareGateway.fetch_spot()`` 合并 stock_info_sh_name_code +
stock_info_sz_name_code，返回 ``[symbol, name, listing_date]``，约 5500 行，
几秒钟拉完。

用法（在项目根目录）::

    PYTHONPATH=src /opt/anaconda3/envs/akq310/bin/python scripts/backfill_stock_names.py

后续：daemon 启动时也可挂一个 weekly job 自动刷新（极少变化，不必每日跑）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 项目根目录运行
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main() -> None:
    from akq_agents.bootstrap import build_workflow
    from akq_agents.services.data.stock_names import StockNameStore

    print("=== backfill_stock_names ===")
    workflow, _ = build_workflow()
    repo = workflow.services.get("data_repository")
    if repo is None:
        print("ERROR: data_repository 未配置，无法拉 spot")
        sys.exit(1)

    print("调用 gateway.fetch_spot()...")
    df = repo._gateway.fetch_spot()
    print(f"  拿到 {len(df)} 行")

    name_map: dict[str, str] = {}
    for _, row in df.iterrows():
        sym = str(row["symbol"]).strip()
        name = row.get("name")
        if sym and name and str(name).strip():
            name_map[sym] = str(name).strip()
    print(f"  有效 (symbol, name) {len(name_map)} 对")

    store: StockNameStore = workflow.services["stock_name_store"]
    written = store.upsert_many(name_map)
    print(f"✓ stock_names 写入 {written} 行")


if __name__ == "__main__":
    main()
