#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v9.1.4 — 修复TP通知自动发送
══════════════════════════════════════════════════════════════════════
v9.1.4 修复：
  🐛 FIX: TP通知没有自动发送的问题
  ✨ NEW: 更频繁的监控检查（默认10秒）
  ✨ NEW: 增强的错误处理和重试机制
  ✨ NEW: 确保TP触发时必定发送通知
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
# 1. 基础配置
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
CHAT_ID  = os.getenv("CHAT_ID")
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]
SCAN_TIMEFRAMES         = ["15m", "30m", "1H"]
MAX_SIGNALS_PER_RUN     = int(os.getenv("MAX_SIGNALS", "12"))
SETUP_SCORE_THRESHOLD   = 68
# 订单流参数
CROSSLINE_BODY_RATIO       = 0.30
SWEEP_VOLUME_RATIO         = 1.8
SWEEP_CONSECUTIVE_MOVES    = 2
NEWS_COOLDOWN_MINUTES      = 60
ABSORPTION_VOL_MULTIPLIER  = 1.8
ABSORPTION_PRICE_THRESHOLD = 0.002
# v8 精度参数
VOLATILITY_HARD_LIMIT   = 0.035
ATR_SL_MULT             = 1.5
RSI_PERIOD              = 14
ADX_PERIOD              = 14
# 监控参数
ENTRY_TOLERANCE         = 0.002
ACTIVE_SIGNALS_FILE     = "active_signals.json"
TRADE_HISTORY_FILE      = "trade_history.json"
SIGNAL_EXPIRE_HOURS     = 24
# v9.1 新增参数
SIGNAL_COOLDOWN_HOURS       = 2
VWAP_PERIODS                = 50
MACD_FAST                   = 12
MACD_SLOW                   = 26
MACD_SIGNAL_PERIOD          = 9
_news_cooldown:    dict = {}
_SIGNAL_COOLDOWN:  dict = {}

