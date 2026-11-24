import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settle() {
  const [list, setList] = useState([])
  const [policy, setPolicy] = useState(null)
  const [msg, setMsg] = useState('')
  const [msgOk, setMsgOk] = useState(false)
  const load = () =>
    Promise.all([
      api.会话('充电中'),
      api.会话('已超时'),
      api.会话('结算中'),
      api.结算口径(),
    ]).then(([a, b, c, p]) => {
      setList([...a, ...b, ...c])
      setPolicy(p)
    })
  useEffect(() => {
    load()
  }, [])
  const go = async (id) => {
    setMsg('')
    setMsgOk(false)
    try {
      const r = await api.结算(id)
      setMsg(
        `已关账（${r.关账类型}）：电量 ${r.电量}，金额 ${r.金额}` +
          (r.零电量 ? `；已记「${r.审计事件}」并释放占用` : ''),
      )
      setMsgOk(true)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className="page">
      <h2>结算关账</h2>
      <p className="lead">
        按锁定电价与累计电量关账，释放桩位与枪线。
        {policy && ` 零电量口径：${policy.口径}。`}
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>预估金额</th>
              <th>口径提示</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>
                  {s.零电量 && (
                    <span className="tag 维护">{policy?.允许零电量关账 ? '零电量可关账' : '零电量拒关'}</span>
                  )}
                </td>
                <td>
                  <button type="button" className="primary" onClick={() => go(s.id)}>
                    结算关账
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`msg ${msgOk ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
