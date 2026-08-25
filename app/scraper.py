import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import twscrape

_WS_RE = re.compile(r"[ \t]+")


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

    async def add_account(
        self,
        username: str,
        password: str,
        email: str = "",
        email_password: str = "",
        proxy: str | None = None,
    ) -> dict: ...

    async def add_account_cookies(self, username: str, cookies: str) -> dict: ...

    async def relogin(self, username: str) -> dict | None: ...

    async def delete_account(self, username: str) -> bool: ...

    async def set_account_active(self, username: str, active: bool) -> dict: ...

    async def close(self) -> None: ...


def strip_media_links(content: str, links: list) -> str:
    """去掉正文里对应图片/视频的 t.co 短链（面板已单独渲染图片，短链多余）。

    links 可以是 twscrape TextLink 对象，也可以是 dict（回填 raw_json 时用）。
    判定依据：display_url 以 pic.twitter.com/ 开头，或展开地址含 twimg.com（推特媒体域名）。
    普通文字链接（如外链、x.com 链接）不会被误删。
    """
    if not links:
        return content
    short_urls: set[str] = set()
    for link in links:
        if hasattr(link, "text"):
            text, url, tc = link.text, link.url, link.tcourl
        else:
            text, url, tc = (link or {}).get("text"), (link or {}).get("url"), (link or {}).get("tcourl")
        text = text or ""
        url = url or ""
        if text.startswith("pic.twitter.com/") or "twimg.com" in url:
            if tc:
                short_urls.add(tc)
    for s in short_urls:
        content = content.replace(s, " ")
    return _WS_RE.sub(" ", content).strip()


def _media_to_dict(m: twscrape.Media | None) -> dict[str, Any] | None:
    """把 twscrape 的 Media 转成前端可直接渲染的直链结构（图片/视频封面/GIF）。"""
    if m is None:
        return None
    out: dict[str, Any] = {}
    if m.photos:
        out["photos"] = [p.url for p in m.photos]
    if m.videos:
        out["videos"] = [
            {
                "cover": v.thumbnailUrl,
                "duration_ms": v.duration,
                "views": v.views,
                "urls": [var.url for var in v.variants],
            }
            for v in m.videos
        ]
    if m.animated:
        out["gifs"] = [{"cover": a.thumbnailUrl, "url": a.videoUrl} for a in m.animated]
    return out or None


def tweet_to_dict(t: twscrape.Tweet) -> dict:
    return {
        "id": t.id,
        "user_id": t.user.id if t.user else t.id,
        "username": t.user.username if t.user else "",
        "created_at": t.date.isoformat() if t.date else "",
        "content": strip_media_links(t.rawContent, t.links),
        "lang": t.lang,
        "reply_count": t.replyCount,
        "retweet_count": t.retweetCount,
        "like_count": t.likeCount,
        "quote_count": t.quoteCount,
        "view_count": t.viewCount,
        "media": _media_to_dict(t.media),
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

    async def _account_info(self, username: str) -> dict:
        """从 accounts_info() 里捞单个账号的当前状态（含登录结果/错误）。"""
        for info in await self._api.pool.accounts_info():
            if info["username"] == username:
                return dict(info)
        raise ValueError(f"账号不存在: {username}")

    async def add_account(
        self,
        username: str,
        password: str,
        email: str = "",
        email_password: str = "",
        proxy: str | None = None,
    ) -> dict:
        pool = self._api.pool
        if await pool.get_account(username) is not None:
            raise ValueError(f"账号已存在: {username}")
        await pool.add_account(username, password, email, email_password, proxy=proxy)
        # 只对刚添加的账号发起登录；login_all 内部会过滤 cookies 账号/已登录账号
        await pool.login_all([username])
        return await self._account_info(username)

    async def add_account_cookies(self, username: str, cookies: str) -> dict:
        # cookies 导入即登录态，无需再 login；已存在账号会被覆盖（刷新会话）
        await self._api.pool.add_account_cookies(username, cookies)
        return await self._account_info(username)

    async def relogin(self, username: str) -> dict | None:
        if await self._api.pool.get_account(username) is None:
            return None
        await self._api.pool.relogin(username)
        return await self._account_info(username)

    async def delete_account(self, username: str) -> bool:
        return await self._api.pool.delete_accounts(username)

    async def set_account_active(self, username: str, active: bool) -> dict:
        # 手动暂停/启用：暂停后取号逻辑不再选中；启用时清空冷却锁立即可用
        await self._api.pool.set_active(username, active)
        return await self._account_info(username)

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
        # 模拟密码账号的登录凭据（不对外展示，重登/删除时用）
        self._secrets: dict[str, str] = {}
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

    async def add_account(
        self,
        username: str,
        password: str,
        email: str = "",
        email_password: str = "",
        proxy: str | None = None,
    ) -> dict:
        if username in self._accounts:
            raise ValueError(f"账号已存在: {username}")
        self._secrets[username] = password
        if password:
            acc = {
                "username": username,
                "logged_in": True,
                "login_method": "password",
                "active": True,
                "last_used": "刚刚",
                "total_req": 0,
                "error_msg": "",
            }
        else:
            # 模拟密码缺失登录失败：给面板一个「不可用」的演示路径
            acc = {
                "username": username,
                "logged_in": False,
                "login_method": "password",
                "active": True,
                "last_used": None,
                "total_req": 0,
                "error_msg": "密码为空，登录失败",
            }
        self._accounts[username] = acc
        return dict(acc)

    async def add_account_cookies(self, username: str, cookies: str) -> dict:
        if "auth_token" not in cookies or "ct0" not in cookies:
            raise ValueError("Cookies 必须包含 auth_token 和 ct0")
        acc = {
            "username": username,
            "logged_in": True,
            "login_method": "cookies",
            "active": True,
            "last_used": "刚刚",
            "total_req": 0,
            "error_msg": "",
        }
        self._accounts[username] = acc
        return dict(acc)

    async def relogin(self, username: str) -> dict | None:
        if username not in self._accounts:
            return None
        acc = self._accounts[username]
        if acc["login_method"] == "password":
            if self._secrets.get(username):
                acc["logged_in"] = True
                acc["error_msg"] = ""
            else:
                acc["logged_in"] = False
                acc["error_msg"] = "密码缺失，登录失败"
        acc["last_used"] = "刚刚"
        return dict(acc)

    async def delete_account(self, username: str) -> bool:
        if username not in self._accounts:
            return False
        del self._accounts[username]
        self._secrets.pop(username, None)
        return True

    async def set_account_active(self, username: str, active: bool) -> dict:
        if username not in self._accounts:
            raise ValueError(f"账号不存在: {username}")
        acc = self._accounts[username]
        acc["active"] = bool(active)
        if active:
            acc["error_msg"] = ""
        return dict(acc)

    async def close(self) -> None:
        return None


def create_scraper(mode: str, accounts_db: str, mock_fail_start: int = 0) -> Scraper:
    if mode == "mock":
        return MockScraper(fail_start=mock_fail_start)
    if mode == "twscrape":
        return TwscrapeScraper(accounts_db)
    raise ValueError(f"未知的 scraper 模式: {mode}（可选 twscrape / mock）")
