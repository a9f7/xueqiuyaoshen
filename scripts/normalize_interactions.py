#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归一化评论/转发：从 raw_comments / raw_reposts 提取真实评论与转发，
内嵌「被评论/被转的原文」作为上下文，并去重（与 posts.json 重复的帖子容器跳过）。

输出：
  data/comments.json   评论列表（每条含 original 上下文）
  data/reposts.json    转发列表（每条含 original 上下文）
  data/interactions.json  合并（comments + reposts），供前端/DB 使用
"""
import json
import os
import re
import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 复用 normalize_posts 的清洗函数
sys.path.insert(0, str(ROOT / "scripts"))
from normalize_posts import strip_html, local_images  # noqa: E402

SYMBOL_RE = re.compile(r"\$([^($<]+?)\(([A-Z]{1,2}\d{4,6})\)")


def extract_symbols(text):
    out = []
    for m in SYMBOL_RE.finditer(text or ""):
        name = m.group(1).strip()
        code = m.group(2).strip()
        out.append({"code": code, "name": name})
    return out


def parse_reply_to(text):
    """从 '回复@某人: ...' 提取被回复者。"""
    m = re.search(r"回复\s*@([^\s:：]+)\s*[:：]", text or "")
    return m.group(1) if m else ""


def _ip_location(mk):
    """从 meta_keywords(JSON字符串) 提取 ip_location，无匹配返回空串。"""
    m = re.search(r'"ip_location"\s*:\s*"([^"]+)"', mk or "")
    return m.group(1) if m else ""


def original_of(s):
    """提取内嵌原文（被评论/被转的帖）。"""
    rt = s.get("retweeted_status")
    if not isinstance(rt, dict) or not rt:
        return None
    u = rt.get("user") or {}
    uid = u.get("id") or rt.get("user_id")
    oid = rt.get("id")
    target = rt.get("target") or (f"/{uid}/{oid}" if uid and oid else "")
    return {
        "id": oid,
        "user_id": uid,
        "user": u.get("screen_name", ""),
        "text": strip_html(rt.get("text") or rt.get("description") or ""),
        "url": ("https://xueqiu.com" + target) if target else "",
        "created_at": rt.get("created_at"),
    }


def norm_item(s, kind):
    user = s.get("user") or {}
    text = strip_html(s.get("text") or s.get("description") or "")
    oid = s.get("id")
    uid = user.get("id") or s.get("user_id") or 2292705444
    target = s.get("target") or (f"/{uid}/{oid}" if oid else "")
    pic = s.get("pic", "") or ""
    sc = s.get("stockCorrelation", []) or []
    symbols = extract_symbols(text)
    # 合并 stockCorrelation(板块) 与 $SYMBOL(个股)
    stocks = list(dict.fromkeys([c for c in sc if c])) + [x["code"] for x in symbols]
    return {
        "id": oid,
        "kind": kind,                     # comment / repost
        "created_at": s.get("created_at"),
        "commentId": s.get("commentId", 0) or 0,
        "reply_to": parse_reply_to(text) if kind == "comment" else "",
        "source": strip_html(s.get("source", "")) or s.get("source", ""),
        "text_html": s.get("text") or s.get("description") or "",
        "text": text,
        "like_count": s.get("like_count", 0) or 0,
        "reply_count": s.get("reply_count", 0) or 0,
        "retweet_count": s.get("retweet_count", 0) or 0,
        "ip_location": _ip_location(s.get("meta_keywords") or ""),
        "stocks": stocks,
        "symbols": symbols,              # [{code,name}]
        "mentioned": [],
        "pic": pic,
        "images": local_images(pic),
        "target": target,
        "url": ("https://xueqiu.com" + target) if target else "",
        "original": original_of(s),      # 内嵌原文上下文（可能 None）
        "user_id": uid,
        "user": user.get("screen_name", "metalslime"),
    }


def load_post_ids():
    pj = DATA / "posts.json"
    if not pj.exists():
        return set()
    return set(p.get("id") for p in json.loads(open(pj, encoding="utf-8", errors="replace").read(), strict=False) if p.get("id"))


def main():
    post_ids = load_post_ids()
    print(f"[norm] posts.json 基准 ID 数: {len(post_ids)}")

    comments = []
    reposts = []
    for f in sorted(glob.glob(str(DATA / "raw_comments" / "page_*.json"))):
        try:
            data = json.loads(open(f, encoding="utf-8", errors="replace").read(), strict=False)
        except Exception as e:
            print(f"[warn] 跳过损坏的评论原始文件 {os.path.basename(f)}: {e}", file=sys.stderr)
            continue
        for s in (data.get("statuses") or []):
            sid = s.get("id")
            # 跳过帖子容器（id 与 posts 重复且非真实评论）
            if sid in post_ids:
                continue
            if not sid:
                continue
            comments.append(norm_item(s, "comment"))
    for f in sorted(glob.glob(str(DATA / "raw_reposts" / "page_*.json"))):
        try:
            data = json.loads(open(f, encoding="utf-8", errors="replace").read(), strict=False)
        except Exception as e:
            print(f"[warn] 跳过损坏的转发原始文件 {os.path.basename(f)}: {e}", file=sys.stderr)
            continue
        for s in (data.get("statuses") or []):
            sid = s.get("id")
            if sid in post_ids:
                continue
            if not sid:
                continue
            reposts.append(norm_item(s, "repost"))

    comments.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    reposts.sort(key=lambda x: x.get("created_at") or 0, reverse=True)

    with_context = sum(1 for c in comments if c["original"])
    json.dump(comments, open(DATA / "comments.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(reposts, open(DATA / "reposts.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(comments + reposts, open(DATA / "interactions.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[norm] 评论: {len(comments)} 条（含内嵌原文上下文 {with_context} 条）| 转发: {len(reposts)} 条")
    print(f"[norm] 写入 comments.json / reposts.json / interactions.json")


if __name__ == "__main__":
    main()
