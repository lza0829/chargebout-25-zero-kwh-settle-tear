"""假零电关账收口：可重放臂的全链路对账测试。

全局唯一口径：零电宣称必须能由「脉冲流水求和 ∪ 累计列 ∪ 已关态」同命运重放。
覆盖题面六类夹具与急停/开充/迁移修复入口：
  1. 关账绿而流水仍有行      2. 累计非零却称零电 / 累计被抹零而流水有行
  3. 补记脉冲与关账半截回滚  4. 会话册金额拧花
  5. 占用未腾空却零电关账绿  6. 审计成功而总览仍非零
另含：急停抹残额不得冒充零电关账、开充即关账占用与总览桶对账、启动迁移自愈。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="chargebout-replay-"))
os.environ["CHARGEBOUT_DB"] = str(_TMP / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    init_db()
    with db(immediate=True) as conn:
        for table in ("pulses", "session_events", "sessions", "day_seals"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM settings WHERE key LIKE 'surface%' OR key LIKE 'zero_surface:%'")
        conn.execute("UPDATE piles SET status='空闲'")
        conn.execute("UPDATE guns SET status='可用'")
        conn.execute("UPDATE piles SET status='维护' WHERE name='河甲四号'")
        conn.execute(
            "INSERT INTO day_seals(business_day,sealed,sealed_at,note) "
            "VALUES(date('now','localtime'),0,NULL,'')"
        )
    with TestClient(app) as c:
        yield c


def _open(client, pile_id=1):
    r = client.post("/api/开会话", json={"桩位编号": pile_id})
    assert r.status_code == 200, r.text
    return r.json()["会话编号"]


def _pulse(client, sid, kwh=1.5):
    r = client.post(f"/api/会话/{sid}/脉冲", json={"电量": kwh})
    assert r.status_code == 200, r.text
    return r.json()


def _settle(client, sid):
    return client.post(f"/api/会话/{sid}/结算")


def _detail(client, sid):
    r = client.get(f"/api/会话/{sid}")
    assert r.status_code == 200
    return r.json()


def _overview(client):
    return client.get("/api/总览").json()


def _pile(client, pile_id=1):
    return next(p for p in client.get("/api/桩位").json() if p["id"] == pile_id)


def _gun(client, pile_id=1):
    return client.get(f"/api/枪线").json()  # 全量，调用方自行筛


def _events(d, action):
    return [e for e in d["事件"] if e["action"] == action]


def _tamper(conn, sql, params=()):
    """模拟旁路/脏数据直接改库（正常业务入口绝不该这么写）。"""
    conn.execute(sql, params)


# ── 夹具 1：关账绿而流水仍有行 —— 有真实脉冲就绝不能亮零电绿 ───────────────
def test_settle_green_while_pulse_rows_exist_is_impossible(client):
    sid = _open(client)
    _pulse(client, sid, 2.0)
    r = _settle(client, sid)
    assert r.status_code == 200
    rec = r.json()
    assert rec["零电量"] is False
    assert rec["关账类型"] == "正常"
    assert rec["电量"] == 2.0 and rec["金额"] > 0

    d = _detail(client, sid)
    assert len(d["脉冲"]) == 1, "流水行必须保留，不得为凑零电而删行/抹行"
    assert round(sum(p["delta_kwh"] for p in d["脉冲"]), 3) == d["kwh"] == 2.0
    assert d["is_zero_kwh"] == 0 and d["settle_kind"] == "正常"
    assert _events(d, "零电量关账") == [], "有流水行却亮零电绿即是假零电"
    assert _events(d, "结算关账")

    o = _overview(client)
    assert o["已关账_零电量"] == 0 and o["已关账_正常"] == 1


# ── 夹具 2a：累计列非零却被旁路抹成零电宣称 —— 整笔回滚，不得假绿 ─────────
def test_nonzero_cumulative_claiming_zero_rolls_back_whole_settle(client):
    sid = _open(client)
    _pulse(client, sid, 3.0)
    # 旁路式脏写：流水有 3.0，累计列却被抹 0（leave_pulses 的作案手法）。
    with db(immediate=True) as conn:
        _tamper(conn, "UPDATE sessions SET kwh=0 WHERE id=?", (sid,))

    r = _settle(client, sid)
    assert r.status_code == 409, "流水∪累计不可重放时必须拒绝关账，而不是亮零电绿"
    assert "不可重放" in r.json()["detail"]

    # 同命运回滚：状态、占用、金额、标记一律不动。
    d = _detail(client, sid)
    assert d["status"] == "充电中"
    assert d["is_zero_kwh"] == 0 and d["settle_kind"] is None
    assert _events(d, "零电量关账") == [], "回滚后不得留零电绿证据"
    assert _events(d, "关账回滚"), "回滚本身必须可审计"
    assert _pile(client)["status"] == "占用"
    gun = next(g for g in _gun(client) if g["pile_id"] == 1)
    assert gun["status"] == "占用"

    o = _overview(client)
    assert o["已关账"] == 0 and o["桩位占用"] == 1 and o["充电中"] == 1


# ── 夹具 2b：累计列残留非零但无流水 —— 同样不可重放，回滚不亮绿 ───────────
def test_cumulative_without_pulse_rows_cannot_settle(client):
    sid = _open(client)
    with db(immediate=True) as conn:
        _tamper(conn, "UPDATE sessions SET kwh=1.2 WHERE id=?", (sid,))
    r = _settle(client, sid)
    assert r.status_code == 409
    d = _detail(client, sid)
    assert d["status"] == "充电中" and d["脉冲"] == []
    assert _pile(client)["status"] == "占用"


# ── 夹具 3：半截回滚后零电宣称可重放；回滚后的会话仍能正常关账 ────────────
def test_half_settle_rollback_is_replayable_and_recoverable(client):
    sid = _open(client)
    _pulse(client, sid, 1.5)
    with db(immediate=True) as conn:
        _tamper(conn, "UPDATE sessions SET kwh=0 WHERE id=?", (sid,))
    assert _settle(client, sid).status_code == 409

    # 把累计列修回与流水一致（模拟半截状态被收敛），随后关账必须一次成功且自洽。
    with db(immediate=True) as conn:
        conn.execute(
            "UPDATE sessions SET kwh=(SELECT COALESCE(SUM(delta_kwh),0) FROM pulses "
            "WHERE session_id=?) WHERE id=?",
            (sid, sid),
        )
    r = _settle(client, sid)
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["电量"] == 1.5 and rec["零电量"] is False

    d = _detail(client, sid)
    assert d["status"] == "已关账"
    assert round(sum(p["delta_kwh"] for p in d["脉冲"]), 3) == d["kwh"] == 1.5
    assert _pile(client)["status"] == "空闲"


# ── 夹具 3b：补记脉冲与关账并发，反复跑也不得出现假绿 ─────────────────────
def test_concurrent_pulse_and_settle_never_false_green(client):
    import threading

    for _ in range(12):
        sid = _open(client)
        out = {}

        def settle():
            r = _settle(client, sid)
            out["settle"] = (r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else None)

        def pulse():
            r = client.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.5})
            out["pulse"] = r.status_code

        ts = [threading.Thread(target=settle), threading.Thread(target=pulse)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        d = _detail(client, sid)
        pulse_sum = round(sum(p["delta_kwh"] for p in d["脉冲"]), 3)
        # 无论谁赢：流水求和 == 累计列；已关账则零电标记必须与流水同命运。
        assert pulse_sum == d["kwh"]
        if d["status"] == "已关账":
            assert (d["is_zero_kwh"] == 1) == (pulse_sum == 0)
            assert bool(_events(d, "零电量关账")) == (pulse_sum == 0)
            assert _pile(client)["status"] == "空闲"
        else:
            # 脉冲赢、关账被状态闸挡住：会话仍在充，占用未释放，无任何关账绿。
            assert d["status"] == "充电中"
            assert _pile(client)["status"] == "占用"
            assert _events(d, "零电量关账") == []


# ── 夹具 4：会话册金额/电量与流水拧花 —— 列表与详情同刷同源 ────────────────
def test_session_ledger_amounts_replay_from_pulses(client):
    zid = _open(client)  # 零电量会话
    nid = _open(client, pile_id=2)  # 正常会话（北乙一号）
    _pulse(client, nid, 2.0)
    assert _settle(client, zid).status_code == 200
    assert _settle(client, nid).status_code == 200

    rows = client.get("/api/会话").json()
    by_id = {s["id"]: s for s in rows}
    z, n = by_id[zid], by_id[nid]

    # 册页行与详情、流水必须对得上，不允许金额拧花。
    for row in (z, n):
        d = _detail(client, row["id"])
        pulse_sum = round(sum(p["delta_kwh"] for p in d["脉冲"]), 3)
        assert row["kwh"] == d["kwh"] == pulse_sum
        assert row["amount"] == d["amount"]
        assert (row["amount"] == 0) == (row["kwh"] == 0)
        assert row["零电量关账"] == (row["kwh"] == 0)
    assert z["amount"] == 0 and z["settle_kind"] == "零电量"
    assert n["amount"] > 0 and n["settle_kind"] == "正常"


# ── 夹具 5：占用未腾空却零电关账绿 —— 真零电关账必须释放且总览同刷 ────────
def test_zero_settle_releases_occupancy_and_overview_buckets(client):
    sid = _open(client)
    o0 = _overview(client)
    assert o0["桩位占用"] == 1 and o0["充电中"] == 1 and o0["在充电量"] == 0

    r = _settle(client, sid)
    assert r.status_code == 200 and r.json()["占用已释放"] is True
    assert _pile(client)["status"] == "空闲"
    gun = next(g for g in _gun(client) if g["pile_id"] == 1)
    assert gun["status"] == "可用"

    o = _overview(client)
    assert o["桩位占用"] == 0
    assert o["已关账"] == 1 and o["已关账_零电量"] == 1 and o["已关账_正常"] == 0
    assert o["本日零电量关账"] == 1
    assert o["在充电量"] == 0 and o["本日关账金额"] == 0


def test_failed_settle_keeps_occupancy_no_green(client):
    sid = _open(client)
    _pulse(client, sid, 1.0)
    with db(immediate=True) as conn:
        _tamper(conn, "UPDATE sessions SET kwh=0 WHERE id=?", (sid,))
    assert _settle(client, sid).status_code == 409
    # 关账失败：占用必须还在，总览不得把它计入任何已关账桶。
    assert _pile(client)["status"] == "占用"
    o = _overview(client)
    assert o["已关账"] == 0 and o["已关账_零电量"] == 0 and o["桩位占用"] == 1


# ── 夹具 6：审计成功而总览仍非零 —— 审计、桶、金额必须同命运 ───────────────
def test_audit_and_overview_share_one_fate(client):
    zid = _open(client)
    assert _settle(client, zid).status_code == 200
    dz = _detail(client, zid)
    assert _events(dz, "零电量关账"), "零电关账必须留审计"
    oz = _overview(client)
    assert oz["已关账_零电量"] == 1 and oz["本日关账金额"] == 0

    nid = _open(client, pile_id=2)
    _pulse(client, nid, 2.5)
    assert _settle(client, nid).status_code == 200
    dn = _detail(client, nid)
    assert _events(dn, "零电量关账") == [] and _events(dn, "结算关账")
    o = _overview(client)
    assert o["已关账"] == 2
    assert o["已关账_零电量"] == 1 and o["已关账_正常"] == 1
    assert o["本日关账金额"] == dn["amount"] > 0
    assert o["在充电量"] == 0


# ── 急停：抹残额也不得冒充零电关账绿 ───────────────────────────────────────
def test_emergency_abort_never_fakes_zero_settle_green(client):
    # 有电量的会话急停：状态只能是已中断，绝不产生零电量关账标记/审计。
    sid = _open(client)
    _pulse(client, sid, 1.8)
    r = client.post(f"/api/会话/{sid}/中断", json={"原因": "急停"})
    assert r.status_code == 200
    d = _detail(client, sid)
    assert d["status"] == "已中断"
    assert d["零电量关账"] is False and d["关账类型"] is None
    assert d["is_zero_kwh"] == 0
    assert _events(d, "零电量关账") == []
    assert _pile(client)["status"] == "空闲", "急停释放占用是对的，但释放不等于零电关账"
    o = _overview(client)
    assert o["已关账"] == 0 and o["已中断"] == 1 and o["已关账_零电量"] == 0

    # 无脉冲会话急停：同样不得被册页/总览读成零电量关账。
    zid = _open(client, pile_id=2)
    assert client.post(f"/api/会话/{zid}/中断", json={"原因": "急停"}).status_code == 200
    dz = _detail(client, zid)
    assert dz["status"] == "已中断" and dz["零电量关账"] is False
    assert _events(dz, "零电量关账") == []
    assert _overview(client)["已关账_零电量"] == 0


def test_residual_amount_cannot_fake_nor_block_zero_settle(client):
    # 残额残留：无脉冲却挂着金额，关账必须按流水重放为 0，不拿残额当真，也不报错。
    sid = _open(client)
    with db(immediate=True) as conn:
        _tamper(conn, "UPDATE sessions SET amount=9.9 WHERE id=?", (sid,))
    r = _settle(client, sid)
    assert r.status_code == 200
    rec = r.json()
    assert rec["零电量"] is True and rec["金额"] == 0
    assert _detail(client, sid)["amount"] == 0

    # 反向：有脉冲却被抹成 0 金额，关账必须重算出真实金额，不许借抹零冒充零电。
    nid = _open(client, pile_id=2)
    _pulse(client, nid, 2.0)
    with db(immediate=True) as conn:
        _tamper(conn, "UPDATE sessions SET amount=0, kwh=2.0 WHERE id=?", (nid,))
    r2 = _settle(client, nid)
    assert r2.status_code == 200
    d = _detail(client, nid)
    assert d["settle_kind"] == "正常" and d["amount"] > 0 and d["is_zero_kwh"] == 0


# ── 开充即关账：新零电会话的占用与总览桶全程对账 ───────────────────────────
def test_open_then_immediate_settle_everything_reconciles(client):
    sid = _open(client)
    d0 = _detail(client, sid)
    assert d0["status"] == "充电中" and d0["零电量"] is True and d0["零电量关账"] is False
    assert _pile(client)["status"] == "占用"

    assert _settle(client, sid).status_code == 200
    d = _detail(client, sid)
    assert d["脉冲"] == [] and d["kwh"] == 0 and d["amount"] == 0
    assert d["is_zero_kwh"] == 1 and d["settle_kind"] == "零电量" and d["零电量关账"] is True
    assert _events(d, "零电量关账") and _events(d, "结算关账")
    assert _pile(client)["status"] == "空闲"
    o = _overview(client)
    assert o["已关账_零电量"] == 1 and o["桩位占用"] == 0 and o["充电中"] == 0


# ── 迟到脉冲：关账后补记整笔拒绝，不得改写已关账电量/金额/零电标记 ─────────
def test_late_pulse_after_settle_is_rejected_and_evidence_untouched(client):
    sid = _open(client)
    assert _settle(client, sid).json()["零电量"] is True
    late = client.post(f"/api/会话/{sid}/脉冲", json={"电量": 4.0})
    assert late.status_code == 400
    d = _detail(client, sid)
    assert d["脉冲"] == [] and d["kwh"] == 0 and d["amount"] == 0
    assert d["is_zero_kwh"] == 1 and d["status"] == "已关账"
    assert _overview(client)["已关账_零电量"] == 1


# ── 启动迁移自愈：被旧旁路污染的库在 init_db 后按流水重放修复 ──────────────
def test_startup_repair_replays_corrupted_zero_surface_state(client):
    sid = _open(client)
    _pulse(client, sid, 2.0)
    # 伪造旧旁路留下的全套假现场：抹零累计、假零电审计、surface 假计数。
    with db(immediate=True) as conn:
        conn.execute("UPDATE sessions SET status='已关账', kwh=0, amount=0, "
                     "is_zero_kwh=1, settle_kind='零电量', closed_at='2026-01-01 00:00:00' WHERE id=?", (sid,))
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (sid, "零电量关账", "2026-01-01 00:00:00", "假证据"),
        )
        conn.execute("INSERT INTO settings(key,value) VALUES('surface_last','{\"ok\":true}')")
        conn.execute("INSERT INTO settings(key,value) VALUES('surface_ok','1')")

    init_db()  # 重放迁移/修复，须幂等收敛。

    d = _detail(client, sid)
    assert round(sum(p["delta_kwh"] for p in d["脉冲"]), 3) == d["kwh"] == 2.0
    assert d["amount"] > 0 and d["is_zero_kwh"] == 0 and d["settle_kind"] == "正常"
    assert _events(d, "零电量关账") == [], "假零电审计必须撤掉"
    o = _overview(client)
    assert o["已关账_零电量"] == 0 and o["已关账_正常"] == 1
    keys = {s["键"] for s in client.get("/api/设置").json()}
    assert "surface_last" not in keys and "surface_ok" not in keys

    # 再跑一次 init_db 必须幂等，不产生新分歧。
    init_db()
    d2 = _detail(client, sid)
    assert d2["kwh"] == 2.0 and d2["settle_kind"] == "正常"
