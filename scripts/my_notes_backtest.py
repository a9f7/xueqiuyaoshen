#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测「我的投资笔记」（data/my_notes.json）。

完全复用 backtest_engine 的 run_backtest：把每条笔记转成与 build_events() 同结构的事件
（date/code/stance/sector/duration/text/url），跑同一套 β剥离 + 匹配久期 + 命中率/IC，
并额外附上每条笔记的逐窗口结果明细（notes），写 data/my_notes_backtest.json。

笔记字段（data/my_notes.json，JSON 数组）：
  id       可选，稳定标识（网页导入/导出用于前端匹配）；缺省用 date+code+stance 派生
  date     观点日期 YYYY-MM-DD（必填）
  code     雪球式代码 SH600519 / SZ000858 / HK00700（必填）
  stance   "看多" / "看空"（必填）
  duration 可选："短期"/"中长期"/"超长期"/"未明确"；缺省按 text 自动判定
  text     笔记正文（必填）
  url      可选原文链接

用法：python scripts/my_notes_backtest.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
sys.path.insert(0, str(HERE))
import backtest_engine as bt
import market_data as md


def _mk_id(note):
    return note.get("id") or f"{note.get('date', '')}|{note.get('code', '')}|{note.get('stance', '')}"


def build_note_events(notes):
    """笔记 -> 回测事件列表；无法回测的笔记 id 计入 skipped。"""
    events, skipped = [], []
    for note in notes:
        code = note.get("code")
        if not code or not md.xq_to_secid(code):
            skipped.append(_mk_id(note))
            continue
        stance = note.get("stance")
        s = 1 if stance == "看多" else (-1 if stance == "看空" else None)
        if s is None:
            skipped.append(_mk_id(note))
            continue
        dt = bt.parse_created_at(note.get("date"))
        if dt is None:
            skipped.append(_mk_id(note))
            continue
        text = note.get("text") or ""
        dur = note.get("duration") or bt.classify_duration(text)
        events.append({
            "date": dt,
            "code": code,
            "stance": s,
            "sector": note.get("sector") or None,
            "duration": dur,
            "url": note.get("url", ""),
            "text": text[:120],
            "id": _mk_id(note),
            "raw_text": text,
        })
    return events, skipped


def main():
    path = DATA / "my_notes.json"
    if not path.exists():
        out = bt._empty_out("尚未添加投资笔记：在网页「我的投资笔记」面板填写，或创建 data/my_notes.json。")
        out["notes"] = []
        out["note_skipped"] = 0
        json.dump(out, open(DATA / "my_notes_backtest.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("[my_notes] 无 my_notes.json，已写空结果。", flush=True)
        return

    notes = json.load(open(path, encoding="utf-8"))
    if not isinstance(notes, list):
        notes = []
    events, skipped = build_note_events(notes)
    print(f"[my_notes] 笔记 {len(notes)} 条，可回测 {len(events)} 条，跳过 {len(skipped)}", flush=True)

    out = bt.run_backtest(events)

    # 附每条笔记的逐窗口结果明细（复用 compute_event，行情已缓存所以很便宜）
    bench = md.get_kline(bt.PRIMARY_BENCH)
    bench_series = [(r[0], r[2]) for r in bench["rows"]] if bench else None
    det_by_id = {}
    for ev in events:
        res = bt.compute_event(ev, bench_series)
        detail = {}
        for k in bt.HORIZONS:
            if not res or k not in res:
                detail[str(k)] = None
                continue
            ret = res[k]["stock_ret"]
            sign = bt._sign(ret)
            detail[str(k)] = {"ret": round(ret, 4), "sign": sign,
                              "hit": (sign == ev["stance"]), "closed": True}
        closed = max((k for k in (res or {}) if res[k] is not None), default=None) if res else None
        det_by_id[ev["id"]] = {
            "date": ev["date"].strftime("%Y-%m-%d"),
            "code": ev["code"],
            "stance": "看多" if ev["stance"] == 1 else "看空",
            "duration": ev["duration"],
            "text": ev["raw_text"],
            "url": ev["url"],
            "closed_horizon": closed,
            "results": detail,
        }

    # 保持与笔记原文顺序一致；被跳过/缺行情的笔记也保留占位
    notes_detail = []
    for note in notes:
        nid = _mk_id(note)
        if nid in det_by_id:
            notes_detail.append(det_by_id[nid])
        else:
            notes_detail.append({
                "date": note.get("date", ""), "code": note.get("code", ""),
                "stance": note.get("stance", ""), "duration": note.get("duration") or "",
                "text": note.get("text", ""), "url": note.get("url", ""),
                "closed_horizon": None, "results": None, "skipped": True,
            })
    out["notes"] = notes_detail
    out["note_skipped"] = len(skipped)
    json.dump(out, open(DATA / "my_notes_backtest.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[my_notes] 完成：回测 {out['overall']['n_events']} 条 -> data/my_notes_backtest.json", flush=True)


if __name__ == "__main__":
    main()
