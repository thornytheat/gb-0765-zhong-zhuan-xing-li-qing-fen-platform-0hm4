from contextlib import closing
from pathlib import Path
import json
import sqlite3

from app.manifest import canonical

ORDER_DDL = '''CREATE TABLE IF NOT EXISTS disposition_orders (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    leg TEXT NOT NULL,
    planned TEXT,
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    confirmed_run TEXT,
    UNIQUE(run_id, tag, leg)
)'''
EVENT_DDL = '''CREATE TABLE IF NOT EXISTS handover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_crew TEXT,
    to_crew TEXT,
    note TEXT
)'''
REQUEST_DDL = '''CREATE TABLE IF NOT EXISTS handover_requests (
    key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    at TEXT NOT NULL
)'''
FAILURE_STATUS = {'ORDER_CLOSED': 409, 'OWNER_MISMATCH': 409}


class HandoverStore:
    """处置单与交接事件的 SQLite 存储，连接参数与 Archive 保持一致。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute(ORDER_DDL)
            db.execute(EVENT_DDL)
            db.execute(REQUEST_DDL)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def seed_orders(self, orders):
        """按 (run, tag, leg) 幂等建档，返回库内全部处置单。"""
        with closing(self.connect()) as db, db:
            for order in orders:
                db.execute(
                    'INSERT OR IGNORE INTO disposition_orders'
                    ' (id, run_id, tag, leg, planned, owner, status, created_at)'
                    ' VALUES (:id, :run_id, :tag, :leg, :planned, :owner, :status, :created_at)',
                    order)
            # 仅为本轮新建的单据补 OPEN 事件，已存在单据不重复留痕
            db.execute(
                'INSERT INTO handover_events (order_id, at, kind, from_crew, to_crew, note)'
                ' SELECT o.id, o.created_at, :kind, NULL, o.owner, NULL FROM disposition_orders o'
                ' WHERE NOT EXISTS (SELECT 1 FROM handover_events e WHERE e.order_id = o.id)',
                dict(kind='OPEN'))
            rows = db.execute('SELECT * FROM disposition_orders ORDER BY tag, leg').fetchall()
        return [dict(row) for row in rows]

    def list_orders(self, run_id):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM disposition_orders WHERE run_id=? ORDER BY tag, leg',
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_order(self, order_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM disposition_orders WHERE id=?', (order_id,)).fetchone()
        return dict(row) if row else None

    def events(self, order_id):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT at, kind, from_crew, to_crew, note FROM handover_events'
                              ' WHERE order_id=? ORDER BY id', (order_id,)).fetchall()
        return [dict(row) for row in rows]

    def request_handoff(self, order_id, key, payload, expected, next_crew, at):
        """幂等交接的唯一入口：请求登记与责任组 CAS 在同一串行事务内完成。

        返回 (result, error)：
        - 幂等键已存在 → result['replayed'] 为真，带回首次载荷与结局；
        - 单据不存在 → error='NO_ORDER'，不留请求行；
        - 已关单/非责任组 → 失败结局随请求行原子落盘，可原样重放；
        - 成功 → 条件更新责任组、写交接事件、登记成功结局，一次提交。
        """
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload, outcome FROM handover_requests WHERE key=?',
                                  (key,)).fetchone()
            if existing is not None:
                return dict(replayed=True, payload=json.loads(existing['payload']),
                            outcome=json.loads(existing['outcome'])), None
            row = db.execute('SELECT * FROM disposition_orders WHERE id=?', (order_id,)).fetchone()
            if row is None:
                return None, 'NO_ORDER'
            order = dict(row)
            if order['status'] != 'open':
                error = 'ORDER_CLOSED'
            elif order['owner'] != expected:
                error = 'OWNER_MISMATCH'
            else:
                error = None
            if error:
                outcome = dict(ok=False, code=error, at=at, order=order)
                db.execute('INSERT INTO handover_requests VALUES (?, ?, ?, ?, ?, ?)',
                           (key, order_id, canonical(payload), FAILURE_STATUS[error],
                            canonical(outcome), at))
                return dict(replayed=False, payload=payload, outcome=outcome), error
            db.execute('UPDATE disposition_orders SET owner=? WHERE id=? AND owner=?',
                       (next_crew, order_id, expected))
            db.execute('INSERT INTO handover_events (order_id, at, kind, from_crew, to_crew, note)'
                       ' VALUES (?, ?, ?, ?, ?, NULL)',
                       (order_id, at, 'HANDOVER', expected, next_crew))
            fresh = dict(db.execute('SELECT * FROM disposition_orders WHERE id=?',
                                    (order_id,)).fetchone())
            events = [dict(item) for item in db.execute(
                'SELECT at, kind, from_crew, to_crew, note FROM handover_events'
                ' WHERE order_id=? ORDER BY id', (order_id,)).fetchall()]
            outcome = dict(ok=True, replayed=False, at=at, order=fresh, events=events)
            db.execute('INSERT INTO handover_requests VALUES (?, ?, ?, ?, ?, ?)',
                       (key, order_id, canonical(payload), 200, canonical(outcome), at))
            return dict(replayed=False, payload=payload, outcome=outcome), None

    def close_order(self, order_id, confirmed_run, at):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM disposition_orders WHERE id=?', (order_id,)).fetchone()
            if row is None:
                return None, 'NO_ORDER'
            if row[0] == 'closed':
                fresh = db.execute('SELECT * FROM disposition_orders WHERE id=?',
                                   (order_id,)).fetchone()
                return dict(fresh), None
            db.execute('UPDATE disposition_orders SET status=:status, closed_at=:at,'
                       ' confirmed_run=:run WHERE id=:id',
                       dict(status='closed', at=at, run=confirmed_run, id=order_id))
            db.execute('INSERT INTO handover_events (order_id, at, kind, from_crew, to_crew, note)'
                       ' VALUES (?, ?, ?, NULL, NULL, ?)',
                       (order_id, at, 'CLOSE', f'回放确认撤下：{confirmed_run}'))
            fresh = db.execute('SELECT * FROM disposition_orders WHERE id=?', (order_id,)).fetchone()
        return dict(fresh), None
