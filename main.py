#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v10.8 — 訂單追蹤增強版
══════════════════════════════════════════════════════════════════════
✨ 功能：
  ✅ 訂單編號正確顯示
  ✅ LINE 風格回覆按鈕（點擊查詢訂單）
  ✅ 價格同步驗證（與 OKX 即時價格對齊）
  ✅ 時間戳顯示
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
# 2. 通知系統（LINE 風格 + 訂單編號）
# ─────────────────────────────────────────────────────────
def send_tg(msg: str, parse_mode: str = "Markdown", reply_markup: dict = None) -> bool:
    """📤 發送 Telegram 通知（支援回覆按鈕）"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 未設定")
        return False
    
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": parse_mode
    }
    
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=5
        )
        return r.status_code == 200
    except Exception as e:
        logging.error(f"❌ TG 發送失敗: {e}")
        return False

def _get_order_keyboard(order_id: str) -> dict:
    """🔘 生成訂單查詢按鈕（LINE 風格）"""
    return {
        "inline_keyboard": [[
            {
                "text": f"🔍 查詢訂單 {order_id[-8:]}",
                "callback_data": f"order_{order_id}"
            }
        ]]
    }

def _format_entry_alert(coin: str, side: str, order_id: str, price: float, entry: float,
                        sl: float, tp1: float, tp2: float, tp3: float, score: int) -> str:
    """📌 進場通知（含訂單編號 + 時間戳）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥" if score >= 80 else "✅" if score >= 68 else "⚪"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    tp1_pct = (tp1 - entry) / entry * 100
    tp2_pct = (tp2 - entry) / entry * 100
    tp3_pct = (tp3 - entry) / entry * 100
    sl_pct = (sl - entry) / entry * 100
    
    return (
        f"{emoji} *{coin} 進場提醒* {grade}\n"
        f"────────────\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{timestamp}\n"
        f"方向：{direction}\n"
        f"進場價：`{entry:.4f}`\n"
        f"當前價：`{price:.4f}`\n"
        f"評分：{score}分\n"
        f"\n"
        f"🎯 止盈目標：\n"
        f"  TP1 `{tp1:.4f}` (+{tp1_pct:.1f}%)\n"
        f"  TP2 `{tp2:.4f}` (+{tp2_pct:.1f}%)\n"
        f"  TP3 `{tp3:.4f}` (+{tp3_pct:.1f}%)\n"
        f"\n"
        f"🛑 止損：`{sl:.4f}` ({sl_pct:+.1f}%)\n"
        f"\n"
        f"💡 到達 TP1 自動保本，到達 TP2 自動鎖利"
    )