# ─────────────────────────────────────────────────────────
# 2. 工具 & 通知
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str, parse_mode: str = "Markdown") -> bool:
    """发送Telegram消息，带重试机制"""
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("TG_TOKEN / CHAT_ID not set")
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
                logging.info("✅ Telegram消息发送成功")
                return True
            else:
                logging.warning(f"Telegram API返回错误: {r.status_code} - {r.text}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False
        except Exception as e:
            logging.error(f"Telegram发送失败 (尝试 {attempt+1}/{max_retries}): {e}")
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
# 2b. v9.1 辅助工具
# ─────────────────────────────────────────────────────────
def check_signal_cooldown(instId: str, side: str) -> bool:
    key  = f"{instId}_{side}"
    last = _SIGNAL_COOLDOWN.get(key, 0)
    return (time.time() - last) >= SIGNAL_COOLDOWN_HOURS * 3600

def set_signal_cooldown(instId: str, side: str):
    _SIGNAL_COOLDOWN[f"{instId}_{side}"] = time.time()

def get_market_session() -> str:
    h = utc_now().hour
    if   13 <= h < 22: return "🌎 美盘"
    elif  7 <= h < 16: return "🌍 欧盘"
    elif  1 <= h <  8: return "🌏 亚盘"
    else:              return "🌙 清淡"

def suggest_position_size(entry: float, sl: float,
                           account_size: float = 1000.0,
                           risk_pct: float = 0.01) -> str:
    try:
        sl_dist = abs(entry - sl)
        if sl_dist <= 0: return "─"
        sl_ratio    = sl_dist / entry
        pos_usdt    = account_size * risk_pct / sl_ratio
        leverage    = pos_usdt / account_size
        return f"≈{pos_usdt:.0f}U  (x{leverage:.1f} | 1%风控/{account_size:.0f}U)"
    except:
        return "─"

# ─────────────────────────────────────────────────────────
# 3. 数据抓取
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
        logging.warning(f"[{instId}/{tf}] Fetch: {e}")
        return None

def fetch_ticker_price(instId: str) -> float:
    """获取即时价格，带重试"""
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
            logging.warning(f"获取价格失败: {res}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return 0.0
        except Exception as e:
            logging.warning(f"获取价格异常 (尝试 {attempt+1}): {e}")
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
            return 1.0, "盘口均衡"
        data    = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio   = bid_vol / ask_vol
        if   ratio >= 1.30: label = f"买盘强势 ({ratio:.2f})"
        elif ratio >= 1.05: label = f"买盘略强 ({ratio:.2f})"
        elif ratio >= 0.95: label = f"盘口均衡 ({ratio:.2f})"
        elif ratio >= 0.77: label = f"卖盘略强 ({ratio:.2f})"
        else:               label = f"卖盘强势 ({ratio:.2f})"
        return ratio, label
    except: return 1.0, "盘口均衡"

def fetch_oi_analysis(instId: str) -> tuple:
    try:
        base = instId.split("-")[0]
        res  = requests.get(
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
        older  = sum(oi_vals[:2])  / 2
        chg    = (recent - older) / (older + 1e-10)
        if   chg >  0.05: return 1.0, f"OI持增 +{chg*100:.1f}%"
        elif chg >  0.01: return 0.7, f"OI微增 +{chg*100:.1f}%"
        elif chg > -0.01: return 0.5, f"OI持平"
        elif chg > -0.05: return 0.3, f"OI微降 {chg*100:.1f}%"
        else:             return 0.0, f"OI下降 {chg*100:.1f}%"
    except Exception as e:
        logging.debug(f"OI分析: {e}")
        return 0.5, "OI─"

# ─────────────────────────────────────────────────────────
# 4. 技术指标
# ─────────────────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["h"] - df["l"]
    hc = np.abs(df["h"] - df["c"].shift())
    lc = np.abs(df["l"] - df["c"].shift())
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
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
    hl2  = (h+l)/2.0
    bu   = hl2 - mult*atr
    bd   = hl2 + mult*atr
    fu   = np.zeros(n); fd = np.zeros(n)
    trend = np.ones(n, dtype=int)
    fu[period]=bu[period]; fd[period]=bd[period]
    for i in range(period+1, n):
        fu[i]=bu[i] if bu[i]>fu[i-1] or c[i-1]<fu[i-1] else fu[i-1]
        fd[i]=bd[i] if bd[i]<fd[i-1] or c[i-1]>fd[i-1] else fd[i-1]
        if   trend[i-1]==-1 and c[i]>fd[i-1]: trend[i]=1
        elif trend[i-1]==1  and c[i]<fu[i-1]: trend[i]=-1
        else: trend[i]=trend[i-1]
    if trend[-1]==1:  return  1,"多头"
    if trend[-1]==-1: return -1,"空头"
    return 0,"未知"

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["c"].diff()
    gain  = delta.where(delta>0, 0).rolling(period).mean()
    loss  = (-delta.where(delta<0, 0)).rolling(period).mean()
    rs    = gain / (loss + 1e-10)
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
        tr[i]  = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        pdm[i] = h_diff if h_diff>l_diff and h_diff>0 else 0
        mdm[i] = l_diff if l_diff>h_diff and l_diff>0 else 0
    atr_w = np.zeros(n); p_w = np.zeros(n); m_w = np.zeros(n)
    atr_w[period]=tr[1:period+1].sum()
    p_w[period]  =pdm[1:period+1].sum()
    m_w[period]  =mdm[1:period+1].sum()
    for i in range(period+1, n):
        atr_w[i] = atr_w[i-1]-atr_w[i-1]/period+tr[i]
        p_w[i]   = p_w[i-1]  -p_w[i-1]/period  +pdm[i]
        m_w[i]   = m_w[i-1]  -m_w[i-1]/period  +mdm[i]
    plus_di  = 100*p_w/(atr_w+1e-10)
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
    tp   = (data["h"] + data["l"] + data["c"]) / 3.0
    vol  = data["v"]
    cum_vol = vol.cumsum()
    vwap = (tp * vol).cumsum() / (cum_vol + 1e-10)
    val  = float(vwap.iloc[-1])
    return val if not np.isnan(val) else float(data["c"].iloc[-1])

def analyze_vwap_position(df: pd.DataFrame, side: str) -> tuple:
    vwap  = calculate_vwap(df)
    price = df["c"].iloc[-1]
    atr   = calculate_atr(df)
    if side == "LONG":
        if   price < vwap - atr * 0.3: return 1.0, f"VWAP之下 {vwap:.4f} ✅"
        elif price < vwap + atr * 0.3: return 0.7, f"VWAP附近 {vwap:.4f}"
        elif price < vwap + atr * 1.0: return 0.4, f"VWAP偏高 {vwap:.4f}"
        else:                          return 0.1, f"VWAP大幅偏高 {vwap:.4f}"
    else:
        if   price > vwap + atr * 0.3: return 1.0, f"VWAP之上 {vwap:.4f} ✅"
        elif price > vwap - atr * 0.3: return 0.7, f"VWAP附近 {vwap:.4f}"
        elif price > vwap - atr * 1.0: return 0.4, f"VWAP偏低 {vwap:.4f}"
        else:                          return 0.1, f"VWAP大幅偏低 {vwap:.4f}"

def calculate_macd(df: pd.DataFrame) -> tuple:
    close     = df["c"]
    ema_fast  = calculate_ema(close, MACD_FAST)
    ema_slow  = calculate_ema(close, MACD_SLOW)
    macd_line = ema_fast - ema_slow
    signal    = calculate_ema(macd_line, MACD_SIGNAL_PERIOD)
    histogram = macd_line - signal
    return macd_line, signal, histogram

def detect_macd_divergence(df: pd.DataFrame, side: str) -> tuple:
    if len(df) < MACD_SLOW + 20:
        return False, "MACD数据不足"
    macd, _, _ = calculate_macd(df)
    lookback   = 20
    macd_arr   = macd.tail(lookback).values
    price_h    = df["h"].tail(lookback).values
    price_l    = df["l"].tail(lookback).values
    mid        = lookback // 2
    if side == "LONG":
        prev_l = price_l[:mid].min(); curr_l = price_l[mid:].min()
        if curr_l < prev_l * 0.999:
            idx1 = int(np.argmin(price_l[:mid]))
            idx2 = mid + int(np.argmin(price_l[mid:]))
            m1   = macd_arr[idx1]; m2 = macd_arr[idx2]
            if m2 > m1 + abs(m1) * 0.05:
                return True, f"MACD看涨背离 ({m2:.4f}>{m1:.4f})"
    else:
        prev_h = price_h[:mid].max(); curr_h = price_h[mid:].max()
        if curr_h > prev_h * 1.001:
            idx1 = int(np.argmax(price_h[:mid]))
            idx2 = mid + int(np.argmax(price_h[mid:]))
            m1   = macd_arr[idx1]; m2 = macd_arr[idx2]
            if m2 < m1 - abs(m1) * 0.05:
                return True, f"MACD看跌背离 ({m2:.4f}<{m1:.4f})"
    return False, "无MACD背离"

# ─────────────────────────────────────────────────────────
# 5. 精度分析模块
# ─────────────────────────────────────────────────────────
def detect_market_regime(df: pd.DataFrame) -> dict:
    adx, pdi, mdi = calculate_adx(df, ADX_PERIOD)
    if   adx < 20: regime = "震荡市"; sc = 0.4
    elif adx < 25: regime = "弱趋势"; sc = 0.6
    elif adx < 40: regime = "强趋势"; sc = 0.9
    else:          regime = "极强趋势"; sc = 1.0
    trend_dir = "上升趋势" if pdi > mdi else "下降趋势"
    return {"regime": regime, "adx": adx, "trend_dir": trend_dir,
            "score": sc, "plus_di": pdi, "minus_di": mdi}

def adx_regime_bonus(regime: dict, side: str) -> tuple:
    adx = regime["adx"]
    is_uptrend = regime["trend_dir"] == "上升趋势"
    if adx >= 25:
        if (side=="LONG" and is_uptrend) or (side=="SHORT" and not is_uptrend):
            return 3, f"ADX趋势{adx:.0f} 顺势 +3"
        return 0, f"ADX趋势{adx:.0f} 逆势"
    else:
        if (side=="LONG" and not is_uptrend) or (side=="SHORT" and is_uptrend):
            return 3, f"ADX震荡{adx:.0f} 均值回归 +3"
        return 1, f"ADX震荡{adx:.0f}"

def detect_rsi_divergence(df: pd.DataFrame, side: str) -> tuple:
    rsi = calculate_rsi(df, RSI_PERIOD)
    if len(rsi) < 20: return False, "RSI数据不足", float(rsi.iloc[-1]) if len(rsi)>0 else 50.0
    lookback = 20
    rsi_arr   = rsi.tail(lookback).values
    price_h   = df["h"].tail(lookback).values
    price_l   = df["l"].tail(lookback).values
    cur_rsi   = float(rsi.iloc[-1])
    mid       = lookback // 2
    if side == "LONG":
        prev_l = price_l[:mid].min(); curr_l = price_l[mid:].min()
        idx1 = int(np.argmin(price_l[:mid])); idx2 = mid + int(np.argmin(price_l[mid:]))
        rsi_1 = rsi_arr[idx1]; rsi_2 = rsi_arr[idx2]
        if curr_l < prev_l * 0.999 and rsi_2 > rsi_1 + 3.0:
            return True, f"看涨背离 RSI={cur_rsi:.1f}", cur_rsi
    else:
        prev_h = price_h[:mid].max(); curr_h = price_h[mid:].max()
        idx1 = int(np.argmax(price_h[:mid])); idx2 = mid + int(np.argmax(price_h[mid:]))
        rsi_1 = rsi_arr[idx1]; rsi_2 = rsi_arr[idx2]
        if curr_h > prev_h * 1.001 and rsi_2 < rsi_1 - 3.0:
            return True, f"看跌背离 RSI={cur_rsi:.1f}", cur_rsi
    return False, f"无背离 RSI={cur_rsi:.1f}", cur_rsi

def get_btc_bias(side: str, _cache: dict) -> tuple:
    if "BTC_1H" not in _cache:
        _cache["BTC_1H"] = fetch_okx("BTC-USDT-SWAP", tf="1H", limit=20)
    df_btc = _cache["BTC_1H"]
    if df_btc is None: return 0.5, "BTC数据不足"
    st_val, _ = calculate_supertrend(df_btc)
    chg = (df_btc["c"].iloc[-1]-df_btc["c"].iloc[-6]) / (df_btc["c"].iloc[-6]+1e-10)
    if side == "LONG":
        if st_val==1  and chg>0:       return 1.0, f"BTC多头({chg*100:.1f}%)"
        elif st_val==1:                return 0.7, f"BTC ST多弱({chg*100:.1f}%)"
        elif st_val==-1 and chg<-0.02: return 0.1, f"BTC大跌({chg*100:.1f}%)"
        else:                          return 0.5, f"BTC中性({chg*100:.1f}%)"
    else:
        if st_val==-1 and chg<0:       return 1.0, f"BTC空头({chg*100:.1f}%)"
        elif st_val==-1:               return 0.7, f"BTC ST空弱({chg*100:.1f}%)"
        elif st_val==1  and chg>0.02:  return 0.1, f"BTC大涨({chg*100:.1f}%)"
        else:                          return 0.5, f"BTC中性({chg*100:.1f}%)"

def get_4h_trend(instId: str, side: str, _cache: dict) -> tuple:
    key = f"{instId}_4H"
    if key not in _cache:
        _cache[key] = fetch_okx(instId, tf="4H", limit=60)
    df4h = _cache[key]
    if df4h is None: return 0.5, "4H数据不足"
    st4, _ = calculate_supertrend(df4h)
    ema21  = calculate_ema(df4h["c"], 21).iloc[-1]
    price  = df4h["c"].iloc[-1]
    if side == "LONG":
        if st4==1 and price>ema21:  return 1.0, "4H多头排列"
        elif st4==1:                return 0.7, "4H ST多头"
        elif price>ema21:           return 0.5, "4H EMA多偏"
        else:                       return 0.2, "4H偏空"
    else:
        if st4==-1 and price<ema21: return 1.0, "4H空头排列"
        elif st4==-1:               return 0.7, "4H ST空头"
        elif price<ema21:           return 0.5, "4H EMA空偏"
        else:                       return 0.2, "4H偏多"

def check_extreme_volatility(df: pd.DataFrame) -> tuple:
    atr   = calculate_atr(df)
    price = df["c"].iloc[-1]
    ratio = atr / (price + 1e-10)
    if ratio > VOLATILITY_HARD_LIMIT:
        return False, f"极端波动 ATR={ratio*100:.2f}%"
    return True, f"波动正常 ATR={ratio*100:.2f}%"

def calculate_dynamic_sl(entry: float, side: str, atr: float,
                          support: float = None, resistance: float = None) -> float:
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
# 6. 摆动点 & 市场结构
# ─────────────────────────────────────────────────────────
def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    data = df.tail(lookback).reset_index(drop=True)
    sh_p, sl_p, sh_i, sl_i = [], [], [], []
    for i in range(n, len(data)-n):
        wh = data["h"].iloc[i-n:i+n+1]; wl = data["l"].iloc[i-n:i+n+1]
        if data["h"].iloc[i]==wh.max(): sh_p.append(data["h"].iloc[i]); sh_i.append(i)
        if data["l"].iloc[i]==wl.min(): sl_p.append(data["l"].iloc[i]); sl_i.append(i)
    return sh_p, sl_p, sh_i, sl_i

def detect_bos_choch(df: pd.DataFrame, side: str) -> tuple:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=80)
    price = df["c"].iloc[-1]; atr = calculate_atr(df)
    result, score = "无明显结构", 0.0
    if side=="LONG":
        if sl and df["l"].iloc[-4:-1].min() < sl[-1]-atr*0.1 and price>sl[-1]:
            result,score = f"CHoCH 扫低反弹 @ {sl[-1]:.4f}", 0.90
        elif sh and not score:
            if price>sh[-1]: result,score = f"BOS 向上突破 {sh[-1]:.4f}", 0.80
            elif len(sh)>=2 and price>sh[-2]: result,score = f"CHoCH 潜在转折 {sh[-2]:.4f}", 0.55
    else:
        if sh and df["h"].iloc[-4:-1].max() > sh[-1]+atr*0.1 and price<sh[-1]:
            result,score = f"CHoCH 扫高回落 @ {sh[-1]:.4f}", 0.90
        elif sl and not score:
            if price<sl[-1]: result,score = f"BOS 向下跌破 {sl[-1]:.4f}", 0.80
            elif len(sl)>=2 and price<sl[-2]: result,score = f"CHoCH 潜在转折 {sl[-2]:.4f}", 0.55
    return result, score

def detect_market_structure(df: pd.DataFrame, side: str) -> str:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=60)
    has_w = len(sl)>=2 and sl[-2]>0 and abs(sl[-2]-sl[-1])/sl[-2]<0.015
    has_m = len(sh)>=2 and sh[-2]>0 and abs(sh[-2]-sh[-1])/sh[-2]<0.015
    if side=="LONG":
        if has_w: return "W 底反转"
        if has_m: return "M 头压制"
    else:
        if has_m: return "M 头反转"
        if has_w: return "W 底支撑"
    recent = df.tail(20)
    slope  = (recent["c"].iloc[-1]-recent["c"].iloc[0])/(recent["c"].iloc[0]+1e-10)
    if slope>0.025:  return "上升趋势延续"
    if slope<-0.025: return "下降趋势延续"
    return "区间盘整"

# ─────────────────────────────────────────────────────────
# 7. 流动性猎取
# ─────────────────────────────────────────────────────────
def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    sh, sl, _, _ = find_swing_points(df, n=2, lookback=lookback)
    price = df["c"].iloc[-1]; atr = calculate_atr(df)
    res = dict(pools=[], sweep_detected=False, sweep_desc="", sweep_score=0.0,
               eqh=None, eql=None, nearest_bsl=None, nearest_ssl=None)
    for i in range(len(sh)-1, 0, -1):
        if abs(sh[i]-sh[i-1])/(sh[i-1]+1e-10)<0.003:
            res["eqh"]=(sh[i-1]+sh[i])/2
            res["pools"].append(f"EQH等高 {res['eqh']:.4f}"); break
    for i in range(len(sl)-1, 0, -1):
        if abs(sl[i]-sl[i-1])/(sl[i-1]+1e-10)<0.003:
            res["eql"]=(sl[i-1]+sl[i])/2
            res["pools"].append(f"EQL等低 {res['eql']:.4f}"); break
    bsl_c=[h for h in sh if h>price]; ssl_c=[l for l in sl if l<price]
    if bsl_c: res["nearest_bsl"]=min(bsl_c)
    if ssl_c: res["nearest_ssl"]=max(ssl_c)
    recent=df.tail(5)
    if side=="LONG":
        for lvl,is_eq in ([(res["eql"],True)] if res["eql"] else []) + ([(res["nearest_ssl"],False)] if res["nearest_ssl"] else []):
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k=recent.iloc[i]
                if k["l"]<lvl-atr*0.05 and k["c"]>lvl:
                    wick=(lvl-k["l"])/(atr+1e-10)
                    res["sweep_detected"]=True
                    res["sweep_desc"]=f"{'EQL' if is_eq else 'SSL'}扫除反弹 {k['l']:.4f}→{k['c']:.4f}"
                    res["sweep_score"]=0.95 if is_eq else min(0.55+wick*0.08,0.90); break
            if res["sweep_detected"]: break
    else:
        for lvl,is_eq in ([(res["eqh"],True)] if res["eqh"] else []) + ([(res["nearest_bsl"],False)] if res["nearest_bsl"] else []):
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k=recent.iloc[i]
                if k["h"]>lvl+atr*0.05 and k["c"]<lvl:
                    wick=(k["h"]-lvl)/(atr+1e-10)
                    res["sweep_detected"]=True
                    res["sweep_desc"]=f"{'EQH' if is_eq else 'BSL'}扫除回落 {k['h']:.4f}→{k['c']:.4f}"
                    res["sweep_score"]=0.95 if is_eq else min(0.55+wick*0.08,0.90); break
            if res["sweep_detected"]: break
    return res

# ─────────────────────────────────────────────────────────
# 8. Order Block & FVG
# ─────────────────────────────────────────────────────────
def find_order_blocks(df: pd.DataFrame, side: str, lookback: int = 60) -> list:
    data=df.tail(lookback).reset_index(drop=True)
    obs=[]; price=data["c"].iloc[-1]; atr=calculate_atr(data)
    for i in range(2, len(data)-3):
        c=data.iloc[i]
        if side=="LONG":
            if c["c"]<c["o"]:
                mv=data["h"].iloc[i+1:i+4].max()-c["h"]
                if mv>atr*1.5:
                    ob=dict(high=c["h"],low=c["l"],mid=(c["h"]+c["l"])/2,strength=mv/(atr+1e-10))
                    if ob["high"]<price*1.005: obs.append(ob)
        else:
            if c["c"]>c["o"]:
                mv=c["l"]-data["l"].iloc[i+1:i+4].min()
                if mv>atr*1.5:
                    ob=dict(high=c["h"],low=c["l"],mid=(c["h"]+c["l"])/2,strength=mv/(atr+1e-10))
                    if ob["low"]>price*0.995: obs.append(ob)
    obs.sort(key=lambda x:x["strength"],reverse=True)
    return obs[:2]

def find_fvg(df: pd.DataFrame, side: str, lookback: int = 40) -> list:
    data=df.tail(lookback).reset_index(drop=True)
    fvgs=[]; price=data["c"].iloc[-1]
    for i in range(2, len(data)):
        if side=="LONG":
            bot,top=data["h"].iloc[i-2],data["l"].iloc[i]
            if top>bot and bot<price: fvgs.append(dict(top=top,bottom=bot,mid=(top+bot)/2,size=top-bot))
        else:
            top,bot=data["l"].iloc[i-2],data["h"].iloc[i]
            if bot<top and top>price: fvgs.append(dict(top=top,bottom=bot,mid=(top+bot)/2,size=top-bot))
    return fvgs[-2:] if fvgs else []

def check_ob_fvg_entry(df: pd.DataFrame, side: str, atr: float) -> tuple:
    price=df["c"].iloc[-1]; obs=find_order_blocks(df,side); fvgs=find_fvg(df,side)
    at_ob=at_fvg=False; ob_d="无OB"; fvg_d="无FVG"; ez=price
    for ob in obs:
        if ob["low"]-atr*0.5<=price<=ob["high"]+atr*0.5:
            at_ob=True; ob_d=f"在OB [{ob['low']:.4f}~{ob['high']:.4f}] 强{ob['strength']:.1f}x"; ez=ob["mid"]; break
        else: ob_d=f"OB [{ob['low']:.4f}~{ob['high']:.4f}]"
    for fvg in reversed(fvgs):
        if fvg["bottom"]-atr*0.3<=price<=fvg["top"]+atr*0.3:
            at_fvg=True; fvg_d=f"在FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
            if not at_ob: ez=fvg["mid"]; break
        else: fvg_d=f"FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
    return at_ob, at_fvg, ob_d, fvg_d, ez

def detect_premium_discount(df: pd.DataFrame, side: str) -> tuple:
    sh,sl,_,_=find_swing_points(df,n=3,lookback=50)
    price=df["c"].iloc[-1]
    if not sh or not sl: return "无法判断",0.5
    hi=max(sh[-2:]) if len(sh)>=2 else sh[-1]; lo=min(sl[-2:]) if len(sl)>=2 else sl[-1]
    rng=hi-lo
    if rng<=0: return "无法判断",0.5
    fib=(price-lo)/rng
    if side=="LONG":
        if   fib<=0.35: return f"Discount {fib*100:.0f}% 做多优质",1.0
        elif fib<=0.5:  return f"均衡偏低 {fib*100:.0f}%",0.6
        elif fib<=0.65: return f"均衡偏高 {fib*100:.0f}%",0.3
        else:           return f"Premium {fib*100:.0f}% 做多不利",0.0
    else:
        if   fib>=0.65: return f"Premium {fib*100:.0f}% 做空优质",1.0
        elif fib>=0.5:  return f"均衡偏高 {fib*100:.0f}%",0.6
        elif fib>=0.35: return f"均衡偏低 {fib*100:.0f}%",0.3
        else:           return f"Discount {fib*100:.0f}% 做空不利",0.0

# ─────────────────────────────────────────────────────────
# 9. 订单流
# ─────────────────────────────────────────────────────────
def detect_crossline(df: pd.DataFrame, lookback: int = 15):
    for i in range(len(df)-1, max(len(df)-lookback-1,0), -1):
        k=df.iloc[i]; body=abs(k["c"]-k["o"]); rng=k["h"]-k["l"]+1e-10
        if body<CROSSLINE_BODY_RATIO*rng:
            uw=k["h"]-max(k["c"],k["o"]); dw=min(k["c"],k["o"])-k["l"]
            pot="SHORT" if uw>dw*1.5 else ("LONG" if dw>uw*1.5 else "NEUTRAL")
            dist=len(df)-1-i
            return dict(price=k["c"],high=k["h"],low=k["l"],body_ratio=body/rng,
                        potential_side=pot,distance=dist,
                        desc=f"十字线@{k['c']:.4f}({pot},{dist}根前)")
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> tuple:
    if len(df)<8: return False,0.0,"数据不足"
    recent=df.tail(8); vol_ma=df["v"].tail(20).mean()
    vol_sc=recent.iloc[-1]["v"]/(vol_ma+1e-10)
    if vol_sc<SWEEP_VOLUME_RATIO: return False,0.0,f"量能不足({vol_sc:.1f}x)"
    moves=0
    for i in range(len(recent)-1,0,-1):
        if side=="LONG"  and recent["c"].iloc[i]>recent["c"].iloc[i-1]: moves+=1
        elif side=="SHORT" and recent["c"].iloc[i]<recent["c"].iloc[i-1]: moves+=1
        else: break
    if moves>=SWEEP_CONSECUTIVE_MOVES:
        return True,min(vol_sc/3.0,1.0),f"主动扫单 连续{moves}根 {vol_sc:.1f}x"
    return False,0.0,f"无连续扫单({moves}根)"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df)<6: return False
    recent=df.tail(6); vol_ma=df["v"].tail(20).mean()
    mv=abs(recent["c"].iloc[-1]-recent["c"].iloc[0])/(recent["c"].iloc[0]+1e-10)
    return mv>=0.005 and recent["v"].iloc[-1]<0.75*vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> tuple:
    if len(df)<15: return False,"无吸收"
    recent=df.tail(5); vol_ma=df["v"].tail(20).mean()
    avg3=recent["v"].iloc[-3:].mean()
    chg=abs(recent["c"].iloc[-1]-recent["c"].iloc[-4])/(recent["c"].iloc[-4]+1e-10)
    if avg3>ABSORPTION_VOL_MULTIPLIER*vol_ma and chg<ABSORPTION_PRICE_THRESHOLD:
        return True,f"吸收 量{avg3/vol_ma:.1f}x 价动{chg*100:.2f}%"
    return False,"无吸收"

