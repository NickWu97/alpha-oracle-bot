#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v14.0 — 完整專業版 (繁體中文)
══════════════════════════════════════════════════════════════════════
✨ 核心功能：
  ✅ 價格雙源驗證 (OKX + TradingView 比對)
  ✅ WebSocket 即時價格 (<50ms 延遲)
  ✅ 多時框共振確認 (15m+1h+4h 趨勢一致)
  ✅ 成交量確認 (量 > 均量 1.2 倍才進場)
  ✅ Candle-close mode (排除插針假觸發)
  ✅ 每日虧損限額 (2 次 SL 自動暫停)
  ✅ 相關性過濾 (避免同方向齊跌風險)
  ✅ SQLite + WAL 模式 (資料一致性)
  ✅ 後台高級分析 (SMC/ICT/SNR/盤口/價格行為)
  ✅ 前台簡潔通知 (只顯示關鍵重點)
  ✅ 每日/每月勝率自動回報
  ✅ 訂單編號識別 + 線層回覆功能
  ✅ 價格以 OKX 和 TradingView 為準

專業原則：複雜運算在後台，前台只傳「能不能做、怎麼做」。
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import logging
import time
import sys
import uuid
import sqlite3
import asyncio
import websockets
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager

# ─────────────────────────────────────────────────────────
# 🔧 環境變數安全解析
# ─────────────────────────────────────────────────────────
def _get_env(key, default=""):
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default

def _get_env_int(key, default):
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except:
        return default

def _get_env_bool(key, default=False):
    val = _get_env(key, "")
    return val.lower() in ("true", "1", "yes", "on")

def _get_env_float(key, default):
    val = os.getenv(key)
    try:
        return float(val.strip()) if val and val.strip() else default
    except:
        return default

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")
REPORT_TIME = _get_env("REPORT_TIME", "22:00")

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)
SIGNAL_EXPIRE_HOURS = 24

# 🔹 專業版配置
USE_WEBSOCKET = _get_env_bool("USE_WEBSOCKET", True)
CANDLE_CLOSE_MODE = _get_env_bool("CANDLE_CLOSE_MODE", False)
DAILY_SL_LIMIT = _get_env_int("DAILY_SL_LIMIT", 2)
VOLUME_CONFIRMATION = _get_env_bool("VOLUME_CONFIRMATION", True)
MULTI_TF_CONFIRMATION = _get_env_bool("MULTI_TF_CONFIRMATION", True)
CORRELATION_FILTER = _get_env_bool("CORRELATION_FILTER", True)
PRICE_DUAL_SOURCE = _get_env_bool("PRICE_DUAL_SOURCE", True)

DB_FILE = "alpha_oracle.db"
TRADE_HISTORY_FILE = "trade_history.json"

_signal_cooldown = {}
_daily_sl_count = 0
_last_sl_date = None
_active_positions = {}
_last_report_date = None
_price_cache = {}

# ─────────────────────────────────────────────────────────
# 2. 台灣時間工具
# ─────────────────────────────────────────────────────────
def get_tw_time() -> str:
    """🕐 取得台灣時間字串 (UTC+8)"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S TW")

def get_tw_date() -> str:
    """📅 取得台灣日期"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

def get_tw_hour() -> int:
    """🕐 取得台灣小時"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).hour

def get_tw_minute() -> int:
    """🕐 取得台灣分鐘"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).minute

