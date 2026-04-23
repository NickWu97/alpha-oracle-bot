#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v10.4 — 调试版 (修复 Exit Code 1)
══════════════════════════════════════════════════════════════════════
🔧 修复：添加详细错误日志 + 更健壮的错误处理
✨ 功能：精致通知格式 + 极速执行
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import logging
import time
import threading
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────
# 🔧 环境变数安全解析 (关键修复)
# ─────────────────────────────────────────────────────────
def _get_env(key, default=""):
    """安全获取环境变数，空值时回传预设值"""
    val = os.getenv(key)
    if val is None:
        logging.warning(f"⚠️ 环境变数 {key} 未设置，使用预设值")
        return default
    val = val.strip()
    if not val:
        logging.warning(f"⚠️ 环境变数 {key} 为空，使用预设值")
        return default
    return val

def _get_env_int(key, default):
    """安全获取整数环境变数"""
    val = os.getenv(key)
    if val is None:
        return default
    val = val.strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        logging.warning(f"⚠️ 环境变数 {key} 解析失败，使用预设值 {default}")
        return default

# ─────────────────────────────────────────────────────────
# 1. 基础配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,  # 🔍 调试模式：输出更详细日志
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),  # 输出到控制台
        logging.FileHandler("alpha_oracle.log", encoding="utf-8", mode="w")  # 输出到文件
    ]
)

logging.info("🔍 调试模式启动")

# 🔍 打印所有环境变数（调试用）
logging.info("📋 环境变数检查:")
for key in ["TG_TOKEN", "CHAT_ID", "MAX_SIGNALS", "SETUP_SCORE_THRESHOLD"]:
    val = os.getenv(key, "❌ 未设置")
    if key in ["TG_TOKEN", "CHAT_ID"]:
        val = val[:10] + "..." if val and len(val) > 10 else val
    logging.info(f"  {key}: {val}")

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

# 🔍 检查 Telegram 凭证
if not TG_TOKEN or not CHAT_ID:
    logging.error("❌ TG_TOKEN 或 CHAT_ID 未正确设置！请检查 GitHub Secrets")
    # 即使没有凭证也继续执行，只是不发送通知

# ⚡ 只监控前 3 大币种
ALL_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
MAX_SIGNALS = _get_env_int("MAX_SIGNALS", 3)
SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

ENTRY_TOLERANCE = 0.002
SIGNAL_EXPIRE_HOURS = 24
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"

_price_cache = {}
_signal_cooldown = {}