# ─────────────────────────────────────────────────────────
# 10. 市场情绪
# ─────────────────────────────────────────────────────────
def calculate_cvd(df: pd.DataFrame, periods: int = 50) -> tuple:
    data  = df.tail(periods).copy()
    delta = np.where(data["c"]>data["o"], data["v"],
                     np.where(data["c"]<data["o"], -data["v"], 0))
    cvd   = np.cumsum(delta); cur = cvd[-1]
    slope = cur - (cvd[-10] if len(cvd)>=10 else cvd[0])
    if slope>0 and cur>0:   lb,sc = f"买盘累积 CVD+{cur:,.0f}", 1.0
    elif slope>0 and cur<0: lb,sc = f"CVD底部翻正(吸筹)", 0.65
    elif slope<0 and cur<0: lb,sc = f"卖盘累积 CVD{cur:,.0f}", 1.0
    elif slope<0 and cur>0: lb,sc = f"CVD顶部翻负(出货)", 0.65
    else:                   lb,sc = f"CVD持平", 0.3
    return cur, slope, lb, sc

def interpret_ls_ratio(ratio: float, side: str) -> tuple:
    if   ratio>=2.5: senti=f"极度多头拥挤({ratio:.2f}) 逆向偏空"
    elif ratio>=1.8: senti=f"多头拥挤({ratio:.2f}) 谨慎做多"
    elif ratio>=1.2: senti=f"略偏多头({ratio:.2f})"
    elif ratio>=0.8: senti=f"均衡({ratio:.2f})"
    elif ratio>=0.5: senti=f"空头拥挤({ratio:.2f}) 谨慎做空"
    else:            senti=f"极度空头拥挤({ratio:.2f}) 逆向偏多"
    if side=="LONG": sc=1.0 if ratio<0.8 else(0.7 if ratio<1.2 else(0.4 if ratio<1.8 else 0.1))
    else:            sc=1.0 if ratio>2.0 else(0.7 if ratio>1.5 else(0.4 if ratio>1.0 else 0.1))
    return sc, senti