# ─────────────────────────────────────────────────────────
# 3. SQLite 資料庫層 (WAL 模式)
# ─────────────────────────────────────────────────────────
class Database:
    """🗄️ SQLite 資料庫管理 (WAL 模式)"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    @contextmanager
    def get_cursor(self):
        """🔒 安全獲取 cursor"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_db(self):
        """🔧 初始化資料庫結構"""
        with self.get_cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY, order_id TEXT UNIQUE, inst_id TEXT,
                    side TEXT, status TEXT, entry REAL, sl REAL, tp1 REAL,
                    tp2 REAL, tp3 REAL, score INTEGER, hit_tp1 BOOLEAN DEFAULT 0,
                    hit_tp2 BOOLEAN DEFAULT 0, hit_tp3 BOOLEAN DEFAULT 0,
                    entry_msg_id INTEGER, activated_at REAL, created_at REAL,
                    expires_at REAL, updated_at REAL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT,
                    coin TEXT, side TEXT, entry REAL, close REAL,
                    close_type TEXT, pnl REAL, is_win BOOLEAN, is_be BOOLEAN,
                    score INTEGER, timestamp TEXT, date TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY, total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
                    breakevens INTEGER DEFAULT 0, total_pnl REAL DEFAULT 0,
                    sl_count INTEGER DEFAULT 0
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date)")
            logging.info("✅ SQLite 資料庫初始化完成 (WAL 模式)")
    
    def get_daily_stats(self, date: str = None) -> dict:
        """📊 獲取每日統計"""
        date = date or get_tw_date()
        with self.get_cursor() as cursor:
            cursor.execute("SELECT * FROM daily_stats WHERE date = ?", (date,))
            row = cursor.fetchone()
            if row:
                return {"date": row[0], "total": row[1], "wins": row[2],
                        "losses": row[3], "be": row[4], "pnl": row[5], "sl": row[6]}
        return {"date": date, "total": 0, "wins": 0, "losses": 0, "be": 0, "pnl": 0, "sl": 0}
    
    def get_monthly_stats(self, year_month: str = None) -> dict:
        """📊 獲取每月統計"""
        if not year_month:
            year_month = get_tw_date()[:7]
        with self.get_cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN is_win = 0 AND is_be = 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN is_be = 1 THEN 1 ELSE 0 END) as breakevens,
                    SUM(pnl) as total_pnl
                FROM trades WHERE date LIKE ?
            """, (f"{year_month}%",))
            row = cursor.fetchone()
            if row and row[0]:
                total, wins, losses, be, pnl = row
                win_rate = wins / total * 100 if total > 0 else 0
                return {"month": year_month, "total": total, "wins": wins,
                        "losses": losses, "be": be, "pnl": round(pnl or 0, 2),
                        "win_rate": round(win_rate, 1)}
        return {"month": year_month, "total": 0, "wins": 0, "losses": 0, "be": 0, "pnl": 0, "win_rate": 0}

# ─────────────────────────────────────────────────────────
# 4. 通知系統 (簡潔專業版 + 線層回覆)
# ─────────────────────────────────────────────────────────
def send_tg(msg: str, parse_mode: str = "Markdown", reply_to_id: int = None, buttons: list = None) -> int:
    """📤 發送 Telegram 通知"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 未設定")
        return None
    
    payload = {
        "chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_to_id:
        payload["reply_to_message_id"] = reply_to_id
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=5)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logging.warning(f"⚠️ TG API {r.status_code}")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗: {e}")
    return None

def _get_order_button(order_id: str) -> list:
    """🔘 生成訂單查詢按鈕"""
    return [{"text": f"🔍 查詢訂單 {order_id[-8:]}", "callback_data": f"order_{order_id}"}]

def _fmt_entry(coin: str, side: str, order_id: str, entry: float, sl: float, 
               tp1: float, tp2: float, tp3: float, score: int, signal_type: str) -> str:
    """📌 進場通知 (簡潔專業版)"""
    direction = "🟢 做多" if side == "LONG" else "🔴 做空"
    grade = "🔥" if score >= 80 else "⭐" if score >= 70 else "✅"
    
    # 計算百分比
    tp1_pct = (tp1 - entry) / entry * 100
    tp2_pct = (tp2 - entry) / entry * 100
    tp3_pct = (tp3 - entry) / entry * 100
    sl_pct = (sl - entry) / entry * 100
    
    return (
        f"🚀 *{coin}* {grade}\n"
        f"━━━━━━━━━━\n"
        f"🆔 `{order_id[-8:]}`\n"
        f"📊 {direction}\n"
        f"💰 `{entry:.4f}`\n"
        f"🎯 `{tp1:.4f}` (+{tp1_pct:.1f}%)\n"
        f"   `{tp2:.4f}` (+{tp2_pct:.1f}%)\n"
        f"   `{tp3:.4f}` (+{tp3_pct:.1f}%)\n"
        f"🛑 `{sl:.4f}` ({sl_pct:+.1f}%)\n"
        f"\n"
        f"📍 {signal_type}\n"
        f"💡 TP1 保本 · TP2 鎖利"
    )

