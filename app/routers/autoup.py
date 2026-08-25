import asyncio
import base64
import json
import re
from datetime import datetime
from sqlite3 import IntegrityError
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..deps import require_api_key
from ..schemas import (
    AutoUpChangeOut,
    AutoUpChangesOut,
    AutoUpSubscriptionPatch,
    AutoUpSubscriptionPut,
    AutoUpTargetOut,
)
from ..state import state
from ..wechat import WechatAuthError, WechatError, WechatTargetError, is_fake_id

router = APIRouter(
    prefix="/integrations/autoup",
    tags=["autoup"],
    dependencies=[Depends(require_api_key)],
)

_PLATFORM_TO_SOURCE = {
    "x": "x",
    "douyin": "dy",
    "kuaishou": "ks",
    "xiaohongshu": "xhs",
    "wechat_official_account": "wx",
}
_SOURCE_TO_PLATFORM = {value: key for key, value in _PLATFORM_TO_SOURCE.items()}
_PROFILE_PATTERNS = {
    "dy": re.compile(r"/user/([^/?#]+)"),
    "ks": re.compile(r"/profile/([^/?#]+)"),
    "xhs": re.compile(r"/user/profile/([0-9a-fA-F]+)"),
}
_PLATFORM_DOMAINS = {
    "dy": "douyin.com",
    "ks": "kuaishou.com",
    "xhs": "xiaohongshu.com",
}
_X_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_EMPTY_TIME = "0001-01-01T00:00:00+00:00"
_registration_lock = asyncio.Lock()


def _competitor_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="competitorId 必须是 UUID") from error


def _source_target_id(value: str) -> int:
    if not value.startswith("t_") or not value[2:].isdigit():
        raise HTTPException(status_code=404, detail="采集目标不存在")
    return int(value[2:])


def normalize_target(platform: str, target: str) -> tuple[str, str]:
    """Return source platform and a stable account key without retaining URL query tokens."""
    source = _PLATFORM_TO_SOURCE[platform]
    value = target.strip()
    if source == "x":
        if "://" in value:
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or parsed.hostname not in (
                "x.com",
                "www.x.com",
                "twitter.com",
                "www.twitter.com",
            ):
                raise ValueError("X 账号必须是用户名或 x.com/twitter.com 主页")
            value = parsed.path.strip("/").split("/", 1)[0]
        value = value.lstrip("@").lower()
        if not _X_USERNAME.fullmatch(value):
            raise ValueError("X 用户名格式无效")
        return source, value

    if source == "wx":
        if not is_fake_id(value):
            raise ValueError("微信公众号采集目标必须是 fakeid")
        return source, value

    if "://" in value:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        expected_domain = _PLATFORM_DOMAINS[source]
        if (
            parsed.scheme not in ("http", "https")
            or (hostname != expected_domain and not hostname.endswith("." + expected_domain))
        ):
            raise ValueError("主页链接格式无效")
        match = _PROFILE_PATTERNS[source].search(parsed.path)
        if not match:
            raise ValueError("主页链接中缺少创作者 ID")
        value = match.group(1)
    else:
        value = value.split("?", 1)[0].strip("/")
    if not value or len(value) > 256 or any(character.isspace() for character in value):
        raise ValueError("创作者 ID 格式无效")
    return source, value


