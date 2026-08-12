"""平台轮询调度：每平台一个常驻循环，错峰 + 全局串行子进程 + 退避/自动暂停 + SSE 发布。

与 X 的 MonitorManager 不同：平台抓取是"按平台批量跑一个子进程"，不是逐监控高频轮询，
因此节奏按平台配置（mc_poll_interval_*），不是按监控项。
"""
import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..db import Database
from ..stream import SSEManager
from .engine import PLATFORMS, MediaCrawlerEngine

logger = logging.getLogger(__name__)

# 三平台启动错峰（秒）：避免同时起浏览器/占 CDP，也给风控留开窗
STAGGER = {"xhs": 0, "dy": 600, "ks": 1200}


@dataclass
class PlatformRuntime:
    consecutive_errors: int = 0
    total_runs: int = 0
    total_new: int = 0
    last_run_ms: int | None = None
    running: bool = False


class PlatformScheduler:
    def __init__(
        self,
        db: Database,
        engine: MediaCrawlerEngine,
        stream: SSEManager,
        settings: Settings,
    ):
        self._db = db
        self._engine = engine
        self._stream = stream
        self._settings = settings
        # 全局串行锁：三平台共用，保证同一时刻只有一个子进程在跑，
        # 避免 CDP 9222 端口争抢 + MediaCrawler 共享 sqlite 写冲突
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._runtime: dict[str, PlatformRuntime] = {}

    def _rt(self, platform: str) -> PlatformRuntime:
        rt = self._runtime.get(platform)
        if rt is None:
            rt = PlatformRuntime()
            self._runtime[platform] = rt
        return rt

    # ---- 生命周期 ----

    async def start(self) -> None:
        if not self._settings.mc_enabled:
            logger.warning("[platform] MC_ENABLED=false，平台监控跳过")
            return
        if not os.path.isdir(self._settings.mc_repo_path):
            logger.warning(
                "[platform] MC_REPO_PATH 不存在，平台监控跳过: %s",
                self._settings.mc_repo_path,
            )
            return
        for platform in PLATFORMS:
            self._tasks[platform] = asyncio.create_task(self._platform_loop(platform))
        logger.info(
            "[platform] 平台监控已启动（xhs/dy/ks），间隔=%s/%s/%s 秒",
            self._settings.mc_poll_interval("xhs"),
            self._settings.mc_poll_interval("dy"),
            self._settings.mc_poll_interval("ks"),
        )

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._runtime.clear()

    # ---- 轮询循环 ----

    async def _platform_loop(self, platform: str) -> None:
        base = self._settings.mc_poll_interval(platform)
        # 启动错峰 + 随机 jitter
        await self._sleep(STAGGER.get(platform, 0) + random.uniform(0, self._settings.jitter_factor * base))
        while True:
            rt = self._rt(platform)
            active = [m for m in await self._db.list_platform_monitors(platform) if m["active"]]
            if active:
                try:
                    await self._run_once(platform, active)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[platform] %s 轮询异常（_run_once 内部已记录错误）", platform)
            interval = self._next_interval(base, rt.consecutive_errors)
            await self._sleep(interval)

    async def _run_once(self, platform: str, active: list[dict[str, Any]]) -> None:
        rt = self._rt(platform)
        if rt.running:
            return  # 防重入（手动触发/定时循环共用）
        rt.running = True
        start = time.monotonic()
        try:
            async with self._lock:
                result = await self._engine.run_platform(platform, active)
            for m in active:
                await self._db.mark_platform_poll(m["id"], None)
            for post in result.new_posts:
                rt.total_new += 1
                self._stream.publish(
                    {
                        "type": "platform_post",
                        "platform": post["platform"],
                        "monitor_id": post["monitor_id"],
                        "post": post,
                    }
                )
            rt.consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            rt.consecutive_errors += 1
            # 有些异常 str(e) 是空串（如 asyncio.TimeoutError），光写 ": " 用户看不出原因，
            # 退而给出异常类型；子进程输出的完整日志在 engine 侧已落盘 data/logs/
            detail = str(e) if str(e) else f"{type(e).__name__}（无异常信息）"
            msg = f"平台{platform}抓取失败: {detail}"
            for m in active:
                await self._db.mark_platform_poll(m["id"], msg)
            if rt.consecutive_errors >= self._settings.pause_after_errors:
                msg2 = f"连续 {rt.consecutive_errors} 次失败，已自动暂停"
                for m in active:
                    await self._db.update_platform_monitor(
                        m["id"], active=0, last_error=msg2
                    )
                rt.consecutive_errors = 0  # 暂停后归零，resume 重新计
        finally:
            rt.running = False
            rt.last_run_ms = int((time.monotonic() - start) * 1000)
            rt.total_runs += 1

    def _next_interval(self, base: int, errors: int) -> int:
        if errors == 0:
            return base
        factor = min(2 ** errors, self._settings.max_poll_interval / base)
        return min(int(base * factor), self._settings.max_poll_interval)

    async def _sleep(self, interval: int) -> None:
        jitter = random.uniform(0, self._settings.jitter_factor) * interval
        try:
            await asyncio.sleep(max(0.0, interval + jitter))
        except asyncio.CancelledError:
            raise

    # ---- 手动触发 / 状态 ----

    async def trigger_platform(self, platform: str) -> dict[str, Any]:
        active = [m for m in await self._db.list_platform_monitors(platform) if m["active"]]
        if not active:
            raise ValueError(f"平台 {platform} 没有运行中的监控")
        if self._rt(platform).running:
            raise ValueError(f"平台 {platform} 正在抓取中")
        asyncio.create_task(self._run_once(platform, active))
        return {
            "ok": True,
            "platform": platform,
            "monitors": [m["id"] for m in active],
        }

    def runtime_snapshot(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for platform, rt in self._runtime.items():
            out.append(
                {
                    "platform": platform,
                    "consecutive_errors": rt.consecutive_errors,
                    "total_runs": rt.total_runs,
                    "total_new": rt.total_new,
                    "last_run_ms": rt.last_run_ms,
                    "running": rt.running,
                }
            )
        return out
