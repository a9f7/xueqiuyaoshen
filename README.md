# 雪球药神 metalslime 全量发言归档

自动化监控 metalslime（雪球 ID: 2292705444）的所有发言，部署在 GitHub Pages。

## 📊 数据概况

- **博主**：metalslime（雪球药神）
- **主页**：<https://xueqiu.com/u/2292705444>
- **状态**：24.5 万粉丝，3 万+ 发言
- **归档覆盖**：约 170 页 / 3297 条最新发言（2025-07 ~ 2026-08）

## 🌐 在线访问

<https://a9f7.github.io/xueqiuyaoshen/>

## 🛠️ 数据结构

```
xueqiuyaoshen/
├── index.html          # 单页应用（时间线 + 搜索 + 筛选）
├── data/
│   ├── posts.json      # 归一化后的发言列表
│   ├── posts_raw.json  # 雪球 API 原始数据
│   ├── user.json       # 博主元信息
│   └── raw/page_*.json # 原始分页数据
├── scripts/
│   ├── fetch_xueqiu.py # playwright 反检测抓取
│   ├── normalize_posts.py
│   └── summarize.py
└── summary.md          # 最新发言摘要
```

## 🔄 自动化更新

每天 8:30 + 20:30 由 WorkBuddy automation 触发：
1. 抓取雪球 page=1..5
2. 合并去重 + 归一化
3. 重新生成 `index.html` 数据
4. 推送到 GitHub → 触发 GitHub Pages 部署
5. 通过企业微信推送摘要

## 🔐 抓取原理

雪球 API `v4/statuses/user_timeline.json` 在 `page>=2` 时要求登录。
- **page=1**：公开，无需 cookies
- **page>=2**：需要登录 cookies，且 WAF 会做浏览器指纹判定

### Cookies 注入

1. 浏览器登录 <https://xueqiu.com>
2. F12 → Network → 任意请求 → 复制 `Cookie` 头
3. 写入环境变量 `XQ_COOKIE`
4. 运行：
   ```bash
   XQ_COOKIE="cookiesu=...; xq_a_token=..." \
   python scripts/fetch_xueqiu.py --pages 1-200
   ```

### WAF 反检测

脚本使用 `playwright` + `chromium`，自动注入反检测脚本：
- 移除 `navigator.webdriver`
- 补 `window.chrome` / `navigator.plugins` / `navigator.languages`
- 完整 1920x1080 视口 + `zh-CN` locale + `Asia/Shanghai` timezone

## 📈 已知限制

- 雪球 API 限流：连续抓 ~170 页后返回 HTTP 405，需等待 30+ 分钟
- cookies 过期：用户重新登录后需更新 `XQ_COOKIE`
- 全量 16194 条发言（810 页）需多次抓取累积
