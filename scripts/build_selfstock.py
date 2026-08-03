#!/usr/bin/env python3
"""构建自选股/关注列表变化追踪。

数据源：真实雪球自选股（fetch_selfstock.py 抓取 -> data/selfstock_raw.json）。
策略：首次运行以当前真实列表为「基线」；之后每次运行对比基线，标注 新增/移除。
同时保留「提及股票」派生（来自原贴+评论的 stockCorrelation），用于说明他实际讨论过哪些。

输出：data/selfstock.json
  {
    source, baseline{date,items}, current{date,items},
    changes{added[],removed[],as_of}, mentioned{...}
  }
"""
import os, sys, json, re
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
RAW = os.path.join(ROOT, "data", "selfstock_raw.json")
OUT = os.path.join(ROOT, "data", "selfstock.json")
POSTS = os.path.join(ROOT, "data", "posts.json")
COMMENTS = os.path.join(ROOT, "data", "comments.json")

TZ = timezone(timedelta(hours=8))

def type_of(it):
    mp = it.get("marketplace")
    cat = it.get("category")
    if mp in ("FUND", "PRIVATE_FUND") or cat == 2:
        return "fund"
    if mp == "CUBE" or cat == 3:
        return "cube"
    return "stock"

def load_raw():
    if not os.path.exists(RAW):
        return None
    d = json.load(open(RAW, encoding="utf-8"))
    items = []
    for it in d.get("items", []):
        items.append({
            "symbol": it.get("symbol"),
            "name": it.get("name"),
            "marketplace": it.get("marketplace"),
            "exchange": it.get("exchange"),
            "watched": it.get("watched"),
            "type": type_of(it),
        })
    return items

def load_prev():
    if not os.path.exists(OUT):
        return None
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return None

def derive_mentioned():
    """从原贴+评论的 stockCorrelation 文本里提取提及的股票（$名称(代码)）。"""
    sym_re = re.compile(r"\$([A-Za-z0-9.\u4e00-\u9fff]+)\(([A-Z]{1,6})\)")
    mentioned = {}
    for path in (POSTS, COMMENTS):
        if not os.path.exists(path):
            continue
        try:
            arr = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for it in arr:
            txt = (it.get("text") or it.get("description") or "")
            for m in sym_re.finditer(txt):
                sym = m.group(2)
                name = m.group(1)
                if sym not in mentioned:
                    mentioned[sym] = {"symbol": sym, "name": name, "mentions": 0}
                mentioned[sym]["mentions"] += 1
    return sorted(mentioned.values(), key=lambda x: -x["mentions"])

def main():
    items = load_raw()
    if not items:
        print("[build_selfstock] 没有 selfstock_raw.json，请先运行 fetch_selfstock.py")
        return
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    prev = load_prev()
    baseline = prev.get("baseline") if prev else None
    if not baseline:
        baseline = {"date": today, "items": items}
        print(f"[build_selfstock] 首次运行：建立基线（{today}，{len(items)} 项）")
    else:
        print(f"[build_selfstock] 已有基线（{baseline.get('date')}，{len(baseline.get('items', []))} 项）；对比当前 {len(items)} 项")

    base_syms = {i["symbol"] for i in baseline.get("items", [])}
    cur_syms = {i["symbol"] for i in items}
    added = [i for i in items if i["symbol"] not in base_syms]
    removed = [i for i in baseline.get("items", []) if i["symbol"] not in cur_syms]

    mentioned = derive_mentioned()
    # 标注哪些自选股被提及过
    men_set = {m["symbol"] for m in mentioned}
    for i in items:
        i["mentioned"] = i["symbol"] in men_set
    for i in baseline.get("items", []):
        i.setdefault("mentioned", i["symbol"] in men_set)

    out = {
        "source": "real_watchlist",
        "baseline": baseline,
        "current": {"date": today, "items": items},
        "changes": {
            "added": sorted(added, key=lambda x: x["symbol"]),
            "removed": sorted(removed, key=lambda x: x["symbol"]),
            "as_of": today,
        },
        "mentioned": mentioned,
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 统计
    by_type = {}
    for i in items:
        by_type[i["type"]] = by_type.get(i["type"], 0) + 1
    print(f"[build_selfstock] 当前 {len(items)} 项（{by_type}）| 新增 {len(added)} | 移除 {len(removed)} | 提及股票 {len(mentioned)}")

if __name__ == "__main__":
    main()