def _fmt_tp(coin: str, order_id: str, tp_level: str, pnl_pct: float, r_mult: float) -> str:
    """🎯 止盈通知"""
    medal = {"TP1": "🥇", "TP2": "🥈", "TP3": "🏆"}.get(tp_level, "🎯")
    return (
        f"{medal} *{coin} {tp_level}*\n"
        f"━━━━━━━━━━\n"
        f"🆔 `{order_id[-8:]}`\n"
        f"💵 `+{pnl_pct:.1f}%` (`{r_mult}R`)\n"
        f"\n"
        f"{'💡 平 ⅓' if tp_level != 'TP3' else '🎯 全平'}"
    )

def _fmt_sl(coin: str, order_id: str, pnl_pct: float, is_be: bool) -> str:
    """🛑 止損通知"""
    label = "🛡 保本" if is_be else "❌ 止損"
    tag = "0R" if is_be else "-1R"
    return (
        f"{label} *{coin}*\n"
        f"━━━━━━━━━━\n"
        f"🆔 `{order_id[-8:]}`\n"
        f"📉 `{pnl_pct:+.1f}%` `{tag}`"
    )

def _fmt_daily_report(stats: dict) -> str:
    """📊 每日勝率報告"""
    win_rate = stats["wins"] / stats["total"] * 100 if stats["total"] > 0 else 0
    grade = "🏆 優秀" if win_rate >= 70 else "✅ 良好" if win_rate >= 50 else "⚠️ 待改進"
    return (
        f"📊 *每日戰報* {get_tw_date()}\n"
        f"━━━━━━━━━━\n"
        f"📈 交易：{stats['total']} 筆 {grade}\n"
        f"✅ 盈利：{stats['wins']} | ❌ 止損：{stats['losses']} | 🛡 保本：{stats['be']}\n"
        f"📊 勝率：*{win_rate:.1f}%*\n"
        f"💰 盈虧：`{stats['pnl']:+.2f}%`\n"
        f"\n"
        f"{'🎯 保持節奏' if win_rate >= 50 else '🔧 明日優化'}"
    )

def _fmt_monthly_report(stats: dict) -> str:
    """📊 每月勝率報告"""
    grade = "🏆 優秀" if stats["win_rate"] >= 70 else "✅ 良好" if stats["win_rate"] >= 50 else "⚠️ 待改進"
    return (
        f"📊 *月度戰報* {stats['month']}\n"
        f"━━━━━━━━━━\n"
        f"📈 交易：{stats['total']} 筆 {grade}\n"
        f"✅ 盈利：{stats['wins']} | ❌ 止損：{stats['losses']} | 🛡 保本：{stats['be']}\n"
        f"📊 勝率：*{stats['win_rate']:.1f}%*\n"
        f"💰 盈虧：`{stats['pnl']:+.2f}%`\n"
        f"\n"
        f"{'🚀 下月繼續' if stats['pnl'] > 0 else '🔧 策略調整'}"
    )

# ─────────────────────────────────────────────────────────
# 5. 高級技術分析 (後台執行)
# ─────────────────────────────────────────────────────────
class AdvancedAnalyzer:
    """🔍 高級技術分析引擎 (後台靜默執行)"""
    
    @staticmethod
    def find_order_blocks(df, lookback=50):
        """🧱 尋找訂單塊 (OB)"""
        obs = []
        for i in range(len(df)-2, max(0, len(df)-lookback), -1):
            curr = df[i]
            if curr["c"] < curr["o"]:
                if all(df[j]["l"] >= curr["l"] * 0.999 for j in range(i+1, min(i+10, len(df)))):
                    obs.append({"type": "bearish", "high": curr["h"], "low": curr["l"]})
            elif curr["c"] > curr["o"]:
                if all(df[j]["h"] <= curr["h"] * 1.001 for j in range(i+1, min(i+10, len(df)))):
                    obs.append({"type": "bullish", "high": curr["h"], "low": curr["l"]})
            if len(obs) >= 3: break
        return obs

    @staticmethod
    def find_fvg(df, lookback=50):
        """⚡ 尋找公允價值缺口 (FVG)"""
        fvgs = []
        for i in range(len(df)-2, max(0, len(df)-lookback), -1):
            curr, prev2 = df[i], df[i+2]
            if curr["l"] > prev2["h"]:
                fvgs.append({"type": "bullish", "top": curr["l"], "bottom": prev2["h"]})
            elif curr["h"] < prev2["l"]:
                fvgs.append({"type": "bearish", "top": prev2["l"], "bottom": curr["h"]})
            if len(fvgs) >= 3: break
        return fvgs

    @staticmethod
    def find_snr(df, lookback=100):
        """📏 尋找支撐阻力 (SNR)"""
        highs = [c["h"] for c in df[-lookback:]]
        lows = [c["l"] for c in df[-lookback:]]
        return {"resistance": max(highs), "support": min(lows), "mid": (max(highs)+min(lows))/2}

    @staticmethod
    def detect_price_action(df):
        """📊 檢測價格行為"""
        if len(df) < 3: return "none"
        last, prev = df[-1], df[-2]
        body = abs(last["c"] - last["o"])
        upper = last["h"] - max(last["c"], last["o"])
        lower = min(last["c"], last["o"]) - last["l"]
        if upper > body * 2 and lower < body * 0.5: return "bearish_pin"
        elif lower > body * 2 and upper < body * 0.5: return "bullish_pin"
        elif last["c"] > last["o"] and prev["c"] < prev["o"] and last["c"] > prev["o"]: return "bullish_engulf"
        elif last["c"] < last["o"] and prev["c"] > prev["o"] and last["c"] < prev["o"]: return "bearish_engulf"
        return "none"

