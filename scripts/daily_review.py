#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「每日首席视角 · 发言标的与观点总结」-> data/daily_review.md（+ data/daily/YYYY-MM-DD.md 归档）

聚焦两件事：
  1) 今日集中在什么标的 —— 行业/主题主线（tags.industry 频次）+ 重点提及个股（正文 $名称(代码) 频次）
  2) 主要表达什么观点 —— 多空立场分布 + 首席视角解读 + 今日金句 + 最热发言

券商首席分析师视角口吻，data-driven（复用 tag_posts 的维度映射）。

用法：
  python scripts/daily_review.py                  # 默认总结「昨天」北京时间全天
  python scripts/daily_review.py --days 1         # 近 24h 滚动窗口
  python scripts/daily_review.py --date 2026-08-04 # 指定某天北京时间全天
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_posts import TAG_DIM, DIMENSIONS

DIM_LABEL = {
    "industry": "行业主题", "region": "地域", "perspective": "视角层级",
    "asset": "资产类别", "stance": "观点立场", "ctype": "内容类型",
}
STANCE_TAGS = ["看多", "看空", "中性", "风险提示", "复盘"]
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

# 提取正文中的 $名称(代码) 或 $名称；名称仅含汉字/字母/连字符/点，过滤纯数字与夹带中文标点的噪声
SYM_RE = re.compile(r"\$\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z\-\.]{0,19})\s*\(?\s*([0-9A-Z]{2,8})?\s*\)?")


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


def excerpt(text, n=180):
    return (text or "").replace("\r", " ").replace("\n", " ").strip()[:n]


