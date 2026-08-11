import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from ..deps import require_api_key
from ..state import state

router = APIRouter(prefix="/stream", tags=["stream"], dependencies=[Depends(require_api_key)])

PING_INTERVAL = 15.0


@router.get("")
async def stream(monitor_id: int | None = Query(default=None)) -> EventSourceResponse:
    queue = state.stream.subscribe()

    async def events() -> AsyncIterator[dict]:
        # 先补发最近历史事件（回放），再进入实时循环
        for ev in state.stream.replay(monitor_id):
            yield {"event": "tweet", "data": json.dumps(ev, ensure_ascii=False)}
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
                    continue
                if monitor_id is not None and data.get("monitor_id") != monitor_id:
                    continue
                yield {"event": "tweet", "data": json.dumps(data, ensure_ascii=False)}
        finally:
            state.stream.unsubscribe(queue)

    return EventSourceResponse(events())
