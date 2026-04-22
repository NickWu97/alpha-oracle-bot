#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v9.1.6 — 狀態同步修復 + 收盤確認 + 狀態推播節流
══════════════════════════════════════════════════════════════════════
v9.1.6 新增：
  🐛 FIX: 掃描當下價格已在進場區 → 訊號直接存 ACTIVE（避免 PENDING 錯位）
  🐛 FIX: 掃描結尾再 check_all 一次（避免新訊號狀態延遲）
  ✨ NEW: TP1/TP2 收盤確認（CONFIRM_TP_ON_CLOSE）→ 去除 wick 誤觸發
         只有「已收盤 K 線的收盤價」穿越 TP1/TP2，才觸發 SL 移動
  ✨ NEW: 狀態摘要推播節流（monitor_once 只在「有變動」或「整點心跳」推播）
         減少 Telegram 噪音，從 288 則/天 → ~12 則/天
  ✨ NEW: status_summary 對 PENDING 訊號若當前價在進場區，顯示 ⚡ 已在進場區
v9.1.4 保留：
  ✅ send_tg/fetch_ticker_price 重試機制 + 詳細通知 log
  ✅ monitor_loop 預設 10 秒間隔
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
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("alpha_oracle.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

SCAN_TIMEFRAMES = ["15m", "30m", "1H"]
MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "12"))
SETUP_SCORE_THRESHOLD = 68

# 訂單流參數
CROSSLINE_BODY_RATIO = 0.30
SWEEP_VOLUME_RATIO = 1.8
SWEEP_CONSECUTIVE_MOVES = 2
NEWS_COOLDOWN_MINUTES = 60
ABSORPTION_VOL_MULTIPLIER = 1.8
ABSORPTION_PRICE_THRESHOLD = 0.002

# v8 精度參數
VOLATILITY_HARD_LIMIT = 0.035
ATR_SL_MULT = 1.5
RSI_PERIOD = 14
ADX_PERIOD = 14

# 監控參數
ENTRY_TOLERANCE = 0.002
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
SIGNAL_EXPIRE_HOURS = 24

# v9.1 新增參數
SIGNAL_COOLDOWN_HOURS = 2
VWAP_PERIODS = 50
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9

# ✨ v9.1.6 新增參數
CONFIRM_TP_ON_CLOSE = os.getenv("CONFIRM_TP_ON_CLOSE", "true").lower() == "true"
HEARTBEAT_MINUTE_WINDOW = 5

_news_cooldown: dict = {}
_SIGNAL_COOLDOWN: dict = {}

# ─────────────────────────────────────────────────────────
# 2. 工具 & 通知
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:
        return float(val)
    except:
        return fallback

def send_tg(msg: str, parse_mode: str = "Markdown") -> bool:
    """發送 Telegram 訊息，帶重試機制"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("TG_TOKEN / CHAT_ID 未設定")
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
    if 13 <= h < 22:
        return "🌎 美盤"
    elif 7 <= h < 16:
        return "🌍 歐盤"
    elif 1 <= h < 8:
        return "🌏 亞盤"
    else:
        return "🌙 清淡"

def suggest_position_size(entry: float, sl: float, account_size: float = 1000.0, risk_pct: float = 0.01) -> str:
    try:
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return "─"
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
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0":
            return None
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"])
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 30 else None
    except Exception as e:
        logging.warning(f"[{instId}/{tf}] 抓取失敗: {e}")
        return None

def fetch_okx_last_closed(instId: str, tf: str = "15m", limit: int = 5):
    """v9.1.6：抓最近幾根「已收盤」的 K 線，用於 TP 收盤確認"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=8).json()
        if res.get("code") != "0":
            return None
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"])
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
            res = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}", timeout=5).json()
            if res.get("code") == "0" and res.get("data"):
                price = float(res["data"][0]["last"])
                if price > 0:
                    return price
            if attempt < max_retries - 1:
                time.sleep(1)
            return 0.0
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
            continue
    return 0.0

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        return float(res["data"][0]["fundingRate"]) if res.get("data") else 0.0
    except:
        return 0.0

def fetch_ls_ratio(symbol: str) -> tuple:
    try:
        base = symbol.split("-")[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}", timeout=5).json()
        if res.get("data"):
            r = float(res["data"][0]["ratio"])
            return r, f"{r:.2f}"
        return 1.0, "N/A"
    except:
        return 1.0, "N/A"

def fetch_order_book(instId: str, depth: int = 20) -> tuple:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5).json()
        if res.get("code") != "0" or not res.get("data"):
            return 1.0, "盤口均衡"
        data = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio = bid_vol / ask_vol
        if ratio >= 1.30:
            label = f"買盤強勢 ({ratio:.2f})"
        elif ratio >= 1.05:
            label = f"買盤略強 ({ratio:.2f})"
        elif ratio >= 0.95:
            label = f"盤口均衡 ({ratio:.2f})"
        elif ratio >= 0.77:
            label = f"賣盤略強 ({ratio:.2f})"
        else:
            label = f"賣盤強勢 ({ratio:.2f})"
        return ratio, label
    except:
        return 1.0, "盤口均衡"

