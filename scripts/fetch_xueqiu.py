#!/usr/bin/env python3
"""雪球 user_timeline 抓取（playwright 反检测版，支持多 cookie 轮换）。

支持三种抓取模式（--mode）：
  posts    原贴（user_timeline type=0，含原创+长文）—— 默认
  comments 评论（user_timeline type=3，每条内嵌被评论原文 retweeted_status）
  reposts  转发（user_timeline type=1，内嵌被转原文 retweeted_status）

用法：
  # 自动抓最旧的 N 页（按 --mode 决定 raw 目录）
  python fetch_xueqiu.py --mode comments --batch 15

  # 抓指定区间
  python fetch_xueqiu.py --mode comments 286 320

  # 仅抓公开 page=1（无 cookies 时）
  python fetch_xueqiu.py --mode posts

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
import math
from playwright.sync_api import sync_playwright

# 默认 cookies 文件（不入库）
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
COOKIE_POOL_FILE = os.path.join(ROOT, "data", "xq_cookies.txt")
COOKIE_FILE = os.path.join(ROOT, "data", "xq_cookie.txt")
CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
USER_ID = 2292705444
BATCH_PER_BROWSER = 30  # 每个浏览器会话最多抓多少页（避免渲染器崩溃）
FALLBACK_MAXPAGE = {"posts": 811, "comments": 1600, "reposts": 60}
COOLDOWN_SEC = 1800  # 触发风控后该 cookie 冷却 30 分钟

# 模式 -> (API type, raw 子目录)
MODES = {
    "posts": (0, "raw"),
    "comments": (3, "raw_comments"),
    "reposts": (1, "raw_reposts"),
}


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


def existing_pages(out_dir):
    if not os.path.isdir(out_dir):
        return set()
    s = set()
    for fn in os.listdir(out_dir):
        if fn.startswith("page_") and fn.endswith(".json"):
            try:
                s.add(int(fn[5:-5]))
            except ValueError:
                pass
    return s


def fetch_batch(start, end, cookie_str, api_type, out_dir, force=False):
    """启动一个全新浏览器会话，抓 [start,end]。

    force=True 时覆盖已存在的页（用于 --newest 追最新发帖：新内容总在第 1 页，
    必须重写才能抓到，否则会被 os.path.exists 跳过而导致最新发帖永远漏抓）。
    返回 (saved, stopped, maxpage)。"""
    saved = 0
    stopped = False
    maxpage = None
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

        # 暖身（简化：仅首页）
        try:
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
        except Exception as e:
            print(f"  [warn] warmup homepage: {e}", flush=True)

        for pno in range(start, end + 1):
            out_file = os.path.join(out_dir, f"page_{pno}.json")
            if not force and os.path.exists(out_file):
                continue
            api_url = f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={USER_ID}&page={pno}&type={api_type}&count=20"
            try:
                data = page.evaluate("""async (url) => {
                    const ctrl = new AbortController();
                    const timer = setTimeout(() => ctrl.abort(), 15000);
                    try {
                        const r = await fetch(url, {credentials: 'include', signal: ctrl.signal});
                        const t = await r.text();
                        clearTimeout(timer);
                        return {ok: r.ok, status: r.status, text: t};
                    } catch (e) {
                        clearTimeout(timer);
                        return {ok: false, status: 0, text: 'FETCH_ERR: ' + e.message};
                    }
                }""", api_url)
                ok = data.get('ok')
                status = data.get('status')
                text = data.get('text', '') or ''
                if ok and status == 200 and text.startswith('{'):
                    try:
                        j = json.loads(text)
                    except Exception:
                        j = None
                    if isinstance(j, dict) and 'statuses' in j:
                        with open(out_file, 'w', encoding='utf-8') as f:
                            json.dump(j, f, ensure_ascii=False, indent=2)
                        total = j.get('total')
                        if total:
                            maxpage = max(maxpage or 0, math.ceil(total / 20))
                        print(f"  page={pno}: {len(j['statuses'])} statuses, total={total}, maxPage={maxpage}", flush=True)
                        saved += 1
                    else:
                        print(f"  page={pno}: JSON 无 statuses (status={status})", flush=True)
                        if '访问验证' in text or 'aliyun_waf' in text:
                            stopped = True
                            break
                elif status == 405:
                    print(f"  [405 LIMIT] page={pno} rate limit. cookie cooldown.", flush=True)
                    stopped = True
                    break
                else:
                    print(f"  page={pno}: FAIL status={status} {text[:80]}", flush=True)
                    # status==0 多为 WAF 拦截导致 fetch 超时/报错；与 405/访问验证 同样按限流处理，
                    # 立即停止本批（否则会逐页 15s 超时重试，单批卡 5+ 分钟）。
                    if status == 0 or '访问验证' in text or 'aliyun_waf' in text:
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
    return saved, stopped, maxpage


def acquire_lock():
    """单实例锁：防止自动化并发启动多个 fetch（曾导致多进程抢 cookie 池 / WAF 预算、
    写同一 raw 目录、账号被风控、渲染器崩溃、进程静默死退出）。
    用法：main 开头调用；退出前 release_lock。若锁文件中的 PID 仍存活则直接退出。"""
    LOCK = os.path.join(ROOT, "data", ".fetch.lock")
    if os.path.exists(LOCK):
        try:
            with open(LOCK, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # 进程存活则抛异常，否则 OSError
            print(f"[LOCK] 已有 fetch 进程 PID {pid} 在运行，本实例退出。", flush=True)
            sys.exit(0)
        except (OSError, ValueError):
            pass  # 过期锁，覆盖
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return LOCK


def release_lock(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        pass


def main():
    lock_path = acquire_lock()
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int, nargs="?", default=None)
    ap.add_argument("end", type=int, nargs="?", default=None)
    ap.add_argument("--mode", choices=list(MODES.keys()), default="posts", help="posts/comments/reposts")
    ap.add_argument("--batch", type=int, default=15, help="自动模式下抓最旧的 N 页")
    ap.add_argument("--newest", type=int, default=None,
                    help="抓取并【覆盖重写】第 1..N 页（追最新发帖/评论，必须覆盖才会更新第 1 页）")
    args = ap.parse_args()

    api_type, subdir = MODES[args.mode]
    OUT_DIR = os.path.join(ROOT, "data", subdir)
    os.makedirs(OUT_DIR, exist_ok=True)

    pool = load_cookie_pool()
    if not pool:
        print("[warn] 无 cookies，仅抓公开 page=1", flush=True)
    print(f"[run] mode={args.mode} api_type={api_type} out={OUT_DIR} cookie pool 共 {len(pool)} 组", flush=True)

    # 决定区间
    if args.newest is not None:
        # 追最新：强制覆盖第 1..N 页
        start, end = 1, max(1, args.newest)
        force_overwrite = True
        print(f"[newest] 覆盖重写 page 1..{end}", flush=True)
    elif args.start is not None:
        start, end = args.start, (args.end if args.end is not None else args.start)
        force_overwrite = False
    else:
        have = existing_pages(OUT_DIR)
        start = 1
        while start in have:
            start += 1
        end = start + args.batch - 1
        end = min(end, FALLBACK_MAXPAGE[args.mode])
        force_overwrite = False
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
        if not force_overwrite:
            while lo <= end and os.path.exists(os.path.join(OUT_DIR, f"page_{lo}.json")):
                lo += 1
            if lo > end:
                break
        hi = min(lo + BATCH_PER_BROWSER - 1, end)

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
        saved, stopped, maxpage = fetch_batch(lo, hi, pool[chosen], api_type, OUT_DIR, force=force_overwrite)
        total_saved += saved
        print(f"[batch done] saved={saved} cumulative={total_saved}", flush=True)
        if stopped:
            cooldown[chosen] = time.time() + COOLDOWN_SEC
            while lo <= hi and os.path.exists(os.path.join(OUT_DIR, f"page_{lo}.json")):
                lo += 1
            if all(cooldown.get(i, 0) > time.time() for i in range(len(pool))):
                print("[STOP] 全部 cookies 冷却，停止。", flush=True)
                break
            continue
        lo = hi + 1
        time.sleep(random.uniform(5, 10))
    print(f"[ALL DONE] mode={args.mode} saved this run: {total_saved}", flush=True)
    release_lock(lock_path)


if __name__ == "__main__":
    main()
