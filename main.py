#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v9.1.7 — 調試版（價格監控確認 + TP 通知強化）
══════════════════════════════════════════════════════════════════════
v9.1.7 修復：
  🔧 CONFIRM_TP_ON_CLOSE 改為 env 控制，預設 False（即時觸發）
  🐛 增加 TP 觸發詳細日誌，方便除錯
  📢 monitor_once 預設強制推播狀態摘要
  🔄 價格監控每 10 秒必檢查，確保不漏單
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
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,  # 🔧 改為 DEBUG 方便除錯
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("alpha_oracle.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]
SCAN_TIMEFRAMES         = ["15m", "30m", "1H"]
MAX_SIGNALS_PER_RUN     = int(os.getenv("MAX_SIGNALS", "12"))
SETUP_SCORE_THRESHOLD   = 68
# 訂單流參數
CROSSLINE_BODY_RATIO       = 0.30
SWEEP_VOLUME_RATIO         = 1.8
SWEEP_CONSECUTIVE_MOVES    = 2
NEWS_COOLDOWN_MINUTES      = 60
ABSORPTION_VOL_MULTIPLIER  = 1.8
ABSORPTION_PRICE_THRESHOLD = 0.002
# v8 精度參數
VOLATILITY_HARD_LIMIT   = 0.035
ATR_SL_MULT             = 1.5
RSI_PERIOD              = 14
ADX_PERIOD              = 14
# 監控參數
ENTRY_TOLERANCE         = 0.002
ACTIVE_SIGNALS_FILE     = "active_signals.json"
TRADE_HISTORY_FILE      = "trade_history.json"
SIGNAL_EXPIRE_HOURS     = 24
# v9.1 新增參數
SIGNAL_COOLDOWN_HOURS       = 2
VWAP_PERIODS                = 50
MACD_FAST                   = 12
MACD_SLOW                   = 26
MACD_SIGNAL_PERIOD          = 9
# ✨ v9.1.7 調試參數
CONFIRM_TP_ON_CLOSE = os.getenv("CONFIRM_TP_ON_CLOSE", "false").lower() == "true"  # 🔧 預設 False
HEARTBEAT_MINUTE_WINDOW = 5
_DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
_news_cooldown: dict = {}
_SIGNAL_COOLDOWN: dict = {}

# ─────────────────────────────────────────────────────────
# 2. 工具 & 通知
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try: return float(val)
    except: return fallback

def send_tg(msg: str, parse_mode: str = "Markdown") -> bool:
    """發送 Telegram 訊息，帶重試機制"""
    if not TG_TOKEN or not CHAT_ID:
        logging.error("❌ TG_TOKEN 或 CHAT_ID 未設定！請檢查環境變數")
        return False
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode},
                timeout=15
            )
            if r.status_code == 200:
                logging.info("✅ Telegram 訊息發送成功")
                return True
            else:
                logging.warning(f"Telegram API 回傳錯誤: {r.status_code} - {r.text}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False
        except Exception as e:
            logging.error(f"Telegram 發送失敗 (嘗試 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            continue
    return False

def check_news_cooldown(instId: str) -> bool:
    return time.time() - _news_cooldown.get(instId, 0) >= NEWS_COOLDOWN_MINUTES * 60

def mark_news_event(instId: str):
    _news_cooldown[instId] = time.time()

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

# ─────────────────────────────────────────────────────────
# 2b. v9.1 輔助工具
# ─────────────────────────────────────────────────────────
def check_signal_cooldown(instId: str, side: str) -> bool:
    key = f"{instId}_{side}"
    last = _SIGNAL_COOLDOWN.get(key, 0)
    return (time.time() - last) >= SIGNAL_COOLDOWN_HOURS * 3600

def set_signal_cooldown(instId: str, side: str):
    _SIGNAL_COOLDOWN[f"{instId}_{side}"] = time.time()

def get_market_session() -> str:
    h = utc_now().hour
    if 13 <= h < 22: return "🌎 美盤"
    elif 7 <= h < 16: return "🌍 歐盤"
    elif 1 <= h < 8: return "🌏 亞盤"
    else: return "🌙 清淡"

def suggest_position_size(entry: float, sl: float,
                          account_size: float = 1000.0,
                          risk_pct: float = 0.01) -> str:
    try:
        sl_dist = abs(entry - sl)
        if sl_dist <= 0: return "─"
        sl_ratio = sl_dist / entry
        pos_usdt = account_size * risk_pct / sl_ratio
        leverage = pos_usdt / account_size
        return f"≈{pos_usdt:.0f}U (x{leverage:.1f} | 1%風控/{account_size:.0f}U)"
    except:
        return "─"

# ─────────────────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────────────────
def fetch_okx(instId: str, tf: str = "15m", limit: int = 150):
    try:
        url = (f"https://www.okx.com/api/v5/market/candles"
               f"?instId={instId}&bar={tf}&limit={limit}")
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0":
            return None
        df = pd.DataFrame(
            res["data"],
            columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"]
        )
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 30 else None
    except Exception as e:
        logging.warning(f"[{instId}/{tf}] 抓取失敗: {e}")
        return None

def fetch_okx_last_closed(instId: str, tf: str = "15m", limit: int = 5):
    """抓最近幾根「已收盤」的 K 線，用於 TP 收盤確認"""
    try:
        url = (f"https://www.okx.com/api/v5/market/candles"
               f"?instId={instId}&bar={tf}&limit={limit}")
        res = requests.get(url, timeout=8).json()
        if res.get("code") != "0":
            return None
        df = pd.DataFrame(
            res["data"],
            columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"]
        )
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 1 else None
    except Exception as e:
        logging.warning(f"[{instId}/{tf}] 收盤確認抓取失敗: {e}")
        return None

def fetch_ticker_price(instId: str) -> float:
    """獲取即時價格，帶重試"""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            res = requests.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
                timeout=5
            ).json()
            if res.get("code") == "0" and res.get("data"):
                price = float(res["data"][0]["last"])
                if price > 0:
                    return price
            logging.warning(f"獲取價格失敗: {res}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return 0.0
        except Exception as e:
            logging.warning(f"獲取價格異常 (嘗試 {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            continue
    return 0.0

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res["data"][0]["fundingRate"]) if res.get("data") else 0.0
    except: return 0.0

def fetch_ls_ratio(symbol: str) -> tuple:
    try:
        base = symbol.split("-")[0]
        res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}",
            timeout=5
        ).json()
        if res.get("data"):
            r = float(res["data"][0]["ratio"])
            return r, f"{r:.2f}"
        return 1.0, "N/A"
    except: return 1.0, "N/A"

