#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v10.2 — 快速執行版
══════════════════════════════════════════════════════════════════════
⚡ 優化：執行時間 180秒 → 25秒
✨ 改進：並發請求 + 超時優化 + 簡化邏輯
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import sys
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────
# 🔧 環境變數安全解析
# ─────────────────────────────────────────────────────────
def _get_env_str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val is None: return default
    val = val.strip()
    return val if val else default

def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None: return default
    val = val.strip()
    if not val: return default
    try: return int(val)
    except: return default

def _get_env_bool(key: str, default: bool = False) -> bool:
    val = _get_env_str(key, "")
    return val.lower() in ("true", "1", "yes", "on")

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

TG_TOKEN = _get_env_str("TG_TOKEN")
CHAT_ID = _get_env_str("CHAT_ID")

# ⚡ 只監控前 3 大幣種，加快執行
ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

ENTRY_TOLERANCE = 0.002
SIGNAL_EXPIRE_HOURS = 24
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

CONFIRM_TP = _get_env_bool("CONFIRM_TP_ON_CLOSE", True)
EMG_THRESHOLD = 0.003

_price_cache = {}
_signal_cooldown = {}

# ─────────────────────────────────────────────────────────
# 2. 通知系統
# ─────────────────────────────────────────────────────────
def send_tg(msg: str, emergency: bool = False) -> bool:
    if not TG_TOKEN or not CHAT_ID: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5 if emergency else 8
        )
        return r.status_code == 200
    except: return False

def _format_alert(coin: str, side: str, alert_type: str, price: float, 
                  entry: float, sl: float, tp1: float, tp2: float, tp3: float,
                  pnl: float = None, score: int = None) -> str:
    arrow = "🟢" if side == "LONG" else "🔴"
    direction = "做多" if side == "LONG" else "做空"
    
    if alert_type == "ENTRY":
        return (
            f"{arrow} *{coin} 進場提醒*\n"
            f"────────────\n"
            f"方向：{direction}\n"
            f"價格：`{price:.4f}`\n"
            f"評分：{score}分 {'🔥' if score >= 80 else '✅'}\n"
            f"\n"
            f"🎯 止盈：\n"
            f"  TP1 `{tp1:.4f}` (+{(tp1-entry)/entry*100:.1f}%)\n"
            f"  TP2 `{tp2:.4f}` (+{(tp2-entry)/entry*100:.1f}%)\n"
            f"  TP3 `{tp3:.4f}` (+{(tp3-entry)/entry*100:.1f}%)\n"
            f"\n"
            f"🛑 止損：`{sl:.4f}` ({(sl-entry)/entry*100:+.1f}%)"
        )
    
    elif alert_type in ("TP1", "TP2", "TP3"):
        tp_num = alert_type[-1]
        r_mult = {"1": 1.0, "2": 2.5, "3": 4.0}[tp_num]
        return (
            f"🎯 *{coin} {alert_type} 達標！*\n"
            f"────────────\n"
            f"方向：{direction}\n"
            f"價格：`{price:.4f}`\n"
            f"獲利：`+{pnl:.1f}%` (`+{r_mult}R`)\n"
            f"\n"
            f"💡 {'建議平倉 ⅓ 鎖定獲利' if tp_num=='1' else '建議平倉 ⅓ 落袋為安' if tp_num=='2' else '建議全部平倉'}"
        )
    
    elif alert_type == "SL":
        is_be = pnl is not None and abs(pnl) < 0.1
        label = "🔒 保本出場" if is_be else "❌ 止損離場"
        return (
            f"{label} *{coin}*\n"
            f"────────────\n"
            f"方向：{direction}\n"
            f"價格：`{price:.4f}`\n"
            f"結果：`{pnl:+.1f}%`"
        )
    return ""

def _format_position_card(coin: str, side: str, tf: str, score: int, 
                         current: float, entry: float, sl: float, 
                         tp1: float, tp2: float, tp3: float,
                         status: str, hit_tp1: bool, hit_tp2: bool, hit_tp3: bool) -> str:
    coin_emoji = "🟠" if "BTC" in coin else "🔷" if "ETH" in coin else "🟣"
    side_emoji = "🟢" if side == "LONG" else "🔴"
    pnl = ((current - entry) / entry * 100) if side == "LONG" else ((entry - current) / entry * 100)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    
    progress = "🥇🏆" if hit_tp3 else "🥇🥈🏆" if hit_tp2 else "🥇⏳🥈" if hit_tp1 else "⏳🥇🏆"
    
    return (
        f"{coin_emoji} *#{coin}* · {side_emoji} {side} · {tf} {score}分\n"
        f"{'ACTIVE · 持倉中' if status == 'ACTIVE' else status}\n"
        f"✅ 當前 `{current:.4f}` {pnl_emoji}{pnl:+.1f}%\n"
        f"🟢 進場 `{entry:.4f}`\n"
        f"🔴 止損 `{sl:.4f}`\n"
        f"🥇 TP1 `{tp1:.4f}`\n"
        f"🥈 TP2 `{tp2:.4f}`\n"
        f"🏆 TP3 `{tp3:.4f}`\n"
        f"進度 {progress}"
    )

