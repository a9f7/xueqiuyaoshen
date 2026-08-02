#!/usr/bin/env python3
"""雪球 user_timeline 抓取（playwright 反检测版）。

用法：
  # 抓最旧的 N 页未抓取数据（自动化增量用，默认 15 页）
  python fetch_xueqiu.py --batch 15

  # 抓指定区间
  python fetch_xueqiu.py 286 320

  # 仅抓公开 page=1（无 cookies 时）
  python fetch_xueqiu.py

cookies 来源（按优先级）：
  1. 环境变量 XQ_COOKIE
  2. 文件 data/xq_cookie.txt
  3. 无 cookies → 只抓 page=1（公开）
"""
import sys
import os
import json
import time
import random
import argparse
from playwright.sync_api import sync_playwright

# 默认 cookies 文件（不入库）
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "..", "data", "xq_cookie.txt")
OUT_DIR = os.path.join(HERE, "..", "data", "raw")
CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
USER_ID = 2292705444
BATCH_PER_BROWSER = 30  # 每个浏览器会话最多抓多少页（避免渲染器崩溃）
FALLBACK_MAXPAGE = 810


def load_cookie_str():
    if os.environ.get("XQ_COOKIE"):
        return os.environ["XQ_COOKIE"].strip()
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, encoding="utf-8") as f:
            s = f.read().strip()
            if s:
                return s
    return ""


def parse_cookies(cookie_str):
    cookies = []
    for part in cookie_str.split(';'):
        part = part.strip()
        if '=' not in part:
            continue
        name, _, value = part.partition('=')
        cookies.append({
            'name': name.strip(),
            'value': value.strip(),
            'domain': '.xueqiu.com',
            'path': '/',
            'secure': False,
            'httpOnly': False,
            'expires': -1,
        })
    return cookies


def existing_pages():
    if not os.path.isdir(OUT_DIR):
        return set()
    s = set()
    for fn in os.listdir(OUT_DIR):
        if fn.startswith("page_") and fn.endswith(".json"):
            try:
                s.add(int(fn[5:-5]))
            except ValueError:
                pass
    return s


def fetch_batch(start, end, cookie_str):
    """启动一个全新浏览器会话，抓 [start,end] 中尚未存在的页。返回 (saved, stopped)。"""
    saved = 0
    stopped = False
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-infobars',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-setuid-sandbox',
                '--lang=zh-CN',
            ],
            chromium_sandbox=False,
        )
        ctx = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            ignore_https_errors=True,
            extra_http_headers={'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'},
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {}, csi: () => {}, loadTimes: () => {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            const _toString = Function.prototype.toString;
            Function.prototype.toString = function() {
                if (this === Function.prototype.toString) return _toString.call(_toString);
                return _toString.call(this);
            };
        """)
        if cookie_str:
            ctx.add_cookies(parse_cookies(cookie_str))
        page = ctx.new_page()

        # 暖身
        try:
            page.goto("https://www.baidu.com/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)
        except Exception:
            pass
        try:
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
        except Exception as e:
            print(f"  [warn] warmup homepage: {e}", flush=True)
        try:
            page.goto(f"https://xueqiu.com/u/{USER_ID}", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
        except Exception as e:
            print(f"  [warn] warmup profile: {e}", flush=True)

        for pno in range(start, end + 1):
            out_file = os.path.join(OUT_DIR, f"page_{pno}.json")
            if os.path.exists(out_file):
                continue
            api_url = f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={USER_ID}&page={pno}&type=0&count=20"
            try:
                data = page.evaluate("""async (url) => {
                    const ctrl = new AbortController();
                    const timer = setTimeout(() => ctrl.abort(), 15000);
                    try {
                        const r = await fetch(url, {credentials: 'include', signal: ctrl.signal});
                        const t = await r.text();
                        clearTimeout(timer);
                        try { return {ok: r.ok, status: r.status, json: JSON.parse(t)}; }
                        catch (e) { return {ok: r.ok, status: r.status, text: t.slice(0, 500)}; }
                    } catch (e) {
                        clearTimeout(timer);
                        return {ok: false, status: 0, text: 'FETCH_ERR: ' + e.message};
                    }
                }""", api_url)
                success = data.get('ok') and isinstance(data.get('json'), dict) and 'statuses' in data['json']
                if success:
                    with open(out_file, 'w', encoding='utf-8') as f:
                        json.dump(data['json'], f, ensure_ascii=False, indent=2)
                    print(f"  page={pno}: {len(data['json']['statuses'])} statuses, total={data['json'].get('total')}, maxPage={data['json'].get('maxPage')}", flush=True)
                    saved += 1
                elif data.get('status') == 405:
                    print(f"  [405 LIMIT] page={pno} rate limit. Stopping batch.", flush=True)
                    stopped = True
                    break
                else:
                    body = str(data.get('text', ''))[:80]
                    print(f"  page={pno}: FAIL status={data.get('status')} {body}", flush=True)
                    # 访问验证 / WAF 挑战：停止本批，避免浪费
                    if '访问验证' in body or 'aliyun_waf' in body:
                        stopped = True
                        break
            except Exception as e:
                print(f"  page={pno}: ERR {e}", flush=True)
                break  # 渲染器崩溃，本批终止（下批重开浏览器）
            time.sleep(random.uniform(1.5, 3))

        try:
            browser.close()
        except Exception:
            pass
    return saved, stopped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int, nargs="?", default=None)
    ap.add_argument("end", type=int, nargs="?", default=None)
    ap.add_argument("--batch", type=int, default=15, help="自动模式下抓最旧的 N 页")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    cookie_str = load_cookie_str()

    # 决定区间
    if args.start is not None:
        start, end = args.start, (args.end if args.end is not None else args.start)
    else:
        # 自动模式：找最旧的未抓取页
        have = existing_pages()
        # 从 page=1 往后找第一个缺口
        start = 1
        while start in have:
            start += 1
        end = start + args.batch - 1
        # 不知道 maxPage 时给个上限保护
        end = min(end, FALLBACK_MAXPAGE)
        print(f"[auto] oldest missing = page {start}, fetching {start}..{end}", flush=True)

    if not cookie_str:
        # 无 cookies：只抓公开 page=1
        if start != 1:
            print("[info] 无 cookies，仅抓公开 page=1", flush=True)
            start, end = 1, 1
        else:
            end = 1

    print(f"[run] cookies={'YES' if cookie_str else 'NO'} range={start}..{end}", flush=True)
    total_saved = 0
    lo = start
    while lo <= end:
        hi = min(lo + BATCH_PER_BROWSER - 1, end)
        # 整段已抓则跳过
        if all(os.path.exists(os.path.join(OUT_DIR, f"page_{p}.json")) for p in range(lo, hi + 1)):
            lo = hi + 1
            continue
        print(f"[batch] {lo}..{hi}", flush=True)
        saved, stopped = fetch_batch(lo, hi, cookie_str)
        total_saved += saved
        print(f"[batch done] saved={saved} cumulative={total_saved}", flush=True)
        if stopped:
            print("[STOP] WAF 限流/验证，停止。", flush=True)
            break
        lo = hi + 1
        time.sleep(random.uniform(5, 10))
    print(f"[ALL DONE] saved this run: {total_saved}", flush=True)


if __name__ == "__main__":
    main()
