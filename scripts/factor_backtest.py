#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓「基本面 / 终局估值」驱动因子回测（月频，beta 中性化）。

思路
----
终局估值 ≈ f(长期增长率 g, 折现率 r, 长期价格/利润率假设)。
用月频数据检验哪些外部可观测变量真正驱动持仓标的。

关键方法点
  1. 日频拉取 -> 月频重采样（last）：延长样本，并修复部分 ticker 月频缺失。
  2. pairwise 对齐：每个 (标的, 因子) 对用各自的交集样本，避免被短历史标的拖垮样本。
  3. beta 中性化（核心）：单纯相关测的大多是「市场共振」而非基本面。
     因此对每只标的先控制市场 beta（恒生 + 标普），再检验单个因子是否仍有
     独立解释力（增量 R²、t 值、p 值）。这才是「基本面 / 估值驱动」的干净检验。
  4. 领先性 lead-1：因子 t-1 -> 标的收益 t。

输出（data/factor_backtest/）
  returns_monthly.csv        标的月收益率
  factors_monthly.csv        因子月变化
  corr_matrix.csv / pvalue_matrix.csv   同期相关与 p 值
  lead1_corr.csv / lead1_pvalue.csv     领先 1 月相关与 p 值
  beta_neutral.csv           beta 中性化后各因子的增量 R² / t / p
  summary.json               汇总

