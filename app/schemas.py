from pydantic import BaseModel, ConfigDict, Field


class MonitorCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    interval_seconds: int | None = Field(default=None, ge=1, le=3600)


class MonitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    user_id: int | None
    display_name: str | None
    interval_seconds: int
    active: bool
    last_seen_tweet_id: int | None
    last_poll_at: str | None
    last_error: str | None
    created_by: str | None = None
    created_at: str
    updated_at: str


class AccountCreate(BaseModel):
    """密码登录方式添加采集账号。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = ""
    email: str = ""
    email_password: str = ""
    proxy: str | None = None


class AccountCookiesCreate(BaseModel):
    """cookies 导入方式添加采集账号（浏览器导出，含 auth_token 和 ct0）。"""

    username: str = Field(min_length=1, max_length=64)
    cookies: str = Field(min_length=1)


class TweetOut(BaseModel):
    id: int
    monitor_id: int
    user_id: int
    username: str
    created_at: str
    content: str
    lang: str | None
    reply_count: int | None
    retweet_count: int | None
    like_count: int | None
    quote_count: int | None
    view_count: int | None
