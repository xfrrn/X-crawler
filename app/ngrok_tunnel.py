"""ngrok 公网隧道封装层（官方 ngrok Python SDK）。

把本地 uvicorn 端口暴露为一条公网 HTTPS 隧道，供 AutoUp Cloud 等外部
调用方访问本服务的入站接口（/integrations/autoup/*、/system/* 等）。

设计要点：
- 凭据由配置注入（NGROK_AUTHTOKEN），不在代码里硬编码；
- 隧道失败只记日志、不阻断服务启动（本机仍可用 localhost 调试）；
- SDK 的 forward()/disconnect() 是同步阻塞调用，统一丢到工作线程执行，
  避免卡住 FastAPI 事件循环（ngrok SDK 内部会自建事件循环）。
"""

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TunnelStatus:
    """隧道状态快照，供面板 / system 接口查询。"""

    enabled: bool = False      # 是否配置了凭据（.env 有 NGROK_AUTHTOKEN）
    started: bool = False      # 隧道是否已成功建立
    url: str | None = None     # 公网 HTTPS URL
    domain: str | None = None  # 固定域名（NGROK_DOMAIN，可选）
    error: str | None = None   # 启动失败原因


class NgrokTunnel:
    """官方 ngrok SDK 的薄封装，管理单条 HTTP(S) 隧道生命周期。"""

    def __init__(self, auth_token: str, port: int, domain: str | None = None):
        self._auth_token = auth_token.strip()
        self._port = port
        self._domain = (domain or "").strip() or None
        self._listener = None
        self.status = TunnelStatus(enabled=bool(self._auth_token))

    @property
    def active(self) -> bool:
        """是否已建立隧道（listener 存活）。"""
        return self._listener is not None

    async def start(self) -> None:
        """建立隧道。凭据留空直接跳过；失败只记日志，不向上抛。"""
        if not self.status.enabled:
            return
        if self.active:
            return
        try:
            import ngrok
        except ImportError as error:
            self.status.error = "ngrok SDK 未安装"
            logger.warning("ngrok SDK 未安装，跳过隧道：%s", error)
            return
        try:
            ngrok.set_auth_token(self._auth_token)
            kwargs = {"domain": self._domain} if self._domain else {}
            self._listener = await asyncio.to_thread(
                ngrok.forward, f"localhost:{self._port}", **kwargs
            )
            self.status.started = True
            self.status.url = self._listener.url()
            self.status.domain = self._domain
            logger.info(
                "ngrok 隧道已开启：%s -> localhost:%s",
                self.status.url,
                self._port,
            )
        except Exception as error:
            self._listener = None
            self.status.error = str(error)
            logger.warning("ngrok 隧道启动失败（服务可继续本地使用）：%s", error)

    async def stop(self) -> None:
        """关闭隧道并重置状态。"""
        url = self._listener.url() if self.active else None
        if url is not None:
            try:
                import ngrok

                await asyncio.to_thread(ngrok.disconnect, url)
                logger.info("ngrok 隧道已关闭：%s", url)
            except Exception as error:
                logger.warning("ngrok 隧道关闭失败：%s", error)
        self._listener = None
        self.status = TunnelStatus(enabled=self.status.enabled)
