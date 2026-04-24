#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v11.2 — 调试版
══════════════════════════════════════════════════════════════════════
✨ 功能：
  ✅ 显示详细扫描日志
  ✅ 显示为什么没发通知（条件不满足？）
  ✅ 测试 Telegram 连接
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
# 🔧 环境变数安全解析
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
# 1. 基础配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,  # 🔍 调试模式
    format="%(asctime)s - %(message)s",
    stream=sys.stdout
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

logging.info("=" * 60)
logging.info("🔍 环境变数检查：")
logging.info(f"  TG_TOKEN: {'✅ 已设置' if TG_TOKEN else '❌ 未设置'}")
logging.info(f"  CHAT_ID: {'✅ 已设置' if CHAT_ID else '❌ 未设置'}")
logging.info("=" * 60)

ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

SIGNAL_EXPIRE_HOURS = 24
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

_price_cache = {}
_signal_cooldown = {}

# ─────────────────────────────────────────────────────────
# 2. 测试 Telegram 连接
# ─────────────────────────────────────────────────────────
def test_telegram():
    """🧪 测试 Telegram 连接"""
    if not TG_TOKEN or not CHAT_ID:
        logging.error("❌ TG_TOKEN 或 CHAT_ID 未设置！")
        return False
    
    try:
        msg = "🧪 *Alpha Oracle Pro 测试*\n\n✅ Telegram 连接正常！\n\n请确认您能收到此消息。"
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
        if r.status_code == 200:
            logging.info("✅ Telegram 测试消息发送成功！")
            return True
        else:
            logging.error(f"❌ Telegram API 错误: {r.text}")
            return False
    except Exception as e:
        logging.error(f"❌ Telegram 连接失败: {e}")
        return False

def send_tg(msg: str, reply_to_id: int = None, buttons: list = None) -> int:
    if not TG_TOKEN or not CHAT_ID:
        logging.error("❌ 无法发送：TG_TOKEN 或 CHAT_ID 为空")
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
            logging.info("✅ Telegram 消息发送成功")
            return r.json().get("result", {}).get("message_id")
        else:
            logging.error(f"❌ Telegram API 错误: {r.text}")
    except Exception as e:
        logging.error(f"❌ 发送失败: {e}")
    return None

def _get_order_button(order_id: str) -> list:
    return [{"text": f"🔍 查询订单 {order_id[-8:]}", "callback_data": f"order_{order_id}"}]

def _format_entry_alert(coin: str, side: str, order_id: str, entry: float,
                        sl: float, tp1: float, tp2: float, tp3: float, 
                        score: int, signal_type: str) -> str:
    direction = "做多 🟢" if side == "LONG" else "做空 🔴"
    grade_emoji = "🔥" if score >= 80 else "✅" if score >= 68 else "⚪"
    sl_pct = (sl - entry) / entry * 100
    
    return (
        f"🚀 *{coin} 进场提醒* {grade_emoji}\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"📊 方向：{direction}\n"
        f"📌 进场：`{entry:.4f}`\n"
        f"🎯 信号：{signal_type}\n"
        f"📈 评分：{score}分\n"
        f"\n"
        f"🎯 TP1：`{tp1:.4f}` (+{(tp1-entry)/entry*100:.1f}%)\n"
        f"🎯 TP2：`{tp2:.4f}` (+{(tp2-entry)/entry*100:.1f}%)\n"
        f"🎯 TP3：`{tp3:.4f}` (+{(tp3-entry)/entry*100:.1f}%)\n"
        f"\n"
        f"🛑 止损：`{sl:.4f}` ({sl_pct:+.1f}%)\n"
        f"\n"
        f"💡 到达 TP1 自动保本，到达 TP2 自动锁利"
    )

def _format_tp_alert(coin: str, side: str, order_id: str, tp_level: str, price: float, 
                     entry: float, pnl_pct: float, r_mult: float) -> str:
    direction = "做多 🟢" if side == "LONG" else "做空 🔴"
    return (
        f"🎉 *{coin} {tp_level} 达标！*\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"📊 方向：{direction}\n"
        f"💰 获利：`+{pnl_pct:.1f}%` (`{r_mult}R`)\n"
        f"\n💡 建议{'平仓 1/3' if tp_level=='TP1' else '平仓 1/3' if tp_level=='TP2' else '全部平仓'}"
    )

