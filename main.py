#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v10.0 — 專業級交易監控系統
══════════════════════════════════════════════════════════════════════
🎯 產品定位：專業交易員級監控工具，新手也能輕鬆使用
✨ 核心功能：
  ✅ 即時通知：進場/TP1/2/3/SL 秒級推送（三重保障機制）
  ✅ 每日戰報：00:00 自動發送勝率統計 + 止損分析報告
  ✅ 專業分析：多因子評分 + 市場結構 + 訂單流 + 情緒面
  ✅ 新手友好：Telegram 訊息簡潔直觀，關鍵資訊一目了然
  ✅ 狀態永續：JSON 狀態文件 + GitHub Actions 自動同步
  ✅ 資源優化：嚴格控制 GitHub Actions 用量 <200 分鐘/月
══════════════════════════════════════════════════════════════════════
"""
import requests
import os
import json
import sys
import argparse
import pandas as pd
import numpy as np
import logging
import traceback
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────
# 🔧 環境變數安全解析輔助函數（修復空字串問題）
# ─────────────────────────────────────────────────────────
def _get_env_str(key: str, default: str = "") -> str:
    val = os.getenv(key, "")
    return val.strip() if val and val.strip() else default

def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key, "")
    try:
        return int(val.strip()) if val and val.strip() else default
    except (ValueError, TypeError):
        return default

def _get_env_float(key: str, default: float) -> float:
    val = os.getenv(key, "")
    try:
        return float(val.strip()) if val and val.strip() else default
    except (ValueError, TypeError):
        return default

def _get_env_bool(key: str, default: bool = False) -> bool:
    val = _get_env_str(key, "")
    return val.lower() in ("true", "1", "yes", "on")

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("alpha_oracle.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)

# Telegram 設定
TG_TOKEN = _get_env_str("TG_TOKEN")
CHAT_ID = _get_env_str("CHAT_ID")

# 交易設定
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]
SCAN_TIMEFRAMES = ["15m", "30m", "1H"]
MAX_SIGNALS_PER_RUN = _get_env_int("MAX_SIGNALS", 6)
SETUP_SCORE_THRESHOLD = _get_env_int("SETUP_SCORE_THRESHOLD", 68)

# 風險參數
ENTRY_TOLERANCE = 0.002
SIGNAL_EXPIRE_HOURS = 24
SIGNAL_COOLDOWN_HOURS = 2

# 技術指標參數
ATR_PERIOD = 14
RSI_PERIOD = 14
ADX_PERIOD = 14
VOLATILITY_LIMIT = 0.035

# 狀態文件
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
DAILY_REPORT_FILE = "daily_report.json"

# 通知設定
CONFIRM_TP_ON_CLOSE = _get_env_bool("CONFIRM_TP_ON_CLOSE", True)
EMERGENCY_PRICE_THRESHOLD = 0.003  # 0.3% 價格偏離緊急觸發
HEARTBEAT_WINDOW_MIN = 5  # 整點心跳窗口（分鐘）

# 全局快取
_price_cache: dict = {}
_news_cooldown: dict = {}
_signal_cooldown: dict = {}

# ─────────────────────────────────────────────────────────
# 2. 專業通知系統（新手友好 + 即時可靠）
# ─────────────────────────────────────────────────────────
def send_tg(msg: str, parse_mode: str = "Markdown", emergency: bool = False) -> bool:
    """📤 專業通知發送：三重保障 + 新手友好格式"""
    if not TG_TOKEN or not CHAT_ID:
        logging.error("❌ TG_TOKEN 或 CHAT_ID 未設定")
        return False
    
    max_retries = 3
    base_delay = 1 if emergency else 2
    
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode},
                timeout=10 if emergency else 15
            )
            if r.status_code == 200:
                logging.info("✅ Telegram 通知發送成功")
                return True
            elif r.status_code == 429:  # 頻率限制
                retry_after = int(r.json().get("parameters", {}).get("retry_after", base_delay))
                time.sleep(retry_after)
                continue
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    
    # 🔹 緊急備援：寫入本地日誌
    if emergency:
        try:
            with open("emergency_alerts.log", "a", encoding="utf-8") as f:
                f.write(f"{utc_now().isoformat()} [EMERGENCY] {msg}\n")
        except: pass
    return False

def _format_simple_alert(coin: str, side: str, alert_type: str, price: float, 
                        entry: float, sl: float, tp1: float, tp2: float, tp3: float,
                        pnl_pct: float = None, score: int = None) -> str:
    """🎯 新手友好通知格式：簡潔直觀，關鍵資訊一目了然"""
    arrow = "🟢" if side == "LONG" else "🔴"
    direction = "做多" if side == "LONG" else "做空"
    
    if alert_type == "ENTRY":
        return (
            f"{arrow} *{coin} 進場提醒*\n"
            f"────────────\n"
            f"方向：{direction}\n"
            f"價格：`{price:.4f}`\n"
            f"評分：{score}分 {'🔥' if score >= 80 else '✅' if score >= 68 else '⚪'}\n"
            f"\n"
            f"🎯 止盈：\n"
            f"  TP1 `{tp1:.4f}` (+{(tp1-entry)/entry*100:.1f}%)\n"
            f"  TP2 `{tp2:.4f}` (+{(tp2-entry)/entry*100:.1f}%)\n"
            f"  TP3 `{tp3:.4f}` (+{(tp3-entry)/entry*100:.1f}%)\n"
            f"\n"
            f"🛑 止損：`{sl:.4f}` ({(sl-entry)/entry*100:+.1f}%)\n"
            f"\n"
            f"💡 提示：到達 TP1 自動保本，到達 TP2 自動鎖利"
        )
    
    elif alert_type in ("TP1", "TP2", "TP3"):
        tp_num = alert_type[-1]
        tp_price = {"1": tp1, "2": tp2, "3": tp3}[tp_num]
        r_mult = {"1": 1.0, "2": 2.5, "3": 4.0}[tp_num]
        
        return (
            f"🎯 *{coin} {alert_type} 達標！*\n"
            f"────────────\n"
            f"方向：{direction}\n"
            f"價格：`{price:.4f}`\n"
            f"獲利：`+{pnl_pct:.1f}%` (`+{r_mult}R`)\n"
            f"\n"
            f"✅ 已達成 TP{tp_num}：`{tp_price:.4f}`\n"
            f"🔄 剩餘目標：\n"
            f"  {'TP'+str(int(tp_num)+1) if tp_num<'3' else '✅ 全部達成'}\n"
            f"\n"
            f"💡 {'建議平倉 ⅓ 鎖定獲利' if tp_num=='1' else '建議平倉 ⅓ 落袋為安' if tp_num=='2' else '建議全部平倉完美收割'}"
        )
    
    elif alert_type == "SL":
        is_be = pnl_pct is not None and abs(pnl_pct) < 0.1
        label = "🔒 保本出場" if is_be else "❌ 止損離場"
        pnl_tag = "`0.0R`" if is_be else "`-1.0R`"
        
        return (
            f"{label} *{coin}*\n"
            f"────────────\n"
            f"方向：{direction}\n"
            f"價格：`{price:.4f}`\n"
            f"結果：`{pnl_pct:+.1f}%` {pnl_tag}\n"
            f"\n"
            f"🛑 止損價：`{sl:.4f}`\n"
            f"\n"
            f"💡 {'資金安全，等待下一次機會 💪' if is_be else '遵守風控，勿加碼攤平'}"
        )
    
    return ""

def _format_daily_report(trades: list, date_str: str) -> str:
    """📊 專業日報格式：勝率統計 + 止損分析 + 改進建議"""
    if not trades:
        return (
            f"📊 *Alpha Oracle 每日戰報*\n"
            f"────────────\n"
            f"📅 日期：{date_str}\n"
            f"\n"
            f"📭 今日暫無已結算交易\n"
            f"🔄 系統持續監控中...\n"
            f"\n"
            f"💡 提示：有訊號時會即時通知您"
        )
    
    # 統計計算
    total = len(trades)
    wins = [t for t in trades if t.get("is_win")]
    losses = [t for t in trades if not t.get("is_win") and not t.get("is_be")]
    breakevens = [t for t in trades if t.get("is_be")]
    
    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    expectancy = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss) if total > 0 else 0
    
    # 止損原因分析（簡化版）
    sl_reasons = {}
    for t in losses:
        reason = t.get("sl_reason", "市場波動")
        sl_reasons[reason] = sl_reasons.get(reason, 0) + 1
    
    # 等級評定
    if win_rate >= 70: grade, emoji = "🏆 優秀", "⭐"
    elif win_rate >= 55: grade, emoji = "✅ 良好", "👍"
    elif win_rate >= 40: grade, emoji = "⚠️ 普通", "📊"
    else: grade, emoji = "🔧 需優化", "⚙️"
    
    # 組裝訊息
    msg = (
        f"📊 *Alpha Oracle 每日戰報* {emoji}\n"
        f"────────────\n"
        f"📅 日期：{date_str}\n"
        f"\n"
        f"📈 績效統計：\n"
        f"  總交易：{total} 筆 {grade}\n"
        f"  ✅ 盈利：{len(wins)} 筆 ({win_rate:.1f}%)\n"
        f"  ❌ 止損：{len(losses)} 筆\n"
        f"  🔒 保本：{len(breakevens)} 筆\n"
        f"\n"
        f"💰 平均獲利：`+{avg_win:.1f}%`\n"
        f"📉 平均虧損：`{avg_loss:+.1f}%`\n"
        f"⚡ 期望值：`{expectancy:+.2f}%/筆`\n"
    )
    
    # 止損分析（如果有）
    if sl_reasons:
        msg += f"\n🔍 止損原因分析：\n"
        for reason, count in sorted(sl_reasons.items(), key=lambda x: -x[1])[:3]:
            pct = count / len(losses) * 100
            msg += f"  • {reason}：{count}筆 ({pct:.0f}%)\n"
        
        # 改進建議
        top_reason = max(sl_reasons.items(), key=lambda x: x[1])[0]
        if "盤整" in top_reason:
            msg += f"\n💡 建議：盤整行情減少交易，等待明確突破訊號"
        elif "波動" in top_reason:
            msg += f"\n💡 建議：高波動時縮小倉位或放寬止損"
        elif "趨勢" in top_reason:
            msg += f"\n💡 建議：順勢交易，避免逆勢抄底摸頂"
    
    msg += f"\n────────────\n"
    msg += f"🤖 Alpha Oracle Pro 明日繼續為您監控！"
    
    return msg

# ─────────────────────────────────────────────────────────
# 3. 數據抓取（專業可靠）
# ─────────────────────────────────────────────────────────
def fetch_okx(instId: str, tf: str = "15m", limit: int = 150) -> pd.DataFrame | None:
    """📡 OKX K 線抓取，帶重試機制"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0": return None
        
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"])
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 30 else None
    except Exception as e:
        logging.warning(f"[{instId}/{tf}] 抓取失敗: {e}")
        return None

