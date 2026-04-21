#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v9.1.8 — 完整整合版（SMC 自動回報 + K 線 wick fallback）
══════════════════════════════════════════════════════════════════════
v9.1.8 新增：
  ✨ SMC 自動回報系統（進場/TP1/TP2/TP3/止損/每日戰報）
  ✨ 動態追蹤止損（TP1→保本，TP2→鎖利至 TP1）
  ✨ 相關幣種去重 + 倉位上限 + 每日止損限額
  ✨ 午夜自動戰績回報（台灣時間 00:00）

v9.1.7 保留：
  🐛 GitHub Actions cron 5min 間隔錯過短暫 wick → 用已收盤 K 線 high/low 備援
  ✨ TP1/TP2 首次觸及時發送「⚡ 觸及（待收盤確認）」TG 通知
  ✨ PENDING 進場區判斷同時吃 tick + K 線 wick

v9.1.6 保留：
  ✅ TP 收盤確認機制（CONFIRM_TP_ON_CLOSE）+ 狀態推播節流
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
from datetime import datetime, timezone, timedelta

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

# 🔥 v9.1.8 風控參數
MAX_DAILY_SL = 2
MAX_CONCURRENT = 3
SL_MIN_PCT = 0.007
COOLDOWN_BARS_SL = 8
COOLDOWN_BARS_TP = 4

CORR_GROUPS = [
    {"BTC-USDT-SWAP", "ETH-USDT-SWAP"},
    {"SOL-USDT-SWAP", "AVAX-USDT-SWAP", "APT-USDT-SWAP"},
    {"LINK-USDT-SWAP", "ADA-USDT-SWAP", "XRP-USDT-SWAP"},
]

SCAN_TIMEFRAMES = ["15m", "30m", "1H"]
MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "12"))
SETUP_SCORE_THRESHOLD = 68

CROSSLINE_BODY_RATIO = 0.30
SWEEP_VOLUME_RATIO = 1.8
SWEEP_CONSECUTIVE_MOVES = 2
NEWS_COOLDOWN_MINUTES = 60
ABSORPTION_VOL_MULTIPLIER = 1.8
ABSORPTION_PRICE_THRESHOLD = 0.002

VOLATILITY_HARD_LIMIT = 0.035
ATR_SL_MULT = 1.5
RSI_PERIOD = 14
ADX_PERIOD = 14

ENTRY_TOLERANCE = 0.002
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
DAILY_STATS_FILE = "daily_stats.csv"
SIGNAL_EXPIRE_HOURS = 24

SIGNAL_COOLDOWN_HOURS = 2
VWAP_PERIODS = 50
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9

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

def safe_int(val, fallback=0):
    try:
        return int(float(val))
    except:
        return fallback

def send_tg(msg: str, parse_mode: str = "Markdown") -> bool:
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

def tw_now() -> datetime:
    return utc_now() + timedelta(hours=8)

def check_signal_cooldown(instId: str, side: str) -> bool:
    key = f"{instId}_{side}"
    last = _SIGNAL_COOLDOWN.get(key, 0)
    return (time.time() - last) >= SIGNAL_COOLDOWN_HOURS * 3600

def set_signal_cooldown(instId: str, side: str):
    _SIGNAL_COOLDOWN[f"{instId}_{side}"] = time.time()

def get_market_session() -> str:
    h = tw_now().hour
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
    h, l, c = df["h"].values.astype(float), df["l"].values.astype(float), df["c"].values.astype(float)
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
    fu, fd = np.zeros(n), np.zeros(n)
    trend = np.ones(n, dtype=int)
    fu[period], fd[period] = bu[period], bd[period]
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
    h, l, c = df["h"].values.astype(float), df["l"].values.astype(float), df["c"].values.astype(float)
    n = len(df)
    tr, pdm, mdm = np.zeros(n), np.zeros(n), np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        pdm[i] = (h[i]-h[i-1]) if (h[i]-h[i-1])>(l[i-1]-l[i]) and (h[i]-h[i-1])>0 else 0
        mdm[i] = (l[i-1]-l[i]) if (l[i-1]-l[i])>(h[i]-h[i-1]) and (l[i-1]-l[i])>0 else 0
    atr_w, p_w, m_w = np.zeros(n), np.zeros(n), np.zeros(n)
    atr_w[period], p_w[period], m_w[period] = tr[1:period+1].sum(), pdm[1:period+1].sum(), mdm[1:period+1].sum()
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
    if side=="LONG" and support and abs(entry - support) < atr*2.5:
        base = min(base, support - atr*0.5)
    if side=="SHORT" and resistance and abs(resistance - entry) < atr*2.5:
        base = max(base, resistance + atr*0.5)
    min_dist = atr * 1.5
    if abs(entry - base) < min_dist:
        base = entry - min_dist if side=="LONG" else entry + min_dist
    return base

