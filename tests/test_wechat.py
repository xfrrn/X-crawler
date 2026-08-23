import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.platform.engine import MediaCrawlerEngine
from app.platform.scheduler import PlatformScheduler
from app.stream import SSEManager
from app.wechat import (
    WechatAuthError,
    WechatError,
    WechatService,
    WechatTargetError,
    normalize_articles,
)


FAKE_ID = "MzA1234567890=="


class WechatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=self.temporary.name,
            mc_enabled=False,
            jitter_factor=0,
            _env_file=None,
        )
        self.db = Database(str(Path(self.temporary.name) / "app.db"))
        await self.db.connect()
        self.service = WechatService(self.settings, self.db)

    async def asyncTearDown(self) -> None:
        await self.service.close()
        await self.db.close()
        self.temporary.cleanup()

    def test_article_metadata_normalization_and_url_boundary(self) -> None:
        publish_info = {
            "appmsgex": [
                {
                    "aid": "article-1",
                    "title": "标题",
                    "digest": "摘要",
                    "cover": "//mmbiz.qpic.cn/cover.jpg",
                    "link": "https://mp.weixin.qq.com/s/abc?token=secret&scene=1",
                    "create_time": 1787450400,
                },
                {
                    "appmsgid": "22",
                    "itemidx": "3",
                    "title": "回退 ID",
                    "link": "https://mp.weixin.qq.com/s/def",
                    "update_time": 1787450500,
                },
                {
                    "aid": "bad-host",
                    "title": "非法链接",
                    "link": "https://evil.example/s/1",
                },
                {
                    "aid": "bad-subdomain",
                    "title": "非原文域名",
                    "link": "https://preview.mp.weixin.qq.com/s/1",
                },
            ]
        }
        data = {
            "publish_page": json.dumps(
                {"publish_list": [{"publish_info": json.dumps(publish_info)}]}
            )
        }
        rows = normalize_articles(data, {"id": 7, "creator_id": FAKE_ID})
        self.assertEqual([row["content_id"] for row in rows], ["article-1", "22:3"])
        self.assertEqual(rows[0]["content"], "摘要")
        self.assertEqual(rows[0]["cover_url"], "https://mmbiz.qpic.cn/cover.jpg")
        self.assertNotIn("token=", rows[0]["work_url"])
        self.assertIsNone(rows[0]["stats"])

    async def test_exact_name_resolution_and_fakeid_shortcut(self) -> None:
        calls = 0

        async def unique(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {"list": [{"nickname": "目标公众号", "fakeid": FAKE_ID}]}

        self.service._request_json = unique  # type: ignore[method-assign]
        self.assertEqual(await self.service.resolve_target(FAKE_ID), FAKE_ID)
        self.assertEqual(calls, 0)
        self.assertEqual(await self.service.resolve_target("目标公众号"), FAKE_ID)

        async def missing(*_args, **_kwargs):
            return {"list": [{"nickname": "近似名称", "fakeid": FAKE_ID}]}

        self.service._request_json = missing  # type: ignore[method-assign]
        with self.assertRaises(WechatTargetError):
            await self.service.resolve_target("目标公众号")

        async def duplicate(*_args, **_kwargs):
            return {
                "list": [
                    {"nickname": "同名", "fakeid": FAKE_ID},
                    {"nickname": "同名", "fakeid": "MzB1234567890=="},
                ]
            }

        self.service._request_json = duplicate  # type: ignore[method-assign]
        with self.assertRaisesRegex(WechatTargetError, "同名"):
            await self.service.resolve_target("同名")

    def test_session_status_never_returns_secrets(self) -> None:
        self.service._persist_session(
            {
                "token": "123456",
                "cookies": [{"name": "session", "value": "cookie-secret"}],
                "user_agent": "test-agent",
                "saved_at": "2026-08-23T00:00:00+00:00",
            }
        )
        encoded = json.dumps(self.service.session_status())
        self.assertNotIn("123456", encoded)
        self.assertNotIn("cookie-secret", encoded)
        self.assertNotIn("test-agent", encoded)

    async def test_missing_expired_and_unexpected_session_errors_are_safe(self) -> None:
        with self.assertRaises(WechatAuthError):
            await self.service.resolve_target("目标公众号")

        self.service._persist_session(
            {
                "token": "123456",
                "cookies": [{"name": "session", "value": "cookie-secret"}],
                "user_agent": "test-agent",
                "saved_at": "2026-08-23T00:00:00+00:00",
            }
        )

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"base_resp":{"ret":200003}}'

        with patch("app.wechat.urlopen", return_value=Response()):
            with self.assertRaises(WechatAuthError):
                await self.service.resolve_target("目标公众号")
        self.assertEqual(self.service.session_status()["status"], "expired")

        with patch("app.wechat.urlopen", side_effect=RuntimeError("cookie-secret token=123456")):
            with self.assertRaises(WechatError) as caught:
                await self.service.resolve_target("目标公众号")
        self.assertNotIn("cookie-secret", str(caught.exception))
        self.assertNotIn("123456", str(caught.exception))

    async def test_duplicate_login_reuses_the_active_task(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def login():
            started.set()
            await release.wait()

        self.service._login = login  # type: ignore[method-assign]
        await self.service.start_login()
        task = self.service._login_task
        await started.wait()
        await self.service.start_login()
        self.assertIs(self.service._login_task, task)
        release.set()
        await task

    async def test_wechat_scheduler_starts_without_mediacrawler(self) -> None:
        scheduler = PlatformScheduler(
            self.db,
            MediaCrawlerEngine(self.settings, self.db),
            self.service,
            SSEManager(replay_size=1),
            self.settings,
        )
        await scheduler.start()
        try:
            self.assertEqual(set(scheduler._tasks), {"wx"})
        finally:
            await scheduler.stop()


class WechatMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_platform_posts_table_gets_work_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE platform_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    monitor_id INTEGER NOT NULL,
                    content_id TEXT NOT NULL,
                    creator_hash TEXT,
                    title TEXT,
                    content TEXT,
                    created_at TEXT,
                    image_urls TEXT,
                    video_url TEXT,
                    cover_url TEXT,
                    stats TEXT,
                    raw_json TEXT,
                    inserted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, content_id)
                )
                """
            )
            connection.close()
            db = Database(str(path))
            await db.connect()
            try:
                columns = {
                    row[1]
                    for row in await (
                        await db.conn.execute("PRAGMA table_info(platform_posts)")
                    ).fetchall()
                }
                self.assertIn("work_url", columns)
            finally:
                await db.close()
            reopened = Database(str(path))
            await reopened.connect()
            await reopened.close()


if __name__ == "__main__":
    unittest.main()
