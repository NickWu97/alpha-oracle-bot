#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v10.5 — 繁體中文版 (快速退出)
══════════════════════════════════════════════════════════════════════
⚡ 修復：GitHub Actions 一直運行的問題
✨ 功能：繁體中文 + 精致通知 + 確保 2 分鐘內完成
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import logging
import time
import sys
from datetime import datetime, timezone

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

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    stream=sys.stdout
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

# ⚡ 只監控前 3 大幣種（加快執行）
ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

SIGNAL_EXPIRE_HOURS = 24
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

_price_cache = {}
_signal_cooldown = {}

# ─────────────────────────────────────────────────────────
# 2. 通知系統（繁體中文）
# ─────────────────────────────────────────────────────────
def send_tg(msg: str) -> bool:
    if not TG_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
        return r.status_code == 200
    except:
        return False

def _format_tp_alert(coin: str, side: str, tp_level: str, price: float, 
                     entry: float, pnl_pct: float, r_mult: float) -> str:
    """🎯 止盈通知（繁體中文）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    return (
        f"{emoji} *{coin} {tp_level} 達標！*\n"
        f"────────────\n"
        f"方向：{direction}\n"
        f"價格：`{price:.4f}`\n"
        f"獲利：`+{pnl_pct:.1f}%` (`+{r_mult}R`)\n"
        f"\n"
        f"💡 建議全部平倉"
    )

def _format_sl_alert(coin: str, side: str, price: float, entry: float, 
                     pnl_pct: float, is_be: bool = False) -> str:
    """🛑 止損通知（繁體中文）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    label = "🔒 保本出場" if is_be else "❌ 止損離場"
    return (
        f"{label} *{coin}*\n"
        f"────────────\n"
        f"方向：{direction}\n"
        f"價格：`{price:.4f}`\n"
        f"結果：`{pnl_pct:+.1f}%`"
    )

