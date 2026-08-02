#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
基于本次最新抓取生成 markdown 摘要，输出到 stdout（重定向到 summary.md）
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

def fmt_time(ts):
    if not ts:
        return ""
    t = time.localtime(ts / 1000)
    return time.strftime("%Y-%m-%d %H:%M", t)

def main():
    posts_fp = DATA / "posts.json"
    if not posts_fp.exists():
        print("no data")
        return
    posts = json.load(open(posts_fp, "r", encoding="utf-8"))
    user_fp = DATA / "user.json"
    user = json.load(open(user_fp, "r", encoding="utf-8")) if user_fp.exists() else {}

    print("# 雪球药神 · 最近发言摘要")
    print()
    if user:
        print(f"- 博主：**{user.get('screen_name','metalslime')}**")
        print(f"- 主页：<{user.get('profile','')}>")
        print(f"- 已收录：**{len(posts)}** 条")
    print()
    print("## 最新 10 条")
    print()
    for p in posts[:10]:
        ts = fmt_time(p.get("created_at"))
        text = (p.get("text") or "").replace("\n", " ")[:200]
        tags = " ".join(f"`{t}`" for t in (p.get("stockCorrelation") or []))
        url = p.get("url") or ""
        print(f"### {ts} · #{p.get('id')}")
        if tags:
            print(f"板块：{tags}")
        print()
        print(f"> {text}{'...' if len(p.get('text',''))>200 else ''}")
        print()
        if url:
            print(f"[原帖]({url})")
        print(f"💬 {p.get('reply_count',0)}  👍 {p.get('like_count',0)}  🔁 {p.get('retweet_count',0)}")
        print()
        print("---")
        print()

    # 关键词
    from collections import Counter
    import re
    words = Counter()
    for p in posts:
        text = p.get("text") or ""
        for w in re.findall(r"[\$#]([\u4e00-\u9fa5A-Za-z]{2,8})", text):
            words[w] += 1
    if words:
        print("## 近期关键词（出现 ≥ 2 次）")
        print()
        items = [w for w, c in words.most_common(30) if c >= 2][:20]
        print("、".join(items) if items else "无")
        print()

if __name__ == "__main__":
    main()
