#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情数据层（零依赖：仅用标准库 urllib 直连行情接口）。

主源：东方财富 push2his 日线（国内 IP 友好、无需密钥）。
兜底：新浪财经日线（https://money.finance.sina.com.cn .../CN_MarketData.getKLineData）。
  - 某些受限网络/代理会拦截 push2his（DNS 解析到 198.18.x 黑洞），此时自动切新浪；
  - 新浪覆盖 A 股个股 + 指数（沪深300/创业板等），但不支持港股与板块指数(BK)，
    这些标的在兜底模式下取不到行情 -> 回测优雅跳过。

数据缓存：首次拉取后落盘 data/market/<secid>.json，后续直接读缓存，
避免重复请求触发限流。取不到行情时所有函数返回 None，调用方应优雅降级。

接口说明（东方财富日线）：
  https://push2his.eastmoney.com/api/qt/stock/kline/get
    ?secid=1.600519            # 1=上交所 0=深交所 113=港股
    &fields1=f1,f2,f3
    &fields2=f51,f52,f53,f54,f55,f56,f57,f58   # 日期,开,收,高,低,量,代码,名称
    &klt=101                  # 101=日线
    &fqt=0                    # 0=不复权
    &beg=0&end=20500101
  klines: ["2026-01-02,open,close,high,low,volume,...", ...] 升序
