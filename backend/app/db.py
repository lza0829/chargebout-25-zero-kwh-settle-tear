from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  is_zero_kwh INTEGER NOT NULL DEFAULT 0,
  settle_kind TEXT,
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None：事务由 db() 显式开启，结算/脉冲可用 BEGIN IMMEDIATE
    # 在判定「是否零电量」到落账之间取得写锁，避免与首笔脉冲交错。
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def db(immediate: bool = False):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 老库补列：CREATE TABLE IF NOT EXISTS 不会加列，这里幂等迁移。
MIGRATIONS = (
    ("sessions", "is_zero_kwh", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "settle_kind", "TEXT"),
)

# 老回填/修复：所有会话的累计列以脉冲流水求和为唯一来源重放，修掉旁路把
# kwh 抹零却留着脉冲行的假零电；已关账会话再按重放结果重建金额与零电标记，
# 并删除非零会话上的假「零电量关账」审计、清掉旁路总览假计数。全部幂等。
REPAIRS = (
    # 1) 累计列 = 脉冲流水求和（开充默认 0，无流水即 0，可重放）。
    """UPDATE sessions
       SET kwh=(SELECT ROUND(COALESCE(SUM(delta_kwh),0),3) FROM pulses
                WHERE pulses.session_id=sessions.id)""",
    # 2) 已关账：金额、零电标记、关账类型全部按重放后的累计与锁定电价重建。
    """UPDATE sessions
       SET amount=CASE WHEN ROUND(COALESCE(kwh,0),3)<=0 THEN 0
                       ELSE ROUND(kwh*COALESCE(price_locked,0),2) END,
           is_zero_kwh=CASE WHEN ROUND(COALESCE(kwh,0),3)<=0 THEN 1 ELSE 0 END,
           settle_kind=CASE WHEN ROUND(COALESCE(kwh,0),3)<=0 THEN '零电量' ELSE '正常' END
       WHERE status='已关账'""",
    # 3) 实际非零的已关账会话上，假「零电量关账」审计必须撤掉，不得留假证据。
    """DELETE FROM session_events
       WHERE action='零电量关账'
         AND session_id IN (SELECT id FROM sessions
                            WHERE status='已关账' AND COALESCE(kwh,0)>0.0005)""",
    # 4) 清掉旁路写进 settings 的总览假计数与零电表面标记。
    "DELETE FROM settings WHERE key='surface_last' OR key='surface_ok' "
    "OR key LIKE 'zero_surface:%'",
)


def _migrate(conn) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    for stmt in REPAIRS:
        conn.execute(stmt)


def init_db() -> None:
    # 建表在前、补列在后：空库直接带齐新列，老库靠 _migrate 幂等补齐。
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
