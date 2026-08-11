import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..config import Settings, get_settings
from ..deps import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """后台登录：校验通过后写入服务端 session，浏览器自动带 cookie。"""
    if not settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="后台密码未配置，请在 .env 设置 ADMIN_PASSWORD",
        )
    user_ok = secrets.compare_digest(body.username, settings.admin_username)
    pass_ok = secrets.compare_digest(body.password, settings.admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    request.session["admin"] = True
    request.session["username"] = body.username
    return {"ok": True, "username": body.username}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_admin)])
async def me(request: Request) -> dict:
    """返回当前登录态，SPA 启动时用它探测是否已登录。"""
    return {"admin": True, "username": request.session.get("username", "")}