"""
import json
import os
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
CACHE_DIR = DATA / "market"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

# 基准指数（沪深市场）：secid
BENCHMARKS = {
    "沪深300": "1.000300",
    "上证指数": "1.000001",
    "创业板指": "0.399006",
}

# 板块名 -> 东方财富板块指数 secid（90. 为板块前缀；best-effort 子集）。
# 用于 β 剥离里的「板块 α」。取不到时回退到仅剥离大盘。
SECTOR_SECID = {
    "医药": "90.BK1040",
    "科技": "90.BK1036",
    "半导体": "90.BK1036",
    "芯片": "90.BK1036",
    "消费": "90.BK1042",
    "白酒": "90.BK1042",
    "食品饮料": "90.BK1042",
    "新能源": "90.BK1038",
    "光伏": "90.BK1038",
    "锂电": "90.BK1038",
    "金融": "90.BK1049",
    "银行": "90.BK1049",
    "券商": "90.BK1049",
    "地产": "90.BK1052",
    "房地产": "90.BK1052",
    "有色": "90.BK1054",
    "黄金有色": "90.BK1054",
    "煤炭": "90.BK1055",
    "钢铁": "90.BK1055",
    "军工": "90.BK1056",
    "汽车": "90.BK1058",
    "整车": "90.BK1058",
    "传媒游戏": "90.BK1063",
    "游戏": "90.BK1063",
    "通信": "90.BK1059",
    "农业": "90.BK1061",
    "化工": "90.BK1057",
    "电力": "90.BK1060",
}


def xq_to_secid(code):
    """雪球代码 -> 东方财富 secid。非个股（板块 BK / 指数）返回 None。

    code 形如：SH600519 / SZ300308 / HK00700 / BK1462 / SH000997 / 600519
    """
    if not code:
        return None
    code = code.strip().upper()
    if code.startswith("BK"):
        return None  # 板块，非个股
    if code.startswith("SH"):
        num = code[2:]
        if num.startswith("000") or num.startswith("399"):
            return None  # 指数（上证/深证成指系列）
        return "1." + num
    if code.startswith("SZ"):
        num = code[2:]
        if num.startswith("399"):
            return None  # 指数
        return "0." + num
    if code.startswith("HK"):
        return "113." + code[2:]
    # 纯 6 位数字
    if len(code) == 6 and code.isdigit():
        if code.startswith("6"):
            return "1." + code
        if code.startswith(("0", "3")):
            return "0." + code
        if code.startswith(("4", "8")):
            return "0." + code  # 北交所/新三板近似
    return None


def secid_to_xq(secid):
    """反向：secid '1.600519' -> 'SH600519'（用于展示/缓存命名）。"""
    market, num = secid.split(".", 1)
    if market == "1":
        return "SH" + num
    if market == "0":
        return "SZ" + num
    if market == "113":
        return "HK" + num
    return num


def _fetch_kline(secid):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           "&klt=101&fqt=0&beg=0&end=20500101")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", "replace")
        d = json.loads(raw)
    except Exception as e:
        return None, str(e)
    if not isinstance(d, dict) or not d.get("data") or not d["data"].get("klines"):
        return None, "empty"
    rows = []
    for line in d["data"]["klines"]:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            date = p[0]
            o, c, h, l, v = (float(p[1]), float(p[2]), float(p[3]),
                             float(p[4]), float(p[5]))
        except Exception:
            continue
        rows.append([date, o, c, h, l, v])
    if not rows:
        return None, "no rows"
    name = d["data"].get("name", "")
    return {"name": name, "rows": rows}, None


def _secid_to_sina(secid):
    """东方财富 secid '1.600519'/'0.300308'/'1.000300'/'113.00700' -> 新浪 symbol
    'sh600519'/'sz300308'/'sh000300'/'hk00700'。板块(90.BKxxxx)返回 None。"""
    try:
        m, num = secid.split(".", 1)
    except Exception:
        return None
    if m == "1":
        return "sh" + num
    if m == "0":
        return "sz" + num
    if m in ("113", "116"):
        return "hk" + num
    return None


def _fetch_kline_sina(secid):
    """新浪财经日线兜底（零依赖）。个股/指数可用；港股与板块指数返回 None。
    返回 {'name', 'rows':[[date,o,c,h,l,v]...]} 升序，或 (None, err)。
    """
    sym = _secid_to_sina(secid)
    if not sym:
        return None, "no sina symbol (sector/hk)"
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData"
           f"?symbol={sym}&scale=240&ma=no&datalen=2000")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://finance.sina.com.cn/",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", "replace")
        arr = json.loads(raw)
    except Exception as e:
        return None, f"sina:{e}"
    if not isinstance(arr, list) or not arr:
        return None, "sina:empty/null"
    rows = []
    for it in arr:
        try:
            date = it["day"]
            o = float(it["open"]); c = float(it["close"])
            h = float(it["high"]); l = float(it["low"])
            v = float(it.get("volume") or 0)
        except Exception:
            continue
        rows.append([date, o, c, h, l, v])
    if not rows:
        return None, "sina:no rows"
    return {"name": sym, "rows": rows}, None


def _fetch_multi(secid, sources):
    last_err = "no sources"
    for src in sources:
        if src == "eastmoney":
            data, err = _fetch_kline(secid)
        elif src == "sina":
            data, err = _fetch_kline_sina(secid)
        else:
            continue
        if data is not None:
            return data, None
        last_err = err
    return None, last_err


def get_kline(secid, use_cache=True, max_age_days=30, sources=("eastmoney", "sina")):
    """返回 {'name', 'rows':[[date,o,c,h,l,v]...]} 或 None。

    默认先试东方财富(push2his)，失败自动回退新浪财经日线（本环境 push2his 常被代理拦截）。
    use_cache=False 强制刷新。缓存超过 max_age_days 天也刷新（保证回测用近期价）。
    """
    cf = CACHE_DIR / f"{secid.replace('.', '_')}.json"
    if use_cache and cf.exists():
        try:
            age = (time.time() - cf.stat().st_mtime) / 86400
            if age <= max_age_days:
                return json.load(open(cf, encoding="utf-8"))
        except Exception:
            pass
    data, err = _fetch_multi(secid, sources)
    if data is None:
        return None
    try:
        json.dump(data, open(cf, "w", encoding="utf-8"))
    except Exception:
        pass
    return data


def get_close_series(secid):
    """返回 [(date_str, close)] 升序，或 None。"""
    d = get_kline(secid)
    if not d:
        return None
    return [(r[0], r[2]) for r in d["rows"]]


def benchmark_secids():
    return BENCHMARKS


if __name__ == "__main__":
    for s in ("1.600519", "1.000300", "0.300308"):
        d = get_kline(s, use_cache=False)
        print(s, "=>", (d["name"], len(d["rows"])) if d else "FAIL")
