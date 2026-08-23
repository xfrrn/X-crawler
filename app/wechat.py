"""微信公众号后台会话、目标解析和文章元数据采集。"""

import asyncio
import csv
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

from .config import Settings
from .db import Database

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://mp.weixin.qq.com/"
_HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home"
_SEARCH_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
_ARTICLES_URL = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
_QR_SELECTOR = ".login__type__container__scan__qrcode"
_FAKE_ID = re.compile(r"^Mz[A-Za-z0-9+/]{8,}={0,2}$")
_TOKEN = re.compile(r"(?:[?&]token=|\btoken\b\s*[:=]\s*['\"])(\d+)")
_MAX_RESPONSE_BYTES = 4 << 20
_AUTH_ERROR_CODES = {200003, 200013, 200014, 200023}


class WechatError(RuntimeError):
    """可安全返回给调用方的微信采集错误。"""


class WechatAuthError(WechatError):
    pass


class WechatTargetError(WechatError):
    pass


def is_fake_id(value: str) -> bool:
    return bool(_FAKE_ID.fullmatch(value.strip()))


def _safe_url(value: Any, *, wechat_article: bool = False) -> str | None:
    raw = str(value or "").strip().replace("\\/", "/")
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not hostname or parsed.username or parsed.password:
        return None
    if wechat_article and hostname != "mp.weixin.qq.com":
        return None
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "token"]
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(query), ""))


