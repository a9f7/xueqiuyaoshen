#!/usr/bin/env python3
"""抓取 metalslime 真实自选股/基金列表（stock.xueqiu.com/v5，公开可见）。

metalslime 主页 xueqiu.com/u/2292705444 显示「自选 121 / 组合 11」。
真实可选股接口（前端实际调用）：
  stock.xueqiu.com/v5/stock/portfolio/stock/list.json?pid=-1&category=1&uid=2292705444   主自选(全部股票)
  stock.xueqiu.com/v5/stock/portfolio/stock/list.json?pid=<X>&category=3&uid=...           其他股票分组
  stock.xueqiu.com/v5/stock/portfolio/stock/watchlist.json?pid=<Y>&uid=...                 基金/私慕分组(category=2)

输出：data/selfstock_raw.json —— 当前真实快照（去重后每只：symbol/name/marketplace/watched/pid/category）
用法：
  python fetch_selfstock.py
"""
import os, sys, json, time, math
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
COOKIE_POOL_FILE = os.path.join(ROOT, "data", "xq_cookies.txt")
COOKIE_FILE = os.path.join(ROOT, "data", "xq_cookie.txt")
RAW_OUT = os.path.join(ROOT, "data", "selfstock_raw.json")
CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
USER_ID = 2292705444

# 需要抓取的分组（覆盖已观测到的全部 pid；缺失分组返回空不影响）
GROUPS = [
    ("list", -1, 1),     # 主自选（全部股票）
    ("list", -120, 3),
    ("list", -24, 3),
    ("watchlist", -17, None),
    ("watchlist", -130, None),
    ("watchlist", -16, None),
]

def load_cookie_pool():
    pool = []
    if os.environ.get("XQ_COOKIE"):
        pool.append(os.environ["XQ_COOKIE"].strip())
    if os.path.exists(COOKIE_POOL_FILE):
        for line in open(COOKIE_POOL_FILE, encoding="utf-8"):
            s = line.strip()
            if s: pool.append(s)
    if not pool and os.path.exists(COOKIE_FILE):
        s = open(COOKIE_FILE, encoding="utf-8").read().strip()
        if s: pool.append(s)
    seen = set(); out = []
    for s in pool:
        if s not in seen:
            seen.add(s); out.append(s)
    return out

def parse_cookies(cs):
    out = []
    for part in cs.split(';'):
        part = part.strip()
        if '=' not in part: continue
        n, _, v = part.partition('=')
        out.append({'name': n.strip(), 'value': v.strip(), 'domain': '.xueqiu.com',
                    'path': '/', 'secure': False, 'httpOnly': False, 'expires': -1})
    return out

def url_for(kind, pid, cat):
    base = "https://stock.xueqiu.com/v5/stock/portfolio/stock"
    if kind == "list":
        return f"{base}/list.json?pid={pid}&category={cat}&size=1000&uid={USER_ID}"
    return f"{base}/watchlist.json?pid={pid}&size=2000&uid={USER_ID}"

def main():
    pool = load_cookie_pool()
    cookie = pool[0] if pool else None
    print(f"[selfstock] cookie: {'有' if cookie else '无'} | groups={len(GROUPS)}")
    agg = {}  # symbol -> record
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH, headless=True,
            args=['--disable-blink-features=AutomationControlled','--no-sandbox',
                  '--disable-dev-shm-usage','--disable-infobars','--lang=zh-CN'],
            chromium_sandbox=False)
        ctx = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
            locale='zh-CN', timezone_id='Asia/Shanghai', ignore_https_errors=True)
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {}, csi: () => {}, loadTimes: () => {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        """)
        if cookie: ctx.add_cookies(parse_cookies(cookie))
        page = ctx.new_page()
        try:
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=30000); time.sleep(3)
        except Exception as e:
            print("  [warn] warmup:", e)
        for kind, pid, cat in GROUPS:
            url = url_for(kind, pid, cat)
            try:
                d = page.evaluate("""async (url) => {
                    const ctrl = new AbortController();
                    const t = setTimeout(() => ctrl.abort(), 15000);
                    try { const r = await fetch(url, {credentials:'include', signal:ctrl.signal});
                          const j = await r.json(); clearTimeout(t); return {status:r.status, json:j}; }
                    catch(e){ clearTimeout(t); return {status:0, json:{'_err':e.message}}; }
                }""", url)
                j = d.get("json", {})
                data = j.get("data") if isinstance(j, dict) else None
                items = []
                if isinstance(data, dict):
                    items = data.get("stocks") or data.get("items") or []
                elif isinstance(data, list):
                    items = data
                print(f"  [{kind} pid={pid} cat={cat}] status={d.get('status')} items={len(items)}")
                for it in items:
                    if not isinstance(it, dict): continue
                    sym = it.get("symbol")
                    if not sym: continue
                    rec = agg.get(sym)
                    if rec is None:
                        rec = {
                            "symbol": sym,
                            "name": it.get("name"),
                            "marketplace": it.get("marketplace"),
                            "exchange": it.get("exchange"),
                            "watched": it.get("watched") or it.get("created"),
                            "category": it.get("category"),
                            "pids": [],
                        }
                        agg[sym] = rec
                    if pid not in rec["pids"]:
                        rec["pids"].append(pid)
            except Exception as e:
                print(f"  [{kind} pid={pid}] ERR {e}")
            time.sleep(1.5)
        browser.close()

    records = sorted(agg.values(), key=lambda r: (r.get("watched") or 0))
    out = {"user_id": USER_ID, "fetched_at": int(time.time()*1000),
           "total": len(records), "items": records}
    json.dump(out, open(RAW_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[selfstock] 真实自选股/基金快照：{len(records)} 项 -> {RAW_OUT}")

if __name__ == "__main__":
    main()
