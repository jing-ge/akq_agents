"""拉沪深300 历史数据写到 ohlcv parquet 缓存。

策略：以 "000300" 为 symbol，把每日 OHLC 写成跟个股一样的 parquet 分区结构，
backtester 用 ds.field("symbol").isin([..., benchmark_symbol]) 就能拿到。
"""

import sys
sys.path.insert(0, "src")

from datetime import date as _date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

OHLCV_DIR = Path("data/parquet/ohlcv")
BENCH_SYMBOL = "000300"


def main() -> None:
    import akshare as ak

    print(f"=== 拉沪深300 (sh{BENCH_SYMBOL}) 历史 ===")
    df = ak.stock_zh_index_daily(symbol=f"sh{BENCH_SYMBOL}")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    print(f"akshare 返回 {len(df)} 行（{df['date'].min()} ~ {df['date'].max()}）")

    # 限定到 ohlcv 覆盖范围内（最近 2 年）
    ohlcv_days = sorted(p.name.split("=", 1)[1] for p in OHLCV_DIR.glob("date=*"))
    if not ohlcv_days:
        print("ohlcv 目录为空，跳过")
        return
    min_d = _date.fromisoformat(ohlcv_days[0])
    max_d = _date.fromisoformat(ohlcv_days[-1])
    sub = df[(df["date"] >= min_d) & (df["date"] <= max_d)].copy()
    print(f"  覆盖范围 {min_d} ~ {max_d}：{len(sub)} 行")

    # 对每个交易日，把 benchmark 一行 append 到该日的 ohlcv parquet
    # 注意：part.parquet 已存在 → 我们读出来 + concat + 重写（最简单做法）
    written = 0
    skipped = 0
    for _, row in sub.iterrows():
        d = row["date"]
        day_dir = OHLCV_DIR / f"date={d.isoformat()}"
        if not day_dir.exists():
            continue
        part_file = day_dir / "part.parquet"
        if not part_file.exists():
            files = list(day_dir.glob("*.parquet"))
            if not files:
                continue
            part_file = files[0]

        existing = pq.read_table(part_file).to_pandas()
        if BENCH_SYMBOL in existing["symbol"].astype(str).values:
            skipped += 1
            continue
        # drop date 列：date 在 parquet hive 分区路径里，不在数据列里写
        existing_no_date = existing.drop(columns=["date"], errors="ignore")
        # 构造 benchmark 行（保持现有列结构）
        new_row: dict = {"symbol": BENCH_SYMBOL}
        for col in existing_no_date.columns:
            if col == "symbol":
                continue
            if col in row.index:
                new_row[col] = float(row[col])
            elif col == "amount":
                # akshare 指数有 volume 没 amount，用 close*volume 近似
                new_row[col] = float(row.get("volume", 0)) * float(row.get("close", 0))
            else:
                new_row[col] = 0.0

        new_df = pd.DataFrame([new_row], columns=existing_no_date.columns)
        combined = pd.concat([existing_no_date, new_df], ignore_index=True)
        pq.write_table(pa.Table.from_pandas(combined, preserve_index=False), part_file)
        written += 1

    print(f"完成：新写入 {written} 行，跳过 {skipped} 行（已存在）")


if __name__ == "__main__":
    main()
