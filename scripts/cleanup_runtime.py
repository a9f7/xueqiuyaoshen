#!/usr/bin/env python3
"""清理雪球爬取运行时残留进程（供自动化每轮开头调用）。

解决的问题：
- 历史自动化曾并发启动多个 fetch_xueqiu.py，互相抢占 cookie 池 / WAF 预算并写同一
  raw_comments 目录，导致账号被风控、渲染器崩溃、进程静默死退出。
- 长会话 playwright 偶发遗留孤儿 chromium 进程，累积耗尽内存。

本脚本只清理「本项目相关的」进程：
1. 命令含 fetch_xueqiu 的 python 进程（确保本轮只有一个 fetch 在跑，不并发）。
2. 可执行路径含 ms-playwright 的 chrome.exe（孤儿浏览器；不会误杀用户真实 Chrome）。
"""
import os
import sys
import subprocess

SELF_PID = os.getpid()


def run_powershell(script: str) -> str:
    """在 Windows 上执行一段 PowerShell，返回 stdout。非 Windows 直接返回空。"""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=120,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"powershell error: {e}"


def main():
    ps = r"""
$log = @()
# 1) 杀掉残留的 fetch_xueqiu 进程（排除自身）
$fetches = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match 'python' -and $_.CommandLine -match 'fetch_xueqiu' -and $_.ProcessId -ne $pid
}
foreach ($p in $fetches) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $log += "killed fetch PID $($p.ProcessId) started $($p.CreationDate)"
}
# 2) 只杀 ms-playwright 的孤儿 chrome（不碰用户真实 Chrome）
$pw = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'chrome.exe' -and $_.ExecutablePath -match 'ms-playwright'
}
foreach ($p in $pw) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
$log += "killed playwright chrome: $($pw.Count)"
$log += "remaining playwright chrome: $((Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.ExecutablePath -match 'ms-playwright' }).Count)"
$log += "remaining fetch procs: $((Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'fetch_xueqiu' }).Count)"
$log -join "`n"
"""
    out = run_powershell(ps)
    print(out or "(non-windows or no output)")
    print("cleanup_runtime done.")


if __name__ == "__main__":
    main()
