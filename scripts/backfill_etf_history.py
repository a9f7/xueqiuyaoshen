#!/usr/bin/env python3
"""回填 ETF 申赎历史数据：遍历最近 N 个日历日，已存在跳过，非交易日脚本自动抛错也跳过。"""
from __future__ import annotations
import subprocess, sys
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "etf_flow"
OUT.mkdir(parents=True, exist_ok=True)
PYTHON = Path(r"C:/Users/d/.workbuddy/binaries/python/envs/default/Scripts/python.exe")
SCRIPT = Path(r"C:/Users/d/.workbuddy/skills/china-etf-flow-premarket/scripts/etf_flow_premarket.py")

def backfill(days: int = 45) -> None:
    today = date.today()
    success, skip_exist, skip_nontrade, err = 0, 0, 0, 0
    print(f"=== ETF 申赎回填：{today - timedelta(days=days)} ~ {today}（共 {days} 日历日）===")
    for i in range(days + 1):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        target = OUT / f"etf_flow_{ds}.json"
        if target.exists() and target.stat().st_size > 1000:
            skip_exist += 1
            continue
        r = subprocess.run(
            [str(PYTHON), str(SCRIPT), "--trade-date", ds, "--output-dir", str(OUT)],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode == 0 and target.exists():
            success += 1
            print(f"  ✓ {ds}")
        elif "is not an A-share trading day" in (r.stderr + r.stdout):
            skip_nontrade += 1
        else:
            err += 1
            print(f"  ✗ {ds}: {(r.stderr or r.stdout).strip()[:100]}")
    print(f"\n完成：新增 {success} | 已存在 {skip_exist} | 非交易日 {skip_nontrade} | 失败 {err}")

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    backfill(n)
