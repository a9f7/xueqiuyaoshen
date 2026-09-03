#!/usr/bin/env python3
"""回填 ETF 申赎历史数据。

策略：每个交易日**优先用原 skill 脚本**（口径与每日自动化完全一致）；
只有当 skill 因「Changed ETFs cannot be valued」抛错（历史日期上部分 ETF
取不到净值/价格，2024-08 之前必然触发）时，才回退到派生脚本
etf_flow_historical.py（把无法估值的 ETF 净流记为 0 而非抛错）。

这样近期数据保持原口径，历史数据也能生成。

用法：
  python scripts/backfill_etf_history.py [days] [--workers N]
    days    往前回填多少个日历日（默认 45；3 年约 1095）
"""
from __future__ import annotations
import subprocess, sys, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "etf_flow"
OUT.mkdir(parents=True, exist_ok=True)
PYTHON = Path(r"C:/Users/d/.workbuddy/binaries/python/envs/default/Scripts/python.exe")
SKILL = Path(r"C:/Users/d/.workbuddy/skills/china-etf-flow-premarket/scripts/etf_flow_premarket.py")
DERIVED = Path(__file__).resolve().parent / "etf_flow_historical.py"

TIMEOUT = 240


def run(script: Path, ds: str):
    return subprocess.run(
        [str(PYTHON), str(script), "--trade-date", ds, "--output-dir", str(OUT)],
        capture_output=True, text=True, timeout=TIMEOUT,
    )


def fetch_one(ds: str):
    """返回 (状态, 备注)。状态: exist / skill / derived / nontrade / fail"""
    target = OUT / f"etf_flow_{ds}.json"
    if target.exists() and target.stat().st_size > 1000:
        return "exist", None

    r = run(SKILL, ds)
    if r.returncode == 0 and target.exists():
        return "skill", None
    err = (r.stderr or "") + (r.stdout or "")

    if "is not an A-share trading day" in err:
        return "nontrade", None

    # 仅在「无法估值」类错误时回退派生脚本
    if "cannot be valued" in err and DERIVED.exists():
        r2 = run(DERIVED, ds)
        if r2.returncode == 0 and target.exists():
            return "derived", None
        return "fail", ((r2.stderr or r2.stdout or "").strip()[:90])

    if target.exists():
        return "skill", None
    return "fail", err.strip()[:90]


def backfill(days: int, workers: int = 1):
    today = date.today()
    dates = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(days + 1)]
    tally = {"exist": 0, "skill": 0, "derived": 0, "nontrade": 0, "fail": 0}
    fails = []
    derived_days = []
    print(f"=== ETF 申赎回填 {dates[-1]} ~ {dates[0]}（{days+1} 日历日, workers={workers}）===", flush=True)

    def work(ds):
        return ds, fetch_one(ds)

    if workers <= 1:
        for ds in dates:
            st, note = fetch_one(ds)
            tally[st] += 1
            if st == "skill":
                print(f"  ✓ {ds}", flush=True)
            elif st == "derived":
                derived_days.append(ds)
                print(f"  ~ {ds} (派生脚本·降级)", flush=True)
            elif st == "fail":
                fails.append((ds, note))
                print(f"  ✗ {ds}: {note}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, ds) for ds in dates]
            for i, f in enumerate(as_completed(futs), 1):
                ds, (st, note) = f.result()
                tally[st] += 1
                if st == "skill":
                    print(f"  ✓ {ds}", flush=True)
                elif st == "derived":
                    derived_days.append(ds)
                    print(f"  ~ {ds} (派生脚本·降级)", flush=True)
                elif st == "fail":
                    fails.append((ds, note))
                    print(f"  ✗ {ds}: {note}", flush=True)
                if i % 25 == 0:
                    print(f"  ...进度 {i}/{len(dates)}  累计={tally}", flush=True)

    print(f"\n完成：新增(skill) {tally['skill']} | 新增(派生降级) {tally['derived']} "
          f"| 已存在 {tally['exist']} | 非交易日 {tally['nontrade']} | 失败 {tally['fail']}")
    if derived_days:
        derived_days.sort()
        print(f"降级日期范围：{derived_days[0]} ~ {derived_days[-1]}（共 {len(derived_days)} 天）")
    if fails:
        print("失败明细（前10）：")
        for ds, note in fails[:10]:
            print(f"  {ds}: {note}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("days", nargs="?", type=int, default=45, help="往前回填的日历日数")
    ap.add_argument("--workers", type=int, default=1, help="并发数（默认1；数据源可能限流，谨慎调高）")
    a = ap.parse_args()
    backfill(a.days, a.workers)
