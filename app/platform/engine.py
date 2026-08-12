"""MediaCrawler 引擎：拼子进程命令 → 跑抓取 → 读它的 sqlite 产物 → 归一化/归属 → UPSERT 入库。

MediaCrawler 是外部 CLI 应用（不是库），本模块只通过 subprocess 调用它，
不 import 任何 MediaCrawler 内部模块（避免与它全局 config / Playwright 登录耦合）。
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ..config import Settings
from ..db import Database, row_to_dict

logger = logging.getLogger(__name__)

# MediaCrawler 的 sqlite 产物表名（白名单，杜绝 SQL 注入）
PLATFORM_TABLE = {"xhs": "xhs_note", "dy": "douyin_aweme", "ks": "kuaishou_video"}
PLATFORMS = ("xhs", "dy", "ks")


class MediaCrawlerError(RuntimeError):
    """MediaCrawler 子进程或产物异常。"""


@dataclass
class EngineResult:
    new_posts: list[dict[str, Any]]
    raw_count: int
    exit_code: int
    output_tail: str


# ---- 时间/JSON 工具 ----

def _unix_to_iso(ts: Any) -> str | None:
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _json_list(joined: str | None) -> str | None:
    """逗号拼接的多图 URL 串 → JSON 数组字符串（落库前）。"""
    items = [u for u in (joined or "").split(",") if u]
    return json.dumps(items, ensure_ascii=False) if items else None


# ---- 归属：MediaCrawler 匿名化后行里只有 creator_hash（sha256(原始user_id)[:16]，见
# 其 tools/user_hash.py）。我们从监控填的 creator_id 里提取原始 id、复算同样的 hash，
# 把产物行归属到对应的 platform_monitor。----

_CREATOR_ID_PATTERNS = {
    "xhs": re.compile(r"/user/profile/([0-9a-fA-F]+)"),
    "dy": re.compile(r"douyin\.com/user/([^?/]+)"),
    "ks": re.compile(r"kuaishou\.com/profile/([^?/]+)"),
}


def _candidate_raw_ids(platform: str, creator_id: str) -> list[str]:
    """从 URL 或裸 id 提取可能的原始 id 候选（URL 命中 + 去掉 query 的裸串）。"""
    s = creator_id.strip()
    out: list[str] = []
    m = _CREATOR_ID_PATTERNS[platform].search(s)
    if m:
        out.append(m.group(1))
    bare = s.split("?")[0].rstrip("/")
    if bare:
        out.append(bare)
    return out


def _expected_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_hash_map(monitors: list[dict]) -> dict[str, int]:
    """creator_hash → monitor_id 的映射。monitors 是 db.list_platform_monitors(platform) 的行。"""
    m: dict[str, int] = {}
    for mon in monitors:
        for raw in _candidate_raw_ids(mon["platform"], mon["creator_id"]):
            m.setdefault(_expected_hash(raw), mon["id"])
    return m


# ---- 归一化：MediaCrawler 平台表行 → platform_posts 行 ----

def _normalize_row(platform: str, row: dict[str, Any], monitor_id: int) -> dict[str, Any]:
    base = {
        "platform": platform,
        "monitor_id": monitor_id,
        "creator_hash": row.get("creator_hash"),
        "title": row.get("title"),
        "content": row.get("desc"),
        "raw_json": json.dumps(dict(row), ensure_ascii=False, default=str),
    }
    if platform == "xhs":
        return {
            **base,
            "content_id": row.get("note_id"),
            "created_at": _unix_to_iso(row.get("time")),
            "image_urls": _json_list(row.get("image_list")),
            "video_url": row.get("video_url"),
            "cover_url": None,
            "stats": json.dumps(
                {
                    "likes": row.get("liked_count"),
                    "comments": row.get("comment_count"),
                    "shares": row.get("share_count"),
                    "views": None,
                },
                ensure_ascii=False,
            ),
        }
    if platform == "dy":
        return {
            **base,
            "content_id": row.get("aweme_id"),
            "created_at": _unix_to_iso(row.get("create_time")),
            "image_urls": _json_list(row.get("note_download_url")),
            "video_url": row.get("video_download_url"),
            "cover_url": row.get("cover_url"),
            "stats": json.dumps(
                {
                    "likes": row.get("liked_count"),
                    "comments": row.get("comment_count"),
                    "shares": row.get("share_count"),
                    "views": None,
                },
                ensure_ascii=False,
            ),
        }
    if platform == "ks":
        return {
            **base,
            "content_id": row.get("video_id"),
            "created_at": _unix_to_iso(row.get("create_time")),
            "image_urls": None,
            "video_url": row.get("video_play_url") or row.get("video_url"),
            "cover_url": row.get("video_cover_url"),
            # 注意 MediaCrawler 里快手是拼写 viewd_count
            "stats": json.dumps(
                {
                    "likes": row.get("liked_count"),
                    "comments": None,
                    "shares": None,
                    "views": row.get("viewd_count"),
                },
                ensure_ascii=False,
            ),
        }
    raise MediaCrawlerError(f"未知平台: {platform}")


class MediaCrawlerEngine:
    def __init__(self, settings: Settings, db: Database):
        self._settings = settings
        self._db = db

    def _build_cmd(self, platform: str, creator_ids: list[str]) -> list[str]:
        cmd = [
            "uv", "run", "python", "main.py",
            "--platform", platform,
            "--lt", self._settings.mc_login_type,
            "--type", "creator",
            "--creator_id", ",".join(creator_ids),
            "--save_data_option", "sqlite",
            "--get_comment", "false",
            "--crawler_max_notes_count", str(self._settings.mc_max_posts_per_creator),
        ]
        if self._settings.mc_login_type == "cookie":
            cookies = self._settings.mc_cookies(platform)
            if cookies:
                cmd += ["--cookies", cookies]
        if self._settings.mc_headless:
            cmd += ["--headless", "true"]
        return cmd

    async def run_platform(self, platform: str, monitors: list[dict]) -> EngineResult:
        """跑一轮某平台的全部 active 监控，归一化后 UPSERT 入库，返回本次新插入的行。"""
        # 相对路径（默认 ./mediacrawler）按项目根解析为绝对路径
        repo = Path(self._settings.mc_repo_path).resolve()
        creator_ids = [m["creator_id"] for m in monitors]
        cmd = self._build_cmd(platform, creator_ids)

        if not repo.is_dir():
            raise MediaCrawlerError(f"MC_REPO_PATH 不存在: {repo}")
        os.makedirs(repo / "database", exist_ok=True)

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        logger.info("[platform] 启动子进程: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            out_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._settings.mc_subprocess_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise MediaCrawlerError(
                f"MediaCrawler 超时(>{self._settings.mc_subprocess_timeout}s): {platform} {','.join(creator_ids)}"
            )
        output = out_bytes.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise MediaCrawlerError(
                f"MediaCrawler 退出码 {proc.returncode}: {output[-2000:]}"
            )

        # 读产物（子进程已退出，SQLAlchemy 已 commit，只读无锁冲突）
        db_path = repo / "database" / "sqlite_tables.db"
        if not os.path.exists(db_path):
            raise MediaCrawlerError(
                "MediaCrawler 未生成 sqlite 库（可能登录失败/未跑到写库步骤）"
            )
        table = PLATFORM_TABLE[platform]
        reader = await aiosqlite.connect(db_path)
        reader.row_factory = aiosqlite.Row
        try:
            cur = await reader.execute(f"SELECT * FROM {table}")
            rows = [row_to_dict(r) for r in await cur.fetchall()]
        finally:
            await reader.close()

        # 归一化 + 归属
        hash_map = build_hash_map(monitors)
        normalized: list[dict[str, Any]] = []
        for row in rows:
            monitor_id = hash_map.get(row.get("creator_hash"))
            if monitor_id is None and len(monitors) == 1:
                # 兜底：平台只有一个监控时，产物行大概率都来自它（复算 hash 偶发对不上时）
                monitor_id = monitors[0]["id"]
            if monitor_id is None:
                logger.warning(
                    "[platform] 无法归属行: %s creator_hash=%s（多监控时请填规范的 creator_id）",
                    platform, row.get("creator_hash"),
                )
                continue
            p = _normalize_row(platform, row, monitor_id)
            if not p["content_id"]:
                continue
            normalized.append(p)

        new_posts = await self._db.upsert_platform_posts(normalized)
        return EngineResult(
            new_posts=new_posts,
            raw_count=len(rows),
            exit_code=proc.returncode,
            output_tail=output[-2000:],
        )