def interpret_funding_rate(fr: float, side: str) -> tuple:
    p=fr*100
    if side=="LONG":
        if   fr<-0.0003: return 1.0,f"费率极佳{p:.4f}%(空头付费)"
        elif fr< 0.0001: return 0.8,f"费率友善{p:.4f}%"
        elif fr< 0.0003: return 0.5,f"费率尚可{p:.4f}%"
        elif fr< 0.0008: return 0.2,f"费率不佳{p:.4f}%"
        else:            return 0.0,f"费率禁入{p:.4f}%"
    else:
        if   fr> 0.0008: return 1.0,f"费率极佳{p:.4f}%(多头付费)"
        elif fr> 0.0003: return 0.8,f"费率友善{p:.4f}%"
        elif fr> 0.0001: return 0.5,f"费率尚可{p:.4f}%"
        elif fr>-0.0003: return 0.2,f"费率不佳{p:.4f}%"
        else:            return 0.0,f"费率禁入{p:.4f}%"

def check_ob_direction(side: str, ob_r: float) -> tuple:
    if side=="LONG":
        if   ob_r>=1.30: return 1.0,f"买盘强势({ob_r:.2f})"
        elif ob_r>=1.05: return 0.7,f"买盘略强({ob_r:.2f})"
        elif ob_r>=0.95: return 0.3,f"盘口均衡({ob_r:.2f})"
        else:            return 0.0,f"卖盘主导，做多风险({ob_r:.2f})"
    else:
        if   ob_r<=0.77: return 1.0,f"卖盘强势({ob_r:.2f})"
        elif ob_r<=0.95: return 0.7,f"卖盘略强({ob_r:.2f})"
        elif ob_r<=1.05: return 0.3,f"盘口均衡({ob_r:.2f})"
        else:            return 0.0,f"买盘主导，做空风险({ob_r:.2f})"