def _format_sl_alert(coin: str, side: str, order_id: str, price: float, entry: float, 
                     pnl_pct: float, is_be: bool = False) -> str:
    direction = "做多 🟢" if side == "LONG" else "做空 🔴"
    label = "🛡 保本出场" if is_be else "🛑 止损离场"
    r_tag = "`0.0R`" if is_be else "`-1.0R`"
    
    return (
        f"{label} *{coin}*\n"
        f"──────────\n"
        f"🆔 `{order_id}`\n"
        f"📊 方向：{direction}\n"
        f"💰 结果：`{pnl_pct:+.1f}%` {r_tag}\n"
        f"\n💡 {'资金安全，等待下一次 💪' if is_be else '遵守风控，勿加码摊平'}"
    )

# ─────────────────────────────────────────────────────────
# 3. OKX 数据源
# ─────────────────────────────────────────────────────────
def fetch_okx_price(instId: str) -> float:
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

def fetch_okx_candles(instId: str, tf: str = "15m", limit: int = 100):
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}", timeout=3).json()
        if res.get("code") != "0": return None
        data = res.get("data", [])
        if len(data) < 50: return None
        confirmed = [row for row in data if row[8] == "1"][::-1]
        return [{"c": float(row[4]), "h": float(row[2]), "l": float(row[3]), "o": float(row[1])} for row in confirmed]
    except:
        return None

# ─────────────────────────────────────────────────────────
# 4. 高级技术分析（SMC/ICT/OB/FVG）
# ─────────────────────────────────────────────────────────
def find_order_blocks(df, lookback: int = 50):
    order_blocks = []
    limit_idx = max(0, len(df) - lookback)
    
    for i in range(len(df)-2, limit_idx, -1):
        if len(order_blocks) >= 3: break
        
        current = df[i]
        
        if current["c"] < current["o"]:
            is_valid = True
            for j in range(i+1, min(i+10, len(df))):
                if df[j]["l"] < current["l"]:
                    is_valid = False
                    break
            
            if is_valid:
                order_blocks.append({
                    "type": "bearish",
                    "high": current["h"],
                    "low": current["l"],
                    "index": i
                })
        
        elif current["c"] > current["o"]:
            is_valid = True
            for j in range(i+1, min(i+10, len(df))):
                if df[j]["h"] > current["h"]:
                    is_valid = False
                    break
            
            if is_valid:
                order_blocks.append({
                    "type": "bullish",
                    "high": current["h"],
                    "low": current["l"],
                    "index": i
                })
    
    return order_blocks

def find_fvg(df, lookback: int = 50):
    fvgs = []
    limit_idx = max(0, len(df) - lookback)
    
    for i in range(len(df)-2, limit_idx, -1):
        if len(fvgs) >= 3: break
        
        curr = df[i]
        prev2 = df[i+2]
        
        if curr["l"] > prev2["h"]:
            fvgs.append({
                "type": "bullish",
                "top": curr["l"],
                "bottom": prev2["h"],
                "index": i
            })
        elif curr["h"] < prev2["l"]:
            fvgs.append({
                "type": "bearish",
                "top": prev2["l"],
                "bottom": curr["h"],
                "index": i
            })
    
    return fvgs

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

