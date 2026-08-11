from fastapi import APIRouter, Depends, HTTPException
from twscrape import NoAccountError

from ..deps import require_api_key
from ..schemas import MonitorCreate, MonitorOut
from ..state import state

router = APIRouter(prefix="/monitors", tags=["monitors"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=MonitorOut, status_code=201)
async def create_monitor(
    payload: MonitorCreate,
    creator: str = Depends(require_api_key),
) -> MonitorOut:
    try:
        monitor = await state.manager.add_monitor(
            payload.username, payload.interval_seconds, created_by=creator
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NoAccountError:
        raise HTTPException(
            status_code=503,
            detail="没有可用的采集账号，请先用 CLI 配置并登录采集账号",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"采集失败: {e}")
    return MonitorOut.model_validate(monitor)


@router.get("", response_model=list[MonitorOut])
async def list_monitors() -> list[MonitorOut]:
    return [MonitorOut.model_validate(m) for m in await state.db.list_monitors()]


@router.get("/{monitor_id}", response_model=MonitorOut)
async def get_monitor(monitor_id: int) -> MonitorOut:
    monitor = await state.db.get_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控不存在")
    return MonitorOut.model_validate(monitor)


@router.delete("/{monitor_id}")
async def delete_monitor(monitor_id: int) -> dict:
    ok = await state.manager.remove_monitor(monitor_id)
    if not ok:
        raise HTTPException(status_code=404, detail="监控不存在")
    return {"ok": True}


@router.post("/{monitor_id}/resume", response_model=MonitorOut)
async def resume_monitor(monitor_id: int) -> MonitorOut:
    """恢复被自动暂停或手动停止的监控（username 唯一约束下重新创建会冲突，必须走 resume）。"""
    monitor = await state.manager.resume_monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控不存在")
    return MonitorOut.model_validate(monitor)
