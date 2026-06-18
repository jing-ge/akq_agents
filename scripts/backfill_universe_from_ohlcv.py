"""从已有 ohlcv parquet 推出每日 universe parquet。

简化策略：universe[d] = ohlcv 当日所有有数据的 symbol 集合。
不做 ST / 上市天数等过滤（这些后续由 PortfolioAgent 里的 RiskFilter 在运行时做）。
"""

import sys
sys.path.insert(0, "src")

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

OHLCV_DIR = Path("data/parquet/ohlcv")
UNIVERSE_DIR = Path("data/parquet/universe")


def main() -> None:
    if not OHLCV_DIR.exists():
        print("ohlcv dir missing")
        return
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)

    days = sorted(p.name.split("=", 1)[1] for p in OHLCV_DIR.glob("date=*"))
    print(f"将处理 {len(days)} 个 ohlcv 分区")

    created = 0
    skipped = 0
    for d in days:
        u_dir = UNIVERSE_DIR / f"date={d}"
        u_file = u_dir / "snap.parquet"
        if u_file.exists():
            skipped += 1
            continue

        ohlcv_file = OHLCV_DIR / f"date={d}" / "snap.parquet"
        if not ohlcv_file.exists():
            # 也许文件名不是 snap.parquet，找别的
            files = list((OHLCV_DIR / f"date={d}").glob("*.parquet"))
            if not files:
                continue
            ohlcv_file = files[0]

        ohlcv = pq.read_table(ohlcv_file).to_pandas()
        symbols = sorted(ohlcv["symbol"].dropna().astype(str).unique().tolist())

        # 写 universe parquet：每行一只票（无 excluded）
        frame = pd.DataFrame({
            "symbol": symbols,
            "excluded_symbol": [None] * len(symbols),
            "reason_code": [None] * len(symbols),
        })
        u_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), u_file)
        created += 1

    print(f"完成：新建 {created} 个 universe 分区，已存在 {skipped} 个")


if __name__ == "__main__":
    main()
