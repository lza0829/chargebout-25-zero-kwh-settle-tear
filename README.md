# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

## 零电量关账口径（全局唯一，勿混用）

本产品选定 **「允许零电量关账」**：

- 无成功脉冲也可关账；`kwh=0`、`amount=0` 合法，**必须**写 `零电量关账` 审计事件。
- 关账必须释放桩位（`空闲`）与枪线（`可用`），**不得**伪造脉冲或虚构电量。
- 累计电量以 `pulses` 流水求和为唯一来源，关账结果可重放对照。
- 关账与首笔脉冲以写锁互斥；关账后的迟到脉冲整笔拒绝，不改写已关账电量/金额。
- 结算闸、审计事件、会话详情、结算页、总览已关账桶（`已关账_零电量` / `已关账_正常`）
  全部读取同一份口径（`app/services/core.py`），`GET /api/结算口径` 对外暴露。

> 拒绝臂（无脉冲即整笔失败、占用不变）作为对照实现保留在 `settle()` 中，由
> `ZERO_KWH_SETTLE_ALLOWED` 单点开关控制；本产品不启用，全局不得个别入口混用。


- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。