def fetch_oi_analysis(instId: str) -> tuple:
    try:
        base = instId.split("-")[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?instId={base}&period=1H&limit=6", timeout=8).json()
        if res.get("code") != "0" or not res.get("data"):
            return 0.5, "OI─"
        oi_vals = [float(d[1]) for d in res["data"]][::-1]
        if len(oi_vals) < 4:
            return 0.5, "OI─"
        recent = sum(oi_vals[-2:]) / 2
        older = sum(oi_vals[:2]) / 2
        chg = (recent - older) / (older + 1e-10)
        if chg > 0.05:
            return 1.0, f"OI 持增 +{chg*100:.1f}%"
        elif chg > 0.01:
            return 0.7, f"OI 微增 +{chg*100:.1f}%"
        elif chg > -0.01:
            return 0.5, f"OI 持平"
        elif chg > -0.05:
            return 0.3, f"OI 微降 {chg*100:.1f}%"
        else:
            return 0.0, f"OI 下降 {chg*100:.1f}%"
    except Exception as e:
        logging.debug(f"OI 分析: {e}")
        return 0.5, "OI─"

# ─────────────────────────────────────────────────────────
# 4. 技術指標
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
    if len(df) < period + 2:
        return 0, "未知"
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
    fu = np.zeros(n)
    fd = np.zeros(n)
    trend = np.ones(n, dtype=int)
    fu[period] = bu[period]
    fd[period] = bd[period]
    for i in range(period+1, n):
        fu[i] = bu[i] if bu[i]>fu[i-1] or c[i-1]<fu[i-1] else fu[i-1]
        fd[i] = bd[i] if bd[i]<fd[i-1] or c[i-1]>fd[i-1] else fd[i-1]
        if trend[i-1]==-1 and c[i]>fd[i-1]:
            trend[i] = 1
        elif trend[i-1]==1 and c[i]<fu[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]
    if trend[-1] == 1:
        return 1, "多頭"
    if trend[-1] == -1:
        return -1, "空頭"
    return 0, "未知"

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["c"].diff()
    gain = delta.where(delta>0, 0).rolling(period).mean()
    loss = (-delta.where(delta<0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100/(1+rs))

def calculate_adx(df: pd.DataFrame, period: int = 14) -> tuple:
    if len(df) < period*2+2:
        return 0.0, 0.0, 0.0
    h = df["h"].values.astype(float)
    l = df["l"].values.astype(float)
    c = df["c"].values.astype(float)
    n = len(df)
    tr = np.zeros(n)
    pdm = np.zeros(n)
    mdm = np.zeros(n)
    for i in range(1, n):
        h_diff = h[i]-h[i-1]
        l_diff = l[i-1]-l[i]
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        pdm[i] = h_diff if h_diff>l_diff and h_diff>0 else 0
        mdm[i] = l_diff if l_diff>h_diff and l_diff>0 else 0
    atr_w = np.zeros(n)
    p_w = np.zeros(n)
    m_w = np.zeros(n)
    atr_w[period] = tr[1:period+1].sum()
    p_w[period] = pdm[1:period+1].sum()
    m_w[period] = mdm[1:period+1].sum()
    for i in range(period+1, n):
        atr_w[i] = atr_w[i-1]-atr_w[i-1]/period+tr[i]
        p_w[i] = p_w[i-1]-p_w[i-1]/period+pdm[i]
        m_w[i] = m_w[i-1]-m_w[i-1]/period+mdm[i]
    plus_di = 100*p_w/(atr_w+1e-10)
    minus_di = 100*m_w/(atr_w+1e-10)
    dx = 100*np.abs(plus_di-minus_di)/(plus_di+minus_di+1e-10)
    adx = np.zeros(n)
    s = 2*period
    if s < n:
        adx[s] = dx[period+1:s+1].mean()
        for i in range(s+1, n):
            adx[i] = (adx[i-1]*(period-1)+dx[i])/period
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
        if price < vwap - atr * 0.3:
            return 1.0, f"VWAP 之下 {vwap:.4f} ✅"
        elif price < vwap + atr * 0.3:
            return 0.7, f"VWAP 附近 {vwap:.4f}"
        elif price < vwap + atr * 1.0:
            return 0.4, f"VWAP 偏高 {vwap:.4f}"
        else:
            return 0.1, f"VWAP 大幅偏高 {vwap:.4f}"
    else:
        if price > vwap + atr * 0.3:
            return 1.0, f"VWAP 之上 {vwap:.4f} ✅"
        elif price > vwap - atr * 0.3:
            return 0.7, f"VWAP 附近 {vwap:.4f}"
        elif price > vwap - atr * 1.0:
            return 0.4, f"VWAP 偏低 {vwap:.4f}"
        else:
            return 0.1, f"VWAP 大幅偏低 {vwap:.4f}"

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
        prev_l = price_l[:mid].min()
        curr_l = price_l[mid:].min()
        if curr_l < prev_l * 0.999:
            idx1 = int(np.argmin(price_l[:mid]))
            idx2 = mid + int(np.argmin(price_l[mid:]))
            m1 = macd_arr[idx1]
            m2 = macd_arr[idx2]
            if m2 > m1 + abs(m1) * 0.05:
                return True, f"MACD 看漲背離 ({m2:.4f}>{m1:.4f})"
    else:
        prev_h = price_h[:mid].max()
        curr_h = price_h[mid:].max()
        if curr_h > prev_h * 1.001:
            idx1 = int(np.argmax(price_h[:mid]))
            idx2 = mid + int(np.argmax(price_h[mid:]))
            m1 = macd_arr[idx1]
            m2 = macd_arr[idx2]
            if m2 < m1 - abs(m1) * 0.05:
                return True, f"MACD 看跌背離 ({m2:.4f}<{m1:.4f})"
    return False, "無 MACD 背離"

# ─────────────────────────────────────────────────────────
# 5. 精度分析模組
# ─────────────────────────────────────────────────────────
def detect_market_regime(df: pd.DataFrame) -> dict:
    adx, pdi, mdi = calculate_adx(df, ADX_PERIOD)
    if adx < 20:
        regime, sc = "震盪市", 0.4
    elif adx < 25:
        regime, sc = "弱趨勢", 0.6
    elif adx < 40:
        regime, sc = "強趨勢", 0.9
    else:
        regime, sc = "極強趨勢", 1.0
    trend_dir = "上升趨勢" if pdi > mdi else "下降趨勢"
    return {"regime": regime, "adx": adx, "trend_dir": trend_dir, "score": sc, "plus_di": pdi, "minus_di": mdi}

def adx_regime_bonus(regime: dict, side: str) -> tuple:
    adx = regime["adx"]
    is_uptrend = regime["trend_dir"] == "上升趨勢"
    if adx >= 25:
        if (side=="LONG" and is_uptrend) or (side=="SHORT" and not is_uptrend):
            return 3, f"ADX 趨勢{adx:.0f} 順勢 +3"
        return 0, f"ADX 趨勢{adx:.0f} 逆勢"
    else:
        if (side=="LONG" and not is_uptrend) or (side=="SHORT" and is_uptrend):
            return 3, f"ADX 震盪{adx:.0f} 均值回歸 +3"
        return 1, f"ADX 震盪{adx:.0f}"

def detect_rsi_divergence(df: pd.DataFrame, side: str) -> tuple:
    rsi = calculate_rsi(df, RSI_PERIOD)
    if len(rsi) < 20:
        return False, "RSI 數據不足", float(rsi.iloc[-1]) if len(rsi)>0 else 50.0
    lookback = 20
    rsi_arr = rsi.tail(lookback).values
    price_h = df["h"].tail(lookback).values
    price_l = df["l"].tail(lookback).values
    cur_rsi = float(rsi.iloc[-1])
    mid = lookback // 2
    if side == "LONG":
        prev_l = price_l[:mid].min()
        curr_l = price_l[mid:].min()
        idx1 = int(np.argmin(price_l[:mid]))
        idx2 = mid + int(np.argmin(price_l[mid:]))
        rsi_1 = rsi_arr[idx1]
        rsi_2 = rsi_arr[idx2]
        if curr_l < prev_l * 0.999 and rsi_2 > rsi_1 + 3.0:
            return True, f"看漲背離 RSI={cur_rsi:.1f}", cur_rsi
    else:
        prev_h = price_h[:mid].max()
        curr_h = price_h[mid:].max()
        idx1 = int(np.argmax(price_h[:mid]))
        idx2 = mid + int(np.argmax(price_h[mid:]))
        rsi_1 = rsi_arr[idx1]
        rsi_2 = rsi_arr[idx2]
        if curr_h > prev_h * 1.001 and rsi_2 < rsi_1 - 3.0:
            return True, f"看跌背離 RSI={cur_rsi:.1f}", cur_rsi
    return False, f"無背離 RSI={cur_rsi:.1f}", cur_rsi

def get_btc_bias(side: str, _cache: dict) -> tuple:
    if "BTC_1H" not in _cache:
        _cache["BTC_1H"] = fetch_okx("BTC-USDT-SWAP", tf="1H", limit=20)
    df_btc = _cache["BTC_1H"]
    if df_btc is None:
        return 0.5, "BTC 數據不足"
    st_val, _ = calculate_supertrend(df_btc)
    chg = (df_btc["c"].iloc[-1]-df_btc["c"].iloc[-6]) / (df_btc["c"].iloc[-6]+1e-10)
    if side == "LONG":
        if st_val==1 and chg>0:
            return 1.0, f"BTC 多頭({chg*100:.1f}%)"
        elif st_val==1:
            return 0.7, f"BTC ST 多弱({chg*100:.1f}%)"
        elif st_val==-1 and chg<-0.02:
            return 0.1, f"BTC 大跌({chg*100:.1f}%)"
        else:
            return 0.5, f"BTC 中性({chg*100:.1f}%)"
    else:
        if st_val==-1 and chg<0:
            return 1.0, f"BTC 空頭({chg*100:.1f}%)"
        elif st_val==-1:
            return 0.7, f"BTC ST 空弱({chg*100:.1f}%)"
        elif st_val==1 and chg>0.02:
            return 0.1, f"BTC 大漲({chg*100:.1f}%)"
        else:
            return 0.5, f"BTC 中性({chg*100:.1f}%)"

def get_4h_trend(instId: str, side: str, _cache: dict) -> tuple:
    key = f"{instId}_4H"
    if key not in _cache:
        _cache[key] = fetch_okx(instId, tf="4H", limit=60)
    df4h = _cache[key]
    if df4h is None:
        return 0.5, "4H 數據不足"
    st4, _ = calculate_supertrend(df4h)
    ema21 = calculate_ema(df4h["c"], 21).iloc[-1]
    price = df4h["c"].iloc[-1]
    if side == "LONG":
        if st4==1 and price>ema21:
            return 1.0, "4H 多頭排列"
        elif st4==1:
            return 0.7, "4H ST 多頭"
        elif price>ema21:
            return 0.5, "4H EMA 多偏"
        else:
            return 0.2, "4H 偏空"
    else:
        if st4==-1 and price<ema21:
            return 1.0, "4H 空頭排列"
        elif st4==-1:
            return 0.7, "4H ST 空頭"
        elif price<ema21:
            return 0.5, "4H EMA 空偏"
        else:
            return 0.2, "4H 偏多"

def check_extreme_volatility(df: pd.DataFrame) -> tuple:
    atr = calculate_atr(df)
    price = df["c"].iloc[-1]
    ratio = atr / (price + 1e-10)
    if ratio > VOLATILITY_HARD_LIMIT:
        return False, f"極端波動 ATR={ratio*100:.2f}%"
    return True, f"波動正常 ATR={ratio*100:.2f}%"

def calculate_dynamic_sl(entry: float, side: str, atr: float, support: float = None, resistance: float = None) -> float:
    base = entry - atr*1.5 if side=="LONG" else entry + atr*1.5
    if side=="LONG" and support:
        if abs(entry - support) < atr*2.5:
            base = min(base, support - atr*0.5)
    if side=="SHORT" and resistance:
        if abs(resistance - entry) < atr*2.5:
            base = max(base, resistance + atr*0.5)
    min_dist = atr * 1.5
    if abs(entry - base) < min_dist:
        base = entry - min_dist if side=="LONG" else entry + min_dist
    return base

# ─────────────────────────────────────────────────────────
# 6. 擺動點 & 市場結構
# ─────────────────────────────────────────────────────────
def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    data = df.tail(lookback).reset_index(drop=True)
    sh_p, sl_p, sh_i, sl_i = [], [], [], []
    for i in range(n, len(data)-n):
        wh = data["h"].iloc[i-n:i+n+1]
        wl = data["l"].iloc[i-n:i+n+1]
        if data["h"].iloc[i] == wh.max():
            sh_p.append(data["h"].iloc[i])
            sh_i.append(i)
        if data["l"].iloc[i] == wl.min():
            sl_p.append(data["l"].iloc[i])
            sl_i.append(i)
    return sh_p, sl_p, sh_i, sl_i

def detect_bos_choch(df: pd.DataFrame, side: str) -> tuple:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=80)
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    result, score = "無明顯結構", 0.0
    if side=="LONG":
        if sl and df["l"].iloc[-4:-1].min() < sl[-1]-atr*0.1 and price>sl[-1]:
            result, score = f"CHoCH 掃低反彈 @ {sl[-1]:.4f}", 0.90
        elif sh and not score:
            if price>sh[-1]:
                result, score = f"BOS 向上突破 {sh[-1]:.4f}", 0.80
            elif len(sh)>=2 and price>sh[-2]:
                result, score = f"CHoCH 潛在轉折 {sh[-2]:.4f}", 0.55
    else:
        if sh and df["h"].iloc[-4:-1].max() > sh[-1]+atr*0.1 and price<sh[-1]:
            result, score = f"CHoCH 掃高回落 @ {sh[-1]:.4f}", 0.90
        elif sl and not score:
            if price<sl[-1]:
                result, score = f"BOS 向下跌破 {sl[-1]:.4f}", 0.80
            elif len(sl)>=2 and price<sl[-2]:
                result, score = f"CHoCH 潛在轉折 {sl[-2]:.4f}", 0.55
    return result, score

def detect_market_structure(df: pd.DataFrame, side: str) -> str:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=60)
    has_w = len(sl)>=2 and sl[-2]>0 and abs(sl[-2]-sl[-1])/sl[-2]<0.015
    has_m = len(sh)>=2 and sh[-2]>0 and abs(sh[-2]-sh[-1])/sh[-2]<0.015
    if side=="LONG":
        if has_w:
            return "W 底反轉"
        if has_m:
            return "M 頭壓制"
    else:
        if has_m:
            return "M 頭反轉"
        if has_w:
            return "W 底支撐"
    recent = df.tail(20)
    slope = (recent["c"].iloc[-1]-recent["c"].iloc[0])/(recent["c"].iloc[0]+1e-10)
    if slope>0.025:
        return "上升趨勢延續"
    if slope<-0.025:
        return "下降趨勢延續"
    return "區間盤整"

