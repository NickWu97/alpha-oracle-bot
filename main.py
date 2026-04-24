#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v10.9 — 線程回覆 + 按鈕版
══════════════════════════════════════════════════════════════════════
✨ 功能：
  ✅ 自動線程回覆：TP/SL 通知會回覆在進場訊息下方
  ✅ 底部互動按鈕：點擊查詢訂單詳情
  ✅ 精簡通知排版：更直觀的交易資訊
  ✅ 訊息 ID 持久化：重啟後依然能正確回覆
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import logging
import time
import sys
import uuid
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

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

SIGNAL_EXPIRE_HOURS = 24
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

_price_cache = {}
_signal_cooldown = {}

# ─────────────────────────────────────────────────────────
# 2. 通知系統（線程回覆 + 按鈕）
# ─────────────────────────────────────────────────────────
def send_tg(msg: str, reply_to_id: int = None, buttons: list = None) -> int:
    """
    📤 發送 Telegram 通知
    ✅ 支援 reply_to_message_id (實現線程回覆)
    ✅ 支援 Inline Keyboard (實現底部按鈕)
    🔄 返回 message_id，以便後續追蹤
    """
    if not TG_TOKEN or not CHAT_ID:
        return None
    
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    
    if reply_to_id:
        payload["reply_to_message_id"] = reply_to_id
    
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=5
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        logging.error(f"❌ TG 發送失敗: {e}")
    
    return None

def _get_order_button(order_id: str) -> list:
    """🔘 生成訂單查詢按鈕"""
    return [{
        "text": f"🔍 查詢訂單 {order_id[-8:]}",
        "callback_data": f"order_{order_id}"
    }]

def _format_entry_alert(coin: str, side: str, order_id: str, price: float, entry: float,
                        sl: float, tp1: float, tp2: float, tp3: float, score: int) -> str:
    """📌 進場通知 (精簡版)"""
    direction = "做多 🟢" if side == "LONG" else "做空 🔴"
    grade_emoji = "🔥" if score >= 80 else "✅" if score >= 68 else "⚪"
    
    return (
        f"🚀 *{coin} 進場提醒* {grade_emoji}\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"📊 方向：{direction}\n"
        f"📌 進場：`{entry:.4f}`\n"
        f"🎯 TP1：`{tp1:.4f}` (+{(tp1-entry)/entry*100:.1f}%)\n"
        f"🎯 TP2：`{tp2:.4f}` (+{(tp2-entry)/entry*100:.1f}%)\n"
        f"🎯 TP3：`{tp3:.4f}` (+{(tp3-entry)/entry*100:.1f}%)\n"
        f"🛑 止損：`{sl:.4f}`\n"
        f"\n"
        f"📝 評分：{score}分"
    )

def _format_tp_alert(coin: str, side: str, order_id: str, tp_level: str, price: float, 
                     entry: float, pnl_pct: float, r_mult: float) -> str:
    """🎯 止盈通知 (精簡版)"""
    direction = "做多 🟢" if side == "LONG" else "做空 🔴"
    return (
        f"🎉 *{coin} {tp_level} 達標！*\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"💰 獲利：`+{pnl_pct:.1f}%` (`{r_mult}R`)\n"
        f"💡 建議{'平倉 1/3' if tp_level=='TP1' else '平倉 1/3' if tp_level=='TP2' else '全部平倉'}"
    )

def _format_sl_alert(coin: str, side: str, order_id: str, price: float, entry: float, 
                     pnl_pct: float, is_be: bool = False) -> str:
    """🛑 止損通知 (精簡版)"""
    label = "🛡 保本出場" if is_be else "🛑 止損離場"
    return (
        f"{label} *{coin}*\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"💰 結果：`{pnl_pct:+.1f}%`\n"
        f"{'💡 資金安全，等待下一次' if is_be else '⚠️ 遵守風控，勿攤平'}"
    )

