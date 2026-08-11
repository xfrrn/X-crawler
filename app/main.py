from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import Database
from .manager import MonitorManager
from .routers import accounts, admin, monitors, stream, system, tweets
from .scraper import create_scraper
from .state import state
from .stream import SSEManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.app_db_path)
    await db.connect()
    scraper = create_scraper(
        settings.scraper_mode, settings.accounts_db_path, settings.mock_fail_start
    )
    stream_bus = SSEManager(replay_size=settings.sse_replay_size)
    manager = MonitorManager(db, scraper, stream_bus, settings)

    state.db = db
    state.scraper = scraper
    state.stream = stream_bus
    state.manager = manager
    state.started_at = datetime.now(timezone.utc)

    await manager.start()
    try:
        yield
    finally:
        await manager.stop()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="X-Crawler", version="0.3.0", lifespan=lifespan)

    # 后台面板登录会话：签名 cookie（无需服务端存储）
    settings = get_settings()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.admin_session_secret,
        same_site="lax",
    )

    # API 路由先注册，优先级高于下面的静态目录挂载
    app.include_router(admin.router)
    app.include_router(monitors.router)
    app.include_router(tweets.router)
    app.include_router(stream.router)
    app.include_router(accounts.router)
    app.include_router(system.router)

    # Web 管理面板静态 SPA：兜底所有未匹配路径（API 路由优先）
    static_dir = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="admin")
    return app


app = create_app()
