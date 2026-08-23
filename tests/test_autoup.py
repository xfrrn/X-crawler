import sys
import tempfile
import types
import unittest
from pathlib import Path
from starlette.requests import Request

# The repository keeps twscrape as a submodule. Database tests only need the
# scraper module's pure helpers, so provide the annotations when the submodule
# has not been initialized in a fresh checkout.
try:
    import twscrape
except ImportError:
    twscrape = types.ModuleType("twscrape")
    sys.modules["twscrape"] = twscrape
for name in ("Media", "Tweet", "AccountsPool", "API"):
    if not hasattr(twscrape, name):
        setattr(twscrape, name, type(name, (), {}))

from app.db import Database
from app.config import Settings
from app.deps import require_api_key
from app.routers.autoup import (
    decode_cursor,
    delete_subscription,
    encode_cursor,
    get_changes,
    normalize_target,
    patch_subscription,
    put_subscription,
)
from app.schemas import AutoUpSubscriptionPatch, AutoUpSubscriptionPut
from app.state import state
from fastapi import HTTPException


class AutoUpIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temporary.name) / "app.db"))
        await self.db.connect()
        self.previous_db = state.db
        self.previous_wechat = state.wechat
        state.db = self.db

    async def asyncTearDown(self) -> None:
        await self.db.close()
        state.db = self.previous_db
        state.wechat = self.previous_wechat
        self.temporary.cleanup()

    def test_target_normalization_and_cursor(self) -> None:
        self.assertEqual(normalize_target("x", "https://x.com/OpenAI"), ("x", "openai"))
        self.assertEqual(
            normalize_target(
                "xiaohongshu",
                "https://www.xiaohongshu.com/user/profile/abc123?xsec_token=secret",
            ),
            ("xhs", "abc123"),
        )
        self.assertEqual(
            normalize_target("douyin", "https://www.douyin.com/user/MS4wLjABAAAA-id?from=web"),
            ("dy", "MS4wLjABAAAA-id"),
        )
        self.assertEqual(
            normalize_target("wechat_official_account", "MzA1234567890=="),
            ("wx", "MzA1234567890=="),
        )
        with self.assertRaises(ValueError):
            normalize_target("douyin", "https://evil.example/user/MS4wLjABAAAA-id")
        cursor = encode_cursor("2026-08-12T10:00:00+00:00", 42)
        self.assertEqual(decode_cursor(cursor), ("2026-08-12T10:00:00+00:00", 42))
        with self.assertRaises(ValueError):
            decode_cursor("not-a-cursor")

    def test_api_key_identity_is_only_a_fingerprint(self) -> None:
        secret = "do-not-store-this-key"
        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {secret}".encode())],
                "session": {},
            }
        )
        identity = require_api_key(request, Settings(api_keys=secret, _env_file=None))
        self.assertTrue(identity.startswith("apikey_sha256:"))
        self.assertNotIn(secret, identity)

    async def test_shared_target_change_feed_and_metric_update(self) -> None:
        monitor = await self.db.create_monitor("openai", 1, "OpenAI", 15)
        target = await self.db.create_autoup_target("x", "openai", monitor["id"])
        await self.db.upsert_autoup_subscription(
            "00000000-0000-4000-8000-000000000001", target["id"], "OpenAI 1", True
        )
        await self.db.upsert_autoup_subscription(
            "00000000-0000-4000-8000-000000000002", target["id"], "OpenAI 2", True
        )
        self.assertEqual(await self.db.count_active_autoup_subscriptions(target["id"]), 2)
        subscription = await self.db.get_autoup_subscription(
            "00000000-0000-4000-8000-000000000001"
        )
        self.assertEqual(subscription["display_name"], "OpenAI 1")

        tweet = {
            "id": 100,
            "monitor_id": monitor["id"],
            "user_id": 1,
            "username": "openai",
            "created_at": "2026-08-12T09:00:00+00:00",
            "content": "hello",
            "lang": "en",
            "reply_count": 1,
            "retweet_count": 2,
            "like_count": 3,
            "quote_count": 4,
            "view_count": 5,
            "raw_json": '{"id":100,"likes":3}',
        }
        self.assertTrue(await self.db.insert_tweet(tweet))
        rows = await self.db.query_autoup_changes("x", monitor["id"], "", 0, 10)
        self.assertEqual(len(rows), 1)
        first_updated_at = rows[0]["updated_at"]

        self.assertFalse(await self.db.insert_tweet(tweet))
        unchanged = await self.db.query_autoup_changes("x", monitor["id"], "", 0, 10)
        self.assertEqual(unchanged[0]["updated_at"], first_updated_at)

        tweet["raw_json"] = '{"id":100,"transport_only":"changed"}'
        self.assertFalse(await self.db.insert_tweet(tweet))
        raw_only = await self.db.query_autoup_changes("x", monitor["id"], "", 0, 10)
        self.assertEqual(raw_only[0]["updated_at"], first_updated_at)

        tweet["like_count"] = 9
        tweet["raw_json"] = '{"id":100,"likes":9}'
        self.assertFalse(await self.db.insert_tweet(tweet))
        changed = await self.db.query_autoup_changes("x", monitor["id"], "", 0, 10)
        self.assertEqual(changed[0]["like_count"], 9)
        self.assertGreaterEqual(changed[0]["updated_at"], first_updated_at)

        await self.db.delete_autoup_subscription("00000000-0000-4000-8000-000000000001")
        self.assertEqual(await self.db.count_active_autoup_subscriptions(target["id"]), 1)
        self.assertIsNone(
            await self.db.delete_autoup_subscription(
                "00000000-0000-4000-8000-000000000001"
            )
        )

    async def test_platform_post_only_advances_on_mapped_field_changes(self) -> None:
        monitor = await self.db.create_platform_monitor("dy", "creator-1", "Creator")
        post = {
            "platform": "dy",
            "monitor_id": monitor["id"],
            "content_id": "post-1",
            "creator_hash": "hash",
            "title": "Title",
            "content": "Body",
            "created_at": "2026-08-12T09:00:00+00:00",
            "image_urls": '["https://example.com/image.jpg"]',
            "video_url": None,
            "cover_url": None,
            "work_url": "https://www.douyin.com/video/post-1",
            "stats": '{"likes":1,"comments":2,"shares":3,"views":4}',
            "raw_json": '{"transport":1}',
        }
        self.assertEqual(len(await self.db.upsert_platform_posts([post])), 1)
        first = (await self.db.query_autoup_changes("dy", monitor["id"], "", 0, 10))[0]
        post["raw_json"] = '{"transport":2}'
        self.assertEqual(await self.db.upsert_platform_posts([post]), [])
        unchanged = (await self.db.query_autoup_changes("dy", monitor["id"], "", 0, 10))[0]
        self.assertEqual(unchanged["updated_at"], first["updated_at"])
        post["work_url"] = "https://www.douyin.com/video/post-1-updated"
        self.assertEqual(await self.db.upsert_platform_posts([post]), [])
        work_url_changed = (await self.db.query_autoup_changes("dy", monitor["id"], "", 0, 10))[0]
        self.assertEqual(work_url_changed["work_url"], post["work_url"])
        self.assertGreaterEqual(work_url_changed["updated_at"], first["updated_at"])
        post["stats"] = '{"likes":9,"comments":2,"shares":3,"views":4}'
        self.assertEqual(await self.db.upsert_platform_posts([post]), [])
        changed = (await self.db.query_autoup_changes("dy", monitor["id"], "", 0, 10))[0]
        self.assertEqual(changed["stats"]["likes"], 9)

    async def test_wechat_fakeid_is_shared_across_workspace_subscriptions(self) -> None:
        class WechatStub:
            async def resolve_target(self, _target: str) -> str:
                return "MzA1234567890=="

        state.wechat = WechatStub()
        first = await put_subscription(
            "00000000-0000-4000-8000-000000000021",
            AutoUpSubscriptionPut(
                platform="wechat_official_account", target="公众号名称", displayName="工作区一"
            ),
            creator="apikey_sha256:test",
        )
        second = await put_subscription(
            "00000000-0000-4000-8000-000000000022",
            AutoUpSubscriptionPut(
                platform="wechat_official_account", target="MzA1234567890==", displayName="工作区二"
            ),
            creator="apikey_sha256:test",
        )
        self.assertEqual(first.source_target_id, second.source_target_id)
        monitors = await self.db.list_platform_monitors("wx")
        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0]["creator_id"], "MzA1234567890==")

    async def test_subscription_routes_share_target_and_page_changes(self) -> None:
        first_id = "00000000-0000-4000-8000-000000000011"
        second_id = "00000000-0000-4000-8000-000000000012"
        first = await put_subscription(
            first_id,
            AutoUpSubscriptionPut(
                platform="xiaohongshu",
                target="https://www.xiaohongshu.com/user/profile/abc123?xsec_token=one",
                displayName="Workspace One",
            ),
            creator="apikey_sha256:test",
        )
        second = await put_subscription(
            second_id,
            AutoUpSubscriptionPut(
                platform="xiaohongshu",
                target="https://www.xiaohongshu.com/user/profile/abc123?xsec_token=two",
                displayName="Workspace Two",
            ),
            creator="apikey_sha256:test",
        )
        self.assertEqual(first.source_target_id, second.source_target_id)
        self.assertEqual(second.display_name, "Workspace Two")
        target = await self.db.get_autoup_target(int(first.source_target_id[2:]))
        self.assertEqual(await self.db.count_active_autoup_subscriptions(target["id"]), 2)

        monitor = await self.db.get_platform_monitor(target["monitor_id"])
        self.assertIn("xsec_token=two", monitor["creator_id"])
        posts = []
        for index in (1, 2):
            posts.append(
                {
                    "platform": "xhs",
                    "monitor_id": monitor["id"],
                    "content_id": f"note-{index}",
                    "creator_hash": "hash",
                    "title": f"Title {index}",
                    "content": "Body",
                    "created_at": f"2026-08-12T09:0{index}:00+00:00",
                    "image_urls": None,
                    "video_url": None,
                    "cover_url": None,
                    "stats": '{"likes":1}',
                    "raw_json": "{}",
                }
            )
        await self.db.upsert_platform_posts(posts)
        page_one = await get_changes(first.source_target_id, limit=1)
        self.assertEqual(len(page_one.items), 1)
        self.assertTrue(page_one.has_more)
        page_two = await get_changes(
            first.source_target_id, cursor=page_one.next_cursor, limit=1
        )
        self.assertEqual(len(page_two.items), 1)
        self.assertFalse(page_two.has_more)
        empty = await get_changes(
            first.source_target_id, cursor=page_two.next_cursor, limit=10
        )
        self.assertEqual(empty.items, [])

        await patch_subscription(
            first_id, AutoUpSubscriptionPatch(enabled=False)
        )
        self.assertTrue((await self.db.get_platform_monitor(monitor["id"]))["active"])
        await delete_subscription(second_id)
        self.assertFalse((await self.db.get_platform_monitor(monitor["id"]))["active"])
        await delete_subscription(second_id)  # idempotent retry

        with self.assertRaises(HTTPException):
            await put_subscription(
                first_id,
                AutoUpSubscriptionPut(
                    platform="douyin",
                    target="https://www.douyin.com/user/other",
                    displayName="Changed Target",
                ),
                creator="apikey_sha256:test",
            )
        with self.assertRaises(HTTPException):
            await get_changes("t_999999", limit=10)


if __name__ == "__main__":
    unittest.main()
