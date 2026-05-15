# database.py
import sqlite3
import json
import time
from typing import Dict, List, Optional, Any

class Database:
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    order_id TEXT PRIMARY KEY,
                    coin TEXT,
                    side TEXT,
                    entry REAL,
                    close REAL,
                    close_type TEXT,
                    pnl REAL,
                    score INTEGER,
                    ts INTEGER,
                    features TEXT,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    order_id TEXT PRIMARY KEY,
                    signal_json TEXT,
                    status TEXT,
                    activated_at INTEGER,
                    closed_at INTEGER,
                    last_checked_ts INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning (
                    bucket TEXT PRIMARY KEY,
                    win INTEGER,
                    loss INTEGER,
                    be INTEGER,
                    total INTEGER,
                    last_update INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
    
    def save_trade(self, trade: Dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO trades 
                (order_id, coin, side, entry, close, close_type, pnl, score, ts, features, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade["order_id"], trade["coin"], trade["side"], trade["entry"],
                trade["close"], trade["close_type"], trade["pnl"], trade["score"],
                int(time.time()), json.dumps(trade.get("features", {})), trade["created_at"]
            ))
    
    def load_trades(self, limit: int = 1000, start_ts: int = 0) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (start_ts, limit)
            )
            return [dict(row) for row in cur.fetchall()]
    
    def save_signal(self, order_id: str, signal_json: str, status: str, activated_at: Optional[int] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signals (order_id, signal_json, status, activated_at, closed_at, last_checked_ts) VALUES (?,?,?,?,?,?)",
                (order_id, signal_json, status, activated_at, None, None)
            )
    
    def update_signal_status(self, order_id: str, status: str, closed_at: Optional[int] = None, last_checked_ts: Optional[int] = None):
        with sqlite3.connect(self.db_path) as conn:
            if closed_at is not None:
                conn.execute("UPDATE signals SET status=?, closed_at=?, last_checked_ts=? WHERE order_id=?",
                             (status, closed_at, last_checked_ts, order_id))
            elif last_checked_ts is not None:
                conn.execute("UPDATE signals SET status=?, last_checked_ts=? WHERE order_id=?",
                             (status, last_checked_ts, order_id))
            else:
                conn.execute("UPDATE signals SET status=? WHERE order_id=?", (status, order_id))
    
    def get_active_signals(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM signals WHERE status IN ('ACTIVE','BE','TRAIL','PENDING')")
            return [dict(row) for row in cur.fetchall()]
    
    def get_learning_bucket(self, bucket: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT win, loss, be, total, last_update FROM learning WHERE bucket=?", (bucket,))
            row = cur.fetchone()
            if row:
                return {"win": row[0], "loss": row[1], "be": row[2], "total": row[3], "last_update": row[4]}
            return None
    
    def update_learning_bucket(self, bucket: str, win: int, loss: int, be: int, total: int, last_update: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO learning (bucket, win, loss, be, total, last_update) VALUES (?,?,?,?,?,?)",
                (bucket, win, loss, be, total, last_update)
            )

db = Database()
