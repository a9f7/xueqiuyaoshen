#!/usr/bin/env python3
"""抓取【你自己】雪球账号的模拟盈亏持仓，并加密后输出可部署的密文。

隐私与安全：
  - 你的 cookie 只从本地 data/my_xq_cookie.txt（gitignore，不入库）或环境变量 XQ_MY_COOKIE 读取，绝不写入仓库。
  - 持仓明文只落本地 data/my_holdings.json（gitignore）。
  - 部署到 GitHub Pages 的是【加密】产物 data/my_holdings.enc.json，无密码解不开。
  - 加密密码从 data/.holdings_pwd（gitignore）或环境变量 HOLDINGS_PWD 读取。

抓取方式：playwright 浏览器内 fetch（带登录 cookie）。
  裸 urllib 直连雪球 API 会被 WAF 拦截（403 / 返回 HTML 登录页），必须用浏览器上下文。

用法：
  # 用内置假数据走通「加密 -> 部署密文」流程（无需 cookie）
  python fetch_myholdings.py --mock

  # 真实抓取（需先放好 cookie 与密码）
  python fetch_myholdings.py
"""
import os
import sys
import json
import time
import re
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
COOKIE_FILE = os.path.join(ROOT, "data", "my_xq_cookie.txt")
PWD_FILE = os.path.join(ROOT, "data", ".holdings_pwd")
ENC_OUT = os.path.join(ROOT, "data", "my_holdings.enc.json")
RAW_OUT = os.path.join(ROOT, "data", "my_holdings.json")     # 明文，gitignore
PROBE_OUT = os.path.join(ROOT, "data", "_holdings_probe.json")

sys.path.insert(0, HERE)
from holdings_crypto import encrypt_obj

CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"

# 候选持仓/组合 API（登录态）。真实可用端点以你账号探测结果为准。
CANDIDATE_TEMPLATES = [
    "https://stock.xueqiu.com/v4/portfolio/portfolio/list.json?uid={uid}&size=20&_={ts}",
    "https://stock.xueqiu.com/v4/portfolio/stock/list.json?uid={uid}&size=100&_={ts}",
    "https://xueqiu.com/v4/portfolio/stock/list.json?uid={uid}&_={ts}",
    "https://stock.xueqiu.com/stock/portfolio/stock/list.json?pid=-1&category=1&uid={uid}&size=1000",
]


def load_cookie():
    if os.environ.get("XQ_MY_COOKIE"):
        return os.environ["XQ_MY_COOKIE"].strip()
    if os.path.exists(COOKIE_FILE):
        return open(COOKIE_FILE, encoding="utf-8").read().strip()
    return None


def load_password():
    if os.environ.get("HOLDINGS_PWD"):
        return os.environ["HOLDINGS_PWD"]
    if os.path.exists(PWD_FILE):
        return open(PWD_FILE, encoding="utf-8").read().strip()
    return None


def get_uid(cookie):
    m = re.search(r"(?:^|[\s;])u=(\d+)", cookie or "")
    return m.group(1) if m else None


def parse_cookies(cs):
    out = []
    for part in cs.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        n, _, v = part.partition("=")
        out.append({"name": n.strip(), "value": v.strip(), "domain": ".xueqiu.com",
                    "path": "/", "secure": False, "httpOnly": False, "expires": -1})
    return out


def pw_session(cookie, urls):
    """单个 playwright 会话内依次 fetch 多个候选 URL（绕过 WAF）。返回 [(status, json), ...]"""
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME_PATH, headless=True,
                              args=["--no-sandbox", "--disable-dev-shm-usage",
                                    "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            locale="zh-CN")
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        ctx.add_cookies(parse_cookies(cookie))
        pg = ctx.new_page()
        pg.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=25000)
        for url in urls:
            d = pg.evaluate("""async (url)=>{
              try{
                const r=await fetch(url,{credentials:'include'});
                const t=await r.text();
                let j; try{ j=JSON.parse(t); }catch(e){ j={'_raw':t.slice(0,160)}; }
                return {status:r.status, json:j};
              }catch(e){ return {status:0, json:{'_err':e.message}}; }
            }""", url)
            out.append((d.get("status"), d.get("json")))
        b.close()
    return out


def _num(x):
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)):
            return float(x)
        return float(str(x).replace(",", "").replace("%", ""))
    except Exception:
        return None


