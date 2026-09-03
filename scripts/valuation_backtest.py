#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「终局估值」口径回测：把资源股的涨跌拆成「商品价格贡献」与「估值重估贡献」。

口径 A（主口径，月频 ~92 月）：相对商品的估值重估
    估值重估收益 = 标的月收益 − 对应商品月收益（组合用持仓权重加权的商品篮子）
    含义：剔除商品价格涨跌本身之后，市场对「每单位商品储量」的股权定价变化。
    这正是终局估值关心的东西——储量寿命、长期成本曲线、折现率、风险溢价。
    再对利率 / 汇率 / 风险偏好 / 中国宏观做 beta 中性化回归。

口径 B（补充口径，季频 ~30 期）：A 股 PB 分解
    用 akshare 季度每股净资产构造 PB，把价格收益拆开：
        r_price ≈ r_PB（估值重估） + r_BPS（净资产增长，即盈利留存累积）
    检验因子驱动的到底是哪一部分。

输出（data/factor_backtest/）
    valuation_repricing.csv   口径 A：估值重估收益对因子的回归
    pb_decomposition.csv      口径 B：PB 分解与因子驱动
    valuation_summary.json    汇总

用法：python scripts/valuation_backtest.py
"""
import os
import json
import warnings

import numpy as np
import pandas as pd
import akshare as ak
from scipy import stats

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "data", "factor_backtest")

# 标的 -> 对应商品篮子（组合按持仓市值权重加权）
RESOURCE = {
    "中海油": {"Brent原油": 1.0},
    "中国宏桥": {"LME铝": 1.0},
    "中国神华": {"CN_焦煤": 1.0},
    "神火股份": {"LME铝": 1.0},
    # 组合权重：中海油 0.478 / 宏桥 0.216 / 神华 0.201 / 神火 0.104
    "组合·资源核心": {"Brent原油": 0.478, "LME铝": 0.320, "CN_焦煤": 0.201},
}

MARKET = ["恒生指数", "标普500"]

# 估值重估的候选驱动（不含商品本身，商品已被剔除）
VAL_FACTORS = [
    "美债10Y", "美元指数", "VIX", "离岸人民币", "纳指",
    "CN_PMI", "CN_PPI同比", "CN_LPR1Y", "CN_社融同比", "CN_用电量同比",
]

# 口径 B：A 股标的 -> akshare 代码
A_SHARES = {"神火股份": "000933", "美的集团": "000333"}


def ols(y, Xcols):
    n = len(y)
    X = np.column_stack([np.ones(n)] + list(Xcols))
    k, dof = X.shape[1], n - X.shape[1]
    if dof <= 0:
        return None
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    sse, sst = float(resid @ resid), float(((y - y.mean()) ** 2).sum())
    r2 = 1 - sse / sst if sst > 0 else 0.0
    cov = (sse / dof) * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    t = np.where(se > 0, beta / se, 0.0)
    p = 2 * (1 - stats.t.cdf(np.abs(t), dof))
    return {"n": n, "k": k, "r2": r2, "t": t, "p": p, "beta": beta}


def stars(p):
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else ""


def part_a(R, F):
    """口径 A：相对商品的估值重估。"""
    rows, summary = [], {}
    for asset, basket in RESOURCE.items():
        if asset not in R.columns:
            continue
        use = {c: w for c, w in basket.items() if c in F.columns}
        if not use:
            continue
        c_ret = sum(F[c] * w for c, w in use.items())
        v = (R[asset] - c_ret).rename(asset)          # 估值重估收益
        mkts = [m for m in MARKET if m in F.columns]
        drivers = []
        for f in VAL_FACTORS:
            if f not in F.columns:
                continue
            sub = pd.concat([v] + [F[m] for m in mkts] + [F[f]], axis=1).dropna()
            if len(sub) < 24:
                continue
            r = ols(sub.iloc[:, 0].values, [sub[m].values for m in mkts] + [sub[f].values])
            if r is None:
                continue
            drivers.append({"factor": f, "n": len(sub), "t": round(float(r["t"][-1]), 2),
                            "p": float(r["p"][-1]), "stars": stars(float(r["p"][-1])),
                            "beta": round(float(r["beta"][-1]), 4),
                            "full_r2": round(float(r["r2"]), 3)})
            rows.append({"asset": asset, "factor": f, "n": len(sub),
                         "t": round(float(r["t"][-1]), 2), "p": round(float(r["p"][-1]), 5),
                         "stars": stars(float(r["p"][-1])), "beta": round(float(r["beta"][-1]), 4),
                         "full_r2": round(float(r["r2"]), 3)})
        drivers.sort(key=lambda d: -abs(d["t"]))
        summary[asset] = {
            "basket": use,
            "significant": [d for d in drivers if d["p"] < 0.05],
            "top": drivers[:5],
        }
    return pd.DataFrame(rows), summary


def part_b(R, F):
    """口径 B：A 股 PB 分解（季度）。"""
    rows, summary = [], {}
    for name, code in A_SHARES.items():
        if name not in R.columns:
            continue
        try:
            df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2019")
        except Exception as e:
            print(f"  [ERR] {name}({code}) 财务指标: {type(e).__name__}: {str(e)[:60]}")
            continue
        dcol = "日期"
        bcol = next((c for c in df.columns if "每股净资产" in str(c)), None)
        if bcol is None:
            print(f"  [SKIP] {name} 无每股净资产列")
            continue
        idx = pd.to_datetime(df[dcol])
        bps = pd.Series(pd.to_numeric(df[bcol], errors="coerce").values, index=idx).dropna().sort_index()
        # 财报披露滞后：报告期 + 3 个月生效，避免前视
        eff = pd.Series(bps.values, index=bps.index + pd.DateOffset(months=3))
        # 季度频率
        rq = (1 + R[name]).resample("QE").prod() - 1          # 季度价格收益
        bps_q = eff.resample("QE").last().ffill()
        r_bps = bps_q.pct_change(fill_method=None)            # 净资产增长
        r_pb = rq - r_bps                                      # 估值重估（近似可加）
        Fq = F.resample("QE").sum(min_count=1)                 # 差分/收益类季度聚合
        mkts = [m for m in MARKET if m in Fq.columns]
        out = {}
        for target, series in [("价格收益", rq), ("估值重估(PB)", r_pb), ("净资产增长", r_bps)]:
            drivers = []
            for f in VAL_FACTORS:
                if f not in Fq.columns:
                    continue
                sub = pd.concat([series] + [Fq[m] for m in mkts] + [Fq[f]], axis=1).dropna()
                if len(sub) < 12:
                    continue
                r = ols(sub.iloc[:, 0].values, [sub[m].values for m in mkts] + [sub[f].values])
                if r is None:
                    continue
                drivers.append({"factor": f, "n": len(sub), "t": round(float(r["t"][-1]), 2),
                                "p": float(r["p"][-1]), "stars": stars(float(r["p"][-1]))})
                rows.append({"asset": name, "target": target, "factor": f, "n": len(sub),
                             "t": round(float(r["t"][-1]), 2), "p": round(float(r["p"][-1]), 5),
                             "stars": stars(float(r["p"][-1]))})
            drivers.sort(key=lambda d: -abs(d["t"]))
            out[target] = {"significant": [d for d in drivers if d["p"] < 0.05],
                           "top": drivers[:4]}
        summary[name] = {"bps_periods": len(bps), "quarterly_n": int(rq.notna().sum()), **out}
        print(f"  {name}: 季报 {len(bps)} 期, 季度样本 {int(rq.notna().sum())}")
    return pd.DataFrame(rows), summary


def main():
    R = pd.read_csv(os.path.join(OUT, "returns_monthly.csv"), index_col=0, parse_dates=True)
    F = pd.read_csv(os.path.join(OUT, "factors_monthly.csv"), index_col=0, parse_dates=True)

    print("=== 口径 A：相对商品的估值重估（月频）===")
    df_a, sum_a = part_a(R, F)
    for a, s in sum_a.items():
        sig = s["significant"]
        txt = ", ".join(f"{d['factor']}{d['stars']}t={d['t']:+.2f}" for d in sig[:4])
        print(f"  {a:<14} {txt if txt else '(无显著驱动)'}")
    df_a.to_csv(os.path.join(OUT, "valuation_repricing.csv"), index=False)

    print("\n=== 口径 B：A 股 PB 分解（季频）===")
    df_b, sum_b = part_b(R, F)
    for a, s in sum_b.items():
        for tgt in ["价格收益", "估值重估(PB)", "净资产增长"]:
            d = s.get(tgt)
            if not d:
                continue
            txt = ", ".join(f"{x['factor']}{x['stars']}t={x['t']:+.2f}" for x in d["significant"][:3])
            print(f"  {a} / {tgt:<12} {txt if txt else '(无显著)'}")
    df_b.to_csv(os.path.join(OUT, "pb_decomposition.csv"), index=False)

    with open(os.path.join(OUT, "valuation_summary.json"), "w", encoding="utf-8") as fp:
        json.dump({"repricing": sum_a, "pb_decomposition": sum_b}, fp, ensure_ascii=False, indent=2)
    print(f"\n输出目录: {OUT}")


if __name__ == "__main__":
    main()
