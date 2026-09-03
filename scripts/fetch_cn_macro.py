#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取中国宏观 + 动力煤数据（akshare），输出统一月频因子。

数据源（均为 2026-09 实测通过）：
  macro_china_pmi                 制造业 PMI（水平，50 为荣枯线）
  macro_china_ppi                 PPI 当月同比
  macro_china_society_electricity 全社会用电量同比（神华的电力煤需求）
  macro_china_gyzjz               工业增加值同比
  macro_china_gdzctz              固定资产投资同比
  macro_china_real_estate         房地产开发（月度涨跌幅，美的的地产后周期）
  macro_china_shrzgm              社会融资规模增量（当月值，本脚本转同比）
  macro_china_lpr                 LPR 1Y（日频，转月末；中国折现率）
  futures_zh_daily_sina(JM0)      焦煤期货（日频转月末；动力煤 ZC 已于 2022-12 停牌）
  futures_zh_daily_sina(RB0)      螺纹钢期货（中国内需 / 基建地产需求代理）

输出：
  data/cn_macro_raw.csv      月频原始水平值
  data/cn_macro_factors.csv  月频因子（已做差分 / 收益率 / 同比变换）

用法：python scripts/fetch_cn_macro.py
"""
import os
import re
import warnings

import numpy as np
import pandas as pd
import akshare as ak

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
RAW_OUT = os.path.join(ROOT, "data", "cn_macro_raw.csv")
FAC_OUT = os.path.join(ROOT, "data", "cn_macro_factors.csv")


def to_month(s):
    """把 akshare 各种日期写法统一解析为月初 Timestamp。"""
    t = str(s).strip()
    m = re.match(r"^(\d{4})年(\d{1,2})月", t)      # 2026年08月份
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{4})\.(\d{1,2})$", t)        # 2003.12
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{4})(\d{2})$", t)            # 201501
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    return pd.to_datetime(t, errors="coerce")


def num(df, col):
    return pd.to_numeric(
        df[col].astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False),
        errors="coerce",
    )


def _series(df, dcol, vcol, datefn=to_month):
    idx = df[dcol].map(datefn)
    s = pd.Series(num(df, vcol).values, index=idx).dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def s_pmi():
    return _series(ak.macro_china_pmi(), "月份", "制造业-指数")


def s_ppi():
    return _series(ak.macro_china_ppi(), "月份", "当月同比增长")


def s_elec():
    return _series(ak.macro_china_society_electricity(), "统计时间", "全社会用电量同比")


def s_gyzjz():
    return _series(ak.macro_china_gyzjz(), "月份", "同比增长")


def s_gdzctz():
    return _series(ak.macro_china_gdzctz(), "月份", "同比增长")


def s_realestate():
    return _series(ak.macro_china_real_estate(), "日期", "涨跌幅")


def s_shrzgm():
    """社融当月增量 -> 同比增速（当月值季节性极强，必须用同比）。"""
    s = _series(ak.macro_china_shrzgm(), "月份", "社会融资规模增量")
    return (s / s.shift(12) - 1.0).dropna()


def s_lpr():
    df = ak.macro_china_lpr()
    idx = pd.to_datetime(df["TRADE_DATE"]).dt.to_period("M").dt.to_timestamp()
    s = pd.Series(pd.to_numeric(df["LPR1Y"], errors="coerce").values, index=idx).dropna()
    return s.groupby(level=0).last().sort_index()


def s_coal():
    """煤价代理：动力煤期货 ZC 已于 2022-12 停牌，改用焦煤 JM 主力连续（覆盖至最新）。"""
    df = ak.futures_zh_daily_sina(symbol="JM0")
    idx = pd.to_datetime(df["date"]).dt.to_period("M").dt.to_timestamp()
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values, index=idx).dropna()
    return s.groupby(level=0).last().sort_index()


def s_rebar():
    """螺纹钢期货 RB 主力连续：中国内需 / 基建地产需求的实时价格代理。"""
    df = ak.futures_zh_daily_sina(symbol="RB0")
    idx = pd.to_datetime(df["date"]).dt.to_period("M").dt.to_timestamp()
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values, index=idx).dropna()
    return s.groupby(level=0).last().sort_index()


# (名称, 取值函数, 变换)  diff=水平差分  ret=收益率  asis=数据本身已是变化率
SPEC = [
    ("CN_焦煤", s_coal, "ret"),
    ("CN_螺纹钢", s_rebar, "ret"),
    ("CN_PMI", s_pmi, "diff"),
    ("CN_PPI同比", s_ppi, "diff"),
    ("CN_用电量同比", s_elec, "diff"),
    ("CN_工业增加值同比", s_gyzjz, "diff"),
    ("CN_固定资产投资同比", s_gdzctz, "diff"),
    ("CN_房地产开发", s_realestate, "asis"),
    ("CN_社融同比", s_shrzgm, "diff"),
    ("CN_LPR1Y", s_lpr, "diff"),
]


def main():
    raw, fac = {}, {}
    for name, fn, kind in SPEC:
        try:
            s = fn()
        except Exception as e:
            print(f"  [ERR] {name}: {type(e).__name__}: {str(e)[:60]}")
            continue
        if s is None or len(s) < 12:
            print(f"  [SKIP] {name} 数据不足({0 if s is None else len(s)})")
            continue
        raw[name] = s
        fac[name] = s.pct_change(fill_method=None) if kind == "ret" else (
            s.diff() if kind == "diff" else s)
        print(f"  {name:<22} months={len(s):>4}  {s.index[0].date()} -> {s.index[-1].date()}  [{kind}]")

    R = pd.DataFrame(raw).sort_index()
    F = pd.DataFrame(fac).sort_index()
    R.to_csv(RAW_OUT)
    F.to_csv(FAC_OUT)
    print(f"\n原始水平值 -> {RAW_OUT}  ({R.shape[0]} 月)")
    print(f"变换后因子 -> {FAC_OUT}  ({F.shape[0]} 月)")
    print("\n最近 6 个月因子值：")
    print(F.tail(6).round(4).to_string())


if __name__ == "__main__":
    main()