# ─────────────────────────────────────────────────────────
# 6-11. 市場結構、流動性、Order Block、訂單流、情緒、評分
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
        elif sh:
            if price>sh[-1]:
                result, score = f"BOS 向上突破 {sh[-1]:.4f}", 0.80
            elif len(sh)>=2 and price>sh[-2]:
                result, score = f"CHoCH 潛在轉折 {sh[-2]:.4f}", 0.55
    else:
        if sh and df["h"].iloc[-4:-1].max() > sh[-1]+atr*0.1 and price<sh[-1]:
            result, score = f"CHoCH 掃高回落 @ {sh[-1]:.4f}", 0.90
        elif sl:
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

def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    sh, sl, _, _ = find_swing_points(df, n=2, lookback=lookback)
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    res = dict(pools=[], sweep_detected=False, sweep_desc="", sweep_score=0.0, eqh=None, eql=None, nearest_bsl=None, nearest_ssl=None)
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

def find_order_blocks(df: pd.DataFrame, side: str, lookback: int = 60) -> list:
    data = df.tail(lookback).reset_index(drop=True)
    obs = []
    price = data["c"].iloc[-1]
    atr = calculate_atr(data)
    for i in range(2, len(data)-3):
        c = data.iloc[i]
        if side=="LONG" and c["c"]<c["o"]:
            mv = data["h"].iloc[i+1:i+4].max() - c["h"]
            if mv > atr*1.5:
                ob = dict(high=c["h"], low=c["l"], mid=(c["h"]+c["l"])/2, strength=mv/(atr+1e-10))
                if ob["high"] < price*1.005:
                    obs.append(ob)
        elif side=="SHORT" and c["c"]>c["o"]:
            mv = c["l"] - data["l"].iloc[i+1:i+4].min()
            if mv > atr*1.5:
                ob = dict(high=c["h"], low=c["l"], mid=(c["h"]+c["l"])/2, strength=mv/(atr+1e-10))
                if ob["low"] > price*0.995:
                    obs.append(ob)
    obs.sort(key=lambda x: x["strength"], reverse=True)
    return obs[:2]

def find_fvg(df: pd.DataFrame, side: str, lookback: int = 40) -> list:
    data = df.tail(lookback).reset_index(drop=True)
    fvgs = []
    price = data["c"].iloc[-1]
    for i in range(2, len(data)):
        if side=="LONG":
            bot = data["h"].iloc[i-2]
            top = data["l"].iloc[i]
            if top > bot and bot < price:
                fvgs.append(dict(top=top, bottom=bot, mid=(top+bot)/2, size=top-bot))
        else:
            top = data["l"].iloc[i-2]
            bot = data["h"].iloc[i]
            if bot < top and top > price:
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
    fib = (price - lo) / rng
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

