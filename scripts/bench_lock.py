"""复现 SQLite 锁争用: 真实 meta.db 副本 + 不同 worker 数跑同批因子, 看吞吐/CPU。
若锁争用是瓶颈: worker 增加吞吐不升甚至降, CPU 停低位 (都在等锁)。"""
import sys, time, tempfile, shutil, os, threading
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, "src")
from concurrent.futures import ThreadPoolExecutor, as_completed
from akq_agents.services.factors.discovery import make_factor
from akq_agents.services.factors.proposal_store import recipe_from_json
from akq_agents.services.factors.history_backfill import (
    HistoryBackfillContext, backfill_one, compute_factor_history_vectorized)
from akq_agents.services.factors.base import compute_forward_returns
from akq_agents.services.portfolio.evaluator import FactorEvaluator

# 生产级数据
rng=np.random.default_rng(1)
dates=pd.bdate_range("2024-01-01",periods=250).date
syms=[f"{i:06d}" for i in range(500)]
rows=[];price={s:10.0 for s in syms}
for d in dates:
    for s in syms:
        price[s]*=1+rng.standard_normal()*0.02;p=price[s]
        rows.append({"date":d,"symbol":s,"open":p,"high":p*1.01,"low":p*0.99,"close":p,"volume":1e5,"amount":p*1e5})
ohlcv=pd.DataFrame(rows)
close=ohlcv.pivot_table(index="date",columns="symbol",values="close",aggfunc="last").sort_index()
fr=compute_forward_returns(close)

OPS=["rolling_mean","rolling_std","zscore","rsi","ema","delta","rolling_max","ts_max_norm"]*3
factors=[make_factor(recipe_from_json(f'{{"base":"close","op":"{op}","window":20,"direction":"long"}}')) for op in OPS]

def run_one(factor, dbpath):
    ev=FactorEvaluator(dbpath, window=90)
    ctx=HistoryBackfillContext.from_existing(ohlcv=ohlcv,close=close,forward_returns=fr,window=90,days=90,step=1)
    backfill_one(factor,ctx,evaluator=ev,compute_factor_history=compute_factor_history_vectorized,mode="full")

def measure_cpu(stop_evt, samples):
    import resource
    last=resource.getrusage(resource.RUSAGE_SELF)
    last_t=time.perf_counter()
    while not stop_evt.is_set():
        time.sleep(0.5)
        now=resource.getrusage(resource.RUSAGE_SELF); now_t=time.perf_counter()
        cpu=(now.ru_utime+now.ru_stime-last.ru_utime-last.ru_stime)/(now_t-last_t)*100
        samples.append(cpu); last, last_t = now, now_t

def bench(nworkers, real_db):
    with tempfile.TemporaryDirectory() as td:
        dbp=Path(td)/"meta.db"
        shutil.copy(real_db, dbp)  # 真实 45MB 库副本
        for ext in ("-wal","-shm"):
            src=Path(str(real_db)+ext)
            if src.exists(): shutil.copy(src, str(dbp)+ext)
        stop=threading.Event(); cpu_s=[]
        mt=threading.Thread(target=measure_cpu,args=(stop,cpu_s)); mt.start()
        t0=time.perf_counter()
        with ThreadPoolExecutor(max_workers=nworkers) as ex:
            list(as_completed([ex.submit(run_one,f,dbp) for f in factors]))
        dt=time.perf_counter()-t0
        stop.set(); mt.join()
        avg_cpu=sum(cpu_s)/len(cpu_s) if cpu_s else 0
        return dt, avg_cpu

REAL_DB="data/meta.db"
n=len(factors)
print(f"物理核={os.cpu_count()}, {n} 因子, 真实 meta.db 副本 (45MB)")
for nw in [1,3,4,8]:
    dt,cpu=bench(nw, REAL_DB)
    print(f"  worker={nw}: {dt:6.2f}s  ({dt/n*1000:.0f}ms/因子)  avgCPU={cpu:.0f}%  throughput={n/dt:.2f}/s")