def encode_cursor(changed_at: str, row_id: int) -> str:
    raw = json.dumps({"t": changed_at, "i": row_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> tuple[str, int]:
    if not value:
        return _EMPTY_TIME, 0
    if len(value) > 512:
        raise ValueError("cursor 过长")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        changed_at, row_id = payload["t"], payload["i"]
    except Exception as error:
        raise ValueError("cursor 无效") from error
    if not isinstance(changed_at, str) or not isinstance(row_id, int) or row_id < 0:
        raise ValueError("cursor 无效")
    try:
        parsed_time = datetime.fromisoformat(changed_at)
    except ValueError as error:
        raise ValueError("cursor 无效") from error
    if parsed_time.tzinfo is None:
        raise ValueError("cursor 无效")
    return changed_at, row_id


async def _monitor_for_target(target: dict) -> dict | None:
    db = state.db
    assert db is not None
    if target["platform"] == "x":
        return await db.get_monitor(target["monitor_id"])
    return await db.get_platform_monitor(target["monitor_id"])


async def _set_target_active(target: dict, active: bool) -> None:
    db = state.db
    assert db is not None
    if target["platform"] == "x":
        if state.manager is not None:
            await state.manager.set_monitor_active(target["monitor_id"], active)
        else:
            await db.update_monitor(target["monitor_id"], active=int(active))
    else:
        await db.update_platform_monitor(
            target["monitor_id"], active=int(active), last_error=None
        )


async def _target_out(target: dict) -> AutoUpTargetOut:
    monitor = await _monitor_for_target(target)
    if monitor is None:
        raise HTTPException(status_code=404, detail="底层采集目标不存在")
    display_name = monitor.get("display_name") or monitor.get("label") or target["canonical_key"]
    return AutoUpTargetOut.model_validate(
        {
            "sourceTargetId": f"t_{target['id']}",
            "platform": _SOURCE_TO_PLATFORM[target["platform"]],
            "accountExternalId": target["canonical_key"],
            "displayName": display_name,
            "active": bool(monitor["active"]),
            "lastCollectedAt": monitor.get("last_success_at"),
            "lastError": monitor.get("last_error"),
        }
    )


async def _find_existing_monitor(source: str, canonical_key: str) -> dict | None:
    db = state.db
    assert db is not None
    if source == "x":
        return next(
            (row for row in await db.list_monitors() if row["username"].lower() == canonical_key),
            None,
        )
    for monitor in await db.list_platform_monitors(source):
        try:
            _, existing_key = normalize_target(_SOURCE_TO_PLATFORM[source], monitor["creator_id"])
        except ValueError:
            continue
        if existing_key == canonical_key:
            return monitor
    return None


@router.put("/subscriptions/{competitor_id}", response_model=AutoUpTargetOut)
async def put_subscription(
    competitor_id: str,
    payload: AutoUpSubscriptionPut,
    creator: str = Depends(require_api_key),
) -> AutoUpTargetOut:
    competitor_id = _competitor_id(competitor_id)
    if payload.platform == "wechat_official_account":
        if state.wechat is None:
            raise HTTPException(status_code=503, detail="微信公众号采集器尚未初始化")
        try:
            canonical_key = await state.wechat.resolve_target(payload.target)
        except WechatTargetError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except WechatAuthError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except WechatError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        source = "wx"
    else:
        try:
            source, canonical_key = normalize_target(payload.platform, payload.target)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    db = state.db
    assert db is not None

    async with _registration_lock:
        target = await db.find_autoup_target(source, canonical_key)
        subscription = await db.get_autoup_subscription(competitor_id)
        if subscription is not None:
            subscribed_target = await db.get_autoup_target(subscription["target_id"])
            if subscribed_target is None:
                raise HTTPException(status_code=409, detail="competitorId 的原采集目标已失效")
            if (
                subscribed_target["platform"] != source
                or subscribed_target["canonical_key"] != canonical_key
            ):
                raise HTTPException(status_code=409, detail="competitorId 已绑定其他采集目标")
            target = subscribed_target
        created = False
        if target is None:
            monitor = await _find_existing_monitor(source, canonical_key)
            if monitor is None:
                if source == "x":
                    if state.manager is None:
                        raise HTTPException(status_code=503, detail="X 采集器尚未初始化")
                    monitor = await state.manager.add_monitor(canonical_key, None, created_by=creator)
                else:
                    try:
                        monitor = await db.create_platform_monitor(
                            source,
                            canonical_key if source == "wx" else payload.target.strip(),
                            payload.display_name,
                            created_by=creator,
                        )
                    except IntegrityError as error:
                        raise HTTPException(status_code=409, detail="采集目标已存在") from error
                created = True
            target = await db.create_autoup_target(source, canonical_key, monitor["id"])

        monitor = await _monitor_for_target(target)
        assert monitor is not None
        if source == "xhs" and "xsec_token" in parse_qs(urlparse(payload.target).query):
            # Only replace the retained locator with another complete XHS locator.
            # Cloud compensation uses the canonical profile URL and must not erase
            # the xsec_token already owned by this service.
            await db.update_platform_monitor(
                monitor["id"], creator_id=payload.target.strip()
            )
        await db.upsert_autoup_subscription(
            competitor_id, target["id"], payload.display_name, payload.enabled
        )
        await _set_target_active(target, (await db.count_active_autoup_subscriptions(target["id"])) > 0)

    if created and source != "x" and state.platform_scheduler is not None:
        try:
            await state.platform_scheduler.trigger_platform(source)
        except ValueError:
            pass
    output = await _target_out(target)
    return output.model_copy(update={"display_name": payload.display_name})


@router.patch("/subscriptions/{competitor_id}", response_model=AutoUpTargetOut)
async def patch_subscription(
    competitor_id: str, payload: AutoUpSubscriptionPatch
) -> AutoUpTargetOut:
    competitor_id = _competitor_id(competitor_id)
    if payload.enabled is None and payload.display_name is None:
        raise HTTPException(status_code=422, detail="至少提供一个修改字段")
    db = state.db
    assert db is not None
    async with _registration_lock:
        subscription = await db.get_autoup_subscription(competitor_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        target = await db.get_autoup_target(subscription["target_id"])
        if target is None:
            raise HTTPException(status_code=404, detail="采集目标不存在")
        monitor = await _monitor_for_target(target)
        if monitor is None:
            raise HTTPException(status_code=404, detail="底层采集目标不存在")
        await db.update_autoup_subscription(
            competitor_id,
            display_name=payload.display_name,
            enabled=payload.enabled,
        )
        if payload.enabled is not None:
            await _set_target_active(
                target, (await db.count_active_autoup_subscriptions(target["id"])) > 0
            )
    output = await _target_out(target)
    return output.model_copy(
        update={"display_name": payload.display_name or subscription["display_name"]}
    )


@router.delete("/subscriptions/{competitor_id}", status_code=204)
async def delete_subscription(competitor_id: str) -> Response:
    competitor_id = _competitor_id(competitor_id)
    db = state.db
    assert db is not None
    async with _registration_lock:
        target_id = await db.delete_autoup_subscription(competitor_id)
        if target_id is None:
            # Cloud may retry after losing a 204 response; deletion must be safely idempotent.
            return Response(status_code=204)
        target = await db.get_autoup_target(target_id)
        if target is not None:
            await _set_target_active(
                target, (await db.count_active_autoup_subscriptions(target_id)) > 0
            )
    return Response(status_code=204)


@router.get("/targets/{source_target_id}/changes", response_model=AutoUpChangesOut)
async def get_changes(
    source_target_id: str,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=200),
) -> AutoUpChangesOut:
    db = state.db
    assert db is not None
    target = await db.get_autoup_target(_source_target_id(source_target_id))
    if target is None:
        raise HTTPException(status_code=404, detail="采集目标不存在")
    try:
        after_time, after_id = decode_cursor(cursor)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    rows = await db.query_autoup_changes(
        target["platform"], target["monitor_id"], after_time, after_id, limit + 1
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = cursor
    if rows:
        next_cursor = encode_cursor(rows[-1]["updated_at"], int(rows[-1]["id"]))
    return AutoUpChangesOut.model_validate(
        {
            "target": (await _target_out(target)).model_dump(by_alias=True),
            "items": [
                {
                    "sourceRecordId": str(row["id"]),
                    "changedAt": row["updated_at"],
                    "payload": row,
                }
                for row in rows
            ],
            "nextCursor": next_cursor,
            "hasMore": has_more,
        }
    )
