#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 data/raw/*.json 合并并归一化所有发言
输出：
  - data/posts.json       归一化后的列表
  - data/posts_raw.json   原始去重后的列表
  - data/user.json        博主元信息
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DATA = ROOT / "data"

def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ")
           .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'"))
    s = re.sub(r"\s+", " ", s).strip()
    return s

def get_ip(meta):
    if not meta:
        return ""
    m = re.search(r'"ip_location"\s*:\s*"([^"]+)"', meta)
    return m.group(1) if m else ""

def local_images(pic):
    """从 pic 字段（逗号分隔 URL 串）提取本地相对路径列表 images/<seg>。"""
    out = []
    if not pic:
        return out
    for u in pic.split(","):
        u = u.strip()
        if not u:
            continue
        seg = u.split('!')[0].rstrip('/').split('/')[-1]
        if seg:
            out.append("images/" + seg)
    return out


def normalize(s):
    user = s.get("user") or {}
    text = strip_html(s.get("text") or "") or strip_html(s.get("description") or "")
    desc = s.get("description") or ""
    mentions = re.findall(r"回复\[@([^\]]+)\]|@([\w\u4e00-\u9fa5]+)", desc)
    mentioned = list(dict.fromkeys([m[0] or m[1] for m in mentions if (m[0] or m[1])]))[:5]
    target = s.get("target") or ""
    uid = user.get("id") or s.get("user_id") or 2292705444
    sid = s.get("id")
    url = f"https://xueqiu.com{target}" if target else (f"https://xueqiu.com/{uid}/{sid}" if sid else "")
    pic = s.get("pic", "") or ""
    return {
        "id": s.get("id"),
        "created_at": s.get("created_at"),
        "timeBefore": s.get("timeBefore", ""),
        "source": strip_html(s.get("source", "")) or s.get("source", ""),
        "title": s.get("title", ""),
        "text_html": s.get("text") or "",
        "text": text,
        "description": desc,
        "retweet_count": s.get("retweet_count", 0) or 0,
        "reply_count": s.get("reply_count", 0) or 0,
        "fav_count": s.get("fav_count", 0) or 0,
        "like_count": s.get("like_count", 0) or 0,
        "view_count": s.get("view_count", 0) or 0,
        "type": str(s.get("type", "0")),
        "ip_location": get_ip(s.get("meta_keywords")),
        "stockCorrelation": s.get("stockCorrelation", []) or [],
        "mentioned": mentioned,
        "pic": pic,
        "images": local_images(pic),
        "target": target,
        "url": url,
        "screen_name": user.get("screen_name", "metalslime"),
        "user_id": user.get("id", 2292705444),
    }

def main():
    RAW.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    files = sorted(RAW.glob("page_*.json"))
    if not files:
        print("[warn] no raw files", file=sys.stderr)
        sys.exit(1)

    seen = set()
    all_posts = []
    for fp in files:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            data = json.loads(f.read(), strict=False)
        for s in (data.get("statuses") or []):
            sid = s.get("id")
            if sid and sid not in seen:
                seen.add(sid)
                all_posts.append(s)

    all_posts.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    normed = [normalize(s) for s in all_posts]

    with open(DATA / "posts.json", "w", encoding="utf-8") as f:
        json.dump(normed, f, ensure_ascii=False, indent=2)
    with open(DATA / "posts_raw.json", "w", encoding="utf-8") as f:
        json.dump(all_posts, f, ensure_ascii=False)

    user = {
        "id": 2292705444,
        "screen_name": "metalslime",
        "profile": "https://xueqiu.com/u/2292705444",
        "description": "雪球博主 · 创新药 & AI 产业链观察",
        "followers_count": 244752,
        "friends_count": 351,
        "status_count": 30003,
        "fetched_at": int(__import__("time").time() * 1000),
    }
    with open(DATA / "user.json", "w", encoding="utf-8") as f:
        json.dump(user, f, ensure_ascii=False, indent=2)

    print(f"saved {len(normed)} posts from {len(files)} files")

if __name__ == "__main__":
    main()