# ─────────────────────────────────────────────────────────
# 7. 流動性獵取
# ─────────────────────────────────────────────────────────
def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    sh, sl, _, _ = find_swing_points(df, n=2, lookback=lookback)
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    res = dict(pools=[], sweep_detected=False, sweep_desc="", sweep_score=0.0,
               eqh=None, eql=None, nearest_bsl=None, nearest_ssl=None)
    for i in range(len(sh)-1, 0, -1):
        if abs(sh[i]-sh[i-1])/(sh[i-1]+1e-10)<0.003:
            res["eqh"]=(sh[i-1]+sh[i])/2
            res["pools"].append(f"EQH 等高 {res['eqh']:.4f}")
            break
    for i in range(len(sl)-1, 0, -1):
        if abs(sl[i]-sl[i-1])/(sl[i-1]+1e-10)<0.003:
            res["eql"]=(sl[i-1]+sl[i])/2
            res["pools"].append(f"EQL 等低 {res['eql']:.4f}")
            break
    bsl_c = [h for h in sh if h>price]
    ssl_c = [l for l in sl if l<price]
    if bsl_c:
        res["nearest_bsl"] = min(bsl_c)
    if ssl_c:
        res["nearest_ssl"] = max(ssl_c)
    recent = df.tail(5)
    if side=="LONG":
        for lvl, is_eq in ([(res["eql"],True)] if res["eql"] else []) + ([(res["nearest_ssl"],False)] if res["nearest_ssl"] else []):
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k = recent.iloc[i]
                if k["l"]<lvl-atr*0.05 and k["c"]>lvl:
                    wick = (lvl-k["l"])/(atr+1e-10)
                    res["sweep_detected"] = True
                    res["sweep_desc"] = f"{'EQL' if is_eq else 'SSL'} 掃除反彈 {k['l']:.4f}→{k['c']:.4f}"
                    res["sweep_score"] = 0.95 if is_eq else min(0.55+wick*0.08, 0.90)
                    break
            if res["sweep_detected"]:
                break
    else:
        for lvl, is_eq in ([(res["eqh"],True)] if res["eqh"] else []) + ([(res["nearest_bsl"],False)] if res["nearest_bsl"] else []):
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k = recent.iloc[i]
                if k["h"]>lvl+atr*0.05 and k["c"]<lvl:
                    wick = (k["h"]-lvl)/(atr+1e-10)
                    res["sweep_detected"] = True
                    res["sweep_desc"] = f"{'EQH' if is_eq else 'BSL'} 掃除回落 {k['h']:.4f}→{k['c']:.4f}"
                    res["sweep_score"] = 0.95 if is_eq else min(0.55+wick*0.08, 0.90)
                    break
            if res["sweep_detected"]:
                break
    return res

# ─────────────────────────────────────────────────────────
# 8. Order Block & FVG
# ─────────────────────────────────────────────────────────
def find_order_blocks(df: pd.DataFrame, side: str, lookback: int = 60) -> list:
    data = df.tail(lookback).reset_index(drop=True)
    obs = []
    price = data["c"].iloc[-1]
    atr = calculate_atr(data)
    for i in range(2, len(data)-3):
        c = data.iloc[i]
        if side=="LONG":
            if c["c"]<c["o"]:
                mv = data["h"].iloc[i+1:i+4].max()-c["h"]
                if mv>atr*1.5:
                    ob = dict(high=c["h"], low=c["l"], mid=(c["h"]+c["l"])/2, strength=mv/(atr+1e-10))
                    if ob["high"]<price*1.005:
                        obs.append(ob)
        else:
            if c["c"]>c["o"]:
                mv = c["l"]-data["l"].iloc[i+1:i+4].min()
                if mv>atr*1.5:
                    ob = dict(high=c["h"], low=c["l"], mid=(c["h"]+c["l"])/2, strength=mv/(atr+1e-10))
                    if ob["low"]>price*0.995:
                        obs.append(ob)
    obs.sort(key=lambda x: x["strength"], reverse=True)
    return obs[:2]

def find_fvg(df: pd.DataFrame, side: str, lookback: int = 40) -> list:
    data = df.tail(lookback).reset_index(drop=True)
    fvgs = []
    price = data["c"].iloc[-1]
    for i in range(2, len(data)):
        if side=="LONG":
            bot, top = data["h"].iloc[i-2], data["l"].iloc[i]
            if top>bot and bot<price:
                fvgs.append(dict(top=top, bottom=bot, mid=(top+bot)/2, size=top-bot))
        else:
            top, bot = data["l"].iloc[i-2], data["h"].iloc[i]
            if bot<top and top>price:
                fvgs.append(dict(top=top, bottom=bot, mid=(top+bot)/2, size=top-bot))
    return fvgs[-2:] if fvgs else []

def check_ob_fvg_entry(df: pd.DataFrame, side: str, atr: float) -> tuple:
    price = df["c"].iloc[-1]
    obs = find_order_blocks(df, side)
    fvgs = find_fvg(df, side)
    at_ob = False
    at_fvg = False
    ob_d = "無 OB"
    fvg_d = "無 FVG"
    ez = price
    for ob in obs:
        if ob["low"]-atr*0.5 <= price <= ob["high"]+atr*0.5:
            at_ob = True
            ob_d = f"在 OB [{ob['low']:.4f}~{ob['high']:.4f}] 強{ob['strength']:.1f}x"
            ez = ob["mid"]
            break
        else:
            ob_d = f"OB [{ob['low']:.4f}~{ob['high']:.4f}]"
    for fvg in reversed(fvgs):
        if fvg["bottom"]-atr*0.3 <= price <= fvg["top"]+atr*0.3:
            at_fvg = True
            fvg_d = f"在 FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
            if not at_ob:
                ez = fvg["mid"]
            break
        else:
            fvg_d = f"FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
    return at_ob, at_fvg, ob_d, fvg_d, ez

def detect_premium_discount(df: pd.DataFrame, side: str) -> tuple:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=50)
    price = df["c"].iloc[-1]
    if not sh or not sl:
        return "無法判斷", 0.5
    hi = max(sh[-2:]) if len(sh)>=2 else sh[-1]
    lo = min(sl[-2:]) if len(sl)>=2 else sl[-1]
    rng = hi - lo
    if rng <= 0:
        return "無法判斷", 0.5
    fib = (price-lo)/rng
    if side=="LONG":
        if fib <= 0.35:
            return f"Discount {fib*100:.0f}% 做多優質", 1.0
        elif fib <= 0.5:
            return f"均衡偏低 {fib*100:.0f}%", 0.6
        elif fib <= 0.65:
            return f"均衡偏高 {fib*100:.0f}%", 0.3
        else:
            return f"Premium {fib*100:.0f}% 做多不利", 0.0
    else:
        if fib >= 0.65:
            return f"Premium {fib*100:.0f}% 做空優質", 1.0
        elif fib >= 0.5:
            return f"均衡偏高 {fib*100:.0f}%", 0.6
        elif fib >= 0.35:
            return f"均衡偏低 {fib*100:.0f}%", 0.3
        else:
            return f"Discount {fib*100:.0f}% 做空不利", 0.0

# ─────────────────────────────────────────────────────────
# 9. 訂單流
# ─────────────────────────────────────────────────────────
def detect_crossline(df: pd.DataFrame, lookback: int = 15):
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k["c"]-k["o"])
        rng = k["h"]-k["l"]+1e-10
        if body < CROSSLINE_BODY_RATIO*rng:
            uw = k["h"]-max(k["c"], k["o"])
            dw = min(k["c"], k["o"])-k["l"]
            pot = "SHORT" if uw>dw*1.5 else ("LONG" if dw>uw*1.5 else "NEUTRAL")
            dist = len(df)-1-i
            return dict(price=k["c"], high=k["h"], low=k["l"], body_ratio=body/rng,
                        potential_side=pot, distance=dist,
                        desc=f"十字線@{k['c']:.4f}({pot},{dist}根前)")
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> tuple:
    if len(df)<8:
        return False, 0.0, "數據不足"
    recent = df.tail(8)
    vol_ma = df["v"].tail(20).mean()
    vol_sc = recent.iloc[-1]["v"]/(vol_ma+1e-10)
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"量能不足({vol_sc:.1f}x)"
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side=="LONG" and recent["c"].iloc[i]>recent["c"].iloc[i-1]:
            moves += 1
        elif side=="SHORT" and recent["c"].iloc[i]<recent["c"].iloc[i-1]:
            moves += 1
        else:
            break
    if moves >= SWEEP_CONSECUTIVE_MOVES:
        return True, min(vol_sc/3.0, 1.0), f"主動掃單 連續{moves}根 {vol_sc:.1f}x"
    return False, 0.0, f"無連續掃單({moves}根)"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df)<6:
        return False
    recent = df.tail(6)
    vol_ma = df["v"].tail(20).mean()
    mv = abs(recent["c"].iloc[-1]-recent["c"].iloc[0])/(recent["c"].iloc[0]+1e-10)
    return mv>=0.005 and recent["v"].iloc[-1]<0.75*vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> tuple:
    if len(df)<15:
        return False, "無吸收"
    recent = df.tail(5)
    vol_ma = df["v"].tail(20).mean()
    avg3 = recent["v"].iloc[-3:].mean()
    chg = abs(recent["c"].iloc[-1]-recent["c"].iloc[-4])/(recent["c"].iloc[-4]+1e-10)
    if avg3>ABSORPTION_VOL_MULTIPLIER*vol_ma and chg<ABSORPTION_PRICE_THRESHOLD:
        return True, f"吸收 量{avg3/vol_ma:.1f}x 價動{chg*100:.2f}%"
    return False, "無吸收"

# ─────────────────────────────────────────────────────────
# 10. 市場情緒
# ─────────────────────────────────────────────────────────
def calculate_cvd(df: pd.DataFrame, periods: int = 50) -> tuple:
    data = df.tail(periods).copy()
    delta = np.where(data["c"]>data["o"], data["v"],
                     np.where(data["c"]<data["o"], -data["v"], 0))
    cvd = np.cumsum(delta)
    cur = cvd[-1]
    slope = cur - (cvd[-10] if len(cvd)>=10 else cvd[0])
    if slope>0 and cur>0:
        lb, sc = f"買盤累積 CVD+{cur:,.0f}", 1.0
    elif slope>0 and cur<0:
        lb, sc = f"CVD 底部翻正 (吸籌)", 0.65
    elif slope<0 and cur<0:
        lb, sc = f"賣盤累積 CVD{cur:,.0f}", 1.0
    elif slope<0 and cur>0:
        lb, sc = f"CVD 頂部翻負 (出貨)", 0.65
    else:
        lb, sc = f"CVD 持平", 0.3
    return cur, slope, lb, sc

