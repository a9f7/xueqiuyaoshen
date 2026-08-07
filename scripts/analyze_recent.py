#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「首席视角 · 近 N 天发言解读」所需的预计算数据 -> data/analysis_recent.json

逻辑：
  - 读取 posts.json / comments.json / reposts.json（全部发言）
  - 按 created_at 过滤最近 N 天（默认 15）
  - 计算：数量分布、各维度标签频次（行业/地域/视角/资产/立场/类型）、
          多空立场汇总、最热发言（按互动量）、关键信号（高互动短句）
  - 新增「方向多空 + 具体标的」分析：
       * 给每条发言判定主立场（看多 / 看空 / 非方向性），基于其 stance 标签
       * 看涨方向 = 看多发言里出现的行业主题；看跌方向 = 看空发言里出现的行业主题
       * 具体标的 = 正文 $名称(代码) 提取，按主立场分「看多标的 / 看空标的」，
         并给出每只标的总体多空倾向（看多>看空=看多，反之看空，均沾=多空交织）
  - 生成 data-driven 的叙述文本（首席视角口吻）+ headline 一句话结论
  - 输出体积很小（几 KB~十几 KB），前端首屏直接加载，不拖累时间线渲染

用法：
  python scripts/analyze_recent.py [--days 15] [--out data/analysis_recent.json]