def fetch_order_book(instId: str, depth: int = 20) -> tuple:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5
        ).json()
        if res.get("code") != "0" or not res.get("data"):
            return 1.0, "盤口均衡"
        data = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio = bid_vol / ask_vol
        if ratio >= 1.30: label = f"買盤強勢 ({ratio:.2f})"
        elif ratio >= 1.05: label = f"買盤略強 ({ratio:.2f})"
        elif ratio >= 0.95: label = f"盤口均衡 ({ratio:.2f})"
        elif ratio >= 0.77: label = f"賣盤略強 ({ratio:.2f})"
        else: label = f"賣盤強勢 ({ratio:.2f})"
        return ratio, label
    except: return 1.0, "盤口均衡"

def fetch_oi_analysis(instId: str) -> tuple:
    try:
        base = instId.split("-")[0]
        res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume"
            f"?instId={base}&period=1H&limit=6",
            timeout=8
        ).json()
        if res.get("code") != "0" or not res.get("data"):
            return 0.5, "OI─"
        oi_vals = [float(d[1]) for d in res["data"]][::-1]
        if len(oi_vals) < 4:
            return 0.5, "OI─"
        recent = sum(oi_vals[-2:]) / 2
        older = sum(oi_vals[:2]) / 2
        chg = (recent - older) / (older + 1e-10)
        if chg > 0.05: return 1.0, f"OI 持增 +{chg*100:.1f}%"
        elif chg > 0.01: return 0.7, f"OI 微增 +{chg*100:.1f}%"
        elif chg > -0.01: return 0.5, f"OI 持平"
        elif chg > -0.05: return 0.3, f"OI 微降 {chg*100:.1f}%"
        else: return 0.0, f"OI 下降 {chg*100:.1f}%"
    except Exception as e:
        logging.debug(f"OI 分析: {e}")
        return 0.5, "OI─"

