from sqlite3 import IntegrityError

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import require_api_key
from ..platform.engine import MediaCrawlerError
from ..schemas import PlatformMonitorCreate, PlatformMonitorOut, PlatformPostOut
from ..state import state
from ..wechat import WechatAuthError, WechatError, WechatTargetError

router = APIRouter(
    prefix="/platform", tags=["platform"], dependencies=[Depends(require_api_key)]
)

_PLATFORMS = ("xhs", "dy", "ks", "wx")


@router.post("/monitors", response_model=PlatformMonitorOut, status_code=201)
async def create_platform_monitor(
    payload: PlatformMonitorCreate,
    creator: str = Depends(require_api_key),
) -> PlatformMonitorOut:
    creator_id = payload.creator_id
    if payload.platform == "wx":
        if state.wechat is None:
            raise HTTPException(status_code=503, detail="微信公众号采集器尚未初始化")
        try:
            creator_id = await state.wechat.resolve_target(creator_id)
        except WechatTargetError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except WechatAuthError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except WechatError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
    try:
        monitor = await state.db.create_platform_monitor(
            payload.platform, creator_id, payload.label, created_by=creator
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail="该平台下已存在相同 creator_id 的监控")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"创建失败: {e}")
    return PlatformMonitorOut.model_validate(monitor)


@router.get("/monitors", response_model=list[PlatformMonitorOut])
async def list_platform_monitors(
    platform: str | None = None,
) -> list[PlatformMonitorOut]:
    return [
        PlatformMonitorOut.model_validate(m)
        for m in await state.db.list_platform_monitors(platform)
    ]


@router.get("/monitors/{monitor_id}", response_model=PlatformMonitorOut)
async def get_platform_monitor(monitor_id: int) -> PlatformMonitorOut:
    monitor = await state.db.get_platform_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控不存在")
    return PlatformMonitorOut.model_validate(monitor)


@router.delete("/monitors/{monitor_id}")
async def delete_platform_monitor(monitor_id: int) -> dict:
    """软删除：置 active=0 停抓，历史内容保留。"""
    monitor = await state.db.get_platform_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控不存在")
    await state.db.update_platform_monitor(monitor_id, active=0, last_error=None)
    return {"ok": True}


@router.post("/monitors/{monitor_id}/resume", response_model=PlatformMonitorOut)
async def resume_platform_monitor(monitor_id: int) -> PlatformMonitorOut:
    """恢复：scheduler 的常驻循环下一 tick 会自动拾起 active 监控。"""
    monitor = await state.db.get_platform_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控不存在")
    monitor = await state.db.update_platform_monitor(
        monitor_id, active=1, last_error=None
    )
    return PlatformMonitorOut.model_validate(monitor)


@router.get("/posts", response_model=list[PlatformPostOut])
async def list_platform_posts(
    platform: str | None = None,
    monitor_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = None,
) -> list[PlatformPostOut]:
    return [
        PlatformPostOut.model_validate(p)
        for p in await state.db.query_platform_posts(
            platform=platform, monitor_id=monitor_id, limit=limit, before_id=before_id
        )
    ]


@router.get("/stats")
async def platform_stats() -> dict:
    monitors = await state.db.list_platform_monitors()
    scheduler = state.platform_scheduler
    per_monitor: dict[str, dict] = {}
    for m in monitors:
        per_monitor[str(m["id"])] = {
            "platform": m["platform"],
            "label": m["label"],
            "active": bool(m["active"]),
            "posts": await state.db.count_platform_posts(monitor_id=m["id"]),
        }
    per_platform: dict[str, dict] = {}
    for platform in _PLATFORMS:
        pm = [m for m in monitors if m["platform"] == platform]
        per_platform[platform] = {
            "monitors": len(pm),
            "active": sum(1 for m in pm if m["active"]),
            "posts": await state.db.count_platform_posts(platform=platform),
        }
    return {
        "per_platform": per_platform,
        "per_monitor": per_monitor,
        "runtime": scheduler.runtime_snapshot() if scheduler else [],
        "components": {
            "playwright_ready": state._bag.get("playwright_ready"),
            "mediacrawler_ready": state._bag.get("mediacrawler_ready"),
        },
    }


@router.post("/run/{platform}")
async def run_platform_now(platform: str) -> dict:
    """手动立即抓取一个平台（验证用）。"""
    if platform not in _PLATFORMS:
        raise HTTPException(status_code=400, detail=f"未知平台: {platform}")
    scheduler = state.platform_scheduler
    if scheduler is None:
        raise HTTPException(status_code=503, detail="平台调度未初始化")
    try:
        return await scheduler.trigger_platform(platform)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MediaCrawlerError as e:
        raise HTTPException(status_code=502, detail=str(e))
