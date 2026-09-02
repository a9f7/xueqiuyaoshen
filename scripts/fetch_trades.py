#!/usr/bin/env python3
"""抓取【你自己】雪球账号的模拟组合【交易记录】，加密后输出可部署密文。

为什么能抓到（2026-09-02 修正）：
  /center/#/assets（我的资产）SPA 页面里，tc.xueqiu.com 的 snowx 接口
  **不需要前端 x 签名头**，只要在该页面上下文里用 fetch(credentials:'include')
  直接调用即可（同源 referer + cookie 就够）：
    - 组合列表：tc.xueqiu.com/tc/snowx/MONI/trans_group/list.json
      -> result_data.trans_groups = [{gid, name}, ...]  （股票 / 基金 两组）
    - 交易记录：tc.xueqiu.com/tc/snowx/MONI/transaction/list.json?gid=<gid>&row=50[&pos=<游标>]
      -> result_data.transactions + result_data.pos（下一页游标）

  历史坑（勿走回头路）：
   - 旧实现靠点击页面「交易记录」tab 再拦截响应。该 tab 在当前 DOM 里【不存在】
     （.moni__tabs__controls 为 null、找不到 innerText=='交易记录' 的元素），
     所以永远拦不到 → 一直 0 条。现改为直接调 API。
   - 正则只匹配 `MONI/transaction/list.json?gid=` 也会漏掉 SPA 实际发出的
     `MONI/trans_group/list.json`（无 gid 参数）。

隐私与安全（同 fetch_myholdings.py）：cookie/明文不出本机，部署的是加密密文。

用法：
  python fetch_trades.py            # 真实抓取（cookie 来自 data/my_xq_cookie.txt 或 XQ_MY_COOKIE）
  python fetch_trades.py --mock     # 内置假数据走通加密流程
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

GROUP_API = "https://tc.xueqiu.com/tc/snowx/MONI/trans_group/list.json"
TX_API = "https://tc.xueqiu.com/tc/snowx/MONI/transaction/list.json?gid={gid}&row=50"
MAX_PAGES = 80          # 每组合最多 80 页 * 50 = 4000 条，足够
TYPE_LABEL = {1: "买入", 2: "卖出", 3: "分红", 4: "送转", 9: "除权除息"}


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


JS_FETCH = """async (u)=>{
  try{
    const res = await fetch(u,{credentials:'include',headers:{'X-Requested-With':'XMLHttpRequest'}});
    const t = await res.text();
    return {status:res.status, body:t};
  }catch(e){ return {status:0, body:String(e)}; }
}"""


def _get_json(pg, url):
    """在页面上下文里 fetch 并解析 JSON。失败返回 (status, None)。"""
    try:
        r = pg.evaluate(JS_FETCH, url)
    except Exception as e:
        return -1, None, str(e)
    st = r.get("status")
    body = r.get("body") or ""
    if st != 200:
        return st, None, body[:300]
    try:
        return st, json.loads(body), ""
    except Exception:
        return st, None, body[:300]


def capture_trades(cookie):
    """返回 ({gid: [tx,...]}, {gid: {'name':...}})，失败返回 (None, None)。"""
    groups = {}
    group_meta = {}
    diag = {"steps": []}
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

        # 必须先落地到雪球页面，后续 fetch 才带对的 referer / 同站 cookie
        pg.goto("https://xueqiu.com/center/#/assets?t=%d" % int(time.time() * 1000),
                wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)

        # 1) 组合列表
        st, gj, err = _get_json(pg, GROUP_API)
        diag["steps"].append({"api": "trans_group", "status": st, "err": err})
        gids = []
        if gj and (gj.get("result_data") or {}).get("trans_groups"):
            for g in gj["result_data"]["trans_groups"]:
                gid = str(g.get("gid") or "")
                if not gid:
                    continue
                gids.append(gid)
                group_meta[gid] = {"name": g.get("name") or "", "cash": g.get("cash")}
        if not gids:
            print("[trades] 组合列表获取失败（status=%s），回退到已知 gid" % st)
            gids = ["2965180"]
            group_meta.setdefault("2965180", {"name": "股票"})
        print("[trades] 组合数=%d -> %s" % (
            len(gids), ", ".join("%s(%s)" % (group_meta.get(g, {}).get("name", ""), g) for g in gids)))

        # 2) 逐组合分页拉交易记录
        for gid in gids:
            url = TX_API.format(gid=gid)
            seen_pos = set()
            pages = 0
            while pages < MAX_PAGES:
                st, d, err = _get_json(pg, url)
                if not d:
                    diag["steps"].append({"api": "transaction", "gid": gid, "status": st, "err": err})
                    break
                rd = d.get("result_data") or {}
                txs = rd.get("transactions") or []
                pos = rd.get("pos")
                if txs:
                    groups.setdefault(gid, []).extend(txs)
                pages += 1
                if not txs or not pos or pos in seen_pos:
                    break
                seen_pos.add(pos)
                url = TX_API.format(gid=gid) + "&pos=%s" % pos
                time.sleep(0.4)
            print("[trades] gid=%s (%s) 抓到 %d 条 / %d 页" % (
                gid, group_meta.get(gid, {}).get("name", ""), len(groups.get(gid, [])), pages))

        b.close()

    if not groups:
        try:
            json.dump({"matched": False, "diag": diag},
                      open(PROBE_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
        return None, None
    return groups, group_meta


def _num(x):
    if x is None:
        return None
    try:
        if isinstance(x, (int, float)):
            return float(x)
        return float(str(x).replace(",", "").replace("%", ""))
    except Exception:
        return None


def parse_trades(groups, group_meta=None):
    """把 {gid:[tx]} 归一为前端可渲染的交易记录列表。"""
    group_meta = group_meta or {}
    items = []
    for gid, txs in groups.items():
        gname = (group_meta.get(gid) or {}).get("name") or ""
        for t in txs:
            tcode = t.get("type")
            label = t.get("type_name") or TYPE_LABEL.get(tcode) or ""
            note = (t.get("comment") or "").strip() or (t.get("desc") or "").strip()
            items.append({
                "gid": gid,
                "group": gname,
                "tid": t.get("tid") or t.get("id"),
                "symbol": str(t.get("symbol") or ""),
                "name": t.get("name"),
                # 前端逻辑：type 非空走内置 typeMap，否则用 action 文案。
                # 雪球 type 码不止 1/2/3/4（还有 9 除权除息等），故统一置空、用 API 中文标签。
                "type": None,
                "type_code": tcode,
                "action": label,
                "price": _num(t.get("price")),
                "shares": _num(t.get("shares")),
                "value": _num(t.get("amount") or t.get("value")),
                "fee": _num(t.get("commission")),
                "tax": _num(t.get("tax")),
                "profit": _num(t.get("profit")),
                "created_at": t.get("time") or t.get("created_at"),
                "note": note,
            })
    seen = set()
    uniq = []
    for it in items:
        k = (it["gid"], it["tid"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    uniq.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return uniq


def _mock_trades():
    now = int(time.time() * 1000)
    return {
        "2965180": [
            {"tid": "t1", "symbol": "SZ000933", "name": "神火股份", "type": 1, "type_name": "买入",
             "price": 25.3, "shares": 2600, "amount": 65780, "commission": 19.7, "time": now - 86400000,
             "comment": "建仓"},
            {"tid": "t2", "symbol": "SZ000933", "name": "神火股份", "type": 2, "type_name": "卖出",
             "price": 27.15, "shares": 2600, "amount": 70590, "commission": 21.1, "time": now - 3600000,
             "comment": "清仓"},
        ]
    }, {"2965180": {"name": "股票"}}


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
        groups, group_meta = _mock_trades()
    else:
        if not cookie:
            print("[trades] 未找到 cookie：请更新 data/my_xq_cookie.txt 或设置 XQ_MY_COOKIE")
            sys.exit(2)
        print(f"[trades] cookie: 有 | uid={uid} | 直调 tc.xueqiu.com snowx 接口 …")
        groups, group_meta = capture_trades(cookie)
        if not groups:
            print("[trades] 未抓到交易记录（WAF 限流 / cookie 过期）。保留上次密文。日志:", PROBE_OUT)
            sys.exit(3)

    items = parse_trades(groups, group_meta)
    by_group = {}
    for it in items:
        by_group[it["group"] or it["gid"]] = by_group.get(it["group"] or it["gid"], 0) + 1
    out = {
        "fetched_at": int(time.time() * 1000),
        "uid": uid,
        "source": "mock" if mock else "xueqiu",
        "count": len(items),
        "groups": {g: (group_meta or {}).get(g, {}).get("name", "") for g in groups},
        "items": items,
    }
    blob = encrypt_obj(out, password)
    json.dump(out, open(RAW_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(blob, open(ENC_OUT, "w", encoding="utf-8"), ensure_ascii=False)
    span = ""
    ts = [i["created_at"] for i in items if i.get("created_at")]
    if ts:
        span = " | 区间 %s ~ %s" % (
            time.strftime("%Y-%m-%d", time.localtime(min(ts) / 1000)),
            time.strftime("%Y-%m-%d", time.localtime(max(ts) / 1000)))
    print("[trades] 交易记录数=%d（%s）%s" % (
        out["count"], ", ".join("%s:%d" % (k, v) for k, v in by_group.items()), span))
    print("[trades] -> %s（密文，可部署）| %s（明文，gitignore）" % (ENC_OUT, RAW_OUT))


if __name__ == "__main__":
    main()
