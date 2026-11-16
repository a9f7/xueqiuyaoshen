#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周度因子监控 —— 替代数据源版（Yahoo 403 时的应急通道）。

背景
  Yahoo Finance 自 2026-10-21 起对本环境出口返回 403（yfinance 抛
  YFRateLimitError），原 scripts/factor_weekly_monitor.py 的 12 个因子会全部
  抓取失败、且因 rows 为空直接 return 不写文件（残留旧报告，极易误导）。
  本脚本改用新浪 / akshare 国内源获取同一批因子的近期日线，**复用原脚本
  完全相同的推算逻辑**（beta × 周度变动、按仓位汇总）与输出字段格式，
  以便 scripts/build_dashboard_data.py 正常聚合。

口径差异（务必随报告一并告知，禁止当作原口径结果）
  - 5 个因子为代理：USO→Brent、GLD→黄金、UUP→美元指数、
    沪铜CU0→LME铜、沪铝AL0→LME铝。ETF 含跟踪误差/管理费/汇率，
    内盘期货为人民币计价，与 LME 美元口径不完全同步，幅度仅供方向参考。
  - 2 个因子缺失：VIX、美债10Y（均为 diff 水平差分口径，无等价可用源），
    涉及 5 条强显著敏感度未纳入推算。
  - beta 仍是月频回归口径，周度变动**只看方向与相对强弱**，非收益预测。