# ─────────────────────────────────────────────────────────
# 5. 综合评分系统
# ─────────────────────────────────────────────────────────
def calc_advanced_score(df, side: str) -> tuple:
    score = 0
    signals = []
    current_price = df[-1]["c"]
    
    # 1. 基础趋势（30分）
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 30
        signals.append("趋势顺向 +30")
    elif st == 0:
        score += 15
        signals.append("震荡 +15")
    
    # 2. RSI 动量（25分）
    rsi = calc_rsi(df)
    if side == "LONG":
        if 30 <= rsi <= 50:
            score += 25
            signals.append(f"RSI 低档 {rsi:.1f} +25")
        elif 50 < rsi < 70:
            score += 15
            signals.append(f"RSI 中性 {rsi:.1f} +15")
    else:
        if 50 <= rsi <= 70:
            score += 25
            signals.append(f"RSI 高档 {rsi:.1f} +25")
        elif 30 < rsi < 50:
            score += 15
            signals.append(f"RSI 中性 {rsi:.1f} +15")
    
    # 3. 订单块 OB（25分）
    obs = find_order_blocks(df)
    in_ob = False
    ob_type = None
    
    for ob in obs:
        if ob["low"] * 0.999 <= current_price <= ob["high"] * 1.001:
            if ob["type"] == "bullish" and side == "LONG":
                in_ob = True
                ob_type = "bullish"
                score += 25
                signals.append("在看涨 OB +25")
                break
            elif ob["type"] == "bearish" and side == "SHORT":
                in_ob = True
                ob_type = "bearish"
                score += 25
                signals.append("在看跌 OB +25")
                break
    
    # 4. FVG（15分）
    fvgs = find_fvg(df)
    in_fvg = False
    
    for fvg in fvgs:
        if fvg["bottom"] * 0.999 <= current_price <= fvg["top"] * 1.001:
            if fvg["type"] == "bullish" and side == "LONG":
                in_fvg = True
                score += 15
                signals.append("在 bullish FVG +15")
                break
            elif fvg["type"] == "bearish" and side == "SHORT":
                in_fvg = True
                score += 15
                signals.append("在 bearish FVG +15")
                break
    
    # 确定信号类型
    if in_ob and in_fvg:
        signal_type = "OB + FVG 共振 🔥"
    elif in_ob:
        signal_type = f"{'看涨' if ob_type == 'bullish' else '看跌'} OB"
    elif in_fvg:
        signal_type = "FVG 回补"
    else:
        signal_type = "技术面"
    
    grade = "A+ 极强 🔥" if score >= 85 else "A 强力 ⭐" if score >= 70 else "B+ 观望 ✅"
    
    return score, grade, signal_type, rsi, st, in_ob, in_fvg

