import time
from dataclasses import dataclass
from typing import Protocol

import twscrape


@dataclass
class ResolvedUser:
    user_id: int
    username: str
    display_name: str | None


class Scraper(Protocol):
    async def resolve_user(self, username: str) -> ResolvedUser | None: ...

    async def recent_tweets(self, user_id: int, limit: int) -> list[dict]: ...

    async def account_infos(self) -> list[dict]: ...

    async def pool_stats(self) -> dict: ...

    async def delete_account(self, username: str) -> bool: ...

    async def close(self) -> None: ...


def tweet_to_dict(t: twscrape.Tweet) -> dict:
    return {
        "id": t.id,
        "user_id": t.user.id if t.user else t.id,
        "username": t.user.username if t.user else "",
        "created_at": t.date.isoformat() if t.date else "",
        "content": t.rawContent,
        "lang": t.lang,
        "reply_count": t.replyCount,
        "retweet_count": t.retweetCount,
        "like_count": t.likeCount,
        "quote_count": t.quoteCount,
        "view_count": t.viewCount,
        "raw_json": t.json(),
    }


class TwscrapeScraper:
    def __init__(self, accounts_db: str):
        pool = twscrape.AccountsPool(
            db_file=accounts_db,
            raise_when_no_account=True,
            wait_timeout=60.0,
        )
        # 随机取号：默认 ORDER BY username 会固定压在最前一个可用账号上，
        # 改为随机让并发/错峰轮询分散到各账号
        pool._order_by = "RANDOM()"
        self._api = twscrape.API(pool=pool)

    async def resolve_user(self, username: str) -> ResolvedUser | None:
        user = await self._api.user_by_login(username)
        if user is None:
            return None
        return ResolvedUser(user_id=user.id, username=user.username, display_name=user.displayname)

    async def recent_tweets(self, user_id: int, limit: int) -> list[dict]:
        out: list[dict] = []
        async for t in self._api.user_tweets(user_id, limit=limit):
            out.append(tweet_to_dict(t))
        return out

    async def account_infos(self) -> list[dict]:
        return await self._api.pool.accounts_info()

    async def pool_stats(self) -> dict:
        return await self._api.pool.stats()

    async def delete_account(self, username: str) -> bool:
        return await self._api.pool.delete_accounts(username)

    async def close(self) -> None:
        # twscrape 的 AccountsPool 无持久连接（每查询开合），无需显式清理
        return None


class MockScraper:
    """无 X 依赖，每次轮询伪造递增推文，用于端到端验证/演示。

    fail_start: 自第 N 次 recent_tweets 起持续抛错，制造连续失败以验证退避/暂停。
    """

    def __init__(self, fail_start: int = 0):
        # 时间基准：跨进程/跨实例也严格递增，避免与新进程生成的 id 撞车
        # 导致 INSERT OR IGNORE 按主键去重而丢推文
        self._next_id = int(time.time() * 1000) * 1000
        self._usernames: dict[int, str] = {}
        self._fail_start = fail_start
        self._calls = 0
        # 演示用假采集账号：与 twscrape accounts_info() 的字段形状一致，
        # 让 mock 模式下「采集账号管理」页面也能完整演示
        self._accounts: dict[str, dict] = {
            "demo_1": {
                "username": "demo_1",
                "logged_in": True,
                "login_method": "cookies",
                "active": True,
                "last_used": "2026-08-11 10:24:03",
                "total_req": 148,
                "error_msg": "",
            },
            "demo_2": {
                "username": "demo_2",
                "logged_in": False,
                "login_method": "password",
                "active": True,
                "last_used": None,
                "total_req": 0,
                "error_msg": "login 失败：需要验证码",
            },
        }

    async def resolve_user(self, username: str) -> ResolvedUser:
        user_id = len(self._usernames) + 1
        self._usernames[user_id] = username
        return ResolvedUser(user_id=user_id, username=username, display_name=f"Mock {username}")

    async def recent_tweets(self, user_id: int, limit: int) -> list[dict]:
        self._calls += 1
        if self._fail_start > 0 and self._calls >= self._fail_start:
            raise RuntimeError("模拟限流 (429): mock 连续失败注入")
        out = []
        for i in range(limit):
            self._next_id += 1
            out.append(
                {
                    "id": self._next_id,
                    "user_id": user_id,
                    "username": self._usernames.get(user_id, f"mock{user_id}"),
                    "created_at": "",
                    "content": f"mock tweet #{self._next_id}",
                    "lang": "en",
                    "reply_count": 0,
                    "retweet_count": 0,
                    "like_count": 0,
                    "quote_count": 0,
                    "view_count": 0,
                    "raw_json": f'{{"id": {self._next_id}}}',
                }
            )
        return out

    async def account_infos(self) -> list[dict]:
        return list(self._accounts.values())

    async def pool_stats(self) -> dict:
        total = len(self._accounts)
        active = sum(1 for a in self._accounts.values() if a["active"])
        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "note": "mock 模式：演示用假账号",
        }

    async def delete_account(self, username: str) -> bool:
        if username not in self._accounts:
            return False
        del self._accounts[username]
        return True

    async def close(self) -> None:
        return None


def create_scraper(mode: str, accounts_db: str, mock_fail_start: int = 0) -> Scraper:
    if mode == "mock":
        return MockScraper(fail_start=mock_fail_start)
    if mode == "twscrape":
        return TwscrapeScraper(accounts_db)
    raise ValueError(f"未知的 scraper 模式: {mode}（可选 twscrape / mock）")
