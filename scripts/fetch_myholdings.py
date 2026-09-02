#!/usr/bin/env python3
"""抓取【你自己】雪球账号的模拟盈亏持仓，并加密后输出可部署的密文。

隐私与安全：
  - 你的 cookie 只从本地 data/my_xq_cookie.txt（gitignore，不入库）或环境变量 XQ_MY_COOKIE 读取，绝不写入仓库。
  - 持仓明文只落本地 data/my_holdings.json（gitignore）。
  - 部署到 GitHub Pages 的是【加密】产物 data/my_holdings.enc.json，无密码解不开。
  - 加密密码从 data/.holdings_pwd（gitignore）或环境变量 HOLDINGS_PWD 读取。

抓取方式（关键突破）：
  裸 urllib 直连 stock.xueqiu.com/v4/portfolio 会被 openresty WAF 拦截（403，需 x 签名头）。
  但「我的资产」SPA（/center/#/assets）会用【带正确签名】的内部请求调用
  tc.xueqiu.com/tc/snowx/MONI/performances.json?gid=<组合ID> 获取模拟组合明细。
  我们用 playwright 打开该页，*拦截 SPA 自己发出的这个响应*，从而绕过 WAF 拿到完整持仓。

用法：
  # 用内置假数据走通「加密 -> 部署密文」流程（无需 cookie）
  python fetch_myholdings.py --mock

  # 真实抓取（需先放好 cookie 与密码）
  python fetch_myholdings.py
"""
import os
import re
import sys
import time
import json
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

PERF_RE = re.compile(r"MONI/performances\.json\?gid=(\d+)")


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


def capture_performances(cookie):
    """打开 /center/#/assets，拦截 SPA 发出的 performances.json 响应。返回 (gid, dict) 或 (None, None)。"""
    matched = {}  # url -> Response
    all_xq = []   # 诊断：所有 xueqiu 响应 URL
    # 本地有固定 chromium 路径就用它；CI/其他环境回退到 playwright 托管的浏览器
    launch_kwargs = dict(headless=True,
                         args=["--no-sandbox", "--disable-dev-shm-usage",
                               "--disable-blink-features=AutomationControlled"])
    if os.path.exists(CHROME_PATH):
        launch_kwargs["executable_path"] = CHROME_PATH
    with sync_playwright() as p:
        b = p.chromium.launch(**launch_kwargs)
        ctx = b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            locale="zh-CN", timezone_id="Asia/Shanghai", ignore_https_errors=True)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        ctx.add_cookies(parse_cookies(cookie))
        pg = ctx.new_page()

        def on_resp(resp):
            u = resp.url
            if "xueqiu.com" in u:
                all_xq.append(u)
            m = PERF_RE.search(u)
            if m and u not in matched:
                matched[u] = resp
        pg.on("response", on_resp)

        pg.goto("https://xueqiu.com/center/#/assets?t=%d" % int(time.time() * 1000),
                wait_until="domcontentloaded", timeout=25000)
        time.sleep(6)
        # 点“股票”区块，触发持仓明细请求（部分账号为懒加载）
        try:
            pg.evaluate("""()=>{const els=[...document.querySelectorAll('div,span,a,li,button')];const t=els.find(e=>e.innerText&&e.innerText.trim()==='股票');if(t)t.click();}""")
        except Exception:
            pass
        time.sleep(6)

        # 轮询等待 SPA 发出 performances 请求（最多 ~25s）
        deadline = time.time() + 25
        while not matched and time.time() < deadline:
            time.sleep(0.5)

        # 在上下文还活着时取 body
        bodies = {}
        for u, resp in matched.items():
            try:
                bodies[u] = resp.body().decode("utf-8", "replace")
            except Exception:
                bodies[u] = ""
        b.close()

    if not bodies:
        # 诊断：把本轮所有 xueqiu 响应 URL 存盘
        try:
            json.dump({"matched": False, "all_xqiu_responses": all_xq[-60:]},
                      open(PROBE_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
        return None, None
    # 选 body 最大的那个
    best_url = max(bodies, key=lambda u: len(bodies[u]))
    gid = PERF_RE.search(best_url).group(1)
    try:
        return gid, json.loads(bodies[best_url])
    except Exception:
        return gid, None


def _num(x):
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)):
            return float(x)
        return float(str(x).replace(",", "").replace("%", ""))
    except Exception:
        return None


