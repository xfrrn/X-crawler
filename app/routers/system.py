from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..deps import require_api_key
from ..state import state

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    """存活探针（公开，无需 API Key）。"""
    monitors = await state.db.list_monitors()
    active = sum(1 for m in monitors if m["active"])
    return {
        "status": "ok",
        "scraper_mode": get_settings().scraper_mode,
        "monitors_total": len(monitors),
        "monitors_active": active,
    }


@router.get("/stats", dependencies=[Depends(require_api_key)])
async def stats() -> dict:
    """聚合统计：每监控运行态、全局抓取量、采集账号健康。"""
    settings = get_settings()
    monitors = await state.db.list_monitors()
    active = sum(1 for m in monitors if m["active"])

    today = datetime.now(timezone.utc).date().isoformat()
    runtime_by_id = {r["monitor_id"]: r for r in state.manager.runtime_snapshot()}

    detail = []
    for m in monitors:
        rt = runtime_by_id.get(m["id"], {})
        detail.append(
            {
                "id": m["id"],
                "username": m["username"],
                "interval_seconds": m["interval_seconds"],
                "active": bool(m["active"]),
                "current_interval": rt.get("current_interval"),
                "consecutive_errors": rt.get("consecutive_errors", 0),
                "total_polls": rt.get("total_polls", 0),
                "total_new": rt.get("total_new", 0),
                "last_poll_ms": rt.get("last_poll_ms"),
                "last_poll_at": m["last_poll_at"],
                "last_error": m["last_error"],
                "last_seen_tweet_id": m["last_seen_tweet_id"],
            }
        )

    uptime = None
    if state.started_at is not None:
        uptime = int((datetime.now(timezone.utc) - state.started_at).total_seconds())

    return {
        "uptime_seconds": uptime,
        "scraper_mode": settings.scraper_mode,
        "monitors_total": len(monitors),
        "monitors_active": active,
        "monitors_paused": len(monitors) - active,
        "tweets_total": await state.db.count_tweets(),
        "tweets_today": await state.db.count_tweets_since(today),
        "accounts": await state.scraper.pool_stats(),
        "account_infos": await state.scraper.account_infos(),
        "monitors_detail": detail,
    }