def interpret_ls_ratio(ratio: float, side: str) -> tuple:
    if ratio>=2.5:
        senti = f"極度多頭擁擠({ratio:.2f}) 逆向偏空"
    elif ratio>=1.8:
        senti = f"多頭擁擠({ratio:.2f}) 謹慎做多"
    elif ratio>=1.2:
        senti = f"略偏多頭({ratio:.2f})"
    elif ratio>=0.8:
        senti = f"均衡({ratio:.2f})"
    elif ratio>=0.5:
        senti = f"空頭擁擠({ratio:.2f}) 謹慎做空"
    else:
        senti = f"極度空頭擁擠({ratio:.2f}) 逆向偏多"
    if side=="LONG":
        sc = 1.0 if ratio<0.8 else (0.7 if ratio<1.2 else (0.4 if ratio<1.8 else 0.1))
    else:
        sc = 1.0 if ratio>2.0 else (0.7 if ratio>1.5 else (0.4 if ratio>1.0 else 0.1))
    return sc, senti

def interpret_funding_rate(fr: float, side: str) -> tuple:
    p = fr*100
    if side=="LONG":
        if fr<-0.0003:
            return 1.0, f"費率極佳{p:.4f}%(空頭付費)"
        elif fr<0.0001:
            return 0.8, f"費率友善{p:.4f}%"
        elif fr<0.0003:
            return 0.5, f"費率尚可{p:.4f}%"
        elif fr<0.0008:
            return 0.2, f"費率不佳{p:.4f}%"
        else:
            return 0.0, f"費率禁入{p:.4f}%"
    else:
        if fr>0.0008:
            return 1.0, f"費率極佳{p:.4f}%(多頭付費)"
        elif fr>0.0003:
            return 0.8, f"費率友善{p:.4f}%"
        elif fr>0.0001:
            return 0.5, f"費率尚可{p:.4f}%"
        elif fr>-0.0003:
            return 0.2, f"費率不佳{p:.4f}%"
        else:
            return 0.0, f"費率禁入{p:.4f}%"

def check_ob_direction(side: str, ob_r: float) -> tuple:
    if side=="LONG":
        if ob_r>=1.30:
            return 1.0, f"買盤強勢({ob_r:.2f})"
        elif ob_r>=1.05:
            return 0.7, f"買盤略強({ob_r:.2f})"
        elif ob_r>=0.95:
            return 0.3, f"盤口均衡({ob_r:.2f})"
        else:
            return 0.0, f"賣盤主導，做多風險({ob_r:.2f})"
    else:
        if ob_r<=0.77:
            return 1.0, f"賣盤強勢({ob_r:.2f})"
        elif ob_r<=0.95:
            return 0.7, f"賣盤略強({ob_r:.2f})"
        elif ob_r<=1.05:
            return 0.3, f"盤口均衡({ob_r:.2f})"
        else:
            return 0.0, f"買盤主導，做空風險({ob_r:.2f})"

