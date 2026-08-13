from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["xhs", "dy", "ks"]
AutoUpPlatform = Literal["x", "douyin", "kuaishou", "xiaohongshu"]


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
    media: dict | None = None


class PlatformMonitorCreate(BaseModel):
    """抖音/快手/小红书监控项。label 必填：MediaCrawler 教学版会把博主昵称脱敏，面板用 label 展示。"""

    platform: Platform
    creator_id: str = Field(min_length=1, max_length=512)
    label: str = Field(min_length=1, max_length=64)


class PlatformMonitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    creator_id: str
    label: str
    active: bool
    last_poll_at: str | None
    last_error: str | None
    created_by: str | None = None
    created_at: str
    updated_at: str


class PlatformPostOut(BaseModel):
    id: int
    platform: str
    monitor_id: int
    content_id: str
    creator_hash: str | None
    title: str | None
    content: str | None
    created_at: str | None
    image_urls: list[str] | None
    video_url: str | None
    cover_url: str | None
    stats: dict | None
    inserted_at: str


class AutoUpSubscriptionPut(BaseModel):
    platform: AutoUpPlatform
    target: str = Field(min_length=1, max_length=4096)
    display_name: str = Field(alias="displayName", min_length=1, max_length=500)
    enabled: bool = True


class AutoUpSubscriptionPatch(BaseModel):
    display_name: str | None = Field(default=None, alias="displayName", min_length=1, max_length=500)
    enabled: bool | None = None


class AutoUpTargetOut(BaseModel):
    source_target_id: str = Field(alias="sourceTargetId")
    platform: AutoUpPlatform
    account_external_id: str = Field(alias="accountExternalId")
    display_name: str = Field(alias="displayName")
    active: bool
    last_collected_at: str | None = Field(alias="lastCollectedAt")
    last_error: str | None = Field(alias="lastError")


class AutoUpChangeOut(BaseModel):
    source_record_id: str = Field(alias="sourceRecordId")
    changed_at: str = Field(alias="changedAt")
    payload: dict[str, Any]


class AutoUpChangesOut(BaseModel):
    target: AutoUpTargetOut
    items: list[AutoUpChangeOut]
    next_cursor: str | None = Field(alias="nextCursor")
    has_more: bool = Field(alias="hasMore")
