#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 data/posts.json 按月切分并精简字段，生成前端友好的分块数据：
  - data/posts_index.json   轻量索引（按月统计 + 全局统计），首屏只加载它
  - data/months/posts_YYYY-MM.json  每月一个文件（精简字段）
全量精简版 data/posts.json 仍保留（供后端/ima 使用）。
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MONTHS = DATA / "months"

# 前端只需的字段（去掉 text_html/description/target/screen_name/user_id/pic/mentioned 等冗余）
KEEP = [
    "id", "created_at", "source", "type", "text",
    "ip_location", "reply_count", "retweet_count", "fav_count",
    "like_count", "stockCorrelation", "url", "images",
]


def slim(p):
    out = {}
    for k in KEEP:
        v = p.get(k)
        # 丢空值减小体积
        if v in (None, "", [], {}):
            continue
        out[k] = v
    return out


def main():
    posts_path = DATA / "posts.json"
    if not posts_path.exists():
        print("[warn] data/posts.json not found", file=sys.stderr)
        sys.exit(1)

    with open(posts_path, "r", encoding="utf-8") as f:
        posts = json.load(f)

    buckets = defaultdict(list)
    for p in posts:
        ts = p.get("created_at")
        if ts:
            ym = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        else:
            ym = "unknown"
        buckets[ym].append(slim(p))

    MONTHS.mkdir(parents=True, exist_ok=True)

    orig = sum(1 for p in posts if str(p.get("type")) == "0")
    repost = sum(1 for p in posts if str(p.get("type")) == "1")

    index = {
        "updated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(posts),
        "orig": orig,
        "repost": repost,
        "months": [],
    }

    for ym in sorted(buckets.keys(), reverse=True):
        arr = buckets[ym]
        # 月内按时间倒序
        arr.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        fn = MONTHS / f"posts_{ym}.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, separators=(",", ":"))
        size_kb = round(os.path.getsize(fn) / 1024)
        index["months"].append({
            "ym": ym,
            "count": len(arr),
            "file": f"months/posts_{ym}.json",
            "size_kb": size_kb,
        })

    with open(DATA / "posts_index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    print(f"total {len(posts)} posts -> {len(buckets)} months")
    for m in index["months"]:
        print(f"  {m['ym']}: {m['count']:>4}  ({m['size_kb']} KB)")
    idx_kb = round(os.path.getsize(DATA / 'posts_index.json') / 1024)
    print(f"index: {idx_kb} KB")


if __name__ == "__main__":
    main()
