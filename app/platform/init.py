"""MediaCrawler 子模块一键初始化（幂等，lifespan 启动时调用）。

首次启动依次做三件事：uv sync 建 venv → 应用 dy/ks 补丁（整文件复制）→ 装 Playwright Chromium。
之后每次启动都有快路径（.venv 存在 / 文件 sha256 一致 / 浏览器已装）直接跳过，毫秒级。
任何一步失败只记 error 不抛，保证主项目照常启动（抓取失败由各监控 last_error 呈现）。
"""
import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..config import Settings

logger = logging.getLogger(__name__)

# 补丁清单：仓库内已打补丁的完整文件 → submodule 内目标文件（相对 mediacrawler 根）
# 说明：
#  - dy/ks 的 client.py 原代码不尊重 --crawler_max_notes_count 上限，需替换；
#  - dy 的 store/douyin/__init__.py 用 author.uid（数字）做创作者匿名哈希，而监控侧存的是
#    sec_user_id，归属永远对不上 → 改为哈希 author.sec_uid（与查询串同值），帖子才能落对监控；
# MediaCrawler 行尾是 CRLF，git apply 打不上，故用整文件复制 + sha256 判定幂等。
_PATCHES = [
    (
        Path(__file__).resolve().parents[2]
        / "patches" / "mediacrawler" / "media_platform" / "douyin" / "client.py",
        Path("media_platform") / "douyin" / "client.py",
    ),
    (
        Path(__file__).resolve().parents[2]
        / "patches" / "mediacrawler" / "media_platform" / "kuaishou" / "client.py",
        Path("media_platform") / "kuaishou" / "client.py",
    ),
    (
        Path(__file__).resolve().parents[2]
        / "patches" / "mediacrawler" / "store" / "douyin" / "__init__.py",
        Path("store") / "douyin" / "__init__.py",
    ),
]

_SYNC_TIMEOUT = 1800  # uv sync / playwright install 首次可慢


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _run(
    cmd: list[str], cwd: Path, timeout: int, extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    """跑子进程，返回 (returncode, 输出尾部)。Windows 隐藏控制台窗口 + UTF-8。

    用阻塞 subprocess.run 放进线程池（asyncio.to_thread），避免 Windows 上
    uvicorn --reload 用 SelectorEventLoop（不支持 asyncio.create_subprocess_exec）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra_env:
        env.update(extra_env)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _sync() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=flags,
        )

    try:
        proc = await asyncio.to_thread(_sync)
    except subprocess.TimeoutExpired:
        return -1, f"<timeout>{timeout}s>"
    return proc.returncode, (proc.stdout or b"").decode("utf-8", errors="replace")


def _chromium_installed() -> bool:
    """Playwright 的 Chromium 是否已装（查浏览器缓存目录，尊重 PLAYWRIGHT_BROWSERS_PATH）。"""
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        base = Path(env_path)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    else:
        base = Path.home() / ".cache" / "ms-playwright"
    if not base.is_dir():
        return False
    return any(p.name.startswith("chromium") for p in base.iterdir() if p.is_dir())


async def ensure_mediacrawler_ready(settings: Settings) -> None:
    if not settings.mc_enabled:
        logger.warning("[platform-init] MC_ENABLED=false，跳过 MediaCrawler 初始化")
        return
    repo = Path(settings.mc_repo_path).resolve()
    if not repo.is_dir():
        logger.warning("[platform-init] MC_REPO_PATH 不存在，跳过初始化: %s", repo)
        return

    # 1) uv sync 建 venv（首次慢，之后快路径跳过）
    if not (repo / ".venv").is_dir():
        logger.info("[platform-init] MediaCrawler 首次初始化：uv sync（首次较慢）…")
        code, tail = await _run(["uv", "sync"], repo, _SYNC_TIMEOUT)
        if code != 0:
            logger.error("[platform-init] uv sync 失败(%s): %s", code, tail[-1000:])
        else:
            logger.info("[platform-init] uv sync 完成")
    else:
        logger.info("[platform-init] MediaCrawler 已就绪（跳过 uv sync）")

    # 2) 应用 dy/ks 补丁（整文件复制，sha256 幂等）
    for src, rel in _PATCHES:
        if not src.is_file():
            logger.error("[platform-init] 补丁文件缺失: %s", src)
            continue
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and _sha256(src) == _sha256(dst):
            logger.info("[platform-init] 补丁已应用: %s", rel)
            continue
        shutil.copy2(src, dst)
        logger.info("[platform-init] 已应用补丁: %s", rel)

    # 3) Playwright Chromium（国内直连 Playwright CDN 常被墙，走 npmmirror 镜像）
    if not _chromium_installed():
        logger.info("[platform-init] 安装 Playwright Chromium（npmmirror 镜像）…")
        code, tail = await _run(
            ["uv", "run", "playwright", "install", "chromium"],
            repo,
            _SYNC_TIMEOUT,
            extra_env={
                "PLAYWRIGHT_DOWNLOAD_HOST": "https://npmmirror.com/mirrors/playwright",
            },
        )
        if code != 0:
            logger.error(
                "[platform-init] playwright install chromium 失败(%s): %s",
                code, tail[-1000:],
            )
        else:
            logger.info("[platform-init] Chromium 就绪")
    else:
        logger.info("[platform-init] Chromium 已装（跳过）")

    logger.info("[platform-init] MediaCrawler 就绪: %s", repo)
