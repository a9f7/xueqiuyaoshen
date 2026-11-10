#!/usr/bin/env python3
"""校验 data/xq_cookies.txt 里每条 cookie 的登录态，可选择性清除无效项。

判定为三态，避免把 WAF / 限流 / 网络抖动误当成 cookie 失效而误删：
  VALID   需登录接口(page=2)返回了非空 statuses
  INVALID 明确鉴权失败（error_code / 401 / 403 / page=2 返回空 statuses）
  UNKNOWN 网络错误 / WAF 挑战页 / 405 / 429 / 非 JSON 响应 -> 一律保留

用法：
  python scripts/check_cookies.py            # 只诊断，不改任何文件
  python scripts/check_cookies.py --prune    # 备份后移除 INVALID 条目
"""
import os
import sys
import re
import json
import time
import shutil
import base64
import datetime
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
POOL = os.path.join(ROOT, "data", "xq_cookies.txt")
USER_ID = 2292705444
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
PATH2 = f"/v4/statuses/user_timeline.json?user_id={USER_ID}&page=2&type=0&count=20"
PATH1 = f"/v4/statuses/user_timeline.json?user_id={USER_ID}&page=1&type=0&count=20"
# api 子域不挂阿里云 WAF，优先；主域兜底
URLS = [f"https://api.xueqiu.com{PATH2}", f"https://xueqiu.com{PATH2}"]


