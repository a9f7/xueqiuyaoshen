#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 data/posts.json + comments.json + reposts.json 按月切分并精简字段，生成前端友好的分块数据：
  - data/posts_index.json   轻量索引（按月统计 + 全局统计 + 评论/转发计数），首屏只加载它
  - data/months/posts_YYYY-MM.json  每月一个文件（含原贴/评论/转发，评论/转发内嵌原文上下文）

评论与转发通过 kind 区分：post / comment / repost。
全量文件 data/posts.json / comments.json / reposts.json / interactions.json 仍保留（供后端/ima 使用）。
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

KEEP_POST = ["id", "created_at", "source", "type", "text", "ip_location",
             "reply_count", "retweet_count", "fav_count", "like_count",
             "stockCorrelation", "url", "images", "tags"]
KEEP_CM = ["id", "created_at", "kind", "text", "original", "reply_to",
           "stocks", "like_count", "url", "images", "ip_location", "source", "tags"]


def slim(p, keep):
    out = {}
    for k in keep:
        v = p.get(k)
        if v in (None, "", [], {}):
            continue
        out[k] = v
    return out


def ym_of(ts):
    if not ts:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
    except Exception:
        return "unknown"


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    posts = load(DATA / "posts.json")
    comments = load(DATA / "comments.json")
    reposts = load(DATA / "reposts.json")

    posts = [{**p, "kind": "post"} for p in posts]

    buckets = defaultdict(list)
    for p in posts:
        buckets[ym_of(p.get("created_at"))].append(slim(p, KEEP_POST))
    for c in comments:
        buckets[ym_of(c.get("created_at"))].append(slim(c, KEEP_CM))
    for r in reposts:
        buckets[ym_of(r.get("created_at"))].append(slim(r, KEEP_CM))

    MONTHS.mkdir(parents=True, exist_ok=True)

    index = {
        "updated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(posts) + len(comments) + len(reposts),
        "post_count": len(posts),
        "comment_count": len(comments),
        "repost_count": len(reposts),
        "selfstock": "data/selfstock.json",
        "months": [],
    }

    for ym in sorted(buckets.keys(), reverse=True):
        arr = buckets[ym]
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

    print(f"posts {len(posts)} + comments {len(comments)} + reposts {len(reposts)} "
          f"-> {len(buckets)} months (total {index['total']})")
    for m in index["months"]:
        print(f"  {m['ym']}: {m['count']:>4}  ({m['size_kb']} KB)")
    print(f"index: {round(os.path.getsize(DATA / 'posts_index.json') / 1024)} KB")


if __name__ == "__main__":
    main()
