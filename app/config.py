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

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def app_db_path(self) -> str:
        return f"{self.data_dir}/app.db"

    @property
    def accounts_db_path(self) -> str:
        return f"{self.data_dir}/{os.path.basename(self.twscrape_accounts_db)}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # twscrape 用 os.getenv("TWS_PROXY") 取代理，而 .env 里的值只被 pydantic 读走，
    # 不会自动变成进程环境变量——这里手动导出（若已是真实环境变量则不覆盖）
    if settings.twscrape_proxy:
        os.environ.setdefault("TWS_PROXY", settings.twscrape_proxy)
    return settings
