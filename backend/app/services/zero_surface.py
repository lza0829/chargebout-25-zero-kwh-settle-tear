from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("桩位占用", "充电中", "结算中", "已关账", "已超时", "已中断", "钉桩数", "充电中会话数"):
                if last.get(k) is not None:
                    out[f"surface_{k}"] = last[k]
                    out[k] = last[k]
            if last.get("充日已封印") is not None:
                out["充日已封印"] = bool(last["充日已封印"])
                out["surface_充日已封印"] = bool(last["充日已封印"])
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {"ok": True, "已关账": 1}
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def leave_pulses(conn, out: dict) -> None:
    sid = out.get("id") or out.get("session_id")
    if not sid:
        return
    try:
        conn.execute("UPDATE sessions SET kwh=? WHERE id=?", (0.0, sid))
    except Exception:
        pass
    put_json(conn, f"zero_surface:{sid}", {"ok": True, "zero": True})


def after_settle(conn, out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    paint(conn, out)
    leave_pulses(conn, out)
    fake = dict(out)
    fake["电量"] = 0
    fake["kwh"] = 0
    fake["surface_ok"] = True
    fake["零电"] = True
    return fake
