#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周度因子监控：拉各因子最新变动，按已回测的敏感度推算对持仓各标的的影响方向。

工作原理
  1. 读取 beta_neutral.csv 中统计显著（p < 0.05）的「因子 -> 标的」敏感度 beta
     （这些 beta 来自月频、控制恒生+标普后的回归，代表该因子的独立驱动强度）
  2. 用 yfinance 拉各因子最近一段时间的变动
  3. 推算影响 = beta × 因子变动，按标的汇总

重要口径说明
  beta 是「月频」估计的，而因子变动是「周度」的，所以推算出的数值只能看
  **方向与相对强弱**，不能直接当作本周收益预测。它的用途是回答：
  「本周哪些外部数据动了，我的哪些仓位会因此承压 / 受益」。

输出
  data/factor_backtest/weekly_monitor.md
  data/factor_backtest/weekly_monitor.json

用法：python scripts/factor_weekly_monitor.py [--weeks 1]
"""
import os
import json
import argparse
import warnings
from datetime import datetime

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "data", "factor_backtest")
BN = os.path.join(OUT, "beta_neutral.csv")

# 因子中文名 -> (yfinance 代码, 变换)   变换: ret=收益率 / diff=水平差分
YF_MAP = {
    "Brent原油": ("BZ=F", "ret"),
    "LME铝": ("ALI=F", "ret"),
    "LME铜": ("HG=F", "ret"),
    "黄金": ("GC=F", "ret"),
    "铀ETF(能源转型代理)": ("URA", "ret"),
    "美债10Y": ("^TNX", "diff"),
    "美元指数": ("DX-Y.NYB", "ret"),
    "离岸人民币": ("CNY=X", "ret"),
    "标普500": ("^GSPC", "ret"),
    "纳指": ("^IXIC", "ret"),
    "恒生指数": ("^HSI", "ret"),
    "VIX": ("^VIX", "diff"),
    "费城半导体": ("^SOX", "ret"),
    "美国生科": ("XBI", "ret"),
    "中概互联网": ("KWEB", "ret"),
}

# 月频发布、周度无新数据的中国宏观因子
CN_MONTHLY = ["CN_PMI", "CN_PPI同比", "CN_用电量同比", "CN_工业增加值同比",
              "CN_固定资产投资同比", "CN_房地产开发", "CN_社融同比", "CN_LPR1Y",
              "CN_焦煤", "CN_螺纹钢"]


def latest_change(sym, kind, weeks):
    """返回 (最新值, N周前值, 变动, 最新日期)。"""
    try:
        h = yf.Ticker(sym).history(period="6mo", interval="1d", auto_adjust=True)
    except Exception as e:
        return None, None, None, f"ERR {type(e).__name__}"
    if h is None or h.empty:
        return None, None, None, "EMPTY"
    s = pd.Series(h["Close"].values, index=h.index)
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    s = s.dropna()
    if len(s) < 2:
        return None, None, None, "SHORT"
    last_v, last_d = float(s.iloc[-1]), s.index[-1].date()
    prev_v = float(s.iloc[-1 - weeks * 5]) if len(s) > weeks * 5 else float(s.iloc[0])
    chg = (last_v / prev_v - 1.0) if kind == "ret" else (last_v - prev_v)
    return last_v, prev_v, chg, str(last_d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=1, help="回看周数，默认 1")
    ap.add_argument("--min-t", type=float, default=3.0,
                    help="只采用 |t| >= 该阈值的强显著敏感度，默认 3.0")
    args = ap.parse_args()

    if not os.path.exists(BN):
        print(f"缺少 {BN}，请先运行 scripts/factor_backtest.py")
        return
    bn = pd.read_csv(BN)
    # 多重比较：数百次检验下 p<0.05 必然产生假阳性，
    # 因此默认只采用 |t| >= 3 的强信号参与推算。
    sig = bn[(bn["p"] < 0.05) & (bn["t"].abs() >= args.min_t) &
             (bn["factor"].isin(YF_MAP))].copy()
    if sig.empty:
        print(f"没有 |t| >= {args.min_t} 的强显著敏感度可供推算")
        return
    print(f"采用 {len(sig)} 条强显著敏感度（|t| >= {args.min_t}）")

    print(f"=== 因子变动（回看 {args.weeks} 周）===")
    changes = {}
    for f in sorted(sig["factor"].unique()):
        sym, kind = YF_MAP[f]
        lv, pv, chg, d = latest_change(sym, kind, args.weeks)
        if chg is None:
            print(f"  {f:<18} {d}")
            continue
        changes[f] = {"change": chg, "last": lv, "prev": pv, "asof": d, "kind": kind}
        unit = "%" if kind == "ret" else "pt"
        print(f"  {f:<18} {chg*100 if kind=='ret' else chg:+8.2f}{unit}  (截至 {d})")

    rows = []
    for _, r in sig.iterrows():
        f = r["factor"]
        if f not in changes:
            continue
        c = changes[f]
        impact = float(r["beta"]) * c["change"]
        rows.append({"asset": r["asset"], "factor": f, "beta": float(r["beta"]),
                     "change": c["change"], "kind": c["kind"], "impact": impact,
                     "asof": c["asof"], "stars": r.get("stars", "")})
    if not rows:
        print("无有效推算")
        return
    D = pd.DataFrame(rows)
    agg = (D.groupby("asset")
             .agg(impact=("impact", "sum"), n=("factor", "size"))
             .sort_values("impact", ascending=False))

    print(f"\n=== 对各仓位的推算影响（方向参考，beta 为月频口径）===")
    for a, r in agg.iterrows():
        sub = D[D["asset"] == a].sort_values("impact", key=abs, ascending=False)
        top = ", ".join(f"{x['factor']}{x['stars']}{x['impact']*100:+.1f}%" for _, x in sub.head(3).iterrows())
        print(f"  {a:<16} 合计{r['impact']*100:+6.2f}%   {top}")

    md = [f"# 持仓因子周度监控", "",
          f"生成时间：{datetime.now():%Y-%m-%d %H:%M}　回看 {args.weeks} 周", "",
          "> beta 来自月频、控制恒生+标普后的回归；因子变动为周度。",
          "> 推算数值**只看方向与相对强弱**，不等同于本周收益预测。", "",
          "## 按仓位的推算影响", "",
          "| 仓位 | 推算影响 | 主要贡献因子 |", "|---|---|---|"]
    for a, r in agg.iterrows():
        sub = D[D["asset"] == a].sort_values("impact", key=abs, ascending=False)
        top = "；".join(f"{x['factor']} {x['impact']*100:+.1f}%" for _, x in sub.head(3).iterrows())
        md.append(f"| {a} | {r['impact']*100:+.2f}% | {top} |")
    md += ["", "## 因子变动明细", "", "| 因子 | 变动 | 类型 | 截至 |", "|---|---|---|---|"]
    for f, c in changes.items():
        v = f"{c['change']*100:+.2f}%" if c["kind"] == "ret" else f"{c['change']:+.2f}pt"
        md.append(f"| {f} | {v} | {c['kind']} | {c['asof']} |")
    md += ["", "## 中国宏观因子", "",
           "以下为月度发布数据，周度监控中不会更新，需运行 `scripts/fetch_cn_macro.py`：", ""]
    md += [f"- {c}" for c in CN_MONTHLY]

    p_md = os.path.join(OUT, "weekly_monitor.md")
    p_js = os.path.join(OUT, "weekly_monitor.json")
    with open(p_md, "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    with open(p_js, "w", encoding="utf-8") as fp:
        json.dump({"generated": datetime.now().isoformat(timespec="seconds"),
                   "weeks": args.weeks,
                   "changes": {k: {kk: (None if pd.isna(vv) else vv) for kk, vv in v.items()}
                               for k, v in changes.items()},
                   "by_asset": {a: {"impact": float(r["impact"]), "n": int(r["n"])}
                                for a, r in agg.iterrows()},
                   "detail": rows}, fp, ensure_ascii=False, indent=2)
    print(f"\n已写出：\n  {p_md}\n  {p_js}")


if __name__ == "__main__":
    main()
