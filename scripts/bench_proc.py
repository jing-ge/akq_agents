"""进程池能否吃满多核跑 CodeFactor (不动数值的纯并行)。带 __main__ 保护 (macOS spawn)。"""
import sys, time, tempfile, os, sqlite3
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, "src")
from concurrent.futures import ProcessPoolExecutor, as_completed

# --- 模块顶层数据构造 (子进程 import 时也会跑, 但用缓存避免重复) ---
def _load():
    c=sqlite3.connect("data/meta.db")
    codes=c.execute("SELECT factor_name, recipe_code FROM factor_proposals WHERE recipe_kind='code' AND recipe_code IS NOT NULL LIMIT 8").fetchall()
    c.close()
    rng=np.random.default_rng(1)
    dates=pd.bdate_range("2024-01-01",periods=180).date
    syms=[f"{i:06d}" for i in range(500)]
    rows=[];price={s:10.0 for s in syms}
    for d in dates:
        for s in syms:
            price[s]*=1+rng.standard_normal()*0.02;p=price[s]
            rows.append({"date":d,"symbol":s,"open":p,"high":p*1.01,"low":p*0.99,"close":p,"volume":1e5,"amount":p*1e5})
    ohlcv=pd.DataFrame(rows)
    close=ohlcv.pivot_table(index="date",columns="symbol",values="close",aggfunc="last").sort_index()
    from akq_agents.services.factors.base import compute_forward_returns
    fr=compute_forward_returns(close)
    return codes, ohlcv, close, fr

_CODES,_OHLCV,_CLOSE,_FR = _load()

def run_one(nc):
    name, code = nc
    from akq_agents.services.factors.sandbox import compile_code_factor
    from akq_agents.services.factors.base import CodeFactor
    from akq_agents.services.factors.history_backfill import HistoryBackfillContext, backfill_one, compute_factor_history_vectorized
    from akq_agents.services.portfolio.evaluator import FactorEvaluator
    fn,ch=compile_code_factor(code, timeout_s=10.0)
    f=CodeFactor(name=name, source_code=code, fn=fn, code_hash=ch)
    import tempfile as _tf
    dbp=Path(_tf.gettempdir())/f"bench_{os.getpid()}.db"
    ev=FactorEvaluator(dbp, window=60)
    ctx=HistoryBackfillContext.from_existing(ohlcv=_OHLCV,close=_CLOSE,forward_returns=_FR,window=60,days=90,step=1)
    backfill_one(f,ctx,evaluator=ev,compute_factor_history=compute_factor_history_vectorized,mode="full")
    return name

if __name__=="__main__":
    n=len(_CODES)
    print(f"物理核={os.cpu_count()}, {n} 个真实 CodeFactor, 500symbol×180天")
    # serial
    t0=time.perf_counter()
    for nc in _CODES: run_one(nc)
    t_ser=time.perf_counter()-t0
    # process pool 6
    t0=time.perf_counter()
    with ProcessPoolExecutor(max_workers=6) as ex:
        list(as_completed([ex.submit(run_one,nc) for nc in _CODES]))
    t_proc=time.perf_counter()-t0
    print(f"  serial      : {t_ser:6.2f}s  ({t_ser/n*1000:.0f}ms/因子)")
    print(f"  process(6)  : {t_proc:6.2f}s  ({t_proc/n*1000:.0f}ms/因子)")
    print(f"  进程池加速: {t_ser/t_proc:.2f}x")
    print(f"  1087因子外推: serial {t_ser/n*1087/60:.1f}min → 进程池 {t_proc/n*1087/60:.1f}min")
