import json
import os
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .scraper import strip_media_links

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    user_id INTEGER,
    display_name TEXT,
    interval_seconds INTEGER NOT NULL DEFAULT 43200,
    active INTEGER NOT NULL DEFAULT 1,
    last_seen_tweet_id INTEGER,
    last_poll_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tweets (
    id INTEGER PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id),
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content TEXT NOT NULL,
    lang TEXT,
    reply_count INTEGER,
    retweet_count INTEGER,
    like_count INTEGER,
    quote_count INTEGER,
    view_count INTEGER,
    media TEXT,
    raw_json TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tweets_monitor ON tweets(monitor_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_username ON tweets(username, id DESC);

CREATE TABLE IF NOT EXISTS platform_monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    label TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    last_poll_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, creator_id)
);

CREATE TABLE IF NOT EXISTS platform_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    monitor_id INTEGER NOT NULL REFERENCES platform_monitors(id),
    content_id TEXT NOT NULL,
    creator_hash TEXT,
    title TEXT,
    content TEXT,
    created_at TEXT,
    image_urls TEXT,
    video_url TEXT,
    cover_url TEXT,
    work_url TEXT,
    stats TEXT,
    raw_json TEXT,
    inserted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, content_id)
);

CREATE INDEX IF NOT EXISTS idx_pp_monitor_created ON platform_posts(monitor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pp_platform_created ON platform_posts(platform, created_at DESC);

CREATE TABLE IF NOT EXISTS autoup_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    monitor_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, canonical_key),
    UNIQUE(platform, monitor_id)
);

