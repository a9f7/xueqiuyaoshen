# 雪球药神 · metalslime 发言归档

> 雪球博主 [metalslime](https://xueqiu.com/u/2292705444)（药神）发言归档与搜索站点。
> 数据每 12 小时由 GitHub Action 自动增量更新。

## 在线访问

- GitHub Pages: https://a9f7.github.io/xueqiuyaoshen/

## 功能

- 博主信息卡 + 收录统计
- 时间线展示（按时间倒序）
- 全文搜索（内容 / 股票代码 / @提及）
- 来源 / 类型筛选
- 股票板块标签
- 长文折叠展开
- 原帖链接跳转

## 目录结构

```
xueqiuyaoshen/
├── index.html                    # 单页应用
├── data/
│   ├── posts.json                # 归一化后的发言列表
│   ├── posts_raw.json            # 原始去重后的发言
│   ├── user.json                 # 博主元信息
│   └── raw/
│       └── page_{N}.json         # 每页原始 API 响应
├── scripts/
│   ├── fetch_xueqiu.py           # 抓取脚本（GitHub Action 调用）
│   ├── normalize_posts.py        # 合并去重 + 归一化
│   └── summarize.py              # 生成 markdown 摘要
└── .github/workflows/
    └── fetch-and-publish.yml     # 每 12 小时抓取 + 部署
```

## 自动更新

GitHub Action（`.github/workflows/fetch-and-publish.yml`）每 12 小时（UTC 0:00 / 12:00）执行：

1. 用 `XQ_COOKIE` secret 抓取最新 5 页（约 100 条）
2. 与 `data/raw/` 已有数据合并去重
3. 重新生成 `posts.json` / `user.json` / `summary.md`
4. commit 并推送
5. 部署到 GitHub Pages

## 本地运行

```bash
pip install requests
python scripts/fetch_xueqiu.py --pages 5        # 需要 XQ_COOKIE 环境变量
python scripts/normalize_posts.py              # 合并归一化
python scripts/summarize.py > summary.md      # 生成摘要
```

## 数据来源

- 雪球 user_timeline API：`/v4/statuses/user_timeline.json?user_id=2292705444&page=N&type=0&count=20`
- page=1 公开可访问；page≥2 需登录 cookies（通过 `XQ_COOKIE` secret 注入）

## 许可

数据归雪球网与博主本人所有，本项目仅用于个人跟踪研究。