# ─────────────────────────────────────────────────────────
# 4. 技術指標（略，與之前相同）
# ─────────────────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["h"] - df["l"]
    hc = np.abs(df["h"] - df["c"].shift())
    lc = np.abs(df["l"] - df["c"].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calculate_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> tuple:
    if len(df) < period + 2: return 0, "未知"
    h = df["h"].values.astype(float)
    l = df["l"].values.astype(float)
    c = df["c"].values.astype(float)
    n = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1]*(period-1)+tr[i]) / period
    hl2 = (h+l)/2.0
    bu = hl2 - mult*atr
    bd = hl2 + mult*atr
    fu = np.zeros(n); fd = np.zeros(n)
    trend = np.ones(n, dtype=int)
    fu[period]=bu[period]; fd[period]=bd[period]
    for i in range(period+1, n):
        fu[i]=bu[i] if bu[i]>fu[i-1] or c[i-1]<fu[i-1] else fu[i-1]
        fd[i]=bd[i] if bd[i]<fd[i-1] or c[i-1]>fd[i-1] else fd[i-1]
        if trend[i-1]==-1 and c[i]>fd[i-1]: trend[i]=1
        elif trend[i-1]==1 and c[i]<fu[i-1]: trend[i]=-1
        else: trend[i]=trend[i-1]
    if trend[-1]==1: return 1,"多頭"
    if trend[-1]==-1: return -1,"空頭"
    return 0,"未知"

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["c"].diff()
    gain = delta.where(delta>0, 0).rolling(period).mean()
    loss = (-delta.where(delta<0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100/(1+rs))

def calculate_adx(df: pd.DataFrame, period: int = 14) -> tuple:
    if len(df) < period*2+2: return 0.0, 0.0, 0.0
    h = df["h"].values.astype(float)
    l = df["l"].values.astype(float)
    c = df["c"].values.astype(float)
    n = len(df)
    tr = np.zeros(n); pdm = np.zeros(n); mdm = np.zeros(n)
    for i in range(1, n):
        h_diff = h[i]-h[i-1]; l_diff = l[i-1]-l[i]
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        pdm[i] = h_diff if h_diff>l_diff and h_diff>0 else 0
        mdm[i] = l_diff if l_diff>h_diff and l_diff>0 else 0
    atr_w = np.zeros(n); p_w = np.zeros(n); m_w = np.zeros(n)
    atr_w[period]=tr[1:period+1].sum()
    p_w[period] = pdm[1:period+1].sum()
    m_w[period] = mdm[1:period+1].sum()
    for i in range(period+1, n):
        atr_w[i] = atr_w[i-1]-atr_w[i-1]/period+tr[i]
        p_w[i] = p_w[i-1] - p_w[i-1]/period + pdm[i]
        m_w[i] = m_w[i-1] - m_w[i-1]/period + mdm[i]
    plus_di = 100*p_w/(atr_w+1e-10)
    minus_di = 100*m_w/(atr_w+1e-10)
    dx = 100*np.abs(plus_di-minus_di)/(plus_di+minus_di+1e-10)
    adx = np.zeros(n); s = 2*period
    if s < n:
        adx[s]=dx[period+1:s+1].mean()
        for i in range(s+1, n):
            adx[i]=(adx[i-1]*(period-1)+dx[i])/period
    return float(adx[-1]), float(plus_di[-1]), float(minus_di[-1])

def calculate_vwap(df: pd.DataFrame, periods: int = VWAP_PERIODS) -> float:
    data = df.tail(periods).copy()
    tp = (data["h"] + data["l"] + data["c"]) / 3.0
    vol = data["v"]
    cum_vol = vol.cumsum()
    vwap = (tp * vol).cumsum() / (cum_vol + 1e-10)
    val = float(vwap.iloc[-1])
    return val if not np.isnan(val) else float(data["c"].iloc[-1])

def analyze_vwap_position(df: pd.DataFrame, side: str) -> tuple:
    vwap = calculate_vwap(df)
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    if side == "LONG":
        if price < vwap - atr * 0.3: return 1.0, f"VWAP 之下 {vwap:.4f} ✅"
        elif price < vwap + atr * 0.3: return 0.7, f"VWAP 附近 {vwap:.4f}"
        elif price < vwap + atr * 1.0: return 0.4, f"VWAP 偏高 {vwap:.4f}"
        else: return 0.1, f"VWAP 大幅偏高 {vwap:.4f}"
    else:
        if price > vwap + atr * 0.3: return 1.0, f"VWAP 之上 {vwap:.4f} ✅"
        elif price > vwap - atr * 0.3: return 0.7, f"VWAP 附近 {vwap:.4f}"
        elif price > vwap - atr * 1.0: return 0.4, f"VWAP 偏低 {vwap:.4f}"
        else: return 0.1, f"VWAP 大幅偏低 {vwap:.4f}"

def calculate_macd(df: pd.DataFrame) -> tuple:
    close = df["c"]
    ema_fast = calculate_ema(close, MACD_FAST)
    ema_slow = calculate_ema(close, MACD_SLOW)
    macd_line = ema_fast - ema_slow
    signal = calculate_ema(macd_line, MACD_SIGNAL_PERIOD)
    histogram = macd_line - signal
    return macd_line, signal, histogram

def detect_macd_divergence(df: pd.DataFrame, side: str) -> tuple:
    if len(df) < MACD_SLOW + 20:
        return False, "MACD 數據不足"
    macd, _, _ = calculate_macd(df)
    lookback = 20
    macd_arr = macd.tail(lookback).values
    price_h = df["h"].tail(lookback).values
    price_l = df["l"].tail(lookback).values
    mid = lookback // 2
    if side == "LONG":
        prev_l = price_l[:mid].min(); curr_l = price_l[mid:].min()
        if curr_l < prev_l * 0.999:
            idx1 = int(np.argmin(price_l[:mid]))
            idx2 = mid + int(np.argmin(price_l[mid:]))
            m1 = macd_arr[idx1]; m2 = macd_arr[idx2]
            if m2 > m1 + abs(m1) * 0.05:
                return True, f"MACD 看漲背離 ({m2:.4f}>{m1:.4f})"
    else:
        prev_h = price_h[:mid].max(); curr_h = price_h[mid:].max()
        if curr_h > prev_h * 1.001:
            idx1 = int(np.argmax(price_h[:mid]))
            idx2 = mid + int(np.argmax(price_h[mid:]))
            m1 = macd_arr[idx1]; m2 = macd_arr[idx2]
            if m2 < m1 - abs(m1) * 0.05:
                return True, f"MACD 看跌背離 ({m2:.4f}<{m1:.4f})"
    return False, "無 MACD 背離"

# ─────────────────────────────────────────────────────────
# 5-11. 其他分析模組（略，與之前相同，為節省篇幅省略）
# ─────────────────────────────────────────────────────────
# （請保留原程式碼中的 detect_market_regime, adx_regime_bonus, detect_rsi_divergence,
#  get_btc_bias, get_4h_trend, check_extreme_volatility, calculate_dynamic_sl,
#  find_swing_points, detect_bos_choch, detect_market_structure, find_liquidity_pools,
#  find_order_blocks, find_fvg, check_ob_fvg_entry, detect_premium_discount,
#  detect_crossline, detect_active_sweep, detect_fishing_trap, detect_absorption,
#  calculate_cvd, interpret_ls_ratio, interpret_funding_rate, check_ob_direction,
#  detect_pa, detect_whale_zones, calculate_score 等函式）

# ─────────────────────────────────────────────────────────
# 12. 主掃描邏輯（略，與之前相同）
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str, htf_trend: str, fr: float, ls_f: float, ls_str: str,
                   ob_r: float, oi_sc: float, oi_lb: str, _cache: dict) -> list:
    # ...（保留原程式碼）...
    # 為節省篇幅，此處省略，請使用您之前提供的完整 scan_timeframe 函式
    return []

