#!/usr/bin/env python3
"""抓取【你自己】雪球账号的模拟组合【交易记录】，加密后输出可部署密文。

为什么能抓到：
  /center/#/assets（我的资产）SPA 里「交易记录」tab 会带正确签名调用
  tc.xueqiu.com/tc/snowx/MONI/transaction/list.json?gid=<组合ID>&row=50&pos=<游标>
  我们用 playwright 打开该页，*拦截 SPA 自己发出的这个响应*，绕过 WAF。

  与 fetch_myholdings.py 的区别：
   - 交易记录是分页的（每页 50 条，next cursor 在 result_data.pos）。
   - 账号可能有多组合（股票组合 + 基金组合），各自有独立交易记录，需逐个抓取。

隐私与安全（同 fetch_myholdings.py）：cookie/明文不出本机，部署的是加密密文。

用法：
  python fetch_trades.py            # 真实抓取（cookie 来自 data/my_xq_cookie.txt 或 XQ_MY_COOKIE）
  python fetch_trades.py --mock    # 内置假数据走通加密流程
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
ENC_OUT = os.path.join(ROOT, "data", "my_trades.enc.json")      # 加密，可部署
RAW_OUT = os.path.join(ROOT, "data", "my_trades.json")          # 明文，gitignore
PROBE_OUT = os.path.join(ROOT, "data", "_trades_probe.json")

sys.path.insert(0, HERE)
from holdings_crypto import encrypt_obj

CHROME_PATH = r"C:\Users\d\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
TX_RE = re.compile(r"MONI/transaction/list\.json\?gid=(\d+)")
TG_RE = re.compile(r"MONI/trans_group/list\.json")


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


def _click_tab(pg, text):
    """点击页面上 innerText==text 的元素（用于切到 交易记录 tab）。返回是否点到。"""
    return pg.evaluate("""(t)=>{
      const a=[...document.querySelectorAll('a,div,span,li,button')].find(e=>e.innerText&&e.innerText.trim()===t);
      if(a){a.click();return true;}
      return false;
    }""", text)


def capture_trades(cookie):
    """打开 /center/#/assets，逐个组合拦截 transaction/list.json，返回 {gid: [tx,...]}。"""
    groups = {}          # gid -> list of transactions
    group_meta = {}      # gid -> {name}
    all_xq = []
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

        captured = {}   # url -> (gid, body)
        def on_resp(resp):
            u = resp.url
            if "xueqiu.com" in u:
                all_xq.append(u)
            m = TX_RE.search(u)
            if m:
                try:
                    body = resp.body().decode("utf-8", "replace")
                except Exception:
                    body = ""
                captured[u] = (m.group(1), body)
        pg.on("response", on_resp)

        pg.goto("https://xueqiu.com/center/#/assets?t=%d" % int(time.time()*1000),
                wait_until="domcontentloaded", timeout=25000)
        time.sleep(6)
        _click_tab(pg, "股票")   # 触发 moni 组件渲染
        time.sleep(4)

        # 找到组合切换 tab（.moni__tabs__controls a），逐个切换并抓交易
        sec_count = pg.evaluate("""()=>{
          const wrap=document.querySelector('.moni__tabs__controls');
          if(!wrap) return 0;
          return wrap.querySelectorAll('a').length;
        }""")
        print("[trades] 组合 tab 数:", sec_count)

        # 先抓默认组合
        _capture_current(pg, captured, groups, group_meta)

        # 逐个切换其余组合
        for i in range(1, max(sec_count, 1)):
            ok = pg.evaluate("""(idx)=>{
              const wrap=document.querySelector('.moni__tabs__controls');
              if(!wrap) return false;
              const a=wrap.querySelectorAll('a')[idx];
              if(a){a.click();return true;}
              return false;
            }""", i)
            if not ok:
                break
            time.sleep(3)
            _capture_current(pg, captured, groups, group_meta)

        b.close()

    if not groups:
        try:
            json.dump({"matched": False, "all_xqiu": all_xq[-60:]},
                      open(PROBE_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
        return None
    return groups


def _capture_current(pg, captured, groups, group_meta):
    """在当前选中组合下，点 交易记录 tab，拦截 transaction/list.json（含分页）。"""
    captured.clear()
    if not _click_tab(pg, "交易记录"):
        return
    # 轮询首屏
    deadline = time.time() + 20
    while not captured and time.time() < deadline:
        time.sleep(0.5)
    # 解析并跟进分页（pos 游标）
    seen_pos = set()
    for _ in range(40):  # 最多 40 页
        if not captured:
            break
        # 取最新一条响应
        url, (gid, body) = next(iter(captured.items()))
        captured.clear()
        try:
            d = json.loads(body)
        except Exception:
            break
        rd = (d.get("result_data") or {})
        txs = rd.get("transactions") or []
        pos = rd.get("pos")
        groups.setdefault(gid, []).extend(txs)
        group_meta.setdefault(gid, {})
        if not pos or pos in seen_pos:
            break
        seen_pos.add(pos)
        # 触发下一页：用捕获的 x 头 replay（best-effort）
        # 通过页面 fetch 复用，pos 递增
        nx = re.sub(r"pos=\d+", "pos=%s" % pos, url) if "pos=" in url else (url + ("&" if "?" in url else "?") + "pos=%s" % pos)
        try:
            pg.evaluate("""(u)=>fetch(u,{credentials:'include',headers:{'X-Requested-With':'XMLHttpRequest'}}).catch(()=>{})""", nx)
        except Exception:
            pass
        time.sleep(2.5)
        if not captured:
            break


def _num(x):
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)):
            return float(x)
        return float(str(x).replace(",", "").replace("%", ""))
    except Exception:
        return None