# ─────────────────────────────────────────────────────────
# 2. 通知系统 (带错误处理)
# ─────────────────────────────────────────────────────────
def send_tg(msg: str) -> bool:
    """发送 Telegram 通知，带详细错误处理"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️ TG_TOKEN 或 CHAT_ID 为空，跳过发送")
        return False
    
    try:
        logging.info(f"📤 准备发送通知: {msg[:50]}...")
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        
        r = requests.post(url, json=payload, timeout=5)
        
        if r.status_code == 200:
            logging.info("✅ Telegram 通知发送成功")
            return True
        else:
            logging.error(f"❌ Telegram API 错误 {r.status_code}: {r.text}")
            return False
    except requests.exceptions.Timeout:
        logging.error("❌ Telegram 请求超时")
        return False
    except requests.exceptions.ConnectionError:
        logging.error("❌ Telegram 连接失败")
        return False
    except Exception as e:
        logging.error(f"❌ Telegram 发送异常: {e}")
        traceback.print_exc()
        return False

def _format_tp_alert(coin: str, side: str, tp_level: str, price: float, 
                     entry: float, pnl_pct: float, r_mult: float) -> str:
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    return (
        f"{emoji} *{coin} {tp_level} 达标！*\n"
        f"────────────\n"
        f"方向：{direction}\n"
        f"价格：`{price:.4f}`\n"
        f"获利：`+{pnl_pct:.1f}%` (`+{r_mult}R`)\n"
        f"\n"
        f"💡 建议全部平仓"
    )

def _format_sl_alert(coin: str, side: str, price: float, entry: float, 
                     pnl_pct: float, is_be: bool = False) -> str:
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    label = "🔒 保本出场" if is_be else "❌ 止损离场"
    return (
        f"{label} *{coin}*\n"
        f"────────────\n"
        f"方向：{direction}\n"
        f"价格：`{price:.4f}`\n"
        f"结果：`{pnl_pct:+.1f}%`"
    )

def _format_entry_alert(coin: str, side: str, price: float, entry: float,
                        sl: float, tp1: float, tp2: float, tp3: float, score: int) -> str:
    direction = "做多" if side == "LONG" else "做空"
    emoji = "🟢" if side == "LONG" else "🔴"
    grade = "🔥" if score >= 80 else "✅" if score >= 68 else "⚪"
    return (
        f"{emoji} *{coin} 进场提醒* {grade}\n"
        f"────────────\n"
        f"方向：{direction}\n"
        f"价格：`{price:.4f}`\n"
        f"评分：{score}分\n"
        f"\n"
        f"🎯 止盈：\n"
        f"  TP1 `{tp1:.4f}` (+{(tp1-entry)/entry*100:.1f}%)\n"
        f"  TP2 `{tp2:.4f}` (+{(tp2-entry)/entry*100:.1f}%)\n"
        f"  TP3 `{tp3:.4f}` (+{(tp3-entry)/entry*100:.1f}%)\n"
        f"\n"
        f"🛑 止损：`{sl:.4f}` ({(sl-entry)/entry*100:+.1f}%)"
    )

def _format_position_card(coin: str, side: str, score: int,
                         current: float, entry: float, sl: float,
                         tp1: float, tp2: float, tp3: float,
                         status: str, hit_tp1: bool, hit_tp2: bool, hit_tp3: bool) -> str:
    coin_emoji = "🟠" if "BTC" in coin else "🔷" if "ETH" in coin else "🟣"
    side_emoji = "🟢" if side == "LONG" else "🔴"
    pnl = ((current - entry) / entry * 100) if side == "LONG" else ((entry - current) / entry * 100)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    progress = "🥇🏆" if hit_tp3 else "🥇🥈🏆" if hit_tp2 else "🥇🥈" if hit_tp1 else "⏳🏆"
    
    return (
        f"{coin_emoji} *#{coin}* · {side_emoji} {side} · {score}分\n"
        f"{' ACTIVE · 持仓中' if status == 'ACTIVE' else status}\n"
        f"✅ 当前 `{current:.4f}` {pnl_emoji}{pnl:+.2f}%\n"
        f"🟢 进场 `{entry:.4f}`\n"
        f"🔴 止损 `{sl:.4f}`\n"
        f"🥇 TP1 `{tp1:.4f}`\n"
        f"🥈 TP2 `{tp2:.4f}`\n"
        f"🏆 TP3 `{tp3:.4f}`\n"
        f"进度 {progress}"
    )

# ─────────────────────────────────────────────────────────
# 3. 数据抓取 (带错误处理)
# ─────────────────────────────────────────────────────────
def fetch_price(instId: str) -> float:
    """获取即时价格，带缓存和错误处理"""
    now = time.time()
    
    # 检查缓存
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 10:  # 10 秒缓存
            return price
    
    try:
        logging.debug(f"🔍 请求价格: {instId}")
        res = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=2
        ).json()
        
        if res.get("code") == "0" and res.get("data"):
            price = float(res["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                logging.debug(f"✅ 获取价格成功: {instId} = {price}")
                return price
    except Exception as e:
        logging.warning(f"⚠️ 获取 {instId} 价格失败: {e}")
    
    # 回传缓存（即使过期）
    if instId in _price_cache:
        return _price_cache[instId][0]
    
    return 0.0

def fetch_candles(instId: str, tf: str = "15m", limit: int = 100):
    """获取 K 线数据，带错误处理"""
    try:
        logging.debug(f"🔍 请求 K 线: {instId} {tf}")
        res = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=3
        ).json()
        
        if res.get("code") != "0":
            logging.warning(f"⚠️ OKX API 错误: {res.get('msg')}")
            return None
        
        # 🔍 简化：不使用 pandas，用纯 Python 处理
        data = res.get("data", [])
        if len(data) < 30:
            return None
        
        # 过滤已确认的 K 线并反转
        confirmed = [row for row in data if row[8] == "1"][::-1]
        
        # 转换为字典列表
        df = []
        for row in confirmed:
            df.append({
                "ts": row[0], "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5])
            })
        
        return df
    except Exception as e:
        logging.warning(f"⚠️ 获取 {instId} K 线失败: {e}")
        return None

# ─────────────────────────────────────────────────────────
# 4. 技术指标 (纯 Python 实现，不依赖 pandas/numpy)
# ─────────────────────────────────────────────────────────
def calc_atr(df, period: int = 14) -> float:
    """计算 ATR (纯 Python 实现)"""
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
    
    # 简单移动平均
    atr = sum(tr_values[-period:]) / period
    return atr if atr > 0 else 0.001

def calc_supertrend(df, period: int = 10, mult: float = 3.0) -> int:
    """计算 Supertrend (简化版)"""
    if len(df) < period + 2:
        return 0
    
    # 计算 ATR
    atr = calc_atr(df, period)
    
    # 简单趋势判断：比较当前价格与中期均线
    mid_price = sum(row["c"] for row in df[-20:]) / 20
    current_price = df[-1]["c"]
    
    if current_price > mid_price + atr * 0.5:
        return 1  # 多头
    elif current_price < mid_price - atr * 0.5:
        return -1  # 空头
    else:
        return 0  # 震荡

def calc_rsi(df, period: int = 14) -> float:
    """计算 RSI (简化版)"""
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
    """计算信号评分"""
    score = 0
    
    # 趋势因子 (60 分)
    st = calc_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1):
        score += 60
    elif st == 0:
        score += 30
    
    # 动量因子 (40 分)
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
    
    grade = "A+ 极强 🔥" if score >= 85 else "A 强力 ⭐" if score >= 70 else "B+ 观望 ✅"
    return score, grade

# ─────────────────────────────────────────────────────────
# 5. 信号生成
# ─────────────────────────────────────────────────────────
def generate_signal(instId: str, df):
    """生成交易信号"""
    if df is None or len(df) < 50:
        return None
    
    price = df[-1]["c"]
    atr = calc_atr(df)
    
    # 波动过滤
    if atr / price > 0.04:
        logging.info(f"[{instId}] 波动过大，跳过")
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
# 6. SignalTracker 类
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.signals = self._load()
        self.transitions = 0
        logging.info(f"📦 加载 {len(self.signals)} 笔信号")
    
    def _load(self) -> dict:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                    logging.info(f"✅ 成功加载 {self.filepath}")
                    return data if isinstance(data, dict) else {}
            logging.info(f"ℹ️  {self.filepath} 不存在，创建新文件")
            return {}
        except json.JSONDecodeError as e:
            logging.error(f"❌ JSON 解析错误 {self.filepath}: {e}")
            return {}
        except Exception as e:
            logging.error(f"❌ 加载 {self.filepath} 失败: {e}")
            return {}
    
    def _save(self):
        try:
            temp = self.filepath + ".tmp"
            with open(temp, "w") as f:
                json.dump(self.signals, f, indent=2)
            os.replace(temp, self.filepath)
            logging.debug(f"💾 保存 {self.filepath} 成功")
        except Exception as e:
            logging.error(f"❌ 保存 {self.filepath} 失败: {e}")
    
    def add(self, signal: dict, active: bool = False) -> str:
        key = f"{signal['instId']}_{signal['side']}_{int(time.time())}"
        with self._lock:
            self.signals[key] = {
                **signal,
                "status": "ACTIVE" if active else "PENDING",
                "hit_tp1": False,
                "hit_tp2": False,
                "hit_tp3": False,
                "activated_at": time.time() if active else None,
            }
            self._save()
        logging.info(f"📌 新增信号: {key} [{'ACTIVE' if active else 'PENDING'}]")
        return key
    
    def remove(self, key: str):
        with self._lock:
            if key in self.signals:
                del self.signals[key]
                self._save()
                logging.info(f"🗑️ 移除信号: {key}")
    
    def check_one(self, key: str, sig: dict) -> bool:
        """检查单一信号状态"""
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                logging.debug(f"[{key}] 无法获取价格，跳过")
                return False
            
            sig["current_price"] = price
            coin = sig["instId"].split("-")[0]
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            
            # PENDING: 等待进场
            if status == "PENDING":
                if time.time() > sig["expires"]:
                    send_tg(f"⏰ *{coin} 信号过期*")
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
                    send_tg(_format_entry_alert(coin, side, price, entry, sl, tp1, tp2, tp3, sig["score"]))
                    self.transitions += 1
                return False
            
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False
            
            def _dev(t):
                return abs(price - t) / t * 100
            
            # SL 触发
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit or (_dev(sl) > 0.003 and ((side == "LONG" and price < sl) or (side == "SHORT" and price > sl))):
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                send_tg(_format_sl_alert(coin, side, price, entry, pnl, is_be))
                _record_trade(coin, side, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            # TP3 触发
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if (tp3_hit or _dev(tp3) > 0.003) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                send_tg(_format_tp_alert(coin, side, "TP3", tp3, entry, pnl, 4.0))
                _record_trade(coin, side, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # TP2 触发
            tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if (tp2_hit or _dev(tp2) > 0.003) and not sig.get("hit_tp2"):
                with self._lock:
                    sig["hit_tp2"] = True
                    sig["sl"] = tp1
                    sig["status"] = "TRAIL"
                    self._save()
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                send_tg(_format_tp_alert(coin, side, "TP2", tp2, entry, pnl, 2.5))
                _record_trade(coin, side, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # TP1 触发
            tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if (tp1_hit or _dev(tp1) > 0.003) and not sig.get("hit_tp1"):
                with self._lock:
                    sig["hit_tp1"] = True
                    sig["sl"] = entry
                    sig["status"] = "BE"
                    self._save()
                send_tg(_format_tp_alert(coin, side, "TP1", tp1, entry, 0.0, 1.0))
                _record_trade(coin, side, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}] 错误: {e}")
            traceback.print_exc()
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
        if to_remove:
            logging.info(f"✅ 移除 {len(to_remove)} 笔已结算信号")
    
    def get_position_stats(self) -> str:
        positions = [
            {**sig, "current_price": fetch_price(sig["instId"])}
            for sig in self.signals.values()
            if sig["status"] in ("ACTIVE", "BE", "TRAIL", "PENDING")
        ]
        if not positions:
            return "📭 *目前无持仓*\n\n🔄 系统持续扫描中"
        
        msg = f"📊 *追踪中信号 ({len(positions)} 笔)*\n" + "═" * 30 + "\n\n"
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
            return "📭 *目前无追踪中信号*\n\n🔄 系统持续扫描中"
        
        lines = [f"📋 *追踪中信号 ({len(items)} 笔)*", "────────────"]
        for sig in items[:5]:
            coin = sig["instId"].split("-")[0]
            arrow = "🟢" if sig["side"] == "LONG" else "🔴"
            price = fetch_price(sig["instId"])
            pnl = ((price - sig["entry"]) / sig["entry"] * 100) if price > 0 and sig["status"] != "PENDING" else 0
            lines.append(f"{arrow} *{coin}* {sig['status']} `{pnl:+.1f}%`")
        lines.append("────────────\n🤖 Alpha Oracle Pro 持续监控中")
        return "\n".join(lines)

def _record_trade(coin: str, side: str, entry: float, close_price: float, 
                  close_type: str, score: int):
    """记录交易历史"""
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
        logging.info(f"📝 记录交易: {coin} {close_type}")
    except Exception as e:
        logging.error(f"❌ 记录交易失败: {e}")

# ─────────────────────────────────────────────────────────
# 7. 主扫描逻辑
# ─────────────────────────────────────────────────────────
def scan_one(instId: str, tracker: SignalTracker) -> bool:
    """扫描单一币种"""
    try:
        logging.info(f"🔍 扫描 {instId}...")
        
        key = f"{instId}_ALL"
        if key in _signal_cooldown and time.time() - _signal_cooldown[key] < 2 * 3600:
            logging.debug(f"[{instId}] 冷却中，跳过")
            return False
        
        df = fetch_candles(instId)
        if df is None:
            logging.warning(f"[{instId}] 获取 K 线失败")
            return False
        
        signal = generate_signal(instId, df)
        if not signal:
            logging.debug(f"[{instId}] 无符合信号")
            return False
        
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
            logging.info(f"✅ {instId} 信号发送成功")
            return True
        else:
            logging.warning(f"[{instId}] 通知发送失败")
            return False
    except Exception as e:
        logging.error(f"[{instId}] 扫描异常: {e}")
        traceback.print_exc()
        return False

def run_scan(tracker: SignalTracker) -> int:
    """执行扫描"""
    logging.info("🚀 开始扫描...")
    sent = 0
    
    # ⚡ 并发执行
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(scan_one, instId, tracker): instId for instId in ALL_COINS}
        for future in as_completed(futures):
            instId = futures[future]
            try:
                if future.result(timeout=8):
                    sent += 1
            except Exception as e:
                logging.error(f"[{instId}] 执行失败: {e}")
    
    tracker.check_all()
    
    if tracker.transitions > 0 or tracker.signals:
        send_tg(tracker.status_summary())
    
    logging.info(f"✅ 扫描完成，发送 {sent} 笔信号")
    return sent

# ─────────────────────────────────────────────────────────
# 8. 主函数
# ─────────────────────────────────────────────────────────
def main():
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v10.4 调试版启动")
        logging.info("=" * 50)
        
        # 🔍 检查依赖
        try:
            import requests
            logging.info("✅ requests 库正常")
        except ImportError:
            logging.error("❌ requests 库未安装")
            return
        
        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        
        # 🔹 处理 /stats 命令
        if len(sys.argv) > 1 and sys.argv[1] in ("/stats", "/持仓"):
            logging.info("📊 执行持仓统计命令")
            send_tg(tracker.get_position_stats())
            return
        
        # 🔹 执行扫描 + 监控
        run_scan(tracker)
        
        logging.info("🎉 程序执行完成")
        
    except Exception as e:
        logging.critical(f"💥 未捕获的异常: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