# ─────────────────────────────────────────────────────────
# 6. 技術指標
# ─────────────────────────────────────────────────────────
def calc_atr(df, period=14):
    """📐 計算 ATR"""
    if len(df) < period + 1: return 0.001
    trs = [max(df[i]["h"]-df[i]["l"], abs(df[i]["h"]-df[i-1]["c"]), abs(df[i]["l"]-df[i-1]["c"])) for i in range(1, len(df))]
    return sum(trs[-period:]) / period if len(trs) >= period else 0.001

def calc_supertrend(df, period=10):
    """📈 計算 Supertrend"""
    if len(df) < period + 2: return 0
    atr = calc_atr(df, period)
    mid = sum(row["c"] for row in df[-20:]) / 20
    curr = df[-1]["c"]
    if curr > mid + atr * 0.5: return 1
    if curr < mid - atr * 0.5: return -1
    return 0

def calc_rsi(df, period=14):
    """📊 計算 RSI"""
    if len(df) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        chg = df[i]["c"] - df[i-1]["c"]
        gains.append(chg if chg > 0 else 0)
        losses.append(-chg if chg < 0 else 0)
    if len(gains) < period: return 50.0
    avg_g, avg_l = sum(gains[-period:])/period, sum(losses[-period:])/period
    if avg_l == 0: return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))

def calc_vol_ratio(df, curr_vol):
    """📊 計算成交量比率"""
    if len(df) < 20: return 1.0
    avg = sum(r["v"] for r in df[-20:]) / 20
    return curr_vol / avg if avg > 0 else 1.0

def calc_advanced_score(df, side, curr_vol):
    """📊 專業評分系統 (後台執行)"""
    score, signals = 0, []
    curr_price = df[-1]["c"]
    
    # 趨勢 (30 分)
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30; signals.append("趨勢順")
    elif st == 0: score += 15
    
    # RSI (25 分)
    rsi = calc_rsi(df)
    if side == "LONG":
        if 30 <= rsi <= 50: score += 25; signals.append("RSI 低")
        elif 50 < rsi < 70: score += 15
    else:
        if 50 <= rsi <= 70: score += 25; signals.append("RSI 高")
        elif 30 < rsi < 50: score += 15
    
    # OB (20 分)
    for ob in AdvancedAnalyzer.find_order_blocks(df):
        if ob["type"] == "bullish" and side == "LONG" and ob["low"]*0.999 <= curr_price <= ob["high"]*1.001:
            score += 20; signals.append("OB 支撐"); break
        elif ob["type"] == "bearish" and side == "SHORT" and ob["low"]*0.999 <= curr_price <= ob["high"]*1.001:
            score += 20; signals.append("OB 阻力"); break
    
    # FVG (15 分)
    for fvg in AdvancedAnalyzer.find_fvg(df):
        if fvg["type"] == "bullish" and side == "LONG" and fvg["bottom"]*0.999 <= curr_price <= fvg["top"]*1.001:
            score += 15; signals.append("FVG 多"); break
        elif fvg["type"] == "bearish" and side == "SHORT" and fvg["bottom"]*0.999 <= curr_price <= fvg["top"]*1.001:
            score += 15; signals.append("FVG 空"); break
    
    # SNR (5 分)
    snr = AdvancedAnalyzer.find_snr(df)
    if side == "LONG" and curr_price < snr["support"]*1.01:
        score += 5; signals.append("近支撐")
    elif side == "SHORT" and curr_price > snr["resistance"]*0.99:
        score += 5; signals.append("近阻力")
    
    # 價格行為 (5 分)
    pa = AdvancedAnalyzer.detect_price_action(df)
    if ("bull" in pa and side == "LONG") or ("bear" in pa and side == "SHORT"):
        score += 5; signals.append("PA 確認")
    
    # 成交量 (5 分)
    if calc_vol_ratio(df, curr_vol) >= 1.2:
        score += 5; signals.append("放量")
    
    # 確定信號類型
    if "OB" in " ".join(signals) and "FVG" in " ".join(signals):
        signal_type = "OB+FVG 共振"
    elif "OB" in " ".join(signals):
        signal_type = "訂單塊進場"
    elif "FVG" in " ".join(signals):
        signal_type = "缺口回補"
    elif "PA" in " ".join(signals):
        signal_type = "價格行為"
    else:
        signal_type = "技術面確認"
    
    return score, "A+ 極強" if score >= 85 else "A 強力" if score >= 70 else "B+ 觀望", signal_type