def detect_pa(df: pd.DataFrame, side: str) -> tuple:
    sigs = []
    for i in range(len(df)-1, max(len(df)-6,0), -1):
        k = df.iloc[i]
        body = abs(k["c"]-k["o"])
        rng = k["h"]-k["l"]+1e-10
        uw = k["h"]-max(k["c"], k["o"])
        dw = min(k["c"], k["o"])-k["l"]
        bp = body/rng
        if side=="SHORT" and uw>=body*2.0 and dw<=body*0.5:
            sigs.append(f"空頭流星線({min(uw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="LONG" and dw>=body*2.0 and uw<=body*0.5:
            sigs.append(f"多頭錘子線({min(dw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="SHORT" and uw/rng>0.40 and k["c"]<k["o"]:
            sigs.append(f"壓力拒絕 (上影{uw/rng*100:.0f}%)@{k['c']:.4f}")
        if side=="LONG" and dw/rng>0.40 and k["c"]>k["o"]:
            sigs.append(f"支撐拒絕 (下影{dw/rng*100:.0f}%)@{k['c']:.4f}")
        if bp>=0.70 and ((side=="LONG" and k["c"]>k["o"]) or (side=="SHORT" and k["c"]<k["o"])):
            sigs.append(f"{'多' if side=='LONG' else '空'}頭動量棒({bp*100:.0f}%)@{k['c']:.4f}")
    sigs = sigs[:3]
    sc = 0.6 if len(sigs)>=3 else (0.4 if len(sigs)>=2 else (0.2 if sigs else 0.0))
    last = df.iloc[-1]
    body = abs(last["c"]-last["o"])
    rng = last["h"]-last["l"]+1e-10
    if body/rng>0.70:
        sc += 0.20
    if (side=="LONG" and last["c"]>last["o"]) or (side=="SHORT" and last["c"]<last["o"]):
        sc += 0.20
    sc = min(sc, 1.0)
    lb = "強 PA" if sc>=0.65 else ("弱 PA" if sc>=0.40 else "無 PA")
    return sc*100, lb, sigs

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    zones = []
    vm = df["v"].rolling(20).mean()
    vs = df["v"].rolling(20).std()
    for i in range(max(len(df)-10,0), len(df)):
        if df["v"].iloc[i] > vm.iloc[i]+2*vs.iloc[i]:
            if df["c"].iloc[i]>df["o"].iloc[i] and side=="LONG":
                zones.append(f"主力吸籌 {df['c'].iloc[i]:.4f}")
            elif df["c"].iloc[i]<df["o"].iloc[i] and side=="SHORT":
                zones.append(f"主力派發 {df['c'].iloc[i]:.4f}")
    hi = df["h"].iloc[-20:].max()
    lo = df["l"].iloc[-20:].min()
    zones.append(f"{'多頭清算' if side=='SHORT' else '空頭清算'} {hi if side=='SHORT' else lo:.4f}")
    return zones[:2]

# ─────────────────────────────────────────────────────────
# 11. 評分系統
# ─────────────────────────────────────────────────────────
def calculate_score(p: dict) -> tuple:
    sc = 0.0
    bd = []
    side = p["side"]
    htf = p.get("htf_trend", "UNKNOWN")
    if htf == side:
        sc += 20
        bd.append("HTF+20")
    elif htf in ("NEUTRAL", "UNKNOWN"):
        sc += 8
        bd.append("HTF+8")
    else:
        sc += 0
        bd.append("HTF+0")
    at_ob = p.get("at_ob", False)
    at_fvg = p.get("at_fvg", False)
    if at_ob and at_fvg:
        sc += 18
        bd.append("OB+FVG+18")
    elif at_ob:
        sc += 15
        bd.append("OB+15")
    elif at_fvg:
        sc += 12
        bd.append("FVG+12")
    pts = round(p.get("sweep_score", 0)*18)
    sc += pts
    if pts:
        bd.append(f"掃除+{pts}")
    pts = round(p.get("active_sweep_score", 0)*13)
    sc += pts
    if pts:
        bd.append(f"主動掃+{pts}")
    pts = round(p.get("crossline_score", 0)*8)
    sc += pts
    if pts:
        bd.append(f"十字+{pts}")
    pts = round(p.get("absorption_score", 0)*7)
    sc += pts
    if pts:
        bd.append(f"吸收+{pts}")
    pts = round(p.get("cvd_score", 0)*12)
    sc += pts
    bd.append(f"CVD+{pts}")
    pts = round(p.get("ls_score", 0)*8)
    sc += pts
    bd.append(f"LS+{pts}")
    pts = round(p.get("fr_score", 0)*5)
    sc += pts
    bd.append(f"FR+{pts}")
    pts = round(p.get("ob_dir_score", 0)*5)
    sc += pts
    bd.append(f"盤口+{pts}")
    if p.get("bos_score", 0) >= 0.75:
        sc += 5
        bd.append("BOS+5")
    pts = round(p.get("trend_4h_score", 0)*5)
    if pts:
        sc += pts
        bd.append(f"4H+{pts}")
    if p.get("has_rsi_divergence", False):
        sc += 5
        bd.append("RSI+5")
    pts = round(p.get("btc_score", 0)*3)
    if pts:
        sc += pts
        bd.append(f"BTC+{pts}")
    adx_b = p.get("adx_bonus", 0)
    if adx_b:
        sc += adx_b
        bd.append(f"ADX+{adx_b}")
    if p.get("pd_score", 0) >= 0.7:
        sc += 3
        bd.append("PD+3")
    vwap_pts = round(p.get("vwap_score", 0.0) * 5)
    if vwap_pts:
        sc += vwap_pts
        bd.append(f"VWAP+{vwap_pts}")
    oi_pts = round(p.get("oi_score", 0.0) * 4)
    if oi_pts:
        sc += oi_pts
        bd.append(f"OI+{oi_pts}")
    if p.get("has_macd_divergence", False):
        sc += 4
        bd.append("MACD 背離+4")
    if htf not in (side, "NEUTRAL", "UNKNOWN"):
        sc -= 15
        bd.append("HTF 逆-15")
    if p.get("fr_score", 1) == 0.0:
        sc -= 10
        bd.append("FR 禁-10")
    if p.get("ob_dir_score", 1) == 0.0:
        sc -= 10
        bd.append("盤口反-10")
    sc = max(0, min(round(sc), 100))
    if sc >= 88:
        grade = "A+ 極強"
    elif sc >= 75:
        grade = "A  強力"
    elif sc >= 65:
        grade = "B+ 觀望"
    elif sc >= 55:
        grade = "B  偏弱"
    else:
        grade = "C  跳過"
    return sc, grade, bd

# ─────────────────────────────────────────────────────────
# 12. 主掃描邏輯
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str, htf_trend: str, fr: float, ls_f: float, ls_str: str,
                   ob_r: float, oi_sc: float, oi_lb: str, _cache: dict) -> list:
    df = fetch_okx(instId, tf=tf, limit=150)
    if df is None or len(df) < 50:
        return []
    vol_ok, vol_msg = check_extreme_volatility(df)
    if not vol_ok:
        logging.info(f"  [{instId}/{tf}] {vol_msg}")
        return []
    atr = calculate_atr(df)
    _, st_lb = calculate_supertrend(df)
    regime = detect_market_regime(df)
    cl = detect_crossline(df)
    abs_b, abs_d = detect_absorption(df, "LONG")
    has_rsi_long, rsi_d_long, rsi_v = detect_rsi_divergence(df, "LONG")
    has_rsi_short, rsi_d_short, _ = detect_rsi_divergence(df, "SHORT")
    opportunities = []
    for side in ["LONG", "SHORT"]:
        ob_dir_sc, ob_dir_lb = check_ob_direction(side, ob_r)
        fr_sc, fr_lb = interpret_funding_rate(fr, side)
        if detect_fishing_trap(df, side):
            continue
        cvd_cur, cvd_sl, cvd_lb, cvd_sc_raw = calculate_cvd(df)
        cvd_aligned = (side=="LONG" and cvd_sl>0) or (side=="SHORT" and cvd_sl<0)
        eff_cvd_sc = cvd_sc_raw if cvd_aligned else cvd_sc_raw*0.25
        liq = find_liquidity_pools(df, side)
        bos_desc, bos_sc = detect_bos_choch(df, side)
        at_ob, at_fvg, ob_d, fvg_d, ez = check_ob_fvg_entry(df, side, atr)
        pd_lb, pd_sc = detect_premium_discount(df, side)
        pa_sc, pa_lb, pa_sigs = detect_pa(df, side)
        structure = detect_market_structure(df, side)
        whale_zones = detect_whale_zones(df, side)
        ls_sc, ls_lb = interpret_ls_ratio(ls_f, side)
        as_bool, as_sc, as_d = detect_active_sweep(df, side)
        vwap_sc, vwap_lb = analyze_vwap_position(df, side)
        has_macd, macd_d = detect_macd_divergence(df, side)
        cl_sc = 0.0
        if cl:
            pot = cl["potential_side"]
            if pot == side or pot == "NEUTRAL":
                cl_sc = max(0.0, 1.0 - cl["distance"]/10) * 0.6 + 0.4
        has_rsi = has_rsi_long if side=="LONG" else has_rsi_short
        rsi_d = rsi_d_long if side=="LONG" else rsi_d_short
        t4h_sc, t4h_lb = get_4h_trend(instId, side, _cache)
        btc_sc, btc_lb = get_btc_bias(side, _cache)
        adx_bonus, adx_lb = adx_regime_bonus(regime, side)
        ab_sc = 0.8 if abs_b else 0.0
        params = dict(
            side=side, htf_trend=htf_trend, at_ob=at_ob, at_fvg=at_fvg,
            sweep_score=liq["sweep_score"], active_sweep_score=as_sc,
            crossline_score=cl_sc, absorption_score=ab_sc, cvd_score=eff_cvd_sc,
            ls_score=ls_sc, fr_score=fr_sc, ob_dir_score=ob_dir_sc,
            bos_score=bos_sc, trend_4h_score=t4h_sc, has_rsi_divergence=has_rsi,
            btc_score=btc_sc, adx_bonus=adx_bonus, pd_score=pd_sc,
            vwap_score=vwap_sc, oi_score=oi_sc, has_macd_divergence=has_macd,
        )
        score, grade, bd = calculate_score(params)
        if score < SETUP_SCORE_THRESHOLD:
            logging.info(f"  [{instId}/{tf}/{side}] {score}分 < {SETUP_SCORE_THRESHOLD}，跳過")
            continue
        price = df["c"].iloc[-1]
        sh, sl_pts, _, _ = find_swing_points(df, n=2, lookback=30)
        support = max([s for s in sl_pts if s<price], default=None)
        resistance = min([h for h in sh if h>price], default=None)
        if liq["sweep_detected"]:
            entry = price
        elif at_ob or at_fvg:
            entry = ez
        elif cl:
            entry = cl["low"] if side=="LONG" else cl["high"]
        elif side=="LONG" and liq["nearest_ssl"]:
            entry = liq["nearest_ssl"]*1.001
        elif side=="SHORT" and liq["nearest_bsl"]:
            entry = liq["nearest_bsl"]*0.999
        else:
            entry = price
        # ✨ v9.1.2 方向校驗：抓即時價做 sanity check
        live = fetch_ticker_price(instId) or price
        tol_px = 0.0005
        if side == "LONG" and entry > live * (1 + tol_px):
            logging.info(f"  [校正/{instId}/{tf}] LONG 進場 {entry:.4f} > 當前 {live:.4f}，改為當前價")
            entry = live
        elif side == "SHORT" and entry < live * (1 - tol_px):
            logging.info(f"  [校正/{instId}/{tf}] SHORT 進場 {entry:.4f} < 當前 {live:.4f}，改為當前價")
            entry = live
        atr_ratio = atr / (live + 1e-10)
        deviation = abs(live - entry) / (live + 1e-10)
        if deviation > max(atr_ratio * 0.8, 0.008):
            logging.info(f"  [略過/{instId}/{tf}/{side}] 即時價 {live:.4f} 偏離進場 {entry:.4f} 過遠 ({deviation*100:.2f}%)")
            continue
        sl_price = calculate_dynamic_sl(entry, side, atr, support, resistance)
        risk = abs(entry - sl_price)
        tp1 = entry+risk if side=="LONG" else entry-risk
        tp2 = entry+risk*2.5 if side=="LONG" else entry-risk*2.5
        tp3 = entry+risk*4.0 if side=="LONG" else entry-risk*4.0
        pos_hint = suggest_position_size(entry, sl_price)
        opp = dict(
            instId=instId, side=side, tf=tf,
            entry=entry, sl=sl_price, tp1=tp1, tp2=tp2, tp3=tp3,
            price=price, atr=atr, structure=structure, bos_desc=bos_desc,
            at_ob=at_ob, at_fvg=at_fvg, ob_d=ob_d, fvg_d=fvg_d,
            pd_lb=pd_lb, liq=liq, crossline=cl, as_bool=as_bool, as_d=as_d,
            abs_bool=abs_b, abs_desc=abs_d, cvd_lb=cvd_lb,
            ls_str=ls_str, ls_lb=ls_lb, fr_lb=fr_lb, ob_dir_lb=ob_dir_lb,
            pa_sc=pa_sc, pa_lb=pa_lb, pa_sigs=pa_sigs, whale_zones=whale_zones,
            htf_trend=htf_trend, st_lb=st_lb, regime=regime,
            has_rsi=has_rsi, rsi_d=rsi_d, rsi_v=rsi_v,
            t4h_lb=t4h_lb, btc_lb=btc_lb, adx_lb=adx_lb, vol_msg=vol_msg,
            score=score, grade=grade, breakdown=bd,
            lev="10x~20x" if atr/price<0.015 else "3x~5x",
            vwap_lb=vwap_lb, oi_lb=oi_lb,
            has_macd=has_macd, macd_d=macd_d,
            pos_hint=pos_hint,
            session=get_market_session(),
        )
        opportunities.append(opp)
    return opportunities

def scan_for_opportunity(instId: str) -> list:
    _cache = {}
    htf_df = fetch_okx(instId, tf="1H", limit=60)
    htf_trend_str = "UNKNOWN"
    if htf_df is not None:
        v, _ = calculate_supertrend(htf_df)
        htf_trend_str = "LONG" if v==1 else ("SHORT" if v==-1 else "NEUTRAL")
        _cache[f"{instId}_1H"] = htf_df
    fr = fetch_funding_rate(instId)
    ls_f, ls_str = fetch_ls_ratio(instId)
    ob_r, _ = fetch_order_book(instId)
    oi_sc, oi_lb = fetch_oi_analysis(instId)
    all_opps = []
    for tf in SCAN_TIMEFRAMES:
        try:
            opps = scan_timeframe(instId, tf, htf_trend_str, fr, ls_f, ls_str,
                                  ob_r, oi_sc, oi_lb, _cache)
            all_opps.extend(opps)
        except Exception as e:
            logging.error(f"  [{instId}/{tf}] {e}")
    seen = {}
    for opp in all_opps:
        k = f"{opp['side']}_{opp['tf']}"
        if k not in seen or opp["score"] > seen[k]["score"]:
            seen[k] = opp
    return list(seen.values())

# ─────────────────────────────────────────────────────────
# 13. 掃描訊號格式化
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    coin = opp["instId"].split("-")[0]
    is_long = opp["side"] == "LONG"
    arrow = "🟢" if is_long else "🔴"
    dir_txt = "LONG" if is_long else "SHORT"
    sign = "+" if is_long else "-"
    sl_sign = "-" if is_long else "+"
    htf_e = {"LONG":"🟢","SHORT":"🔴","NEUTRAL":"⚪","UNKNOWN":"⚪"}.get(opp["htf_trend"], "⚪")
    entry = opp["entry"]
    sl_pct = abs(entry - opp["sl"]) / entry * 100
    tp1_pct = abs(opp["tp1"] - entry) / entry * 100
    tp2_pct = abs(opp["tp2"] - entry) / entry * 100
    tp3_pct = abs(opp["tp3"] - entry) / entry * 100
    liq = opp["liq"]
    regime = opp["regime"]
    session = opp.get("session", get_market_session())
    vwap_lb = opp.get("vwap_lb", "")
    oi_lb = opp.get("oi_lb", "")
    pos_hint = opp.get("pos_hint", "─")
    top_bd = [x for x in opp["breakdown"] if not x.endswith("+0")][:5]
    bd_line = "  ".join(top_bd)
    grade_icon = {"S":"🏆","A":"⭐","B":"✅","C":"📊"}.get(opp.get("grade","C"), "📊")
    triggers = []
    if liq["sweep_detected"]:
        triggers.append(f"💧 {liq['sweep_desc']}")
    if opp["at_ob"]:
        triggers.append(f"🟦 {opp['ob_d']}")
    if opp["at_fvg"]:
        triggers.append(f"🟩 {opp['fvg_d']}")
    if opp["bos_desc"] not in ("無明顯結構", ""):
        triggers.append(f"🏗 {opp['bos_desc']}")
    if opp["as_bool"]:
        triggers.append(f"⚡ {opp['as_d']}")
    if opp["has_rsi"]:
        triggers.append(f"📉 {opp['rsi_d']}")
    if opp.get("has_macd"):
        triggers.append(f"〽️ {opp['macd_d']}")
    if not triggers:
        triggers.append("⚪ 等待進場區確認")
    trigger_txt = "\n".join(f"  • {t}" for t in triggers[:4])
    liq_parts = []
    if liq["nearest_bsl"]:
        liq_parts.append(f"BSL `{liq['nearest_bsl']:.2f}`")
    if liq["nearest_ssl"]:
        liq_parts.append(f"SSL `{liq['nearest_ssl']:.2f}`")
    if liq["eqh"]:
        liq_parts.append(f"EQH `{liq['eqh']:.2f}`")
    if liq["eql"]:
        liq_parts.append(f"EQL `{liq['eql']:.2f}`")
    liq_line = "  ·  ".join(liq_parts) if liq_parts else "─"
    ctx = []
    ctx.append(f"ADX {regime['adx']:.0f} {regime['regime']}")
    if vwap_lb:
        ctx.append(vwap_lb)
    if oi_lb:
        ctx.append(oi_lb)
    ctx.append(opp["btc_lb"])
    ctx_line = "  ·  ".join(ctx)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow} *#{coin} · {dir_txt}*  {grade_icon} *{opp['score']}分*  {session}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{opp['tf']}`  ·  1H {htf_e}  ·  {opp['t4h_lb']}  [{opp['lev']}]\n"
        f"📊 {bd_line}\n"
        f"\n"
        f"📌 進場    `{entry:.4f}`\n"
        f"🛑 止損    `{opp['sl']:.4f}`  `{sl_sign}{sl_pct:.2f}%`\n"
        f"\n"
        f"🥇 TP1    `{opp['tp1']:.4f}`  `{sign}{tp1_pct:.2f}%`  ⅓倉\n"
        f"🥈 TP2    `{opp['tp2']:.4f}`  `{sign}{tp2_pct:.2f}%`  ⅓倉\n"
        f"🏆 TP3    `{opp['tp3']:.4f}`  `{sign}{tp3_pct:.2f}%`  ⅓倉\n"
        f"💼 {pos_hint}\n"
        f"─────────────────────────\n"
        f"{trigger_txt}\n"
        f"─────────────────────────\n"
        f"🗺 {opp['structure']}  ·  P/D {opp['pd_lb']}  ·  {liq_line}\n"
        f"📡 {ctx_line}\n"
        f"🧬 {opp['cvd_lb']}  ·  多空比 {opp['ls_str']}  ·  💸 {opp['fr_lb']}"
    )

