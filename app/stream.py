import asyncio
from collections import deque
from typing import Any


class SSEManager:
    """广播新推文事件给所有订阅的 SSE 连接，并保留最近 N 条供新订阅者回放。

    队列满时丢弃最旧事件（实时流不保全量，历史靠 DB 查询 + 回放缓冲兜底）。
    """

    def __init__(self, maxsize: int = 100, replay_size: int = 50):
        self._maxsize = maxsize
        self._history: deque[dict[str, Any]] = deque(maxlen=replay_size)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def replay(self, monitor_id: int | None = None) -> list[dict[str, Any]]:
        if monitor_id is None:
            return list(self._history)
        return [e for e in self._history if e.get("monitor_id") == monitor_id]

    def publish(self, data: dict[str, Any]) -> None:
        self._history.append(data)
        for q in list(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
