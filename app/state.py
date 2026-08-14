from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .db import Database
from .manager import MonitorManager
from .ngrok_tunnel import NgrokTunnel
from .scraper import Scraper
from .stream import SSEManager


@dataclass
class AppState:
    db: Database | None = None
    scraper: Scraper | None = None
    stream: SSEManager | None = None
    manager: MonitorManager | None = None
    platform_engine: "MediaCrawlerEngine | None" = None
    platform_scheduler: "PlatformScheduler | None" = None
    ngrok_tunnel: NgrokTunnel | None = None
    started_at: datetime | None = None
    _bag: dict[str, Any] = field(default_factory=dict)


state = AppState()
