#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建「自选股/关注股票」变化追踪（派生）。

由于雪球真实自选股列表为私有、无法用第三方 cookie 抓取，这里用 metalslime
在所有【原贴 + 评论 + 转发】中提到的个股（$股票名(代码)）作为「关注股票」代理，
按月份追踪每只股票的：
  - 首次出现(first_month) / 最近出现(last_month)
  - 出现过的月份列表
  - 状态标注：新关注 / 重提 / 已冷落 / 持续关注
并输出变化集合（new / revived / cold / core）供前端标注。

输出：data/selfstock.json
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "scripts"))
from normalize_interactions import extract_symbols  # noqa: E402

COLD_MONTHS = 6       # 超过 N 个月未提及 → 已冷落
NEW_MONTHS = 2         # 首次出现在最近 N 个月内 → 新关注
REVIVE_GAP = 3         # 间隔 >= N 个月重新出现 → 重提


def month_of(ms):
    if not ms:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None
    return dt.strftime("%Y-%m")


def load_all_texts():
    """返回 [(created_at_ms, text), ...] 来自 posts + comments + reposts。"""
    items = []
    for fn in ["posts.json", "comments.json", "reposts.json"]:
        fp = DATA / fn
        if not fp.exists():
            continue
        for p in json.load(open(fp, encoding="utf-8")):
            txt = p.get("text") or ""
            # 也扫原文上下文（被评论原帖里提到的股票）
            orig = p.get("original") or {}
            if orig.get("text"):
                txt = txt + " " + orig["text"]
            items.append((p.get("created_at") or 0, txt))
    return items


def main():
    items = load_all_texts()
    # code -> {name, months:set, count, first, last}
    stocks = defaultdict(lambda: {"name": "", "months": set(), "count": 0, "first": None, "last": None})
    for ms, txt in items:
        m = month_of(ms)
        if not m:
            continue
        for sym in extract_symbols(txt):
            code = sym["code"]
            rec = stocks[code]
            rec["name"] = sym["name"] or rec["name"]
            rec["months"].add(m)
            rec["count"] += 1
            if rec["first"] is None or m < rec["first"]:
                rec["first"] = m
            if rec["last"] is None or m > rec["last"]:
                rec["last"] = m

    now = datetime.now(timezone.utc)
    latest_month = max((rec["last"] for rec in stocks.values()), default=None)
    # 计算最新月份与某月份的间隔（以月末近似）
    def month_diff(a, b):
        ya, ma = map(int, a.split("-"))
        yb, mb = map(int, b.split("-"))
        return (ya - yb) * 12 + (ma - mb)

    out = []
    new_list, revived_list, cold_list, core_list = [], [], [], []
    for code, rec in stocks.items():
        months = sorted(rec["months"])
        gap = 0
        for i in range(1, len(months)):
            gap = max(gap, month_diff(months[i], months[i - 1]))
        months_since_last = month_diff(latest_month, rec["last"]) if latest_month else 999
        months_since_first = month_diff(latest_month, rec["first"]) if latest_month else 999

        if months_since_first <= NEW_MONTHS:
            status = "新关注"
            new_list.append(code)
        elif gap >= REVIVE_GAP and months_since_last <= NEW_MONTHS:
            status = "重提"
            revived_list.append(code)
        elif months_since_last > COLD_MONTHS:
            status = "已冷落"
            cold_list.append(code)
        else:
            status = "持续关注"
            core_list.append(code)

        out.append({
            "code": code,
            "name": rec["name"],
            "first_month": rec["first"],
            "last_month": rec["last"],
            "months_count": len(months),
            "mention_count": rec["count"],
            "months": months,
            "max_gap": gap,
            "months_since_last": months_since_last,
            "status": status,
        })

    # 排序：最近出现优先，其次提及次数
    out.sort(key=lambda x: (x["last_month"], x["mention_count"]), reverse=True)

    result = {
        "generated_at": int(time.time() * 1000),
        "latest_month": latest_month,
        "total_stocks": len(out),
        "params": {"cold_months": COLD_MONTHS, "new_months": NEW_MONTHS, "revive_gap": REVIVE_GAP},
        "changes": {
            "new": [c for c in new_list],
            "revived": [c for c in revived_list],
            "cold": [c for c in cold_list],
            "core": [c for c in core_list],
        },
        "stocks": out,
    }
    json.dump(result, open(DATA / "selfstock.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[selfstock] 关注股票总数: {len(out)} | 最新月份: {latest_month}")
    print(f"  新关注 {len(new_list)} | 重提 {len(revived_list)} | 已冷落 {len(cold_list)} | 持续关注 {len(core_list)}")


if __name__ == "__main__":
    main()