def detect_pa(df: pd.DataFrame, side: str) -> tuple:
    sigs=[]
    for i in range(len(df)-1, max(len(df)-6,0), -1):
        k=df.iloc[i]; body=abs(k["c"]-k["o"]); rng=k["h"]-k["l"]+1e-10
        uw=k["h"]-max(k["c"],k["o"]); dw=min(k["c"],k["o"])-k["l"]; bp=body/rng
        if side=="SHORT" and uw>=body*2.0 and dw<=body*0.5: sigs.append(f"空头流星线({min(uw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="LONG"  and dw>=body*2.0 and uw<=body*0.5: sigs.append(f"多头锤子线({min(dw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="SHORT" and uw/rng>0.40 and k["c"]<k["o"]: sigs.append(f"压力拒绝(上影{uw/rng*100:.0f}%)@{k['c']:.4f}")
        if side=="LONG"  and dw/rng>0.40 and k["c"]>k["o"]: sigs.append(f"支撑拒绝(下影{dw/rng*100:.0f}%)@{k['c']:.4f}")
        if bp>=0.70 and ((side=="LONG" and k["c"]>k["o"]) or (side=="SHORT" and k["c"]<k["o"])):
            sigs.append(f"{'多' if side=='LONG' else '空'}头动量棒({bp*100:.0f}%)@{k['c']:.4f}")
    sigs=sigs[:3]
    sc=0.6 if len(sigs)>=3 else(0.4 if len(sigs)>=2 else(0.2 if sigs else 0.0))
    last=df.iloc[-1]; body=abs(last["c"]-last["o"]); rng=last["h"]-last["l"]+1e-10
    if body/rng>0.70: sc+=0.20
    if (side=="LONG" and last["c"]>last["o"]) or (side=="SHORT" and last["c"]<last["o"]): sc+=0.20
    sc=min(sc,1.0); lb="强PA" if sc>=0.65 else("弱PA" if sc>=0.40 else "无PA")
    return sc*100, lb, sigs

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    zones=[]; vm=df["v"].rolling(20).mean(); vs=df["v"].rolling(20).std()
    for i in range(max(len(df)-10,0), len(df)):
        if df["v"].iloc[i]>vm.iloc[i]+2*vs.iloc[i]:
            if df["c"].iloc[i]>df["o"].iloc[i] and side=="LONG": zones.append(f"主力吸筹 {df['c'].iloc[i]:.4f}")
            elif df["c"].iloc[i]<df["o"].iloc[i] and side=="SHORT": zones.append(f"主力派发 {df['c'].iloc[i]:.4f}")
    hi=df["h"].iloc[-20:].max(); lo=df["l"].iloc[-20:].min()
    zones.append(f"{'多头清算' if side=='SHORT' else '空头清算'} {hi if side=='SHORT' else lo:.4f}")
    return zones[:2]

# ─────────────────────────────────────────────────────────
# 11. 评分系统
# ─────────────────────────────────────────────────────────
def calculate_score(p: dict) -> tuple:
    sc=0.0; bd=[]; side=p["side"]
    htf=p.get("htf_trend","UNKNOWN")
    if htf==side:                      sc+=20; bd.append("HTF+20")
    elif htf in("NEUTRAL","UNKNOWN"):  sc+=8;  bd.append("HTF+8")
    else:                              sc+=0;  bd.append("HTF+0")
    at_ob=p.get("at_ob",False); at_fvg=p.get("at_fvg",False)
    if at_ob and at_fvg:  sc+=18; bd.append("OB+FVG+18")
    elif at_ob:           sc+=15; bd.append("OB+15")
    elif at_fvg:          sc+=12; bd.append("FVG+12")
    pts=round(p.get("sweep_score",0)*18); sc+=pts
    if pts: bd.append(f"扫除+{pts}")
    pts=round(p.get("active_sweep_score",0)*13); sc+=pts
    if pts: bd.append(f"主动扫+{pts}")
    pts=round(p.get("crossline_score",0)*8); sc+=pts
    if pts: bd.append(f"十字+{pts}")
    pts=round(p.get("absorption_score",0)*7); sc+=pts
    if pts: bd.append(f"吸收+{pts}")
    pts=round(p.get("cvd_score",0)*12); sc+=pts; bd.append(f"CVD+{pts}")
    pts=round(p.get("ls_score",0)*8);   sc+=pts; bd.append(f"LS+{pts}")
    pts=round(p.get("fr_score",0)*5);   sc+=pts; bd.append(f"FR+{pts}")
    pts=round(p.get("ob_dir_score",0)*5); sc+=pts; bd.append(f"盘口+{pts}")
    if p.get("bos_score",0)>=0.75:    sc+=5; bd.append("BOS+5")
    pts=round(p.get("trend_4h_score",0)*5)
    if pts: sc+=pts; bd.append(f"4H+{pts}")
    if p.get("has_rsi_divergence",False): sc+=5; bd.append("RSI+5")
    pts=round(p.get("btc_score",0)*3)
    if pts: sc+=pts; bd.append(f"BTC+{pts}")
    adx_b=p.get("adx_bonus",0)
    if adx_b: sc+=adx_b; bd.append(f"ADX+{adx_b}")
    if p.get("pd_score",0)>=0.7: sc+=3; bd.append("PD+3")
    vwap_pts = round(p.get("vwap_score", 0.0) * 5)
    if vwap_pts: sc += vwap_pts; bd.append(f"VWAP+{vwap_pts}")
    oi_pts = round(p.get("oi_score", 0.0) * 4)
    if oi_pts: sc += oi_pts; bd.append(f"OI+{oi_pts}")
    if p.get("has_macd_divergence", False): sc += 4; bd.append("MACD背离+4")
    if htf not in(side,"NEUTRAL","UNKNOWN"): sc-=15; bd.append("HTF逆-15")
    if p.get("fr_score",1)==0.0:             sc-=10; bd.append("FR禁-10")
    if p.get("ob_dir_score",1)==0.0:         sc-=10; bd.append("盘口反-10")
    sc=max(0,min(round(sc),100))
    if   sc>=88: grade="A+ 极强"
    elif sc>=75: grade="A  强力"
    elif sc>=65: grade="B+ 观望"
    elif sc>=55: grade="B  偏弱"
    else:        grade="C  跳过"
    return sc, grade, bd

# ─────────────────────────────────────────────────────────
# 12. 主扫描逻辑
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str,
                   htf_trend: str, fr: float, ls_f: float, ls_str: str,
                   ob_r: float, oi_sc: float, oi_lb: str,
                   _cache: dict) -> list:
    df = fetch_okx(instId, tf=tf, limit=150)
    if df is None or len(df) < 50: return []
    vol_ok, vol_msg = check_extreme_volatility(df)
    if not vol_ok:
        logging.info(f"  [{instId}/{tf}] {vol_msg}"); return []
    atr = calculate_atr(df); _, st_lb = calculate_supertrend(df)
    regime = detect_market_regime(df); cl = detect_crossline(df)
    abs_b, abs_d = detect_absorption(df, "LONG")
    has_rsi_long,  rsi_d_long,  rsi_v = detect_rsi_divergence(df, "LONG")
    has_rsi_short, rsi_d_short, _     = detect_rsi_divergence(df, "SHORT")
    opportunities = []
    for side in ["LONG", "SHORT"]:
        ob_dir_sc, ob_dir_lb = check_ob_direction(side, ob_r)
        fr_sc, fr_lb = interpret_funding_rate(fr, side)
        if detect_fishing_trap(df, side): continue
        cvd_cur, cvd_sl, cvd_lb, cvd_sc_raw = calculate_cvd(df)
        cvd_aligned = (side=="LONG" and cvd_sl>0) or (side=="SHORT" and cvd_sl<0)
        eff_cvd_sc  = cvd_sc_raw if cvd_aligned else cvd_sc_raw*0.25
        liq              = find_liquidity_pools(df, side)
        bos_desc, bos_sc = detect_bos_choch(df, side)
        at_ob,at_fvg,ob_d,fvg_d,ez = check_ob_fvg_entry(df, side, atr)
        pd_lb, pd_sc     = detect_premium_discount(df, side)
        pa_sc,pa_lb,pa_sigs = detect_pa(df, side)
        structure        = detect_market_structure(df, side)
        whale_zones      = detect_whale_zones(df, side)
        ls_sc, ls_lb     = interpret_ls_ratio(ls_f, side)
        as_bool,as_sc,as_d = detect_active_sweep(df, side)
        vwap_sc, vwap_lb   = analyze_vwap_position(df, side)
        has_macd, macd_d   = detect_macd_divergence(df, side)
        cl_sc = 0.0
        if cl:
            pot=cl["potential_side"]
            if pot==side or pot=="NEUTRAL":
                cl_sc = max(0.0, 1.0 - cl["distance"]/10) * 0.6 + 0.4
        has_rsi = has_rsi_long if side=="LONG" else has_rsi_short
        rsi_d   = rsi_d_long  if side=="LONG" else rsi_d_short
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
            logging.info(f"  [{instId}/{tf}/{side}] {score}分 < {SETUP_SCORE_THRESHOLD}，跳过"); continue
        price = df["c"].iloc[-1]
        sh,sl,_,_ = find_swing_points(df, n=2, lookback=30)
        support    = max([s for s in sl if s<price], default=None)
        resistance = min([h for h in sh if h>price], default=None)
        if liq["sweep_detected"]:      entry = price
        elif at_ob or at_fvg:          entry = ez
        elif cl:                       entry = cl["low"] if side=="LONG" else cl["high"]
        elif side=="LONG" and liq["nearest_ssl"]: entry = liq["nearest_ssl"]*1.001
        elif side=="SHORT" and liq["nearest_bsl"]: entry = liq["nearest_bsl"]*0.999
        else:                          entry = price

        # ✨ v9.1.2 方向校验：抓即时价做 sanity check
        # LONG 不追高（进场应 ≤ 当前，等回踩）
        # SHORT 不追低（进场应 ≥ 当前，等反弹）
        live = fetch_ticker_price(instId) or price
        tol_px = 0.0005  # 容许 0.05% 误差（高波动币可微调）
        if side == "LONG" and entry > live * (1 + tol_px):
            logging.info(f"  [校正/{instId}/{tf}] LONG 进场 {entry:.4f} > 当前 {live:.4f}，改为当前价")
            entry = live
        elif side == "SHORT" and entry < live * (1 - tol_px):
            logging.info(f"  [校正/{instId}/{tf}] SHORT 进场 {entry:.4f} < 当前 {live:.4f}，改为当前价")
            entry = live
        # 若即时价已远离预设进场区（> 0.8% ATR 比例），直接跳过这个讯号
        atr_ratio = atr / (live + 1e-10)
        deviation = abs(live - entry) / (live + 1e-10)
        if deviation > max(atr_ratio * 0.8, 0.008):
            logging.info(f"  [略过/{instId}/{tf}/{side}] 即时价 {live:.4f} 偏离进场 {entry:.4f} 过远 ({deviation*100:.2f}%)")
            continue

        sl_price = calculate_dynamic_sl(entry, side, atr, support, resistance)
        risk     = abs(entry - sl_price)
        tp1 = entry+risk     if side=="LONG" else entry-risk
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
        v,_ = calculate_supertrend(htf_df)
        htf_trend_str = "LONG" if v==1 else ("SHORT" if v==-1 else "NEUTRAL")
        _cache[f"{instId}_1H"] = htf_df
    fr           = fetch_funding_rate(instId)
    ls_f, ls_str = fetch_ls_ratio(instId)
    ob_r, _      = fetch_order_book(instId)
    oi_sc, oi_lb = fetch_oi_analysis(instId)
    all_opps = []
    for tf in SCAN_TIMEFRAMES:
        try:
            opps = scan_timeframe(instId, tf, htf_trend_str, fr, ls_f, ls_str,
                                  ob_r, oi_sc, oi_lb, _cache)
            all_opps.extend(opps)
        except Exception as e:
            logging.error(f"  [{instId}/{tf}] {e}")
    seen={}
    for opp in all_opps:
        k=f"{opp['side']}_{opp['tf']}"
        if k not in seen or opp["score"]>seen[k]["score"]: seen[k]=opp
    return list(seen.values())

# ─────────────────────────────────────────────────────────
# 13. 扫描讯号格式化
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    coin    = opp["instId"].split("-")[0]
    is_long = opp["side"] == "LONG"
    arrow   = "🟢" if is_long else "🔴"
    dir_txt = "LONG" if is_long else "SHORT"
    sign    = "+" if is_long else "-"
    sl_sign = "-" if is_long else "+"
    htf_e   = {"LONG":"🟢","SHORT":"🔴","NEUTRAL":"⚪","UNKNOWN":"⚪"}.get(opp["htf_trend"],"⚪")
    entry   = opp["entry"]
    sl_pct  = abs(entry - opp["sl"])  / entry * 100
    tp1_pct = abs(opp["tp1"] - entry) / entry * 100
    tp2_pct = abs(opp["tp2"] - entry) / entry * 100
    tp3_pct = abs(opp["tp3"] - entry) / entry * 100
    liq      = opp["liq"]
    regime   = opp["regime"]
    session  = opp.get("session", get_market_session())
    vwap_lb  = opp.get("vwap_lb", "")
    oi_lb    = opp.get("oi_lb",   "")
    pos_hint = opp.get("pos_hint","─")
    top_bd  = [x for x in opp["breakdown"] if not x.endswith("+0")][:5]
    bd_line = "  ".join(top_bd)
    grade_icon = {"S":"🏆","A":"⭐","B":"✅","C":"📊"}.get(opp.get("grade","C"), "📊")
    triggers = []
    if liq["sweep_detected"]:                          triggers.append(f"💧 {liq['sweep_desc']}")
    if opp["at_ob"]:                                   triggers.append(f"🟦 {opp['ob_d']}")
    if opp["at_fvg"]:                                  triggers.append(f"🟩 {opp['fvg_d']}")
    if opp["bos_desc"] not in ("无明显结构", ""):      triggers.append(f"🏗 {opp['bos_desc']}")
    if opp["as_bool"]:                                 triggers.append(f"⚡ {opp['as_d']}")
    if opp["has_rsi"]:                                 triggers.append(f"📉 {opp['rsi_d']}")
    if opp.get("has_macd"):                            triggers.append(f"〽️ {opp['macd_d']}")
    if not triggers:                                   triggers.append("⚪ 等待进场区确认")
    trigger_txt = "\n".join(f"  • {t}" for t in triggers[:4])
    liq_parts = []
    if liq["nearest_bsl"]: liq_parts.append(f"BSL `{liq['nearest_bsl']:.2f}`")
    if liq["nearest_ssl"]: liq_parts.append(f"SSL `{liq['nearest_ssl']:.2f}`")
    if liq["eqh"]:         liq_parts.append(f"EQH `{liq['eqh']:.2f}`")
    if liq["eql"]:         liq_parts.append(f"EQL `{liq['eql']:.2f}`")
    liq_line = "  ·  ".join(liq_parts) if liq_parts else "─"
    ctx = []
    ctx.append(f"ADX {regime['adx']:.0f} {regime['regime']}")
    if vwap_lb: ctx.append(vwap_lb)
    if oi_lb:   ctx.append(oi_lb)
    ctx.append(opp["btc_lb"])
    ctx_line = "  ·  ".join(ctx)
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow} *#{coin} · {dir_txt}*  {grade_icon} *{opp['score']}分*  {session}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ `{opp['tf']}`  ·  1H {htf_e}  ·  {opp['t4h_lb']}  [{opp['lev']}]\n"
        f"📊 {bd_line}\n"
        f"\n"
        f"📌 进场    `{entry:.4f}`\n"
        f"🛑 止损    `{opp['sl']:.4f}`  `{sl_sign}{sl_pct:.2f}%`\n"
        f"\n"
        f"🥇 TP1    `{opp['tp1']:.4f}`  `{sign}{tp1_pct:.2f}%`  ⅓仓\n"
        f"🥈 TP2    `{opp['tp2']:.4f}`  `{sign}{tp2_pct:.2f}%`  ⅓仓\n"
        f"🏆 TP3    `{opp['tp3']:.4f}`  `{sign}{tp3_pct:.2f}%`  ⅓仓\n"
        f"💼 {pos_hint}\n"
        f"─────────────────────────\n"
        f"{trigger_txt}\n"
        f"─────────────────────────\n"
        f"🗺 {opp['structure']}  ·  P/D {opp['pd_lb']}  ·  {liq_line}\n"
        f"📡 {ctx_line}\n"
        f"🧬 {opp['cvd_lb']}  ·  多空比 {opp['ls_str']}  ·  💸 {opp['fr_lb']}"
    )

# ─────────────────────────────────────────────────────────
# 13b. 追踪讯号格式化（v9.1.1 重排版：进度条 + 分段留白）
# ─────────────────────────────────────────────────────────
def _progress_bar(hit_tp1: bool, hit_tp2: bool, hit_tp3: bool = False) -> str:
    """产生 [🥇✅ 🥈⏳ ⏳] 格式的进度条"""
    p1 = "🥇✅" if hit_tp1 else "🥇⏳"
    p2 = "🥈✅" if hit_tp2 else "🥈⏳"
    p3 = "🏆✅" if hit_tp3 else "🏆"
    return f"[ {p1}  {p2}  {p3} ]"

def format_alert(coin: str, side: str, alert_type: str,
                 price: float, entry: float, sl: float,
                 tp1: float, tp2: float, tp3: float,
                 new_sl: float = None, score: int = 0) -> str:
    arrow = "🟢" if side == "LONG" else "🔴"
    st    = "多" if side == "LONG" else "空"
    sign  = "+" if side == "LONG" else "-"

    # ════════════════════════════════════════════
    # ENTRY — 进场提醒
    # ════════════════════════════════════════════
    if alert_type == "ENTRY":
        sl_pct  = abs(entry - sl)  / entry * 100
        tp1_pct = abs(tp1   - entry) / entry * 100
        tp2_pct = abs(tp2   - entry) / entry * 100
        tp3_pct = abs(tp3   - entry) / entry * 100
        sl_sign = "-" if side == "LONG" else "+"
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *进场提醒*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💰 *价格到达进场区！*\n"
            f"\n"
            f"📍 当前价    `{price:.4f}`\n"
            f"📌 进场价    `{entry:.4f}`\n"
            f"📊 评分      `{score}分`\n"
            f"\n"
            f"─────────────────────────\n"
            f"🛑 止损    `{sl:.4f}`    `{sl_sign}{sl_pct:.2f}%`\n"
            f"🥇 TP1     `{tp1:.4f}`    `{sign}{tp1_pct:.2f}%`   ⅓仓\n"
            f"🥈 TP2     `{tp2:.4f}`    `{sign}{tp2_pct:.2f}%`   ⅓仓\n"
            f"🏆 TP3     `{tp3:.4f}`    `{sign}{tp3_pct:.2f}%`   ⅓仓\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 *三段止盈 + 动态追踪止损已启动*\n"
            f"   到 TP1 → SL 自动移至保本\n"
            f"   到 TP2 → SL 自动移至 TP1\n"
            f"   到 TP3 → 完美收割"
        )

    # ════════════════════════════════════════════
    # TP1 — 到达第一目标，保本移损
    # ════════════════════════════════════════════
    elif alert_type == "TP1":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        tp2_pct = abs(tp2 - entry) / entry * 100
        tp3_pct = abs(tp3 - entry) / entry * 100
        new_sl_str = f"`{new_sl:.4f}`" if new_sl else f"`{entry:.4f}`"
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP1 达标！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 当前    `{price:.4f}`    `{sign}{pnl:.2f}%`\n"
            f"\n"
            f"进度   {_progress_bar(True, False, False)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"✅ TP1 `{tp1:.4f}`  已达成\n"
            f"🛡 SL 移至 {new_sl_str}  *(保本)*\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 *操作建议*\n"
            f"   • 平仓  ⅓  部位锁定获利\n"
            f"   • 剩余  ⅔  续抱追击\n"
            f"\n"
            f"🎯 *下一目标*\n"
            f"   🥈 TP2   `{tp2:.4f}`   `{sign}{tp2_pct:.2f}%`\n"
            f"   🏆 TP3   `{tp3:.4f}`   `{sign}{tp3_pct:.2f}%`"
        )

    # ════════════════════════════════════════════
    # TP2 — 到达第二目标，移损到 TP1 锁利
    # ════════════════════════════════════════════
    elif alert_type == "TP2":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        tp3_pct = abs(tp3 - entry) / entry * 100
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP2 达标！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 当前    `{price:.4f}`    `{sign}{pnl:.2f}%`\n"
            f"\n"
            f"进度   {_progress_bar(True, True, False)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"✅ TP2 `{tp2:.4f}`  已达成\n"
            f"🛡 SL 移至 `{tp1:.4f}`  *(锁利)*\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 *操作建议*\n"
            f"   • 再平仓  ⅓  部位落袋\n"
            f"   • 剩余  ⅓  冲击 TP3\n"
            f"\n"
            f"🏆 *最终目标*\n"
            f"   TP3   `{tp3:.4f}`   `{sign}{tp3_pct:.2f}%` 🚀"
        )

    # ════════════════════════════════════════════
    # TP3 — 完美收割
    # ════════════════════════════════════════════
    elif alert_type == "TP3":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *TP3 完美收割！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💎 当前    `{price:.4f}`    `{sign}{pnl:.2f}%`\n"
            f"\n"
            f"进度   {_progress_bar(True, True, True)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"🎉 *三段止盈全部达成！*\n"
            f"🏆 TP3 `{tp3:.4f}`  已达成\n"
            f"─────────────────────────\n"
            f"\n"
            f"💡 建议 *立即平仓全部剩余部位*\n"
            f"📊 本单表现   🌟🌟 优秀\n"
            f"\n"
            f"恭喜获利 🎊"
        )

    # ════════════════════════════════════════════
    # SL — 止损 / 保本止损
    # ════════════════════════════════════════════
    elif alert_type == "SL":
        pnl = ((price - entry) / entry * 100) if side == "LONG" else ((entry - price) / entry * 100)
        is_be      = new_sl is not None and abs(new_sl - entry) < entry * 0.0001
        label      = "保本止损" if is_be else "止损触发"
        header_em  = "🛡" if is_be else "🛑"
        sl_display = f"`{new_sl:.4f}`" if new_sl else f"`{sl:.4f}`"
        if is_be:
            outcome = ("💡 仓位已平仓于成本价\n"
                       "   资金安全，等下一个机会 💪")
        else:
            outcome = ("⚠️ 仓位已止损出场\n"
                       "   请遵守风控，莫加码摊平")
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{header_em} *{label}*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 当前    `{price:.4f}`    `{pnl:+.2f}%`\n"
            f"\n"
            f"─────────────────────────\n"
            f"{header_em} 止损价 {sl_display}  已触发\n"
            f"─────────────────────────\n"
            f"\n"
            f"{outcome}"
        )

    return ""