def _format_order_detail(coin: str, side: str, order_id: str, price: float, entry: float,
                         sl: float, tp1: float, tp2: float, tp3: float,
                         hit_tp1: bool, hit_tp2: bool, hit_tp3: bool) -> str:
    """📊 訂單詳情 (點擊按鈕回覆)"""
    pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
    direction = "做多 🟢" if side == "LONG" else "做空 🔴"
    
    status_parts = []
    if hit_tp3: status_parts.append("✅ TP3")
    elif hit_tp2: status_parts.append("✅ TP2")
    elif hit_tp1: status_parts.append("✅ TP1")
    else: status_parts.append("⏳ 持倉中")
    
    return (
        f"📊 *訂單詳情*\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"📊 {coin} {direction}\n"
        f"💰 當前：`{price:.4f}` ({pnl:+.1f}%)\n"
        f"📌 進場：`{entry:.4f}`\n"
        f"🛑 止損：`{sl:.4f}`\n"
        f"🎯 目標：{', '.join(status_parts)}\n"
        f"🎯 TP1：`{tp1:.4f}`\n"
        f"🎯 TP2：`{tp2:.4f}`\n"
        f"🎯 TP3：`{tp3:.4f}`"
    )

# ─────────────────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:
            return price
    
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}", timeout=2).json()
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
        res = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}", timeout=3).json()
        if res.get("code") != "0": return None
        data = res.get("data", [])
        if len(data) < 30: return None
        confirmed = [row for row in data if row[8] == "1"][::-1]
        return [{"c": float(row[4]), "h": float(row[2]), "l": float(row[3])} for row in confirmed]
    except:
        return None

# ─────────────────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────────────────
def calc_atr(df, period: int = 14) -> float:
    if len(df) < period + 1: return 0.001
    tr_values = []
    for i in range(1, len(df)):
        tr = max(df[i]["h"] - df[i]["l"], abs(df[i]["h"] - df[i-1]["c"]), abs(df[i]["l"] - df[i-1]["c"]))
        tr_values.append(tr)
    return sum(tr_values[-period:]) / period if len(tr_values) >= period else 0.001

def calc_supertrend(df, period: int = 10, mult: float = 3.0) -> int:
    if len(df) < period + 2: return 0
    atr = calc_atr(df, period)
    mid_price = sum(row["c"] for row in df[-20:]) / 20
    current = df[-1]["c"]
    if current > mid_price + atr * 0.5: return 1
    if current < mid_price - atr * 0.5: return -1
    return 0

def calc_rsi(df, period: int = 14) -> float:
    if len(df) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        chg = df[i]["c"] - df[i-1]["c"]
        if chg > 0: gains.append(chg); losses.append(0)
        else: gains.append(0); losses.append(-chg)
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0: return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))

def calc_score(df, side: str) -> tuple:
    score = 0
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1): score += 60
    elif st == 0: score += 30
    
    rsi = calc_rsi(df)
    if side == "LONG": score += 40 if 30 <= rsi <= 50 else (20 if 50 < rsi < 70 else 0)
    else: score += 40 if 50 <= rsi <= 70 else (20 if 30 < rsi < 50 else 0)
    
    return score, "A+" if score >= 85 else "A" if score >= 70 else "B+"

