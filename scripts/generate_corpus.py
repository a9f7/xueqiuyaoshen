#!/usr/bin/env python3
"""将归一化后的 posts.json 按月份导出为纯文本语料（用于导入 ima 知识库）。
每个月份一个 txt 文件：data/corpus/posts_YYYY-MM.txt
格式：
  [2026-08-02 10:18] （赞265 转0 评12 | 北京） $BABA
  正文文本
  [图] https://...jpg
  ---
"""
import json, os, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "posts.json")
OUT = os.path.join(ROOT, "data", "corpus")
os.makedirs(OUT, exist_ok=True)


def fmt(ts):
    return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def main():
    posts = json.load(open(SRC, encoding="utf-8"))
    months = {}
    for p in posts:
        ts = p.get("created_at", 0)
        ym = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m")
        months.setdefault(ym, []).append(p)

    total_files = 0
    total_bytes = 0
    for ym in sorted(months):
        items = sorted(months[ym], key=lambda x: x.get("created_at", 0), reverse=True)
        path = os.path.join(OUT, f"posts_{ym}.txt")
        buf = []
        buf.append(f"# 雪球用户 metalslime（@metalslime）发言归档 - {ym}")
        buf.append(f"# 本月共 {len(items)} 条发言")
        buf.append("")
        for p in items:
            head = f"[{fmt(p.get('created_at', 0))}]"
            like = p.get("like_count") or 0
            ret = p.get("retweet_count") or 0
            rep = p.get("reply_count") or 0
            loc = p.get("ip_location") or ""
            stk = p.get("stockCorrelation") or ""
            head += f" （赞{like} 转{ret} 评{rep}"
            if loc:
                head += f" | {loc}"
            head += "）"
            if stk:
                head += f" {stk}"
            buf.append(head)
            text = (p.get("text") or "").strip()
            buf.append(text if text else "（无正文）")
            imgs = p.get("images") or []
            if imgs:
                buf.append("[图] " + " ; ".join(imgs))
            buf.append("---")
            buf.append("")
        content = "\n".join(buf)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        total_files += 1
        total_bytes += len(content.encode("utf-8"))

    print(f"生成 {total_files} 个月份语料文件，总计 {total_bytes/1024/1024:.1f} MB")
    # 列出最大的几个文件，便于评估单文件上限
    sizes = sorted(
        ((f, os.path.getsize(os.path.join(OUT, f))) for f in os.listdir(OUT) if f.endswith(".txt")),
        key=lambda x: -x[1],
    )[:5]
    print("最大文件：")
    for f, s in sizes:
        print(f"  {f}: {s/1024:.1f} KB")


if __name__ == "__main__":
    main()