# ─────────────────────────────────────────────────────────
# 14. WinRateTracker — 胜率统计 & 战报
# ─────────────────────────────────────────────────────────
class WinRateTracker:
    """
    记录每笔已结算交易，持久化到 trade_history.json。
    close_type: TP1 / TP2 / TP3 (胜) | BE (保本平手) | SL (败)
    """
    def __init__(self, filepath: str = TRADE_HISTORY_FILE):
        self.filepath = filepath
        self._lock    = threading.Lock()
        self.history  = self._load()

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

    def record(self, coin: str, side: str, tf: str,
               entry: float, close_price: float,
               close_type: str, score: int):
        is_win = close_type in ("TP1", "TP2", "TP3")
        is_be  = (close_type == "BE")
        pnl_pct = ((close_price - entry) / entry * 100
                   if side == "LONG"
                   else (entry - close_price) / entry * 100)
        now = utc_now()
        rec = {
            "time":       now.strftime("%Y-%m-%d %H:%M"),
            "date":       now.strftime("%Y-%m-%d"),
            "month":      now.strftime("%Y-%m"),
            "coin":       coin, "side": side, "tf": tf,
            "entry":      round(entry, 6), "close": round(close_price, 6),
            "close_type": close_type, "pnl_pct": round(pnl_pct, 3),
            "is_win":     is_win, "is_be": is_be, "score": score,
        }
        with self._lock:
            self.history.append(rec)
            self._save()
        logging.info(f"📝 记录 {coin} {side} {close_type} {pnl_pct:+.2f}%")

    def _stats(self, trades: list):
        if not trades: return None
        wins   = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"] and not t.get("is_be")]
        be     = [t for t in trades if t.get("is_be")]
        total  = len(trades)
        win_r  = len(wins) / total * 100
        avg_win  = sum(t["pnl_pct"] for t in wins)  / len(wins)  if wins  else 0.0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0.0
        exp = (win_r/100 * avg_win) + ((1-win_r/100) * avg_loss)
        streak = 0; streak_type = ""
        for t in reversed(trades):
            if t.get("is_be"):
                continue
            if t["is_win"]:
                if streak_type in ("W", ""):
                    streak_type = "W"; streak += 1
                else:
                    break
            else:
                if streak_type in ("L", ""):
                    streak_type = "L"; streak += 1
                else:
                    break
        if   streak_type == "W" and streak >= 3: streak_str = f"🔥 连胜 {streak} 笔！"
        elif streak_type == "W" and streak >= 2: streak_str = f"✅ 连胜 {streak} 笔"
        elif streak_type == "W":                 streak_str = f"✅ 最近一胜"
        elif streak_type == "L" and streak >= 3: streak_str = f"❄️ 连败 {streak} 笔，注意风控"
        elif streak_type == "L" and streak >= 2: streak_str = f"⚠️ 连败 {streak} 笔"
        elif streak_type == "L":                 streak_str = f"❌ 最近一败"
        else:                                    streak_str = ""
        return {"total":total,"wins":len(wins),"losses":len(losses),"be":len(be),
                "win_rate":win_r,"avg_win":avg_win,"avg_loss":avg_loss,"expectancy":exp,
                "streak_str": streak_str}

    def _trade_lines(self, trades: list, n: int = 8) -> str:
        ct_map = {"TP1":"🥇","TP2":"🥈","TP3":"🏆","BE":"⚖️","SL":"🛑"}
        lines = []
        for t in trades[-n:]:
            arrow = "🟢" if t["side"]=="LONG" else "🔴"
            ico   = ct_map.get(t["close_type"],"❓")
            lines.append(f"{ico} #{t['coin']} {arrow}  {t['pnl_pct']:+.2f}%  [{t['close_type']}]  {t['time'][-5:]}")
        return "\n".join(lines)

    def daily_report(self, date_str: str = None) -> str:
        if not date_str: date_str = utc_now().strftime("%Y-%m-%d")
        trades = [t for t in self.history if t["date"] == date_str]
        s = self._stats(trades)
        if not s:
            return (f"📊 *今日战报 {date_str}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"今日暂无已结算讯号\n"
                    f"持续扫描中... 💪\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖 Alpha Oracle v9.1.4 持续监控中")
        grade = ("🏆 优秀" if s["win_rate"]>=70 else
                 "✅ 良好" if s["win_rate"]>=55 else
                 "⚠️ 一般" if s["win_rate"]>=40 else "❌ 待改善")
        streak_line = f"\n{s['streak_str']}" if s.get("streak_str") else ""
        return (
            f"📊 *今日战报 {date_str}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 讯号总数：{s['total']} 笔  {grade}\n"
            f"✅ 胜：{s['wins']}  ❌ 败：{s['losses']}  ⚖️ 保本：{s['be']}\n"
            f"📈 *胜率：{s['win_rate']:.1f}%*\n"
            f"💰 平均获利：{s['avg_win']:+.2f}%\n"
            f"📉 平均亏损：{s['avg_loss']:+.2f}%\n"
            f"⚡ 期望值：{s['expectancy']:+.2f}%/笔{streak_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{self._trade_lines(trades)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Alpha Oracle v9.1.4 明日继续！"
        )

    def monthly_report(self, month_str: str = None) -> str:
        if not month_str: month_str = utc_now().strftime("%Y-%m")
        trades = [t for t in self.history if t["month"] == month_str]
        s = self._stats(trades)
        if not s:
            return (f"📅 *月度战报 {month_str}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"本月暂无已结算讯号\n"
                    f"持续扫描中... 💪")
        coin_stats: dict = {}
        for t in trades:
            cn = t["coin"]
            if cn not in coin_stats: coin_stats[cn] = {"w":0,"l":0,"b":0}
            if t["is_win"]: coin_stats[cn]["w"] += 1
            elif t["is_be"]: coin_stats[cn]["b"] += 1
            else: coin_stats[cn]["l"] += 1
        coin_lines = [f"  #{cn}  W{cs['w']} L{cs['l']} BE{cs['b']}"
                      for cn, cs in sorted(coin_stats.items(), key=lambda x: -x[1]["w"])]
        grade = ("🏆 杰出" if s["win_rate"]>=70 else
                 "✅ 良好" if s["win_rate"]>=55 else
                 "⚠️ 普通" if s["win_rate"]>=40 else "❌ 需优化")
        streak_line = f"\n{s['streak_str']}" if s.get("streak_str") else ""
        return (
            f"📅 *月度战报 {month_str}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 本月讯号：{s['total']} 笔  {grade}\n"
            f"✅ 胜：{s['wins']}  ❌ 败：{s['losses']}  ⚖️ 保本：{s['be']}\n"
            f"📈 *月胜率：{s['win_rate']:.1f}%*\n"
            f"💰 平均获利：{s['avg_win']:+.2f}%\n"
            f"📉 平均亏损：{s['avg_loss']:+.2f}%\n"
            f"⚡ 月期望值：{s['expectancy']:+.2f}%/笔{streak_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏅 各币种：\n"
            + "\n".join(coin_lines) +
            f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Alpha Oracle v9.1.4 下月继续！"
        )

