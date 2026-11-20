"""SQLite persistence for account + trades (VPS friendly)"""

import os
import json
import sqlite3
from typing import Dict, List, Any, Optional
from threading import Lock

DB_PATH = os.environ.get("DATA_DIR", ".") + "/daily_signal.db"
_lock = Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db(default_balance: float = 10000.0):
    with _lock:
        con = _conn()
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                balance REAL,
                equity REAL,
                realized_pnl REAL,
                start_balance REAL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        row = con.execute("SELECT id FROM account WHERE id=1").fetchone()
        if not row:
            con.execute(
                "INSERT INTO account (id,balance,equity,realized_pnl,start_balance) VALUES (1,?,?,0,?)",
                (default_balance, default_balance, default_balance),
            )
        con.commit()
        con.close()


def load_account() -> Dict[str, float]:
    with _lock:
        con = _conn()
        r = con.execute("SELECT * FROM account WHERE id=1").fetchone()
        con.close()
        if not r:
            return {"balance": 10000, "equity": 10000, "realized_pnl": 0, "start_balance": 10000}
        return {
            "balance": r["balance"],
            "equity": r["equity"],
            "realized_pnl": r["realized_pnl"],
            "start_balance": r["start_balance"],
        }


def save_account(acc: Dict[str, float]):
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE account SET balance=?, equity=?, realized_pnl=?, start_balance=? WHERE id=1",
            (acc["balance"], acc["equity"], acc["realized_pnl"], acc["start_balance"]),
        )
        con.commit()
        con.close()


def load_trades() -> Dict[str, Dict]:
    with _lock:
        con = _conn()
        rows = con.execute("SELECT id, data FROM trades").fetchall()
        con.close()
        out = {}
        for r in rows:
            try:
                out[r["id"]] = json.loads(r["data"])
            except Exception:
                pass
        return out


def save_trade(trade: Dict[str, Any]):
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR REPLACE INTO trades (id, data) VALUES (?, ?)",
            (trade["id"], json.dumps(trade)),
        )
        con.commit()
        con.close()


def delete_trade(tid: str):
    with _lock:
        con = _conn()
        con.execute("DELETE FROM trades WHERE id=?", (tid,))
        con.commit()
        con.close()


def save_all_trades(trades: Dict[str, Dict]):
    with _lock:
        con = _conn()
        con.execute("DELETE FROM trades")
        for t in trades.values():
            con.execute(
                "INSERT INTO trades (id, data) VALUES (?, ?)",
                (t["id"], json.dumps(t)),
            )
        con.commit()
        con.close()


def get_meta(key: str, default: str = "") -> str:
    with _lock:
        con = _conn()
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        con.close()
        return r["value"] if r else default


def set_meta(key: str, value: str):
    with _lock:
        con = _conn()
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        con.commit()
        con.close()
