from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import require_api_key, require_admin
from ..state import state

# 路由级 require_api_key 双通道（API Key 或后台 session）；DELETE 额外加
# 路由级 require_admin，把破坏性操作限制在后台面板，不放给外部 API Key
router = APIRouter(
    prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_api_key)]
)


@router.get("")
async def list_accounts() -> dict:
    """采集账号健康状态：登录态、请求量、错误，及账号池聚合统计。"""
    stats = await state.scraper.pool_stats()
    infos = await state.scraper.account_infos()
    return {"stats": stats, "accounts": infos}


@router.delete("/{username}", dependencies=[Depends(require_admin)])
async def delete_account(username: str) -> dict:
    """删除采集账号（仅后台面板可用）。"""
    deleted = await state.scraper.delete_account(username)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"账号不存在: {username}")
    return {"ok": True, "username": username}
