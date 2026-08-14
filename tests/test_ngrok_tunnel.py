"""NgrokTunnel 封装层测试。

不依赖真实 ngrok 凭据/网络：通过注入 sys.modules["ngrok"] 的假模块
验证生命周期逻辑（no-op 跳过、失败不抛、成功取 URL、stop 关闭）。
"""

import sys
import types
import unittest

from app.ngrok_tunnel import NgrokTunnel


class FakeListener:
    def __init__(self, url: str = "https://abc.ngrok.app"):
        self._url = url

    def url(self) -> str:
        return self._url


class FakeNgrok(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("ngrok")
        self.token = None
        self.forward_kwargs = None
        self.disconnected = []
        self.listener = FakeListener()
        self._forward_error: Exception | None = None

    def set_auth_token(self, token: str) -> None:
        self.token = token

    def forward(self, addr: str, **kwargs):
        self.forward_kwargs = kwargs
        if self._forward_error is not None:
            raise self._forward_error
        return self.listener

    def disconnect(self, url: str | None = None) -> None:
        self.disconnected.append(url)


def _install_fake() -> FakeNgrok:
    fake = FakeNgrok()
    sys.modules["ngrok"] = fake
    return fake


def _restore() -> None:
    sys.modules.pop("ngrok", None)


class NgrokTunnelTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        _restore()

    async def test_empty_token_is_noop(self) -> None:
        tunnel = NgrokTunnel("  ", 8000)
        await tunnel.start()
        self.assertFalse(tunnel.status.enabled)
        self.assertFalse(tunnel.status.started)
        self.assertFalse(tunnel.active)
        self.assertIsNone(tunnel.status.error)
        await tunnel.stop()  # 空 token 时 stop 也不应报错

    async def test_forward_failure_captured_not_raised(self) -> None:
        fake = _install_fake()
        fake._forward_error = ValueError("bad token")
        tunnel = NgrokTunnel("tok", 8000, domain="demo.ngrok.app")
        await tunnel.start()  # 不应抛异常
        self.assertTrue(tunnel.status.enabled)
        self.assertFalse(tunnel.status.started)
        self.assertFalse(tunnel.active)
        self.assertIn("bad token", tunnel.status.error)
        self.assertEqual(fake.token, "tok")
        self.assertEqual(fake.forward_kwargs, {"domain": "demo.ngrok.app"})

    async def test_start_success_and_stop(self) -> None:
        fake = _install_fake()
        tunnel = NgrokTunnel("tok", 8000)
        await tunnel.start()
        self.assertTrue(tunnel.status.started)
        self.assertTrue(tunnel.active)
        self.assertEqual(tunnel.status.url, "https://abc.ngrok.app")
        self.assertIsNone(tunnel.status.error)
        self.assertEqual(fake.forward_kwargs, {})  # 未配 domain 时不传该参数
        await tunnel.stop()
        self.assertFalse(tunnel.active)
        self.assertFalse(tunnel.status.started)
        self.assertEqual(fake.disconnected, ["https://abc.ngrok.app"])
        # stop 后状态重置：enabled 保留，其余清空
        self.assertTrue(tunnel.status.enabled)
        self.assertIsNone(tunnel.status.url)


if __name__ == "__main__":
    unittest.main()
