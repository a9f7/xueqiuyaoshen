#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚合「我的看板」所需数据 -> data/dashboard.json（明文，供前端直接 fetch）。

汇总四块内容：
  1. 因子监控   data/factor_backtest/weekly_monitor.json
                （各因子最新变动 + 按已回测敏感度推算的对仓位影响）
  2. 中国宏观   data/cn_macro_raw.csv
                （PMI / PPI / 社融 / 用电量 / 焦煤 / 螺纹钢 等最新值与近 12 月走势）
  3. 驱动关系   data/factor_backtest/beta_neutral.csv（仅取 |t| >= 3 的强信号）
  4. ETF 申赎   data/etf_flow/etf_flow_YYYYMMDD.json（最新一期）

用法：python scripts/build_dashboard_data.py
"""
import os
import re
import json
import glob
import warnings
from datetime import datetime

import pandas as pd

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(ROOT, "data")
FB = os.path.join(DATA, "factor_backtest")
OUT = os.path.join(DATA, "dashboard.json")

MIN_T = 3.0   # 驱动关系只保留强信号，避免多重比较假阳性
SPARK_N = 12  # sparkline 取最近 12 个月

# 宏观指标的展示元信息：单位 + 一句话含义
MACRO_META = {
    "CN_PMI": ("指数", "50 为荣枯线，>50 扩张"),
    "CN_PPI同比": ("%", "工业品出厂价格同比，资源股价格风向"),
    "CN_用电量同比": ("%", "全社会用电量同比，电力煤需求"),
    "CN_工业增加值同比": ("%", "工业生产景气度"),
    "CN_固定资产投资同比": ("%", "投资端需求"),
    "CN_房地产开发": ("%", "地产开发月度涨跌幅"),
    "CN_社融同比": ("%", "信用扩张速度，风格轮动信号"),
    "CN_LPR1Y": ("%", "1 年期 LPR，中国政策利率"),
    "CN_焦煤": ("元/吨", "焦煤期货主力，煤价代理（动力煤已停牌）"),
    "CN_螺纹钢": ("元/吨", "螺纹钢期货主力，内需与基建代理"),
}


def load_factor_monitor():
    p = os.path.join(FB, "weekly_monitor.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p, encoding="utf-8"))
    changes = []
    for name, c in (d.get("changes") or {}).items():
        changes.append({
            "name": name,
            "change": c.get("change"),
            "last": c.get("last"),
            "prev": c.get("prev"),
            "kind": c.get("kind"),
            "asof": c.get("asof"),
        })
    changes.sort(key=lambda x: -abs(x.get("change") or 0))
    impact = []
    for a, v in (d.get("by_asset") or {}).items():
        impact.append({"asset": a, "impact": v.get("impact"), "n": v.get("n")})
    impact.sort(key=lambda x: -(x.get("impact") or 0))
    # 明细：每个仓位由哪些因子贡献
    detail = {}
    for r in (d.get("detail") or []):
        detail.setdefault(r["asset"], []).append({
            "factor": r["factor"], "impact": r["impact"],
            "change": r["change"], "kind": r["kind"], "beta": r["beta"],
        })
    for a in detail:
        detail[a].sort(key=lambda x: -abs(x.get("impact") or 0))
    for it in impact:
        it["drivers"] = detail.get(it["asset"], [])[:4]
    return {"generated": d.get("generated"), "weeks": d.get("weeks"),
            "changes": changes, "by_asset": impact}


def load_macro():
    p = os.path.join(DATA, "cn_macro_raw.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True).sort_index()
    items = []
    for col in df.columns:
        s = df[col].dropna()
        if len(s) < 2:
            continue
        unit, desc = MACRO_META.get(col, ("", ""))
        latest, prev = float(s.iloc[-1]), float(s.iloc[-2])
        tail = s.tail(SPARK_N)
        items.append({
            "name": col, "unit": unit, "desc": desc,
            "latest": latest, "prev": prev,
            "change": latest - prev if unit != "%" else latest - prev,
            "asof": s.index[-1].strftime("%Y-%m"),
            "series": [{"m": ix.strftime("%y-%m"), "v": round(float(vv), 4)}
                       for ix, vv in tail.items()],
        })
    items.sort(key=lambda x: x["name"])
    return {"asof": df.index[-1].strftime("%Y-%m"), "items": items}


def load_drivers():
    p = os.path.join(FB, "beta_neutral.csv")
    if not os.path.exists(p):
        return None
    bn = pd.read_csv(p)
    sig = bn[(bn["p"] < 0.05) & (bn["t"].abs() >= MIN_T)].copy()
    out = []
    for a, g in sig.groupby("asset"):
        ds = g.reindex(g["t"].abs().sort_values(ascending=False).index)
        out.append({"asset": a, "drivers": [
            {"factor": r["factor"], "t": float(r["t"]), "stars": r.get("stars", ""),
             "delta_r2": float(r["delta_r2"]), "n": int(r["n"])}
            for _, r in ds.head(5).iterrows()]})
    out.sort(key=lambda x: -abs(x["drivers"][0]["t"]) if x["drivers"] else 0)
    return {"min_t": MIN_T, "assets": out}


def load_etf():
    files = sorted(glob.glob(os.path.join(DATA, "etf_flow", "etf_flow_*.json")))
    if not files:
        return None
    d = json.load(open(files[-1], encoding="utf-8"))
    td = d.get("trade_date", "")
    def slim(lst, keys):
        return [{k: it.get(k) for k in keys if k in it} for it in (lst or [])]
    keys = ["ts_code", "name", "fund_type", "share_change_wan",
            "share_change_pct", "unit_nav", "estimated_flow_yi"]
    return {
        "trade_date": td,
        "prev_trade_date": d.get("previous_trade_date"),
        "generated_at": d.get("generated_at"),
        "summary": d.get("summary"),
        "quality": d.get("quality"),
        "by_fund_type": d.get("by_fund_type"),
        "top_subscriptions": slim(d.get("top_subscriptions"), keys),
        "top_redemptions": slim(d.get("top_redemptions"), keys),
    }


def main():
    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "factors": load_factor_monitor(),
        "macro": load_macro(),
        "drivers": load_drivers(),
        "etf": load_etf(),
    }
    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(OUT)
    f = out["factors"]
    print(f"因子监控: {'有' if f else '无'}"
          + (f" {len(f['changes'])} 个因子 / {len(f['by_asset'])} 个仓位" if f else ""))
    m = out["macro"]
    print(f"中国宏观: {'有' if m else '无'}" + (f" {len(m['items'])} 个指标 (截至 {m['asof']})" if m else ""))
    dr = out["drivers"]
    print(f"驱动关系: {'有' if dr else '无'}" + (f" {len(dr['assets'])} 个标的 (|t|>={dr['min_t']})" if dr else ""))
    e = out["etf"]
    print(f"ETF 申赎: {'有' if e else '无'}"
          + (f" {e['trade_date']} 净{(e.get('summary') or {}).get('direction')} "
             f"{round((e.get('summary') or {}).get('net_flow_yi') or 0, 2)} 亿元" if e else ""))
    print(f"\n已写出 {OUT}  ({size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