def _published_at(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > _MAX_RESPONSE_BYTES:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_articles(data: dict[str, Any], monitor: dict[str, Any]) -> list[dict[str, Any]]:
    """把 appmsgpublish 的嵌套 JSON 字符串归一化为 platform_posts 行。"""
    publish_page = _json_object(data.get("publish_page"))
    if publish_page is None:
        raise WechatError("微信公众号文章列表格式已变化")
    publish_list = publish_page.get("publish_list")
    if not isinstance(publish_list, list):
        raise WechatError("微信公众号文章列表格式已变化")

    posts: list[dict[str, Any]] = []
    for published in publish_list:
        if not isinstance(published, dict):
            continue
        publish_info = _json_object(published.get("publish_info"))
        items = publish_info.get("appmsgex") if publish_info else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            content_id = str(item.get("aid") or "").strip()
            if not content_id:
                appmsg_id = str(item.get("appmsgid") or "").strip()
                item_index = str(item.get("itemidx") or "").strip()
                content_id = f"{appmsg_id}:{item_index}" if appmsg_id and item_index else ""
            work_url = _safe_url(item.get("link"), wechat_article=True)
            if not content_id or len(content_id) > 500 or not work_url:
                continue
            posts.append(
                {
                    "platform": "wx",
                    "monitor_id": monitor["id"],
                    "content_id": content_id,
                    "creator_hash": monitor["creator_id"],
                    "title": str(item.get("title") or "").strip()[:1000],
                    "content": str(item.get("digest") or "").strip()[:20000],
                    "created_at": _published_at(item.get("create_time") or item.get("update_time")),
                    "image_urls": None,
                    "video_url": None,
                    "cover_url": _safe_url(item.get("cover")),
                    "work_url": work_url,
                    "stats": None,
                    "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                }
            )
    return posts


class WechatService:
    def __init__(self, settings: Settings, db: Database):
        self._settings = settings
        self._db = db
        self._session_path = Path(settings.data_dir) / "wechat-session.json"
        self._login_task: asyncio.Task[None] | None = None
        self._qr_png: bytes | None = None
        self._status = "ready" if self._read_session() else "missing"
        self._last_error: str | None = None

    def session_status(self) -> dict[str, Any]:
        session = self._read_session()
        status = self._status
        if status not in {"starting", "waiting_scan", "error", "expired"}:
            status = "ready" if session else "missing"
        return {
            "status": status,
            "qrReady": self._qr_png is not None,
            "savedAt": session.get("saved_at") if session else None,
            "lastError": self._last_error,
        }

    async def start_login(self) -> dict[str, Any]:
        if self._login_task is None or self._login_task.done():
            self._status = "starting"
            self._last_error = None
            self._qr_png = None
            self._login_task = asyncio.create_task(self._login_with_timeout())
        return self.session_status()

    def qr_png(self) -> bytes | None:
        return self._qr_png

    async def close(self) -> None:
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            await asyncio.gather(self._login_task, return_exceptions=True)

    async def resolve_target(self, target: str) -> str:
        value = target.strip()
        if is_fake_id(value):
            return value
        if not value or len(value) > 100:
            raise WechatTargetError("请输入公众号精确名称或 fakeid")
        data = await self._request_json(
            _SEARCH_URL,
            {"action": "search_biz", "begin": 0, "count": 20, "query": value},
        )
        results = data.get("list")
        if not isinstance(results, list):
            raise WechatError("微信公众号搜索响应格式已变化")
        exact = [
            item
            for item in results
            if isinstance(item, dict)
            and str(item.get("nickname") or "").strip() == value
            and is_fake_id(str(item.get("fakeid") or ""))
        ]
        if not exact:
            raise WechatTargetError("未找到精确匹配的公众号，请检查名称或改填 fakeid")
        if len(exact) > 1:
            raise WechatTargetError("存在同名公众号，请改填 fakeid")
        return str(exact[0]["fakeid"]).strip()

    async def collect(self, monitors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        posts: list[dict[str, Any]] = []
        for monitor in monitors:
            data = await self._request_json(
                _ARTICLES_URL,
                {
                    "sub": "list",
                    "sub_action": "list_ex",
                    "begin": 0,
                    "count": self._settings.wechat_max_articles,
                    "fakeid": monitor["creator_id"],
                },
            )
            posts.extend(normalize_articles(data, monitor))
        return await self._db.upsert_platform_posts(posts)

    async def _login_with_timeout(self) -> None:
        try:
            await asyncio.wait_for(self._login(), timeout=180)
        except TimeoutError:
            self._status = "ready" if self._read_session() else "error"
            self._last_error = "扫码登录失败或超时，请重新发起登录"
            self._qr_png = None
            logger.warning("[wechat] 微信公众号后台扫码登录失败或超时")

    async def _login(self) -> None:
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                    qr = page.locator(_QR_SELECTOR).first
                    await qr.wait_for(state="visible", timeout=20_000)
                    qr_png = await qr.screenshot(type="png")
                    if len(qr_png) < 400:
                        raise RuntimeError("invalid qr")
                    self._qr_png = qr_png
                    self._status = "waiting_scan"
                    await page.wait_for_url(re.compile(r".*(?:[?&]token=|/cgi-bin/home).*"), timeout=180_000)
                    token = _token_from_text(page.url)
                    if not token:
                        await page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=20_000)
                        token = _token_from_text(page.url) or _token_from_text(await page.content())
                    cookies = await context.cookies()
                    user_agent = await page.evaluate("navigator.userAgent")
                    if not token or not cookies or not isinstance(user_agent, str):
                        raise RuntimeError("incomplete session")
                    self._persist_session(
                        {
                            "token": token,
                            "cookies": [
                                {key: cookie.get(key) for key in ("name", "value", "domain", "path", "expires")}
                                for cookie in cookies
                            ],
                            "user_agent": user_agent[:1024],
                            "saved_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    self._status = "ready"
                    self._last_error = None
                    self._qr_png = None
                    logger.info("[wechat] 微信公众号后台扫码登录成功")
                finally:
                    await browser.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 浏览器异常经常包含完整跳转 URL；不得把可能带 token 的原始异常写入日志或响应。
            self._status = "ready" if self._read_session() else "error"
            self._last_error = "扫码登录失败或超时，请重新发起登录"
            self._qr_png = None
            logger.warning("[wechat] 微信公众号后台扫码登录失败或超时")

    async def _request_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        session = self._read_session()
        if session is None:
            self._status = "expired"
            raise WechatAuthError("微信公众号后台尚未登录，请先在 X-crawler 管理页扫码")
        request_params = {
            **params,
            "token": session["token"],
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }

        def request() -> dict[str, Any]:
            url = endpoint + "?" + urlencode(request_params)
            cookie = "; ".join(f"{item['name']}={item['value']}" for item in session["cookies"])
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie,
                "Referer": f"{_HOME_URL}?t=home/index&lang=zh_CN&token={session['token']}",
                "User-Agent": session["user_agent"],
            }
            try:
                with urlopen(Request(url, headers=headers), timeout=15) as response:
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
            except HTTPError as error:
                if error.code in (401, 403):
                    raise WechatAuthError("微信公众号后台登录态已失效，请重新扫码") from None
                raise WechatError("微信公众号接口暂不可用") from None
            except (URLError, TimeoutError, OSError):
                raise WechatError("微信公众号接口暂不可用") from None
            if len(body) > _MAX_RESPONSE_BYTES:
                raise WechatError("微信公众号响应过大")
            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, ValueError):
                raise WechatError("微信公众号返回了无效响应") from None
            if not isinstance(decoded, dict):
                raise WechatError("微信公众号返回了无效响应")
            base_response = decoded.get("base_resp")
            if isinstance(base_response, dict):
                try:
                    ret = int(base_response.get("ret", 0))
                except (TypeError, ValueError):
                    ret = -1
                if ret in _AUTH_ERROR_CODES:
                    raise WechatAuthError("微信公众号后台登录态已失效，请重新扫码")
                if ret != 0:
                    raise WechatError("微信公众号接口拒绝了本次请求")
            return decoded

        try:
            result = await asyncio.to_thread(request)
        except WechatAuthError:
            self._status = "expired"
            self._last_error = "微信公众号后台登录态已失效，请重新扫码"
            raise
        except WechatError:
            raise
        except Exception:
            # 不可预期的 HTTP/header 异常可能包含 Cookie 或 token，统一在此脱敏。
            raise WechatError("微信公众号接口暂不可用") from None
        self._status = "ready"
        self._last_error = None
        return result

    def _read_session(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        token, cookies, user_agent = raw.get("token"), raw.get("cookies"), raw.get("user_agent")
        if not isinstance(token, str) or not token.isdigit() or len(token) > 64:
            return None
        if not isinstance(user_agent, str) or not user_agent or len(user_agent) > 1024:
            return None
        if not isinstance(cookies, list) or not 1 <= len(cookies) <= 100:
            return None
        normalized: list[dict[str, str]] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                return None
            name, value = cookie.get("name"), cookie.get("value")
            if not isinstance(name, str) or not name or len(name) > 256:
                return None
            if not isinstance(value, str) or len(value) > 8192:
                return None
            normalized.append({"name": name, "value": value})
        return {**raw, "cookies": normalized}

    def _persist_session(self, session: dict[str, Any]) -> None:
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._session_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
            _restrict_session_permissions(temporary)
            os.replace(temporary, self._session_path)
        finally:
            temporary.unlink(missing_ok=True)


def _token_from_text(value: str) -> str | None:
    match = _TOKEN.search(value or "")
    return match.group(1) if match else None


def _restrict_session_permissions(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    flags = subprocess.CREATE_NO_WINDOW
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        creationflags=flags,
        check=False,
    )
    try:
        sid = next(csv.reader([identity.stdout.strip()]))[1]
    except (IndexError, StopIteration):
        sid = ""
    if identity.returncode != 0 or not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise OSError("无法确认微信公众号会话文件的本机账户权限")
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        check=False,
    )
    if result.returncode != 0:
        raise OSError("无法限制微信公众号会话文件权限")