# ─────────────────────────────────────────────────────────
# 5. SignalTracker 類（核心升級）
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals = self._load()
        self.transitions = 0
    
    def _load(self) -> dict:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f: return json.load(f)
        except: pass
        return {}
    
    def _save(self):
        try:
            temp = self.filepath + ".tmp"
            with open(temp, "w") as f: json.dump(self.signals, f, indent=2)
            os.replace(temp, self.filepath)
        except: pass
    
    def add(self, signal: dict, active: bool = False, msg_id: int = None) -> str:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "entry_msg_id": msg_id,  # 🔹 儲存 Telegram 訊息 ID
            "activated_at": time.time() if active else None,
        }
        self._save()
        return key
    
    def update_signal(self, key: str, **kwargs):
        if key in self.signals:
            self.signals[key].update(kwargs)
            self._save()
    
    def remove(self, key: str):
        if key in self.signals:
            del self.signals[key]
            self._save()
    
    def check_all(self):
        self.transitions = 0
        to_remove = []
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig): to_remove.append(key)
        for key in to_remove: self.remove(key)
    
    def _check_one(self, key: str, sig: dict) -> bool:
        try:
            price = fetch_price(sig["instId"])
            if price <= 0: return False
            
            coin = sig["instId"].split("-")[0]
            order_id = sig.get("order_id", "N/A")
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            
            # 獲取進場訊息 ID 用於回覆
            reply_id = sig.get("entry_msg_id")
            
            # PENDING: 等待進場
            if status == "PENDING":
                if time.time() > sig["expires"]:
                    send_tg(f"⏰ *{coin} 訊號過期*\n🆔 `{order_id}`", reply_to_id=reply_id)
                    self.transitions += 1
                    return True
                
                in_zone = (
                    (side == "LONG" and entry*(1-0.006) <= price <= entry*(1+0.002)) or
                    (side == "SHORT" and entry*(1-0.002) <= price <= entry*(1+0.006))
                )
                if in_zone:
                    sig["status"] = "ACTIVE"
                    sig["activated_at"] = time.time()
                    # 🔹 發送進場通知並獲取 Message ID
                    msg = _format_entry_alert(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, sig["score"])
                    new_msg_id = send_tg(msg, reply_to_id=reply_id, buttons=_get_order_button(order_id))
                    if new_msg_id:
                        sig["entry_msg_id"] = new_msg_id  # 🔹 更新最新訊息 ID
                        self._save()
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"): return False
            
            def _dev(t): return abs(price - t) / t * 100
            
            # 🔴 嚴格 SL 觸發
            sl_triggered = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_triggered or (_dev(sl) > 0.003):
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(_format_sl_alert(coin, side, order_id, price, entry, pnl, is_be), 
                        reply_to_id=reply_id, buttons=_get_order_button(order_id))
                _record_trade(coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            # 🏆 TP3
            tp3_triggered = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if (tp3_triggered or _dev(tp3) > 0.003) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                send_tg(_format_tp_alert(coin, side, order_id, "TP3", tp3, entry, pnl, 4.0), 
                        reply_to_id=reply_id, buttons=_get_order_button(order_id))
                _record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # 🥈 TP2
            tp2_triggered = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if (tp2_triggered or _dev(tp2) > 0.003) and not sig.get("hit_tp2"):
                sig["hit_tp2"] = True
                sig["sl"] = tp1
                sig["status"] = "TRAIL"
                self._save()
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                send_tg(_format_tp_alert(coin, side, order_id, "TP2", tp2, entry, pnl, 2.5), 
                        reply_to_id=reply_id, buttons=_get_order_button(order_id))
                _record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # 🥇 TP1
            tp1_triggered = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if (tp1_triggered or _dev(tp1) > 0.003) and not sig.get("hit_tp1"):
                sig["hit_tp1"] = True
                sig["sl"] = entry
                sig["status"] = "BE"
                self._save()
                send_tg(_format_tp_alert(coin, side, order_id, "TP1", tp1, entry, 0.0, 1.0), 
                        reply_to_id=reply_id, buttons=_get_order_button(order_id))
                _record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 錯誤: {e}")
            return False
    
    def query_order(self, order_id: str):
        """🔍 查詢訂單並回覆"""
        found = None
        for key, sig in self.signals.items():
            if sig.get("order_id") == order_id:
                found = sig
                break
        
        if found:
            price = fetch_price(found["instId"])
            coin = found["instId"].split("-")[0]
            msg = _format_order_detail(coin, found["side"], order_id, price, found["entry"],
                                       found["sl"], found["tp1"], found["tp2"], found["tp3"],
                                       found.get("hit_tp1", False), found.get("hit_tp2", False), found.get("hit_tp3", False))
            send_tg(msg, reply_to_id=found.get("entry_msg_id"), buttons=_get_order_button(order_id))
        else:
            send_tg(f"❌ 找不到訂單 `{order_id}`")

def _record_trade(coin: str, side: str, order_id: str, entry: float, close_price: float, 
                  close_type: str, score: int):
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    trade = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "order_id": order_id, "coin": coin, "side": side,
        "entry": entry, "close": close_price, "close_type": close_type,
        "pnl": round(pnl, 2), "is_win": is_win, "score": score,
    }
    try:
        history = []
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r") as f: history = json.load(f)
        history.append(trade)
        with open(TRADE_HISTORY_FILE, "w") as f: json.dump(history, f, indent=2)
    except: pass