def _format_entry_alert(coin: str, side: str, price: float, entry: float,
                        sl: float, tp1: float, tp2: float, tp3: float, score: int) -> str:
    """📌 進場通知（繁體中文）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥" if score >= 80 else "✅" if score >= 68 else "⚪"
    return (
        f"{emoji} *{coin} 進場提醒* {grade}\n"
        f"────────────\n"
        f"方向：{direction}\n"
        f"價格：`{price:.4f}`\n"
        f"評分：{score}分\n"
        f"\n"
        f"🎯 止盈：\n"
        f"  TP1 `{tp1:.4f}` (+{(tp1-entry)/entry*100:.1f}%)\n"
        f"  TP2 `{tp2:.4f}` (+{(tp2-entry)/entry*100:.1f}%)\n"
        f"  TP3 `{tp3:.4f}` (+{(tp3-entry)/entry*100:.1f}%)\n"
        f"\n"
        f"🛑 止損：`{sl:.4f}` ({(sl-entry)/entry*100:+.1f}%)"
    )

def _format_position_card(coin: str, side: str, score: int,
                         current: float, entry: float, sl: float,
                         tp1: float, tp2: float, tp3: float,
                         status: str, hit_tp1: bool, hit_tp2: bool, hit_tp3: bool) -> str:
    """📊 持倉卡片（繁體中文）"""
    coin_emoji = "🟠" if "BTC" in coin else "🔷" if "ETH" in coin else "🟣"
    side_emoji = "🟢" if side == "LONG" else "🔴"
    pnl = ((current - entry) / entry * 100) if side == "LONG" else ((entry - current) / entry * 100)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    progress = "🥇🏆" if hit_tp3 else "🥇🥈🏆" if hit_tp2 else "🥇🥈" if hit_tp1 else "⏳🏆"
    
    return (
        f"{coin_emoji} *#{coin}* · {side_emoji} {side} · {score}分\n"
        f"{' ACTIVE · 持倉中' if status == 'ACTIVE' else status}\n"
        f"✅ 當前 `{current:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
        f"🟢 進場 `{entry:.4f}`\n"
        f"🔴 止損 `{sl:.4f}`\n"
        f"🥇 TP1 `{tp1:.4f}`\n"
        f"🥈 TP2 `{tp2:.4f}`\n"
        f"🏆 TP3 `{tp3:.4f}`\n"
        f"進度 {progress}"
    )

# ─────────────────────────────────────────────────────────
# 3. 數據抓取（快速版）
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 10:
            return price
    
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=2
        ).json()
        if res.get("code") == "0" and res.get("data"):
            price = float(res["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                return price
    except:
        pass
    
    return _price_cache.get(instId, (0, 0))[0] if instId in _price_cache else 0.0

def fetch_candles(instId: str, tf: str = "15m", limit: int = 100):
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=3
        ).json()
        if res.get("code") != "0":
            return None
        
        data = res.get("data", [])
        if len(data) < 30:
            return None
        
        confirmed = [row for row in data if row[8] == "1"][::-1]
        
        df = []
        for row in confirmed:
            df.append({
                "ts": row[0], "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5])
            })
        
        return df
    except:
        return None

# ─────────────────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────────────────
def calc_atr(df, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.001
    
    tr_values = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i-1]["c"])
        lc = abs(df[i]["l"] - df[i-1]["c"])
        tr = max(hl, hc, lc)
        tr_values.append(tr)
    
    if len(tr_values) < period:
        return 0.001
    
    atr = sum(tr_values[-period:]) / period
    return atr if atr > 0 else 0.001

def calc_supertrend(df, period: int = 10, mult: float = 3.0) -> int:
    if len(df) < period + 2:
        return 0
    
    atr = calc_atr(df, period)
    mid_price = sum(row["c"] for row in df[-20:]) / 20
    current_price = df[-1]["c"]
    
    if current_price > mid_price + atr * 0.5:
        return 1
    elif current_price < mid_price - atr * 0.5:
        return -1
    else:
        return 0

def calc_rsi(df, period: int = 14) -> float:
    if len(df) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    
    for i in range(1, len(df)):
        change = df[i]["c"] - df[i-1]["c"]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-change)
    
    if len(gains) < period:
        return 50.0
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_score(df, side: str) -> tuple:
    score = 0
    
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 60
    elif st == 0:
        score += 30
    
    rsi = calc_rsi(df)
    if side == "LONG":
        if 30 <= rsi <= 50:
            score += 40
        elif 50 < rsi < 70:
            score += 20
    else:
        if 50 <= rsi <= 70:
            score += 40
        elif 30 < rsi < 50:
            score += 20
    
    grade = "A+ 極強 🔥" if score >= 85 else "A 強力 ⭐" if score >= 70 else "B+ 觀望 ✅"
    return score, grade

# ─────────────────────────────────────────────────────────
# 5. 訊號生成
# ─────────────────────────────────────────────────────────
def generate_signal(instId: str, df):
    if df is None or len(df) < 50:
        return None
    
    price = df[-1]["c"]
    atr = calc_atr(df)
    
    if atr / price > 0.04:
        return None
    
    signals = []
    for side in ["LONG", "SHORT"]:
        score, grade = calc_score(df, side)
        if score < SCORE_THRESHOLD:
            continue
        
        entry = price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)
        
        signal = {
            "instId": instId,
            "side": side,
            "tf": "15m",
            "entry": round(entry, 4),
            "sl": round(sl, 4),
            "tp1": round(entry + risk if side == "LONG" else entry - risk, 4),
            "tp2": round(entry + risk*2.5 if side == "LONG" else entry - risk*2.5, 4),
            "tp3": round(entry + risk*4.0 if side == "LONG" else entry - risk*4.0, 4),
            "score": score,
            "grade": grade,
            "created": time.time(),
            "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
        }
        signals.append(signal)
    
    return max(signals, key=lambda x: x["score"]) if signals else None

# ─────────────────────────────────────────────────────────
# 6. SignalTracker 類
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals = self._load()
        self.transitions = 0
    
    def _load(self) -> dict:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f:
                    return json.load(f)
        except:
            pass
        return {}
    
    def _save(self):
        try:
            temp = self.filepath + ".tmp"
            with open(temp, "w") as f:
                json.dump(self.signals, f, indent=2)
            os.replace(temp, self.filepath)
        except:
            pass
    
    def add(self, signal: dict, active: bool = False) -> str:
        key = f"{signal['instId']}_{signal['side']}_{int(time.time())}"
        self.signals[key] = {
            **signal,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "activated_at": time.time() if active else None,
        }
        self._save()
        return key
    
    def remove(self, key: str):
        if key in self.signals:
            del self.signals[key]
            self._save()
    
    def check_all(self):
        self.transitions = 0
        to_remove = []
        
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig):
                to_remove.append(key)
        
        for key in to_remove:
            del self.signals[key]
        self._save()
    
    def _check_one(self, key: str, sig: dict) -> bool:
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                return False
            
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
                    sig["status"] = "ACTIVE"
                    sig["activated_at"] = time.time()
                    self._save()
                    send_tg(_format_entry_alert(coin, side, price, entry, sl, tp1, tp2, tp3, sig["score"]))
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False
            
            def _dev(t):
                return abs(price - t) / t * 100
            
            # SL 觸發
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit or (_dev(sl) > 0.003 and ((side == "LONG" and price < sl) or (side == "SHORT" and price > sl))):
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(_format_sl_alert(coin, side, price, entry, pnl, is_be))
                _record_trade(coin, side, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            # TP3 觸發
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if (tp3_hit or _dev(tp3) > 0.003) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                send_tg(_format_tp_alert(coin, side, "TP3", tp3, entry, pnl, 4.0))
                _record_trade(coin, side, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # TP2 觸發
            tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if (tp2_hit or _dev(tp2) > 0.003) and not sig.get("hit_tp2"):
                sig["hit_tp2"] = True
                sig["sl"] = tp1
                sig["status"] = "TRAIL"
                self._save()
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                send_tg(_format_tp_alert(coin, side, "TP2", tp2, entry, pnl, 2.5))
                _record_trade(coin, side, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # TP1 觸發
            tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if (tp1_hit or _dev(tp1) > 0.003) and not sig.get("hit_tp1"):
                sig["hit_tp1"] = True
                sig["sl"] = entry
                sig["status"] = "BE"
                self._save()
                send_tg(_format_tp_alert(coin, side, "TP1", tp1, entry, 0.0, 1.0))
                _record_trade(coin, side, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
        except:
            return False
    
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
                coin=p["instId"].split("-")[0],
                side=p["side"],
                score=p.get("score", 0),
                current=p.get("current_price", p["entry"]),
                entry=p["entry"],
                sl=p["sl"],
                tp1=p["tp1"],
                tp2=p["tp2"],
                tp3=p["tp3"],
                status=p["status"],
                hit_tp1=p.get("hit_tp1", False),
                hit_tp2=p.get("hit_tp2", False),
                hit_tp3=p.get("hit_tp3", False)
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

def _record_trade(coin: str, side: str, entry: float, close_price: float, 
                  close_type: str, score: int):
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    
    trade = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "coin": coin,
        "side": side,
        "entry": entry,
        "close": close_price,
        "close_type": close_type,
        "pnl": round(pnl, 2),
        "is_win": is_win,
        "is_be": is_be,
        "score": score,
    }
    try:
        history = []
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r") as f:
                history = json.load(f)
        history.append(trade)
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except:
        pass

# ─────────────────────────────────────────────────────────
# 7. 主掃描邏輯（快速版）
# ─────────────────────────────────────────────────────────
def run_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 開始掃描...")
    sent = 0
    
    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS:
            break
        
        key = f"{instId}_ALL"
        if key in _signal_cooldown and time.time() - _signal_cooldown[key] < 2 * 3600:
            continue
        
        try:
            df = fetch_candles(instId)
            if df is None:
                continue
            
            signal = generate_signal(instId, df)
            if not signal:
                continue
            
            if send_tg(_format_entry_alert(
                coin=instId.split("-")[0],
                side=signal["side"],
                price=signal["entry"],
                entry=signal["entry"],
                sl=signal["sl"],
                tp1=signal["tp1"],
                tp2=signal["tp2"],
                tp3=signal["tp3"],
                score=signal["score"]
            )):
                _signal_cooldown[key] = time.time()
                price = fetch_price(instId)
                in_zone = (
                    (signal["side"] == "LONG" and signal["entry"]*(1-0.006) <= price <= signal["entry"]*(1+0.002)) or
                    (signal["side"] == "SHORT" and signal["entry"]*(1-0.002) <= price <= signal["entry"]*(1+0.006))
                )
                tracker.add(signal, active=in_zone and price > 0)
                sent += 1
        except:
            continue
    
    tracker.check_all()
    
    if tracker.transitions > 0 or tracker.signals:
        send_tg(tracker.status_summary())
    
    logging.info(f"✅ 掃描完成，發送 {sent} 筆訊號")
    return sent

# ─────────────────────────────────────────────────────────
# 8. 主函式
# ─────────────────────────────────────────────────────────
def main():
    try:
        logging.info("=" * 40)
        logging.info("🤖 Alpha Oracle Pro v10.5 啟動")
        logging.info("=" * 40)
        
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        
        # 🔹 處理 /stats 命令
        if len(sys.argv) > 1 and sys.argv[1] in ("/stats", "/持倉"):
            send_tg(tracker.get_position_stats())
            return
        
        # 🔹 執行掃描 + 監控
        run_scan(tracker)
        
        logging.info("🎉 程式執行完成")
        
    except Exception as e:
        logging.error(f"🔥 系統錯誤: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