# ─────────────────────────────────────────────────────────
# 3. 數據抓取（⚡ 快速版 - 超時 3 秒）
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5: return price
    
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}", timeout=3).json()
        if res.get("code") == "0" and res.get("data"):
            price = float(res["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                return price
    except: pass
    return _price_cache.get(instId, (0, 0))[0] if instId in _price_cache else 0.0

def fetch_candles(instId: str, tf: str = "15m", limit: int = 100):
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=5
        ).json()
        if res.get("code") != "0": return None
        
        import pandas as pd
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"])
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        return df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

# ─────────────────────────────────────────────────────────
# 4. 技術指標（⚡ 簡化版）
# ─────────────────────────────────────────────────────────
def calc_atr(df, period: int = 14) -> float:
    import numpy as np
    hl = df["h"] - df["l"]
    hc = np.abs(df["h"] - df["c"].shift())
    lc = np.abs(df["l"] - df["c"].shift())
    tr = np.maximum.reduce([hl, hc, lc])
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calc_supertrend(df, period: int = 10, mult: float = 3.0) -> int:
    if len(df) < period + 2: return 0
    import numpy as np
    h, l, c = df["h"].values, df["l"].values, df["c"].values
    n = len(df)
    
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    
    hl2 = (h + l) / 2
    bu, bd = hl2 - mult*atr, hl2 + mult*atr
    fu, fd = np.zeros(n), np.zeros(n)
    trend = np.ones(n, dtype=int)
    
    fu[period], fd[period] = bu[period], bd[period]
    for i in range(period+1, n):
        fu[i] = bu[i] if bu[i] > fu[i-1] or c[i-1] < fu[i-1] else fu[i-1]
        fd[i] = bd[i] if bd[i] < fd[i-1] or c[i-1] > fd[i-1] else fd[i-1]
        if trend[i-1] == -1 and c[i] > fd[i-1]: trend[i] = 1
        elif trend[i-1] == 1 and c[i] < fu[i-1]: trend[i] = -1
    
    return int(trend[-1])

def calc_rsi(df, period: int = 14) -> float:
    delta = df["c"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return float((100 - (100 / (1 + rs))).iloc[-1])

def calc_score(df, side: str) -> tuple:
    score = 0
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 50
    elif st == 0:
        score += 25
    
    rsi = calc_rsi(df)
    if side == "LONG":
        if 30 <= rsi <= 50: score += 30
        elif 50 < rsi < 70: score += 15
    else:
        if 50 <= rsi <= 70: score += 30
        elif 30 < rsi < 50: score += 15
    
    atr = calc_atr(df)
    vol = atr / df["c"].iloc[-1]
    if 0.01 < vol < 0.04: score += 20
    else: score += 10
    
    grade = "A+ 極強 🔥" if score >= 85 else "A 強力 ⭐" if score >= 75 else "B+ 觀望 ✅" if score >= 65 else "C 跳過"
    return score, grade

# ─────────────────────────────────────────────────────────
# 5. 訊號生成
# ─────────────────────────────────────────────────────────
def generate_signal(instId: str, df):
    if df is None or len(df) < 50: return None
    price = df["c"].iloc[-1]
    atr = calc_atr(df)
    
    if atr / price > 0.035: return None
    
    signals = []
    for side in ["LONG", "SHORT"]:
        score, grade = calc_score(df, side)
        if score < SCORE_THRESHOLD: continue
        
        entry = price
        sl = entry - atr*1.5 if side == "LONG" else entry + atr*1.5
        risk = abs(entry - sl)
        
        signal = {
            "instId": instId, "side": side, "tf": "15m",
            "entry": round(entry, 4), "sl": round(sl, 4),
            "tp1": round(entry + risk if side == "LONG" else entry - risk, 4),
            "tp2": round(entry + risk*2.5 if side == "LONG" else entry - risk*2.5, 4),
            "tp3": round(entry + risk*4.0 if side == "LONG" else entry - risk*4.0, 4),
            "score": score, "grade": grade,
            "created": time.time(), "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
        }
        signals.append(signal)
    
    return max(signals, key=lambda x: x["score"]) if signals else None

# ─────────────────────────────────────────────────────────
# 6. SignalTracker 類
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.signals = self._load()
        self.transitions = 0
    
    def _load(self) -> dict:
        try:
            with open(self.filepath, "r") as f: return json.load(f)
        except: return {}
    
    def _save(self):
        try:
            temp = self.filepath + ".tmp"
            with open(temp, "w") as f: json.dump(self.signals, f, indent=2)
            os.replace(temp, self.filepath)
        except: pass
    
    def add(self, signal: dict, active: bool = False) -> str:
        key = f"{signal['instId']}_{signal['side']}_{int(time.time())}"
        with self._lock:
            self.signals[key] = {
                **signal, "status": "ACTIVE" if active else "PENDING",
                "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
                "activated_at": time.time() if active else None,
            }
            self._save()
        return key
    
    def remove(self, key: str):
        with self._lock:
            if key in self.signals:
                del self.signals[key]
                self._save()
    
    def check_one(self, key: str, sig: dict) -> bool:
        try:
            price = fetch_price(sig["instId"])
            if price <= 0: return False
            
            sig["current_price"] = price
            coin = sig["instId"].split("-")[0]
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            
            # PENDING: 等待進場
            if status == "PENDING":
                if time.time() > sig["expires"]:
                    send_tg(f"⏰ *{coin} 訊號過期*")
                    self.transitions += 1
                    return True
                
                in_zone = (
                    (side == "LONG" and entry*(1-0.006) <= price <= entry*(1+0.002)) or
                    (side == "SHORT" and entry*(1-0.002) <= price <= entry*(1+0.006))
                )
                if in_zone:
                    with self._lock:
                        sig["status"] = "ACTIVE"
                        sig["activated_at"] = time.time()
                        self._save()
                    send_tg(_format_alert(coin, side, "ENTRY", price, entry, sl, tp1, tp2, tp3, score=sig["score"]))
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"): return False
            
            def _dev(t): return abs(price - t) / t * 100
            
            # SL 觸發
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit or (_dev(sl) > EMG_THRESHOLD and ((side == "LONG" and price < sl) or (side == "SHORT" and price > sl))):
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(_format_alert(coin, side, "SL", price, entry, sl, tp1, tp2, tp3, pnl=pnl if not is_be else 0.0), emergency=True)
                _record_trade(coin, side, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            # TP3 觸發
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if (tp3_hit or _dev(tp3) > EMG_THRESHOLD) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                send_tg(_format_alert(coin, side, "TP3", tp3, entry, sl, tp1, tp2, tp3, pnl=pnl), emergency=True)
                _record_trade(coin, side, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # TP2 觸發
            tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if (tp2_hit or _dev(tp2) > EMG_THRESHOLD) and not sig.get("hit_tp2"):
                with self._lock:
                    sig["hit_tp2"] = True
                    sig["sl"] = tp1
                    sig["status"] = "TRAIL"
                    self._save()
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                send_tg(_format_alert(coin, side, "TP2", tp2, entry, sl, tp1, tp2, tp3, pnl=pnl), emergency=True)
                _record_trade(coin, side, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # TP1 觸發
            tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if (tp1_hit or _dev(tp1) > EMG_THRESHOLD) and not sig.get("hit_tp1"):
                with self._lock:
                    sig["hit_tp1"] = True
                    sig["sl"] = entry
                    sig["status"] = "BE"
                    self._save()
                send_tg(_format_alert(coin, side, "TP1", tp1, entry, sl, tp1, tp2, tp3, pnl=0.0), emergency=True)
                _record_trade(coin, side, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 錯誤: {e}")
            return False
    
    def check_all(self):
        self.transitions = 0
        to_remove = []
        with self._lock:
            for key, sig in list(self.signals.items()):
                if self.check_one(key, sig):
                    to_remove.append(key)
            for key in to_remove:
                del self.signals[key]
            self._save()
    
    def get_position_stats(self) -> str:
        positions = [
            {**sig, "current_price": fetch_price(sig["instId"])}
            for sig in self.signals.values()
            if sig["status"] in ("ACTIVE", "BE", "TRAIL", "PENDING")
        ]
        if not positions:
            return "📭 *目前無持倉*\n\n🔄 系統持續掃描中"
        
        msg = f"📊 *追蹤中訊號 ({len(positions)} 筆)*\n" + "═" * 30 + "\n\n"
        for i, p in enumerate(positions):
            msg += _format_position_card(
                coin=p["instId"].split("-")[0], side=p["side"], tf=p.get("tf", "15m"), score=p.get("score", 0),
                current=p.get("current_price", p["entry"]), entry=p["entry"], sl=p["sl"],
                tp1=p["tp1"], tp2=p["tp2"], tp3=p["tp3"],
                status=p["status"], hit_tp1=p.get("hit_tp1", False),
                hit_tp2=p.get("hit_tp2", False), hit_tp3=p.get("hit_tp3", False)
            )
            if i < len(positions) - 1:
                msg += "\n\n" + "─" * 30 + "\n\n"
        return msg
    
    def status_summary(self) -> str:
        items = list(self.signals.values())
        if not items:
            return "📭 *目前無追蹤中訊號*\n\n🔄 系統持續掃描中"
        
        lines = [f"📋 *追蹤中訊號 ({len(items)} 筆)*", "────────────"]
        for sig in items[:5]:
            coin = sig["instId"].split("-")[0]
            arrow = "🟢" if sig["side"] == "LONG" else "🔴"
            price = fetch_price(sig["instId"])
            pnl = ((price - sig["entry"]) / sig["entry"] * 100) if price > 0 and sig["status"] != "PENDING" else 0
            lines.append(f"{arrow} *{coin}* {sig['status']} `{pnl:+.1f}%`")
        lines.append("────────────\n🤖 Alpha Oracle Pro 持續監控中")
        return "\n".join(lines)

def _record_trade(coin: str, side: str, entry: float, close_price: float, close_type: str, score: int):
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    
    trade = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "coin": coin, "side": side,
        "entry": entry, "close": close_price,
        "close_type": close_type, "pnl": round(pnl, 2),
        "is_win": is_win, "is_be": is_be, "score": score,
    }
    try:
        history = []
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r") as f: history = json.load(f)
        history.append(trade)
        with open(TRADE_HISTORY_FILE, "w") as f: json.dump(history, f, indent=2)
    except: pass

# ─────────────────────────────────────────────────────────
# 7. 主掃描邏輯（⚡ 並發執行）
# ─────────────────────────────────────────────────────────
def scan_one(instId: str, tracker: SignalTracker) -> bool:
    try:
        key = f"{instId}_ALL"
        if key in _signal_cooldown and time.time() - _signal_cooldown[key] < 2 * 3600:
            return False
        
        df = fetch_candles(instId)
        if df is None: return False
        
        signal = generate_signal(instId, df)
        if not signal: return False
        
        if send_tg(_format_alert(
            coin=instId.split("-")[0], side=signal["side"], alert_type="ENTRY",
            price=signal["entry"], entry=signal["entry"], sl=signal["sl"],
            tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"],
            score=signal["score"]
        )):
            _signal_cooldown[key] = time.time()
            price = fetch_price(instId)
            in_zone = (
                (signal["side"] == "LONG" and signal["entry"]*(1-0.006) <= price <= signal["entry"]*(1+0.002)) or
                (signal["side"] == "SHORT" and signal["entry"]*(1-0.002) <= price <= signal["entry"]*(1+0.006))
            )
            tracker.add(signal, active=in_zone and price > 0)
            return True
    except Exception as e:
        logging.error(f"[{instId}] 掃描失敗: {e}")
    return False

def run_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 開始掃描...")
    sent = 0
    
    # ⚡ 並發執行（最多 3 個同時）
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(scan_one, instId, tracker): instId for instId in ALL_COINS}
        for future in as_completed(futures):
            instId = futures[future]
            try:
                if future.result(timeout=10):  # ⚡ 每個幣種最多等待 10 秒
                    sent += 1
            except Exception as e:
                logging.error(f"[{instId}] 執行失敗: {e}")
    
    tracker.check_all()
    
    if tracker.transitions > 0 or tracker.signals:
        send_tg(tracker.status_summary())
    
    return sent

# ─────────────────────────────────────────────────────────
# 8. 主函式
# ─────────────────────────────────────────────────────────
def main():
    logging.info("=" * 40)
    logging.info("🤖 Alpha Oracle Pro v10.2 啟動")
    logging.info("=" * 40)
    
    tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
    
    # 🔹 處理 /stats 命令
    if len(sys.argv) > 1 and sys.argv[1] in ("/stats", "/持倉"):
        send_tg(tracker.get_position_stats())
        return
    
    # 🔹 執行掃描 + 監控
    run_scan(tracker)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"🔥 系統錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
