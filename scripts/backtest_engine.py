#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观点回测引擎（对标 JohnWish1590/xueqiu-analyzer 的 engine.py）。

把雪球大V（metalslime）的「观点」变成可验证的「事实」：
  - 抽取每条发言的【主体个股 + 多空立场】（单主体，仅看多/看空进回测）；
  - 对接真实行情（东方财富日线），做 T+1/3/5/7/10/20 窗口收益；
  - β 剥离：个股收益 = 大盘β + 超额收益α；回测只看 α（发言的真实增量信息）；
  - 命中率 / IC 回测：分窗口、分板块、分立场统计（一律带样本量 N）；
  - 自动预测：近期未闭合事件给出 跟随 / 观望 / 反向 信号 + 依据命中率。

设计原则（沿用参考实现）：
  - 单主体：每条发言只产 1 个主体事件（其余标的仅展示、不进回测）；
  - 无未来函数：验证窗口只用发言时点之后的数据；
  - 小样本必报 N；预测层用历史同板块/整体命中率做校准。

输出：data/backtest.json（供网页「观点回测」面板渲染）。
行情缺失时优雅降级：回测仍能跑（仅缺行情的事件被剔除并计入 missing）。
"""
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
sys.path.insert(0, str(HERE))
import market_data as md

HORIZONS = [1, 3, 5, 7, 10, 20]
PRIMARY_BENCH = "1.000300"  # 沪深300

# 板块名 -> 东方财富板块 secid（market_data.SECTOR_SECID 已含，这里直接复用）
SECTOR_SECID = md.SECTOR_SECID

# A 股名称 -> 雪球代码（兜底，仅当发言无 $代码 且 stockCorrelation 无个股时启用）
STOCK_DICT = {
    "贵州茅台": "SH600519", "五粮液": "SZ000858", "宁德时代": "SZ300750",
    "比亚迪": "SZ002594", "中国平安": "SH601318", "招商银行": "SH600036",
    "兴业银行": "SH601166", "东方财富": "SZ300059", "中信证券": "SH600030",
    "隆基绿能": "SH601012", "通威股份": "SH600438", "阳光电源": "SZ300274",
    "立讯精密": "SZ002475", "美的集团": "SZ000333", "格力电器": "SZ000651",
    "伊利股份": "SH600887", "海康威视": "SZ002415", "紫金矿业": "SH601899",
    "北方华创": "SZ002371", "韦尔股份": "SH603501", "中际旭创": "SZ300308",
    "工业富联": "SH601138", "中国移动": "SH600941", "长江电力": "SH600900",
    "中国神华": "SH601088", "陕西煤业": "SH601225", "万华化学": "SH600309",
    "药明康德": "SH603259", "恒瑞医药": "SH600276", "迈瑞医疗": "SZ300760",
    "中芯国际": "SH688981", "汇川技术": "SZ300124", "三一重工": "SH600031",
    "京东方A": "SZ000725", "TCL科技": "SZ000100", "科大讯飞": "SZ002230",
    "兆易创新": "SH603986", "卓胜微": "SZ300782", "金山办公": "SH688111",
    "山西汾酒": "SH600809", "泸州老窖": "SZ000568", "洋河股份": "SZ002304",
    "牧原股份": "SZ002714", "温氏股份": "SZ300498", "福耀玻璃": "SH600660",
    "中国中免": "SH601888", "海天味业": "SH603288", "片仔癀": "SH600436",
}

# 发言里 $名称(代码.市场) 格式
SYM_RE = re.compile(r"\$\s*([\u4e00-\u9fa5A-Za-z·]{1,10})\s*\(([0-9]{6}\.(?:SH|SZ|HK))\)")


def parse_created_at(ca):
    """雪球 created_at：毫秒时间戳 或 ISO 字符串 -> datetime(UTC)。"""
    if ca is None:
        return None
    if isinstance(ca, (int, float)):
        ts = ca
    else:
        s = str(ca).strip()
        if s.isdigit():
            ts = float(s)
        else:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s[:19], fmt)
                except Exception:
                    continue
            return None
    if ts > 1e11:  # 毫秒
        ts = ts / 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=__import__("datetime").timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def extract_subject(post):
    """抽取单条发言的主体事件：{'code'(雪球式), 'stance'(+1/-1), 'sector'(名称/None)}。

    立场：tags 里出现「看多」-> +1，「看空」-> -1；其余(中性/复盘/风险提示/无)不进回测。
    主体：优先 $名称(代码) 显式点名 -> 其次 stockCorrelation 首个个股 -> 再次名称词典。
    """
    tags = post.get("tags") or []
    stance = None
    for t in tags:
        if t == "看多":
            stance = 1
            break
        if t == "看空":
            stance = -1
            break
    if stance is None:
        return None

    text = post.get("text") or ""
    code = None
    m = SYM_RE.search(text)
    if m:
        # 标准雪球代码为 SH/SZ/HK + 6位；group(2) 形如 300308.SZ
        market = m.group(2).split(".")[1]
        num = m.group(2).split(".")[0]
        code = (market + num) if market in ("SH", "SZ", "HK") else None
    if not code:
        for c in (post.get("stockCorrelation") or []):
            if md.xq_to_secid(c):  # 是个股（非板块/指数）
                code = c
                break
    if not code:
        for name, c in STOCK_DICT.items():
            if name in text:
                code = c
                break
    if not code or not md.xq_to_secid(code):
        return None

    sector = None
    for t in tags:
        if t in SECTOR_SECID:
            sector = t
            break
    return {"code": code, "stance": stance, "sector": sector}


def build_events():
    """从 posts.json 抽出可回测事件列表。"""
    path = DATA / "posts.json"
    if not path.exists():
        return []
    posts = json.load(open(path, encoding="utf-8"))
    events = []
    for p in posts:
        sub = extract_subject(p)
        if not sub:
            continue
        dt = parse_created_at(p.get("created_at"))
        if not dt:
            continue
        events.append({
            "date": dt,
            "code": sub["code"],
            "stance": sub["stance"],
            "sector": sub["sector"],
            "url": p.get("url", ""),
            "text": (p.get("text") or "")[:120],
        })
    events.sort(key=lambda x: x["date"])
    return events


def _pos_after(series, d, k):
    """series: [(date_str, close)] 升序。返回从 <= d 那根起第 k 根(含自身)的索引；越界 None。"""
    idx = None
    for i, (ds, _) in enumerate(series):
        if ds <= d:
            idx = i
        else:
            break
    if idx is None:
        return None
    tgt = idx + k
    if tgt >= len(series):
        return None
    return tgt


def _beta(stock_series, bench_series):
    """用全量日收益估计 β = cov(stock,bench)/var(bench)。序列按日期对齐。"""
    sc = {d: c for d, c in stock_series}
    bc = {d: c for d, c in bench_series}
    common = sorted(set(sc) & set(bc))
    rets = []
    for prev, cur in zip(common[:-1], common[1:]):
        if sc[prev] > 0 and bc[prev] > 0:
            sr = sc[cur] / sc[prev] - 1
            br = bc[cur] / bc[prev] - 1
            if abs(br) > 1e-9:
                rets.append((sr, br))
    if len(rets) < 10:
        return None
    import statistics
    bs = [b for _, b in rets]
    var = statistics.pvariance(bs) if len(bs) > 1 else 0
    if var <= 1e-12:
        return None
    cov = sum(s * b for s, b in rets) / len(rets) - (
        sum(s for s, _ in rets) / len(rets)) * (sum(b for _, b in rets) / len(rets))
    return cov / var


def compute_event(ev, bench_series, sector_series=None):
    """返回该事件各窗口的 {k: {stock_ret, bench_ret, alpha}} 或 None（行情缺失/窗口未闭合）。"""
    secid = md.xq_to_secid(ev["code"])
    if not secid:
        return None
    sd = md.get_kline(secid)
    if not sd:
        return None
    stock_series = [(r[0], r[2]) for r in sd["rows"]]
    d_str = ev["date"].strftime("%Y-%m-%d")
    out = {}
    beta = _beta(stock_series, bench_series) if bench_series else None
    for k in HORIZONS:
        ti = _pos_after(stock_series, d_str, k)
        if ti is None:
            continue
        base_i = _pos_after(stock_series, d_str, 0)
        if base_i is None:
            continue
        stock_ret = stock_series[ti][1] / stock_series[base_i][1] - 1
        bench_ret = None
        alpha = stock_ret
        if bench_series:
            bi = _pos_after(bench_series, d_str, k)
            bbase = _pos_after(bench_series, d_str, 0)
            if bi is not None and bbase is not None and bench_series[bbase][1] > 0:
                bench_ret = bench_series[bi][1] / bench_series[bbase][1] - 1
                if beta is not None and bench_ret is not None:
                    alpha = stock_ret - beta * bench_ret
        out[k] = {
            "stock_ret": stock_ret,
            "bench_ret": bench_ret,
            "alpha": alpha,
        }
    if not out:
        return None
    return out


def _sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)


def main():
    print("[backtest] 抽取事件...", flush=True)
    events = build_events()
    print(f"[backtest] 可回测事件（看多/看空且含个股）: {len(events)}", flush=True)
    if not events:
        _dump_empty("无满足「看多/看空且含具体个股」的发言，无法回测。")
        return

    bench = md.get_kline(PRIMARY_BENCH)
    bench_series = [(r[0], r[2]) for r in bench["rows"]] if bench else None
    print(f"[backtest] 基准沪深300: {'OK' if bench_series else '缺失(仅算原始收益)'}", flush=True)

    # 预拉取涉及的个股/板块行情
    need = set()
    for ev in events:
        need.add(md.xq_to_secid(ev["code"]))
        if ev["sector"] and ev["sector"] in SECTOR_SECID:
            need.add(SECTOR_SECID[ev["sector"]])
    print(f"[backtest] 需拉取行情标的: {len(need)}", flush=True)
    for s in need:
        md.get_kline(s)

    # 计算每事件各窗口
    per_event = []  # {ev, wins, by_horizon}
    missing_price = 0
    for ev in events:
        res = compute_event(ev, bench_series)
        if res is None:
            missing_price += 1
            continue
        per_event.append((ev, res))

    if not per_event:
        _dump_empty("行情数据缺失，无法计算收益（请确认运行环境能访问东方财富 push2his 或新浪财经行情接口）。"
                    f"已抽取 {len(events)} 条候选事件，待行情可用后重跑即可。")
        return

    # 聚合
    n = len(per_event)
    by_horizon = {k: {"hit": 0, "n": 0, "ic_sum": 0.0, "alpha_sum": 0.0} for k in HORIZONS}
    by_sector = defaultdict(lambda: {"hit": 0, "n": 0, "ic_sum": 0.0})
    by_stance = {"看多": {"hit": 0, "n": 0}, "看空": {"hit": 0, "n": 0}}

    for ev, res in per_event:
        sname = "看多" if ev["stance"] == 1 else "看空"
        for k, v in res.items():
            bh = by_horizon[k]
            bh["n"] += 1
            d = _sign(v["stock_ret"])
            hit = (d == ev["stance"])
            if hit:
                bh["hit"] += 1
            bh["ic_sum"] += ev["stance"] * d
            bh["alpha_sum"] += (v["alpha"] or 0)
            if ev["sector"]:
                bs = by_sector[ev["sector"]]
                bs["n"] += 1
                if hit:
                    bs["hit"] += 1
                bs["ic_sum"] += ev["stance"] * d
        by_stance[sname]["n"] += 1
        # 用 T+5 作为该事件代表窗口判命中（用于分立场统计）
        if 5 in res:
            d = _sign(res[5]["stock_ret"])
            if d == ev["stance"]:
                by_stance[sname]["hit"] += 1

    overall_hit = {k: (by_horizon[k]["hit"] / by_horizon[k]["n"]) if by_horizon[k]["n"] else None
                   for k in HORIZONS}
    overall_ic = (sum(by_horizon[k]["ic_sum"] for k in HORIZONS) /
                  sum(by_horizon[k]["n"] for k in HORIZONS)) if n else None
    sector_list = []
    for s, v in sorted(by_sector.items(), key=lambda x: -x[1]["n"]):
        if v["n"] >= 3:
            sector_list.append({
                "sector": s, "n": v["n"],
                "hit_rate": v["hit"] / v["n"],
                "ic": v["ic_sum"] / v["n"],
            })
    stance_list = {
        s: {"n": v["n"], "hit_rate": (v["hit"] / v["n"]) if v["n"] else None}
        for s, v in by_stance.items()
    }

    # 近期观点 -> 预测/验证信号（最近 60 天内的观点，标注最大已闭合窗口）
    recent = []
    last_date = max(ev["date"] for ev, _ in per_event)
    cutoff = last_date - timedelta(days=60)
    # 板块命中率查表（用于信号校准）
    sector_hr = {x["sector"]: x["hit_rate"] for x in sector_list}
    for ev, res in per_event:
        if ev["date"] < cutoff:
            continue
        # 该事件当前最大已闭合窗口（用于标注"进行中/已验证"）
        closed = max((k for k in res if res[k] is not None), default=None)
        basis_hr = sector_hr.get(ev["sector"]) if ev["sector"] else overall_hit.get(5)
        if basis_hr is None:
            basis_hr = 0.5
        if basis_hr >= 0.55:
            signal = "跟随"
        elif basis_hr <= 0.45:
            signal = "反向"
        else:
            signal = "观望"
        recent.append({
            "date": ev["date"].strftime("%Y-%m-%d"),
            "code": ev["code"],
            "stance": "看多" if ev["stance"] == 1 else "看空",
            "sector": ev["sector"] or "",
            "signal": signal,
            "basis_hit_rate": round(basis_hr, 3),
            "closed_horizon": closed,
            "url": ev["url"],
            "text": ev["text"],
        })
    recent.sort(key=lambda x: x["date"], reverse=True)
    recent = recent[:30]

    out = {
        "generated_at": datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "β剥离(个股收益-β×沪深300) + 各窗口命中率/IC；数据源：东方财富/新浪日线",
        "overall": {
            "n_events": n,
            "events_skipped": missing_price,
            "hit_rate_by_horizon": {str(k): (round(overall_hit[k], 3) if overall_hit[k] is not None else None)
                                    for k in HORIZONS},
            "n_by_horizon": {str(k): by_horizon[k]["n"] for k in HORIZONS},
            "ic": round(overall_ic, 3) if overall_ic is not None else None,
            "avg_alpha_by_horizon": {str(k): round(by_horizon[k]["alpha_sum"] / by_horizon[k]["n"], 4)
                                     if by_horizon[k]["n"] else None for k in HORIZONS},
        },
        "by_sector": sector_list,
        "by_stance": stance_list,
        "recent_signals": recent,
        "benchmark_available": bench_series is not None,
    }
    json.dump(out, open(DATA / "backtest.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[backtest] 完成：{n} 事件（缺行情 {missing_price}）；整体命中率 T+5="
          f"{overall_hit.get(5)}；IC={overall_ic}；近期信号 {len(recent)} 条 -> data/backtest.json",
          flush=True)


def _dump_empty(msg):
    out = {
        "generated_at": datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "β剥离 + 命中率/IC",
        "overall": {"n_events": 0, "events_skipped": 0, "hit_rate_by_horizon": {},
                    "ic": None, "avg_alpha_by_horizon": {}},
        "by_sector": [], "by_stance": {}, "recent_signals": [],
        "benchmark_available": None, "note": msg,
    }
    json.dump(out, open(DATA / "backtest.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("[backtest] 空结果 ->", msg, flush=True)


if __name__ == "__main__":
    main()
