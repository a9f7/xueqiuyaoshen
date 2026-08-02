#!/usr/bin/env python3
"""
雪球 user_timeline 抓取脚本（playwright 反检测版）
- 优先用环境变量 XQ_COOKIE 注入登录态，可抓 page=1..810
- 无 cookie 时只能抓 page=1 公开数据
- 按页写入 data/raw/page_{N}.json
- 限流（HTTP 405）时自动停止，等待下次再跑
"""
import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright required: pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(2)

USER_ID = 2292705444
API = "https://xueqiu.com/v4/statuses/user_timeline.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

# 从以下位置找 Chrome：playwright 默认 / 系统 Chrome / Edge
CHROME_CANDIDATES = [
    r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None  # 用 playwright 默认


def parse_cookies(cookie_str):
    cookies = []
    if not cookie_str:
        return cookies
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".xueqiu.com",
            "path": "/",
            "secure": False,
            "httpOnly": False,
            "expires": -1,
        })
    return cookies


def has_login(jar):
    return any(k in jar for k in ("xq_a_token", "xqat", "xq_id_token", "xq_is_login"))


def fetch_range(start_page, end_page, cookie_str, out_dir, request_delay=3.0):
    """抓 page=start_page..end_page（连续页）。遇到 405 限流立即停止。"""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    cookies = parse_cookies(cookie_str)
    has_login_cookie = has_login({c['name'] for c in cookies})

    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--lang=zh-CN',
            ],
            chromium_sandbox=False,
        )
        chrome_path = find_chrome()
        if chrome_path:
            launch_kwargs['executable_path'] = chrome_path

        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=UA,
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            ignore_https_errors=True,
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {}, csi: () => {}, loadTimes: () => {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        """)
        if cookies:
            ctx.add_cookies(cookies)
            print(f"[init] injected {len(cookies)} cookies (login={has_login_cookie})", file=sys.stderr, flush=True)
        else:
            print("[init] no cookies, can only fetch page=1", file=sys.stderr, flush=True)

        page = ctx.new_page()

        # 暖身
        try:
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            if has_login_cookie:
                page.goto(f"https://xueqiu.com/u/{USER_ID}", wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
        except Exception as e:
            print(f"[warmup] err: {e}", file=sys.stderr, flush=True)

        ok = 0
        rate_limited = False
        for pno in range(start_page, end_page + 1):
            api_url = f"{API}?user_id={USER_ID}&page={pno}&type=0&count=20"
            try:
                data = page.evaluate("""async (url) => {
                    const r = await fetch(url, {credentials: 'include'});
                    const t = await r.text();
                    try { return {ok: r.ok, status: r.status, json: JSON.parse(t)}; }
                    catch (e) { return {ok: r.ok, status: r.status, text: t.slice(0, 300)}; }
                }""", api_url)
                success = data.get('ok') and isinstance(data.get('json'), dict) and 'statuses' in data['json']
                if success:
                    statuses = data['json']['statuses']
                    out_file = os.path.join(out_dir, f"page_{pno}.json")
                    with open(out_file, 'w', encoding='utf-8') as f:
                        json.dump(data['json'], f, ensure_ascii=False, indent=2)
                    print(f"  page={pno}: {len(statuses)} statuses, total={data['json'].get('total')}, maxPage={data['json'].get('maxPage')}", flush=True)
                    ok += 1
                else:
                    code = data.get('status', '?')
                    print(f"  page={pno}: FAIL {code} (likely rate-limited, stopping)", file=sys.stderr, flush=True)
                    rate_limited = True
                    break
            except Exception as e:
                print(f"  page={pno}: ERR {e}", file=sys.stderr, flush=True)
                break
            time.sleep(request_delay + random.uniform(0, 1.5))

        print(f"[done] {ok}/{end_page-start_page+1} pages saved (range {start_page}..{end_page}) rate_limited={rate_limited}", file=sys.stderr, flush=True)
        browser.close()
        return ok, rate_limited


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="1-5", help="页码范围 e.g. 1-30")
    ap.add_argument("--out", default="data/raw", help="输出目录")
    ap.add_argument("--delay", type=float, default=3.0, help="请求间隔秒数")
    ap.add_argument("--cookie-env", default="XQ_COOKIE", help="cookies 环境变量名")
    ap.add_argument("--cookie-file", default=None, help="cookies 文件路径（优先于 env）")
    args = ap.parse_args()

    start, end = (int(x) for x in args.pages.split("-"))

    cookie_str = ""
    if args.cookie_file and os.path.exists(args.cookie_file):
        cookie_str = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    else:
        cookie_str = os.environ.get(args.cookie_env, "")

    ok, rl = fetch_range(start, end, cookie_str, args.out, args.delay)
    sys.exit(0 if ok > 0 or rl else 1)


if __name__ == "__main__":
    main()