# ─────────────────────────────────────────────────────────
# 6. 主掃描邏輯
# ─────────────────────────────────────────────────────────
def run_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 開始掃描...")
    sent = 0
    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS: break
        key_prefix = f"{instId}_ALL"
        if key_prefix in _signal_cooldown and time.time() - _signal_cooldown[key_prefix] < 2 * 3600: continue
        
        try:
            current_price = fetch_price(instId)
            if current_price <= 0: continue
            
            df = fetch_candles(instId)
            if not df: continue
            
            signal = None
            for side in ["LONG", "SHORT"]:
                score, grade = calc_score(df, side)
                if score >= SCORE_THRESHOLD:
                    atr = calc_atr(df)
                    entry = current_price
                    sl = entry - atr*1.5 if side == "LONG" else entry + atr*1.5
                    risk = abs(entry - sl)
                    signal = {
                        "instId": instId, "side": side, "tf": "15m",
                        "entry": round(entry, 4), "sl": round(sl, 4),
                        "tp1": round(entry + risk if side=="LONG" else entry - risk, 4),
                        "tp2": round(entry + risk*2.5 if side=="LONG" else entry - risk*2.5, 4),
                        "tp3": round(entry + risk*4.0 if side=="LONG" else entry - risk*4.0, 4),
                        "score": score, "grade": grade, "created": time.time(),
                        "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
                    }
                    break
            
            if signal:
                _signal_cooldown[key_prefix] = time.time()
                # 🔹 先發送訊息獲取 ID，再添加到追蹤
                msg = _format_entry_alert(instId.split("-")[0], signal["side"], "PENDING", 
                                          current_price, signal["entry"], signal["sl"], 
                                          signal["tp1"], signal["tp2"], signal["tp3"], signal["score"])
                # 先發送一個暫時的訊息，獲取 ID 後再更新
                # 實際上為了簡化，我們直接 add，然後在 check_all 裡發送正式通知
                # 但為了實現 "回覆" 效果，最好先有 "進場" 訊息
                # 這裡採用策略：生成訊號 -> 發送進場通知 -> 將訊息 ID 存入 SignalTracker
                
                msg_id = send_tg(msg)
                tracker.add(signal, active=True, msg_id=msg_id)
                sent += 1
        except: continue
    
    tracker.check_all()
    logging.info(f"✅ 掃描完成，發送 {sent} 筆訊號")
    return sent

# ─────────────────────────────────────────────────────────
# 7. 主函式
# ─────────────────────────────────────────────────────────
def main():
    try:
        logging.info("=" * 40)
        logging.info("🤖 Alpha Oracle Pro v10.9 線程回覆版啟動")
        logging.info("=" * 40)
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        
        # 🔹 處理 /stats 命令
        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd == "/stats":
                # 簡單顯示追蹤中數量
                count = len([s for s in tracker.signals.values() if s["status"] not in ("CLOSED",)])
                send_tg(f"📊 目前追蹤中：{count} 筆訂單")
                return
            elif cmd.startswith("order_"):
                order_id = cmd.split("order_")[1]
                tracker.query_order(order_id)
                return
        
        run_scan(tracker)
        logging.info("🎉 程式執行完成")
    except Exception as e:
        logging.error(f"🔥 系統錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