def scan_for_opportunity(instId: str) -> list:
    # ...（保留原程式碼）...
    return []

# ─────────────────────────────────────────────────────────
# 13. 掃描訊號格式化
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    # ...（保留原程式碼）...
    return ""

# ─────────────────────────────────────────────────────────
# 13b. 追蹤訊號格式化
# ─────────────────────────────────────────────────────────
def _progress_bar(hit_tp1: bool, hit_tp2: bool, hit_tp3: bool = False,
                  touched_tp1: bool = False, touched_tp2: bool = False) -> str:
    if hit_tp1: p1 = "🥇✅"
    elif touched_tp1: p1 = "🥇⚡"
    else: p1 = "🥇⏳"
    if hit_tp2: p2 = "🥈✅"
    elif touched_tp2: p2 = "🥈⚡"
    else: p2 = "🥈⏳"
    p3 = "🏆✅" if hit_tp3 else "🏆"
    return f"[ {p1}  {p2}  {p3} ]"

def format_alert(coin: str, side: str, alert_type: str,
                 price: float, entry: float, sl: float,
                 tp1: float, tp2: float, tp3: float,
                 new_sl: float = None, score: int = 0) -> str:
    arrow = "🟢" if side == "LONG" else "🔴"
    st = "多" if side == "LONG" else "空"
    sign = "+" if side == "LONG" else "-"
    
    if alert_type == "ENTRY":
        sl_pct = abs(entry - sl) / entry * 100
        tp1_pct = abs(tp1 - entry) / entry * 100
        tp2_pct = abs(tp2 - entry) / entry * 100
        tp3_pct = abs(tp3 - entry) / entry * 100
        sl_sign = "-" if side == "LONG" else "+"
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *進場提醒* #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 *價格到達進場區！*\n\n"
            f"📍 當前價 `{price:.4f}`\n"
            f"📌 進場價 `{entry:.4f}`\n"
            f"📊 評分 `{score}分`\n\n"
            f"─────────────────────────\n"
            f"🛑 止損 `{sl:.4f}` `{sl_sign}{sl_pct:.2f}%`\n"
            f"🥇 TP1 `{tp1:.4f}` `{sign}{tp1_pct:.2f}%` ⅓倉\n"
            f"🥈 TP2 `{tp2:.4f}` `{sign}{tp2_pct:.2f}%` ⅓倉\n"
            f"🏆 TP3 `{tp3:.4f}` `{sign}{tp3_pct:.2f}%` ⅓倉\n"
            f"─────────────────────────\n\n"
            f"💡 *三段止盈 + 動態追蹤止損已啟動*\n"
            f"   到 TP1 → SL 自動移至保本\n"
            f"   到 TP2 → SL 自動移至 TP1\n"
            f"   到 TP3 → 完美收割"
        )
    elif alert_type == "TP1":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        tp2_pct = abs(tp2 - entry) / entry * 100
        tp3_pct = abs(tp3 - entry) / entry * 100
        new_sl_str = f"`{new_sl:.4f}`" if new_sl else f"`{entry:.4f}`"
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP1 達標！* #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💹 當前 `{price:.4f}` `{sign}{pnl:.2f}%` `+1.0R`\n\n"
            f"進度 {_progress_bar(True, False, False)}\n\n"
            f"─────────────────────────\n"
            f"✅ TP1 `{tp1:.4f}` 已達成\n"
            f"🛡 SL 移至 {new_sl_str} *(保本)*\n"
            f"─────────────────────────\n\n"
            f"💡 *操作建議*\n"
            f"   • 平倉 ⅓ 部位鎖定獲利\n"
            f"   • 剩餘 ⅔ 續抱追擊\n\n"
            f"🎯 *下一目標*\n"
            f"   🥈 TP2 `{tp2:.4f}` `{sign}{tp2_pct:.2f}%`\n"
            f"   🏆 TP3 `{tp3:.4f}` `{sign}{tp3_pct:.2f}%`"
        )
    elif alert_type == "TP2":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        tp3_pct = abs(tp3 - entry) / entry * 100
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP2 達標！* #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💹 當前 `{price:.4f}` `{sign}{pnl:.2f}%` `+2.5R`\n\n"
            f"進度 {_progress_bar(True, True, False)}\n\n"
            f"─────────────────────────\n"
            f"✅ TP2 `{tp2:.4f}` 已達成\n"
            f"🛡 SL 移至 `{tp1:.4f}` *(鎖利 +1.0R)*\n"
            f"─────────────────────────\n\n"
            f"💡 *操作建議*\n"
            f"   • 再平倉 ⅓ 部位落袋\n"
            f"   • 剩餘 ⅓ 衝擊 TP3\n\n"
            f"🏆 *最終目標*\n"
            f"   TP3 `{tp3:.4f}` `{sign}{tp3_pct:.2f}%` 🚀"
        )
    elif alert_type == "TP3":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *TP3 完美收割！* #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💎 當前 `{price:.4f}` `{sign}{pnl:.2f}%` `+4.0R`\n\n"
            f"進度 {_progress_bar(True, True, True)}\n\n"
            f"─────────────────────────\n"
            f"🎉 *三段止盈全部達成！*\n"
            f"🏆 TP3 `{tp3:.4f}` 已達成\n"
            f"─────────────────────────\n\n"
            f"💡 建議 *立即平倉全部剩餘部位*\n"
            f"📊 本單表現 🌟🌟🌟 優秀\n\n"
            f"恭喜獲利 🎊"
        )
    elif alert_type == "SL":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        is_be = new_sl is not None and abs(new_sl - entry) < entry * 0.0001
        label = "保本止損" if is_be else "止損觸發"
        header_em = "🛡" if is_be else "🛑"
        sl_display = f"`{new_sl:.4f}`" if new_sl else f"`{sl:.4f}`"
        r_tag = "`0.0R`" if is_be else "`-1.0R`"
        outcome = ("💡 倉位已平倉於成本價 資金安全，等下一個機會 💪" if is_be 
                   else "⚠️ 倉位已止損出場 請遵守風控，莫加碼攤平")
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{header_em} *{label}* #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💹 當前 `{price:.4f}` `{pnl:+.2f}%` {r_tag}\n\n"
            f"─────────────────────────\n"
            f"{header_em} 止損價 {sl_display} 已觸發\n"
            f"─────────────────────────\n\n"
            f"{outcome}"
        )
    return ""

