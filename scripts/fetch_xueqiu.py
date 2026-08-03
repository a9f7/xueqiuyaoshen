#!/usr/bin/env python3
"""雪球 user_timeline 抓取（playwright 反检测版，支持多 cookie 轮换）。

用法：
  # 抓最旧的 N 页未抓取数据（自动化增量用，默认 15 页）
  python fetch_xueqiu.py --batch 15

  # 抓指定区间
  python fetch_xueqiu.py 286 320

  # 仅抓公开 page=1（无 cookies 时）
  python fetch_xueqiu.py

cookies 来源（按优先级）：
  1. 环境变量 XQ_COOKIE（单条）
  2. 文件 data/xq_cookies.txt（多行，每行一组 cookie，轮换使用）
  3. 文件 data/xq_cookie.txt（单条，兼容旧版）
  4. 无 cookies → 只抓 page=1（公开）

轮换策略：
  - 每个浏览器会话（≤30 页）轮流使用池里下一个 cookie
  - 某 cookie 触发 405/访问验证 → 进入冷却（默认 30 分钟），切换下一个
  - 所有 cookie 都冷却 → 停止，避免加剧风控
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
COOKIE_POOL_FILE = os.path.join(HERE, "..", "data", "xq_cookies.txt")
COOKIE_FILE = os.path.join(HERE, "..", "data", "xq_cookie.txt")
OUT_DIR = os.path.join(HERE, "..", "data", "raw")
CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
USER_ID = 2292705444
BATCH_PER_BROWSER = 30  # 每个浏览器会话最多抓多少页（避免渲染器崩溃）
FALLBACK_MAXPAGE = 810
COOLDOWN_SEC = 1800  # 触发风控后该 cookie 冷却 30 分钟


def load_cookie_pool():
    """返回 cookie 字符串列表（去重、去空）。"""
    pool = []
    if os.environ.get("XQ_COOKIE"):
        pool.append(os.environ["XQ_COOKIE"].strip())
    if os.path.exists(COOKIE_POOL_FILE):
        with open(COOKIE_POOL_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    pool.append(s)
    if not pool and os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, encoding="utf-8") as f:
            s = f.read().strip()
            if s:
                pool.append(s)
    # 去重保持顺序
    seen = set()
    uniq = []
    for s in pool:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


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
                    print(f"  [405 LIMIT] page={pno} rate limit. cookie cooldown.", flush=True)
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
    pool = load_cookie_pool()
    if not pool:
        print("[warn] 无 cookies，仅抓公开 page=1", flush=True)
    print(f"[run] cookie pool 共 {len(pool)} 组", flush=True)

    # 决定区间
    if args.start is not None:
        start, end = args.start, (args.end if args.end is not None else args.start)
    else:
        have = existing_pages()
        start = 1
        while start in have:
            start += 1
        end = start + args.batch - 1
        end = min(end, FALLBACK_MAXPAGE)
        print(f"[auto] oldest missing = page {start}, fetching {start}..{end}", flush=True)

    if not pool:
        if start != 1:
            print("[info] 无 cookies，仅抓公开 page=1", flush=True)
            start, end = 1, 1
        else:
            end = 1

    cooldown = {}  # idx -> 冷却截止时间戳
    idx = 0
    total_saved = 0
    lo = start
    while lo <= end:
        # 跳过已抓
        while lo <= end and os.path.exists(os.path.join(OUT_DIR, f"page_{lo}.json")):
            lo += 1
        if lo > end:
            break
        hi = min(lo + BATCH_PER_BROWSER - 1, end)

        # 选一个可用 cookie（轮转 + 跳过冷却）
        chosen = None
        for _ in range(len(pool)):
            i = idx % len(pool)
            if cooldown.get(i, 0) <= time.time():
                chosen = i
                idx = i + 1
                break
            idx += 1
        if chosen is None:
            print("[STOP] 所有 cookies 均冷却中，停止。", flush=True)
            break

        print(f"[batch] pages {lo}..{hi} | cookie#{chosen} ({'有' if pool else '无'})", flush=True)
        saved, stopped = fetch_batch(lo, hi, pool[chosen])
        total_saved += saved
        print(f"[batch done] saved={saved} cumulative={total_saved}", flush=True)
        if stopped:
            cooldown[chosen] = time.time() + COOLDOWN_SEC
            # 重新定位 lo 到本批第一个未抓页，用下一个 cookie 重试
            while lo <= hi and os.path.exists(os.path.join(OUT_DIR, f"page_{lo}.json")):
                lo += 1
            if all(cooldown.get(i, 0) > time.time() for i in range(len(pool))):
                print("[STOP] 全部 cookies 冷却，停止。", flush=True)
                break
            continue
        lo = hi + 1
        time.sleep(random.uniform(5, 10))
    print(f"[ALL DONE] saved this run: {total_saved}", flush=True)


if __name__ == "__main__":
    main()