def detect_crossline(df: pd.DataFrame, lookback: int = 15):
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k["c"] - k["o"])
        rng = k["h"] - k["l"] + 1e-10
        if body < CROSSLINE_BODY_RATIO * rng:
            uw = k["h"] - max(k["c"], k["o"])
            dw = min(k["c"], k["o"]) - k["l"]
            pot = "SHORT" if uw > dw*1.5 else ("LONG" if dw > uw*1.5 else "NEUTRAL")
            dist = len(df) - 1 - i
            return dict(price=k["c"], high=k["h"], low=k["l"], body_ratio=body/rng, potential_side=pot, distance=dist, desc=f"十字線@{k['c']:.4f}({pot},{dist}根前)")
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> tuple:
    if len(df) < 8:
        return False, 0.0, "數據不足"
    recent = df.tail(8)
    vol_ma = df["v"].tail(20).mean()
    vol_sc = recent.iloc[-1]["v"] / (vol_ma + 1e-10)
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"量能不足({vol_sc:.1f}x)"
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side=="LONG" and recent["c"].iloc[i] > recent["c"].iloc[i-1]:
            moves += 1
        elif side=="SHORT" and recent["c"].iloc[i] < recent["c"].iloc[i-1]:
            moves += 1
        else:
            break
    if moves >= SWEEP_CONSECUTIVE_MOVES:
        return True, min(vol_sc/3.0, 1.0), f"主動掃單 連續{moves}根 {vol_sc:.1f}x"
    return False, 0.0, f"無連續掃單({moves}根)"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df) < 6:
        return False
    recent = df.tail(6)
    vol_ma = df["v"].tail(20).mean()
    mv = abs(recent["c"].iloc[-1] - recent["c"].iloc[0]) / (recent["c"].iloc[0] + 1e-10)
    return mv >= 0.005 and recent["v"].iloc[-1] < 0.75 * vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> tuple:
    if len(df) < 15:
        return False, "無吸收"
    recent = df.tail(5)
    vol_ma = df["v"].tail(20).mean()
    avg3 = recent["v"].iloc[-3:].mean()
    chg = abs(recent["c"].iloc[-1] - recent["c"].iloc[-4]) / (recent["c"].iloc[-4] + 1e-10)
    if avg3 > ABSORPTION_VOL_MULTIPLIER * vol_ma and chg < ABSORPTION_PRICE_THRESHOLD:
        return True, f"吸收 量{avg3/vol_ma:.1f}x 價動{chg*100:.2f}%"
    return False, "無吸收"

def calculate_cvd(df: pd.DataFrame, periods: int = 50) -> tuple:
    data = df.tail(periods).copy()
    delta = np.where(data["c"]>data["o"], data["v"], np.where(data["c"]<data["o"], -data["v"], 0))
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
    if ratio >= 2.5:
        senti = f"極度多頭擁擠({ratio:.2f}) 逆向偏空"
    elif ratio >= 1.8:
        senti = f"多頭擁擠({ratio:.2f}) 謹慎做多"
    elif ratio >= 1.2:
        senti = f"略偏多頭({ratio:.2f})"
    elif ratio >= 0.8:
        senti = f"均衡({ratio:.2f})"
    elif ratio >= 0.5:
        senti = f"空頭擁擠({ratio:.2f}) 謹慎做空"
    else:
        senti = f"極度空頭擁擠({ratio:.2f}) 逆向偏多"
    if side=="LONG":
        sc = 1.0 if ratio<0.8 else (0.7 if ratio<1.2 else (0.4 if ratio<1.8 else 0.1))
    else:
        sc = 1.0 if ratio>2.0 else (0.7 if ratio>1.5 else (0.4 if ratio>1.0 else 0.1))
    return sc, senti

def interpret_funding_rate(fr: float, side: str) -> tuple:
    p = fr * 100
    if side=="LONG":
        if fr < -0.0003:
            return 1.0, f"費率極佳{p:.4f}%(空頭付費)"
        elif fr < 0.0001:
            return 0.8, f"費率友善{p:.4f}%"
        elif fr < 0.0003:
            return 0.5, f"費率尚可{p:.4f}%"
        elif fr < 0.0008:
            return 0.2, f"費率不佳{p:.4f}%"
        else:
            return 0.0, f"費率禁入{p:.4f}%"
    else:
        if fr > 0.0008:
            return 1.0, f"費率極佳{p:.4f}%(多頭付費)"
        elif fr > 0.0003:
            return 0.8, f"費率友善{p:.4f}%"
        elif fr > 0.0001:
            return 0.5, f"費率尚可{p:.4f}%"
        elif fr > -0.0003:
            return 0.2, f"費率不佳{p:.4f}%"
        else:
            return 0.0, f"費率禁入{p:.4f}%"

def check_ob_direction(side: str, ob_r: float) -> tuple:
    if side=="LONG":
        if ob_r >= 1.30:
            return 1.0, f"買盤強勢({ob_r:.2f})"
        elif ob_r >= 1.05:
            return 0.7, f"買盤略強({ob_r:.2f})"
        elif ob_r >= 0.95:
            return 0.3, f"盤口均衡({ob_r:.2f})"
        else:
            return 0.0, f"賣盤主導，做多風險({ob_r:.2f})"
    else:
        if ob_r <= 0.77:
            return 1.0, f"賣盤強勢({ob_r:.2f})"
        elif ob_r <= 0.95:
            return 0.7, f"
