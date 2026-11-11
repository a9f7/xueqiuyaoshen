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
import time
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
    "my_holdings.enc.json",
    "my_trades.enc.json",
    "dashboard.json",
    "cn_macro_raw.csv",
    "cn_macro_factors.csv",
    "factor_history.json",
]


def run_git(args, cwd, timeout=180):
    env = dict(os.environ)
    # 绕过全局 git config 中可能失效的 http.proxy（如 socks5h://127.0.0.1:7890 未运行），
    # 直接用直连；经验证本沙箱直连 github.com 可用。
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.proxy"
    env["GIT_CONFIG_VALUE_0"] = ""
    # 同时清掉可能指向死代理的环境变量
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                           env=env, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: git {args[0]}"


def repo_is_valid():
    """实测 .git 是否为可用工作树，而不是只判断目录存在。

    历史事故：Temp/xq_deploy/.git 只剩 objects/refs/logs，缺 HEAD/config/index，
    os.path.isdir('.git') 仍为 True，导致 git ls-remote 报 origin 不存在、
    git_reachable() 恒判不可达，每轮退化到 Contents API 兜底。
    """
    git_dir = os.path.join(DEPLOY, ".git")
    if not os.path.isdir(git_dir):
        return False
    for required in ("HEAD", "config", "index"):
        if not os.path.exists(os.path.join(git_dir, required)):
            return False
    code, out = run_git(["rev-parse", "--is-inside-work-tree"], DEPLOY)
    return code == 0 and out.strip() == "true"


def ensure_repo():
    if repo_is_valid():
        # 已存在且有效：确保 remote 用 token，避免退回凭据管理器
        code, out = run_git(["remote", "set-url", "origin", AUTH_REPO], DEPLOY)
        if code != 0:
            print("remote set-url 失败：", redact(out))
            return False
        return True

    # 残缺或不存在：把旧 .git 整体移到备份目录（不删除，可回滚），再重新 clone。
    os.makedirs(os.path.dirname(DEPLOY), exist_ok=True)
    git_dir = os.path.join(DEPLOY, ".git")
    if os.path.isdir(git_dir):
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = os.path.join(os.path.dirname(DEPLOY), f"xq_deploy_git_broken_{stamp}")
        try:
            shutil.move(git_dir, backup)
            print(f"检测到残缺 .git，已备份到 {backup}（未删除，可回滚）")
        except OSError as e:
            print(f"备份残缺 .git 失败，中止以免破坏现场：{e}")
            return False

    # clone 要求目标目录为空。残缺 .git 移走后通常只剩 sync_files 建的空目录，
    # 逐个清掉；一旦发现任何真实文件立即中止，避免覆盖未知内容。
    if os.path.isdir(DEPLOY):
        leftovers = []
        for root, dirs, files in os.walk(DEPLOY, topdown=False):
            for f in files:
                leftovers.append(os.path.join(root, f))
        if leftovers:
            print(f"部署目录残留 {len(leftovers)} 个真实文件，中止以免覆盖："
                  f"{leftovers[:3]}")
            return False
        for root, dirs, files in os.walk(DEPLOY, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
    print(f"clone {redact(AUTH_REPO)} -> {DEPLOY}")
    code, out = run_git(["clone", "--depth", "1", "--single-branch", "--branch", "main",
                         AUTH_REPO, DEPLOY], cwd=os.path.dirname(DEPLOY), timeout=600)
    if code != 0:
        print("clone failed:\n", redact(out))
        return False
    run_git(["remote", "set-url", "origin", AUTH_REPO], DEPLOY)
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
              "requirements.txt", "CLOUDSTUDIO_SETUP_xueqiu.md",
              "factor_dashboard.html"]:
        s = os.path.join(WORKSPACE, f)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(DEPLOY, f))
    print("sync done.")


def find_conflict_files(paths=None):
    """扫描 deploy 工作树里含 git 冲突标记（<<<<<<< / >>>>>>>）的文本文件。

    2026-11-11 事故：pull --rebase --autostash 与远端同名文件冲突后，工作树残留
    冲突标记，脚本仍 add -A + commit，把 5 个带 <<<<<<< 的 JSON 推上线，
    前端 JSON.parse 直接崩。此函数用于提交前兜底检测。
    """
    # 只扫「数据文件」，不扫 .py/.js 等源码：本脚本自身的源码里就含有冲突标记
    # 字面量（用于检测），扫源码会必然误报。git 冲突标记成对出现，需同时命中。
    DATA_EXT = (".json", ".jsonl", ".csv", ".txt", ".md")
    if paths is None:
        paths = []
        for root, dirs, files in os.walk(DEPLOY):
            dirs[:] = [d for d in dirs if d != ".git"]
            for fn in files:
                if fn.endswith(DATA_EXT):
                    paths.append(os.path.relpath(os.path.join(root, fn), DEPLOY).replace(os.sep, "/"))
    hits = []
    for rel in paths:
        if not rel.endswith(DATA_EXT):
            continue
        p = os.path.join(DEPLOY, *rel.split("/"))
        try:
            if os.path.getsize(p) > 3_000_000:
                continue
            with open(p, "rb") as f:
                b = f.read()
        except OSError:
            continue
        if b"<<<<<<< " in b and b">>>>>>> " in b:
            hits.append(rel)
    return hits


def git_reachable():
    """探测 git 协议是否可达（本环境 github.com 常被 GFW reset）。

    用带 token 的绝对 URL 而非 'origin'：残缺仓库里 origin 无法解析会被误判成
    「网络不可达」，进而每轮错误地退化到 Contents API 兜底。
    """
    code, out = run_git(["ls-remote", "--heads", AUTH_REPO, "main"], DEPLOY, timeout=60)
    if code != 0:
        print("git 探测结果：", redact(out)[:200])
    return code == 0 and "main" in out


