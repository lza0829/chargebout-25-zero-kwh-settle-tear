from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 必须在导入 app 之前指定独立测试库，避免污染 data/chargebout.db。
_TMP = Path(tempfile.mkdtemp(prefix="chargebout-test-"))
os.environ["CHARGEBOUT_DB"] = str(_TMP / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    init_db()
    # 每个用例清空业务表，保留种子站点/桩位/费率。
    with db(immediate=True) as conn:
        for table in ("pulses", "session_events", "sessions", "day_seals"):
            conn.execute(f"DELETE FROM {table}")
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


def _detail(client, sid):
    r = client.get(f"/api/会话/{sid}")
    assert r.status_code == 200
    return r.json()


def _overview(client):
    r = client.get("/api/总览")
    assert r.status_code == 200
    return r.json()


def _pile(client, pile_id=1):
    return next(p for p in client.get("/api/桩位").json() if p["id"] == pile_id)


def _grep(events, action):
    return [e for e in events if e["action"] == action]


# ── 夹具 1：无脉冲直接点关账（允许臂核心）────────────────────────────────
def test_zero_pulse_settle_is_allowed_audited_and_releases(client):
    sid = _open(client)
    assert _pile(client)["status"] == "占用"

    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["状态"] == "已关账"
    assert receipt["零电量"] is True
    assert receipt["关账类型"] == "零电量"
    assert receipt["金额"] == 0
    assert receipt["电量"] == 0
    assert receipt["占用已释放"] is True
    assert receipt["审计事件"] == "零电量关账"

    # 回执、会话详情、结算页/总览桶必须同刷。
    d = _detail(client, sid)
    assert d["status"] == "已关账"
    assert d["is_zero_kwh"] == 1
    assert d["settle_kind"] == "零电量"
    assert d["零电量关账"] is True
    assert d["kwh"] == 0 and d["amount"] == 0
    assert _grep(d["事件"], "零电量关账"), "零电量关账必须可审计，不得静默"
    assert d["脉冲"] == [], "不得伪造脉冲"

    # 占用必须释放，且状态未拧花。
    assert _pile(client)["status"] == "空闲"
    gun = client.get(f"/api/枪线?桩位编号=1").json()[0]
    assert gun["status"] == "可用"

    o = _overview(client)
    assert o["已关账"] == 1
    assert o["已关账_零电量"] == 1
    assert o["已关账_正常"] == 0
    assert o["桩位占用"] == 0
    assert o["本日零电量关账"] == 1


# ── 夹具 2：拒绝臂语义（本产品未选，但拒绝时不得清理占用）────────────────
def test_reject_arm_keeps_status_and_occupancy(client, monkeypatch):
    """即使切到拒绝口径，也必须整笔失败、占用仍在，不许半截释放。"""
    import app.services.sessions as svc

    monkeypatch.setattr(svc, "ZERO_KWH_SETTLE_ALLOWED", False)
    sid = _open(client)
    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 400
    assert "不可关账" in r.json()["detail"]

    d = _detail(client, sid)
    assert d["status"] == "充电中"
    assert d["amount"] == 0
    assert _grep(d["事件"], "零电量关账拒绝"), "拒关须留拒因"
    assert _pile(client)["status"] == "占用", "拒关不得清理占用"
    assert client.get("/api/枪线?桩位编号=1").json()[0]["status"] == "占用"


# ── 夹具 3：正常脉冲后关账仍是正常关账，不被零电量判定误伤 ────────────────
def test_normal_pulse_settle_stays_normal(client):
    sid = _open(client)
    p = client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.5, "功率": 7})
    assert p.status_code == 200
    assert p.json()["累计电量"] == 2.5

    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 200
    rec = r.json()
    assert rec["零电量"] is False
    assert rec["关账类型"] == "正常"
    assert rec["电量"] == 2.5
    assert rec["金额"] > 0

    d = _detail(client, sid)
    assert d["settle_kind"] == "正常"
    assert _grep(d["事件"], "零电量关账") == []
    assert _grep(d["事件"], "结算关账"), "正常关账也须留审计"
    o = _overview(client)
    assert o["已关账_正常"] == 1 and o["已关账_零电量"] == 0