用法：python scripts/factor_weekly_monitor_alt.py [--weeks 1]
"""
import os
import json
import argparse
from datetime import datetime

import pandas as pd
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "data", "factor_backtest")
BN = os.path.join(OUT, "beta_neutral.csv")

# 因子中文名 -> (取数方式, 标的代码, 变换, 数据源说明)
#   us_stock : ak.stock_us_daily      us_index : ak.index_us_stock_sina
#   zh_fut   : ak.futures_zh_daily_sina
#   取数方式 None 表示该因子当前无可用源，记为缺失。
SOURCES = {
    "Brent原油":          ("us_stock", "USO",   "ret",  "USO（WTI原油ETF）代理 Brent"),
    "LME铝":              ("zh_fut",   "AL0",   "ret",  "沪铝AL0（内盘）代理 LME铝"),
    "LME铜":              ("zh_fut",   "CU0",   "ret",  "沪铜CU0（内盘）代理 LME铜"),
    "VIX":                (None,       None,    "diff", "无可用源，缺失"),
    "中概互联网":          ("us_stock", "KWEB",  "ret",  "KWEB（原标的）"),
    "纳指":               ("us_index", ".IXIC", "ret",  "纳斯达克综指（原标的）"),
    "美债10Y":            (None,       None,    "diff", "无可用源，缺失"),
    "美元指数":            ("us_stock", "UUP",   "ret",  "UUP（美元指数ETF）代理"),
    "美国生科":            ("us_stock", "XBI",   "ret",  "XBI（原标的）"),
    "费城半导体":          ("us_index", ".SOX",  "ret",  "费城半导体指数（原标的）"),
    "铀ETF(能源转型代理)": ("us_stock", "URA",   "ret",  "URA（原标的）"),
    "黄金":               ("us_stock", "GLD",   "ret",  "GLD（黄金ETF）代理"),
}

CN_MONTHLY = ["CN_PMI", "CN_PPI同比", "CN_用电量同比", "CN_工业增加值同比",
              "CN_固定资产投资同比", "CN_房地产开发", "CN_社融同比", "CN_LPR1Y",
              "CN_焦煤", "CN_螺纹钢"]


def fetch_series(way, sym):
    if way == "us_stock":
        return ak.stock_us_daily(symbol=sym, adjust="")
    if way == "us_index":
        return ak.index_us_stock_sina(symbol=sym)
    if way == "zh_fut":
        return ak.futures_zh_daily_sina(symbol=sym)
    return None


def weekly_change(df, kind, weeks):
    """与原脚本一致：最新收盘 vs weeks*5 个交易日前收盘。"""
    if df is None or len(df) == 0:
        return None
    d = df.sort_values("date")
    c = pd.to_numeric(d["close"], errors="coerce").dropna()
    if len(c) < weeks * 5 + 1:
        return None
    arr = c.values
    last, prev = float(arr[-1]), float(arr[-1 - weeks * 5])
    chg = (last / prev - 1.0) if kind == "ret" else (last - prev)
    try:
        asof = str(pd.to_datetime(d["date"].iloc[-1]).date())
    except Exception:
        asof = str(d["date"].iloc[-1])
    return {"change": chg, "last": last, "prev": prev,
            "asof": asof, "kind": kind}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=1)
    ap.add_argument("--min-t", type=float, default=3.0)
    args = ap.parse_args()

    if not os.path.exists(BN):
        print(f"缺少 {BN}，请先运行 scripts/factor_backtest.py")
        return
    bn = pd.read_csv(BN)
    sig = bn[(bn["p"] < 0.05) & (bn["t"].abs() >= args.min_t) &
             (bn["factor"].isin(SOURCES))].copy()
    print(f"采用 {len(sig)} 条强显著敏感度（|t| >= {args.min_t}）")

    print(f"=== 因子变动（回看 {args.weeks} 周，替代数据源）===")
    changes, missing, failed = {}, [], []
    proxies = {}
    for f in sorted(sig["factor"].unique()):
        way, sym, kind, desc = SOURCES[f]
        if way is None:
            missing.append((f, desc))
            print(f"  {f:<18} MISSING  {desc}")
            continue
        try:
            c = weekly_change(fetch_series(way, sym), kind, args.weeks)
        except Exception as e:
            c = None
            failed.append((f, f"{type(e).__name__}: {e}"))
        if c is None:
            print(f"  {f:<18} FAIL/EMPTY")
            if f not in [x[0] for x in failed]:
                failed.append((f, "无有效数据"))
            continue
        c["note"] = desc
        changes[f] = c
        if "代理" in desc and "原标的" not in desc:
            proxies[f] = desc
        unit = "%" if kind == "ret" else "pt"
        v = c["change"] * 100 if kind == "ret" else c["change"]
        print(f"  {f:<18} {v:+8.2f}{unit}  (截至 {c['asof']}) [{desc}]")

    rows = []
    for _, r in sig.iterrows():
        f = r["factor"]
        if f not in changes:
            continue
        c = changes[f]
        rows.append({"asset": r["asset"], "factor": f,
                     "beta": float(r["beta"]), "change": c["change"],
                     "kind": c["kind"], "impact": float(r["beta"]) * c["change"],
                     "asof": c["asof"], "stars": r.get("stars", "")})
    if not rows:
        print("无有效推算")
        return
    D = pd.DataFrame(rows)
    agg = (D.groupby("asset")
             .agg(impact=("impact", "sum"), n=("factor", "size"))
             .sort_values("impact", ascending=False))

    # 核心驱动完全落在缺失因子上的仓位（原脚本 sig 里只被缺失因子驱动的资产）
    all_assets = set(sig["asset"])
    covered = set(D["asset"])
    uncovered = sorted(all_assets - covered)

    print(f"\n=== 对各仓位的推算影响（方向参考，含代理口径）===")
    for a, r in agg.iterrows():
        sub = D[D["asset"] == a].sort_values("impact", key=abs, ascending=False)
        top = ", ".join(f"{x['factor']}{x['stars']}{x['impact']*100:+.1f}%" for _, x in sub.head(3).iterrows())
        print(f"  {a:<16} 合计{r['impact']*100:+6.2f}%   {top}")

    warn_lines = [
        "> **数据源告警**：Yahoo Finance 持续返回 403，本次改用新浪 / akshare 国内源，",
        f"> {len(proxies)} 个因子为**代理口径**（见下表），与原始期货/指数不完全同步，幅度仅供方向参考。",
        "> VIX、美债10Y 两个 diff 口径因子无可用源，本次**未纳入推算**（涉及 5 条敏感度）。",
        "> beta 为月频回归口径，周度变动**只看方向与相对强弱**，不等于本周收益预测。",
    ]
    md = ["# 持仓因子周度监控（替代数据源）", "",
          f"生成时间：{datetime.now():%Y-%m-%d %H:%M}　回看 {args.weeks} 周", ""]
    md += warn_lines + ["", "## 替代数据源映射", "",
                        "| 因子 | 实际取数 | 口径说明 |", "|---|---|---|"]
    for f in sorted(SOURCES):
        way, sym, kind, desc = SOURCES[f]
        src = "-" if way is None else f"{way} / {sym}"
        md.append(f"| {f} | {src} | {desc} |")
    md += ["", "## 缺失因子与不可完整评估的仓位", "",
           f"- 缺失因子：{('、'.join(x[0] for x in missing)) or '无'}（VIX、美债10Y 为水平差分口径，无等价替代）",
           f"- 因核心驱动缺失而完全无法评估的仓位：{('、'.join(uncovered)) or '无'}",
           f"- 抓取失败因子：{('、'.join(x[0] for x in failed)) or '无'}", "",
           "## 按仓位的推算影响", "",
           "| 仓位 | 推算影响 | 主要贡献因子 |", "|---|---|---|"]
    for a, r in agg.iterrows():
        sub = D[D["asset"] == a].sort_values("impact", key=abs, ascending=False)
        top = "；".join(f"{x['factor']} {x['impact']*100:+.1f}%" for _, x in sub.head(3).iterrows())
        md.append(f"| {a} | {r['impact']*100:+.2f}% | {top} |")
    md += ["", "## 因子变动明细", "", "| 因子 | 变动 | 类型 | 截至 | 数据源 |", "|---|---|---|---|---|"]
    for f, c in changes.items():
        v = f"{c['change']*100:+.2f}%" if c["kind"] == "ret" else f"{c['change']:+.2f}pt"
        md.append(f"| {f} | {v} | {c['kind']} | {c['asof']} | {c['note']} |")
    md += ["", "## 中国宏观因子", "",
           "以下为月度发布数据，周度监控中不会更新，需运行 `scripts/fetch_cn_macro.py`：", ""]
    md += [f"- {c}" for c in CN_MONTHLY]
    md += ["", "---", "",
           f"覆盖率：{len(changes)}/12 个因子可用（其中 {len(proxies)} 个代理口径），"
           f"{len(missing)} 个缺失；{len(agg)}/{len(all_assets)} 个仓位纳入推算。"]

    p_md = os.path.join(OUT, "weekly_monitor.md")
    p_js = os.path.join(OUT, "weekly_monitor.json")
    with open(p_md, "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    with open(p_js, "w", encoding="utf-8") as fp:
        json.dump({"generated": datetime.now().isoformat(timespec="seconds"),
                   "weeks": args.weeks,
                   "data_source": "sina/akshare (Yahoo 403 fallback)",
                   "proxies": proxies,
                   "missing_factors": [x[0] for x in missing],
                   "failed_factors": [x[0] for x in failed],
                   "uncovered_assets": uncovered,
                   "warnings": [
                       "Yahoo Finance 403，改用新浪/akshare 国内源",
                       f"{len(proxies)} 个因子为代理口径，幅度仅供方向参考",
                       "VIX、美债10Y 缺失，涉及 5 条敏感度未纳入",
                       "beta 月频口径，只看方向与相对强弱，非收益预测"],
                   "changes": {k: {kk: (None if pd.isna(vv) else vv) for kk, vv in v.items()}
                               for k, v in changes.items()},
                   "by_asset": {a: {"impact": float(r["impact"]), "n": int(r["n"])}
                                for a, r in agg.iterrows()},
                   "detail": rows}, fp, ensure_ascii=False, indent=2)
    print(f"\n已写出：\n  {p_md}\n  {p_js}")


if __name__ == "__main__":
    main()