# ─────────────────────────────────────────────────────────
# 13b. 追蹤訊號格式化
# ─────────────────────────────────────────────────────────
def _progress_bar(hit_tp1: bool, hit_tp2: bool, hit_tp3: bool = False,
                  touched_tp1: bool = False, touched_tp2: bool = False) -> str:
    """v9.1.6：新增「已觸及但未確認」狀態（⚡）"""
    if hit_tp1:
        p1 = "🥇✅"
    elif touched_tp1:
        p1 = "🥇⚡"
    else:
        p1 = "🥇⏳"
    if hit_tp2:
        p2 = "🥈✅"
    elif touched_tp2:
        p2 = "🥈⚡"
    else:
        p2 = "🥈⏳"
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
            f"✅ *進場提醒*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💰 *價格到達進場區！*\n"
            f"\n"
            f"📍 當前價    `{price:.4f}`\n"
            f"📌 進場價    `{entry:.4f}`\n"
            f"📊 評分      `{score}分`\n"
            f"\n"
            f"─────────────────────────\n"
            f"🛑 止損    `{sl:.4f}`    `{sl_sign}{sl_pct:.2f}%`\n"
            f"🥇 TP1     `{tp1:.4f}`    `{sign}{tp1_pct:.2f}%`   ⅓倉\n"
            f"🥈 TP2     `{tp2:.4f}`    `{sign}{tp2_pct:.2f}%`   ⅓倉\n"
            f"🏆 TP3     `{tp3:.4f}`    `{sign}{tp3_pct:.2f}%`   ⅓倉\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 *三段止盈 + 動態追蹤止損已啟動*\n"
            f"   到 TP1 (收盤確認) → SL 自動移至保本\n"
            f"   到 TP2 (收盤確認) → SL 自動移至 TP1\n"
            f"   到 TP3 → 完美收割"
        )
    elif alert_type == "TP1":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        tp2_pct = abs(tp2 - entry) / entry * 100
        tp3_pct = abs(tp3 - entry) / entry * 100
        new_sl_str = f"`{new_sl:.4f}`" if new_sl else f"`{entry:.4f}`"
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP1 達標！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 當前    `{price:.4f}`    `{sign}{pnl:.2f}%`    `+1.0R`\n"
            f"\n"
            f"進度   {_progress_bar(True, False, False)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"✅ TP1 `{tp1:.4f}`  已收盤確認\n"
            f"🛡 SL 移至 {new_sl_str}  *(保本)*\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 *操作建議*\n"
            f"   • 平倉  ⅓  部位鎖定獲利\n"
            f"   • 剩餘  ⅔  續抱追擊\n"
            f"\n"
            f"🎯 *下一目標*\n"
            f"   🥈 TP2   `{tp2:.4f}`   `{sign}{tp2_pct:.2f}%`\n"
            f"   🏆 TP3   `{tp3:.4f}`   `{sign}{tp3_pct:.2f}%`"
        )
    elif alert_type == "TP2":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        tp3_pct = abs(tp3 - entry) / entry * 100
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP2 達標！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 當前    `{price:.4f}`    `{sign}{pnl:.2f}%`    `+2.5R`\n"
            f"\n"
            f"進度   {_progress_bar(True, True, False)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"✅ TP2 `{tp2:.4f}`  已收盤確認\n"
            f"🛡 SL 移至 `{tp1:.4f}`  *(鎖利 +1.0R)*\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 *操作建議*\n"
            f"   • 再平倉  ⅓  部位落袋\n"
            f"   • 剩餘  ⅓  衝擊 TP3\n"
            f"\n"
            f"🏆 *最終目標*\n"
            f"   TP3   `{tp3:.4f}`   `{sign}{tp3_pct:.2f}%` 🚀"
        )
    elif alert_type == "TP3":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *TP3 完美收割！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💎 當前    `{price:.4f}`    `{sign}{pnl:.2f}%`    `+4.0R`\n"
            f"\n"
            f"進度   {_progress_bar(True, True, True)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"🎉 *三段止盈全部達成！*\n"
            f"🏆 TP3 `{tp3:.4f}`  已達成\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 建議 *立即平倉全部剩餘部位*\n"
            f"📊 本單表現   🌟🌟🌟 優秀\n"
            f"\n"
            f"恭喜獲利 🎊"
        )
    elif alert_type == "SL":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        is_be = new_sl is not None and abs(new_sl - entry) < entry * 0.0001
        label = "保本止損" if is_be else "止損觸發"
        header_em = "🛡" if is_be else "🛑"
        sl_display = f"`{new_sl:.4f}`" if new_sl else f"`{sl:.4f}`"
        r_tag = "`0.0R`" if is_be else "`-1.0R`"
        if is_be:
            outcome = ("💡 倉位已平倉於成本價 資金安全，等下一個機會 💪")
        else:
            outcome = ("⚠️ 倉位已止損出場 請遵守風控，莫加碼攤平")
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{header_em} *{label}*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 當前    `{price:.4f}`    `{pnl:+.2f}%`    {r_tag}\n"
            f"\n"
            f"─────────────────────────\n"
            f"{header_em} 止損價 {sl_display}  已觸發\n"
            f"─────────────────────────\n"
            f"\n"
            f"{outcome}"
        )
    return ""

# ─────────────────────────────────────────────────────────
# 14. WinRateTracker — 勝率統計 & 戰報
# ─────────────────────────────────────────────────────────
class WinRateTracker:
    """記錄每筆已結算交易，持久化到 trade_history.json"""
    def __init__(self, filepath: str = TRADE_HISTORY_FILE):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.history = self._load()
    
    def _load(self) -> list:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []
    
    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)
    
    def record(self, coin: str, side: str, tf: str, entry: float, close_price: float, close_type: str, score: int):
        is_win = close_type in ("TP1", "TP2", "TP3")
        is_be = (close_type == "BE")
        pnl_pct = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
        now = utc_now()
        rec = {
            "time": now.strftime("%Y-%m-%d %H:%M"),
            "date": now.strftime("%Y-%m-%d"),
            "month": now.strftime("%Y-%m"),
            "coin": coin, "side": side, "tf": tf,
            "entry": round(entry, 6), "close": round(close_price, 6),
            "close_type": close_type, "pnl_pct": round(pnl_pct, 3),
            "is_win": is_win, "is_be": is_be, "score": score,
        }
        with self._lock:
            self.history.append(rec)
            self._save()
        logging.info(f"📝 記錄 {coin} {side} {close_type} {pnl_pct:+.2f}%")
    
    def _stats(self, trades: list):
        if not trades:
            return None
        wins = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"] and not t.get("is_be")]
        be = [t for t in trades if t.get("is_be")]
        total = len(trades)
        win_r = len(wins) / total * 100
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0.0
        exp = (win_r/100 * avg_win) + ((1-win_r/100) * avg_loss)
        streak = 0
        streak_type = ""
        for t in reversed(trades):
            if t.get("is_be"):
                continue
            if t["is_win"]:
                if streak_type in ("W", ""):
                    streak_type = "W"
                    streak += 1
                else:
                    break
            else:
                if streak_type in ("L", ""):
                    streak_type = "L"
                    streak += 1
                else:
                    break
        if streak_type == "W" and streak >= 3:
            streak_str = f"🔥 連勝 {streak} 筆！"
        elif streak_type == "W" and streak >= 2:
            streak_str = f"✅ 連勝 {streak} 筆"
        elif streak_type == "W":
            streak_str = f"✅ 最近一勝"
        elif streak_type == "L" and streak >= 3:
            streak_str = f"❄️ 連敗 {streak} 筆，注意風控"
        elif streak_type == "L" and streak >= 2:
            streak_str = f"⚠️ 連敗 {streak} 筆"
        elif streak_type == "L":
            streak_str = f"❌ 最近一敗"
        else:
            streak_str = ""
        return {"total":total,"wins":len(wins),"losses":len(losses),"be":len(be),
                "win_rate":win_r,"avg_win":avg_win,"avg_loss":avg_loss,"expectancy":exp,
                "streak_str": streak_str}
    
    def _trade_lines(self, trades: list, n: int = 8) -> str:
        ct_map = {"TP1":"🥇","TP2":"🥈","TP3":"🏆","BE":"⚖️","SL":"🛑"}
        lines = []
        for t in trades[-n:]:
            arrow = "🟢" if t["side"]=="LONG" else "🔴"
            ico = ct_map.get(t["close_type"],"❓")
            lines.append(f"{ico} #{t['coin']} {arrow}  {t['pnl_pct']:+.2f}%  [{t['close_type']}]  {t['time'][-5:]}")
        return "\n".join(lines)
    
    def daily_report(self, date_str: str = None) -> str:
        if not date_str:
            date_str = utc_now().strftime("%Y-%m-%d")
        trades = [t for t in self.history if t["date"] == date_str]
        s = self._stats(trades)
        if not s:
            return (f"📊 *今日戰報 {date_str}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"今日暫無已結算訊號\n"
                    f"持續掃描中... 💪\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖 Alpha Oracle v9.1.6 持續監控中")
        grade = ("🏆 優秀" if s["win_rate"]>=70 else
                 "✅ 良好" if s["win_rate"]>=55 else
                 "⚠️ 一般" if s["win_rate"]>=40 else "❌ 待改善")
        streak_line = f"\n{s['streak_str']}" if s.get("streak_str") else ""
        return (
            f"📊 *今日戰報 {date_str}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 訊號總數：{s['total']} 筆  {grade}\n"
            f"✅ 勝：{s['wins']}  ❌ 敗：{s['losses']}  ⚖️ 保本：{s['be']}\n"
            f"📈 *勝率：{s['win_rate']:.1f}%*\n"
            f"💰 平均獲利：{s['avg_win']:+.2f}%\n"
            f"📉 平均虧損：{s['avg_loss']:+.2f}%\n"
            f"⚡ 期望值：{s['expectancy']:+.2f}%/筆{streak_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{self._trade_lines(trades)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Alpha Oracle v9.1.6 明日繼續！"
        )
    
    def monthly_report(self, month_str: str = None) -> str:
        if not month_str:
            month_str = utc_now().strftime("%Y-%m")
        trades = [t for t in self.history if t["month"] == month_str]
        s = self._stats(trades)
        if not s:
            return (f"📅 *月度戰報 {month_str}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"本月暫無已結算訊號\n"
                    f"持續掃描中... 💪")
        coin_stats: dict = {}
        for t in trades:
            cn = t["coin"]
            if cn not in coin_stats:
                coin_stats[cn] = {"w":0,"l":0,"b":0}
            if t["is_win"]:
                coin_stats[cn]["w"] += 1
            elif t["is_be"]:
                coin_stats[cn]["b"] += 1
            else:
                coin_stats[cn]["l"] += 1
        coin_lines = [f"  #{cn}  W{cs['w']} L{cs['l']} BE{cs['b']}"
                      for cn, cs in sorted(coin_stats.items(), key=lambda x: -x[1]["w"])]
        grade = ("🏆 傑出" if s["win_rate"]>=70 else
                 "✅ 良好" if s["win_rate"]>=55 else
                 "⚠️ 普通" if s["win_rate"]>=40 else "❌ 需優化")
        streak_line = f"\n{s['streak_str']}" if s.get("streak_str") else ""
        return (
            f"📅 *月度戰報 {month_str}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 本月訊號：{s['total']} 筆  {grade}\n"
            f"✅ 勝：{s['wins']}  ❌ 敗：{s['losses']}  ⚖️ 保本：{s['be']}\n"
            f"📈 *月勝率：{s['win_rate']:.1f}%*\n"
            f"💰 平均獲利：{s['avg_win']:+.2f}%\n"
            f"📉 平均虧損：{s['avg_loss']:+.2f}%\n"
            f"⚡ 月期望值：{s['expectancy']:+.2f}%/筆{streak_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏅 各幣種：\n"
            + "\n".join(coin_lines) +
            f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Alpha Oracle v9.1.6 下月繼續！"
        )