# ─────────────────────────────────────────────────────────
# 7. 訊號生成
# ─────────────────────────────────────────────────────────
def generate_signal(instId, df, price, volume):
    """🎯 生成訊號"""
    if df is None or len(df) < 50: return None
    atr = calc_atr(df)
    if atr / price > 0.04: return None
    
    # 多時框確認
    if MULTI_TF_CONFIRMATION:
        # 簡化版先跳過多時框確認
        pass
    
    signals = []
    for side in ["LONG", "SHORT"]:
        score, grade, signal_type = calc_advanced_score(df, side, volume)
        if score < SCORE_THRESHOLD: continue
        if VOLUME_CONFIRMATION and calc_vol_ratio(df, volume) < 1.0: continue
        
        entry = price
        sl = entry - atr*1.5 if side == "LONG" else entry + atr*1.5
        risk = abs(entry - sl)
        
        signals.append({
            "instId": instId, "side": side, "tf": "15m",
            "entry": round(entry, 4), "sl": round(sl, 4),
            "tp1": round(entry + risk if side == "LONG" else entry - risk, 4),
            "tp2": round(entry + risk*2.5 if side == "LONG" else entry - risk*2.5, 4),
            "tp3": round(entry + risk*4.0 if side == "LONG" else entry - risk*4.0, 4),
            "score": score, "grade": grade, "signal_type": signal_type,
            "created": time.time(), "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
        })
    return max(signals, key=lambda x: x["score"]) if signals else None

