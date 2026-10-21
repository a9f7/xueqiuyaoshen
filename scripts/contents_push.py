#!/usr/bin/env python3
"""GitHub Contents API 兜底推送（用于 git push 不可达时的小文件投递）。

背景：
- 本沙箱/特定环境下 git 直连 github.com 不可达（全局 http.proxy 指向已关闭的
  127.0.0.1:7890，且直连 github.com 返回 000），导致 deploy_push.py 的 git push
  失败、本地提交悬空。
- 但 api.github.com 可直接访问（HTTP 200），故用 Contents API（GET sha -> PUT
  base64）绕过 git pack，直接把文件写进 main 分支。
- 仅推送「小文件」：每日首席视角（daily_review.* / data/daily/*）与近15天面板
  （analysis_recent.json）。这些文件单个体积极小（几 KB），远低于 Contents API
  的 1MB 限制。大文件（posts.json / xueqiu.db 等）仍由 git 负责，本脚本不碰。

用法：
  python scripts/contents_push.py                 # 推送全部日常小文件
  python scripts/contents_push.py --date 2026-10-20   # 仅推送指定日期 daily 文件
  python scripts/contents_push.py --no-reconcile  # 推送后不尝试重置本地镜像
"""
import os
import sys
import json
import base64
import argparse
import subprocess
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, ".."))          # .../xueqiuyaoshen
DEPLOY = r"C:\Users\d\AppData\Local\Temp\xq_deploy"
REPO = "a9f7/xueqiuyaoshen"
TOKEN_FILE = os.path.join(WORKSPACE, ".github_token")

# 始终推送的核心小文件（每日任务产物 + 近15天面板）
CORE_FILES = [
    "data/daily_review.md",
    "data/daily_review.json",
    "data/analysis_recent.json",
]

# 除超大文件(posts.json/comments.json/interactions.json/xueqiu.db)外的全部 data 小文件，
# 这些均 <900KB，可走 Contents API 完整刷新网站主数据（当 git 直连被 GFW reset 时）。
# 网站前端只读分块数据(months/* + posts_index/tags_index) + 持仓(my_holdings.enc/
# my_trades.enc) + 面板(analysis_recent)，因此推这些即可让网站完整刷新；大文件由 git 负责。
SMALL_DATA_FILES = [
    "data/posts_index.json",
    "data/tags_index.json",
    "data/backtest.json",
    "data/dashboard.json",
    "data/my_holdings.enc.json",
    "data/my_trades.enc.json",
    "data/cn_macro_raw.csv",
    "data/cn_macro_factors.csv",
    "data/factor_history.json",
    "data/selfstock.json",
    "data/user.json",
]


def month_files():
    """返回 data/months 下所有分块文件（按月分块，单文件均 <900KB）。"""
    out = []
    mdir = os.path.join(WORKSPACE, "data", "months")
    if not os.path.isdir(mdir):
        return out
    for name in sorted(os.listdir(mdir)):
        p = os.path.join(mdir, name)
        if os.path.isfile(p):
            out.append(os.path.join("data", "months", name))
    return out


def load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    return os.environ.get("GITHUB_TOKEN", "").strip() or None


TOKEN = load_token()


def api(method, path, data=None):
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {TOKEN}",
        "User-Agent": "xq-contents-push",
        "Accept": "application/vnd.github+json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def push_file(rel, message):
    local = os.path.join(WORKSPACE, rel)
    if not os.path.exists(local):
        print(f"SKIP (missing): {rel}")
        return False
    with open(local, "rb") as f:
        raw = f.read()
    if len(raw) > 900 * 1024:
        print(f"SKIP (too large {len(raw)//1024}KB): {rel}")
        return False
    b64 = base64.b64encode(raw).decode("ascii")
    status, resp = api("GET", rel)
    sha = resp.get("sha") if status == 200 else None
    if status == 200:
        print(f"EXISTS {rel} sha={sha[:8] if sha else '?'}")
    elif status == 404:
        print(f"NEW   {rel}")
    else:
        print(f"GET {rel} -> {status}: {str(resp)[:160]}")
    payload = {"message": message, "content": b64, "branch": "main"}
    if sha:
        payload["sha"] = sha
    st, out = api("PUT", rel, payload)
    if st in (200, 201):
        print(f"OK    {rel} -> {st}")
        return True
    print(f"FAIL  {rel} -> {st}: {str(out)[:240]}")
    return False


def daily_files_for(date=None):
    """返回 data/daily 下要推送的文件（按日期筛选可选）。"""
    out = []
    ddir = os.path.join(WORKSPACE, "data", "daily")
    if not os.path.isdir(ddir):
        return out
    for name in sorted(os.listdir(ddir)):
        if not (name.endswith(".md") or name.endswith(".json")):
            continue
        # 若指定日期，仅推送该日期（YYYY-MM-DD.md / .json）
        if date and not name.startswith(date):
            continue
        out.append(os.path.join("data", "daily", name))
    return out


def reconcile_local_repo():
    """尝试把本地镜像重置到远端 main（需要网络 fetch，不可达时跳过）。"""
    if not os.path.isdir(os.path.join(DEPLOY, ".git")):
        return
    # 先取远端 main 的 sha（通过 API，不需要 git 网络）
    st, resp = api("GET", "", "")  # 列出根目录也可，但用 branches 更准
    # 用 branches API 取 main sha
    url = f"https://api.github.com/repos/{REPO}/branches/main"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {TOKEN}",
            "User-Agent": "xq-contents-push",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        remote_sha = data["commit"]["sha"]
    except Exception as e:  # noqa: BLE001
        print(f"reconcile: 无法取得远端 sha（{e}），跳过本地重置")
        return
    # fetch 远端（需要 git 网络；若不可达则 fetch 失败，跳过 reset）
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.proxy"
    env["GIT_CONFIG_VALUE_0"] = ""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    r = subprocess.run(["git", "fetch", "origin", remote_sha], cwd=DEPLOY,
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"reconcile: fetch 失败（{r.stderr.strip()[:120]}），跳过本地重置；"
              f"待网络恢复后由 deploy_push 自行 pull --rebase 处理")
        return
    rr = subprocess.run(["git", "reset", "--hard", remote_sha], cwd=DEPLOY,
                        capture_output=True, text=True, env=env)
    if rr.returncode == 0:
        print(f"reconcile: 本地镜像已重置到远端 main {remote_sha[:8]}")
    else:
        print(f"reconcile: reset 失败（{rr.stderr.strip()[:120]}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="仅推送该日期的 daily 文件，如 2026-10-20")
    ap.add_argument("--message", default=None)
    ap.add_argument("--no-reconcile", action="store_true")
    args = ap.parse_args()

    if not TOKEN:
        print("ERROR: 未找到 GitHub token（.github_token 或 GITHUB_TOKEN）")
        sys.exit(2)

    msg = args.message or f"chore: 每日首席视角总结 {args.date or 'latest'} (contents api)"
    files = list(CORE_FILES) + daily_files_for(args.date) + SMALL_DATA_FILES + month_files()
    print(f"== Contents API 兜底推送（{len(files)} 个文件）==")
    ok = 0
    for rel in files:
        if push_file(rel, msg):
            ok += 1
    print(f"== 完成：{ok}/{len(files)} 成功 ==")

    if not args.no_reconcile:
        reconcile_local_repo()
    sys.exit(0 if ok == len(files) else 1)


if __name__ == "__main__":
    main()
