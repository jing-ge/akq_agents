"""拉申万一级行业分类 → 写入 meta.db.industry_map 表。

每只股票一行：(symbol, industry_code, industry_name, fetched_at)
覆盖 31 个一级行业，~5 分钟。
"""

import sys
sys.path.insert(0, "src")

import sqlite3
import time
from datetime import datetime

DB_PATH = "data/meta.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS industry_map (
  symbol TEXT PRIMARY KEY,
  industry_code TEXT NOT NULL,
  industry_name TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
"""


def main() -> None:
    import akshare as ak

    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.commit()

    print("=== 拉申万一级行业列表 ===")
    sw_industries = ak.sw_index_first_info()
    print(f"共 {len(sw_industries)} 个行业")

    ts = datetime.now().isoformat(timespec="seconds")
    total_rows = 0
    for idx, row in sw_industries.iterrows():
        code = row["行业代码"].replace(".SI", "")
        name = row["行业名称"]
        print(f"  [{idx+1}/{len(sw_industries)}] {code} {name}...", end=" ", flush=True)
        start_t = time.monotonic()
        try:
            comp = ak.index_component_sw(symbol=code)
        except Exception as e:
            print(f"FAIL: {str(e)[:80]}")
            continue
        # 写入 db
        rows = [
            (str(r["证券代码"]).zfill(6), code, name, ts)
            for _, r in comp.iterrows()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO industry_map (symbol, industry_code, industry_name, fetched_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        total_rows += len(rows)
        print(f"+{len(rows)} symbols ({time.monotonic()-start_t:.1f}s)")

    cnt = conn.execute("SELECT COUNT(*) FROM industry_map").fetchone()[0]
    industries = conn.execute("SELECT COUNT(DISTINCT industry_code) FROM industry_map").fetchone()[0]
    print(f"\n完成：industry_map 共 {cnt} 行（{industries} 个行业）")


if __name__ == "__main__":
    main()