def extract_symbols(text):
    res = []
    for m in SYM_RE.finditer(text or ""):
        name = m.group(1).strip()
        code = m.group(2)
        if not name or name.isdigit() or "%" in name or "$" in name or len(name) < 2:
            continue
        res.append((name, code))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="近 N 天滚动窗口（覆盖默认昨天）")
    ap.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（北京时间全天）")
    ap.add_argument("--out", default=str(DATA / "daily_review.md"))
    args = ap.parse_args()

    now = datetime.now(TZ)
    if args.date:
        y, m, d = map(int, args.date.split("-"))
        start = datetime(y, m, d, 0, 0, 0, tzinfo=TZ)
        end = start + timedelta(days=1)
    elif args.days:
        end = now
        start = now - timedelta(days=args.days)
    else:
        yesterday = (now - timedelta(days=1)).date()
        start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=TZ)
        end = start + timedelta(days=1)
    cut_s = int(start.timestamp() * 1000)
    cut_e = int(end.timestamp() * 1000)
    report_date = start.strftime("%Y-%m-%d")

    # 收集窗口内发言
    items = []
    for fn in ["posts.json", "comments.json", "reposts.json"]:
        for it in load_arr(DATA / fn):
            ts = it.get("created_at") or 0
            if cut_s <= ts < cut_e:
                it["_kind"] = it.get("kind") or ("post" if fn == "posts.json" else fn.replace(".json", ""))
                it["_eng"] = engagement(it)
                items.append(it)
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)

    # ---- 标的统计 ----
    sym_counter = Counter()
    sym_industry = defaultdict(Counter)
    sym_code = {}
    for it in items:
        txt = it.get("text") or it.get("description") or ""
        seen = set()
        for name, code in extract_symbols(txt):
            if name in seen:
                continue
            seen.add(name)
            sym_counter[name] += 1
            if code:
                sym_code[name] = code
            for t in (it.get("tags") or []):
                if TAG_DIM.get(t) == "industry":
                    sym_industry[name][t] += 1
    top_syms = sym_counter.most_common(15)

    # ---- 维度标签 ----
    tag_counter = Counter()
    for it in items:
        for t in (it.get("tags") or []):
            tag_counter[t] += 1
    dims = {}
    for dim, _ in DIMENSIONS:
        tags = [(t, c) for t, c in tag_counter.items() if TAG_DIM.get(t) == dim]
        tags.sort(key=lambda x: x[1], reverse=True)
        dims[dim] = [{"tag": t, "count": c} for t, c in tags[:12]]
    stance_summary = {s: tag_counter.get(s, 0) for s in STANCE_TAGS}
    kind_counter = Counter(it["_kind"] for it in items)
    counts = {"post": kind_counter.get("post", 0), "comment": kind_counter.get("comment", 0),
              "repost": kind_counter.get("repost", 0), "total": len(items)}

    # ---- 最热 / 金句 ----
    top = sorted(items, key=lambda x: x["_eng"], reverse=True)[:5]
    top_posts = [{
        "id": it.get("id"), "created_at": it.get("created_at"), "kind": it["_kind"],
        "text": excerpt(it.get("text") or it.get("description") or "", 180),
        "reply": it.get("reply_count", 0), "like": it.get("like_count", 0), "retweet": it.get("retweet_count", 0),
        "url": it.get("url") or (it.get("original", {}) or {}).get("url") or f"https://xueqiu.com/2292705444/{it.get('id')}",
        "tags": it.get("tags") or [],
    } for it in top]

    short = [it for it in items if 24 <= len((it.get("text") or "").strip()) <= 90]
    short.sort(key=lambda x: x["_eng"], reverse=True)
    signals = []
    seen = set()
    for it in short:
        if it.get("id") in seen:
            continue
        seen.add(it.get("id"))
        signals.append({
            "id": it.get("id"), "created_at": it.get("created_at"),
            "text": (it.get("text") or "").replace("\n", " ").strip(),
            "url": it.get("url") or f"https://xueqiu.com/2292705444/{it.get('id')}",
        })
        if len(signals) >= 5:
            break

    # ---- data-driven 叙述 ----
    ind = dims.get("industry", [])
    top3 = "、".join(t["tag"] for t in ind[:3]) if ind else "多主题"
    bull = stance_summary.get("看多", 0)
    bear = stance_summary.get("看空", 0)
    reg = dims.get("region", [])
    reg_top = "、".join(t["tag"] for t in reg[:2]) if reg else ""
    persp = dims.get("perspective", [])
    persp_top = "、".join(t["tag"] for t in persp[:3]) if persp else ""

    sym_lines = []
    if ind:
        sym_lines.append("行业/主题主线高度集中于 **" + ind[0]["tag"] + "**（" + str(ind[0]["count"]) + " 条）" +
                         (", 其次 " + "、".join(t["tag"] for t in ind[1:3]) if len(ind) > 1 else "") +
                         ("；地域侧重 " + reg_top if reg_top else "") + "。")
    if top_syms:
        sym_lines.append("具体提及个股（按频次）：" + "、".join(
            f"{n}" + (f"({sym_code[n]})" if n in sym_code else "") for n, _ in top_syms[:8]))

    if bull or bear:
        tone = "多空交织、偏审慎" if bear >= bull * 0.5 else ("明显偏多" if bull > bear else "明显偏空")
        view_lines = [f"立场分布：看多 {bull} / 看空 {bear} / 中性 {stance_summary.get('中性', 0)} / 风险提示 {stance_summary.get('风险提示', 0)} / 复盘 {stance_summary.get('复盘', 0)}，整体{tone}。" +
                     (f"视角以 {persp_top} 为主，" if persp_top else "") +
                     "其论述延续「研究驱动、结构性而非单边」的一贯风格——从产业链价值分配与「谁在捕获利润」切入，而非简单判断指数方向。"]
    else:
        view_lines = ["本期发言以问答与随笔为主，未形成明确方向性立场。"]

    # ---- markdown 渲染 ----
    md = []
    md.append(f"# 雪球药神 · 每日首席视角（{report_date}）")
    md.append("")
    md.append(f"> 覆盖窗口：**{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')}**（北京时间）  ")
    md.append(f"> 共 **{counts['total']}** 条发言（原贴 {counts['post']} / 评论 {counts['comment']} / 转发 {counts['repost']}）")
    md.append("")
    md.append("## 一、今日集中在什么标的")
    md.append("")
    md.append("### 行业 / 主题主线")
    md.append("")
    if ind:
        tot = sum(t["count"] for t in ind) or 1
        for t in ind[:10]:
            pct = t["count"] * 100 // tot
            md.append(f"- **{t['tag']}** · {t['count']} 条（{pct}%）")
    else:
        md.append("- 无明显行业集中")
    md.append("")
    md.append("### 重点提及个股（按提及频次）")
    md.append("")
    if top_syms:
        md.append("| 标的 | 代码 | 提及次数 | 关联主线 |")
        md.append("| --- | --- | --- | --- |")
        for n, c in top_syms:
            code = sym_code.get(n, "")
            main_ind = sym_industry[n].most_common(1)
            ind_s = main_ind[0][0] if main_ind else ""
            md.append(f"| {n} | {code} | {c} | {ind_s} |")
    else:
        md.append("- 正文未显式提及具体个股代码（$名称）")
    md.append("")
    md.append("## 二、主要表达什么观点")
    md.append("")
    md.append("### 多空立场")
    md.append("")
    md.append(f"- 看多 **{bull}** ｜ 看空 **{bear}** ｜ 中性 {stance_summary.get('中性', 0)} ｜ 风险提示 {stance_summary.get('风险提示', 0)} ｜ 复盘 {stance_summary.get('复盘', 0)}")
    md.append("")
    md.append("### 首席视角解读")
    md.append("")
    for s in (sym_lines + view_lines):
        md.append(f"- {s}")
    md.append("")
    if signals:
        md.append("### 今日金句")
        md.append("")
        for s in signals:
            md.append(f"> “{s['text']}”  ")
            md.append(f"> — [原文]({s['url']})")
            md.append("")
    md.append("## 三、最热发言 Top5")
    md.append("")
    for i, p in enumerate(top_posts, 1):
        ts = datetime.fromtimestamp(p["created_at"] / 1000, tz=TZ).strftime("%m-%d %H:%M") if p["created_at"] else ""
        md.append(f"**{i}. {ts} · {p['kind']}** ｜ 💬{p['reply']} 👍{p['like']} 🔁{p['retweet']}")
        md.append("")
        md.append(f"> {p['text']}{'…' if len(p['text']) > 180 else ''}")
        md.append("")
        md.append(f"标签：{' '.join('`' + t + '`' for t in p['tags'])}  ｜ [原文]({p['url']})")
        md.append("")
        md.append("---")
        md.append("")
    md.append(f"*生成于 {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}（北京时间） · 数据来源：雪球 metalslime 历史抓取 · 自动生成*")
    md.append("")

    text = "\n".join(md)
    open(args.out, "w", encoding="utf-8").write(text)
    arch = DATA / "daily" / f"{report_date}.md"
    os.makedirs(arch.parent, exist_ok=True)
    arch.write_text(text, encoding="utf-8")
    print(f"[daily] 窗口 {report_date} 发言 {counts['total']} 条 -> {args.out} + {arch}")
    print(f"[daily] 行业 Top3: " + ", ".join(f"{t['tag']}({t['count']})" for t in ind[:3]))
    print(f"[daily] 个股 Top5: " + ", ".join(f"{n}({sym_code.get(n, '')})" for n, _ in top_syms[:5]))
    print(f"[daily] 立场: {stance_summary}")


if __name__ == "__main__":
    main()
