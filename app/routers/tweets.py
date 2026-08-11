from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import require_api_key
from ..schemas import TweetOut
from ..state import state

router = APIRouter(tags=["tweets"], dependencies=[Depends(require_api_key)])


@router.get("/monitors/{monitor_id}/tweets", response_model=list[TweetOut])
async def get_monitor_tweets(
    monitor_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    since_id: int | None = None,
    before_id: int | None = None,
) -> list[TweetOut]:
    if await state.db.get_monitor(monitor_id) is None:
        raise HTTPException(status_code=404, detail="监控不存在")
    rows = await state.db.query_tweets(
        monitor_id=monitor_id, limit=limit, since_id=since_id, before_id=before_id
    )
    return [TweetOut.model_validate(r) for r in rows]


@router.get("/tweets", response_model=list[TweetOut])
async def get_tweets(
    username: str | None = None,
    monitor_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    since_id: int | None = None,
    before_id: int | None = None,
) -> list[TweetOut]:
    rows = await state.db.query_tweets(
        monitor_id=monitor_id,
        username=username,
        limit=limit,
        since_id=since_id,
        before_id=before_id,
    )
    return [TweetOut.model_validate(r) for r in rows]