def probe(url, cookie):
    headers = {
        "Cookie": cookie or "",
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://xueqiu.com/",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, "ERR:" + str(e)


def classify(status, body):
    """把一次响应归为 VALID / INVALID / UNKNOWN。"""
    b = (body or "").lstrip()
    if status == 0:
        return "UNKNOWN", "网络错误/超时"
    if status in (405, 429):
        return "UNKNOWN", f"HTTP {status}（限流/风控，不代表 cookie 失效）"
    if status in (401, 403):
        return "INVALID", f"HTTP {status}（未登录/鉴权失败）"
    if b.startswith("<") or "aliyun_waf" in b or "renderData" in b[:2000]:
        return "UNKNOWN", "WAF 挑战页"
    if not b.startswith("{"):
        return "UNKNOWN", "非 JSON 响应: " + b[:60].replace("\n", " ")
    try:
        j = json.loads(body)
    except Exception:
        return "UNKNOWN", "JSON 解析失败"
    if isinstance(j, dict) and "statuses" in j:
        n = len(j.get("statuses") or [])
        if n > 0:
            return "VALID", f"statuses={n} total={j.get('total')}"
        return "INVALID", "page=2 返回空 statuses（该用户 page=2 必有数据）"
    if isinstance(j, dict) and any(k in j for k in ("error_code", "error_description", "error")):
        desc = str(j.get("error_description") or j.get("error") or "")
        code = j.get("error_code")
        # 400013 timeout_access_token 等：服务端明确判定 token 已失效
        if any(w in desc.lower() for w in ("timeout_access_token", "access_token",
                                           "invalid_token", "token_expired", "expired")):
            return "INVALID", "token 已失效: %s %s" % (code, desc[:60])
        if any(w in desc for w in ("登录", "login", "鉴权", "未授权", "权限")):
            return "INVALID", "需登录/鉴权失败: %s %s" % (code, desc[:60])
        if any(w in desc for w in ("频繁", "繁忙", "限流", "稍后", "重试")):
            return "UNKNOWN", "疑似限流: %s" % desc[:40]
        return "UNKNOWN", "其他错误: %s %s" % (code, desc[:60])
    return "UNKNOWN", "未知结构: " + ",".join(list(j.keys())[:6])


def check(cookie, attempts=3):
    """依次尝试候选 URL；UNKNOWN 时多轮重试，任一给出明确结论即返回。

    WAF 挑战页往往是瞬时/连续的，重试可避免把「碰上风控」误判成 cookie 状态。
    """
    last = "-"
    for attempt in range(attempts):
        for u in URLS:
            st, body = probe(u, cookie)
            verdict, why = classify(st, body)
            host = u.split("/")[2]
            if verdict != "UNKNOWN":
                return verdict, why, host
            last = f"{host}: {why}"
            time.sleep(0.8)
        if attempt < attempts - 1:
            time.sleep(3)
    return "UNKNOWN", last, "-"


def meta(cookie, idx):
    uid = re.search(r"(?:^|[;\s])u=(\d+)", cookie)
    tok = re.search(r"xq_a_token=([^;]+)", cookie)
    m = re.search(r"xq_id_token=([^;]+)", cookie)
    exp = None
    if m:
        try:
            pl = m.group(1).split(".")[1]
            pl += "=" * (-len(pl) % 4)
            exp = json.loads(base64.urlsafe_b64decode(pl)).get("exp")
        except Exception:
            pass
    d = (datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
         .strftime("%Y-%m-%d") if exp else "?")
    return "#%d uid=%s token=%s jwt_exp=%s" % (
        idx, uid.group(1) if uid else "?", tok.group(1)[:8] if tok else "?", d)


def network_self_test():
    """无 cookie 基准必须被接口「明确拒绝」，才证明该接口能区分登录态。

    - UNKNOWN -> 网络/WAF 异常，不可信
    - VALID   -> 接口无需登录，判据失效
    - INVALID -> 接口确实校验登录态，判据可靠
    """
    st, body = probe(f"https://api.xueqiu.com{PATH1}", None)
    v, why = classify(st, body)
    print(f"[自检] page=1 无 cookie 基准: {v} - {why}", flush=True)
    if v == "UNKNOWN":
        return False, "网络/WAF 异常，无法判定"
    if v == "VALID":
        return False, "该接口无需登录即可返回数据，无法用来区分 cookie 有效性"
    return True, "接口确实校验登录态"


def main():
    prune = "--prune" in sys.argv
    lines = [l.strip() for l in open(POOL, encoding="utf-8") if l.strip()]
    print(f"[池] 共 {len(lines)} 条 cookie\n", flush=True)

    ok, why = network_self_test()
    if not ok:
        print(f"\n[中止] {why}；为避免误判，不执行任何清除操作。", flush=True)
        return 2
    print(f"       基准结论：{why}\n", flush=True)

    # 正对照：刚更新的 my_xq_cookie.txt 是用户提供的最新登录 cookie。
    # 只有当它能被判为 VALID，才说明当前请求方式没被 WAF/指纹整体拦截，判定标准可信。
    pos_file = os.path.join(ROOT, "data", "my_xq_cookie.txt")
    if os.path.exists(pos_file):
        pos = open(pos_file, encoding="utf-8").read().strip()
        pv, pwhy, phost = check(pos)
        print(f"[正对照] my_xq_cookie.txt: {pv} [{phost}] {pwhy}", flush=True)
        if pv != "VALID":
            print("\n[中止] 连已知有效的新 cookie 都没通过校验 —— 当前直连方式被 WAF/指纹拦截，"
                  "\n       测试结果不可信，不能据此清理池。需改用 playwright 浏览器方式测试。",
                  flush=True)
            return 2
        print("       正对照通过，判定标准可信。\n", flush=True)

    results = []
    for i, c in enumerate(lines):
        verdict, why, host = check(c)
        results.append(verdict)
        print(f"  {meta(c, i)}\n     -> {verdict:7s} [{host}] {why}", flush=True)
        time.sleep(1.0)

    n_valid = results.count("VALID")
    n_invalid = results.count("INVALID")
    n_unknown = results.count("UNKNOWN")
    print(f"\n[汇总] VALID={n_valid}  INVALID={n_invalid}  UNKNOWN={n_unknown}", flush=True)

    if not prune:
        print("（诊断模式，未修改任何文件；加 --prune 才会清除 INVALID 条目）", flush=True)
        return 0

    if n_invalid == 0:
        print("[prune] 没有明确无效的条目，池保持不变。", flush=True)
        return 0

    bak = POOL + ".bak.preprune." + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(POOL, bak)
    kept = [c for c, v in zip(lines, results) if v != "INVALID"]
    with open(POOL, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    print(f"[prune] 已备份到 {os.path.basename(bak)}", flush=True)
    print(f"[prune] 移除 {n_invalid} 条 INVALID，池剩余 {len(kept)} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
