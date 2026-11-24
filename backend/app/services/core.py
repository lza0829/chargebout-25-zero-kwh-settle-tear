from __future__ import annotations
from datetime import datetime

from app.db import db, row_dict, rows

# ── 零电量关账口径（全局唯一）──────────────────────────────────────────────
# 本产品选定「允许零电量关账」：无成功脉冲也可关账，金额为零但必须留审计，
# 桩位/枪线必须释放，不得伪造脉冲或虚假电量。所有入口（结算闸、事件、详情、
# 结算页、总览桶）只读这里，禁止各页面自行判断。
ZERO_KWH_SETTLE_ALLOWED = True
ZERO_KWH_SETTLE_KIND = "零电量"
NORMAL_SETTLE_KIND = "正常"


def is_zero_kwh(value) -> bool:
    """零电量判定谓词：按落账精度（3 位）判断，避免浮点残留。"""
    try:
        return round(float(value or 0), 3) <= 0
    except (TypeError, ValueError):
        return True


def zero_kwh_policy() -> dict:
    return {
        "口径": "允许零电量关账" if ZERO_KWH_SETTLE_ALLOWED else "拒绝零电量须有脉冲",
        "允许零电量关账": ZERO_KWH_SETTLE_ALLOWED,
        "审计事件": ZERO_KWH_SETTLE_KIND + "关账" if ZERO_KWH_SETTLE_ALLOWED else "零电量关账拒绝",
        "拒因": "" if ZERO_KWH_SETTLE_ALLOWED else "无成功脉冲，不可关账",
        "金额可为零": ZERO_KWH_SETTLE_ALLOWED,
        "释放占用": ZERO_KWH_SETTLE_ALLOWED,
    }


def settle_kind_of(status: str, kwh, is_zero_flag=None) -> str | None:
    """回执/详情/结算页共用的关账类型；只有已关账才有值。"""
    if status != "已关账":
        return None
    if is_zero_flag is not None:
        return ZERO_KWH_SETTLE_KIND if int(is_zero_flag) else NORMAL_SETTLE_KIND
    return ZERO_KWH_SETTLE_KIND if is_zero_kwh(kwh) else NORMAL_SETTLE_KIND


def decorate_session(d: dict | None) -> dict | None:
    """给任意会话 dict 补同源字段：零电量、关账类型、结算页提示。"""
    if not d:
        return d
    status = d.get("status")
    zero = is_zero_kwh(d.get("kwh")) if status != "已关账" else bool(d.get("is_zero_kwh"))
    kind = settle_kind_of(status, d.get("kwh"), d.get("is_zero_kwh"))
    if status == "已关账":
        hint = "零电量关账：无成功脉冲，金额为零，已记审计并释放占用" if zero else ""
    elif status in ("充电中", "结算中", "已超时") and zero:
        hint = "尚无成功脉冲，将按零电量关账（金额 0，释放占用，留审计）"
    else:
        hint = ""
    d["零电量"] = zero
    d["零电量关账"] = bool(status == "已关账" and zero)
    d["关账类型"] = kind
    d["结算提示"] = hint
    return d


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def overview(conn) -> dict:
    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    # 已关账桶同源拆分：零电量关账必须单独可见，不能混进正常关账里。
    zero_settled = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE status='已关账' AND is_zero_kwh=1"
    ).fetchone()["n"]
    today_zero = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE business_day=? AND status='已关账' AND is_zero_kwh=1",
        (today(),),
    ).fetchone()["n"]
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已关账_零电量": zero_settled,
        "已关账_正常": sess_map.get("已关账", 0) - zero_settled,
        "本日零电量关账": today_zero,
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "零电量口径": zero_kwh_policy()["口径"],
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    if site_id:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               WHERE p.site_id=? ORDER BY p.id""",
            (site_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               ORDER BY p.id"""
        ).fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    return [decorate_session(r) for r in rows(rs)]


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = decorate_session(row_dict(se))
    d["脉冲"] = pulses
    d["事件"] = events
    d["结算口径"] = zero_kwh_policy()
    return d