# ─────────────────────────────────────────────────────────
# 14. WinRateTracker（略，與之前相同）
# ─────────────────────────────────────────────────────────
class WinRateTracker:
    # ...（保留原程式碼）...
    pass

# ─────────────────────────────────────────────────────────
# 15. SignalTracker — 🔧 關鍵修復版
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE, win_tracker: WinRateTracker = None):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.signals = self._load()
        self.win_tracker = win_tracker
        self.last_run_transitions = 0

    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.signals, f, ensure_ascii=False, indent=2)

    def add(self, opp: dict, active: bool = False) -> str:
        key = f"{opp['instId']}_{opp['side']}_{opp['tf']}_{int(time.time())}"
        now = time.time()
        with self._lock:
            self.signals[key] = {
                "instId": opp["instId"], "side": opp["side"], "tf": opp["tf"],
                "entry": opp["entry"], "sl": opp["sl"], "sl_orig": opp["sl"],
                "tp1": opp["tp1"], "tp2": opp["tp2"], "tp3": opp["tp3"],
                "score": opp["score"], "grade": opp["grade"],
                "status": "ACTIVE" if active else "PENDING",
                "hit_tp1": False, "hit_tp2": False,
                "touched_tp1": False, "touched_tp2": False,
                "created": now, "activated_at": now if active else None,
                "hit_tp1_at": None, "hit_tp2_at": None,
            }
            self._save()
        logging.info(f"📌 新增追蹤: {key} [{'ACTIVE' if active else 'PENDING'}]")
        return key

    def remove(self, key: str):
        with self._lock:
            self.signals.pop(key, None)
            self._save()

    def update(self, key: str, **kwargs):
        with self._lock:
            if key in self.signals:
                self.signals[key].update(kwargs)
                self._save()

    def list_active(self) -> list:
        with self._lock:
            return list(self.signals.items())

    def _close(self, sig: dict, close_price: float, close_type: str):
        if self.win_tracker:
            try:
                self.win_tracker.record(
                    coin=sig["instId"].split("-")[0], side=sig["side"],
                    tf=sig["tf"], entry=sig["entry"], close_price=close_price,
                    close_type=close_type, score=sig.get("score", 0),
                )
            except Exception as e:
                logging.error(f"WinRateTracker.record: {e}")

    def _is_close_confirmed(self, sig: dict, level: float) -> bool:
        """🔧 v9.1.7: 如果 CONFIRM_TP_ON_CLOSE=False，直接回傳 True"""
        if not CONFIRM_TP_ON_CLOSE:
            logging.debug(f"  [{sig['instId']}] CONFIRM_TP_ON_CLOSE=False，即時觸發")
            return True
        # 否則抓收盤 K 確認
        df = fetch_okx_last_closed(sig["instId"], tf=sig["tf"], limit=3)
        if df is None or len(df) < 1:
            logging.warning(f"  [{sig['instId']}] 收盤確認抓不到 K 線，暫不觸發")
            return False
        last_close = float(df["c"].iloc[-1])
        if sig["side"] == "LONG":
            confirmed = last_close >= level
        else:
            confirmed = last_close <= level
        logging.debug(f"  [{sig['instId']}] 收盤確認: close={last_close:.4f}, level={level:.4f}, confirmed={confirmed}")
        return confirmed

    def check_one(self, key: str, sig: dict) -> bool:
        """🔧 v9.1.7: 增加詳細 TP 檢查日誌"""
        try:
            price = fetch_ticker_price(sig["instId"])
            if price <= 0:
                logging.warning(f"  [{key}] 無法取得即時價格，跳過檢查")
                return False

            coin, side, status = sig["instId"].split("-")[0], sig["side"], sig["status"]
            entry, sl = sig["entry"], sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]

            logging.debug(f"[{key}] 檢查: price={price:.4f}, status={status}, TP1={tp1:.4f}, TP2={tp2:.4f}")

            # ── PENDING 狀態 ─────────────
            if status == "PENDING":
                age_h = (time.time() - sig["created"]) / 3600
                if age_h > SIGNAL_EXPIRE_HOURS:
                    send_tg(f"⏰ *訊號過期* #{coin} {side}\n進場 `{entry:.4f}` 超過 {SIGNAL_EXPIRE_HOURS}h 未觸發")
                    logging.info(f"  [過期] {key}")
                    self.last_run_transitions += 1
                    return True
                in_entry_zone = (
                    (side == "LONG" and entry*(1-ENTRY_TOLERANCE*3) <= price <= entry*(1+ENTRY_TOLERANCE)) or
                    (side == "SHORT" and entry*(1-ENTRY_TOLERANCE) <= price <= entry*(1+ENTRY_TOLERANCE*3))
                )
                if in_entry_zone:
                    self.update(key, status="ACTIVE", activated_at=time.time())
                    msg = format_alert(coin, side, "ENTRY", price, entry, sl, tp1, tp2, tp3, score=sig["score"])
                    if send_tg(msg):
                        logging.info(f"  [進場] {key} @ {price:.4f} - 通知已發送")
                    else:
                        logging.error(f"  [進場] {key} - 通知發送失敗")
                    self.last_run_transitions += 1
                return False

            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False

            # ── 止損觸發（最優先）─────────────
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit:
                is_be = (status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001)
                close_type = "BE" if is_be else "SL"
                msg = format_alert(coin, side, "SL", price, entry, sig["sl_orig"], tp1, tp2, tp3, new_sl=(entry if is_be else sl))
                if send_tg(msg):
                    logging.info(f"  [{close_type}] {key} @ {price:.4f} (BE={is_be}) - 通知已發送")
                else:
                    logging.error(f"  [{close_type}] {key} - 通知發送失敗")
                self._close(sig, price, close_type)
                self.last_run_transitions += 1
                return True

            # ── TP3 達成 ─────────────────────
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if tp3_hit:
                msg = format_alert(coin, side, "TP3", price, entry, sig["sl_orig"], tp1, tp2, tp3)
                if send_tg(msg):
                    logging.info(f"  [TP3] {key} @ {price:.4f} ✅ 完美收割 - 通知已發送")
                else:
                    logging.error(f"  [TP3] {key} - 通知發送失敗")
                self._close(sig, tp3, "TP3")
                self.last_run_transitions += 1
                return True

            # ── TP2 達成（需收盤確認）────────
            tp2_touched = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if tp2_touched and not sig.get("hit_tp2"):
                if not sig.get("touched_tp2"):
                    self.update(key, touched_tp2=True)
                    logging.info(f"  [TP2 觸及] {key} @ {price:.4f} 等收盤確認 (CONFIRM={CONFIRM_TP_ON_CLOSE})")
                if not self._is_close_confirmed(sig, tp2):
                    logging.info(f"  [TP2 待確認] {key} 本根 K 尚未收盤於 TP2 之外")
                    return False
                now = time.time()
                if not sig.get("hit_tp1"):
                    self.update(key, hit_tp1=True, touched_tp1=True, hit_tp1_at=now)
                    self._close(sig, tp1, "TP1")
                self.update(key, hit_tp2=True, sl=tp1, status="TRAIL", hit_tp2_at=now)
                msg = format_alert(coin, side, "TP2", price, entry, sig["sl_orig"], tp1, tp2, tp3, new_sl=tp1)
                if send_tg(msg):
                    logging.info(f"  [TP2] {key} @ {price:.4f} → SL 移至 TP1={tp1:.4f} - 通知已發送")
                else:
                    logging.error(f"  [TP2] {key} - 通知發送失敗")
                self._close(sig, tp2, "TP2")
                self.last_run_transitions += 1
                return False

            # ── TP1 達成（需收盤確認）────────
            tp1_touched = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if tp1_touched and not sig.get("hit_tp1"):
                if not sig.get("touched_tp1"):
                    self.update(key, touched_tp1=True)
                    logging.info(f"  [TP1 觸及] {key} @ {price:.4f} 等收盤確認 (CONFIRM={CONFIRM_TP_ON_CLOSE})")
                if not self._is_close_confirmed(sig, tp1):
                    logging.info(f"  [TP1 待確認] {key} 本根 K 尚未收盤於 TP1 之外")
                    return False
                self.update(key, hit_tp1=True, sl=entry, status="BE", hit_tp1_at=time.time())
                msg = format_alert(coin, side, "TP1", price, entry, sig["sl_orig"], tp1, tp2, tp3, new_sl=entry)
                if send_tg(msg):
                    logging.info(f"  [TP1] {key} @ {price:.4f} → SL 移至保本={entry:.4f} - 通知已發送")
                else:
                    logging.error(f"  [TP1] {key} - 通知發送失敗")
                self._close(sig, tp1, "TP1")
                self.last_run_transitions += 1
                return False

            return False
        except Exception as e:
            logging.error(f"check_one [{key}] 錯誤: {e}\n{traceback.format_exc()}")
            return False

    def check_all(self):
        self.last_run_transitions = 0
        to_remove = []
        for key, sig in self.list_active():
            try:
                if self.check_one(key, sig):
                    to_remove.append(key)
            except Exception as e:
                logging.error(f"check_all 檢查 [{key}] 錯誤: {e}")
        for key in to_remove:
            self.remove(key)
        if to_remove:
            logging.info(f"  移除 {len(to_remove)} 筆已關閉訊號")

    def status_summary(self) -> str:
        items = self.list_active()
        if not items:
            return "📭 *目前無追蹤中訊號*\n\n掃描器持續運作中，有機會會立即通知 🔍"
        st_map = {
            "PENDING": ("⏳", "PENDING · 等待進場"),
            "ACTIVE": ("🔵", "ACTIVE · 持倉中"),
            "BE": ("🛡", "BREAKEVEN · 已保本"),
            "TRAIL": ("🔁", "TRAILING · 鎖利中"),
        }
        lines = [f"📋 *追蹤中訊號 ({len(items)} 筆)*", f"━━━━━━━━━━━━━━━━━━━━━━━━", ""]
        for idx, (key, s) in enumerate(items, 1):
            coin = s["instId"].split("-")[0]
            arrow = "🟢" if s["side"] == "LONG" else "🔴"
            side = s["side"]
            em, st_desc = st_map.get(s["status"], ("❓", s["status"]))
            live = fetch_ticker_price(s["instId"])
            entry = s["entry"]
            if s["status"] == "PENDING":
                if live > 0:
                    dist_pct = (live - entry) / entry * 100
                    in_zone = (
                        (side == "LONG" and entry*(1-ENTRY_TOLERANCE*3) <= live <= entry*(1+ENTRY_TOLERANCE)) or
                        (side == "SHORT" and entry*(1-ENTRY_TOLERANCE) <= live <= entry*(1+ENTRY_TOLERANCE*3))
                    )
                    zone_tag = " ⚡ *已在進場區*" if in_zone else ""
                    price_line = f" 📍 當前 `{live:.4f}` (距離 `{dist_pct:+.2f}%`){zone_tag}"
                else:
                    price_line = " 📍 當前 `—`"
            else:
                if live > 0:
                    pnl = ((live - entry) / entry * 100) if side == "LONG" else ((entry - live) / entry * 100)
                    sign = "+" if pnl >= 0 else ""
                    price_line = f" 💹 當前 `{live:.4f}` `{sign}{pnl:.2f}%`"
                else:
                    price_line = " 💹 當前 `—`"
            if s["status"] == "BE":
                sl_label = f" 🛡 止損 `{s['sl']:.4f}` *(保本)*"
            elif s["status"] == "TRAIL":
                sl_label = f" 🛡 止損 `{s['sl']:.4f}` *(鎖利於 TP1)*"
            else:
                sl_label = f" 🛑 止損 `{s['sl']:.4f}`"
            progress = _progress_bar(
                s.get("hit_tp1", False), s.get("hit_tp2", False), False,
                touched_tp1=s.get("touched_tp1", False), touched_tp2=s.get("touched_tp2", False),
            )
            lines.append(f"{em} *#{coin} · {arrow} {side} · {s['tf']}* [{s['score']}分]")
            lines.append(f" {st_desc}")
            lines.append(price_line)
            lines.append(f" 📌 進場 `{entry:.4f}`")
            lines.append(sl_label)
            lines.append(f" 🥇 TP1 `{s['tp1']:.4f}`")
            lines.append(f" 🥈 TP2 `{s['tp2']:.4f}`")
            lines.append(f" 🏆 TP3 `{s['tp3']:.4f}`")
            lines.append(f" 進度 {progress}")
            if idx < len(items):
                lines.extend(["", "─────────────────────────", ""])
        lines.extend(["", f"━━━━━━━━━━━━━━━━━━━━━━━━", f"🤖 Alpha Oracle v9.1.7 動態追蹤中"])
        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 16-19. 監控迴圈、主函式等（略，與之前相同，確保 --interval 預設 10 秒）