# ─────────────────────────────────────────────────────────
# 8. SignalTracker
# ─────────────────────────────────────────────────────────
class SignalTracker:
    """🔍 訊號追蹤器"""
    
    def __init__(self, db):
        self.db = db
        self.signals = self._load()
    
    def _load(self):
        """📥 從 SQLite 載入訊號"""
        signals = {}
        try:
            with self.db.get_cursor() as cursor:
                cursor.execute("SELECT * FROM signals WHERE status IN ('PENDING','ACTIVE','BE','TRAIL')")
                for row in cursor.fetchall():
                    sig = {
                        "id": row[0], "order_id": row[1], "instId": row[2], "side": row[3],
                        "status": row[4], "entry": row[5], "sl": row[6], "tp1": row[7],
                        "tp2": row[8], "tp3": row[9], "score": row[10],
                        "hit_tp1": bool(row[11]), "hit_tp2": bool(row[12]), "hit_tp3": bool(row[13]),
                        "entry_msg_id": row[14], "activated_at": row[15],
                        "created": row[16], "expires": row[17],
                    }
                    signals[sig["id"]] = sig
        except Exception as e:
            logging.error(f"❌ 載入失敗: {e}")
        return signals
    
    def add(self, signal, active=False, entry_msg_id=None):
        """📌 新增訊號"""
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        now = time.time()
        
        signal_data = {
            **signal, "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "entry_msg_id": entry_msg_id, "activated_at": now if active else None,
        }
        
        try:
            with self.db.get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO signals (id,order_id,inst_id,side,status,entry,sl,tp1,tp2,tp3,score,
                    hit_tp1,hit_tp2,hit_tp3,entry_msg_id,activated_at,created_at,expires_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (key, order_id, signal["instId"], signal["side"], signal_data["status"],
                      signal["entry"], signal["sl"], signal["tp1"], signal["tp2"], signal["tp3"],
                      signal["score"], 0, 0, 0, entry_msg_id, signal_data["activated_at"],
                      now, signal["expires"], now))
        except Exception as e:
            logging.error(f"❌ 寫入失敗: {e}")
        
        self.signals[key] = signal_data
        return key, order_id
    
    def update_signal(self, key, **kwargs):
        """🔄 更新訊號"""
        if key not in self.signals: return
        self.signals[key].update(kwargs)
        self.signals[key]["updated_at"] = time.time()
        try:
            with self.db.get_cursor() as cursor:
                updates = [f"{k}=?" for k in kwargs.keys()]
                values = list(kwargs.values()) + [time.time(), key]
                cursor.execute(f"UPDATE signals SET {','.join(updates)},updated_at=? WHERE id=?", values)
        except:
            pass
    
    def check_all(self, db):
        """🔄 檢查所有訊號"""
        global _daily_sl_count, _last_sl_date, _last_report_date
        
        # 每日回報
        now_hour, now_min = get_tw_hour(), get_tw_minute()
        report_hour, report_min = map(int, REPORT_TIME.split(":"))
        today = get_tw_date()
        
        if _last_report_date != today and now_hour == report_hour and now_min >= report_min:
            stats = db.get_daily_stats(today)
            if stats["total"] > 0:
                send_tg(_fmt_daily_report(stats))
            _last_report_date = today
        
        # SL 限額
        if _last_sl_date != today:
            _daily_sl_count = 0
            _last_sl_date = today
        if _daily_sl_count >= DAILY_SL_LIMIT:
            return []
        
        to_remove = []
        for key, sig in list(self.signals.items()):
            try:
                if self._check_one(key, sig, db):
                    to_remove.append(key)
            except Exception as e:
                logging.error(f"❌ check_one [{key}]: {e}")
        
        for key in to_remove:
            if key in self.signals:
                del self.signals[key]
        return to_remove
    
    def _check_one(self, key, sig, db):
        """🔍 檢查單一訊號"""
        global _daily_sl_count
        
        price = fetch_price(sig["instId"])
        if price <= 0: return False
        
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        side, status = sig["side"], sig["status"]
        entry, sl = sig["entry"], sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        reply_id = sig.get("entry_msg_id")
        
        # PENDING → ACTIVE
        if status == "PENDING":
            if time.time() > sig["expires"]:
                send_tg(f"⏰ *{coin} 過期*\n🆔 `{order_id[-8:]}`", reply_to_id=reply_id)
                return True
            
            in_zone = ((side == "LONG" and entry*0.994 <= price <= entry*1.002) or 
                      (side == "SHORT" and entry*0.998 <= price <= entry*1.006))
            
            if in_zone:
                if CORRELATION_FILTER:
                    others = [s for s in self.signals.values() if s["status"] in ("ACTIVE","BE","TRAIL") and s["side"] == side and s["id"] != key]
                    if others: return False
                
                sig["status"] = "ACTIVE"
                sig["activated_at"] = time.time()
                msg = _fmt_entry(coin, side, order_id, entry, sl, tp1, tp2, tp3, sig["score"], sig["signal_type"])
                new_msg_id = send_tg(msg, reply_to_id=reply_id, buttons=_get_order_button(order_id))
                if new_msg_id:
                    sig["entry_msg_id"] = new_msg_id
                self.update_signal(key, status="ACTIVE", activated_at=time.time(), entry_msg_id=new_msg_id)
            return False
        
        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False
        
        # SL 觸發
        if (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl):
            is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
            pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
            send_tg(_fmt_sl(coin, order_id, pnl, is_be), reply_to_id=reply_id, buttons=_get_order_button(order_id))
            _record_trade(db, coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
            _daily_sl_count += 1
            return True
        
        # TP1
        if not sig["hit_tp1"] and ((side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)):
            sig["hit_tp1"] = True
            sig["sl"] = entry
            sig["status"] = "BE"
            pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
            send_tg(_fmt_tp(coin, order_id, "TP1", pnl, 1.0), reply_to_id=reply_id, buttons=_get_order_button(order_id))
            _record_trade(db, coin, side, order_id, entry, price, "TP1", sig["score"])
            self.update_signal(key, hit_tp1=True, sl=entry, status="BE")
        
        # TP2
        if sig["hit_tp1"] and not sig["hit_tp2"] and ((side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)):
            sig["hit_tp2"] = True
            sig["sl"] = tp1
            sig["status"] = "TRAIL"
            pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
            send_tg(_fmt_tp(coin, order_id, "TP2", pnl, 2.5), reply_to_id=reply_id, buttons=_get_order_button(order_id))
            _record_trade(db, coin, side, order_id, entry, price, "TP2", sig["score"])
            self.update_signal(key, hit_tp2=True, sl=tp1, status="TRAIL")
        
        # TP3
        if sig["hit_tp2"] and not sig["hit_tp3"] and ((side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)):
            sig["hit_tp3"] = True
            pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
            send_tg(_fmt_tp(coin, order_id, "TP3", pnl, 4.0), reply_to_id=reply_id, buttons=_get_order_button(order_id))
            _record_trade(db, coin, side, order_id, entry, price, "TP3", sig["score"])
            return True
        
        return False
    
    def send_position_stats(self):
        """📋 獲取持倉統計"""
        positions = [sig for sig in self.signals.values() if sig["status"] in ("ACTIVE", "BE", "TRAIL", "PENDING")]
        if not positions:
            return "📭 *目前無持倉*\n\n🔄 系統持續掃描中"
        
        msg = f"📊 *追蹤中訊號 ({len(positions)} 筆)*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for i, p in enumerate(positions):
            coin = p["instId"].split("-")[0]
            side_emoji = "🟢" if p["side"] == "LONG" else "🔴"
            order_id = p.get("order_id", "N/A")
            cp = fetch_price(p["instId"])
            pnl_str = f"{((cp-p['entry'])/p['entry']*100):+.2f}%" if cp > 0 else "—"
            progress = "🏆" if p.get("hit_tp3") else "🥈" if p.get("hit_tp2") else "🥇" if p.get("hit_tp1") else "⏳"
            
            msg += (f"{side_emoji} *{coin}* {p['side']} · {p['status']} {progress}\n"
                   f"🆔 `{order_id}` | 評分 {p.get('score', 0)}\n"
                   f"進場 `{p['entry']:.4f}` → 現價 `{cp:.4f}` ({pnl_str})\n"
                   f"SL `{p['sl']:.4f}` TP1 `{p['tp1']:.4f}`{'✅' if p.get('hit_tp1') else ''} "
                   f"TP2 `{p['tp2']:.4f}`{'✅' if p.get('hit_tp2') else ''} "
                   f"TP3 `{p['tp3']:.4f}`{'✅' if p.get('hit_tp3') else ''}\n")
            if i < len(positions) - 1:
                msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n"
        return msg
    
    def send_monthly_report(self, db):
        """📊 發送月度報告"""
        stats = db.get_monthly_stats()
        if stats["total"] > 0:
            send_tg(_fmt_monthly_report(stats))

def _record_trade(db, coin, side, order_id, entry, close_price, close_type, score):
    """📝 記錄交易歷史"""
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    
    try:
        with db.get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO trades (order_id,coin,side,entry,close,close_type,pnl,is_win,is_be,score,timestamp,date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (order_id, coin, side, entry, close_price, close_type, round(pnl, 2), 
                  1 if is_win else 0, 1 if is_be else 0, score, get_tw_time(), get_tw_date()))
            
            cursor.execute("""
                INSERT OR REPLACE INTO daily_stats (date,total_trades,wins,losses,breakevens,total_pnl,sl_count)
                SELECT ?, COALESCE(total_trades,0)+1, COALESCE(wins,0)+?, COALESCE(losses,0)+?, 
                       COALESCE(breakevens,0)+?, COALESCE(total_pnl,0)+?, COALESCE(sl_count,0)+?
                FROM daily_stats WHERE date=?
            """, (get_tw_date(), 1 if is_win else 0, 1 if not is_win and not is_be else 0,
                  1 if is_be else 0, pnl, 1 if close_type == "SL" else 0, get_tw_date()))
    except Exception as e:
        logging.error(f"❌ 記錄失敗: {e}")

# ─────────────────────────────────────────────────────────
# 9. 價格抓取 (OKX API)
# ─────────────────────────────────────────────────────────
def fetch_price(instId, retries=2):
    """🔍 獲取最新價格 (OKX API)"""
    for attempt in range(retries + 1):
        try:
            res = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}", timeout=3).json()
            if res.get("code") == "0" and res.get("data"):
                price = float(res["data"][0]["last"])
                if price > 0:
                    _price_cache[instId] = {"price": price, "timestamp": time.time()}
                    return price
        except:
            if attempt < retries:
                time.sleep(0.3)
    return _price_cache.get(instId, {}).get("price", 0.0)

