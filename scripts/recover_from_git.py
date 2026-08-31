#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从部署仓库 git 历史抢救丢失的帖文/评论（修补时间线空洞）。

背景：每小时任务用 fetch --newest 8 覆盖重写 raw 第 1~8 页；被新内容挤出
第 8 页的旧记录会脱离 raw 覆盖范围（第 9 页起的文件是更早时点的快照），
而 normalize 早期是「按 raw 全量重写」，于是 posts.json / comments.json 会
出现中段时间线空洞。normalize_posts / normalize_interactions 已改为
append-only 并集，可防止将来再丢；本脚本负责把已经丢掉的记录从 git 历史
（历史提交里的 data/posts.json、data/comments.json）里找回来。

用法：
  python scripts/recover_from_git.py                 # 试运行，只报告
  python scripts/recover_from_git.py --apply         # 实际写回
  python scripts/recover_from_git.py --apply --per-day 3 --since 2026-07-01
"""
import argparse
import datetime
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_REPO = Path.home() / "AppData" / "Local" / "Temp" / "xq_deploy"

TARGETS = [
    ("data/posts.json", DATA / "posts.json"),
    ("data/comments.json", DATA / "comments.json"),
]


def git(repo, *args, binary=False):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def pick_commits(repo, path, since, per_day):
    """按天挑提交：每天取最新 + 最旧（per_day>=2 时），避免逐个提交全扫。"""
    out = git(repo, "log", "--pretty=%H %ad", "--date=short",
              f"--since={since}", "--", path)
    by_day = defaultdict(list)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            by_day[parts[1]].append(parts[0])
    picked = []
    for day in sorted(by_day):
        shas = by_day[day]          # git log 默认新->旧
        take = [shas[0]]
        if per_day >= 2 and len(shas) > 1:
            take.append(shas[-1])
        if per_day >= 3 and len(shas) > 2:
            take.append(shas[len(shas) // 2])
        picked.extend((day, s) for s in take)
    return picked


def load_json_at(repo, sha, path):
    raw = git(repo, "show", f"{sha}:{path}", binary=True)
    return json.loads(raw.decode("utf-8", "replace"), strict=False)


def day_of(rec):
    ts = rec.get("created_at") or 0
    if not ts:
        return "?"
    return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--per-day", type=int, default=2)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    if not (repo / ".git").exists():
        print(f"[fatal] 部署仓库不存在: {repo}", file=sys.stderr)
        sys.exit(1)

    for rel, local_path in TARGETS:
        if not local_path.exists():
            print(f"[skip] 本地缺少 {local_path.name}")
            continue
        cur = json.loads(open(local_path, encoding="utf-8", errors="replace").read(), strict=False)
        have = {r.get("id") for r in cur if r.get("id")}
        print(f"\n=== {rel} | 本地 {len(cur)} 条 ===")

        commits = pick_commits(repo, rel, args.since, args.per_day)
        print(f"[scan] 候选提交 {len(commits)} 个（{args.since} 起，每天取 {args.per_day} 个）")

        found = {}
        for i, (day, sha) in enumerate(commits, 1):
            try:
                recs = load_json_at(repo, sha, rel)
            except Exception as e:
                print(f"  [warn] {day} {sha[:8]}: {e}")
                continue
            new = 0
            for r in recs:
                rid = r.get("id")
                if rid and rid not in have and rid not in found:
                    found[rid] = r
                    new += 1
            if new:
                print(f"  {day} {sha[:8]}: +{new}（累计 {len(found)}）")
            elif i % 10 == 0:
                print(f"  ...已扫 {i}/{len(commits)}")

        if not found:
            print("[ok] 无缺失记录，时间线完整")
            continue

        by_day = defaultdict(int)
        for r in found.values():
            by_day[day_of(r)] += 1
        print(f"[found] 可抢救 {len(found)} 条，按日分布：")
        print("  " + " ".join(f"{d}:{n}" for d, n in sorted(by_day.items())))

        if not args.apply:
            print("[dry-run] 未写回；加 --apply 生效")
            continue

        merged = {r["id"]: r for r in cur if r.get("id")}
        merged.update({k: v for k, v in found.items() if k not in merged})
        out = sorted(merged.values(), key=lambda r: r.get("created_at") or 0, reverse=True)
        bak = local_path.with_suffix(".json.bak")
        bak.write_bytes(local_path.read_bytes())
        json.dump(out, open(local_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[apply] {local_path.name}: {len(cur)} -> {len(out)} 条（备份 {bak.name}）")

    # 重建 interactions.json = comments + reposts
    if args.apply:
        cj, rj = DATA / "comments.json", DATA / "reposts.json"
        if cj.exists():
            comments = json.loads(open(cj, encoding="utf-8", errors="replace").read(), strict=False)
            reposts = json.loads(open(rj, encoding="utf-8", errors="replace").read(), strict=False) if rj.exists() else []
            json.dump(comments + reposts, open(DATA / "interactions.json", "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            print(f"[apply] interactions.json 重建：{len(comments)} 评论 + {len(reposts)} 转发")


if __name__ == "__main__":
    main()
