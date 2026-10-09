#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 GitHub Git Database API 推送 data/factor_history.json 与 data/factor_history.jsonl 到
a9f7/xueqiuyaoshen 仓库 main 分支。工作区非 git 仓库，直接用 .github_token（public_repo PAT）。

为什么用 Git Database API 而不是 Contents API：
- Contents API 的 GET 会内联返回整个旧文件的 base64 内容（当前已 >190KB），而本机到 api.github.com 的
  链路对大体积下载会被中间设备硬重置（IncompleteRead / ConnectionReset），导致取 sha 失败。
- Git Database 流程只上传新内容（blob），其余步骤（ref/commit/tree）都是极小 JSON，彻底绕开"下载旧文件"这一步。
  流程：get ref -> get commit tree -> create blobs -> create tree(基于旧 tree，仅替换两文件) -> create commit -> update ref。

安全边界：
- 仅推送这两个因子历史文件。
- data/my_holdings.json 为敏感文件（.gitignore 已忽略），绝不读取或推送。
"""
import os
import sys
import json
import base64
import time
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


def _request(method, path, payload=None, timeout=60):
    data = None
    headers = dict(HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        return json.loads(body) if body else {}


def _request_with_retry(method, path, payload=None, timeout=60, tries=4, label=""):
    last = None
    for i in range(tries):
        try:
            return _request(method, path, payload=payload, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                print(f"[push]   {label} 第{i+1}次失败({type(e).__name__})，{3*(i+1)}s 后重试")
                time.sleep(3 * (i + 1))
    raise last


def get_ref():
    d = _request_with_retry("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}", timeout=30, label="get_ref")
    return d["object"]["sha"]


def get_commit_tree(commit_sha):
    d = _request_with_retry("GET", f"/repos/{REPO}/git/commits/{commit_sha}", timeout=30, label="get_commit_tree")
    return d["tree"]["sha"]


def create_blob(content_bytes):
    b64 = base64.b64encode(content_bytes).decode("ascii")
    d = _request_with_retry("POST", f"/repos/{REPO}/git/blobs",
                            {"content": b64, "encoding": "base64"}, timeout=90, tries=6, label="create_blob")
    return d["sha"]


def create_tree(base_tree, entries):
    d = _request_with_retry("POST", f"/repos/{REPO}/git/trees",
                            {"base_tree": base_tree, "tree": entries}, timeout=60, tries=4, label="create_tree")
    return d["sha"]


def create_commit(tree_sha, parent_sha, message):
    d = _request_with_retry("POST", f"/repos/{REPO}/git/commits",
                            {"message": message, "tree": tree_sha, "parents": [parent_sha]},
                            timeout=60, tries=4, label="create_commit")
    return d["sha"]


def update_ref(commit_sha):
    d = _request_with_retry("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}",
                            {"sha": commit_sha, "force": False}, timeout=60, tries=4, label="update_ref")
    return d["object"]["sha"]


def push_files():
    parent_commit = get_ref()
    print(f"[push] 父提交 {parent_commit[:12]}")
    base_tree = get_commit_tree(parent_commit)
    entries = []
    for f in FILES:
        name = os.path.basename(f)
        content = open(f, "rb").read()
        blob_sha = create_blob(content)
        entries.append({"path": f"data/{name}", "mode": "100644",
                       "type": "blob", "sha": blob_sha})
        print(f"[push] {name}: blob 创建完成 大小={len(content)}B")
    new_tree = create_tree(base_tree, entries)
    new_commit = create_commit(new_tree, parent_commit, "chore: update macro factor snapshot")
    update_ref(new_commit)
    print(f"[push] 新提交 {new_commit[:12]} 已更新 {BRANCH}（基于父提交 {parent_commit[:12]}）")


def main():
    if not TOKEN:
        print("[push] 缺少 .github_token，中止")
        sys.exit(1)
    for f in FILES:
        if not os.path.exists(f):
            print(f"[push] 本地缺失 {f}，跳过")
            return
    # 整条流水线外层重试（应对链路临时大流量阻断：10054 / IncompleteRead）
    for attempt in range(6):
        try:
            push_files()
            break
        except Exception as e:  # noqa: BLE001
            if attempt < 5:
                print(f"[push] 第{attempt+1}次整体失败({type(e).__name__})，20s 后整体重试")
                time.sleep(20)
            else:
                print(f"[push] 失败: {repr(e)}")
                sys.exit(2)
    print("[push] 完成；my_holdings.json 未推送（敏感）")


if __name__ == "__main__":
    main()
