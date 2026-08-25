import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.db import Database
from app.manager import MonitorManager
from app.platform.engine import MediaCrawlerEngine, MediaCrawlerError, build_hash_map
from app.platform.scheduler import PlatformScheduler
from app.scraper import TwscrapeScraper
from app.schemas import target_status
from app.stream import SSEManager


class EmptyScraper:
    async def recent_tweets(self, user_id: int, limit: int) -> list[dict]:
        return []

    async def close(self) -> None:
        return None


class FailingEngine:
    async def run_platform(self, platform: str, monitors: list[dict]):
        raise RuntimeError("boom")


class PartialEngine:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    async def run_platform(self, platform: str, monitors: list[dict]):
        self.calls.append([monitor["id"] for monitor in monitors])
        if monitors[0]["creator_id"] == "bad":
            raise RuntimeError("bad creator")
        return SimpleNamespace(new_posts=[])


class AccountPool:
    def __init__(self) -> None:
        self.usernames = {"ok"}

    async def accounts_info(self) -> list[dict]:
        return [{"username": "ok", "error_msg": "None"}]

    async def get_account(self, username: str):
        return username if username in self.usernames else None

    async def delete_accounts(self, username: str) -> None:
        self.usernames.discard(username)


class StatusTest(unittest.IsolatedAsyncioTestCase):
    def test_target_status_is_derived_from_existing_signals(self) -> None:
        self.assertEqual(target_status(True, None, None), "waiting")
        self.assertEqual(target_status(True, "now", None), "healthy")
        self.assertEqual(target_status(True, "before", "failed"), "error")
        self.assertEqual(target_status(False, "before", None), "paused")
        self.assertEqual(target_status(False, "before", "failed"), "auto_paused")

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temporary.name) / "app.db"))
        await self.db.connect()
        self.settings = Settings(
            data_dir=self.temporary.name,
            default_poll_interval=1,
            jitter_factor=0,
            pause_after_errors=1,
            mc_repo_path=self.temporary.name,
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temporary.cleanup()

    async def test_attempt_and_success_times_are_distinct(self) -> None:
        monitor = await self.db.create_monitor("empty", 1, None, 1)
        await self.db.mark_poll(monitor["id"], None, None)
        succeeded = await self.db.get_monitor(monitor["id"])
        await self.db.mark_poll(monitor["id"], None, "failed")
        failed = await self.db.get_monitor(monitor["id"])

        self.assertIsNotNone(succeeded["last_success_at"])
        self.assertEqual(failed["last_success_at"], succeeded["last_success_at"])
        self.assertEqual(failed["last_error"], "failed")

    async def test_empty_x_poll_is_recorded_as_success(self) -> None:
        monitor = await self.db.create_monitor("empty", 1, None, 1)
        manager = MonitorManager(
            self.db, EmptyScraper(), SSEManager(replay_size=1), self.settings
        )

        async def stop_after_first_poll(interval: int) -> None:
            raise asyncio.CancelledError

        manager._sleep = stop_after_first_poll  # type: ignore[method-assign]
        with self.assertRaises(asyncio.CancelledError):
            await manager._poll_loop(monitor["id"])

        saved = await self.db.get_monitor(monitor["id"])
        self.assertIsNotNone(saved["last_success_at"])
        self.assertIsNone(saved["last_error"])

    async def test_platform_auto_pause_keeps_the_cause(self) -> None:
        monitor = await self.db.create_platform_monitor("xhs", "creator", "Creator")
        scheduler = PlatformScheduler(
            self.db,
            FailingEngine(),
            SimpleNamespace(),
            SSEManager(replay_size=1),
            self.settings,
        )
        await scheduler._run_once("xhs", [monitor])

        saved = await self.db.get_platform_monitor(monitor["id"])
        runtime = next(item for item in scheduler.runtime_snapshot() if item["platform"] == "xhs")
        self.assertFalse(saved["active"])
        self.assertIn("boom", saved["last_error"])
        self.assertIn("boom", runtime["last_error"])

    async def test_platform_failure_is_isolated_per_monitor(self) -> None:
        good = await self.db.create_platform_monitor("xhs", "good", "Good")
        bad = await self.db.create_platform_monitor("xhs", "bad", "Bad")
        engine = PartialEngine()
        scheduler = PlatformScheduler(
            self.db,
            engine,
            SimpleNamespace(),
            SSEManager(replay_size=1),
            self.settings,
        )

        await scheduler._run_once("xhs", [good, bad])

        saved_good = await self.db.get_platform_monitor(good["id"])
        saved_bad = await self.db.get_platform_monitor(bad["id"])
        self.assertEqual(engine.calls, [[good["id"]], [bad["id"]]])
        self.assertTrue(saved_good["active"])
        self.assertIsNone(saved_good["last_error"])
        self.assertIsNotNone(saved_good["last_success_at"])
        self.assertFalse(saved_bad["active"])
        self.assertIn("bad creator", saved_bad["last_error"])

    async def test_engine_reads_only_rows_touched_by_current_run(self) -> None:
        monitor = await self.db.create_platform_monitor("xhs", "creator", "Creator")
        mc_db = Path(self.temporary.name) / "database" / "sqlite_tables.db"
        mc_db.parent.mkdir()
        connection = sqlite3.connect(mc_db)
        connection.executescript(
            """
            CREATE TABLE xhs_note (
                note_id TEXT, creator_hash TEXT, last_modify_ts INTEGER
            );
            INSERT INTO xhs_note VALUES ('stale', 'other', 0);
            """
        )
        creator_hash = next(iter(build_hash_map([monitor])))
        connection.execute(
            "INSERT INTO xhs_note VALUES (?, ?, ?)",
            ("fresh", creator_hash, 9999999999999),
        )
        connection.commit()
        connection.close()
        engine = MediaCrawlerEngine(self.settings, self.db)

        process = SimpleNamespace(stdout=b"", returncode=0)
        with patch(
            "app.platform.engine.asyncio.to_thread",
            new=AsyncMock(return_value=process),
        ):
            result = await engine.run_platform("xhs", [monitor])

        self.assertEqual(result.raw_count, 1)
        posts = await self.db.query_platform_posts(monitor_id=monitor["id"])
        self.assertEqual([post["content_id"] for post in posts], ["fresh"])

    async def test_engine_treats_swallowed_creator_error_as_failure(self) -> None:
        monitor = await self.db.create_platform_monitor("xhs", "bad", "Bad")
        engine = MediaCrawlerEngine(self.settings, self.db)
        process = SimpleNamespace(
            stdout=b"Failed to parse creator URL: invalid", returncode=0
        )

        with patch(
            "app.platform.engine.asyncio.to_thread",
            new=AsyncMock(return_value=process),
        ):
            with self.assertRaises(MediaCrawlerError):
                await engine.run_platform("xhs", [monitor])

    async def test_account_none_error_is_normalized(self) -> None:
        scraper = object.__new__(TwscrapeScraper)
        scraper._api = SimpleNamespace(pool=AccountPool())
        self.assertIsNone((await scraper.account_infos())[0]["error_msg"])
        self.assertTrue(await scraper.delete_account("ok"))
        self.assertFalse(await scraper.delete_account("missing"))


class StatusMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_last_success_is_backfilled_only_for_previous_successes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE monitors (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE, user_id INTEGER,
                    display_name TEXT, interval_seconds INTEGER, active INTEGER,
                    last_seen_tweet_id INTEGER, last_poll_at TEXT, last_error TEXT,
                    created_by TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE platform_monitors (
                    id INTEGER PRIMARY KEY, platform TEXT, creator_id TEXT, label TEXT,
                    active INTEGER, last_poll_at TEXT, last_error TEXT, created_by TEXT,
                    created_at TEXT, updated_at TEXT, UNIQUE(platform, creator_id)
                );
                INSERT INTO monitors VALUES
                    (1, 'ok', 1, NULL, 1, 1, NULL, 'success-at', NULL, NULL, 'now', 'now'),
                    (2, 'bad', 2, NULL, 1, 1, NULL, 'failed-at', 'failed', NULL, 'now', 'now');
                INSERT INTO platform_monitors VALUES
                    (1, 'xhs', 'creator', 'Creator', 1, 'success-at', NULL, NULL, 'now', 'now');
                """
            )
            connection.close()

            db = Database(str(path))
            await db.connect()
            try:
                self.assertEqual((await db.get_monitor(1))["last_success_at"], "success-at")
                self.assertIsNone((await db.get_monitor(2))["last_success_at"])
                self.assertEqual(
                    (await db.get_platform_monitor(1))["last_success_at"], "success-at"
                )
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