用法：python scripts/factor_backtest.py
"""
import os
import json
import warnings

import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "data", "factor_backtest")
os.makedirs(OUT, exist_ok=True)

START = "2019-01-01"
MIN_N = 24

# ---------------- 标的：核心持仓 ----------------
HOLDINGS = {
    "0883.HK": "中海油",
    "1378.HK": "中国宏桥",
    "1088.HK": "中国神华",
    "000933.SZ": "神火股份",
    "000333.SZ": "美的集团",
    "0005.HK": "汇丰控股",
    "2268.HK": "药明合联",
    "6990.HK": "科伦博泰",
    "0981.HK": "中芯国际",
    "0939.HK": "建设银行",
    "3153.HK": "南方日经225",
    "MU": "美光科技",
    "PDD": "拼多多",
    "IAU": "黄金ETF",
    "ATAT": "亚朵",
    "HTHT": "华住",
    "VOO": "标普500ETF",
    "QQQ": "纳指ETF",
    "COST": "好市多",
    "KO": "可口可乐",
    "BRK-B": "伯克希尔B",
    "ONC": "百济神州",
    "BITB": "比特币ETF",
}

# ---------------- 因子：(名称, 变换) ----------------
FACTORS = {
    "BZ=F": ("Brent原油", "ret"),
    "ALI=F": ("LME铝", "ret"),
    "HG=F": ("LME铜", "ret"),
    "GC=F": ("黄金", "ret"),
    "URA": ("铀ETF(能源转型代理)", "ret"),
    "^TNX": ("美债10Y", "diff"),
    "DX-Y.NYB": ("美元指数", "ret"),
    "CNY=X": ("离岸人民币", "ret"),
    "^GSPC": ("标普500", "ret"),
    "^IXIC": ("纳指", "ret"),
    "^HSI": ("恒生指数", "ret"),
    "^VIX": ("VIX", "diff"),
    "^SOX": ("费城半导体", "ret"),
    "XBI": ("美国生科", "ret"),
    "KWEB": ("中概互联网", "ret"),
}

# beta 中性化的市场控制变量
MARKET = ["恒生指数", "标普500"]

PORTFOLIOS = {
    "组合·资源核心": {"中海油": 325260, "中国宏桥": 147160, "中国神华": 136620, "神火股份": 70590},
    "组合·港股+A核心": {
        "中海油": 325260, "中国宏桥": 147160, "中国神华": 136620, "神火股份": 70590,
        "美的集团": 60886, "汇丰控股": 65040, "药明合联": 71100,
        "科伦博泰": 49300, "中芯国际": 34125, "建设银行": 19340, "南方日经225": 24624,
    },
}


def fetch_monthly(sym):
    """日频拉取 -> 取每月最后交易日 -> 月频序列（index 为月初）。"""
    try:
        h = yf.Ticker(sym).history(start=START, interval="1d", auto_adjust=True)
    except Exception as e:
        print(f"  [ERR] {sym}: {type(e).__name__}: {str(e)[:60]}")
        return None
    if h is None or h.empty:
        return None
    s = pd.Series(h["Close"].values, index=h.index)
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    m = s.groupby(pd.PeriodIndex(s.index, freq="M")).last()
    m.index = m.index.to_timestamp()
    return m.sort_index()


def stars(p):
    if p < 0.001:
        return "***"
    if p < 0.01:
        return "**"
    if p < 0.05:
        return "*"
    return ""


def ols(y, Xcols):
    """最小二乘，返回 r2 / 各系数 t、p（含截距在 index 0）。"""
    n = len(y)
    X = np.column_stack([np.ones(n)] + list(Xcols))
    k = X.shape[1]
    dof = n - k
    if dof <= 0:
        return None
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    sse = float(resid @ resid)
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - sse / sst if sst > 0 else 0.0
    sigma2 = sse / dof
    cov = sigma2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, beta / se, 0.0)
    p = 2 * (1 - stats.t.cdf(np.abs(t), dof))
    return {"n": n, "r2": r2, "beta": beta, "t": t, "p": p, "k": k}


def main():
    print("=== 拉取标的（日频->月频）===")
    cols = {}
    for sym, name in HOLDINGS.items():
        s = fetch_monthly(sym)
        if s is not None and len(s) >= MIN_N:
            cols[name] = s
            print(f"  {name:<12}({sym:<10}) months={len(s):>3}  {s.index[0].date()} -> {s.index[-1].date()}")
        else:
            print(f"  [SKIP] {name}({sym}) 数据不足({0 if s is None else len(s)})")
    P = pd.DataFrame(cols)
    R = P.pct_change(fill_method=None)

    for pname, wmap in PORTFOLIOS.items():
        avail = {k: v for k, v in wmap.items() if k in R.columns}
        if not avail:
            continue
        tot = sum(avail.values())
        w = pd.Series({k: v / tot for k, v in avail.items()})
        R[pname] = (R[list(avail.keys())] * w).sum(axis=1, min_count=len(avail))

    print("\n=== 拉取因子 ===")
    fraw = {}
    for sym, (name, kind) in FACTORS.items():
        s = fetch_monthly(sym)
        if s is None or len(s) < MIN_N:
            print(f"  [SKIP] {name}({sym}) 数据不足")
            continue
        fraw[name] = (s, kind)
        print(f"  {name:<18}({sym:<10}) months={len(s):>3} transform={kind}")
    F = pd.DataFrame({n: (s.pct_change(fill_method=None) if k == "ret" else s.diff())
                      for n, (s, k) in fraw.items()})

    # 合并中国宏观因子（由 scripts/fetch_cn_macro.py 生成）。
    # 统计类数据（PMI/PPI/社融等）次月才发布，滞后 1 月以避免前视偏差；
    # 期货价格（焦煤/螺纹钢）为实时数据，保持同期。
    cn_path = os.path.join(ROOT, "data", "cn_macro_factors.csv")
    if os.path.exists(cn_path):
        cn = pd.read_csv(cn_path, index_col=0, parse_dates=True)
        LAG_CN = ["CN_PMI", "CN_PPI同比", "CN_用电量同比", "CN_工业增加值同比",
                  "CN_固定资产投资同比", "CN_房地产开发", "CN_社融同比"]
        lagged = [c for c in LAG_CN if c in cn.columns]
        for c in lagged:
            cn[c] = cn[c].shift(1)
        F = pd.concat([F, cn], axis=1)
        F = F[~F.index.duplicated(keep="last")].sort_index()
        print(f"\n合并中国宏观因子 {len(cn.columns)} 个"
              f"（其中 {len(lagged)} 个为次月发布数据，已滞后 1 月）")

    pd.DataFrame({n: s for n, (s, k) in fraw.items()}).to_csv(
        os.path.join(OUT, "factors_raw_monthly.csv"))

    R.to_csv(os.path.join(OUT, "returns_monthly.csv"))
    F.to_csv(os.path.join(OUT, "factors_monthly.csv"))

    assets = list(R.columns)
    facs = list(F.columns)
    print(f"\n标的 {len(assets)} 个 / 因子 {len(facs)} 个")

    # ---------- 1. 同期相关（pairwise） ----------
    corr = pd.DataFrame(index=facs, columns=assets, dtype=float)
    pval = pd.DataFrame(index=facs, columns=assets, dtype=float)
    lead1 = pd.DataFrame(index=facs, columns=assets, dtype=float)
    lead1p = pd.DataFrame(index=facs, columns=assets, dtype=float)
    nsample = pd.DataFrame(index=facs, columns=assets, dtype=float)

    for a in assets:
        for f in facs:
            sub = pd.concat([R[a], F[f]], axis=1).dropna()
            if len(sub) < MIN_N:
                continue
            x, y = sub.iloc[:, 1].values, sub.iloc[:, 0].values
            if np.std(x) == 0 or np.std(y) == 0:
                continue
            rr, pp = stats.pearsonr(x, y)
            corr.loc[f, a], pval.loc[f, a], nsample.loc[f, a] = round(rr, 3), pp, len(sub)
            sub2 = pd.concat([R[a], F[f].shift(1)], axis=1).dropna()
            if len(sub2) >= MIN_N:
                x2, y2 = sub2.iloc[:, 1].values, sub2.iloc[:, 0].values
                if np.std(x2) != 0:
                    r2v, p2v = stats.pearsonr(x2, y2)
                    lead1.loc[f, a], lead1p.loc[f, a] = round(r2v, 3), p2v

    # ---------- 2. beta 中性化：控制市场后单因子增量解释力 ----------
    bn_rows = []
    summary = {"meta": {"start": START, "min_n": MIN_N,
                        "market_control": MARKET}, "assets": {}}

    for a in assets:
        mkts = [m for m in MARKET if m in F.columns]
        # 基准：仅市场
        sub_b = pd.concat([R[a]] + [F[m] for m in mkts], axis=1).dropna()
        base = None
        if len(sub_b) >= MIN_N and mkts:
            base = ols(sub_b.iloc[:, 0].values, [sub_b[m].values for m in mkts])
        drivers = []
        for f in facs:
            if f in mkts:
                continue
            sub = pd.concat([R[a]] + [F[m] for m in mkts] + [F[f]], axis=1).dropna()
            if len(sub) < MIN_N:
                continue
            y = sub.iloc[:, 0].values
            full = ols(y, [sub[m].values for m in mkts] + [sub[f].values])
            if full is None:
                continue
            t_f, p_f, b_f = float(full["t"][-1]), float(full["p"][-1]), float(full["beta"][-1])
            dr2 = (full["r2"] - base["r2"]) if base else full["r2"]
            drivers.append({"factor": f, "n": len(sub), "beta": round(b_f, 4),
                            "t": round(t_f, 2), "p": p_f, "delta_r2": round(float(dr2), 3),
                            "full_r2": round(float(full["r2"]), 3), "stars": stars(p_f)})
            bn_rows.append({"asset": a, "factor": f, "n": len(sub), "beta": round(b_f, 4),
                            "t": round(t_f, 2), "p": round(p_f, 5),
                            "delta_r2": round(float(dr2), 3), "full_r2": round(float(full["r2"]), 3),
                            "stars": stars(p_f)})
        drivers.sort(key=lambda d: -abs(d["t"]))

        raw = [{"factor": f, "corr": float(corr.loc[f, a]), "p": float(pval.loc[f, a]),
                "stars": stars(float(pval.loc[f, a]))}
               for f in facs if not pd.isna(corr.loc[f, a])]
        raw.sort(key=lambda d: -abs(d["corr"]))
        l1 = [{"factor": f, "corr": float(lead1.loc[f, a]), "p": float(lead1p.loc[f, a]),
               "stars": stars(float(lead1p.loc[f, a]))}
              for f in facs if not pd.isna(lead1.loc[f, a])]
        l1.sort(key=lambda d: -abs(d["corr"]))

        summary["assets"][a] = {
            "market_r2": round(float(base["r2"]), 3) if base else None,
            "market_n": int(len(sub_b)) if base else None,
            "raw_top": raw[:5],
            "beta_neutral_significant": [d for d in drivers if d["p"] < 0.05][:6],
            "beta_neutral_top": drivers[:6],
            "lead1_significant": [d for d in l1 if d["p"] < 0.05][:5],
        }

    corr.to_csv(os.path.join(OUT, "corr_matrix.csv"))
    pval.to_csv(os.path.join(OUT, "pvalue_matrix.csv"))
    lead1.to_csv(os.path.join(OUT, "lead1_corr.csv"))
    lead1p.to_csv(os.path.join(OUT, "lead1_pvalue.csv"))
    nsample.to_csv(os.path.join(OUT, "n_samples.csv"))
    pd.DataFrame(bn_rows).to_csv(os.path.join(OUT, "beta_neutral.csv"), index=False)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)

    print("\n=== beta 中性化结果（控制恒生+标普后仍显著的因子）===")
    for a in assets:
        s = summary["assets"][a]
        sig = s["beta_neutral_significant"]
        txt = ", ".join(f"{d['factor']}{d['stars']}t={d['t']:+.2f}ΔR²={d['delta_r2']:+.2f}" for d in sig[:3])
        print(f"  {a:<14} R²mkt={s['market_r2']}  {txt if txt else '(无显著独立因子)'}")
    print(f"\n输出目录: {OUT}")


if __name__ == "__main__":
    main()
