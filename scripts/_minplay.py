#!/usr/bin/env python3
"""最小 playwright 启动测试：定位卡在 launch / new_page / goto 哪一步。内置看门狗 90s 强退。"""
import time, os, threading
from playwright.sync_api import sync_playwright

CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
t0 = time.time()


def log(msg):
    print(f"[{round(time.time() - t0, 1)}s] {msg}", flush=True)


def watchdog(sec):
    time.sleep(sec)
    log(f"WATCHDOG {sec}s exceeded -> force exit")
    os._exit(1)


threading.Thread(target=watchdog, args=(90,), daemon=True).start()

log("sync_playwright import OK")
with sync_playwright() as p:
    log("entered context")
    b = p.chromium.launch(executable_path=CHROME_PATH, headless=True,
                          args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                                '--disable-blink-features=AutomationControlled'])
    log("LAUNCHED")
    pg = b.new_page()
    log("NEW_PAGE")
    pg.goto('about:blank')
    log("ABOUT_BLANK_OK")
    pg.set_content('<h1 id=x>hi</h1>')
    log("SET_CONTENT_OK title=" + repr(pg.title()))
    try:
        pg.goto('https://xueqiu.com/', wait_until='domcontentloaded', timeout=30000)
        log("GOTO_XUEQIU_OK url=" + pg.url)
    except Exception as e:
        log("GOTO_XUEQIU_FAIL " + str(e)[:120])
    b.close()
    log("CLOSED")
log("ALL DONE")