"""
import json
import os
import re
import sys
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 复用 tag_posts.py 的维度映射 + daily_review.py 的个股提取
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_posts import TAG_DIM, DIMENSIONS
from daily_review import SYM_RE, extract_symbols

DIM_LABEL = {
    "industry": "行业主题",
    "region": "地域",
    "perspective": "视角层级",
    "asset": "资产类别",
    "stance": "观点立场",
    "ctype": "内容类型",
}
STANCE_TAGS = ["看多", "看空", "中性", "风险提示", "复盘"]

# 主立场优先级：看多 > 看空 > 其余（非方向性）
def item_stance(tags):
    tset = set(tags or [])
    if "看多" in tset and "看空" not in tset:
        return "bull"
    if "看空" in tset and "看多" not in tset:
        return "bear"
    return None  # 非方向性（复盘/风险/中性/其他）或 多空并存（极少）


def load_arr(path):
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        try:
            return json.loads(open(path, encoding="utf-8", errors="replace").read(), strict=False)
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
    ap.add_argument("--archive", action="store_true",
                    help="同时将结果按北京时间整点归档到 data/hourly/YYYY-MM-DD_HH.json（每小时任务用）")
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
                it["_stance"] = item_stance(it.get("tags"))
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

    # ---------------- 方向多空 + 具体标的 ----------------
    bull_items = [it for it in items if it["_stance"] == "bull"]
    bear_items = [it for it in items if it["_stance"] == "bear"]

    # 方向多空（净方向）：同一行业在「看多发言」vs「看空发言」里的出现次数之差
    #   net>0 视为该行业整体被看多，net<0 视为被看空；net=0 视为多空平衡/结构性
    ind_bull = Counter()
    ind_bear = Counter()
    for it in bull_items:
        for t in (it.get("tags") or []):
            if TAG_DIM.get(t) == "industry":
                ind_bull[t] += 1
    for it in bear_items:
        for t in (it.get("tags") or []):
            if TAG_DIM.get(t) == "industry":
                ind_bear[t] += 1
    all_ind = set(ind_bull) | set(ind_bear)
    net = {t: ind_bull[t] - ind_bear[t] for t in all_ind}
    net_bull = sorted([t for t in all_ind if net[t] > 0], key=lambda t: -net[t])
    net_bear = sorted([t for t in all_ind if net[t] < 0], key=lambda t: net[t])

    # 具体标的：正文 $名称(代码) 提取，按主立场累加
    sym = {}  # name -> {code, count, bull, bear}
    for it in items:
        txt = it.get("text") or it.get("description") or ""
        st = it["_stance"]
        seen_sym = set()
        for name, code in extract_symbols(txt):
            if name in seen_sym:
                continue
            seen_sym.add(name)
            if name not in sym:
                sym[name] = {"code": code or "", "count": 0, "bull": 0, "bear": 0}
            sym[name]["count"] += 1
            if st == "bull":
                sym[name]["bull"] += 1
            elif st == "bear":
                sym[name]["bear"] += 1
            if code:
                sym[name]["code"] = code

    sym_list = []
    for name, v in sym.items():
        b, r = v["bull"], v["bear"]
        if b > r:
            stance = "bull"
        elif r > b:
            stance = "bear"
        elif b > 0 or r > 0:
            stance = "mixed"
        else:
            stance = "neutral"
        sym_list.append({"name": name, "code": v["code"], "count": v["count"],
                         "bull": b, "bear": r, "stance": stance})
    sym_list.sort(key=lambda x: (-x["count"], -x["bull"], x["name"]))

    bull_syms = [s for s in sym_list if s["stance"] == "bull"]
    bear_syms = [s for s in sym_list if s["stance"] == "bear"]
    mixed_syms = [s for s in sym_list if s["stance"] == "mixed"]

    bullish = {
        "items": len(bull_items),
        "directions": [{"tag": t, "count": net[t]} for t in net_bull[:6]],
        "symbols": [{"name": s["name"], "code": s["code"], "count": s["count"]} for s in bull_syms[:8]],
    }
    bearish = {
        "items": len(bear_items),
        "directions": [{"tag": t, "count": -net[t]} for t in net_bear[:6]],
        "symbols": [{"name": s["name"], "code": s["code"], "count": s["count"]} for s in bear_syms[:8]],
    }

    # ---------------- data-driven 叙述（首席视角口吻）----------------
    ind = dims.get("industry", [])
    top3 = "、".join(t["tag"] for t in ind[:3]) if ind else "多主题"
    bull = stance_summary.get("看多", 0)
    bear = stance_summary.get("看空", 0)
    reg = dims.get("region", [])
    reg_top = "、".join(t["tag"] for t in reg[:2]) if reg else ""
    persp = dims.get("perspective", [])
    persp_top = "、".join(t["tag"] for t in persp[:3]) if persp else ""

    bull_dir_top = "、".join(net_bull[:3]) if net_bull else ""
    bear_dir_top = "、".join(net_bear[:3]) if net_bear else ""
    bull_sym_top = "、".join(s["name"] + (f"({s['code']})" if s["code"] else "") for s in bull_syms[:4])
    bear_sym_top = "、".join(s["name"] + (f"({s['code']})" if s["code"] else "") for s in bear_syms[:4])

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
    # 方向多空段（新增）
    dir_parts = []
    if bull_dir_top:
        dir_parts.append(f"**看涨方向**集中在 {bull_dir_top}")
    if bear_dir_top:
        dir_parts.append(f"**看跌方向**集中在 {bear_dir_top}")
    if dir_parts:
        narrative.append("从方向看，" + "；".join(dir_parts) + "。")
    sym_parts = []
    if bull_sym_top:
        sym_parts.append(f"明确看多的标的：{bull_sym_top}")
    if bear_sym_top:
        sym_parts.append(f"明确看空的标的：{bear_sym_top}")
    if mixed_syms:
        sym_parts.append("多空交织（既被看多也被看空）的标的：" +
                         "、".join(s["name"] + (f"({s['code']})" if s["code"] else "") for s in mixed_syms[:5]))
    if not sym_parts:
        if sym_list:
            sym_parts.append("正文点名的具体个股（未明确多空倾向）：" +
                             "、".join(s["name"] + (f"({s['code']})" if s["code"] else "") for s in sym_list[:6]))
        else:
            sym_parts.append("正文未以 $名称(代码) 形式显式点名具体个股，方向判断主要依据行业主题与措辞倾向")
    narrative.append("具体标的：" + "；".join(sym_parts) + "。")

    # headline 一句话结论
    if bull or bear:
        tone_short = "多空交织偏审慎" if bear >= bull * 0.5 else ("明显偏多" if bull > bear else "明显偏空")
    else:
        tone_short = "未形成明确方向"
    headline = f"近 {args.days} 天：{tone_short}。"
    if bull_dir_top:
        headline += f" 看涨 {bull_dir_top}。"
    if bear_dir_top:
        headline += f" 看跌 {bear_dir_top}。"
    if bull_sym_top:
        headline += f" 看多标的 {bull_sym_top}。"
    if bear_sym_top:
        headline += f" 看空标的 {bear_sym_top}。"

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
        "bullish": bullish,
        "bearish": bearish,
        "symbols": sym_list,
        "headline": headline,
        "narrative": narrative,
    }
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 归档：按北京时间整点保存到 data/hourly/（本地，不进 GitHub，避免仓库膨胀）
    if args.archive:
        bj = datetime.now(timezone(timedelta(hours=8)))
        hourly_dir = DATA / "hourly"
        hourly_dir.mkdir(parents=True, exist_ok=True)
        arch_path = hourly_dir / f"{bj.strftime('%Y-%m-%d_%H')}.json"
        json.dump(out, open(arch_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[analyze] 归档 -> {arch_path}")

    print(f"[analyze] 窗口 {args.days} 天，发言 {counts['total']} 条 -> {args.out}")
    print(f"[analyze] 行业 Top5: " + ", ".join(f"{t['tag']}({t['count']})" for t in ind[:5]))
    print(f"[analyze] 立场: {stance_summary}")
    print(f"[analyze] 看涨方向: {bull_dir_top or '—'} | 看跌方向: {bear_dir_top or '—'}")
    print(f"[analyze] 看多标的 {len(bull_syms)} / 看空标的 {len(bear_syms)} / 多空交织 {len(mixed_syms)} / 全部 {len(sym_list)}")
    print(f"[analyze] headline: {headline}")


if __name__ == "__main__":
    main()
