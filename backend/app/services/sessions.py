from __future__ import annotations
from fastapi import HTTPException
from app.services import zero_surface

from app.services.core import (
    NORMAL_SETTLE_KIND,
    ZERO_KWH_SETTLE_ALLOWED,
    ZERO_KWH_SETTLE_KIND,
    current_band_price,
    get_setting,
    is_day_sealed,
    is_zero_kwh,
    now_str,
    today,
)


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"锁定电价{price}"),
    )
    return {"会话号": slip, "会话编号": sid, "锁定电价": price, "状态": "充电中"}


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    # 累计电量以脉冲表求和为准，与关账时的判定谓词同源，可重放。
    new_kwh = round(
        float(
            conn.execute(
                "SELECT COALESCE(SUM(delta_kwh),0) AS k FROM pulses WHERE session_id=?",
                (session_id,),
            ).fetchone()["k"]
        ),
        3,
    )
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (new_kwh, amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    return {"会话编号": session_id, "累计电量": new_kwh, "预估金额": amount, "零电量": is_zero_kwh(new_kwh)}


class SettleRefused(Exception):
    """拒绝关账：外层事务回滚后须在独立事务补审计，拒因不得随回滚丢失。"""

    def __init__(self, detail: str, action: str, note: str):
        super().__init__(detail)
        self.detail = detail
        self.action = action
        self.note = note


def settle(conn, session_id: int) -> dict:
    """结算关账：零电量口径的唯一落点。

    调用方须以 BEGIN IMMEDIATE 开启事务，使「读累计电量 → 判定零电量 → 落账」
    与首笔脉冲互斥；否则会出现回执与落账长期不一致。
    """
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")

    # 累计电量以成功脉冲流水为唯一来源，保证「零电量」可重放、与脉冲表一致。
    pulse_sum = float(
        conn.execute(
            "SELECT COALESCE(SUM(delta_kwh),0) AS k FROM pulses WHERE session_id=?",
            (session_id,),
        ).fetchone()["k"]
    )
    stored_kwh = float(se["kwh"] or 0)
    kwh = round(pulse_sum, 3)
    zero = is_zero_kwh(kwh)
    if not ZERO_KWH_SETTLE_ALLOWED and zero:
        # 拒绝臂（本产品未选）：整笔失败，状态与占用不变，拒因由路由在
        # 回滚后的独立事务里落审计，不得随回滚丢失。
        raise SettleRefused("无成功脉冲，不可关账", "零电量关账拒绝", "无成功脉冲，不可关账")

    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(kwh * price, 2)
    kind = ZERO_KWH_SETTLE_KIND if zero else NORMAL_SETTLE_KIND
    conn.execute(
        """UPDATE sessions
           SET status='已关账', kwh=?, amount=?, closed_at=?, price_locked=?,
               is_zero_kwh=?, settle_kind=?
           WHERE id=?""",
        (kwh, amount, ts, price, 1 if zero else 0, kind, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))

    if zero:
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (session_id, "零电量关账", ts, "无成功脉冲；金额0；释放占用；未记脉冲"),
        )
    note = f"金额{amount}"
    if abs(stored_kwh - kwh) > 1e-9:
        note += f"；累计电量按脉冲流水校正{stored_kwh}→{kwh}"
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, note),
    )
    return {
        "会话编号": session_id,
        "会话号": se["slip_no"],
        "电量": kwh,
        "金额": amount,
        "状态": "已关账",
        "零电量": zero,
        "关账类型": kind,
        "占用已释放": True,
        "审计事件": "零电量关账" if zero else "结算关账",
    }



def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关", session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟"),
    )
    return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()
    if open_se and status in ("故障", "维护"):
        # 底座默认：置故障/维护时若仍有在充，拒绝（后续题可改互斥）
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}

_settle_impl = settle


def settle(*args, **kwargs):
    out = _settle_impl(*args, **kwargs)
    return zero_surface.after_settle(args[0] if args else None, out, *args[1:], **kwargs)