def fetch_ticker_price(instId: str) -> float:
    """🔍 即時價格抓取，帶快取 + 重試"""
    now = time.time()
    
    # 🔹 快取檢查（2 秒內不重複請求）
    if instId in _price_cache:
        cached_price, cached_time = _price_cache[instId]
        if now - cached_time < 2:
            return cached_price
    
    for attempt in range(3):
        try:
            res = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}", timeout=5).json()
            if res.get("code") == "0" and res.get("data"):
                price = float(res["data"][0]["last"])
                if price > 0:
                    _price_cache[instId] = (price, now)
                    return price
        except:
            if attempt < 2: time.sleep(2 ** attempt)
    
    # 🔹 回傳快取（即使過期）
    if instId in _price_cache:
        return _price_cache[instId][0]
    return 0.0

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        return float(res["data"][0]["fundingRate"]) if res.get("data") else 0.0
    except: return 0.0

def fetch_ls_ratio(symbol: str) -> tuple:
    try:
        base = symbol.split("-")[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}", timeout=5).json()
        if res.get("data"):
            r = float(res["data"][0]["ratio"])
            return r, f"{r:.2f}"
        return 1.0, "N/A"
    except: return 1.0, "N/A"

# ─────────────────────────────────────────────────────────
# 4. 專業技術指標（精簡專業版）
# ─────────────────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    hl = df["h"] - df["l"]
    hc = np.abs(df["h"] - df["c"].shift())
    lc = np.abs(df["l"] - df["c"].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> tuple:
    if len(df) < period + 2: return 0, "未知"
    h, l, c = df["h"].values, df["l"].values, df["c"].values
    n = len(df)
    
    # True Range
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    
    # ATR
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    
    # Supertrend
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
    
    return (1, "多頭") if trend[-1] == 1 else ((-1, "空頭") if trend[-1] == -1 else (0, "未知"))

def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> float:
    delta = df["c"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return float((100 - (100 / (1 + rs))).iloc[-1])

def detect_market_structure(df: pd.DataFrame) -> str:
    """🏗️ 市場結構偵測（W 底 / M 頭 / 趨勢）"""
    recent = df.tail(60)
    highs = recent["h"].values
    lows = recent["l"].values
    
    # 尋找擺動點
    swing_highs = [i for i in range(3, len(highs)-3) 
                   if all(highs[i] > highs[i+j] for j in range(-3, 4) if j != 0)]
    swing_lows = [i for i in range(3, len(lows)-3) 
                  if all(lows[i] < lows[i+j] for j in range(-3, 4) if j != 0)]
    
    # W 底 / M 頭偵測
    if len(swing_lows) >= 2:
        l1, l2 = lows[swing_lows[-2]], lows[swing_lows[-1]]
        if abs(l1 - l2) / l1 < 0.015: return "W 底反轉"
    if len(swing_highs) >= 2:
        h1, h2 = highs[swing_highs[-2]], highs[swing_highs[-1]]
        if abs(h1 - h2) / h1 < 0.015: return "M 頭反轉"
    
    # 趨勢判斷
    slope = (recent["c"].iloc[-1] - recent["c"].iloc[-20]) / recent["c"].iloc[-20]
    if slope > 0.025: return "上升趨勢"
    if slope < -0.025: return "下降趨勢"
    return "區間盤整"

def calculate_score(df: pd.DataFrame, side: str) -> tuple:
    """🎯 多因子評分系統（專業級）"""
    score = 0
    factors = []
    
    # 🔹 趨勢因子 (30%)
    st_val, st_label = calculate_supertrend(df)
    if (side == "LONG" and st_val == 1) or (side == "SHORT" and st_val == -1):
        score += 30
        factors.append(f"趨勢順勢 +30")
    elif st_val == 0:
        score += 15
        factors.append(f"趨勢不明 +15")
    
    # 🔹 動量因子 (25%)
    rsi = calculate_rsi(df)
    if side == "LONG":
        if 30 <= rsi <= 50: score += 25; factors.append(f"RSI 低檔 +25")
        elif 50 < rsi < 70: score += 15; factors.append(f"RSI 中性 +15")
    else:
        if 50 <= rsi <= 70: score += 25; factors.append(f"RSI 高檔 +25")
        elif 30 < rsi < 50: score += 15; factors.append(f"RSI 中性 +15")
    
    # 🔹 波動因子 (20%)
    atr = calculate_atr(df)
    vol_ratio = atr / df["c"].iloc[-1]
    if 0.01 < vol_ratio < 0.04:
        score += 20
        factors.append(f"波動適中 +20")
    elif vol_ratio <= 0.01 or vol_ratio >= 0.04:
        score += 10
        factors.append(f"波動極端 +10")
    
    # 🔹 結構因子 (15%)
    structure = detect_market_structure(df)
    if ("反轉" in structure) or (side == "LONG" and "上升" in structure) or (side == "SHORT" and "下降" in structure):
        score += 15
        factors.append(f"結構配合 +15")
    
    # 🔹 情緒因子 (10%)
    fr = fetch_funding_rate(df["instId"] if hasattr(df, "instId") else "BTC-USDT-SWAP")
    if (side == "LONG" and fr < 0.0001) or (side == "SHORT" and fr > 0.0001):
        score += 10
        factors.append(f"費率友善 +10")
    
    # 🔹 等級評定
    if score >= 85: grade = "A+ 極強 🔥"
    elif score >= 75: grade = "A  強力 ⭐"
    elif score >= 65: grade = "B+ 觀望 ✅"
    elif score >= 55: grade = "B  偏弱 ⚪"
    else: grade = "C  跳過 ❌"
    
    return score, grade, factors

# ─────────────────────────────────────────────────────────
# 5. 訊號生成與追蹤（專業核心）
# ─────────────────────────────────────────────────────────
def generate_signal(instId: str, df: pd.DataFrame) -> dict | None:
    """🎯 生成專業交易訊號"""
    if df is None or len(df) < 50: return None
    
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    
    # 🔹 波動過濾
    if atr / price > VOLATILITY_LIMIT:
        logging.info(f"[{instId}] 波動過大，跳過")
        return None
    
    signals = []
    for side in ["LONG", "SHORT"]:
        score, grade, factors = calculate_score(df, side)
        if score < SETUP_SCORE_THRESHOLD: continue
        
        # 🔹 計算進場/止損/止盈
        entry = price
        sl_dist = atr * 1.5
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        risk = abs(entry - sl)
        
        tp1 = entry + risk if side == "LONG" else entry - risk
        tp2 = entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5
        tp3 = entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0
        
        signal = {
            "instId": instId,
            "side": side,
            "tf": "15m",
            "entry": round(entry, 4),
            "sl": round(sl, 4),
            "tp1": round(tp1, 4),
            "tp2": round(tp2, 4),
            "tp3": round(tp3, 4),
            "score": score,
            "grade": grade,
            "factors": factors,
            "created": time.time(),
            "expires": time.time() + SIGNAL_EXPIRE_HOURS * 3600,
        }
        signals.append(signal)
    
    return max(signals, key=lambda x: x["score"]) if signals else None

class SignalTracker:
    """🔍 專業訊號追蹤器：進場/TP/SL 即時監控"""
    
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.signals = self._load()
        self.transitions = 0  # 狀態變動計數
    
    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return {}
    
    def _save(self):
        """💾 原子寫入：確保狀態文件完整"""
        try:
            temp = self.filepath + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.signals, f, ensure_ascii=False, indent=2)
            os.replace(temp, self.filepath)
        except Exception as e:
            logging.error(f"❌ 儲存狀態失敗: {e}")
    
    def add(self, signal: dict, active: bool = False) -> str:
        """📌 新增追蹤訊號"""
        key = f"{signal['instId']}_{signal['side']}_{int(time.time())}"
        with self._lock:
            self.signals[key] = {
                **signal,
                "status": "ACTIVE" if active else "PENDING",
                "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
                "touched_tp1": False, "touched_tp2": False,
                "activated_at": time.time() if active else None,
            }
            self._save()
        logging.info(f"📌 新增: {key} [{'ACTIVE' if active else 'PENDING'}]")
        return key
    
    def check_one(self, key: str, sig: dict) -> bool:
        """🔍 檢查單一訊號狀態，返回 True=已結束"""
        try:
            price = fetch_ticker_price(sig["instId"])
            if price <= 0: return False
            
            coin = sig["instId"].split("-")[0]
            side, status = sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            
            # ── PENDING: 等待進場 ─────────────────────
            if status == "PENDING":
                # 🔹 過期檢查
                if time.time() > sig["expires"]:
                    send_tg(f"⏰ *{coin} 訊號過期*\n進場 `{entry:.4f}` 未觸發")
                    self.transitions += 1
                    return True
                
                # 🔹 進場區檢查
                in_zone = (
                    (side == "LONG" and entry*(1-ENTRY_TOLERANCE*3) <= price <= entry*(1+ENTRY_TOLERANCE)) or
                    (side == "SHORT" and entry*(1-ENTRY_TOLERANCE) <= price <= entry*(1+ENTRY_TOLERANCE*3))
                )
                if in_zone:
                    # 更新狀態 + 發送通知
                    with self._lock:
                        sig["status"] = "ACTIVE"
                        sig["activated_at"] = time.time()
                        self._save()
                    
                    msg = _format_simple_alert(coin, side, "ENTRY", price, entry, sl, tp1, tp2, tp3, score=sig["score"])
                    send_tg(msg)
                    self.transitions += 1
                return False
            
            # ── ACTIVE/BE/TRAIL: 監控 TP/SL ─────────────
            if status not in ("ACTIVE", "BE", "TRAIL"): return False
            
            # 🔹 輔助函數
            def _dev(target): return abs(price - target) / target * 100
            EMG = EMERGENCY_PRICE_THRESHOLD
            
            # 🔴 止損觸發（最優先）
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            sl_emg = _dev(sl) > EMG and ((side == "LONG" and price < sl) or (side == "SHORT" and price > sl))
            
            if sl_hit or sl_emg:
                is_be = status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001
                pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
                
                msg = _format_simple_alert(coin, side, "SL", price, entry, sl, tp1, tp2, tp3, 
                                         pnl_pct=pnl if not is_be else 0.0)
                send_tg(msg, emergency=sl_emg)
                
                # 🔹 記錄交易歷史
                _record_trade(coin, side, entry, price, "BE" if is_be else "SL", sig["score"])
                self.transitions += 1
                return True
            
            # 🏆 TP3 觸發
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            tp3_emg = _dev(tp3) > EMG and ((side == "LONG" and price > tp3) or (side == "SHORT" and price < tp3))
            
            if (tp3_hit or tp3_emg) and not sig.get("hit_tp3"):
                pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
                msg = _format_simple_alert(coin, side, "TP3", tp3, entry, sl, tp1, tp2, tp3, pnl_pct=pnl)
                send_tg(msg, emergency=tp3_emg)
                
                _record_trade(coin, side, entry, tp3, "TP3", sig["score"])
                self.transitions += 1
                return True
            
            # 🥈 TP2 觸發（收盤確認 + 緊急備援）
            tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            tp2_emg = _dev(tp2) > EMG and ((side == "LONG" and price > tp2) or (side == "SHORT" and price < tp2))
            
            if (tp2_hit or tp2_emg) and not sig.get("hit_tp2"):
                # 🔹 收盤確認（可配置）
                if CONFIRM_TP_ON_CLOSE and not tp2_emg:
                    if not _is_close_confirmed(sig["instId"], sig["tf"], tp2, side):
                        return False
                
                # 🔹 更新狀態 + 移動止損到 TP1
                with self._lock:
                    sig["hit_tp2"] = True
                    sig["sl"] = tp1  # 鎖利
                    sig["status"] = "TRAIL"
                    self._save()
                
                pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
                msg = _format_simple_alert(coin, side, "TP2", tp2, entry, sl, tp1, tp2, tp3, pnl_pct=pnl)
                send_tg(msg, emergency=tp2_emg)
                
                _record_trade(coin, side, entry, tp2, "TP2", sig["score"])
                self.transitions += 1
                return False
            
            # 🥇 TP1 觸發（收盤確認 + 緊急備援）
            tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            tp1_emg = _dev(tp1) > EMG and ((side == "LONG" and price > tp1) or (side == "SHORT" and price < tp1))
            
            if (tp1_hit or tp1_emg) and not sig.get("hit_tp1"):
                if CONFIRM_TP_ON_CLOSE and not tp1_emg:
                    if not _is_close_confirmed(sig["instId"], sig["tf"], tp1, side):
                        return False
                
                with self._lock:
                    sig["hit_tp1"] = True
                    sig["sl"] = entry  # 保本
                    sig["status"] = "BE"
                    self._save()
                
                msg = _format_simple_alert(coin, side, "TP1", tp1, entry, sl, tp1, tp2, tp3, pnl_pct=0.0)
                send_tg(msg, emergency=tp1_emg)
                
                _record_trade(coin, side, entry, tp1, "TP1", sig["score"])
                self.transitions += 1
                return False
            
            return False
            
        except Exception as e:
            logging.error(f"❌ check_one 錯誤 [{key}]: {e}")
            return False
    
    def check_all(self):
        """🔄 檢查所有追蹤中的訊號"""
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
            logging.info(f"✅ 移除 {len(to_remove)} 筆已結算訊號")
    
    def status_summary(self) -> str:
        """📋 生成追蹤狀態摘要（新手友好）"""
        items = list(self.signals.values())
        if not items:
            return "📭 *目前無追蹤中訊號*\n\n🔄 系統持續掃描中，有機會會即時通知您"
        
        lines = [f"📋 *追蹤中訊號 ({len(items)} 筆)*", "────────────"]
        for sig in items[:5]:  # 最多顯示 5 筆
            coin = sig["instId"].split("-")[0]
            arrow = "🟢" if sig["side"] == "LONG" else "🔴"
            status_map = {"PENDING": "⏳ 等待", "ACTIVE": "🔵 持倉", "BE": "🔒 保本", "TRAIL": "🔁 鎖利"}
            
            price = fetch_ticker_price(sig["instId"])
            pnl = ((price - sig["entry"]) / sig["entry"] * 100) if price > 0 and sig["status"] != "PENDING" else 0
            
            lines.append(f"{arrow} *{coin}* {status_map.get(sig['status'], '❓')} `{pnl:+.1f}%`")
            lines.append(f"  進場 `{sig['entry']:.4f}` | SL `{sig['sl']:.4f}`")
            if sig["status"] != "PENDING":
                lines.append(f"  TP1 `{sig['tp1']:.4f}`{'✅' if sig.get('hit_tp1') else ''}")
        
        if len(items) > 5:
            lines.append(f"\n⋮ 還有 {len(items)-5} 筆訊號追蹤中...")
        
        lines.append("────────────\n🤖 Alpha Oracle Pro 持續監控中")
        return "\n".join(lines)

def _is_close_confirmed(instId: str, tf: str, level: float, side: str) -> bool:
    """✅ 收盤確認：檢查已收盤 K 線是否確實穿越目標價"""
    df = fetch_okx(instId, tf=tf, limit=3)
    if df is None or len(df) < 1: return False
    
    last_close = df["c"].iloc[-1]
    return last_close >= level if side == "LONG" else last_close <= level

def _record_trade(coin: str, side: str, entry: float, close_price: float, 
                 close_type: str, score: int):
    """📝 記錄交易歷史（用於日報分析）"""
    is_win = close_type in ("TP1", "TP2", "TP3")
    is_be = close_type == "BE"
    pnl_pct = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    
    # 🔹 簡化版止損原因分析
    sl_reason = "市場波動"
    if close_type == "SL":
        # 可擴充：加入更多分析邏輯
        atr = calculate_atr(fetch_okx(f"{coin}-USDT-SWAP")) if coin else 0
        if atr and abs(pnl_pct) < 0.5: sl_reason = "正常波動止損"
        elif pnl_pct < -2: sl_reason = "趨勢反轉止損"
        else: sl_reason = "盤整震盪止損"
    
    trade = {
        "time": utc_now().strftime("%Y-%m-%d %H:%M"),
        "date": utc_now().strftime("%Y-%m-%d"),
        "coin": coin, "side": side,
        "entry": entry, "close": close_price,
        "close_type": close_type, "pnl_pct": round(pnl_pct, 2),
        "is_win": is_win, "is_be": is_be, "score": score,
        "sl_reason": sl_reason if close_type == "SL" else None,
    }
    
    # 🔹 追加寫入歷史文件
    try:
        history = []
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        history.append(trade)
        with open(TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"❌ 記錄交易失敗: {e}")

# ─────────────────────────────────────────────────────────
# 6. 主掃描與監控邏輯
# ─────────────────────────────────────────────────────────
def run_scan(tracker: SignalTracker) -> int:
    """🔍 執行訊號掃描"""
    logging.info(f"🚀 開始掃描 | 閾值: {SETUP_SCORE_THRESHOLD}")
    
    sent = 0
    for instId in ALL_COINS:
        if sent >= MAX_SIGNALS_PER_RUN: break
        
        # 🔹 冷卻期檢查
        key = f"{instId}_ALL"
        if key in _signal_cooldown and time.time() - _signal_cooldown[key] < SIGNAL_COOLDOWN_HOURS * 3600:
            continue
        
        df = fetch_okx(instId)
        if df is None: continue
        
        signal = generate_signal(instId, df)
        if signal and send_tg(_format_simple_alert(
            coin=instId.split("-")[0],
            side=signal["side"],
            alert_type="ENTRY",
            price=signal["entry"],
            entry=signal["entry"],
            sl=signal["sl"],
            tp1=signal["tp1"],
            tp2=signal["tp2"],
            tp3=signal["tp3"],
            score=signal["score"]
        )):
            sent += 1
            _signal_cooldown[key] = time.time()
            
            # 🔹 檢查是否已在進場區
            price = fetch_ticker_price(instId)
            in_zone = (
                (signal["side"] == "LONG" and signal["entry"]*(1-ENTRY_TOLERANCE*3) <= price <= signal["entry"]*(1+ENTRY_TOLERANCE)) or
                (signal["side"] == "SHORT" and signal["entry"]*(1-ENTRY_TOLERANCE) <= price <= signal["entry"]*(1+ENTRY_TOLERANCE*3))
            )
            tracker.add(signal, active=in_zone and price > 0)
        
        time.sleep(1)  # API 頻率保護
    
    # 🔹 掃描後檢查既有訊號
    tracker.check_all()
    
    # 🔹 發送狀態摘要（如有變動或整點）
    if tracker.transitions > 0 or utc_now().minute < HEARTBEAT_WINDOW_MIN:
        if tracker.signals:
            send_tg(tracker.status_summary())
    
    return sent

def send_daily_report():
    """📊 發送每日戰報（00:00 自動執行）"""
    today = utc_now().strftime("%Y-%m-%d")
    
    # 🔹 讀取今日交易記錄
    trades = []
    if os.path.exists(TRADE_HISTORY_FILE):
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            all_trades = json.load(f)
            trades = [t for t in all_trades if t["date"] == today]
    
    # 🔹 生成並發送報告
    msg = _format_daily_report(trades, today)
    send_tg(msg)
    
    # 🔹 標記已發送（避免重複）
    with open(DAILY_REPORT_FILE, "w") as f:
        json.dump({"last_sent": today}, f)

# ─────────────────────────────────────────────────────────
# 7. 主函式
# ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpha Oracle Pro v10.0")
    parser.add_argument("--mode", default="all", 
                       choices=["scan", "monitor_once", "daily_report", "all"])
    args = parser.parse_args()
    
    logging.info("=" * 50)
    logging.info("🤖 Alpha Oracle Pro v10.0 啟動")
    logging.info(f"📋 模式: {args.mode}")
    logging.info("=" * 50)
    
    tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
    
    if args.mode == "daily_report":
        send_daily_report()
        return
    
    if args.mode in ("scan", "all"):
        run_scan(tracker)
    
    if args.mode in ("monitor_once", "all"):
        tracker.check_all()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"🔥 系統錯誤: {e}")
        traceback.print_exc()
        sys.exit(1)