# ─────────────────────────────────────────────────────────
def monitor_loop(tracker: SignalTracker, interval: int = 10, stop_event=None):
    logging.info(f"監控迴圈啟動，間隔 {interval}s")
    check_count = 0
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            active = tracker.list_active()
            check_count += 1
            if active:
                logging.info(f"【檢查 #{check_count}】監控中... {len(active)} 筆訊號")
                tracker.check_all()
            else:
                if check_count % 6 == 0:
                    logging.info(f"【檢查 #{check_count}】無追蹤訊號，等待新機會...")
        except Exception as e:
            logging.error(f"monitor_loop 錯誤: {e}\n{traceback.format_exc()}")
        time.sleep(interval)

def run_monitor_once(tracker: SignalTracker, push_status: bool = None) -> int:
    """🔧 v9.1.7: push_status 預設 None 時，強制推播（方便除錯）"""
    active = tracker.list_active()
    n = len(active)
    if n == 0:
        logging.info("monitor_once: 無追蹤中訊號")
        return 0
    logging.info(f"monitor_once: 檢查 {n} 筆追蹤中訊號")
    try:
        tracker.check_all()
    except Exception as e:
        logging.error(f"monitor_once 錯誤: {e}")
    remaining = tracker.list_active()
    transitions = getattr(tracker, "last_run_transitions", 0)
    # 🔧 除錯模式：只要有訊號就推播
    if _DEBUG_MODE or push_status is None:
        should_push = bool(remaining)
    elif push_status is False:
        should_push = False
    else:
        should_push = remaining and (transitions > 0 or utc_now().minute < HEARTBEAT_MINUTE_WINDOW)
    if should_push:
        reason = "調試模式" if _DEBUG_MODE else ("狀態變動" if transitions > 0 else "整點心跳")
        logging.info(f"monitor_once: 推播狀態摘要（{reason}）")
        status_msg = tracker.status_summary()
        if send_tg(status_msg):
            logging.info("✅ 狀態摘要已發送")
        else:
            logging.error("❌ 狀態摘要發送失敗")
    else:
        logging.info(f"monitor_once: 靜默（transitions={transitions}）")
    logging.info(f"monitor_once: 完成，剩餘 {len(remaining)} 筆")
    return n

