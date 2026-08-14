# 雪球药神监控 · 云端运行方案（CloudStudio）

目标：**爬取 / 分析 / 部署全部在云端跑，不再依赖本机 WorkBuddy 自动化任务**。
本地现有自动化暂不动（双轨并行，跑通并验证后再决定是否停用本机任务）。

> 为什么选 CloudStudio：腾讯云（国内/新加坡）数据中心 IP，比 GitHub Actions 的美国 IP
> 访问雪球更稳；且是持久化 Linux 工作区（不像 Actions 每次 fresh），适合长期滚动爬取。
> 它不能定时（免费版会休眠），因此采用「CloudStudio 工作区当常驻云端主机 + 工作区内 nohup 循环/保活」。

---

## 一、前置：GitHub Token（推送公开仓库）

管线末尾 `deploy_push.py` 用 `GH_TOKEN` 把数据/网页推到 `a9f7/xueqiuyaoshen`。
仓库是公开仓库，`public_repo` 范围即可（**不需要** `workflow` 范围，我们不用 Actions）。

- 现有 PAT 已可用（public_repo 范围，形如 `github_pat_xxx` 或 `ghp_xxx`，请填你自己的）。
- 也可在 GitHub → Settings → Developer settings → PAT 新建一个，范围勾 `public_repo`。

---

## 二、在 CloudStudio 建一个常驻工作区

1. 打开 https://ide.cloud.tencent.com/ ，用你的腾讯云账号（微信/QQ）登录。
2. 新建工作区 → **关联仓库** `https://github.com/a9f7/xueqiuyaoshen`；
   或建空白工作区后 `git clone https://github.com/a9f7/xueqiuyaoshen.git` 再 `cd xueqiuyaoshen`。
3. 进工作区终端，确认 Python：
   ```bash
   python3 --version   # 需 >= 3.10
   ```

> 历史参考：之前「机票监控(ticketmonitor)」项目已在 CloudStudio 跑通过整套方案
> （`~/.workbuddy/cloudstudio-deploy-history/` 里有其工作空间标识
> workspaceKey=1b1dbff6aa4ff496，可复用同一账号下的工作区配额）。

---

## 三、配置运行（三种模式）

云端入口脚本 `scripts/cloud_run.sh` 已就绪，复刻了本地三套任务：

| 模式 | 对应本地任务 | 做什么 |
|------|-------------|--------|
| `latest`  | 每小时最新抓取+分析 | 抓最新发帖/评论(--newest 8) → 归一化/标签/分块/分析 → 推 GitHub |
| `history` | 每12h历史回填 | 历史评论回填一小批(--batch 30) → 同上 |
| `daily`   | 每日09:30总结 | 每日首席视角总结 + 重算近15天面板 → 推 GitHub |
| `all`     | — | 依次 history → latest → daily |

首次运行会自动 `pip install playwright` + `playwright install chromium`，无需手动配环境。

### 方式 A：手动跑一把（推荐先验证）
```bash
cd xueqiuyaoshen
export GH_TOKEN="ghp_你的token"          # 用于推送
# cookie：二选一
#   (1) 把本地 data/xq_cookies.txt 整文件上传到工作区 data/ 下（多组轮换，推荐）
#   (2) 或设单条： export XQ_COOKIE="你的雪球cookie字符串"
bash scripts/cloud_run.sh latest
```

### 方式 B：nohup 循环保活（免费版变通）
CloudStudio 免费版空闲会休眠、有月时长上限，crontab 不可靠。用常驻循环代替：
```bash
# 每 1 小时跑一次 latest；崩溃自动重启；日志落 logs/
nohup bash -c 'while true; do bash scripts/cloud_run.sh latest; sleep 3600; done' >> logs/loop.log 2>&1 &
# 每天跑一次 daily（另开一个循环，或合并进上面脚本）
nohup bash -c 'while true; do bash scripts/cloud_run.sh daily; sleep 86400; done' >> logs/daily.log 2>&1 &
# history 回填：手动跑，或也放循环里（注意 WAF 限流，别太频繁）
```
> 注意：免费版月在线时长有限，长期 nohup 会快速耗尽额度。若想真 7x24 自动，
> 把该工作区设为「常驻/不自动休眠」套餐（付费），或改用国内轻量 ECS（≈200元/年）。

---

## 四、cookie 怎么上云（重要）

`data/xq_cookies.txt` / `data/xq_cookie.txt` 在 `.gitignore` 里，**不会**随仓库克隆到云端。
两种上云方式：

1. **推荐**：在 CloudStudio 工作区终端，把本地 cookie 文件内容粘贴/上传到
   `xueqiuyaoshen/data/xq_cookies.txt`（多行多组，fetch 自动轮换）。工作区持久化，重启不丢。
2. 单次单条：设环境变量 `XQ_COOKIE="..."`（fetch 已支持，但只有一组，限流时无法切换）。

> cookie 是敏感凭据，工作区只你自己登录的腾讯云账号可见；不要写进仓库或提交 git。

---

## 五、验证清单

- [ ] 工作区 `python3 --version` >= 3.10
- [ ] `GH_TOKEN` 已设且 `echo $GH_TOKEN` 可见
- [ ] `data/xq_cookies.txt` 已上传（或多组 cookie 就绪）
- [ ] 手动跑 `bash scripts/cloud_run.sh latest`，观察日志：fetch 有新增、push 成功
- [ ] 浏览器打开 https://a9f7.github.io/xueqiuyaoshen/ 看到最新数据 + 近15天面板刷新
- [ ] （可选）nohup 循环进程在跑

---

## 六、与本地现状的关系

- 本机 WorkBuddy 的三套自动化任务**暂保留不动**，与云端可并行（fetch 有单实例锁，同一时刻只一个在抓，互不冲突）。
- 云端跑通并连续验证几天后，可在 WorkBuddy 里把本机三套任务暂停/删除，彻底脱离本机。
- 之前为清理本机「自动化收件箱绿点」建的两条 Windows 计划任务（WorkBuddyArchiveAutomationRuns*
  每天 03:00/15:00）在停用本机自动化后自然无事可做，可保留无害或删除。