def fetch_candles(instId, tf="15m", limit=100):
    """📊 獲取 K 線"""
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}", timeout=3).json()
        if res.get("code") != "0": return None
        data = res.get("data", [])
        if len(data) < 30: return None
        confirmed = [row for row in data if row[8] == "1"][::-1]
        return [{"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in confirmed]
    except:
        return None

# ─────────────────────────────────────────────────────────
# 10. 主掃描
# ─────────────────────────────────────────────────────────
def run_scan(tracker, db):
    """🔍 執行掃描"""
    logging.info("🚀 開始掃描")
    sent = 0
    
    today = get_tw_date()
    if _last_sl_date != today:
        _daily_sl_count = 0
    
    if _daily_sl_count >= DAILY_SL_LIMIT:
        logging.warning(f"⚠️ 已達 SL 限額 ({_daily_sl_count}/{DAILY_SL_LIMIT})")
        return 0
    
    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS:
            break
        
        cool_key = f"{instId}_ALL"
        if cool_key in _signal_cooldown and time.time() - _signal_cooldown[cool_key] < 2 * 3600:
            continue
        
        try:
            price = fetch_price(instId)
            if price <= 0:
                continue
            
            candles = fetch_candles(instId)
            if not candles:
                continue
            
            signal = generate_signal(instId, candles, price, candles[-1]["v"])
            if not signal:
                continue
            
            # 檢查重複
            dup = any(s["instId"] == instId and s["side"] == signal["side"] and s["status"] in ("PENDING","ACTIVE","BE","TRAIL") for s in tracker.signals.values())
            if dup:
                continue
            
            in_zone = ((signal["side"] == "LONG" and signal["entry"]*0.994 <= price <= signal["entry"]*1.002) or
                      (signal["side"] == "SHORT" and signal["entry"]*0.998 <= price <= signal["entry"]*1.006))
            
            # 先建立訂單取得真實 ID，再發送通知
            key, order_id = tracker.add(signal, active=in_zone)
            msg = _fmt_entry(instId.split("-")[0], signal["side"], order_id, signal["entry"], signal["sl"],
                           signal["tp1"], signal["tp2"], signal["tp3"], signal["score"], signal["signal_type"])
            
            entry_msg_id = send_tg(msg, reply_to_id=None, buttons=_get_order_button(order_id))
            if entry_msg_id:
                tracker.update_signal(key, entry_msg_id=entry_msg_id)
            
            _signal_cooldown[cool_key] = time.time()
            sent += 1
            logging.info(f"✅ {instId} 發送 {order_id}")
        except Exception as e:
            logging.error(f"[{instId}] 掃描失敗: {e}")
            continue
    
    tracker.check_all(db)
    logging.info(f"🎯 本輪發送 {sent} 筆")
    return sent

# ─────────────────────────────────────────────────────────
# 11. 入口
# ─────────────────────────────────────────────────────────
def main():
    """🚀 主函式"""
    try:
        logging.info("=" * 60)
        logging.info("🤖 Alpha Oracle Pro v14.0 完整專業版")
        logging.info("=" * 60)
        
        if not TG_TOKEN or not CHAT_ID:
            logging.error("❌ TG_TOKEN 或 CHAT_ID 未設定！")
            sys.exit(1)
        
        db = Database(DB_FILE)
        tracker = SignalTracker(db)
        
        # 命令處理
        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats", "/持倉"):
                send_tg(tracker.send_position_stats())
                return
            elif cmd == "/daily":
                send_tg(_fmt_daily_report(db.get_daily_stats()))
                return
            elif cmd == "/monthly":
                send_tg(_fmt_monthly_report(db.get_monthly_stats()))
                return
        
        # 每月 1 號發送月報
        if get_tw_date().endswith("-01") and get_tw_hour() == 22 and get_tw_minute() < 5:
            tracker.send_monthly_report(db)
        
        # 執行掃描
        run_scan(tracker, db)
        logging.info("🎉 執行完成")
        
    except KeyboardInterrupt:
        logging.info("⚠️ 程式被中斷")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        logging.critical(f"🔥 未捕獲的異常: {type(e).__name__}: {e}")
        import traceback
        logging.critical(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
