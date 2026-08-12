import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from ..deps import require_api_key
from ..state import state

router = APIRouter(prefix="/stream", tags=["stream"], dependencies=[Depends(require_api_key)])

PING_INTERVAL = 15.0


def _event_name(data: dict) -> str:
    """推文事件是 tweet；平台监控新动态是 platform_post。"""
    return "platform_post" if data.get("type") == "platform_post" else "tweet"


@router.get("")
async def stream(monitor_id: int | None = Query(default=None)) -> EventSourceResponse:
    queue = state.stream.subscribe()

    async def events() -> AsyncIterator[dict]:
        # 先补发最近历史事件（回放），再进入实时循环
        for ev in state.stream.replay(monitor_id):
            yield {"event": _event_name(ev), "data": json.dumps(ev, ensure_ascii=False)}
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
                    continue
                # X 与平台监控的 monitor_id 主键数值可能重叠，消费者按事件名区分；
                # 传了 monitor_id 时仍按数值过滤（同一事件域内的精确筛选）
                if monitor_id is not None and data.get("monitor_id") != monitor_id:
                    continue
                yield {"event": _event_name(data), "data": json.dumps(data, ensure_ascii=False)}
        finally:
            state.stream.unsubscribe(queue)

    return EventSourceResponse(events())