def push(message):
    if not ensure_repo():
        return False

    # 先探测 git 是否可达。不可达（GFW reset / 网络断）时跳过 git，直接走 Contents
    # API 兜底推送（按 1MB 上限推小文件），避免 pull --rebase 卡死 19+ 分钟。
    if not git_reachable():
        print("git 不可达（GFW/网络）-> 跳过 git，直接走 Contents API 兜底推送")
        return contents_api_fallback(message)

    # git 可达：先 fetch；若本地有悬挂提交(领先 origin/main)——例如本自动化或其他
    # 自动化留下的未推提交、或与 origin/main 已分叉——直接 reset --hard origin/main
    # 丢弃，避免 pull --rebase 与远端 300+ 提交冲突而卡死（交互式 rebase 等待输入）。
    run_git(["fetch", "origin"], DEPLOY)
    code_a, ahead = run_git(["rev-list", "--count", "origin/main..HEAD"], DEPLOY)
    if code_a == 0 and ahead.strip() not in ("", "0"):
        print(f"本地领先 origin/main {ahead.strip()} 个悬挂提交 -> reset --hard origin/main 丢弃，避免 rebase 冲突卡死")
        run_git(["reset", "--hard", "origin/main"], DEPLOY)

    # 2026-11-11 修复：sync 必须放在 pull 之后。
    # 旧顺序 sync -> pull --rebase --autostash 会把刚同步的本地未提交改动 stash 起来，
    # 而远端若同期也改了同名文件（另一条自动化也推 posts_index/tags_index/
    # analysis_recent），apply stash 冲突后工作树残留 <<<<<<< 标记，脚本仍
    # add -A + commit 把污染 JSON 推上线。先 pull 到干净 HEAD、再同步即可根治。
    code, out = run_git(["pull", "--rebase", "--autostash"], DEPLOY, timeout=300)
    print("pull:", redact(out))
    # 即便 pull 失败也继续（可能无远程更新），但最终 push 会校验

    # 双保险：pull 若留下未合并/冲突标记，先丢弃本地冲突态回到干净 HEAD，
    # 避免把冲突标记写进提交。
    code_u, unmerged = run_git(["diff", "--name-only", "--diff-filter=U"], DEPLOY)
    conflicts = find_conflict_files()
    if (code_u == 0 and unmerged.strip()) or conflicts:
        print("检测到 pull 冲突残留 (未合并 %d / 标记文件 %d) -> reset --hard 回到干净 HEAD"
              % (len(unmerged.split()), len(conflicts)))
        if conflicts:
            print("  冲突文件:", ", ".join(conflicts[:10]))
        run_git(["reset", "--hard", "HEAD"], DEPLOY)
        code_s, stash_list = run_git(["stash", "list"], DEPLOY)
        if code_s == 0 and "autostash" in stash_list:
            run_git(["stash", "drop"], DEPLOY)   # 丢弃过期的冲突 autostash，防止累积

    sync_files()

    # 提交前最后一道防线：同步后的工作树绝不能含冲突标记，否则中止本轮，
    # 保留现场让下轮（或人工）处理，绝不把坏 JSON 推上线。
    bad = find_conflict_files()
    if bad:
        print("ABORT: 同步后仍存在冲突标记文件，拒绝提交:", ", ".join(bad[:10]))
        return False

    code, out = run_git(["add", "-A"], DEPLOY)
    code, out = run_git(["status", "--porcelain"], DEPLOY)
    if not out.strip():
        # 没有文件变更，但仍可能有上一轮「提交成功却推送失败」的悬空 commit，补推一次
        code_u, out_u = run_git(["rev-list", "--count", "origin/main..HEAD"], DEPLOY)
        if code_u == 0 and out_u.strip() not in ("", "0"):
            print(f"no file changes, but {out_u.strip()} unpushed commit(s); pushing...")
            code, out = run_git(["push", "origin", "main"], DEPLOY, timeout=600)
            print("push:", redact(out))
            return code == 0
        print("no changes to commit.")
        return True

    code, out = run_git(["commit", "-m", message], DEPLOY)
    print("commit:", redact(out))
    if code != 0:
        return False

    code, out = run_git(["push", "origin", "main"], DEPLOY, timeout=600)
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
            # 推送失败（偶发网络/权限/代理不可达）：先尝试 Contents API 兜底小文件
            if contents_api_fallback(message):
                return True
            # 兜底也失败：保留本地提交，下一轮由 deploy_push 自行 retry
            print("PUSH FAILED - local commit kept, will retry next run.")
            return False
    return True


def contents_api_fallback(message):
    """git push 不可达时的兜底：用 GitHub Contents API 推送小文件（daily/analysis）。

    仅推送日常产物（daily_review.* / data/daily/* / analysis_recent.json），这些文件
    体积极小、远低于 Contents API 的 1MB 限制；大文件（posts.json / xueqiu.db 等）
    仍依赖 git，本兜底不碰。需要网络时（fetch）无法连通则跳过本地重置。
    """
    if not TOKEN:
        print("contents fallback: 无 token，跳过")
        return False
    script = os.path.join(HERE, "contents_push.py")
    if not os.path.exists(script):
        print("contents fallback: 脚本缺失，跳过")
        return False
    print("git push 失败 -> 尝试 Contents API 兜底（小文件）")
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    r = subprocess.run([sys.executable, script, "--message", message, "--no-reconcile"],
                       cwd=WORKSPACE, capture_output=True, text=True, env=env)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip())
    return r.returncode == 0


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