# ─────────────────────────────────────────────────────────
# 15. SignalTracker — 进场/TP/SL 监控 + 动态追踪止损
# ─────────────────────────────────────────────────────────
class SignalTracker:
    """
    追踪活跃讯号，持久化到 JSON，监控进场/TP/SL 触发。
    平仓时自动写入 WinRateTracker。

    状态机：
      PENDING  → 等待价格到达进场区
      ACTIVE   → 已进场，等待 TP/SL
      BE       → TP1 已中，SL 已移至进场价（保本）
      TRAIL    → TP2 已中，SL 已移至 TP1（锁利）
      closed   → 已平仓（从追踪列表移除）

    动态追踪止损：
      • 到达 TP1 → SL 移至进场价（保本）
      • 到达 TP2 → SL 移至 TP1（锁利）
      • 到达 TP3 → 全部平仓，完美收割
    """
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE,
                 win_tracker: WinRateTracker = None):
        self.filepath    = filepath
        self._lock       = threading.Lock()
        self.signals     = self._load()
        self.win_tracker = win_tracker

    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.signals, f, ensure_ascii=False, indent=2)

    def add(self, opp: dict) -> str:
        key = f"{opp['instId']}_{opp['side']}_{opp['tf']}_{int(time.time())}"
        with self._lock:
            self.signals[key] = {
                "instId"      : opp["instId"],
                "side"        : opp["side"],
                "tf"          : opp["tf"],
                "entry"       : opp["entry"],
                "sl"          : opp["sl"],         # 当前 SL（会动态更新）
                "sl_orig"     : opp["sl"],         # 原始 SL（显示用）
                "tp1"         : opp["tp1"],
                "tp2"         : opp["tp2"],
                "tp3"         : opp["tp3"],
                "score"       : opp["score"],
                "grade"       : opp["grade"],
                "status"      : "PENDING",         # PENDING/ACTIVE/BE/TRAIL
                "hit_tp1"     : False,
                "hit_tp2"     : False,
                "created"     : time.time(),
                "activated_at": None,
                "hit_tp1_at"  : None,
                "hit_tp2_at"  : None,
            }
            self._save()
        logging.info(f"📌 新增追踪: {key}")
        return key

    def remove(self, key: str):
        with self._lock:
            self.signals.pop(key, None); self._save()

    def update(self, key: str, **kwargs):
        with self._lock:
            if key in self.signals:
                self.signals[key].update(kwargs); self._save()

    def list_active(self) -> list:
        with self._lock: return list(self.signals.items())

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

    def check_one(self, key: str, sig: dict) -> bool:
        """
        检查单一讯号。返回 True = 已结束可移除；False = 继续追踪。
        """
        try:
            price = fetch_ticker_price(sig["instId"])
            if price <= 0:
                logging.warning(f"  [{key}] 无法取得即时价格，跳过检查")
                return False

            coin   = sig["instId"].split("-")[0]
            side   = sig["side"]
            status = sig["status"]
            entry  = sig["entry"]
            sl     = sig["sl"]                # 当前 SL（可能已移动）
            tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]

            logging.debug(f"[{key}] 检查: 价格={price:.4f}, 状态={status}, TP1={tp1:.4f}, TP2={tp2:.4f}, TP3={tp3:.4f}")

            # ── 1. PENDING 状态：检查过期 & 进场区 ─────────────
            if status == "PENDING":
                # 过期检查
                age_h = (time.time() - sig["created"]) / 3600
                if age_h > SIGNAL_EXPIRE_HOURS:
                    send_tg(f"⏰ *讯号过期* #{coin} {side}\n"
                            f"进场 `{entry:.4f}` 超过 {SIGNAL_EXPIRE_HOURS}h 未触发")
                    logging.info(f"  [过期] {key}")
                    return True
                # 进场区判定
                in_entry_zone = (
                    (side == "LONG"  and entry*(1-ENTRY_TOLERANCE*3) <= price <= entry*(1+ENTRY_TOLERANCE)) or
                    (side == "SHORT" and entry*(1-ENTRY_TOLERANCE) <= price <= entry*(1+ENTRY_TOLERANCE*3))
                )
                if in_entry_zone:
                    self.update(key, status="ACTIVE", activated_at=time.time())
                    msg = format_alert(coin, side, "ENTRY",
                                       price, entry, sl, tp1, tp2, tp3, score=sig["score"])
                    if send_tg(msg):
                        logging.info(f"  [进場] {key} @ {price:.4f} - 通知已发送")
                    else:
                        logging.error(f"  [进場] {key} - 通知发送失败")
                return False

            # ── 2. 非活跃状态不处理 ─────────────────────────
            if status not in ("ACTIVE", "BE", "TRAIL"):
                logging.debug(f"  [{key}] 状态 {status} 不需要检查TP")
                return False

            # ── 3. 止损触发（最优先检查，避免错过保护）───────
            sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
            if sl_hit:
                is_be = (status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001)
                close_type = "BE" if is_be else "SL"
                msg = format_alert(coin, side, "SL", price, entry, sig["sl_orig"],
                                     tp1, tp2, tp3,
                                     new_sl=(entry if is_be else sl))
                if send_tg(msg):
                    logging.info(f"  [{close_type}] {key} @ {price:.4f} (BE={is_be}) - 通知已发送")
                else:
                    logging.error(f"  [{close_type}] {key} - 通知发送失败")
                self._close(sig, price, close_type)
                return True

            # ── 4. TP3 达成 → 全部平仓 ─────────────────────
            tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
            if tp3_hit:
                msg = format_alert(coin, side, "TP3", price, entry, sig["sl_orig"], tp1, tp2, tp3)
                if send_tg(msg):
                    logging.info(f"  [TP3] {key} @ {price:.4f} ✅ 完美收割 - 通知已发送")
                else:
                    logging.error(f"  [TP3] {key} - 通知发送失败")
                self._close(sig, tp3, "TP3")
                return True

            # ── 5. TP2 达成 → 移损至 TP1（同时标记 TP1 已过）────
            tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
            if tp2_hit and not sig.get("hit_tp2"):
                now = time.time()
                # ✅ v9.1.1 修正：同时标记 hit_tp1=True，避免下一轮误触发 TP1
                if not sig.get("hit_tp1"):
                    self.update(key, hit_tp1=True, hit_tp1_at=now)
                    self._close(sig, tp1, "TP1")  # 补记一笔 TP1 部分获利
                self.update(key, hit_tp2=True, sl=tp1, status="TRAIL", hit_tp2_at=now)
                msg = format_alert(coin, side, "TP2", price, entry, sig["sl_orig"],
                                     tp1, tp2, tp3, new_sl=tp1)
                if send_tg(msg):
                    logging.info(f"  [TP2] {key} @ {price:.4f} → SL移至TP1={tp1:.4f} - 通知已发送")
                else:
                    logging.error(f"  [TP2] {key} - 通知发送失败")
                self._close(sig, tp2, "TP2")
                return False

            # ── 6. TP1 达成 → 移损至进场价（保本）─────────────
            tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
            if tp1_hit and not sig.get("hit_tp1"):
                self.update(key, hit_tp1=True, sl=entry, status="BE", hit_tp1_at=time.time())
                msg = format_alert(coin, side, "TP1", price, entry, sig["sl_orig"],
                                     tp1, tp2, tp3, new_sl=entry)
                if send_tg(msg):
                    logging.info(f"  [TP1] {key} @ {price:.4f} → SL移至保本={entry:.4f} - 通知已发送")
                else:
                    logging.error(f"  [TP1] {key} - 通知发送失败")
                self._close(sig, tp1, "TP1")
                return False

            # ── 7. 无触发，继续追踪 ────────────────────────
            logging.debug(f"  [{key}] 无触发，继续追踪")
            return False
            
        except Exception as e:
            logging.error(f"check_one [{key}] 错误: {e}\n{traceback.format_exc()}")
            return False

    def check_all(self):
        to_remove = []
        for key, sig in self.list_active():
            try:
                if self.check_one(key, sig): 
                    to_remove.append(key)
            except Exception as e:
                logging.error(f"check_all 检查 [{key}] 错误: {e}")
        for key in to_remove: 
            self.remove(key)
        if to_remove: 
            logging.info(f"  移除 {len(to_remove)} 笔已关闭讯号")

    # ─────────────────────────────────────────────────────
    # ✨ v9.1.1：多行结构 + 进度条 + 当前价距离提示
    # ─────────────────────────────────────────────────────
    def status_summary(self) -> str:
        items = self.list_active()
        if not items:
            return "📭 *目前无追踪中讯号*\n\n扫描器持续运作中，有机会会立即通知 🔍"

        # 状态图示对应
        st_map = {
            "PENDING": ("⏳", "PENDING · 等待进场"),
            "ACTIVE":  ("🔵", "ACTIVE · 持仓中"),
            "BE":      ("🛡",  "BREAKEVEN · 已保本"),
            "TRAIL":   ("🔁", "TRAILING · 锁利中"),
        }

        lines = [
            f"📋 *追踪中讯号 ({len(items)} 笔)*",
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for idx, (key, s) in enumerate(items, 1):
            coin  = s["instId"].split("-")[0]
            arrow = "🟢" if s["side"] == "LONG" else "🔴"
            side  = s["side"]
            em, st_desc = st_map.get(s["status"], ("❓", s["status"]))

            # 取即时价
            live = fetch_ticker_price(s["instId"])
            entry = s["entry"]

            # 当前 PnL（若已进场）& 距离
            if s["status"] == "PENDING":
                dist_pct = (live - entry) / entry * 100 if live > 0 else 0.0
                price_line = f"   📍 当前   `{live:.4f}`  (距离 `{dist_pct:+.2f}%`)" if live > 0 else "   📍 当前   `—`"
            else:
                if live > 0:
                    pnl = ((live - entry) / entry * 100) if side == "LONG" else ((entry - live) / entry * 100)
                    sign = "+" if pnl >= 0 else ""
                    price_line = f"   💹 当前   `{live:.4f}`  `{sign}{pnl:.2f}%`"
                else:
                    price_line = f"   💹 当前   `—`"

            # SL 标示（是否保本/锁利）
            if s["status"] == "BE":
                sl_label = f"   🛡 止损   `{s['sl']:.4f}`  *(保本)*"
            elif s["status"] == "TRAIL":
                sl_label = f"   🛡 止损   `{s['sl']:.4f}`  *(锁利于 TP1)*"
            else:
                sl_label = f"   🛑 止损   `{s['sl']:.4f}`"

            # TP 进度
            progress = _progress_bar(
                s.get("hit_tp1", False),
                s.get("hit_tp2", False),
                False,
            )

            # 组装该讯号区块
            lines.append(f"{em} *#{coin} · {arrow} {side} · {s['tf']}*    [{s['score']}分]")
            lines.append(f"   {st_desc}")
            lines.append(price_line)
            lines.append(f"   📌 进场   `{entry:.4f}`")
            lines.append(sl_label)
            lines.append(f"   🥇 TP1    `{s['tp1']:.4f}`")
            lines.append(f"   🥈 TP2    `{s['tp2']:.4f}`")
            lines.append(f"   🏆 TP3    `{s['tp3']:.4f}`")
            lines.append(f"   进度 {progress}")

            # 分隔线（最后一笔不加）
            if idx < len(items):
                lines.append("")
                lines.append("─────────────────────────")
                lines.append("")

        lines.append("")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🤖 Alpha Oracle v9.1.4 动态追踪中")

        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 16. 监控循环
# ─────────────────────────────────────────────────────────
def monitor_loop(tracker: SignalTracker, interval: int = 10, stop_event=None):
    """
    监控循环 - 更频繁的检查（默认10秒）
    """
    logging.info(f"监控循环启动，间隔 {interval}s")
    check_count = 0
    while True:
        if stop_event and stop_event.is_set(): 
            break
        try:
            active = tracker.list_active()
            check_count += 1
            if active:
                logging.info(f"【检查 #{check_count}】监控中... {len(active)} 笔讯号")
                tracker.check_all()
            else:
                if check_count % 6 == 0:  # 每60秒显示一次
                    logging.info(f"【检查 #{check_count}】无追踪讯号，等待新机会...")
        except Exception as e:
            logging.error(f"monitor_loop 错误: {e}\n{traceback.format_exc()}")
        time.sleep(interval)

# ─────────────────────────────────────────────────────────
# 17. 即时进场区判断
# ─────────────────────────────────────────────────────────
def _check_entry_zone(opp: dict) -> tuple:
    live = fetch_ticker_price(opp["instId"])
    if live <= 0: return False, 0.0, "无法取得即时价"
    entry = opp["entry"]; side = opp["side"]; tol = ENTRY_TOLERANCE
    in_zone = (
        (side=="LONG"  and live <= entry*(1+tol) and live >= entry*(1-tol*3)) or
        (side=="SHORT" and live >= entry*(1-tol) and live <= entry*(1+tol*3))
    )
    dist_pct = (live - entry) / entry * 100
    if in_zone:
        return True, live, f"已在进场区 {live:.4f}（{dist_pct:+.2f}%）"
    elif (side=="LONG" and live > entry):
        return False, live, f"价格高于进场区 {dist_pct:+.2f}% 等待回踩"
    elif (side=="SHORT" and live < entry):
        return False, live, f"价格低于进场区 {dist_pct:+.2f}% 等待回升"
    else:
        return False, live, f"等待接近进场区（{abs(dist_pct):.2f}%）"

# ─────────────────────────────────────────────────────────
# 18. 主扫描函数
# ─────────────────────────────────────────────────────────
def _scan_one_coin(coin: str) -> list:
    if not check_news_cooldown(coin):
        logging.info(f"  [{coin}] 新闻冷却期")
        return []
    try:
        return scan_for_opportunity(coin)
    except Exception as e:
        logging.error(f"[{coin}] 扫描错误: {e}")
        return []

def run_scan(tracker: SignalTracker) -> int:
    # ✨ v9.1.3：扫描前先检查既有讯号，确保云端环境下 PENDING/ACTIVE 也会被更新
    active_before = len(tracker.list_active())
    if active_before > 0:
        logging.info(f"═══ 预检 {active_before} 笔既有讯号 ═══")
        try:
            tracker.check_all()
        except Exception as e:
            logging.error(f"check_all 预检错误: {e}")

    logging.info(f"═══ 扫描开始 阈值={SETUP_SCORE_THRESHOLD} 时框={SCAN_TIMEFRAMES} ═══")
    all_opps: list = []
    workers = min(5, len(ALL_COINS))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_scan_one_coin, c): c for c in ALL_COINS}
        for fut in as_completed(futures):
            coin = futures[fut]
            try:
                opps = fut.result()
                if opps:
                    logging.info(f"  [{coin}] 找到 {len(opps)} 个机会")
                    all_opps.extend(opps)
            except Exception as e:
                logging.error(f"[{coin}] Future 错误: {e}")
    all_opps.sort(key=lambda x: x["score"], reverse=True)
    sent = 0
    for opp in all_opps:
        if sent >= MAX_SIGNALS_PER_RUN:
            break
        if not check_signal_cooldown(opp["instId"], opp["side"]):
            logging.info(f"  [{opp['instId']}/{opp['side']}] 冷却期中（{SIGNAL_COOLDOWN_HOURS}h），跳过")
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
                    logging.info(f"     ✅ 进场通知已发送")
                else:
                    logging.error(f"     ❌ 进场通知发送失败")
            tracker.add(opp)
        time.sleep(0.8)
    logging.info(f"扫描完成，发送 {sent} 笔")
    # ✨ v9.1.3：只要有追踪中讯号就推播状态快照（不再限定必须有新讯号）
    if len(tracker.list_active()) > 0:
        status_msg = tracker.status_summary()
        if send_tg(status_msg):
            logging.info("✅ 状态摘要已发送")
        else:
            logging.error("❌ 状态摘要发送失败")
    return sent

