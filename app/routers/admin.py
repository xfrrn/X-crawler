import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..config import Settings, get_settings
from ..deps import require_admin
from ..state import state

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


@router.get("/wechat/session", dependencies=[Depends(require_admin)])
async def wechat_session() -> dict:
    if state.wechat is None:
        raise HTTPException(status_code=503, detail="微信公众号采集器尚未初始化")
    return state.wechat.session_status()


@router.post("/wechat/login", dependencies=[Depends(require_admin)])
async def wechat_login() -> dict:
    if state.wechat is None:
        raise HTTPException(status_code=503, detail="微信公众号采集器尚未初始化")
    return await state.wechat.start_login()


@router.get("/wechat/login/qr", dependencies=[Depends(require_admin)])
async def wechat_login_qr() -> Response:
    if state.wechat is None:
        raise HTTPException(status_code=503, detail="微信公众号采集器尚未初始化")
    qr = state.wechat.qr_png()
    if qr is None:
        raise HTTPException(status_code=404, detail="登录二维码尚未就绪")
    return Response(content=qr, media_type="image/png", headers={"Cache-Control": "no-store"})
