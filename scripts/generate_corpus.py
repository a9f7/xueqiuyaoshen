#!/usr/bin/env python3
"""将归一化后的 posts.json / comments.json / reposts.json 按月份导出为纯文本语料（用于导入 ima 知识库）。
  - data/corpus/posts_YYYY-MM.txt        原贴
  - data/corpus/comments_YYYY-MM.txt     评论（含内嵌「原帖」上下文）
  - data/corpus/reposts_YYYY-MM.txt      转发（含内嵌「原帖」上下文）
"""
import json
import os
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "corpus")
os.makedirs(OUT, exist_ok=True)


def fmt(ts):
    try:
        return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def write_month(prefix, ym, items, title):
    items = sorted(items, key=lambda x: x.get("created_at", 0), reverse=True)
    path = os.path.join(OUT, f"{prefix}_{ym}.txt")
    buf = [f"# 雪球用户 metalslime（@metalslime）{title} - {ym}", f"# 本月共 {len(items)} 条", ""]
    for p in items:
        kind = p.get("kind", "post")
        head = f"[{fmt(p.get('created_at', 0))}]"
        like = p.get("like_count") or 0
        head += f" （赞{like}"
        if p.get("ip_location"):
            head += f" | {p['ip_location']}"
        head += "）"
        if kind == "comment":
            head = "评论 " + head
            if p.get("reply_to"):
                head += f" 回复 @{p['reply_to']}"
        elif kind == "repost":
            head = "转发 " + head
        buf.append(head)
        text = (p.get("text") or "").strip()
        buf.append(text if text else "（无正文）")
        # 内嵌原文上下文
        orig = p.get("original")
        if isinstance(orig, dict) and orig.get("text"):
            ot = orig.get("text", "").strip().replace("\n", " ")
            buf.append(f"> 原帖 @{orig.get('user', '')}: {ot[:300]}")
        # 股票
        stk = p.get("stocks") or p.get("stockCorrelation") or []
        if stk:
            buf.append("股票: " + " ".join(stk))
        # 语义标签
        tags = p.get("tags") or []
        if tags:
            buf.append("标签: " + " ".join(tags))
        imgs = p.get("images") or []
        if imgs:
            buf.append("[图] " + " ; ".join(imgs))
        buf.append("---")
        buf.append("")
    content = "\n".join(buf)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return len(content.encode("utf-8"))


def main():
    files = 0
    bytes_total = 0
    # 原贴
    posts = json.load(open(os.path.join(ROOT, "data", "posts.json"), encoding="utf-8"))
    pmon = {}
    for p in posts:
        ym = datetime.datetime.fromtimestamp(p.get("created_at", 0) / 1000).strftime("%Y-%m")
        pmon.setdefault(ym, []).append(p)
    for ym in sorted(pmon):
        b = write_month("posts", ym, pmon[ym], "发言归档")
        files += 1
        bytes_total += b
    # 评论 + 转发
    for src, prefix, title in [("comments.json", "comments", "评论归档"), ("reposts.json", "reposts", "转发归档")]:
        path = os.path.join(ROOT, "data", src)
        if not os.path.exists(path):
            continue
        arr = json.load(open(path, encoding="utf-8"))
        cmon = {}
        for c in arr:
            ts = c.get("created_at", 0)
            if not ts:
                continue
            ym = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m")
            cmon.setdefault(ym, []).append(c)
        for ym in sorted(cmon):
            b = write_month(prefix, ym, cmon[ym], title)
            files += 1
            bytes_total += b

    print(f"生成 {files} 个月份语料文件，总计 {bytes_total/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()
