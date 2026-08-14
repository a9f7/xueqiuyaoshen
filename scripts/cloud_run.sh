#!/usr/bin/env bash
# ============================================================================
#  CloudStudio 云端运行器（xueqiuyaoshen 雪球药神监控）
#  复刻本地三套自动化任务，全部依赖环境变量，不写死任何本地路径。
#  设计目标：在 CloudStudio 工作区（Ubuntu + 系统 python3）里跑，结果推 GitHub Pages。
#
#  必须预设的环境变量：
#    GH_TOKEN        GitHub PAT（public_repo 即可，用于 deploy_push 推仓库）
#    XQ_COOKIE       雪球 cookie 单条（可选；若工作区 data/xq_cookies.txt 已上传则优先用文件多组轮换）
#    XQ_CHROME_PATH  留空("")即可改用 playwright 自带的 chromium（本脚本自动安装）
#
#  用法：
#    bash scripts/cloud_run.sh latest     # 追最新发帖+评论（类比每小时任务）
#    bash scripts/cloud_run.sh history    # 历史评论回填一小批（类比每12h任务）
#    bash scripts/cloud_run.sh daily      # 每日首席视角总结（类比每日09:30任务）
#    bash scripts/cloud_run.sh all        # 依次跑 history -> latest -> daily
#
#  注意：CloudStudio 免费版会空闲休眠、有月时长上限，不能 7x24 靠 crontab。
#        要"常驻"，用 nohup 循环（见 CLOUDSTUDIO_SETUP_xueqiu.md 第三节）。
# ============================================================================
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-latest}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOG_DIR/cloud_run_$STAMP.log"
exec > >(tee -a "$LOG") 2>&1

echo "==================== [$(date)] cloud_run start (mode=$MODE) ===================="
echo "GH_TOKEN = ${GH_TOKEN:+set}"
echo "XQ_COOKIE = ${XQ_COOKIE:+set}"

# --- 0. 准备 python + playwright -------------------------------------------
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "ERROR: 找不到 python3，请在 CloudStudio 工作区安装 Python >=3.10"
  exit 1
fi
echo "PYTHON = $($PY --version 2>&1)"

# 依赖：仅首次或 requirements 变更时安装
if [ ! -f "$ROOT/.venv_ready" ] || [ -n "$(find requirements.txt -newer "$ROOT/.venv_ready" 2>/dev/null)" ]; then
  echo "[$(date)] pip install playwright ..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
  "$PY" -m playwright install --with-deps chromium 2>/dev/null || "$PY" -m playwright install chromium
  touch "$ROOT/.venv_ready"
fi

# 云端用 playwright 自带 chromium（不设则用默认 Windows 路径会找不到）
export XQ_CHROME_PATH="${XQ_CHROME_PATH:-}"

# --- 1. 清理残留（云端 chromium 进程） --------------------------------------
"$PY" scripts/cleanup_runtime.py 2>/dev/null || true

# --- 2. 分模式执行 ---------------------------------------------------------
run_latest() {
  echo "---- [latest] 抓最新发帖/评论 (--newest 8) ----"
  "$PY" scripts/fetch_xueqiu.py --mode posts    --newest 8
  "$PY" scripts/fetch_xueqiu.py --mode comments --newest 8
  "$PY" scripts/normalize_posts.py
  "$PY" scripts/normalize_interactions.py
  "$PY" scripts/tag_posts.py
  "$PY" scripts/split_posts.py          # 末尾自动调 analyze_recent 刷新近15天面板
  "$PY" scripts/build_db.py
  "$PY" scripts/deploy_push.py --message "chore: cloud latest 抓取+分析 $STAMP"
}

run_history() {
  echo "---- [history] 历史评论回填小批 (--batch 30) ----"
  "$PY" scripts/fetch_xueqiu.py --mode comments --batch 30
  "$PY" scripts/normalize_posts.py
  "$PY" scripts/normalize_interactions.py
  "$PY" scripts/tag_posts.py
  "$PY" scripts/split_posts.py
  "$PY" scripts/build_db.py
  "$PY" scripts/deploy_push.py --message "chore: cloud history 回填 $STAMP"
}

run_daily() {
  echo "---- [daily] 每日首席视角总结 ----"
  "$PY" scripts/daily_review.py
  "$PY" scripts/analyze_recent.py --days 15
  "$PY" scripts/deploy_push.py --message "chore: cloud daily 总结 $STAMP"
}

case "$MODE" in
  latest)  run_latest  ;;
  history) run_history ;;
  daily)   run_daily   ;;
  all)
    run_history
    run_latest
    run_daily
    ;;
  *)
    echo "ERROR: 未知模式 '$MODE'，请用 latest|history|daily|all"
    exit 1
    ;;
esac

echo "==================== [$(date)] cloud_run done (exit $?) ===================="
