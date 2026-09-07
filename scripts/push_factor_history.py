#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 GitHub Contents API 推送 data/factor_history.json 与 data/factor_history.jsonl 到
a9f7/xueqiuyaoshen 仓库 main 分支。工作区非 git 仓库，直接用 .github_token（public_repo PAT）。

安全边界：
- 仅推送这两个因子历史文件。
- data/my_holdings.json 为敏感文件（.gitignore 已忽略），绝不读取或推送。
"""
import os
import sys
import json
import base64
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, ".."))
TOKEN = open(os.path.join(WORKSPACE, ".github_token"), encoding="utf-8").read().strip()
REPO = "a9f7/xueqiuyaoshen"
BRANCH = "main"
FILES = [
    os.path.join(WORKSPACE, "data", "factor_history.json"),
    os.path.join(WORKSPACE, "data", "factor_history.jsonl"),
]
API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "curl/8",
    "Accept": "application/vnd.github+json",
}


def api_get(path):
    req = urllib.request.Request(f"{API}{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def api_put(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}", data=data, headers={**HEADERS, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def push_file(local_path):
    name = os.path.basename(local_path)
    api_path = f"data/{name}"
    content = open(local_path, "rb").read()
    b64 = base64.b64encode(content).decode("ascii")
    # 取远端 sha（不存在则无 sha，走新建）
    sha = None
    try:
        meta = api_get(f"/repos/{REPO}/contents/{api_path}?ref={BRANCH}")
        sha = meta.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sha = None
        else:
            raise
    payload = {
        "message": "chore: update macro factor snapshot",
        "content": b64,
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    print(f"[push] {name}: 远端sha={sha[:12] if sha else 'None(新建)'} 大小={len(content)}B")
    resp = api_put(f"/repos/{REPO}/contents/{api_path}", payload)
    print(f"[push] {name} -> commit {resp.get('commit', {}).get('sha', '?')[:12]} ({'更新' if sha else '新建'})")
    return resp.get("commit", {}).get("sha")


def main():
    if not TOKEN:
        print("[push] 缺少 .github_token，中止"); sys.exit(1)
    for f in FILES:
        if not os.path.exists(f):
            print(f"[push] 本地缺失 {f}，跳过"); continue
        try:
            push_file(f)
        except Exception as e:  # noqa: BLE001
            print(f"[push] {os.path.basename(f)} 失败: {repr(e)}")
            sys.exit(2)
    print("[push] 完成；my_holdings.json 未推送（敏感）")


if __name__ == "__main__":
    main()
