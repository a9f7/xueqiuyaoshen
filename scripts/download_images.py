#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
下载带图发言的图片到本地 data/images/，供前端展示 & GitHub Pages 托管。

- 数据源：data/posts.json 的 pic 字段（逗号分隔的图片 URL 字符串）
- 命名：取 URL 唯一段（如 19fb809adabbe91f3fc1d7fa.png）作为文件名，去 !thumb 后缀取原图
- 并发下载，跳过已存在文件，失败自动重试
- 幂等：可反复运行（回填存量 + 增量新图），不会重复下载

用法：
  python download_images.py                 # 下载 posts.json 中所有图片
  python download_images.py --workers 8     # 自定义并发数
  python download_images.py --limit 50      # 仅下载前 50 个新图（调试）
"""
import json
import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IMG_DIR = DATA / "images"
POSTS = DATA / "posts.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
REFERER = "https://xueqiu.com/"
TIMEOUT = 30
MAX_BYTES = 1.5 * 1024 * 1024  # 原图超过 1.5MB 则降级到 !large / !thumb


def url_to_base(u):
    """去掉 !thumb / !large 等变换后缀，得到原图基址。"""
    return u.split('!')[0].rstrip('/')


def url_to_seg(u):
    """从 URL 提取唯一文件名段。"""
    base = url_to_base(u)
    seg = base.rstrip('/').split('/')[-1]
    return seg


def fetch_bytes(url):
    req = Request(url, headers={"User-Agent": UA, "Referer": REFERER, "Accept": "image/*,*/*"})
    with urlopen(req, timeout=TIMEOUT) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read()
        return data, ctype


def try_download(url):
    """尝试下载原图，过大则降级。返回 (data, ext) 或 None。"""
    base = url_to_base(url)
    candidates = [base, base + "!large.jpg", base + "!thumb.jpg"]
    last = None
    for c in candidates:
        try:
            data, ctype = fetch_bytes(c)
        except (URLError, HTTPError, Exception) as e:
            last = e
            continue
        if not data or len(data) < 200:
            last = "empty"
            continue
        if "image" not in ctype and not data[:4] in (b"\xff\xd8\xff\xe0", b"\x89PNG", b"GIF8", b"WEBP"):
            last = "not image"
            continue
        # 原图过大 → 尝试降级版本
        if c == base and len(data) > MAX_BYTES and "!large" not in base and "!thumb" not in base:
            # 记下原图，继续试 large/thumb
            last = ("big", data)
            continue
        ext = "jpg"
        if "png" in ctype:
            ext = "png"
        elif "gif" in ctype:
            ext = "gif"
        elif "webp" in ctype:
            ext = "webp"
        else:
            # 从文件名推断
            seg = url_to_seg(url)
            if "." in seg:
                ext = seg.rsplit(".", 1)[-1].lower()[:4]
        return data, ext
    # 原图过大，返回原图本身
    if isinstance(last, tuple) and last[0] == "big":
        data = last[1]
        seg = url_to_seg(url)
        ext = seg.rsplit(".", 1)[-1].lower()[:4] if "." in seg else "jpg"
        return data, ext
    return None


def collect_urls():
    """从 posts.json 收集所有图片 URL（去重）。"""
    posts = json.load(open(POSTS, encoding="utf-8"))
    urls = []
    seen = set()
    for p in posts:
        pic = p.get("pic") or ""
        if not pic:
            continue
        for u in pic.split(","):
            u = u.strip()
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="仅下载前 N 个新图（调试）")
    args = ap.parse_args()

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    urls = collect_urls()
    print(f"[scan] posts.json 中共有 {len(urls)} 个图片 URL（去重后）", flush=True)

    # 过滤：仅下载本地不存在的
    todo = []
    for u in urls:
        seg = url_to_seg(u)
        fp = IMG_DIR / seg
        if fp.exists() and fp.stat().st_size > 200:
            continue
        todo.append(u)
    print(f"[filter] 待下载: {len(todo)} | 已存在跳过: {len(urls) - len(todo)}", flush=True)

    if args.limit:
        todo = todo[: args.limit]

    ok = 0
    fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        fut_map = {ex.submit(try_download, u): u for u in todo}
        done = 0
        for fut in as_completed(fut_map):
            u = fut_map[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as e:
                res = None
                fail += 1
                if done % 50 == 0 or fail <= 5:
                    print(f"  [fail] {u[:60]} {e}", flush=True)
                continue
            if res is None:
                fail += 1
                if fail <= 10:
                    print(f"  [fail] {u[:60]} 下载失败", flush=True)
                continue
            data, ext = res
            seg = url_to_seg(u)
            if "." not in seg:
                seg = f"{seg}.{ext}"
            fp = IMG_DIR / seg
            fp.write_bytes(data)
            ok += 1
            if done % 50 == 0:
                print(f"  ... {done}/{len(todo)} | ok={ok} fail={fail} | {int(time.time()-t0)}s", flush=True)
    print(f"[DONE] 下载成功 {ok} | 失败 {fail} | 用时 {int(time.time()-t0)}s", flush=True)
    # 统计目录大小
    total = sum(f.stat().st_size for f in IMG_DIR.glob('*') if f.is_file())
    print(f"[info] data/images 共 {len(list(IMG_DIR.glob('*')))} 个文件，{round(total/1024/1024,1)} MB", flush=True)


if __name__ == "__main__":
    main()