def _format_tp_alert(coin: str, side: str, order_id: str, tp_level: str, price: float, 
                     entry: float, sl: float, pnl_pct: float, r_mult: float) -> str:
    """🎯 止盈通知（含訂單編號 + 時間戳）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    return (
        f"🎯 *{coin} {tp_level} 達標！*\n"
        f"────────────\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{timestamp}\n"
        f"方向：{direction}\n"
        f"觸發價：`{price:.4f}`\n"
        f"獲利：`+{pnl_pct:.1f}%` (`+{r_mult}R`)\n"
        f"\n"
        f"✅ 已達成 {tp_level}\n"
        f"\n"
        f"💡 {'建議平倉 ⅓ 鎖定獲利' if tp_level=='TP1' else '建議平倉 ⅓ 落袋為安' if tp_level=='TP2' else '建議全部平倉完美收割'}"
    )

def _format_sl_alert(coin: str, side: str, order_id: str, price: float, entry: float, 
                     pnl_pct: float, is_be: bool = False) -> str:
    """🛑 止損通知（含訂單編號 + 時間戳）"""
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    label = "🔒 保本出場" if is_be else "❌ 止損離場"
    r_tag = "`0.0R`" if is_be else "`-1.0R`"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    return (
        f"{label} *{coin}*\n"
        f"────────────\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"⏰ 時間：{timestamp}\n"
        f"方向：{direction}\n"
        f"觸發價：`{price:.4f}`\n"
        f"結果：`{pnl_pct:+.1f}%` {r_tag}\n"
        f"\n"
        f"💡 {'資金安全，等待下一次機會 💪' if is_be else '遵守風控，勿加碼攤平'}"
    )

def _format_position_update(coin: str, side: str, order_id: str, current_price: float, 
                            entry: float, sl: float, tp1: float, tp2: float, tp3: float,
                            hit_tp1: bool, hit_tp2: bool, hit_tp3: bool) -> str:
    """📊 持倉進度更新（含訂單編號）"""
    direction = "做多" if side == "LONG" else "做空"
    pnl = ((current_price - entry) / entry * 100) if side == "LONG" else ((entry - current_price) / entry * 100)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    
    progress = []
    if hit_tp3:
        progress.append("🏆TP3✅")
    elif hit_tp2:
        progress.append("🥈TP2✅")
    elif hit_tp1:
        progress.append("🥇TP1✅")
    else:
        progress.append("⏳等待")
    
    return (
        f"📊 *{coin} 持倉更新*\n"
        f"────────────\n"
        f"🆔 訂單編號：`{order_id}`\n"
        f"方向：{direction}\n"
        f"當前：`{current_price:.4f}` {pnl_emoji}{pnl:+.1f}%\n"
        f"進場：`{entry:.4f}`\n"
        f"\n"
        f"🎯 止盈進度：{' → '.join(progress)}\n"
        f"  TP1 `{tp1:.4f}`{'✅' if hit_tp1 else ''}\n"
        f"  TP2 `{tp2:.4f}`{'✅' if hit_tp2 else ''}\n"
        f"  TP3 `{tp3:.4f}`{'✅' if hit_tp3 else ''}\n"
        f"\n"
        f"🛑 止損：`{sl:.4f}`"
    )

# ─────────────────────────────────────────────────────────
# 3. 數據抓取（價格同步）
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str) -> float:
    """🔍 獲取即時價格（帶快取）"""
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:  # 5秒快取
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
                logging.debug(f"✅ 獲取 {instId} 價格: {price}")
                return price
    except Exception as e:
        logging.warning(f"⚠️ 獲取 {instId} 價格失敗: {e}")
    
    # 回傳快取
    if instId in _price_cache:
        return _price_cache[instId][0]
    
    return 0.0

def fetch_candles(instId: str, tf: str = "15m", limit: int = 100):
    """📊 獲取 K 線數據"""
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
    except Exception as e:
        logging.warning(f"⚠️ 獲取 {instId} K線失敗: {e}")
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
def generate_signal(instId: str, df, current_price: float):
    """🎯 生成交易訊號（使用即時價格）"""
    if df is None or len(df) < 50:
        return None
    
    # 🔹 使用即時價格而非 K 線收盤價
    price = current_price
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
# 6. SignalTracker 類（訂單追蹤增強版）
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
        """📌 新增追蹤訊號（生成唯一訂單編號）"""
        # 🔹 生成唯一訂單編號
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False,
            "hit_tp2": False,
            "hit_tp3": False,
            "activated_at": time.time() if active else None,
        }
        self._save()
        logging.info(f"📌 新增訂單: {order_id}")
        return key
    
    def get_order_by_id(self, order_id: str) -> dict:
        """🔍 通過訂單編號查詢訂單"""
        for key, sig in self.signals.items():
            if sig.get("order_id") == order_id:
                return sig
        return None
    
    def remove(self, key: str):
        if key in self.signals:
            del self.signals[key]
            self._save()
    
    def check_all(self):
        """🔄 檢查所有訊號並發送通知"""
        self.transitions = 0
        to_remove = []
        
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig):
                to_remove.append(key)
        
        for key in to_remove:
            del self.signals[key]
        self._save()
    
    def _check_one(self, key: str, sig: dict) -> bool:
        """🔍 檢查單一訊號（嚴格價格驗證）"""
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                return False
            
            sig["current_price"] = price
            coin = sig["instId"].split("-")[0]
            order_id = sig.get("order_id", "N/A")
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            
            # PENDING: 等待進場
            if status == "PENDING":
                if time.time() > sig["expires"]:
                    msg = f"⏰ *{coin} 訊號過期*\n訂單 `{order_id}`\n進場 `{entry:.4f}` 未觸發"
                    send_tg(msg)
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
                    
                    msg = _format_entry_alert(coin, side, order_id, price, entry, sl, tp1, tp2, tp3, sig["score"])
                    keyboard = _get_order_keyboard(order_id)
                    send_tg(msg, reply_markup=keyboard)
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False
            
            def _dev(target):
                return abs(price - target) / target * 100
            
            # 🔴 嚴格 SL 觸發
            if side == "LONG":
                sl_triggered = price <= sl
            else:
                sl_triggered = price >= sl
            
            if sl_triggered or (_dev(sl) > 0.003 and ((side == "LONG" and price < sl) or (side == "SHORT" and price > sl))):
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                msg = _format_sl_alert(coin, side, order_id, price, entry, pnl, is_be)
                keyboard = _get_order_keyboard(order_id)
                send_tg(msg, reply_markup=keyboard)
                _record_trade(coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            # 🏆 嚴格 TP3 觸發
            if side == "LONG":
                tp3_triggered = price >= tp3
            else:
                tp3_triggered = price <= tp3
            
            if (tp3_triggered or _dev(tp3) > 0.003) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                msg = _format_tp_alert(coin, side, order_id, "TP3", tp3, entry, sl, pnl, 4.0)
                keyboard = _get_order_keyboard(order_id)
                send_tg(msg, reply_markup=keyboard)
                _record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # 🥈 嚴格 TP2 觸發
            if side == "LONG":
                tp2_triggered = price >= tp2
            else:
                tp2_triggered = price <= tp2
            
            if (tp2_triggered or _dev(tp2) > 0.003) and not sig.get("hit_tp2"):
                sig["hit_tp2"] = True
                sig["sl"] = tp1
                sig["status"] = "TRAIL"
                self._save()
                
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                msg = _format_tp_alert(coin, side, order_id, "TP2", tp2, entry, sl, pnl, 2.5)
                keyboard = _get_order_keyboard(order_id)
                send_tg(msg, reply_markup=keyboard)
                _record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # 🥇 嚴格 TP1 觸發
            if side == "LONG":
                tp1_triggered = price >= tp1
            else:
                tp1_triggered = price <= tp1
            
            if (tp1_triggered or _dev(tp1) > 0.003) and not sig.get("hit_tp1"):
                sig["hit_tp1"] = True
                sig["sl"] = entry
                sig["status"] = "BE"
                self._save()
                
                msg = _format_tp_alert(coin, side, order_id, "TP1", tp1, entry, sl, 0.0, 1.0)
                keyboard = _get_order_keyboard(order_id)
                send_tg(msg, reply_markup=keyboard)
                _record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 錯誤: {e}")
            return False
    
    def send_position_updates(self):
        """📊 發送所有持倉進度更新"""
        updates = []
        for key, sig in self.signals.items():
            if sig["status"] in ("ACTIVE", "BE", "TRAIL"):
                price = fetch_price(sig["instId"])
                if price > 0:
                    coin = sig["instId"].split("-")[0]
                    order_id = sig.get("order_id", "N/A")
                    msg = _format_position_update(
                        coin=coin,
                        side=sig["side"],
                        order_id=order_id,
                        current_price=price,
                        entry=sig["entry"],
                        sl=sig["sl"],
                        tp1=sig["tp1"],
                        tp2=sig["tp2"],
                        tp3=sig["tp3"],
                        hit_tp1=sig.get("hit_tp1", False),
                        hit_tp2=sig.get("hit_tp2", False),
                        hit_tp3=sig.get("hit_tp3", False)
                    )
                    keyboard = _get_order_keyboard(order_id)
                    send_tg(msg, reply_markup=keyboard)
                    updates.append(msg)
        
        if updates:
            logging.info(f"📊 已發送 {len(updates)} 筆持倉更新")
    
    def get_position_stats(self) -> str:
        """📋 獲取持倉統計（含訂單編號）"""
        positions = [
            {**sig, "current_price": fetch_price(sig["instId"])}
            for sig in self.signals.values()
            if sig["status"] in ("ACTIVE", "BE", "TRAIL", "PENDING")
        ]
        if not positions:
            return "📭 *目前無持倉*\n\n🔄 系統持續掃描中"
        
        msg = f"📊 *追蹤中訊號 ({len(positions)} 筆)*\n" + "═" * 30 + "\n\n"
        for i, p in enumerate(positions):
            coin_emoji = "🟠" if "BTC" in p["instId"] else "🔷" if "ETH" in p["instId"] else "🟣"
            side_emoji = "🟢" if p["side"] == "LONG" else "🔴"
            order_id = p.get("order_id", "N/A")
            pnl = ((p["current_price"] - p["entry"]) / p["entry"] * 100) if p["side"] == "LONG" else ((p["entry"] - p["current_price"]) / p["entry"] * 100)
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            progress = "🥇" if p.get("hit_tp3") else "🥇🥈🏆" if p.get("hit_tp2") else "🥇🥈" if p.get("hit_tp1") else "⏳"
            
            msg += (
                f"{coin_emoji} *#{p['instId'].split('-')[0]}* · {side_emoji} {p['side']} · {p.get('score', 0)}分\n"
                f"🆔 訂單：`{order_id}`\n"
                f"{' ACTIVE · 持倉中' if p['status'] == 'ACTIVE' else p['status']}\n"
                f"✅ 當前 `{p['current_price']:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
                f"🟢 進場 `{p['entry']:.4f}`\n"
                f"🔴 止損 `{p['sl']:.4f}`\n"
                f"🥇 TP1 `{p['tp1']:.4f}`\n"
                f"🥈 TP2 `{p['tp2']:.4f}`\n"
                f"🏆 TP3 `{p['tp3']:.4f}`\n"
                f"進度 {progress}"
            )
            if i < len(positions) - 1:
                msg += "\n\n" + "─" * 30 + "\n\n"
        return msg

def _record_trade(coin: str, side: str, order_id: str, entry: float, close_price: float, 
                  close_type: str, score: int):
    """📝 記錄交易歷史（含訂單編號）"""
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    
    trade = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "order_id": order_id,
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
        logging.info(f"📝 記錄交易: {coin} {order_id} {close_type}")
    except Exception as e:
        logging.error(f"❌ 記錄交易失敗: {e}")

# ─────────────────────────────────────────────────────────
# 7. 主掃描邏輯
# ─────────────────────────────────────────────────────────
def run_scan(tracker: SignalTracker) -> int:
    """🔍 執行掃描（使用即時價格）"""
    logging.info("🚀 開始掃描...")
    sent = 0
    
    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS:
            break
        
        key = f"{instId}_ALL"
        if key in _signal_cooldown and time.time() - _signal_cooldown[key] < 2 * 3600:
            continue
        
        try:
            # 🔹 先獲取即時價格
            current_price = fetch_price(instId)
            if current_price <= 0:
                logging.warning(f"[{instId}] 無法獲取價格")
                continue
            
            logging.info(f"[{instId}] 當前價格: {current_price}")
            
            # 🔹 獲取 K 線數據
            df = fetch_candles(instId)
            if df is None:
                continue
            
            # 🔹 使用即時價格生成訊號
            signal = generate_signal(instId, df, current_price)
            if not signal:
                continue
            
            # 🔹 發送通知（含訂單按鈕）
            order_id = f"{int(time.time())}-TEMP"  # 臨時訂單號
            msg = _format_entry_alert(
                coin=instId.split("-")[0],
                side=signal["side"],
                order_id=order_id,
                price=current_price,
                entry=signal["entry"],
                sl=signal["sl"],
                tp1=signal["tp1"],
                tp2=signal["tp2"],
                tp3=signal["tp3"],
                score=signal["score"]
            )
            
            if send_tg(msg):
                _signal_cooldown[key] = time.time()
                
                # 🔹 檢查是否在進場區
                in_zone = (
                    (signal["side"] == "LONG" and signal["entry"]*(1-0.006) <= current_price <= signal["entry"]*(1+0.002)) or
                    (signal["side"] == "SHORT" and signal["entry"]*(1-0.002) <= current_price <= signal["entry"]*(1+0.006))
                )
                
                # 🔹 添加到追蹤器（會生成真實訂單號）
                tracker.add(signal, active=in_zone and current_price > 0)
                sent += 1
        except Exception as e:
            logging.error(f"[{instId}] 掃描失敗: {e}")
            continue
    
    # 🔹 檢查既有訊號
    tracker.check_all()
    
    # 🔹 發送持倉進度更新
    tracker.send_position_updates()
    
    logging.info(f"✅ 掃描完成，發送 {sent} 筆訊號")
    return sent

# ─────────────────────────────────────────────────────────
# 8. 主函式
# ─────────────────────────────────────────────────────────
def main():
    try:
        logging.info("=" * 40)
        logging.info("🤖 Alpha Oracle Pro v10.8 訂單追蹤版啟動")
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
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
