import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import Database
from .scraper import Scraper
from .stream import SSEManager

POLL_LIMIT = 15


@dataclass
class MonitorRuntime:
    """每个监控的运行时状态（内存态，重启重置，不落库）。"""

    consecutive_errors: int = 0
    total_polls: int = 0
    total_new: int = 0
    last_poll_ms: int | None = None
    current_interval: int | None = None
    _loop_started: bool = field(default=False, repr=False)


class MonitorManager:
    def __init__(
        self,
        db: Database,
        scraper: Scraper,
        stream: SSEManager,
        settings: Settings,
    ):
        self._db = db
        self._scraper = scraper
        self._stream = stream
        self._settings = settings
        self._tasks: dict[int, asyncio.Task] = {}
        self._runtime: dict[int, MonitorRuntime] = {}

    async def start(self) -> None:
        for m in await self._db.list_monitors():
            if m["active"]:
                self._tasks[m["id"]] = asyncio.create_task(self._poll_loop(m["id"]))

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._runtime.clear()
        await self._scraper.close()

    # ---- 状态 ----

    def _rt(self, monitor_id: int) -> MonitorRuntime:
        rt = self._runtime.get(monitor_id)
        if rt is None:
            rt = MonitorRuntime()
            self._runtime[monitor_id] = rt
        return rt

    def runtime_snapshot(self) -> list[dict[str, Any]]:
        """供 /stats 使用的每监控运行态快照。"""
        out: list[dict[str, Any]] = []
        for mid, rt in self._runtime.items():
            out.append(
                {
                    "monitor_id": mid,
                    "consecutive_errors": rt.consecutive_errors,
                    "total_polls": rt.total_polls,
                    "total_new": rt.total_new,
                    "last_poll_ms": rt.last_poll_ms,
                    "current_interval": rt.current_interval,
                    "task_alive": self.task_alive(mid),
                }
            )
        return out

    def task_alive(self, monitor_id: int) -> bool:
        task = self._tasks.get(monitor_id)
        return task is not None and not task.done()

    # ---- 监控生命周期 ----

    async def add_monitor(
        self, username: str, interval_seconds: int | None, created_by: str | None = None
    ) -> dict[str, Any]:
        interval = interval_seconds or self._settings.default_poll_interval
        user = await self._scraper.resolve_user(username)
        if user is None:
            raise ValueError(f"无法解析用户 {username}")
        monitor = await self._db.create_monitor(
            username=user.username,
            user_id=user.user_id,
            display_name=user.display_name,
            interval_seconds=interval,
            created_by=created_by,
        )
        self._tasks[monitor["id"]] = asyncio.create_task(self._poll_loop(monitor["id"]))
        return monitor

    async def remove_monitor(self, monitor_id: int) -> bool:
        monitor = await self._db.get_monitor(monitor_id)
        if monitor is None:
            return False
        await self._db.update_monitor(monitor_id, active=0, last_error=None)
        task = self._tasks.pop(monitor_id, None)
        if task is not None:
            task.cancel()
        self._runtime.pop(monitor_id, None)
        return True

    async def resume_monitor(self, monitor_id: int) -> dict[str, Any] | None:
        monitor = await self._db.get_monitor(monitor_id)
        if monitor is None:
            return None
        if not monitor["active"]:
            await self._db.update_monitor(monitor_id, active=1, last_error=None)
            self._runtime.pop(monitor_id, None)
        if not self.task_alive(monitor_id):
            self._tasks[monitor_id] = asyncio.create_task(self._poll_loop(monitor_id))
        return await self._db.get_monitor(monitor_id)

    async def set_monitor_active(self, monitor_id: int, active: bool) -> dict[str, Any] | None:
        if active:
            return await self.resume_monitor(monitor_id)
        monitor = await self._db.get_monitor(monitor_id)
        if monitor is None:
            return None
        await self._db.update_monitor(monitor_id, active=0, last_error=None)
        task = self._tasks.pop(monitor_id, None)
        if task is not None:
            task.cancel()
        self._runtime.pop(monitor_id, None)
        return await self._db.get_monitor(monitor_id)

    # ---- 轮询循环 ----

    async def _poll_loop(self, monitor_id: int) -> None:
        rt = self._rt(monitor_id)
        try:
            while True:
                monitor = await self._db.get_monitor(monitor_id)
                if monitor is None or not monitor["active"]:
                    return

                base_interval = monitor["interval_seconds"]
                if rt.current_interval is None:
                    rt.current_interval = base_interval

                start = time.monotonic()
                pause = False
                try:
                    tweets = await self._scraper.recent_tweets(monitor["user_id"], limit=POLL_LIMIT)
                    last_seen = monitor["last_seen_tweet_id"]
                    for t in tweets:
                        t["monitor_id"] = monitor_id
                        if await self._db.insert_tweet(t):
                            rt.total_new += 1
                            self._stream.publish(
                                {"type": "tweet", "monitor_id": monitor_id, "tweet": t}
                            )
                        if last_seen is None or t["id"] > last_seen:
                            last_seen = t["id"]

                    await self._db.mark_poll(monitor_id, last_seen, None)
                    rt.consecutive_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    rt.consecutive_errors += 1
                    detail = str(e) or f"{type(e).__name__}（无异常信息）"
                    await self._db.mark_poll(monitor_id, None, detail)
                    if rt.consecutive_errors >= self._settings.pause_after_errors:
                        msg = f"连续 {rt.consecutive_errors} 次失败，已自动暂停: {detail}"
                        await self._db.update_monitor(monitor_id, active=0, last_error=msg)
                        pause = True

                rt.last_poll_ms = int((time.monotonic() - start) * 1000)
                rt.total_polls += 1
                rt.current_interval = self._next_interval(base_interval, rt.consecutive_errors)

                if pause:
                    return

                await self._sleep(rt.current_interval)
        finally:
            # 循环退出（暂停/删除/停服）时清理任务引用，避免已完成任务堆积
            self._tasks.pop(monitor_id, None)

    def _next_interval(self, base: int, errors: int) -> int:
        if errors == 0:
            return base
        # 指数退避：base * 2^errors，封顶 max_poll_interval
        factor = min(2 ** errors, self._settings.max_poll_interval / base)
        return min(int(base * factor), self._settings.max_poll_interval)

    async def _sleep(self, interval: int) -> None:
        # 错峰：每次 sleep 加 0~jitter_factor 比例的随机偏移，避免所有监控同时打接口
        jitter = random.uniform(0, self._settings.jitter_factor) * interval
        try:
            await asyncio.sleep(interval + jitter)
        except asyncio.CancelledError:
            raise
