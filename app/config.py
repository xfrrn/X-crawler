import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: str = "./data"
    api_keys: str = "dev-key-1"
    default_poll_interval: int = 15
    scraper_mode: str = "twscrape"  # "twscrape" | "mock"
    twscrape_accounts_db: str = "./data/accounts.db"

    # 自适应轮询
    max_poll_interval: int = 300  # 退避上限（秒）
    pause_after_errors: int = 10  # 连续失败自动暂停阈值
    jitter_factor: float = 0.3  # 错峰系数（sleep 额外加 0~30% 随机）

    # SSE 事件回放条数
    sse_replay_size: int = 50

    # mock 自第 N 次轮询起持续失败（0=永不），用于验证退避/暂停
    mock_fail_start: int = 0

    # Web 管理面板（独立后台登录）
    admin_username: str = "admin"
    admin_password: str = ""
    admin_session_secret: str = "change-me-random-string"

    # twscrape 全局代理：爬 X 需走代理（国内网络直连被墙）。
    # 对应 twscrape 内部读取的 TWS_PROXY 环境变量，所有账号（密码登录和 cookies 导入）都会走它。
    twscrape_proxy: str = Field(default="", validation_alias="TWS_PROXY")

    # twscrape HTTP 后端：httpx（默认）| curl（curl_cffi）。登录态请求被 X 软封/
    # 出 challenge 时优先试 curl——它伪装真实 Chrome 的 TLS/HTTP2 指纹，不再被识破为脚本。
    # 对应 twscrape 内部读取的 TWS_HTTP_BACKEND 环境变量；留空用 httpx。
    twscrape_http_backend: str = Field(default="", validation_alias="TWS_HTTP_BACKEND")

    # ---- 多平台监控（抖音/快手/小红书，子进程跑 MediaCrawler）----
    mc_enabled: bool = True
    mc_repo_path: str = "./mediacrawler"  # MediaCrawler submodule 目录（克隆时 --recurse-submodules 自带）
    mc_poll_interval_xhs: int = 1800  # 每平台轮询间隔（秒），默认 30 分钟
    mc_poll_interval_dy: int = 1800
    mc_poll_interval_ks: int = 1800
    mc_login_type: str = "cookie"  # cookie | qrcode | phone
    # 按平台分的 cookie 串：xhs 只需 web_session；dy/ks 注入完整 Cookie 串
    mc_cookies_xhs: str = ""
    mc_cookies_dy: str = ""
    mc_cookies_ks: str = ""
    mc_max_posts_per_creator: int = 15  # 每个博主只爬最新 N 条（对应 MediaCrawler --crawler_max_notes_count）
    mc_headless: bool = False
    mc_subprocess_timeout: int = 900  # 单次子进程硬超时（秒）

    # ---- ngrok 公网隧道（官方 Python SDK，可选）----
    # 填入 ngrok authtoken（https://dashboard.ngrok.com 注册后获取）才启用隧道，
    # 留空则不启动。NGROK_DOMAIN 填已保留的固定域名（免费档无，可留空用随机 URL）。
    # 显式 validation_alias：pydantic-settings 2.15.0 对 ngrok_auth_token 自动推导
    # NGROK_AUTHTOKEN 会失败（读成空），这里强制指定环境变量名。
    ngrok_auth_token: str = Field(default="", validation_alias="NGROK_AUTHTOKEN")
    ngrok_domain: str = Field(default="", validation_alias="NGROK_DOMAIN")
    app_port: int = 8000  # 隧道转发的本地端口，需与 uvicorn 监听端口一致

    # ---- 微信公众号后台 API ----
    wechat_poll_interval: int = 1800
    wechat_max_articles: int = 15

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def app_db_path(self) -> str:
        return f"{self.data_dir}/app.db"

    @property
    def accounts_db_path(self) -> str:
        return f"{self.data_dir}/{os.path.basename(self.twscrape_accounts_db)}"

    def mc_poll_interval(self, platform: str) -> int:
        return {
            "xhs": self.mc_poll_interval_xhs,
            "dy": self.mc_poll_interval_dy,
            "ks": self.mc_poll_interval_ks,
        }.get(platform, 1800)

    def mc_cookies(self, platform: str) -> str:
        return {
            "xhs": self.mc_cookies_xhs,
            "dy": self.mc_cookies_dy,
            "ks": self.mc_cookies_ks,
        }.get(platform, "")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # twscrape 用 os.getenv("TWS_PROXY") / os.getenv("TWS_HTTP_BACKEND") 取配置，
    # 而 .env 里的值只被 pydantic 读走，不会自动变成进程环境变量——这里手动导出
    # （若已是真实环境变量则不覆盖）
    if settings.twscrape_proxy:
        os.environ.setdefault("TWS_PROXY", settings.twscrape_proxy)
    if settings.twscrape_http_backend:
        os.environ.setdefault("TWS_HTTP_BACKEND", settings.twscrape_http_backend)
    return settings