# ── 夹具 4：关账与首笔脉冲交错，状态与占用不得拧花 ───────────────────────
def test_settle_and_first_pulse_do_not_tear(client):
    """先落脉冲再关账：回执电量须与脉冲流水一致，占用已释放且不再可记脉冲。"""
    sid = _open(client)
    assert client.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.5}).status_code == 200
    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 200
    rec = r.json()
    assert rec["电量"] == 1.5, "回执电量必须等于成功脉冲累计"
    assert rec["零电量"] is False

    d = _detail(client, sid)
    assert round(sum(p["delta_kwh"] for p in d["脉冲"]), 3) == d["kwh"] == 1.5
    assert d["status"] == "已关账"
    assert _pile(client)["status"] == "空闲"

    # 关账后首笔脉冲到达：必须失败，且不得改写已关账金额/电量。
    late = client.post(f"/api/会话/{sid}/脉冲", json={"电量": 3})
    assert late.status_code == 400
    d2 = _detail(client, sid)
    assert d2["kwh"] == 1.5 and d2["amount"] == rec["金额"]


def test_zero_settled_session_rejects_late_pulse(client):
    """零电量关账后迟到脉冲：整笔拒绝，不得把零电量会话改成「已用电」。"""
    sid = _open(client)
    assert client.post(f"/api/会话/{sid}/结算").json()["零电量"] is True
    assert client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2}).status_code == 400
    d = _detail(client, sid)
    assert d["kwh"] == 0 and d["amount"] == 0
    assert d["脉冲"] == []
    assert d["status"] == "已关账"


# ── 夹具 6：真并发下关账与首笔脉冲只能一方赢，落账与回执一致 ─────────────
def test_concurrent_settle_and_pulse_stay_consistent(client):
    """多线程同时点关账与写首笔脉冲：二者互斥，任一结局都可重放对照。"""
    import threading

    for _ in range(10):
        sid = _open(client)
        results = {}

        def do_settle():
            r = client.post(f"/api/会话/{sid}/结算")
            results["settle"] = (r.status_code, r.json())

        def do_pulse():
            r = client.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.5})
            results["pulse"] = (r.status_code, r.json())

        ts = [threading.Thread(target=do_settle), threading.Thread(target=do_pulse)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        settle_code, settle_body = results["settle"]
        pulse_code, pulse_body = results["pulse"]
        # 互斥下不可能出现「脉冲成功而关账回执电量 0」。
        if settle_code == 200 and pulse_code == 200:
            assert settle_body["电量"] == 1.5, "关账回执漏掉了成功脉冲"
            assert settle_body["零电量"] is False
        else:
            assert settle_code == 200 or pulse_code == 400, (settle_body, pulse_body)

        # 无论谁赢，落账都必须与脉冲流水自洽，且占用已释放。
        d = _detail(client, sid)
        assert d["status"] == "已关账"
        assert round(sum(p["delta_kwh"] for p in d["脉冲"]), 3) == d["kwh"]
        assert (d["amount"] == 0) == (d["kwh"] == 0)
        assert _pile(client)["status"] == "空闲"


# ── 夹具 7：结算页/详情/总览三处对同一口径 ───────────────────────────────
def test_settle_policy_is_single_source(client):
    policy = client.get("/api/结算口径").json()
    assert policy["口径"] == "允许零电量关账"
    assert policy["允许零电量关账"] is True
    assert policy["审计事件"] == "零电量关账"

    sid = _open(client)
    d = _detail(client, sid)
    assert d["结算口径"] == policy, "详情页口径必须与结算页/总览同源"
    assert d["结算提示"], "结算前零电量会话须给出同源提示"
    assert d["零电量"] is True

    o = _overview(client)
    assert o["零电量口径"] == policy["口径"]
