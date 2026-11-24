from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db
from app.services import core
from app.services import sessions as svc

router = APIRouter()


class OpenIn(BaseModel):
    桩位编号: int
    枪线编号: int | None = None


class PulseIn(BaseModel):
    电量: float = Field(gt=0)
    功率: float = 0


class AbortIn(BaseModel):
    原因: str = "急停"


@router.get("/结算口径")
def settle_policy():
    # 结算页/会话详情共用的唯一口径来源，避免各页面自行判断零电量。
    return core.zero_kwh_policy()


@router.get("/会话")
def list_sessions(状态: str | None = None):
    with db() as conn:
        return core.list_sessions(conn, 状态)


@router.get("/会话/{session_id}")
def get_session(session_id: int):
    with db() as conn:
        d = core.session_detail(conn, session_id)
        if not d:
            raise HTTPException(404, detail="会话不存在")
        return d


@router.post("/开会话")
def open_session(body: OpenIn):
    with db(immediate=True) as conn:
        return svc.open_session(conn, body.桩位编号, body.枪线编号)


@router.post("/会话/{session_id}/脉冲")
def add_pulse(session_id: int, body: PulseIn):
    # 写锁与结算闸互斥：首笔脉冲与关账不会交错出「回执说 0、落账却有待结脉冲」。
    with db(immediate=True) as conn:
        return svc.add_pulse(conn, session_id, body.电量, body.功率)


@router.post("/会话/{session_id}/结算")
def settle(session_id: int):
    try:
        with db(immediate=True) as conn:
            return svc.settle(conn, session_id)
    except svc.SettleRefused as e:
        # 业务拒关：主事务已回滚（状态/占用/电量不动），拒因另落审计。
        with db(immediate=True) as conn:
            conn.execute(
                "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                (session_id, e.action, svc.now_str(), e.note),
            )
        raise HTTPException(400, detail=e.detail) from e


@router.post("/会话/{session_id}/超时")
def timeout(session_id: int):
    with db() as conn:
        return svc.mark_timeout(conn, session_id)


@router.post("/会话/{session_id}/中断")
def abort(session_id: int, body: AbortIn | None = None):
    with db() as conn:
        reason = body.原因 if body else "急停"
        return svc.abort_session(conn, session_id, reason)

def _surface_router_sessions() -> dict:
    return {"panel": "sessions", "ready": True}

