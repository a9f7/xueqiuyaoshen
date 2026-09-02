#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取「宏观因子 + 模拟持仓净值」快照，落盘为 data/factor_history.json（数组，供前端看盘页读取）
与 data/factor_history.jsonl（原始追加日志，供助理直接读取）。

数据来源（均为公开免费、无需 cookie，绕开雪球 WAF）：
- Yahoo Finance chart API：布伦特(BZ=F) / WTI(CL=F) / 黄金(GC=F) / 美元指数(DX-Y.NYB) /
  恒生指数(^HSI) / 日经225(^N225)
- FRED CSV：美债10Y(DGS10) / 美债30Y(DGS30) / 联邦基金利率(FEDFUNDS)
- 南向资金净买入：免费源不稳定，先留 None，后续可接付费/自建 feed（见 SOUTHBOUND_URL）

持仓净值取自 data/my_holdings.json（由 fetch_myholdings.py 刷新，本脚本只读不抓）。

用法：
  python scripts/factor_snapshot.py            # 抓一次并追加
  python scripts/factor_snapshot.py --force   # 忽略"距上次不足 N 分钟"的节流
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(WORKSPACE, "data")
HISTORY_JSON = os.path.join(DATA, "factor_history.json")
HISTORY_JSONL = os.path.join(DATA, "factor_history.jsonl")
HOLDINGS = os.path.join(DATA, "my_holdings.json")

# 距上次快照最小间隔（分钟），避免高频自动化空转；--force 可绕过
MIN_INTERVAL_MIN = 20
MAX_RECORDS = 6000  # 环形上限，超出丢弃最旧

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (key, yahoo_symbol, 中文名, 单位)
YAHOO_SERIES = [
    ("brent", "BZ=F", "布伦特原油", "美元/桶"),
    ("wti", "CL=F", "WTI原油", "美元/桶"),
    ("gold", "GC=F", "黄金", "美元/盎司"),
    ("dxy", "DX-Y.NYB", "美元指数", ""),
    ("hsi", "^HSI", "恒生指数", "点"),
    ("n225", "^N225", "日经225", "点"),
    ("us10y", "^TNX", "美债10年", "%"),
    ("us30y", "^TYX", "美债30年", "%"),
]
# 南向资金净买入（亿港元）：免费公开源不稳定，默认 None，留接口
SOUTHBOUND_URL = os.environ.get("SOUTHBOUND_URL")  # 可选：接入后可填


def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _http_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_yahoo(symbol):
    """返回 (price, prev_close) 或 (None, None)。"""
    last_err = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{symbol}?range=1d&interval=1d"
            d = _http_json(url)
            res = (d.get("chart") or {}).get("result")
            if not res:
                continue
            meta = res[0].get("meta") or {}
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            return price, prev
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    return None, last_err


def fetch_southbound():
    if not SOUTHBOUND_URL:
        return None
    try:
        return float(_http_text(SOUTHBOUND_URL).strip())
    except Exception:  # noqa: BLE001
        return None


def load_holdings():
    try:
        d = json.load(open(HOLDINGS, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    s = d.get("summary") or {}
    segs = []
    for seg in d.get("segments") or []:
        if seg.get("mv", 0) <= 0:
            continue
        segs.append({
            "name": seg.get("name"),
            "count": seg.get("count"),
            "mv": seg.get("mv"),
            "day_float": seg.get("day_float"),
            "float": seg.get("float"),
        })
    # 活跃持仓前 12（按市值）
    items = [i for i in (d.get("items") or []) if (i.get("value") or 0) > 0]
    items.sort(key=lambda x: -(x.get("value") or 0))
    top = [{
        "name": i.get("name"),
        "market": i.get("market_name"),
        "value": i.get("value"),
        "weight": i.get("weight"),
        "profit": i.get("profit"),
        "profit_rate": i.get("profit_rate"),
        "day_profit": i.get("day_profit"),
    } for i in items[:12]]
    return {
        "assets": s.get("assets"),
        "principal": s.get("principal"),
        "cash": s.get("cash"),
        "market_value": s.get("market_value"),
        "float_amount": s.get("float_amount"),
        "float_rate": s.get("float_rate"),
        "accum_amount": s.get("accum_amount"),
        "accum_rate": s.get("accum_rate"),
        "day_float_amount": s.get("day_float_amount"),
        "day_float_rate": s.get("day_float_rate"),
        "segments": segs,
        "top": top,
        "active_count": len(items),
        "fetched_at": d.get("fetched_at"),
    }


def collect():
    factors = {}
    meta = {}  # 中文名/单位/涨跌
    for key, sym, zh, unit in YAHOO_SERIES:
        price, prev = fetch_yahoo(sym)
        factors[key] = price
        meta[key] = {"zh": zh, "unit": unit,
                     "prev": prev,
                     "chg": (price - prev) if (price is not None and prev) else None}
    factors["southbound"] = fetch_southbound()
    meta["southbound"] = {"zh": "南向净买入", "unit": "亿港元", "prev": None, "chg": None}
    return factors, meta


def main():
    force = "--force" in sys.argv
    now = int(time.time() * 1000)

    # 节流
    if not force and os.path.exists(HISTORY_JSON):
        try:
            arr = json.load(open(HISTORY_JSON, encoding="utf-8"))
            if arr:
                last_ts = arr[-1].get("ts", 0)
                if (now - last_ts) < MIN_INTERVAL_MIN * 60 * 1000:
                    print(f"[factor] 距上次快照不足 {MIN_INTERVAL_MIN} 分钟，跳过（--force 可强制）。")
                    return
        except Exception:  # noqa: BLE001
            pass

    factors, meta = collect()
    holdings = load_holdings()

    rec = {
        "ts": now,
        "date": time.strftime("%Y-%m-%d", time.localtime(now / 1000)),
        "time": time.strftime("%H:%M", time.localtime(now / 1000)),
        "factors": factors,
        "meta": meta,
        "holdings": holdings,
    }

    # 写 jsonl（追加）
    with open(HISTORY_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 写 json（数组，环形上限）
    arr = []
    if os.path.exists(HISTORY_JSON):
        try:
            arr = json.load(open(HISTORY_JSON, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            arr = []
    arr.append(rec)
    if len(arr) > MAX_RECORDS:
        arr = arr[-MAX_RECORDS:]
    json.dump(arr, open(HISTORY_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 控制台摘要
    print(f"[factor] 快照 {rec['date']} {rec['time']}  记录数={len(arr)}")
    for k, v in factors.items():
        m = meta.get(k, {})
        if v is None:
            print(f"  {m.get('zh', k):10s}: 无数据")
        else:
            chg = m.get("chg")
            chg_s = f" (prev {chg:.2f})" if isinstance(chg, (int, float)) else ""
            print(f"  {m.get('zh', k):10s}: {v:.2f} {m.get('unit','')}{chg_s}")
    if holdings:
        print(f"[factor] 持仓: 总资产 {holdings['assets']:,.0f} | 市值 {holdings['market_value']:,.0f} "
              f"| 浮动 {holdings['float_amount']:,.0f}({holdings['float_rate']*100:.2f}%) "
              f"| 累计 {holdings['accum_amount']:,.0f}({holdings['accum_rate']*100:.2f}%)")
    print(f"[factor] 已写入 {os.path.basename(HISTORY_JSON)} 与 {os.path.basename(HISTORY_JSONL)}")


if __name__ == "__main__":
    main()
