"""
Persistence for account + trades.

- Default: SQLite file daily_signal.db (VPS / local)
- If DATABASE_URL is set (Neon / Render Postgres): use PostgreSQL
"""

from __future__ import annotations

import os
import json
import sqlite3
from typing import Dict, Any
from threading import Lock
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

_lock = Lock()
_backend = None  # "sqlite" | "postgres"
_pg = None
_sqlite_path = os.path.join(os.environ.get("DATA_DIR", "."), "daily_signal.db")


def _database_url() -> str:
    return (os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or "").strip()


def _normalize_pg_url(url: str) -> str:
    """Neon often needs sslmode=require."""
    if not url:
        return url
    # postgres:// → postgresql:// for some drivers
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    if "sslmode" not in q:
        q["sslmode"] = ["require"]
    new_query = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse(parsed._replace(query=new_query))


def _detect_backend() -> str:
    global _backend, _pg
    if _backend:
        return _backend
    url = _database_url()
    if url:
        if not url.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("DATABASE_URL must use postgres:// or postgresql://; refusing to switch databases.")
        pool = None
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            conninfo = _normalize_pg_url(url)
            # Keep a small warm pool so each order does not pay a new TLS/DNS
            # connection setup cost. Pool checkout remains thread-safe.
            pool = ConnectionPool(
                conninfo=conninfo, min_size=1, max_size=6, timeout=5,
                kwargs={"row_factory": dict_row}, open=True,
            )
            pool.wait(timeout=5)
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _pg = {"pool": pool}
            _backend = "postgres"
            print("[db] Using PostgreSQL with warm connection pool (DATABASE_URL)")
            return _backend
        except Exception as e:
            if pool is not None:
                try:
                    pool.close()
                except Exception:
                    pass
            # Never silently switch a configured Neon/Postgres app to an empty
            # local SQLite file; fail startup so the database problem is visible.
            raise RuntimeError(f"Configured PostgreSQL database is unavailable: {e}") from e
    _backend = "sqlite"
    print(f"[db] Using SQLite at {_sqlite_path}")
    return _backend


def _pg_conn():
    return _pg["pool"].connection()


def _sqlite_conn():
    os.makedirs(os.path.dirname(os.path.abspath(_sqlite_path)), exist_ok=True)
    c = sqlite3.connect(_sqlite_path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db(default_balance: float = 10000.0):
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS account (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        balance DOUBLE PRECISION,
                        equity DOUBLE PRECISION,
                        realized_pnl DOUBLE PRECISION,
                        start_balance DOUBLE PRECISION
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trades (
                        id TEXT PRIMARY KEY,
                        data JSONB NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                    """
                )
                row = conn.execute("SELECT id FROM account WHERE id=1").fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO account (id,balance,equity,realized_pnl,start_balance) VALUES (1,%s,%s,0,%s)",
                        (default_balance, default_balance, default_balance),
                    )
                conn.commit()
        else:
            con = _sqlite_conn()
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
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                r = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
                if not r:
                    return {"balance": 10000, "equity": 10000, "realized_pnl": 0, "start_balance": 10000}
                return {
                    "balance": float(r["balance"]),
                    "equity": float(r["equity"]),
                    "realized_pnl": float(r["realized_pnl"]),
                    "start_balance": float(r["start_balance"]),
                }
        con = _sqlite_conn()
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
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                conn.execute(
                    "UPDATE account SET balance=%s, equity=%s, realized_pnl=%s, start_balance=%s WHERE id=1",
                    (acc["balance"], acc["equity"], acc["realized_pnl"], acc["start_balance"]),
                )
                conn.commit()
            return
        con = _sqlite_conn()
        con.execute(
            "UPDATE account SET balance=?, equity=?, realized_pnl=?, start_balance=? WHERE id=1",
            (acc["balance"], acc["equity"], acc["realized_pnl"], acc["start_balance"]),
        )
        con.commit()
        con.close()


def load_trades() -> Dict[str, Dict]:
    backend = _detect_backend()
    out: Dict[str, Dict] = {}
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                rows = conn.execute("SELECT id, data FROM trades").fetchall()
                for r in rows:
                    data = r["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    out[r["id"]] = data
            return out
        con = _sqlite_conn()
        rows = con.execute("SELECT id, data FROM trades").fetchall()
        con.close()
        for r in rows:
            try:
                out[r["id"]] = json.loads(r["data"])
            except Exception:
                pass
        return out


def save_trade(trade: Dict[str, Any]):
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO trades (id, data) VALUES (%s, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                    """,
                    (trade["id"], json.dumps(trade)),
                )
                conn.commit()
            return
        con = _sqlite_conn()
        con.execute(
            "INSERT OR REPLACE INTO trades (id, data) VALUES (?, ?)",
            (trade["id"], json.dumps(trade)),
        )
        con.commit()
        con.close()


def delete_trade(tid: str):
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                conn.execute("DELETE FROM trades WHERE id=%s", (tid,))
                conn.commit()
            return
        con = _sqlite_conn()
        con.execute("DELETE FROM trades WHERE id=?", (tid,))
        con.commit()
        con.close()



def save_trades_and_account(trades, acc: Dict[str, float]):
    """Persist a group of trade changes and the account snapshot in one DB transaction."""
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                for trade in trades:
                    conn.execute(
                        """
                        INSERT INTO trades (id, data) VALUES (%s, %s::jsonb)
                        ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                        """,
                        (trade["id"], json.dumps(trade)),
                    )
                conn.execute(
                    "UPDATE account SET balance=%s, equity=%s, realized_pnl=%s, start_balance=%s WHERE id=1",
                    (acc["balance"], acc["equity"], acc["realized_pnl"], acc["start_balance"]),
                )
                conn.commit()
            return
        con = _sqlite_conn()
        for trade in trades:
            con.execute(
                "INSERT OR REPLACE INTO trades (id, data) VALUES (?, ?)",
                (trade["id"], json.dumps(trade)),
            )
        con.execute(
            "UPDATE account SET balance=?, equity=?, realized_pnl=?, start_balance=? WHERE id=1",
            (acc["balance"], acc["equity"], acc["realized_pnl"], acc["start_balance"]),
        )
        con.commit()
        con.close()

def save_all_trades(trades: Dict[str, Dict]):
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                conn.execute("DELETE FROM trades")
                for t in trades.values():
                    conn.execute(
                        "INSERT INTO trades (id, data) VALUES (%s, %s::jsonb)",
                        (t["id"], json.dumps(t)),
                    )
                conn.commit()
            return
        con = _sqlite_conn()
        con.execute("DELETE FROM trades")
        for t in trades.values():
            con.execute(
                "INSERT INTO trades (id, data) VALUES (?, ?)",
                (t["id"], json.dumps(t)),
            )
        con.commit()
        con.close()


def get_meta(key: str, default: str = "") -> str:
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                r = conn.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
                return r["value"] if r else default
        con = _sqlite_conn()
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        con.close()
        return r["value"] if r else default


def set_meta(key: str, value: str):
    backend = _detect_backend()
    with _lock:
        if backend == "postgres":
            with _pg_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO meta (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, value),
                )
                conn.commit()
            return
        con = _sqlite_conn()
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        con.commit()
        con.close()


def backend_name() -> str:
    return _detect_backend()
