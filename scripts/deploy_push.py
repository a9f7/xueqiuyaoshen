#!/usr/bin/env python3
"""把工作区生成的数据同步到 GitHub Pages 仓库并推送。

背景 / 修复点：
- 工作区（本脚本所在项目的父目录）是「唯一权威工作目录」：scripts 在此运行、
  cookies 在此读取、raw_comments 爬取进度（gitignore，不入库）只在此累积。
- 推送目标仓库在 C:\\Users\\d\\AppData\\Local\\Temp\\xq_deploy（Windows 临时目录，
  不是 Git Bash 的 /tmp；历史上 /tmp/xq_deploy 不存在导致 git push 报
  "not a git repository"）。本脚本用绝对 Windows 路径，必要时自动 clone。
- 历史自动化缺一步「把生成的产物从工作区拷进仓库」，导致推上去的是陈旧数据。
  本脚本显式同步后再提交。

用法：python scripts/deploy_push.py [--message "chore: ..."]
"""
import os
import sys
import shutil
import subprocess
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(HERE, ".."))          # .../xueqiuyaoshen
DEPLOY = r"C:\Users\d\AppData\Local\Temp\xq_deploy"
REPO = "https://github.com/a9f7/xueqiuyaoshen.git"

# GitHub PAT：优先取环境变量 GITHUB_TOKEN，否则读本地 .github_token（gitignore，不入库）。
# 内嵌到 remote URL 以摆脱对 Windows 凭据管理器的依赖。
TOKEN_FILE = os.path.join(WORKSPACE, ".github_token")


def load_token():
    t = os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    return None


TOKEN = load_token()


def auth_repo(token):
    if not token:
        return REPO
    return REPO.replace("https://", f"https://{token}@", 1)


AUTH_REPO = auth_repo(TOKEN)


def redact(s):
    """打印前抹掉 token，避免泄露到日志/终端。"""
    if TOKEN:
        s = s.replace(TOKEN, "***TOKEN***")
    return s

# 需要同步的数据文件（不含 gitignore 的 raw_* / posts_raw.json / xq_cookie* / _*.json）
DATA_FILES = [
    "posts.json", "comments.json", "reposts.json", "interactions.json",
    "selfstock.json", "selfstock_raw.json", "posts_index.json",
    "tags_index.json", "analysis_recent.json", "user.json", "xueqiu.db",
    "daily_review.md", "daily_review.json", "backtest.json",
    "my_notes.json", "my_notes_backtest.json",
]


def run_git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def ensure_repo():
    if os.path.isdir(os.path.join(DEPLOY, ".git")):
        # 已存在：确保 remote 用 token，避免退回凭据管理器
        run_git(["remote", "set-url", "origin", AUTH_REPO], DEPLOY)
        return True
    os.makedirs(os.path.dirname(DEPLOY), exist_ok=True)
    print(f"clone {redact(AUTH_REPO)} -> {DEPLOY}")
    code, out = run_git(["clone", AUTH_REPO, DEPLOY], cwd=os.path.dirname(DEPLOY))
    if code != 0:
        print("clone failed:\n", redact(out))
        return False
    return True


def sync_files():
    dsrc = os.path.join(WORKSPACE, "data")
    ddst = os.path.join(DEPLOY, "data")
    os.makedirs(ddst, exist_ok=True)

    # 数据文件
    for f in DATA_FILES:
        s = os.path.join(dsrc, f)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(ddst, f))

    # 月份分块目录（整体镜像）
    ms = os.path.join(dsrc, "months")
    md = os.path.join(ddst, "months")
    if os.path.isdir(ms):
        shutil.copytree(ms, md, dirs_exist_ok=True)

    # 语料（可选，存在才同步）
    cs = os.path.join(dsrc, "corpus")
    if os.path.isdir(cs):
        shutil.copytree(cs, os.path.join(ddst, "corpus"), dirs_exist_ok=True)

    # 每日首席视角总结（可选，存在才同步；按日期累积归档）
    ds = os.path.join(dsrc, "daily")
    if os.path.isdir(ds):
        shutil.copytree(ds, os.path.join(ddst, "daily"), dirs_exist_ok=True)

    # scripts（排除 _*.py 与 __pycache__）
    ssrc = os.path.join(WORKSPACE, "scripts")
    sdst = os.path.join(DEPLOY, "scripts")
    os.makedirs(sdst, exist_ok=True)
    for name in os.listdir(ssrc):
        if name.startswith("_") or name == "__pycache__":
            continue
        if name.endswith(".py") or name.endswith(".sh"):
            shutil.copy2(os.path.join(ssrc, name), os.path.join(sdst, name))

    # 根目录文件（含云端运行所需：requirements.txt / 上云指南）
    for f in ["index.html", "summary.md", "README.md", ".gitignore",
              "requirements.txt", "CLOUDSTUDIO_SETUP_xueqiu.md"]:
        s = os.path.join(WORKSPACE, f)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(DEPLOY, f))
    print("sync done.")


def push(message):
    if not ensure_repo():
        return False
    sync_files()

    code, out = run_git(["pull", "--rebase", "--autostash"], DEPLOY)
    print("pull:", redact(out))
    # 即便 pull 失败也继续（可能无远程更新），但最终 push 会校验

    code, out = run_git(["add", "-A"], DEPLOY)
    code, out = run_git(["status", "--porcelain"], DEPLOY)
    if not out.strip():
        print("no changes to commit.")
        return True

    code, out = run_git(["commit", "-m", message], DEPLOY)
    print("commit:", redact(out))
    if code != 0:
        return False

    code, out = run_git(["push", "origin", "main"], DEPLOY)
    print("push:", redact(out))
    if code != 0:
        # 偶发网络：若 push 失败疑似 HTTPS 代理不可达，自动取消代理后重试一次
        proxy_keys = [k for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy") if k in os.environ]
        if proxy_keys:
            print(f"push failed with proxy {proxy_keys}; retry without proxy...")
            env_no_proxy = {k: v for k, v in os.environ.items() if k not in proxy_keys}
            r2 = subprocess.run(["git", "push", "origin", "main"], cwd=DEPLOY,
                                capture_output=True, text=True, env=env_no_proxy)
            out2 = (r2.stdout + r2.stderr).strip()
            print("push(no-proxy):", redact(out2))
            if r2.returncode == 0:
                return True
            out = out2
            code = r2.returncode
        if code != 0:
            # 推送失败（偶发网络/权限）：保留本地提交，下一轮补推
            print("PUSH FAILED - local commit kept, will retry next run.")
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default="chore: auto fetch")
    args = ap.parse_args()
    ok = push(args.message)
    # 验证线上已更新（raw 不缓存）
    print("verify: https://raw.githubusercontent.com/a9f7/xueqiuyaoshen/main/data/tags_index.json")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
