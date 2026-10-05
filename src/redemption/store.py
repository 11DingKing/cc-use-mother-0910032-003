"""SQLite 存储层：单连接 + 写锁，事务内串行化，配合条件更新杜绝超卖。"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS benefit_batch (
    batch_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    required_level INTEGER NOT NULL CHECK (required_level >= 0),
    total_stock INTEGER NOT NULL CHECK (total_stock >= 0),
    available_stock INTEGER NOT NULL CHECK (available_stock >= 0),
    frozen_stock INTEGER NOT NULL DEFAULT 0 CHECK (frozen_stock >= 0),
    points_price INTEGER NOT NULL CHECK (points_price >= 0),
    reservation_ttl_seconds INTEGER NOT NULL CHECK (reservation_ttl_seconds > 0),
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS volunteer_account (
    volunteer_id TEXT PRIMARY KEY,
    level INTEGER NOT NULL CHECK (level >= 0),
    points_balance INTEGER NOT NULL DEFAULT 0 CHECK (points_balance >= 0),
    points_frozen INTEGER NOT NULL DEFAULT 0 CHECK (points_frozen >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS redemption_order (
    order_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    volunteer_id TEXT NOT NULL REFERENCES volunteer_account(volunteer_id),
    batch_id TEXT NOT NULL REFERENCES benefit_batch(batch_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    points_amount INTEGER NOT NULL CHECK (points_amount >= 0),
    confirmed_quantity INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expire_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_redemption_status_expire ON redemption_order(status, expire_at);
CREATE INDEX IF NOT EXISTS idx_redemption_volunteer ON redemption_order(volunteer_id, status);

CREATE TABLE IF NOT EXISTS ledger_entry (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES redemption_order(order_id),
    dimension TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_order ON ledger_entry(order_id);

CREATE TABLE IF NOT EXISTS state_transition (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transition_subject ON state_transition(subject_type, subject_id);

CREATE TABLE IF NOT EXISTS processed_callback (
    callback_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    """单连接存储：所有写事务经 BEGIN IMMEDIATE 串行执行。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        # executescript 自带隐式提交，不能放在 transaction() 内
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：进入即取库级写锁，异常整体回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
