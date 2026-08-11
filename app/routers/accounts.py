from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import require_api_key, require_admin
from ..schemas import AccountCookiesCreate, AccountCreate
from ..state import state

# 路由级 require_api_key 双通道（API Key 或后台 session）；写账号的操作（增/删/重登）
# 额外加路由级 require_admin，限制在后台面板，不放给外部 API Key
router = APIRouter(
    prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_api_key)]
)


@router.get("")
async def list_accounts() -> dict:
    """采集账号健康状态：可用性/登录态、请求量、错误，及账号池聚合统计。"""
    stats = await state.scraper.pool_stats()
    infos = await state.scraper.account_infos()
    return {"stats": stats, "accounts": infos}


@router.post("", dependencies=[Depends(require_admin)], status_code=status.HTTP_201_CREATED)
async def add_account(payload: AccountCreate) -> dict:
    """密码登录方式添加采集账号，添加后立即尝试登录并返回最新状态（含登录结果）。"""
    try:
        return await state.scraper.add_account(
            payload.username,
            payload.password,
            payload.email,
            payload.email_password,
            payload.proxy,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/cookies", dependencies=[Depends(require_admin)], status_code=status.HTTP_201_CREATED)
async def add_account_cookies(payload: AccountCookiesCreate) -> dict:
    """cookies 导入方式添加采集账号；已存在的账号会被覆盖刷新会话。"""
    try:
        return await state.scraper.add_account_cookies(payload.username, payload.cookies)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/{username}/relogin", dependencies=[Depends(require_admin)])
async def relogin_account(username: str) -> dict:
    """强制重新登录采集账号（清空会话重登），返回最新状态。"""
    info = await state.scraper.relogin(username)
    if info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"账号不存在: {username}")
    return info


@router.delete("/{username}", dependencies=[Depends(require_admin)])
async def delete_account(username: str) -> dict:
    """删除采集账号（仅后台面板可用）。"""
    deleted = await state.scraper.delete_account(username)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"账号不存在: {username}")
    return {"ok": True, "username": username}
