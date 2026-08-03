#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「首席视角 · 近 N 天发言解读」所需的预计算数据 -> data/analysis_recent.json

逻辑：
  - 读取 posts.json / comments.json / reposts.json（全部发言）
  - 按 created_at 过滤最近 N 天（默认 15）
  - 计算：数量分布、各维度标签频次（行业/地域/视角/资产/立场/类型）、
          多空立场汇总、最热发言（按互动量）、关键信号（高互动短句）
  - 生成 data-driven 的叙述文本（首席视角口吻）；质量可在本地人工润色后写回
  - 输出体积很小（几 KB），前端首屏直接加载，不拖累时间线渲染

用法：
  python scripts/analyze_recent.py [--days 15] [--out data/analysis_recent.json]
"""
import json
import os
import re
import sys
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 复用 tag_posts.py 的维度映射
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_posts import TAG_DIM, DIMENSIONS

DIM_LABEL = {
    "industry": "行业主题",
    "region": "地域",
    "perspective": "视角层级",
    "asset": "资产类别",
    "stance": "观点立场",
    "ctype": "内容类型",
}
STANCE_TAGS = ["看多", "看空", "中性", "风险提示", "复盘"]


def load_arr(path):
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return []


def engagement(it):
    return (it.get("like_count") or 0) + (it.get("reply_count") or 0) + (it.get("retweet_count") or 0)


def excerpt(text, n=140):
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--out", default=str(DATA / "analysis_recent.json"))
    args = ap.parse_args()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cut = now_ms - args.days * 86400 * 1000

    items = []
    for fn in ["posts.json", "comments.json", "reposts.json"]:
        for it in load_arr(DATA / fn):
            ts = it.get("created_at") or 0
            if ts >= cut:
                it["_kind"] = it.get("kind") or ("post" if fn == "posts.json" else fn.replace(".json", ""))
                it["_eng"] = engagement(it)
                items.append(it)

    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)

    # 数量分布
    kind_counter = Counter(it["_kind"] for it in items)
    counts = {
        "post": kind_counter.get("post", 0),
        "comment": kind_counter.get("comment", 0),
        "repost": kind_counter.get("repost", 0),
        "total": len(items),
    }

    # 维度标签频次
    tag_counter = Counter()
    for it in items:
        for t in (it.get("tags") or []):
            tag_counter[t] += 1

    dims = {}
    for dim, _ in DIMENSIONS:
        tags = [(t, c) for t, c in tag_counter.items() if TAG_DIM.get(t) == dim]
        tags.sort(key=lambda x: x[1], reverse=True)
        dims[dim] = [{"tag": t, "count": c} for t, c in tags[:10]]

    stance_summary = {s: tag_counter.get(s, 0) for s in STANCE_TAGS}

    # 最热发言（按互动量）
    top = sorted(items, key=lambda x: x["_eng"], reverse=True)[:6]
    top_posts = []
    for it in top:
        top_posts.append({
            "id": it.get("id"),
            "created_at": it.get("created_at"),
            "kind": it["_kind"],
            "text": excerpt(it.get("text") or it.get("description") or "", 160),
            "engagement": it["_eng"],
            "url": it.get("url") or (it.get("original", {}) or {}).get("url")
                   or f"https://xueqiu.com/2292705444/{it.get('id')}",
            "tags": it.get("tags") or [],
        })

    # 关键信号（高互动 + 短句，作为金句引用）
    short = [it for it in items if 24 <= len((it.get("text") or "").strip()) <= 90]
    short.sort(key=lambda x: x["_eng"], reverse=True)
    signals = []
    seen = set()
    for it in short:
        key = it.get("id")
        if key in seen:
            continue
        seen.add(key)
        signals.append({
            "id": it.get("id"),
            "created_at": it.get("created_at"),
            "text": (it.get("text") or "").replace("\n", " ").strip(),
            "url": it.get("url") or f"https://xueqiu.com/2292705444/{it.get('id')}",
        })
        if len(signals) >= 5:
            break

    # ---- data-driven 叙述（首席视角口吻）----
    ind = dims.get("industry", [])
    top3 = "、".join(t["tag"] for t in ind[:3]) if ind else "多主题"
    bull = stance_summary.get("看多", 0)
    bear = stance_summary.get("看空", 0)
    reg = dims.get("region", [])
    reg_top = "、".join(t["tag"] for t in reg[:2]) if reg else ""
    persp = dims.get("perspective", [])
    persp_top = "、".join(t["tag"] for t in persp[:3]) if persp else ""

    narrative = []
    narrative.append(
        f"近 {args.days} 天（{datetime.fromtimestamp(cut/1000, tz=timezone.utc).strftime('%Y-%m-%d')} 至今），"
        f"药神共发布 {counts['total']} 条发言（原贴 {counts['post']} / 评论 {counts['comment']} / 转发 {counts['repost']}）。"
        f"讨论高度聚焦于 {top3} 等主线，整体呈现「研究驱动、结构性而非单边」的表达特征。"
    )
    if ind:
        lead = ind[0]
        narrative.append(
            f"核心主线是「{lead['tag']}」（{lead['count']} 条），其次为 "
            + "、".join(t["tag"] for t in ind[1:3]) + "。"
            + (f"视角上以 {persp_top} 为主，" if persp_top else "")
            + (f"地域覆盖偏向 {reg_top}。" if reg_top else "")
            + "其论述多从产业链价值分配与「谁在捕获利润」切入，而非简单做多/做空指数。"
        )
    # 立场段
    if bull or bear:
        tone = "多空交织、偏审慎" if bear >= bull * 0.5 else ("明显偏多" if bull > bear else "明显偏空")
        narrative.append(
            f"立场分布：看多 {bull} 条 vs 看空 {bear} 条，整体{tone}；"
            f"风险提示 {stance_summary.get('风险提示',0)} 条、复盘 {stance_summary.get('复盘',0)} 条，"
            "说明作者在输出观点的同时保留了较强的风险意识与自我迭代痕迹。"
        )

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "days": args.days,
            "start_ts": cut,
            "end_ts": now_ms,
            "start": datetime.fromtimestamp(cut / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "end": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        },
        "counts": counts,
        "dims": dims,
        "dim_label": DIM_LABEL,
        "stance_summary": stance_summary,
        "top_posts": top_posts,
        "signals": signals,
        "narrative": narrative,
    }
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[analyze] 窗口 {args.days} 天，发言 {counts['total']} 条 -> {args.out}")
    print(f"[analyze] 行业 Top5: " + ", ".join(f"{t['tag']}({t['count']})" for t in ind[:5]))
    print(f"[analyze] 立场: {stance_summary}")
    print(f"[analyze] 最热发言 Top3 互动: " + ", ".join(str(p['engagement']) for p in top_posts[:3]))


if __name__ == "__main__":
    main()
