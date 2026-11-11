#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/factor_history.json 与 data/factor_history.jsonl 推送到 a9f7/xueqiuyaoshen@main。

工作区非 git 仓库，直接用 .github_token（public_repo PAT）。默认走 GitHub Contents API：

- 先用 git trees API（只返回路径+blob sha，极小 JSON）拿到远端文件 sha，避免 Contents API
  的 GET 内联返回整个旧文件 base64（当前 json >200KB），从而绕开本机链路上对
  ≥8KB 下载的中间设备重置。
- 再对每个文件 PUT /contents/<path>（带 sha 更新 / 远端不存在则新建，提交信息
  "chore: update macro factor snapshot"）。
- 推送后重新取一次 tree sha，与本地 git blob sha 逐文件比对校验。

`--git-db` 可切回旧的 Git Database API 流程（get ref -> blobs -> tree -> commit -> update ref），
用于 Contents API 上传被阻断时兜底。

安全边界：
- 仅推送这两个因子历史文件。
- data/my_holdings.json 为敏感文件（.gitignore 已忽略），绝不读取或推送。
"""
import os
import sys
import json
import base64
import hashlib
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, ".."))
TOKEN = open(os.path.join(WORKSPACE, ".github_token"), encoding="utf-8").read().strip()
REPO = "a9f7/xueqiuyaoshen"
BRANCH = "main"
MESSAGE = "chore: update macro factor snapshot"
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


def blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def local_shas(raw):
    """远端 blob 可能被 git 做过 CRLF->LF 规范化，两种字节都算"未变化"。"""
    return {blob_sha(raw), blob_sha(raw.replace(b"\r\n", b"\n"))}


def remote_blob_shas():
    d = _request_with_retry("GET", f"/repos/{REPO}/git/trees/{BRANCH}?recursive=1",
                            timeout=45, label="get_tree")
    if d.get("truncated"):
        raise RuntimeError("tree API 返回 truncated，无法一次性取全 sha")
    return {e["path"]: e["sha"] for e in d.get("tree", []) if e.get("type") == "blob"}


# ---------- Contents API（默认） ----------

def push_via_contents():
    shas = remote_blob_shas()
    print(f"[push] tree API 取回 {len(shas)} 个远端 blob sha")
    results = []
    for f in FILES:
        name = os.path.basename(f)
        rel = f"data/{name}"
        raw = open(f, "rb").read()
        sha = shas.get(rel)
        if sha in local_shas(raw):
            print(f"[push] {name}: 远端已与本地一致（{len(raw)}B，sha {sha[:12]}），无需提交")
            results.append((name, "same"))
            continue
        payload = {
            "message": MESSAGE,
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": BRANCH,
        }
        if sha:
            payload["sha"] = sha
        else:
            print(f"[push] {name}: 远端不存在，将新建")
        d = _request_with_retry("PUT", f"/repos/{REPO}/contents/{rel}", payload,
                                 timeout=120, tries=5, label=f"put {name}")
        commit = (d.get("commit") or {}).get("sha", "?")[:12]
        print(f"[push] {name}: PUT 完成，大小={len(raw)}B，commit {commit}")
        results.append((name, "pushed"))
    return results


def verify_via_contents():
    shas = remote_blob_shas()
    ok = True
    for f in FILES:
        name = os.path.basename(f)
        rel = f"data/{name}"
        raw = open(f, "rb").read()
        hit = shas.get(rel) in local_shas(raw)
        print(f"[push] 校验 {name}: 远端 sha={str(shas.get(rel))[:12]} "
              f"本地 sha={blob_sha(raw)[:12]} -> {'一致' if hit else '不一致'}")
        ok = ok and hit
    return ok


# ---------- Git Database API（--git-db 兜底） ----------

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


def push_via_git_db():
    parent_commit = get_ref()
    print(f"[push] 父提交 {parent_commit[:12]}")
    base_tree = get_commit_tree(parent_commit)
    entries = []
    for f in FILES:
        name = os.path.basename(f)
        content = open(f, "rb").read()
        blob = create_blob(content)
        entries.append({"path": f"data/{name}", "mode": "100644", "type": "blob", "sha": blob})
        print(f"[push] {name}: blob 创建完成 大小={len(content)}B")
    new_tree = create_tree(base_tree, entries)
    new_commit = create_commit(new_tree, parent_commit, MESSAGE)
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

    use_git_db = "--git-db" in sys.argv
    for attempt in range(4):
        try:
            if use_git_db:
                push_via_git_db()
            else:
                push_via_contents()
                if not verify_via_contents():
                    raise RuntimeError("Contents API 推送后远端 blob sha 与本地不一致")
            print("[push] 完成；my_holdings.json 未推送（敏感）")
            return
        except Exception as e:  # noqa: BLE001
            if attempt < 3:
                print(f"[push] 第{attempt+1}次整体失败({type(e).__name__}: {str(e)[:120]})，20s 后整体重试")
                time.sleep(20)
            else:
                print(f"[push] 失败: {repr(e)}")
                if not use_git_db:
                    print("[push] 可尝试 --git-db 走 Git Database API 兜底")
                sys.exit(2)


if __name__ == "__main__":
    main()