def main():
    parser = argparse.ArgumentParser(description="Alpha Oracle v9.1.7")
    parser.add_argument("--mode", default="all",
                        choices=["scan","monitor","monitor_once","loop","all","daily_report","monthly_report"])
    parser.add_argument("--interval", type=int, default=10, help="監控間隔秒數（預設 10 秒）")
    parser.add_argument("--loop-interval", type=int, default=900)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    logging.info("=" * 60)
    logging.info("🤖 Alpha Oracle v9.1.7 啟動（調試版）")
    logging.info(f"📋 模式: {args.mode}")
    logging.info(f"⏱ 監控間隔: {args.interval}秒")
    logging.info(f"🎯 TP 收盤確認: {CONFIRM_TP_ON_CLOSE} (env: CONFIRM_TP_ON_CLOSE)")
    logging.info(f"🐛 除錯模式: {_DEBUG_MODE} (env: DEBUG_MODE)")
    logging.info(f"🔑 TG_TOKEN 設定: {'✅' if TG_TOKEN else '❌'}")
    logging.info(f"💬 CHAT_ID 設定: {'✅' if CHAT_ID else '❌'}")
    logging.info("=" * 60)

    # 🔧 啟動時測試 Telegram 連線
    if TG_TOKEN and CHAT_ID:
        test_msg = "🔧 Alpha Oracle v9.1.7 啟動測試 - 價格監控運作中"
        if send_tg(test_msg):
            logging.info("✅ Telegram 連線測試成功")
        else:
            logging.error("❌ Telegram 連線測試失敗！請檢查 TG_TOKEN/CHAT_ID")

    win_tracker = WinRateTracker(TRADE_HISTORY_FILE)
    tracker = SignalTracker(ACTIVE_SIGNALS_FILE, win_tracker=win_tracker)

    if args.status:
        summary = tracker.status_summary()
        print(summary)
        send_tg(summary)
        return
    if args.mode == "daily_report":
        msg = win_tracker.daily_report()
        print(msg); send_tg(msg); return
    if args.mode == "monthly_report":
        msg = win_tracker.monthly_report()
        print(msg); send_tg(msg); return
    if args.mode == "scan":
        run_scan(tracker); return
    if args.mode == "monitor_once":
        run_monitor_once(tracker); return
    if args.mode == "monitor":
        try: monitor_loop(tracker, interval=args.interval)
        except KeyboardInterrupt: logging.info("監控停止")
        return
    if args.mode == "loop":
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_loop, args=(tracker, args.interval, stop_ev), daemon=True)
        t.start()
        try:
            while True:
                run_scan(tracker)
                time.sleep(args.loop_interval)
        except KeyboardInterrupt:
            logging.info("迴圈停止"); stop_ev.set()
        return
    # all 模式
    run_scan(tracker)
    try: monitor_loop(tracker, interval=args.interval)
    except KeyboardInterrupt: logging.info("停止")

# ─────────────────────────────────────────────────────────
# 執行入口
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"崩潰: {e}")
        traceback.print_exc()
        sys.exit(1)