def parse_performances(d, uid):
    """把 performances.json 解析成 {summary, segments, items}。"""
    if not isinstance(d, dict):
        return None
    perf = (d.get("result_data") or {}).get("performances")
    if not isinstance(perf, list):
        return None

    all_seg = next((s for s in perf if s.get("market") == "ALL"), None)
    summary = {}
    if all_seg:
        summary = {
            "assets": _num(all_seg.get("assets")),
            "principal": _num(all_seg.get("principal")),
            "cash": _num(all_seg.get("cash")),
            "market_value": _num(all_seg.get("market_value")),
            "float_amount": _num(all_seg.get("float_amount")),
            "float_rate": _num(all_seg.get("float_rate")),
            "accum_amount": _num(all_seg.get("accum_amount")),
            "accum_rate": _num(all_seg.get("accum_rate")),
            "day_float_amount": _num(all_seg.get("day_float_amount")),
            "day_float_rate": _num(all_seg.get("day_float_rate")),
        }

    segments = []
    items = []
    for seg in perf:
        if seg.get("market") == "ALL":
            continue
        lst = seg.get("list") or []
        seg_mv = 0.0
        seg_float = 0.0
        seg_day = 0.0
        seg_accum = 0.0
        for it in lst:
            mv = _num(it.get("market_value")) or 0.0
            fl = _num(it.get("float_amount")) or 0.0
            dy = _num(it.get("day_float_amount")) or 0.0
            ac = _num(it.get("accum_amount")) or 0.0
            seg_mv += mv
            seg_float += fl
            seg_day += dy
            seg_accum += ac
            items.append({
                "symbol": str(it.get("symbol") or ""),
                "name": it.get("name"),
                "market": seg.get("market"),
                "market_name": seg.get("name"),
                "currency": it.get("currency"),
                "current": _num(it.get("current")),
                "cost": _num(it.get("diluted_cost")),       # 摊薄成本
                "hold_cost": _num(it.get("hold_cost")),
                "amount": _num(it.get("shares")),            # 股数/份额
                "value": _num(it.get("market_value")),
                "profit": fl,                                 # 浮动盈亏（未实现）
                "profit_rate": _num(it.get("float_rate")),
                "day_profit": dy,                            # 今日浮动盈亏
                "day_profit_rate": _num(it.get("day_float_rate")),
                "accum_profit": ac,                          # 累计盈亏
                "accum_rate": _num(it.get("accum_rate")),
                "open_time": _num(it.get("open_time")),
            })
        segments.append({
            "market": seg.get("market"),
            "name": seg.get("name"),
            "count": len(lst),
            "mv": round(seg_mv, 2),
            "day_float": round(seg_day, 2),
            "float": round(seg_float, 2),
        })

    total_value = round(sum((i["value"] or 0) for i in items), 2)
    total_profit = round(sum((i["profit"] or 0) for i in items), 2)
    total_day = round(sum((i["day_profit"] or 0) for i in items), 2)
    total_accum = round(sum((i["accum_profit"] or 0) for i in items), 2)
    # 仓位占比（相对本组持仓总市值）
    for i in items:
        i["weight"] = round((i["value"] or 0) / total_value * 100, 2) if total_value else None

    return {
        "summary": summary,
        "segments": segments,
        "items": items,
        "count": len(items),
        "total_value": total_value,
        "total_profit": total_profit,
        "total_day_profit": total_day,
        "total_accum_profit": total_accum,
    }


