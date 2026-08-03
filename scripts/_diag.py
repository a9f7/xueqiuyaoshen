#!/usr/bin/env python3
"""诊断：用 cookie 池每个账号各做一次真实 API 请求，打印完整响应，判断是风控/网络/WAF。"""
import os, sys, time, json, threading
from playwright.sync_api import sync_playwright


def _wd(sec):
    time.sleep(sec)
    print(f'[watchdog] {sec}s force exit', flush=True)
    os._exit(1)


threading.Thread(target=_wd, args=(120,), daemon=True).start()

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_POOL_FILE = os.path.join(HERE, "..", "data", "xq_cookies.txt")
CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
USER_ID = 2292705444
PNO = int(sys.argv[1]) if len(sys.argv) > 1 else 639


def load_pool():
    pool = []
    if os.path.exists(COOKIE_POOL_FILE):
        for line in open(COOKIE_POOL_FILE, encoding="utf-8"):
            s = line.strip()
            if s:
                pool.append(s)
    return pool


def parse(c):
    return [{'name': n.strip(), 'value': v.strip(), 'domain': '.xueqiu.com', 'path': '/',
             'secure': False, 'httpOnly': False, 'expires': -1}
            for n, _, v in (p.partition('=') for p in c.split(';') if '=' in p)]


def diag_one(ck, label):
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME_PATH, headless=True,
                              args=['--disable-blink-features=AutomationControlled', '--no-sandbox',
                                    '--disable-dev-shm-usage', '--lang=zh-CN'], chromium_sandbox=False)
        ctx = b.new_context(viewport={'width': 1920, 'height': 1080},
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
                            locale='zh-CN', timezone_id='Asia/Shanghai', ignore_https_errors=True)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{},csi:()=>{},loadTimes:()=>{}};Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});")
        if ck:
            ctx.add_cookies(parse(ck))
        pg = ctx.new_page()
        try:
            pg.goto('https://xueqiu.com/', wait_until='domcontentloaded', timeout=30000)
            print(f'[{label}] warmup OK', flush=True)
        except Exception as e:
            print(f'[{label}] warmup FAIL: {e}', flush=True)
        time.sleep(3)
        url = f'https://xueqiu.com/v4/statuses/user_timeline.json?user_id={USER_ID}&page={PNO}&type=0&count=20'
        res = pg.evaluate("""async(u)=>{const c=new AbortController();const t=setTimeout(()=>c.abort(),15000);try{const r=await fetch(u,{credentials:'include',signal:c.signal});const txt=await r.text();clearTimeout(t);return {ok:r.ok,status:r.status,body:txt.slice(0,500)};}catch(e){clearTimeout(t);return {ok:false,status:0,body:'FETCH_ERR '+e.message};}}""", url)
        print(f'=== [{label}] RESULT ===', flush=True)
        print(f'  ok={res.get("ok")} status={res.get("status")}', flush=True)
        print(f'  body[:500]={res.get("body","")}', flush=True)
        b.close()


if __name__ == "__main__":
    pool = load_pool()
    print(f'[diag] pool size={len(pool)}', flush=True)
    for i, ck in enumerate(pool):
        short = 'NO_COOKIE' if not ck else ck[:60] + ('...' if len(ck) > 60 else '')
        print(f'[diag] cookie#{i}: {short}', flush=True)
        diag_one(ck, f'cookie#{i}')