def parse_trades(groups):
    """把 {gid:[tx]} 归一为交易记录列表。"""
    items = []
    for gid, txs in groups.items():
        for t in txs:
            items.append({
                "gid": gid,
                "tid": t.get("tid") or t.get("id"),
                "symbol": str(t.get("symbol") or ""),
                "name": t.get("name"),
                "type": t.get("type"),             # 1买/2卖/3...见雪球
                "action": t.get("action"),
                "price": _num(t.get("price")),
                "shares": _num(t.get("shares") or t.get("amount")),
                "value": _num(t.get("value") or t.get("current_value")),
                "fee": _num(t.get("fee") or t.get("commission")),
                "profit": _num(t.get("profit") or t.get("gain")),
                "created_at": t.get("created_at") or t.get("time") or t.get("date"),
                "note": t.get("note") or t.get("comment"),
            })
    # 去重（同 gid+tid）
    seen = set()
    uniq = []
    for it in items:
        k = (it["gid"], it["tid"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq


def _mock_trades():
    now = int(time.time()*1000)
    return {
        "2965180": [
            {"tid": "t1", "symbol": "SZ000933", "name": "神火股份", "type": 1, "price": 25.3,
             "shares": 2600, "value": 65780, "fee": 19.7, "profit": None, "created_at": now-86400000, "note": "建仓"},
            {"tid": "t2", "symbol": "SZ000933", "name": "神火股份", "type": 2, "price": 27.15,
             "shares": 2600, "value": 70590, "fee": 21.1, "profit": 4744.22, "created_at": now-3600000, "note": "清仓"},
        ]
    }


def main():
    mock = "--mock" in sys.argv
    cookie = None if mock else load_cookie()
    uid = get_uid(cookie) if cookie else None
    password = load_password()
    if not password:
        print("[trades] 未找到密码：请写入 data/.holdings_pwd 或设置 HOLDINGS_PWD")
        sys.exit(2)

    if mock:
        print("[trades] MOCK 模式")
        groups = _mock_trades()
    else:
        if not cookie:
            print("[trades] 未找到 cookie")
            sys.exit(2)
        print(f"[trades] cookie: 有 | uid={uid} | 拦截 transaction/list.json …")
        groups = capture_trades(cookie)
        if not groups:
            print("[trades] 未捕获到交易记录（SPA 未发出请求 / WAF 限流 / cookie 过期）。已存日志:", PROBE_OUT)
            sys.exit(3)

    items = parse_trades(groups)
    out = {
        "fetched_at": int(time.time()*1000),
        "uid": uid,
        "source": "mock" if mock else "xueqiu",
        "count": len(items),
        "items": items,
    }
    blob = encrypt_obj(out, password)
    json.dump(out, open(RAW_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(blob, open(ENC_OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[trades] 交易记录数={out['count']} -> {ENC_OUT}（密文）| {RAW_OUT}（明文）")


if __name__ == "__main__":
    main()