def _mock_holdings():
    """内置假数据（演示用），结构与真实解析一致。"""
    now = int(time.time() * 1000)
    fake = {
        "summary": {"assets": 1645279.89, "principal": 903780.63, "cash": 320319.78,
                     "market_value": 1324960.1, "float_amount": -37929.38, "float_rate": -0.0278,
                     "accum_amount": 741498.97, "accum_rate": 0.616,
                     "day_float_amount": -10827.69, "day_float_rate": -0.0065},
        "segments": [
            {"market": "CHA", "name": "A股", "count": 2, "mv": 70590.0, "day_float": -884.0, "float": 4744.22},
            {"market": "HK", "name": "港股", "count": 1, "mv": 70950.0, "day_float": -300.0, "float": 1050.0},
        ],
        "items": [
            {"symbol": "SZ000933", "name": "神火股份", "market": "CHA", "market_name": "A股",
             "currency": "CNY", "current": 27.15, "cost": 25.325, "hold_cost": 25.325,
             "amount": 2600.0, "value": 70590.0, "profit": 4744.22, "profit_rate": 0.0721,
             "day_profit": -884.0, "day_profit_rate": -0.0124, "accum_profit": 4744.22,
             "accum_rate": 0.0721, "open_time": now, "weight": 49.9},
            {"symbol": "SH600519", "name": "贵州茅台", "market": "CHA", "market_name": "A股",
             "currency": "CNY", "current": 1680.0, "cost": 1500.0, "hold_cost": 1500.0,
             "amount": 100.0, "value": 168000.0, "profit": 18000.0, "profit_rate": 0.12,
             "day_profit": -200.0, "day_profit_rate": -0.001, "accum_profit": 18000.0,
             "accum_rate": 0.12, "open_time": now, "weight": 50.1},
            {"symbol": "02268", "name": "药明合联", "market": "HK", "market_name": "港股",
             "currency": "HKD", "current": 70.95, "cost": 69.9, "hold_cost": 69.9,
             "amount": 1000.0, "value": 70950.0, "profit": 1050.0, "profit_rate": 0.015,
             "day_profit": -300.0, "day_profit_rate": -0.0042, "accum_profit": 1050.0,
             "accum_rate": 0.015, "open_time": now, "weight": 100.0},
        ],
        "count": 3,
        "total_value": 309540.0,
        "total_profit": 23794.22,
        "total_day_profit": -1384.0,
        "total_accum_profit": 23794.22,
    }
    return fake


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
        parsed = _mock_holdings()
    else:
        if not cookie:
            print("[holdings] 未找到 cookie：请写入 data/my_xq_cookie.txt 或设置 XQ_MY_COOKIE")
            sys.exit(2)
        print(f"[holdings] cookie: 有 | uid={uid} | 打开 /center/#/assets 拦截 performances 请求…")
        gid, raw = capture_performances(cookie)
        if not raw:
            print("[holdings] 未捕获到 performances.json 响应（SPA 可能未发出请求 / 网络异常）。")
            print("[holdings] 请确认 cookie 有效且未过期。已存响应日志:", PROBE_OUT)
            sys.exit(3)
        print(f"[holdings] 捕获成功 | gid={gid} | 解析中…")
        parsed = parse_performances(raw, uid)
        if not parsed or not parsed["items"]:
            print("[holdings] 解析失败或持仓为空。原始结构摘要:",
                  json.dumps({k: (list(v.keys()) if isinstance(v, dict) else type(v).__name__)
                              for k, v in (raw.get("result_data") or {}).items()},
                             ensure_ascii=False)[:300])
            sys.exit(3)

        # 存一份原始响应日志，便于排查
        json.dump({"gid": gid, "url_hint": "tc.xueqiu.com/tc/snowx/MONI/performances.json",
                   "success": True}, open(PROBE_OUT, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    out = {
        "fetched_at": int(time.time() * 1000),
        "uid": uid,
        "source": "mock" if mock else "xueqiu",
        "gid": parsed.get("summary") and None,  # gid 仅服务端用，不写明文/密文
        "count": parsed["count"],
        "total_value": parsed["total_value"],
        "total_profit": parsed["total_profit"],
        "total_day_profit": parsed["total_day_profit"],
        "total_accum_profit": parsed["total_accum_profit"],
        "summary": parsed["summary"],
        "segments": parsed["segments"],
        "items": parsed["items"],
    }
    blob = encrypt_obj(out, password)
    json.dump(out, open(RAW_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(blob, open(ENC_OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[holdings] 持仓数={out['count']} 本组总市值={out['total_value']:.2f} "
          f"浮动盈亏={out['total_profit']:.2f} 今日={out['total_day_profit']:.2f}")
    print(f"[holdings] 加密完成 -> {ENC_OUT}（密文，可部署）| 明文 -> {RAW_OUT}（gitignore）")


if __name__ == "__main__":
    main()
