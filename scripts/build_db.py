#!/usr/bin/env python3
"""把归一化后的 data/posts.json 生成为 SQLite 数据库 data/xueqiu.db。

用途：
- 在 GitHub 上随仓库保留一份「真正的数据库」副本，可与本地保持一致；
- 便于用 SQL 直接分析全部发言（按时间、类型、互动量、股票关联等筛选）。

前端网页仍只用 data/posts_index.json + data/months/*，本文件不影响前端。
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTS = os.path.join(ROOT, "data", "posts.json")
DB = os.path.join(ROOT, "data", "xueqiu.db")


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def main():
    with open(POSTS, encoding="utf-8") as f:
        posts = json.load(f)

    if os.path.exists(DB):
        os.remove(DB)

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS posts")
    c.execute(
        """
        CREATE TABLE posts (
            id               INTEGER PRIMARY KEY,
            user_id          TEXT,
            created_at       INTEGER,
            created_date     TEXT,
            source           TEXT,
            type             TEXT,
            text             TEXT,
            url              TEXT,
            reply_count      INTEGER,
            like_count       INTEGER,
            retweet_count    INTEGER,
            fav_count        INTEGER,
            view_count       INTEGER,
            stock_correlation TEXT,
            mentioned        TEXT,
            tags             TEXT
        )
        """
    )

    # 评论 / 转发交互表（内嵌原文上下文）
    c.execute("DROP TABLE IF EXISTS interactions")
    c.execute(
        """
        CREATE TABLE interactions (
            id               INTEGER PRIMARY KEY,
            kind             TEXT,            -- comment / repost
            created_at       INTEGER,
            created_date     TEXT,
            text             TEXT,
            reply_to         TEXT,
            like_count       INTEGER,
            original_id      INTEGER,
            original_user    TEXT,
            original_text    TEXT,
            original_url     TEXT,
            stocks           TEXT,
            tags             TEXT
        )
        """
    )

    rows = []
    for p in posts:
        ca = p.get("created_at", 0) or 0
        # 雪球 created_at 是毫秒级时间戳，需 /1000 转成秒
        dt = datetime.fromtimestamp(ca / 1000, tz=timezone.utc) if ca else None
        rows.append(
            (
                p.get("id"),
                str(p.get("user_id", "")),
                ca,
                dt.strftime("%Y-%m-%d") if dt else "",
                p.get("source", ""),
                p.get("type", ""),
                p.get("text", ""),
                p.get("url", ""),
                to_int(p.get("reply_count")),
                to_int(p.get("like_count")),
                to_int(p.get("retweet_count")),
                to_int(p.get("fav_count")),
                to_int(p.get("view_count")),
                json.dumps(p.get("stockCorrelation", []), ensure_ascii=False),
                json.dumps(p.get("mentioned", []), ensure_ascii=False),
                json.dumps(p.get("tags", []), ensure_ascii=False),
            )
        )

    c.executemany(
        "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )

    # 互动（评论/转发）
    COMMENTS = os.path.join(ROOT, "data", "comments.json")
    REPOSTS = os.path.join(ROOT, "data", "reposts.json")
    irows = []
    for path in (COMMENTS, REPOSTS):
        if not os.path.exists(path):
            continue
        for it in json.load(open(path, encoding="utf-8")):
            ca = it.get("created_at", 0) or 0
            dt = datetime.fromtimestamp(ca / 1000, tz=timezone.utc) if ca else None
            orig = it.get("original") or {}
            irows.append((
                it.get("id"),
                it.get("kind", ""),
                ca,
                dt.strftime("%Y-%m-%d") if dt else "",
                it.get("text", ""),
                it.get("reply_to", ""),
                to_int(it.get("like_count")),
                orig.get("id"),
                orig.get("user", ""),
                orig.get("text", ""),
                orig.get("url", ""),
                json.dumps(it.get("stocks", []), ensure_ascii=False),
                json.dumps(it.get("tags", []), ensure_ascii=False),
            ))
    c.executemany(
        "INSERT OR REPLACE INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", irows
    )

    c.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON posts(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_type ON posts(type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_like ON posts(like_count)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_date ON posts(created_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_i_kind ON interactions(kind)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_i_created ON interactions(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_i_orig ON interactions(original_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_type ON posts(type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_like ON posts(like_count)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_date ON posts(created_date)")
    conn.commit()

    n = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    like_sum = c.execute("SELECT COALESCE(SUM(like_count),0) FROM posts").fetchone()[0]
    reply_sum = c.execute("SELECT COALESCE(SUM(reply_count),0) FROM posts").fetchone()[0]
    i_n = c.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    i_c = c.execute("SELECT COUNT(*) FROM interactions WHERE kind='comment'").fetchone()[0]
    i_r = c.execute("SELECT COUNT(*) FROM interactions WHERE kind='repost'").fetchone()[0]
    conn.close()

    size = os.path.getsize(DB)
    print(
        f"SQLite 构建完成: posts {n} 行 + interactions {i_n} 行 (评论 {i_c}/转发 {i_r}) "
        f"-> {DB} ({size/1024/1024:.2f} MB) | 赞合计 {like_sum:,} | 回复合计 {reply_sum:,}"
    )


if __name__ == "__main__":
    main()
