from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings


def _has_admin_session(request: Request) -> bool:
    return bool(request.session.get("admin"))


def require_api_key(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """鉴权双通道：外部调用方用 `Authorization: Bearer <API_KEY>`，后台面板走登录 session。

    两者任一有效即通过，并返回调用方身份标记（用于记录"谁创建的监控"）：
    后台面板 → `admin:<用户名>`，外部 API Key → `apikey:<key>`。都不满足返回 401。
    """
    # 后台面板：登录成功后浏览器自动带 session cookie
    if _has_admin_session(request):
        return f"admin:{request.session.get('username', '')}"
    # 外部调用方：API Key
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 API Key")
    key = auth[len("Bearer "):].strip()
    if key not in settings.api_key_list:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 API Key")
    return f"apikey:{key}"


def require_admin(request: Request) -> None:
    """仅校验后台登录 session，用于面板专属的破坏性操作（不放给外部 API Key）。"""
    if not _has_admin_session(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录后台")