# ─────────────────────────────────────────────────────────
# 6. SignalTracker 类
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
            "entry_msg_id": msg_id,
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
            if self._check_one(key, sig): to_remove.append(key)
        for key in to_remove: self.remove(key)
    
    def _check_one(self, key: str, sig: dict) -> bool:
        try:
            price = fetch_okx_price(sig["instId"])
            if price <= 0: return False
            
            coin = sig["instId"].split("-")[0]
            order_id = sig.get("order_id", "N/A")
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            
            reply_id = sig.get("entry_msg_id")
            
            if status == "PENDING":
                if time.time() > sig["expires"]:
                    send_tg(f"⏰ *{coin} 讯号过期*\n🆔 `{order_id}`", reply_to_id=reply_id)
                    self.transitions += 1
                    return True
                
                in_zone = (
                    (side == "LONG" and entry*(1-0.006) <= price <= entry*(1+0.002)) or
                    (side == "SHORT" and entry*(1-0.002) <= price <= entry*(1+0.006))
                )
                if in_zone:
                    sig["status"] = "ACTIVE"
                    sig["activated_at"] = time.time()
                    msg = _format_entry_alert(coin, side, order_id, entry, sl, tp1, tp2, tp3, 
                                            sig["score"], sig.get("signal_type", "OKX"))
                    new_msg_id = send_tg(msg, reply_to_id=reply_id, buttons=_get_order_button(order_id))
                    if new_msg_id:
                        sig["entry_msg_id"] = new_msg_id
                        self._save()
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"): return False
            
            def _dev(t): return abs(price - t) / t * 100
            
            sl_triggered = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_triggered or (_dev(sl) > 0.003):
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(_format_sl_alert(coin, side, order_id, price, entry, pnl, is_be), 
                        reply_to_id=reply_id, buttons=_get_order_button(order_id))
                _record_trade(coin, side, order_id, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            tp3_triggered = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if (tp3_triggered or _dev(tp3) > 0.003) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                send_tg(_format_tp_alert(coin, side, order_id, "TP3", tp3, entry, pnl, 4.0), 
                        reply_to_id=reply_id, buttons=_get_order_button(order_id))
                _record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
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
            logging.error(f"❌ check_one [{key}] 错误: {e}")
            return False

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
# 7. OKX 主动扫描（调试版）
# ─────────────────────────────────────────────────────────
def run_okx_scan(tracker: SignalTracker) -> int:
    logging.info("🚀 OKX 开始扫描（SMC/ICT 分析）...")
    sent = 0
    skipped_no_ob_fvg = 0
    
    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS: break
        key_prefix = f"{instId}_ALL"
        if key_prefix in _signal_cooldown and time.time() - _signal_cooldown[key_prefix] < 2 * 3600: 
            logging.info(f"⏭️  {instId} 冷却中，跳过")
            continue
        
        try:
            current_price = fetch_okx_price(instId)
            if current_price <= 0: 
                logging.warning(f"⚠️  {instId} 无法获取价格")
                continue
            
            df = fetch_okx_candles(instId)
            if not df: 
                logging.warning(f"⚠️  {instId} 无法获取 K 线")
                continue
            
            logging.info(f"\n📊 分析 {instId} @ {current_price}...")
            
            best_signal = None
            best_score = 0
            
            for side in ["LONG", "SHORT"]:
                score, grade, signal_type, rsi, st, in_ob, in_fvg = calc_advanced_score(df, side)
                
                logging.info(f"  {side}: 评分={score}, 信号={signal_type}, RSI={rsi:.1f}, OB={in_ob}, FVG={in_fvg}")
                
                # 🔴 核心逻辑：只在 OB 或 FVG 区域进单
                if score >= SCORE_THRESHOLD and ("OB" in signal_type or "FVG" in signal_type):
                    if score > best_score:
                        best_score = score
                        atr = calc_atr(df)
                        entry = current_price
                        sl = entry - atr*1.5 if side == "LONG" else entry + atr*1.5
                        risk = abs(entry - sl)
                        
                        best_signal = {
                            "instId": instId, "side": side, "tf": "15m",
                            "entry": round(entry, 4), "sl": round(sl, 4),
                            "tp1": round(entry + risk if side=="LONG" else entry - risk, 4),
                            "tp2": round(entry + risk*2.5 if side=="LONG" else entry - risk*2.5, 4),
                            "tp3": round(entry + risk*4.0 if side=="LONG" else entry - risk*4.0, 4),
                            "score": score, "grade": grade, "signal_type": signal_type,
                            "created": time.time(),
                            "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
                            "source": "OKX"
                        }
                else:
                    skipped_no_ob_fvg += 1
                    reason = []
                    if score < SCORE_THRESHOLD:
                        reason.append(f"评分不足 ({score}<{SCORE_THRESHOLD})")
                    if not in_ob and not in_fvg:
                        reason.append("不在 OB/FVG 区域")
                    logging.info(f"    ❌ 跳过: {', '.join(reason)}")
            
            if best_signal:
                _signal_cooldown[key_prefix] = time.time()
                msg = _format_entry_alert(instId.split("-")[0], best_signal["side"], "PENDING", 
                                          best_signal["entry"], best_signal["sl"], 
                                          best_signal["tp1"], best_signal["tp2"], best_signal["tp3"], 
                                          best_signal["score"], best_signal["signal_type"])
                msg_id = send_tg(msg)
                if msg_id:
                    tracker.add(best_signal, active=True, msg_id=msg_id)
                    sent += 1
                    logging.info(f"✅ {instId} {best_signal['side']} {best_signal['signal_type']} 评分:{best_score} - 已发送通知")
            else:
                logging.info(f"  ⚠️  {instId} 无符合条件的信号")
        
        except Exception as e:
            logging.error(f"[{instId}] 扫描失败: {e}")
            continue
    
    logging.info(f"\n{'='*60}")
    logging.info(f"📊 扫描总结：")
    logging.info(f"  发送信号：{sent} 笔")
    logging.info(f"  跳过（无 OB/FVG）：{skipped_no_ob_fvg} 次")
    logging.info(f"{'='*60}")
    
    tracker.check_all()
    return sent

# ─────────────────────────────────────────────────────────
# 8. 主函数
# ─────────────────────────────────────────────────────────
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "okx"
    
    try:
        # 🔍 先测试 Telegram 连接
        if mode == "test":
            if test_telegram():
                logging.info("✅ Telegram 测试成功！请检查您的 Telegram。")
            else:
                logging.error("❌ Telegram 测试失败！请检查 TG_TOKEN 和 CHAT_ID。")
            return
        
        logging.info("=" * 60)
        logging.info("🤖 Alpha Oracle Pro v11.2 调试版启动")
        logging.info("=" * 60)
        
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        
        if mode == "okx":
            run_okx_scan(tracker)
        elif mode == "check":
            tracker.check_all()
        else:
            run_okx_scan(tracker)
    
    except Exception as e:
        logging.error(f"🔥 系统错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