def extract_holdings(j):
    """启发式：从各种雪球返回结构里提取含 symbol + 名称 + 盈亏/价格的持仓数组。"""
    items = []
    data = j.get("data") if isinstance(j, dict) else None
    arr = None
    if isinstance(data, dict):
        for k in ("stocks", "items", "list", "holdings", "positions"):
            v = data.get(k)
            if isinstance(v, list):
                arr = v
                break
    elif isinstance(data, list):
        arr = data
    if not arr and isinstance(j, dict):
        for k in ("stocks", "items", "list", "holdings", "positions"):
            v = j.get(k)
            if isinstance(v, list):
                arr = v
                break
    if not arr:
        return items
    for it in arr:
        if not isinstance(it, dict):
            continue
        sym = it.get("symbol") or it.get("stock_symbol") or it.get("code")
        if not sym:
            continue
        items.append({
            "symbol": str(sym),
            "name": it.get("name") or it.get("stock_name") or it.get("company_name"),
            "current": _num(it.get("current") or it.get("price") or it.get("last")),
            "cost": _num(it.get("cost") or it.get("avg_cost") or it.get("holding_cost")),
            "amount": _num(it.get("amount") or it.get("volume") or it.get("shares")),
            "value": _num(it.get("value") or it.get("market_value") or it.get("position_value")),
            "profit": _num(it.get("profit") or it.get("gain") or it.get("pl")),
            "profit_rate": _num(it.get("profit_rate") or it.get("rate") or it.get("pl_rate")),
            "weight": _num(it.get("weight") or it.get("prop") or it.get("percent")),
            "updated": it.get("updated_at") or it.get("time") or None,
        })
    return items


def _mock_holdings():
    now = int(time.time() * 1000)
    return [
        {"symbol": "SH600519", "name": "贵州茅台", "current": 1680.0, "cost": 1500.0,
         "amount": 100.0, "value": 168000.0, "profit": 18000.0, "profit_rate": 12.0,
         "weight": 40.0, "updated": now},
        {"symbol": "SZ000858", "name": "五粮液", "current": 150.0, "cost": 160.0,
         "amount": 1000.0, "value": 150000.0, "profit": -10000.0, "profit_rate": -6.25,
         "weight": 35.0, "updated": now},
        {"symbol": "HK00700", "name": "腾讯控股", "current": 380.0, "cost": 300.0,
         "amount": 500.0, "value": 190000.0, "profit": 40000.0, "profit_rate": 26.67,
         "weight": 25.0, "updated": now},
    ]


def main():
    mock = "--mock" in sys.argv
    cookie = None if mock else load_cookie()
    uid = get_uid(cookie) if cookie else None
    password = load_password()
    if not password:
        print("[holdings] 未找到密码：请写入 data/.holdings_pwd 或设置环境变量 HOLDINGS_PWD")
        sys.exit(2)

    if mock:
        print("[holdings] MOCK 模式：内置假数据走加密流程")
        holdings = _mock_holdings()
    else:
        if not cookie:
            print("[holdings] 未找到 cookie：请写入 data/my_xq_cookie.txt 或设置 XQ_MY_COOKIE")
            sys.exit(2)
        print(f"[holdings] cookie: 有 | uid={uid}")
        ts = int(time.time() * 1000)
        urls = [tpl.format(uid=uid or "", ts=ts) for tpl in CANDIDATE_TEMPLATES
                if ("{uid}" not in tpl) or uid]
        results = pw_session(cookie, urls)
        probe = {}
        for u, (st, j) in zip(urls, results):
            arr = len(extract_holdings(j)) if isinstance(j, dict) else 0
            probe[u] = {"status": st, "array_len": arr}
            print(f"  probe status={st} arr={arr} {u[:70]}")
        json.dump(probe, open(PROBE_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        holdings = None
        for (st, j) in results:
            items = extract_holdings(j) if isinstance(j, dict) else []
            if items:
                holdings = items
                print(f"[holdings] 命中 API：{len(items)} 项")
                break
        if not holdings:
            print("[holdings] 候选 API 均未解析到持仓；原始响应已存:", PROBE_OUT)
            print("[holdings] 请把该文件内容贴给我，我来精修解析。")
            sys.exit(3)

    total_value = sum((h["value"] or 0) for h in holdings)
    total_profit = sum((h["profit"] or 0) for h in holdings)
    out = {
        "fetched_at": int(time.time() * 1000),
        "uid": uid,
        "source": "mock" if mock else "xueqiu",
        "count": len(holdings),
        "total_value": round(total_value, 2),
        "total_profit": round(total_profit, 2),
        "items": holdings,
    }
    blob = encrypt_obj(out, password)
    json.dump(out, open(RAW_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(blob, open(ENC_OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[holdings] 加密完成 -> {ENC_OUT}（密文，可部署）| 明文 -> {RAW_OUT}（gitignore）")


if __name__ == "__main__":
    main()