# ─────────────────────────────────────────────────────────
# 15. SignalTracker — 進場/TP/SL 監控 + 動態追蹤止損
# ─────────────────────────────────────────────────────────
class SignalTracker:
    """
    追蹤活躍訊號，持久化到 JSON，監控進場/TP/SL 觸發。
    平倉時自動寫入 WinRateTracker。
    狀態機：
      PENDING  → 等待價格到達進場區
      ACTIVE   → 已進場，等待 TP/SL
      BE       → TP1 已中，SL 已移至進場價（保本）
      TRAIL    → TP2 已中，SL 已移至 TP1（鎖利）
      closed   → 已平倉（從追蹤列表移除）
    動態追蹤止損：
      • 到達 TP1 (收盤確認) → SL 移至進場價（保本）
      • 到達 TP2 (收盤確認) → SL 移至 TP1（鎖利）
      • 到達 TP3 → 全部平倉，完美收割
    """
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE, win_tracker: WinRateTracker = None):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.signals = self._load()
        self.win_tracker = win_tracker
        # v9.1.6: 每輪 check_all 的狀態變動計數，用於決定是否推播狀態摘要
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
        """
        v9.1.6：新增 active 參數
          active=False → 預設 PENDING（等價格到進場區才觸發 ENTRY 通知）
          active=True  → 直接 ACTIVE（掃描當下價格已在進場區，ENTRY 通知已由呼叫方發出）
        """
        key = f"{opp['instId']}_{opp['side']}_{opp['tf']}_{int(time.time())}"
        now = time.time()
        with self._lock:
            self.signals[key] = {
                "instId": opp["instId"],
                "side": opp["side"],
                "tf": opp["tf"],
                "entry": opp["entry"],
                "sl": opp["sl"],
                "sl_orig": opp["sl"],
                "tp1": opp["tp1"],
                "tp2": opp["tp2"],
                "tp3": opp["tp3"],
                "score": opp["score"],
                "grade": opp["grade"],
                "status": "ACTIVE" if active else "PENDING",
                "hit_tp1": False,
                "hit_tp2": False,
                # v9.1.6：收盤確認的中間狀態
                "touched_tp1": False,
                "touched_tp2": False,
                "created": now,
                "activated_at": now if active else None,
                "hit_tp1_at": None,
                "hit_tp2_at": None,
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
        """
        ✨ v9.1.6：抓最近 1~3 根已收盤 K，檢查收盤價是否穿越 level。
        LONG  → 需要 close >= level
        SHORT → 需要 close <= level
        抓不到 K 線則回傳 False（保守，不觸發 SL 移動）。
        """
        if not CONFIRM_TP_ON_CLOSE:
            return True
        df = fetch_okx_last_closed(sig["instId"], tf=sig["tf"], limit=3)
        if df is None or len(df) < 1:
            logging.warning(f"  [{sig['instId']}] 收盤確認抓不到 K 線，暫不觸發 SL 移動")
            return False
        last_close = float(df["c"].iloc[-1])
        if sig["side"] == "LONG":
            return last_close >= level
        else:
            return last_close <= level
    
    def check_one(self, key: str, sig: dict) -> bool:
        """
        檢查單一訊號。返回 True = 已結束可移除；False = 繼續追蹤。
        """
        try:
            price = fetch_ticker_price(sig["instId"])
            if price <= 0:
                logging.warning(f"  [{key}] 無法取得即時價格，跳過檢查")
                return False
            coin = sig["instId"].split("-")[0]
            side = sig["side"]
            status = sig["status"]
            entry = sig["entry"]
            sl = sig["sl"]
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
            logging.debug(f"[{key}] 檢查: 價格={price:.4f}, 狀態={status}")
            # ── 1. PENDING 狀態：檢查過期 & 進場區 ─────────────
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
            # ── 2. 非活躍狀態不處理 ─────────────────────────
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False
            # ── 3. 止損觸發（最優先，避免錯過保護）─────────────
            #      SL 不需要收盤確認，tick 觸及立即觸發
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit:
                is_be = (status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001)
                close_type = "BE" if is_be else "SL"
                msg = format_alert(coin, side, "SL", price, entry, sig["sl_orig"],
                                   tp1, tp2, tp3, new_sl=(entry if is_be else sl))
                if send_tg(msg):
                    logging.info(f"  [{close_type}] {key} @ {price:.4f} (BE={is_be}) - 通知已發送")
                else:
                    logging.error(f"  [{close_type}] {key} - 通知發送失敗")
                self._close(sig, price, close_type)
                self.last_run_transitions += 1
                return True
            # ── 4. TP3 達成 → 全部平倉（不需收盤確認，行情已走很遠）────
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
            # ── 5. TP2 達成 → 移損至 TP1（需收盤確認）────────
            tp2_touched_now = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if tp2_touched_now and not sig.get("hit_tp2"):
                # 標記觸及（讓 status_summary 顯示 ⚡）
                if not sig.get("touched_tp2"):
                    self.update(key, touched_tp2=True)
                    logging.info(f"  [TP2 觸及] {key} @ {price:.4f}  等收盤確認")
                # 收盤確認
                if not self._is_close_confirmed(sig, tp2):
                    logging.info(f"  [TP2 待確認] {key}  本根 K 尚未收盤於 TP2 之上/下")
                    return False
                now = time.time()
                # 同時標記 hit_tp1（避免下一輪誤觸發 TP1）
                if not sig.get("hit_tp1"):
                    self.update(key, hit_tp1=True, touched_tp1=True, hit_tp1_at=now)
                    self._close(sig, tp1, "TP1")
                self.update(key, hit_tp2=True, sl=tp1, status="TRAIL", hit_tp2_at=now)
                msg = format_alert(coin, side, "TP2", price, entry, sig["sl_orig"],
                                   tp1, tp2, tp3, new_sl=tp1)
                if send_tg(msg):
                    logging.info(f"  [TP2] {key} @ {price:.4f} → SL 移至 TP1={tp1:.4f} - 通知已發送")
                else:
                    logging.error(f"  [TP2] {key} - 通知發送失敗")
                self._close(sig, tp2, "TP2")
                self.last_run_transitions += 1
                return False
            # ── 6. TP1 達成 → 移損至進場價（需收盤確認）─────
            tp1_touched_now = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if tp1_touched_now and not sig.get("hit_tp1"):
                # 標記觸及
                if not sig.get("touched_tp1"):
                    self.update(key, touched_tp1=True)
                    logging.info(f"  [TP1 觸及] {key} @ {price:.4f}  等收盤確認")
                # 收盤確認
                if not self._is_close_confirmed(sig, tp1):
                    logging.info(f"  [TP1 待確認] {key}  本根 K 尚未收盤於 TP1 之上/下")
                    return False
                self.update(key, hit_tp1=True, sl=entry, status="BE", hit_tp1_at=time.time())
                msg = format_alert(coin, side, "TP1", price, entry, sig["sl_orig"],
                                   tp1, tp2, tp3, new_sl=entry)
                if send_tg(msg):
                    logging.info(f"  [TP1] {key} @ {price:.4f} → SL 移至保本={entry:.4f} - 通知已發送")
                else:
                    logging.error(f"  [TP1] {key} - 通知發送失敗")
                self._close(sig, tp1, "TP1")
                self.last_run_transitions += 1
                return False
            # ── 7. 無觸發 ────────────────────────────────
            return False
        except Exception as e:
            logging.error(f"check_one [{key}] 錯誤: {e}\n{traceback.format_exc()}")
            return False
    
    def check_all(self):
        """v9.1.6：重置 transitions 計數，供外部判斷是否需要推播狀態"""
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
            # 當前價 / 距離 / PnL
            if s["status"] == "PENDING":
                if live > 0:
                    dist_pct = (live - entry) / entry * 100
                    # v9.1.6：判斷是否已在進場區
                    in_zone = (
                        (side == "LONG" and entry*(1-ENTRY_TOLERANCE*3) <= live <= entry*(1+ENTRY_TOLERANCE)) or
                        (side == "SHORT" and entry*(1-ENTRY_TOLERANCE) <= live <= entry*(1+ENTRY_TOLERANCE*3))
                    )
                    zone_tag = "  ⚡ *已在進場區*" if in_zone else ""
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
            # SL 標示
            if s["status"] == "BE":
                sl_label = f" 🛡 止損 `{s['sl']:.4f}` *(保本)*"
            elif s["status"] == "TRAIL":
                sl_label = f" 🛡 止損 `{s['sl']:.4f}` *(鎖利於 TP1)*"
            else:
                sl_label = f" 🛑 止損 `{s['sl']:.4f}`"
            # 進度條（含 ⚡ 待確認狀態）
            progress = _progress_bar(
                s.get("hit_tp1", False), s.get("hit_tp2", False), False,
                touched_tp1=s.get("touched_tp1", False), touched_tp2=s.get("touched_tp2", False)
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
        lines.extend(["", f"━━━━━━━━━━━━━━━━━━━━━━━━", f"🤖 Alpha Oracle v9.1.6 動態追蹤中"])
        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 16. 監控迴圈
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

# ─────────────────────────────────────────────────────────
# 17. 即時進場區判斷
# ─────────────────────────────────────────────────────────
def _check_entry_zone(opp: dict) -> tuple:
    live = fetch_ticker_price(opp["instId"])
    if live <= 0:
        return False, 0.0, "無法取得即時價"
    entry = opp["entry"]
    side = opp["side"]
    tol = ENTRY_TOLERANCE
    in_zone = (
        (side=="LONG" and live <= entry*(1+tol) and live >= entry*(1-tol*3)) or
        (side=="SHORT" and live >= entry*(1-tol) and live <= entry*(1+tol*3))
    )
    dist_pct = (live - entry) / entry * 100
    if in_zone:
        return True, live, f"✅ 已在進場區 {live:.4f}（{dist_pct:+.2f}%）"
    elif (side=="LONG" and live > entry):
        return False, live, f"⬆️ 價格高於進場區 {dist_pct:+.2f}% 等待回踩"
    elif (side=="SHORT" and live < entry):
        return False, live, f"⬇️ 價格低於進場區 {dist_pct:+.2f}% 等待回升"
    else:
        return False, live, f"⏳ 等待接近進場區（{abs(dist_pct):.2f}%）"

# ─────────────────────────────────────────────────────────
# 18. 主掃描函式
# ─────────────────────────────────────────────────────────
def _scan_one_coin(coin: str) -> list:
    if not check_news_cooldown(coin):
        logging.info(f"  [{coin}] 新聞冷卻期")
        return []
    try:
        return scan_for_opportunity(coin)
    except Exception as e:
        logging.error(f"[{coin}] 掃描錯誤: {e}")
        return []

def run_scan(tracker: SignalTracker) -> int:
    # 預檢既有訊號
    active_before = len(tracker.list_active())
    if active_before > 0:
        logging.info(f"═══ 預檢 {active_before} 筆既有訊號 ═══")
        try:
            tracker.check_all()
        except Exception as e:
            logging.error(f"check_all 預檢錯誤: {e}")
    logging.info(f"═══ 掃描開始 閾值={SETUP_SCORE_THRESHOLD} 時框={SCAN_TIMEFRAMES} ═══")
    all_opps: list = []
    workers = min(5, len(ALL_COINS))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_scan_one_coin, c): c for c in ALL_COINS}
        for fut in as_completed(futures):
            coin = futures[fut]
            try:
                opps = fut.result()
                if opps:
                    logging.info(f"  [{coin}] 找到 {len(opps)} 個機會")
                    all_opps.extend(opps)
            except Exception as e:
                logging.error(f"[{coin}] Future 錯誤: {e}")
    all_opps.sort(key=lambda x: x["score"], reverse=True)
    sent = 0
    for opp in all_opps:
        if sent >= MAX_SIGNALS_PER_RUN:
            break
        if not check_signal_cooldown(opp["instId"], opp["side"]):
            logging.info(f"  [{opp['instId']}/{opp['side']}] 冷卻期中（{SIGNAL_COOLDOWN_HOURS}h），跳過")
            continue
        if send_tg(format_signal(opp)):
            sent += 1
            set_signal_cooldown(opp["instId"], opp["side"])
            logging.info(f"  #{sent} {opp['instId']} [{opp['tf']}]{opp['side']} {opp['score']}分")
            in_zone, live, zone_msg = _check_entry_zone(opp)
            logging.info(f"     {zone_msg}")
            if in_zone and live > 0:
                time.sleep(0.5)
                if send_tg(format_alert(
                    coin=opp["instId"].split("-")[0], side=opp["side"],
                    alert_type="ENTRY", price=live,
                    entry=opp["entry"], sl=opp["sl"],
                    tp1=opp["tp1"], tp2=opp["tp2"], tp3=opp["tp3"],
                    score=opp["score"],
                )):
                    logging.info(f"     ✅ 進場通知已發送")
                else:
                    logging.error(f"     ❌ 進場通知發送失敗")
                # ✨ v9.1.6：掃描當下已在進場區 → 直接存 ACTIVE
                tracker.add(opp, active=True)
            else:
                tracker.add(opp, active=False)
        time.sleep(0.8)
    logging.info(f"掃描完成，發送 {sent} 筆")
    # ✨ v9.1.6：結尾再 check_all 一次（涵蓋掃描過程中價格才進入區間的新訊號）
    try:
        if len(tracker.list_active()) > 0:
            tracker.check_all()
    except Exception as e:
        logging.error(f"run_scan 收尾 check_all 錯誤: {e}")
    # 掃描完一律推播狀態（有新訊號 or 現有訊號，用戶想看）
    if len(tracker.list_active()) > 0:
        status_msg = tracker.status_summary()
        if send_tg(status_msg):
            logging.info("✅ 狀態摘要已發送")
        else:
            logging.error("❌ 狀態摘要發送失敗")
    return sent

