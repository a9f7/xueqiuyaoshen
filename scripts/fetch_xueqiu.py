#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
雪球 user_timeline 抓取脚本
- 从环境变量 XQ_COOKIE 读取 cookie 串（无 cookie 时只能拿 page=1 公开数据）
- 按页写入 data/raw/page_{N}.json
- 自动重试 + UA 伪装
"""
import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests required: pip install requests", file=sys.stderr)
    sys.exit(2)

USER_ID = 2292705444
API = "https://xueqiu.com/v4/statuses/user_timeline.json"
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

def parse_cookie(s):
    jar = {}
    if not s:
        return jar
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar

def has_login_cookie(jar):
    return any(k in jar for k in ("xq_a_token", "xqat", "xq_id_token", "xq_is_login"))

def fetch_page(session, page, count=20):
    params = {
        "user_id": USER_ID,
        "page": page,
        "type": 0,
        "count": count,
    }
    headers = {
        "User-Agent": random.choice(UA_LIST),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"https://xueqiu.com/u/{USER_ID}",
        "X-Requested-With": "XMLHttpRequest",
    }
    r = session.get(API, params=params, headers=headers, timeout=15)
    if r.status_code != 200:
        return None, f"http {r.status_code}"
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype.lower():
        return None, f"non-json (WAF?): {ctype}; first 200 bytes: {r.text[:200]!r}"
    try:
        data = r.json()
    except Exception as e:
        return None, f"json parse error: {e}"
    if isinstance(data, dict) and data.get("error_code"):
        return None, f"xueqiu error {data.get('error_code')}: {data.get('error_description')}"
    if not isinstance(data, dict) or "statuses" not in data:
        return None, f"unexpected payload: {str(data)[:200]}"
    return data, None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="5", help="抓取页数（逗号分隔或 N）")
    ap.add_argument("--out", default="data/raw", help="输出目录")
    ap.add_argument("--start", type=int, default=1, help="起始页（默认 1）")
    ap.add_argument("--sleep-min", type=float, default=1.5)
    ap.add_argument("--sleep-max", type=float, default=4.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if "," in str(args.pages):
        page_list = [int(x) for x in args.pages.split(",") if x.strip()]
    else:
        end = args.start + int(args.pages) - 1
        page_list = list(range(args.start, end + 1))

    cookie_str = os.environ.get("XQ_COOKIE", "")
    jar = parse_cookie(cookie_str)
    session = requests.Session()
    if jar:
        for k, v in jar.items():
            session.cookies.set(k, v, domain="xueqiu.com")
        print(f"[cookie] loaded {len(jar)} cookies; login={has_login_cookie(jar)}", file=sys.stderr)
    else:
        print("[cookie] no XQ_COOKIE env, only page 1 may work", file=sys.stderr)

    success = 0
    fail = []
    for p in page_list:
        data, err = fetch_page(session, p)
        if data is None:
            print(f"[page {p}] FAILED: {err}", file=sys.stderr)
            fail.append((p, err))
            if "请登录" in (err or "") and not has_login_cookie(jar):
                print("[abort] need login cookies to continue past page 1", file=sys.stderr)
                break
        else:
            n = len(data.get("statuses", []))
            fp = out / f"page_{p}.json"
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[page {p}] saved {n} posts → {fp}", file=sys.stderr)
            success += 1
        if p != page_list[-1]:
            time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    print(f"[done] success={success}/{len(page_list)}; failed={fail}", file=sys.stderr)
    if success == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