CREATE TABLE IF NOT EXISTS autoup_subscriptions (
    competitor_id TEXT PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES autoup_targets(id),
    display_name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_autoup_subscriptions_target
ON autoup_subscriptions(target_id, enabled);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def media_from_raw_json(raw: str | None) -> dict[str, Any] | None:
    """从已入库的 raw_json（twscrape `Tweet.json()` 序列化的 dataclass）解析出 media 结构。

    新推文在抓取时已把 `tweet.media` 提取成同样的结构存进 media 列；
    这里用于**回填老数据**（media 列为空的历史推文），保证旧推文也能显示图片。
    """
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except Exception:
        return None
    m = doc.get("media")
    if not isinstance(m, dict):
        return None
    out: dict[str, Any] = {}
    photos = [
        p.get("url") for p in m.get("photos", []) if isinstance(p, dict) and p.get("url")
    ]
    if photos:
        out["photos"] = photos
    videos = []
    for v in m.get("videos", []):
        if not isinstance(v, dict):
            continue
        videos.append(
            {
                "cover": v.get("thumbnailUrl"),
                "duration_ms": v.get("duration"),
                "views": v.get("views"),
                "urls": [
                    var.get("url")
                    for var in v.get("variants", [])
                    if isinstance(var, dict) and var.get("url")
                ],
            }
        )
    if videos:
        out["videos"] = videos
    gifs = [
        {"cover": a.get("thumbnailUrl"), "url": a.get("videoUrl")}
        for a in m.get("animated", [])
        if isinstance(a, dict) and a.get("thumbnailUrl")
    ]
    if gifs:
        out["gifs"] = gifs
    return out or None


def clean_content_from_raw_json(raw: str | None) -> str | None:
    """从 raw_json（twscrape `Tweet.json()` 序列化）重算正文：去掉媒体短链。

    用于回填老推文——它们入库时还没这个逻辑，正文里还留着 `https://t.co/xxx`。
    与抓取时的 `tweet_to_dict` 用同一套 `strip_media_links`，保证新老一致。
    """
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except Exception:
        return None
    content = doc.get("rawContent")
    if not content:
        return None
    return strip_media_links(content, doc.get("links") or [])


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        # 轻量迁移：老库的 monitors 表补 created_by 列（记录谁创建的监控）
        cols = {row[1] for row in await (await self._conn.execute("PRAGMA table_info(monitors)")).fetchall()}
        if "created_by" not in cols:
            await self._conn.execute("ALTER TABLE monitors ADD COLUMN created_by TEXT")
            await self._conn.commit()
        if "last_success_at" not in cols:
            await self._conn.execute("ALTER TABLE monitors ADD COLUMN last_success_at TEXT")
            await self._conn.execute(
                "UPDATE monitors SET last_success_at = last_poll_at "
                "WHERE last_error IS NULL"
            )
            await self._conn.commit()
        # 轻量迁移：tweets 表补 media 列（推文图片/视频直链），并回填老数据
        tcols = {row[1] for row in await (await self._conn.execute("PRAGMA table_info(tweets)")).fetchall()}
        if "media" not in tcols:
            await self._conn.execute("ALTER TABLE tweets ADD COLUMN media TEXT")
            await self._conn.commit()
        if "updated_at" not in tcols:
            await self._conn.execute("ALTER TABLE tweets ADD COLUMN updated_at TEXT")
            await self._conn.execute(
                "UPDATE tweets SET updated_at = inserted_at WHERE updated_at IS NULL"
            )
            await self._conn.commit()
        cur = await self._conn.execute("SELECT id, raw_json FROM tweets WHERE media IS NULL")
        backfill = await cur.fetchall()
        for tid, raw in backfill:
            m = media_from_raw_json(raw)
            if m is not None:
                await self._conn.execute(
                    "UPDATE tweets SET media = ? WHERE id = ?",
                    (json.dumps(m, ensure_ascii=False), tid),
                )
        if backfill:
            await self._conn.commit()
        # 回填：老推文正文里还留着已显示为图片/视频的 t.co 短链，按 raw_json 重算剥掉
        cur = await self._conn.execute(
            "SELECT id, raw_json, content FROM tweets WHERE content LIKE '%t.co/%'"
        )
        old_content = await cur.fetchall()
        for tid, raw, content in old_content:
            cleaned = clean_content_from_raw_json(raw)
            if cleaned is not None and cleaned != content:
                await self._conn.execute(
                    "UPDATE tweets SET content = ? WHERE id = ?", (cleaned, tid)
                )
        if old_content:
            await self._conn.commit()
        pcols = {
            row[1]
            for row in await (
                await self._conn.execute("PRAGMA table_info(platform_posts)")
            ).fetchall()
        }
        if "updated_at" not in pcols:
            await self._conn.execute("ALTER TABLE platform_posts ADD COLUMN updated_at TEXT")
            await self._conn.execute(
                "UPDATE platform_posts SET updated_at = inserted_at WHERE updated_at IS NULL"
            )
            await self._conn.commit()
        if "work_url" not in pcols:
            await self._conn.execute("ALTER TABLE platform_posts ADD COLUMN work_url TEXT")
            await self._conn.commit()
        monitor_columns = {
            row[1]
            for row in await (
                await self._conn.execute("PRAGMA table_info(platform_monitors)")
            ).fetchall()
        }
        if "last_success_at" not in monitor_columns:
            await self._conn.execute(
                "ALTER TABLE platform_monitors ADD COLUMN last_success_at TEXT"
            )
            await self._conn.execute(
                "UPDATE platform_monitors SET last_success_at = last_poll_at "
                "WHERE last_error IS NULL"
            )
            await self._conn.commit()
        subscription_columns = {
            row[1]
            for row in await (
                await self._conn.execute("PRAGMA table_info(autoup_subscriptions)")
            ).fetchall()
        }
        if "display_name" not in subscription_columns:
            await self._conn.execute(
                "ALTER TABLE autoup_subscriptions ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
            )
            await self._conn.commit()
        # Older builds stored the full bearer key in created_by. It cannot be
        # reconstructed safely, so redact it once and only store fingerprints going forward.
        await self._conn.execute(
            "UPDATE monitors SET created_by = 'apikey:[redacted]' WHERE created_by LIKE 'apikey:%'"
        )
        await self._conn.execute(
            "UPDATE platform_monitors SET created_by = 'apikey:[redacted]' WHERE created_by LIKE 'apikey:%'"
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- monitors ----

    async def create_monitor(
        self,
        username: str,
        user_id: int | None,
        display_name: str | None,
        interval_seconds: int,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        ts = now_iso()
        cur = await self.conn.execute(
            """
            INSERT INTO monitors (username, user_id, display_name, interval_seconds, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (username, user_id, display_name, interval_seconds, created_by, ts, ts),
        )
        await self.conn.commit()
        return await self.get_monitor(cur.lastrowid)

    async def get_monitor(self, monitor_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        row = await cur.fetchone()
        return row_to_dict(row) if row else None

    async def list_monitors(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM monitors ORDER BY id")
        return [row_to_dict(r) for r in await cur.fetchall()]

    async def update_monitor(self, monitor_id: int, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return await self.get_monitor(monitor_id)
        keys = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE monitors SET {keys}, updated_at = ? WHERE id = ?",
            (*fields.values(), now_iso(), monitor_id),
        )
        await self.conn.commit()
        return await self.get_monitor(monitor_id)

    async def mark_poll(self, monitor_id: int, last_seen: int | None, error: str | None) -> None:
        ts = now_iso()
        await self.conn.execute(
            """
            UPDATE monitors
            SET last_seen_tweet_id = COALESCE(?, last_seen_tweet_id),
                last_poll_at = ?,
                last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (last_seen, ts, error, ts, error, ts, monitor_id),
        )
        await self.conn.commit()

    # ---- tweets ----

    async def insert_tweet(self, tweet: dict[str, Any]) -> bool:
        """Upsert a tweet and return True only when it was newly inserted."""
        existed = await self.tweet_exists(tweet["id"])
        ts = now_iso()
        media = json.dumps(tweet.get("media"), ensure_ascii=False) if tweet.get("media") else None
        cur = await self.conn.execute(
            """
            INSERT INTO tweets
                (id, monitor_id, user_id, username, created_at, content, lang,
                 reply_count, retweet_count, like_count, quote_count, view_count,
                 media, raw_json, inserted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                monitor_id = excluded.monitor_id,
                user_id = excluded.user_id,
                username = excluded.username,
                created_at = excluded.created_at,
                content = excluded.content,
                lang = excluded.lang,
                reply_count = excluded.reply_count,
                retweet_count = excluded.retweet_count,
                like_count = excluded.like_count,
                quote_count = excluded.quote_count,
                view_count = excluded.view_count,
                media = excluded.media,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            WHERE tweets.monitor_id IS NOT excluded.monitor_id
               OR tweets.user_id IS NOT excluded.user_id
               OR tweets.username IS NOT excluded.username
               OR tweets.created_at IS NOT excluded.created_at
               OR tweets.content IS NOT excluded.content
               OR tweets.lang IS NOT excluded.lang
               OR tweets.reply_count IS NOT excluded.reply_count
               OR tweets.retweet_count IS NOT excluded.retweet_count
               OR tweets.like_count IS NOT excluded.like_count
               OR tweets.quote_count IS NOT excluded.quote_count
               OR tweets.view_count IS NOT excluded.view_count
               OR tweets.media IS NOT excluded.media
            """,
            (
                tweet["id"],
                tweet["monitor_id"],
                tweet["user_id"],
                tweet["username"],
                tweet["created_at"],
                tweet["content"],
                tweet.get("lang"),
                tweet.get("reply_count"),
                tweet.get("retweet_count"),
                tweet.get("like_count"),
                tweet.get("quote_count"),
                tweet.get("view_count"),
                media,
                tweet["raw_json"],
                ts,
                ts,
            ),
        )
        await self.conn.commit()
        return not existed

    async def query_tweets(
        self,
        monitor_id: int | None = None,
        username: str | None = None,
        limit: int = 50,
        since_id: int | None = None,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tweets WHERE 1=1"
        params: list[Any] = []
        if monitor_id is not None:
            sql += " AND monitor_id = ?"
            params.append(monitor_id)
        if username is not None:
            sql += " AND username = ?"
            params.append(username)
        if since_id is not None:
            sql += " AND id > ?"
            params.append(since_id)
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await self.conn.execute(sql, params)
        rows = [row_to_dict(r) for r in await cur.fetchall()]
        for row in rows:
            if row.get("media"):
                try:
                    row["media"] = json.loads(row["media"])
                except Exception:
                    row["media"] = None
        return rows

    async def tweet_exists(self, tweet_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM tweets WHERE id = ?", (tweet_id,))
        return await cur.fetchone() is not None

    async def count_tweets(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM tweets")
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def count_tweets_since(self, iso: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM tweets WHERE inserted_at >= ?", (iso,)
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    # ---- platform monitors（抖音/快手/小红书）----

    async def create_platform_monitor(
        self,
        platform: str,
        creator_id: str,
        label: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """UNIQUE(platform, creator_id) 冲突会抛 sqlite3.IntegrityError，由路由映射 409。"""
        ts = now_iso()
        cur = await self.conn.execute(
            """
            INSERT INTO platform_monitors (platform, creator_id, label, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (platform, creator_id, label, created_by, ts, ts),
        )
        await self.conn.commit()
        return await self.get_platform_monitor(cur.lastrowid)

    async def get_platform_monitor(self, monitor_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM platform_monitors WHERE id = ?", (monitor_id,))
        row = await cur.fetchone()
        return row_to_dict(row) if row else None

    async def list_platform_monitors(self, platform: str | None = None) -> list[dict[str, Any]]:
        if platform is not None:
            cur = await self.conn.execute(
                "SELECT * FROM platform_monitors WHERE platform = ? ORDER BY id", (platform,)
            )
        else:
            cur = await self.conn.execute("SELECT * FROM platform_monitors ORDER BY id")
        return [row_to_dict(r) for r in await cur.fetchall()]

    async def update_platform_monitor(self, monitor_id: int, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return await self.get_platform_monitor(monitor_id)
        keys = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE platform_monitors SET {keys}, updated_at = ? WHERE id = ?",
            (*fields.values(), now_iso(), monitor_id),
        )
        await self.conn.commit()
        return await self.get_platform_monitor(monitor_id)

    async def mark_platform_poll(self, monitor_id: int, error: str | None) -> None:
        ts = now_iso()
        await self.conn.execute(
            """
            UPDATE platform_monitors
            SET last_poll_at = ?,
                last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (ts, error, ts, error, ts, monitor_id),
        )
        await self.conn.commit()

    # ---- platform posts ----

    async def upsert_platform_posts(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """已存在的 (platform, content_id) 刷新数据，新的插入；返回本次真正新插入的行（供 SSE）。"""
        if not posts:
            return []
        platform = posts[0]["platform"]
        cur = await self.conn.execute(
            "SELECT * FROM platform_posts WHERE platform = ?", (platform,)
        )
        existing = {row["content_id"]: row_to_dict(row) for row in await cur.fetchall()}

        new_posts: list[dict[str, Any]] = []
        for p in posts:
            current = existing.get(p["content_id"])
            values = (
                p.get("monitor_id"), p.get("creator_hash"), p.get("title"),
                p.get("content"), p.get("created_at"), p.get("image_urls"),
                p.get("video_url"), p.get("cover_url"), p.get("work_url"), p.get("stats"),
                p.get("raw_json"),
            )
            tracked_columns = (
                "monitor_id", "creator_hash", "title", "content", "created_at",
                "image_urls", "video_url", "cover_url", "work_url", "stats",
            )
            if current is not None:
                if any(
                    current[name] != value
                    for name, value in zip(tracked_columns, values[:-1])
                ):
                    await self.conn.execute(
                    """
                    UPDATE platform_posts
                    SET monitor_id = ?, creator_hash = ?, title = ?, content = ?, created_at = ?,
                        image_urls = ?, video_url = ?, cover_url = ?, work_url = ?, stats = ?, raw_json = ?,
                        updated_at = ?
                    WHERE platform = ? AND content_id = ?
                    """,
                        (*values, now_iso(), platform, p["content_id"]),
                    )
            else:
                ts = now_iso()
                await self.conn.execute(
                    """
                    INSERT INTO platform_posts
                        (platform, monitor_id, content_id, creator_hash, title, content,
                         created_at, image_urls, video_url, cover_url, work_url, stats, raw_json,
                         inserted_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        p.get("monitor_id"),
                        p["content_id"],
                        p.get("creator_hash"),
                        p.get("title"),
                        p.get("content"),
                        p.get("created_at"),
                        p.get("image_urls"),
                        p.get("video_url"),
                        p.get("cover_url"),
                        p.get("work_url"),
                        p.get("stats"),
                        p.get("raw_json"),
                        ts,
                        ts,
                    ),
                )
                new_posts.append({**p, "inserted_at": ts, "updated_at": ts})
        await self.conn.commit()
        return new_posts

    async def query_platform_posts(
        self,
        platform: str | None = None,
        monitor_id: int | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM platform_posts WHERE 1=1"
        params: list[Any] = []
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if monitor_id is not None:
            sql += " AND monitor_id = ?"
            params.append(monitor_id)
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await self.conn.execute(sql, params)
        rows = [row_to_dict(r) for r in await cur.fetchall()]
        for row in rows:
            for col in ("image_urls", "stats"):
                if row.get(col):
                    try:
                        row[col] = json.loads(row[col])
                    except Exception:
                        row[col] = None
        return rows

    async def count_platform_posts(
        self, platform: str | None = None, monitor_id: int | None = None
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM platform_posts WHERE 1=1"
        params: list[Any] = []
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if monitor_id is not None:
            sql += " AND monitor_id = ?"
            params.append(monitor_id)
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def count_platform_posts_since(self, iso: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM platform_posts WHERE inserted_at >= ?", (iso,)
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    # ---- AutoUp integration ----

    async def get_autoup_target(self, target_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM autoup_targets WHERE id = ?", (target_id,))
        row = await cur.fetchone()
        return row_to_dict(row) if row else None

    async def find_autoup_target(self, platform: str, canonical_key: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM autoup_targets WHERE platform = ? AND canonical_key = ?",
            (platform, canonical_key),
        )
        row = await cur.fetchone()
        return row_to_dict(row) if row else None

    async def create_autoup_target(self, platform: str, canonical_key: str, monitor_id: int) -> dict[str, Any]:
        ts = now_iso()
        cur = await self.conn.execute(
            """
            INSERT INTO autoup_targets (platform, canonical_key, monitor_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (platform, canonical_key, monitor_id, ts, ts),
        )
        await self.conn.commit()
        target = await self.get_autoup_target(cur.lastrowid)
        assert target is not None
        return target

    async def upsert_autoup_subscription(
        self, competitor_id: str, target_id: int, display_name: str, enabled: bool
    ) -> None:
        ts = now_iso()
        await self.conn.execute(
            """
            INSERT INTO autoup_subscriptions (
                competitor_id, target_id, display_name, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(competitor_id) DO UPDATE SET
                target_id = excluded.target_id,
                display_name = excluded.display_name,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (competitor_id, target_id, display_name, int(enabled), ts, ts),
        )
        await self.conn.commit()

    async def get_autoup_subscription(self, competitor_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM autoup_subscriptions WHERE competitor_id = ?", (competitor_id,)
        )
        row = await cur.fetchone()
        return row_to_dict(row) if row else None

    async def update_autoup_subscription(
        self,
        competitor_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        fields: dict[str, Any] = {"updated_at": now_iso()}
        if display_name is not None:
            fields["display_name"] = display_name
        if enabled is not None:
            fields["enabled"] = int(enabled)
        assignments = ", ".join(f"{column} = ?" for column in fields)
        await self.conn.execute(
            f"UPDATE autoup_subscriptions SET {assignments} WHERE competitor_id = ?",
            (*fields.values(), competitor_id),
        )
        await self.conn.commit()
        return await self.get_autoup_subscription(competitor_id)

    async def delete_autoup_subscription(self, competitor_id: str) -> int | None:
        subscription = await self.get_autoup_subscription(competitor_id)
        if subscription is None:
            return None
        await self.conn.execute(
            "DELETE FROM autoup_subscriptions WHERE competitor_id = ?", (competitor_id,)
        )
        await self.conn.commit()
        return int(subscription["target_id"])

    async def count_active_autoup_subscriptions(self, target_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM autoup_subscriptions WHERE target_id = ? AND enabled = 1",
            (target_id,),
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def query_autoup_changes(
        self, platform: str, monitor_id: int, after_time: str, after_id: int, limit: int
    ) -> list[dict[str, Any]]:
        table = "tweets" if platform == "x" else "platform_posts"
        cur = await self.conn.execute(
            f"""
            SELECT * FROM {table}
            WHERE monitor_id = ?
              AND (updated_at > ? OR (updated_at = ? AND id > ?))
            ORDER BY updated_at, id
            LIMIT ?
            """,
            (monitor_id, after_time, after_time, after_id, limit),
        )
        rows = [row_to_dict(row) for row in await cur.fetchall()]
        for row in rows:
            for column in (("media",) if platform == "x" else ("image_urls", "stats")):
                if row.get(column):
                    try:
                        row[column] = json.loads(row[column])
                    except Exception:
                        row[column] = None
            row.pop("raw_json", None)
        return rows
