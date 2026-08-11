import os
from datetime import datetime, timezone
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    user_id INTEGER,
    display_name TEXT,
    interval_seconds INTEGER NOT NULL DEFAULT 15,
    active INTEGER NOT NULL DEFAULT 1,
    last_seen_tweet_id INTEGER,
    last_poll_at TEXT,
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
    raw_json TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tweets_monitor ON tweets(monitor_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_username ON tweets(username, id DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


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
        await self.conn.execute(
            """
            UPDATE monitors
            SET last_seen_tweet_id = COALESCE(?, last_seen_tweet_id),
                last_poll_at = ?,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (last_seen, now_iso(), error, now_iso(), monitor_id),
        )
        await self.conn.commit()

    # ---- tweets ----

    async def insert_tweet(self, tweet: dict[str, Any]) -> bool:
        """Returns True if inserted, False if already existed (dedup by id)."""
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO tweets
                (id, monitor_id, user_id, username, created_at, content, lang,
                 reply_count, retweet_count, like_count, quote_count, view_count,
                 raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                tweet["raw_json"],
                now_iso(),
            ),
        )
        await self.conn.commit()
        return cur.rowcount > 0

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
        return [row_to_dict(r) for r in await cur.fetchall()]

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
