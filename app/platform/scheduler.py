"""平台轮询调度：每平台一个常驻循环，错峰 + 全局串行 + 退避/自动暂停 + SSE 发布。

抖音/快手/小红书在平台轮询内逐目标运行 MediaCrawler，微信按平台批量请求后台接口；
两者都复用现有运行态、手动触发与 SSE，轮询节奏按平台配置而不是按监控项。
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
from .engine import PLATFORMS as MEDIA_PLATFORMS, MediaCrawlerEngine
from ..wechat import WechatService

logger = logging.getLogger(__name__)

# 四个平台启动错峰（秒）：避免同时占采集资源，也给风控留开窗
PLATFORMS = (*MEDIA_PLATFORMS, "wx")
STAGGER = {"xhs": 0, "wx": 300, "dy": 600, "ks": 1200}


@dataclass
class PlatformRuntime:
    consecutive_errors: int = 0
    total_runs: int = 0
    total_new: int = 0
    last_run_ms: int | None = None
    running: bool = False
    last_error: str | None = None


class PlatformScheduler:
    def __init__(
        self,
        db: Database,
        engine: MediaCrawlerEngine,
        wechat: WechatService,
        stream: SSEManager,
        settings: Settings,
    ):
        self._db = db
        self._engine = engine
        self._wechat = wechat
        self._stream = stream
        self._settings = settings
        # 全局串行锁：沿用三平台的单采集任务边界，避免 CDP 9222 端口争抢和共享 sqlite 写冲突。
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._runtime: dict[str, PlatformRuntime] = {}
        self._monitor_errors: dict[int, int] = {}

    def _rt(self, platform: str) -> PlatformRuntime:
        rt = self._runtime.get(platform)
        if rt is None:
            rt = PlatformRuntime()
            self._runtime[platform] = rt
        return rt

    # ---- 生命周期 ----

    async def start(self) -> None:
        media_ready = self._settings.mc_enabled and os.path.isdir(self._settings.mc_repo_path)
        if self._settings.mc_enabled and not media_ready:
            logger.warning(
                "[platform] MC_REPO_PATH 不存在，MediaCrawler 平台监控跳过: %s",
                self._settings.mc_repo_path,
            )
        for platform in PLATFORMS:
            if platform in MEDIA_PLATFORMS and not media_ready:
                continue
            self._tasks[platform] = asyncio.create_task(self._platform_loop(platform))
        logger.info("[platform] 平台监控已启动: %s", "/".join(self._tasks))

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._runtime.clear()
        self._monitor_errors.clear()

    # ---- 轮询循环 ----

    async def _platform_loop(self, platform: str) -> None:
        base = (
            self._settings.wechat_poll_interval
            if platform == "wx"
            else self._settings.mc_poll_interval(platform)
        )
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
        new_posts: list[dict[str, Any]] = []
        try:
            async with self._lock:
                # ponytail: 共用现有全局锁；平台量明显增加时再拆成浏览器锁和 HTTP 锁。
                if platform == "wx":
                    new_posts = await self._wechat.collect(active)
                    for m in active:
                        await self._db.mark_platform_poll(m["id"], None)
                    rt.consecutive_errors = 0
                    rt.last_error = None
                else:
                    failures: list[tuple[dict[str, Any], str]] = []
                    for m in active:
                        try:
                            result = await self._engine.run_platform(platform, [m])
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            detail = str(error) or f"{type(error).__name__}（无异常信息）"
                            failures.append((m, detail))
                            count = self._monitor_errors.get(m["id"], 0) + 1
                            self._monitor_errors[m["id"]] = count
                            await self._db.mark_platform_poll(
                                m["id"], f"{m['label']}抓取失败: {detail}"
                            )
                            if count >= self._settings.pause_after_errors:
                                await self._db.update_platform_monitor(
                                    m["id"],
                                    active=0,
                                    last_error=f"连续 {count} 次失败，已自动暂停: {detail}",
                                )
                                self._monitor_errors.pop(m["id"], None)
                        else:
                            self._monitor_errors.pop(m["id"], None)
                            await self._db.mark_platform_poll(m["id"], None)
                            new_posts.extend(result.new_posts)
                    if failures:
                        rt.consecutive_errors += 1
                        failed_details = "；".join(
                            f"{m['label']}: {detail}" for m, detail in failures
                        )
                        rt.last_error = (
                            f"平台{platform}有 {len(failures)}/{len(active)} 个目标失败: "
                            f"{failed_details}"
                        )
                    else:
                        rt.consecutive_errors = 0
                        rt.last_error = None
            for post in new_posts:
                rt.total_new += 1
                self._stream.publish(
                    {
                        "type": "platform_post",
                        "platform": post["platform"],
                        "monitor_id": post["monitor_id"],
                        "post": post,
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            rt.consecutive_errors += 1
            # 有些异常 str(e) 是空串（如 asyncio.TimeoutError），光写 ": " 用户看不出原因，
            # 退而给出异常类型；子进程输出的完整日志在 engine 侧已落盘 data/logs/
            detail = str(e) if str(e) else f"{type(e).__name__}（无异常信息）"
            msg = f"平台{platform}抓取失败: {detail}"
            rt.last_error = msg
            for m in active:
                await self._db.mark_platform_poll(m["id"], msg)
            if rt.consecutive_errors >= self._settings.pause_after_errors:
                msg2 = f"连续 {rt.consecutive_errors} 次失败，已自动暂停: {detail}"
                for m in active:
                    await self._db.update_platform_monitor(
                        m["id"], active=0, last_error=msg2
                    )
                rt.consecutive_errors = 0  # 暂停后归零，resume 重新计
                rt.last_error = msg2
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
        if platform not in PLATFORMS:
            raise ValueError(f"未知平台: {platform}")
        if platform in MEDIA_PLATFORMS and (
            not self._settings.mc_enabled or not os.path.isdir(self._settings.mc_repo_path)
        ):
            raise ValueError("MediaCrawler 未启用或目录不存在")
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
        for platform in PLATFORMS:
            rt = self._runtime.get(platform) or PlatformRuntime()
            task = self._tasks.get(platform)
            scheduled = task is not None and not task.done()
            unavailable_reason = None
            if platform in MEDIA_PLATFORMS and not self._settings.mc_enabled:
                unavailable_reason = "MediaCrawler 未启用"
            elif platform in MEDIA_PLATFORMS and not os.path.isdir(self._settings.mc_repo_path):
                unavailable_reason = "MediaCrawler 目录不存在"
            elif task is None:
                unavailable_reason = "调度任务未启动"
            elif task.done():
                if task.cancelled():
                    unavailable_reason = "调度任务已停止"
                else:
                    error = task.exception()
                    unavailable_reason = (
                        str(error) or type(error).__name__
                        if error is not None
                        else "调度任务已停止"
                    )
            out.append(
                {
                    "platform": platform,
                    "consecutive_errors": rt.consecutive_errors,
                    "total_runs": rt.total_runs,
                    "total_new": rt.total_new,
                    "last_run_ms": rt.last_run_ms,
                    "running": rt.running,
                    "last_error": rt.last_error,
                    "scheduled": scheduled,
                    "unavailable_reason": unavailable_reason,
                }
            )
        return out