def _is_heartbeat_window() -> bool:
    """v9.1.6：是否在每小時的整點心跳窗口（每小時 0~4 分鐘）"""
    return utc_now().minute < HEARTBEAT_MINUTE_WINDOW

def run_monitor_once(tracker: SignalTracker, push_status: bool = None) -> int:
    """
    v9.1.6 智能推播節流：
      - 有狀態變動（進場/TP/SL）→ 一定推狀態摘要
      - 無變動 + 整點心跳窗口 → 推狀態摘要
      - 無變動 + 非心跳窗口 → 靜默（不推狀態，但 TP/SL 通知仍照常發）
    `push_status` 參數若為 None 則走智能判斷；True/False 則強制覆寫。
    """
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
    # 判斷是否推播狀態摘要
    transitions = getattr(tracker, "last_run_transitions", 0)
    heartbeat = _is_heartbeat_window()
    if push_status is None:
        should_push = remaining and (transitions > 0 or heartbeat)
    else:
        should_push = push_status and remaining
    if should_push:
        reason = "狀態變動" if transitions > 0 else "整點心跳"
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

# ─────────────────────────────────────────────────────────
# 19. 主函式
# ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpha Oracle v9.1.6")
    parser.add_argument("--mode", default="all",
                        choices=["scan","monitor","monitor_once","loop","all",
                                 "daily_report","monthly_report"],
                        help="scan=掃描 | monitor=持續監控 | monitor_once=單次監控 | loop=定時掃描+監控 | all=掃描+監控 | daily_report=今日戰報 | monthly_report=月度戰報")
    parser.add_argument("--interval", type=int, default=10, help="監控間隔秒數（預設 10 秒）")
    parser.add_argument("--loop-interval", type=int, default=900)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    logging.info("=" * 60)
    logging.info("🤖 Alpha Oracle v9.1.6 啟動")
    logging.info(f"📋 模式: {args.mode}")
    logging.info(f"⏱ 監控間隔: {args.interval}秒")
    logging.info(f"🎯 TP 收盤確認: {CONFIRM_TP_ON_CLOSE}")
    logging.info("=" * 60)
    win_tracker = WinRateTracker(TRADE_HISTORY_FILE)
    tracker = SignalTracker(ACTIVE_SIGNALS_FILE, win_tracker=win_tracker)
    if args.status:
        summary = tracker.status_summary()
        print(summary)
        send_tg(summary)
        return
    if args.mode == "daily_report":
        msg = win_tracker.daily_report()
        logging.info("發送每日戰報")
        print(msg)
        send_tg(msg)
        return
    if args.mode == "monthly_report":
        msg = win_tracker.monthly_report()
        logging.info("發送月度戰報")
        print(msg)
        send_tg(msg)
        return
    if args.mode == "scan":
        run_scan(tracker)
        return
    if args.mode == "monitor_once":
        run_monitor_once(tracker)
        return
    if args.mode == "monitor":
        try:
            monitor_loop(tracker, interval=args.interval)
        except KeyboardInterrupt:
            logging.info("監控停止")
        return
    if args.mode == "loop":
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_loop, args=(tracker, args.interval, stop_ev), daemon=True)
        t.start()
        try:
            while True:
                run_scan(tracker)
                logging.info(f"下次掃描：{args.loop_interval}s 後")
                time.sleep(args.loop_interval)
        except KeyboardInterrupt:
            logging.info("迴圈停止")
            stop_ev.set()
        return
    # all 模式（預設）
    run_scan(tracker)
    try:
        monitor_loop(tracker, interval=args.interval)
    except KeyboardInterrupt:
        logging.info("停止")

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