def run_monitor_once(tracker: SignalTracker, push_status: bool = True) -> int:
    """
    ✨ v9.1.3 轻量监控模式：只检查既有讯号一次，不扫描新讯号。
    适合云端 cron（例如每 3~5 分钟）快速同步 TP/SL 状态。
    回传被检查的讯号数量。
    """
    active = tracker.list_active()
    n = len(active)
    if n == 0:
        logging.info("monitor_once: 无追踪中讯号")
        return 0
    logging.info(f"monitor_once: 检查 {n} 笔追踪中讯号")
    try:
        tracker.check_all()
    except Exception as e:
        logging.error(f"monitor_once 错误: {e}")
    # 检查完若还有存活讯号，推播状态
    remaining = tracker.list_active()
    if push_status and remaining:
        status_msg = tracker.status_summary()
        if send_tg(status_msg):
            logging.info("✅ 状态摘要已发送")
        else:
            logging.error("❌ 状态摘要发送失败")
    logging.info(f"monitor_once: 完成，剩余 {len(remaining)} 笔")
    return n

# ─────────────────────────────────────────────────────────
# 19. 主函数
# ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpha Oracle v9.1.4")
    parser.add_argument("--mode", default="all",
                        choices=["scan","monitor","monitor_once","loop","all",
                                 "daily_report","monthly_report"],
                        help="scan=扫描（含预检既有讯号） | monitor=持续监控（阻塞） | "
                             "monitor_once=轻量单次监控（云端用） | loop=定时扫描+监控 | "
                             "all=扫描+监控 | daily_report=今日战报 | monthly_report=月度战报")
    parser.add_argument("--interval",      type=int, default=10, help="监控间隔秒数（默认10秒）")
    parser.add_argument("--loop-interval", type=int, default=900)
    parser.add_argument("--status",        action="store_true")
    args = parser.parse_args()
    
    logging.info("=" * 60)
    logging.info("🤖 Alpha Oracle v9.1.4 启动")
    logging.info(f"📋 模式: {args.mode}")
    logging.info(f"⏱  监控间隔: {args.interval}秒")
    logging.info("=" * 60)
    
    win_tracker = WinRateTracker(TRADE_HISTORY_FILE)
    tracker     = SignalTracker(ACTIVE_SIGNALS_FILE, win_tracker=win_tracker)
    
    if args.status:
        summary = tracker.status_summary()
        print(summary)
        send_tg(summary)
        return
    
    if args.mode == "daily_report":
        msg = win_tracker.daily_report()
        logging.info("发送每日战报")
        print(msg)
        send_tg(msg)
        return
    
    if args.mode == "monthly_report":
        msg = win_tracker.monthly_report()
        logging.info("发送月度战报")
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
            logging.info("监控停止")
        return
    
    if args.mode == "loop":
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_loop,
                             args=(tracker, args.interval, stop_ev), daemon=True)
        t.start()
        try:
            while True:
                run_scan(tracker)
                logging.info(f"下次扫描：{args.loop_interval}s 后")
                time.sleep(args.loop_interval)
        except KeyboardInterrupt:
            logging.info("循环停止")
            stop_ev.set()
        return
    
    # all 模式（预设）
    run_scan(tracker)
    try:
        monitor_loop(tracker, interval=args.interval)
    except KeyboardInterrupt:
        logging.info("停止")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"崩溃: {e}")
        traceback.print_exc()
        sys.exit(1)
