#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v9.2.0 — 完整版（掃描 + 進場監控 + 勝率統計 + 每日/月報 + 動態追蹤止損）
══════════════════════════════════════════════════════════════════════
v9.2.0 修復與增強：
  🐛 FIX: fetch_ticker_price 添加重試機制，避免價格獲取失敗導致監控失效
  🐛 FIX: send_tg 添加詳細錯誤日誌，方便排查 Telegram 通知問題
  🐛 FIX: check_one 添加調試日誌 + 確保連續觸發正確處理（TP1→TP2→TP3）
  ✨ NEW: 價格獲取失敗時使用最後已知價格作為備用
  ✨ NEW: 添加監控心跳日誌，方便確認程式正常運行
  ✨ NEW: 進場後立即強制檢查一次，避免錯過快速觸發

保留 v9.1.1 功能：
  ✅ VWAP 分析 / OI 持倉量 / MACD 背離 / 訊號冷卻 / 盤別 / 連勝連敗 / 倉位建議
  ✅ PENDING 訊號修復 / TP2 觸發後正確標記 TP1 / format_alert 重排版

══ 執行模式 ══════════════════════════════════════
  python main.py                       → 掃描 + 監控一次（本地用）
  python main.py --mode scan           → 只掃描一次（GitHub Actions 用）
  python main.py --mode monitor        → 只監控活躍訊號
  python main.py --mode loop           → 定時掃描 + 持續監控（Render/VPS）
  python main.py --mode daily_report   → 發送今日戰報
  python main.py --mode monthly_report → 發送月度戰報
  python main.py --status              → 查詢目前追蹤中訊號
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
from typing import Optional, Tuple, List, Dict, Any

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
logger = logging.getLogger(__name__)

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

# v9.1+ 新增參數
SIGNAL_COOLDOWN_HOURS       = 2
VWAP_PERIODS                = 50
MACD_FAST                   = 12
MACD_SLOW                   = 26
MACD_SIGNAL_PERIOD          = 9

# 全局緩存
_news_cooldown:    dict = {}
_SIGNAL_COOLDOWN:  dict = {}
_price_cache:      dict = {}  # v9.2.0: 價格緩存，避免重複請求

# ─────────────────────────────────────────────────────────
# 2. 工具 & 通知
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str, parse_mode: str = "Markdown", alert_type: str = None, coin: str = None) -> bool:
    """發送 Telegram 通知，添加詳細日誌"""
    if not TG_TOKEN or not CHAT_ID:
        logger.error(f"❌ TG 配置缺失 (TG_TOKEN/CHAT_ID)，無法發送 {alert_type} [{coin}]")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode, "disable_web_page_preview": True}
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info(f"✅ TG 發送成功: {alert_type or '通知'} [{coin or '系統'}]")
            return True
        else:
            logger.error(f"❌ TG API 返回 {r.status_code}: {r.text[:200]} | {alert_type} [{coin}]")
            return False
    except requests.exceptions.Timeout:
        logger.error(f"❌ TG 發送超時: {alert_type} [{coin}]")
        return False
    except requests.exceptions.ConnectionError:
        logger.error(f"❌ TG 連接錯誤: {alert_type} [{coin}]")
        return False
    except Exception as e:
        logger.error(f"❌ TG 發送異常 {alert_type} [{coin}]: {type(e).__name__}: {e}")
        return False

def check_news_cooldown(instId: str) -> bool:
    return time.time() - _news_cooldown.get(instId, 0) >= NEWS_COOLDOWN_MINUTES * 60

def mark_news_event(instId: str):
    _news_cooldown[instId] = time.time()

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

# ─────────────────────────────────────────────────────────
# 2b. v9.1+ 輔助工具
# ─────────────────────────────────────────────────────────
def check_signal_cooldown(instId: str, side: str) -> bool:
    key  = f"{instId}_{side}"
    last = _SIGNAL_COOLDOWN.get(key, 0)
    return (time.time() - last) >= SIGNAL_COOLDOWN_HOURS * 3600

def set_signal_cooldown(instId: str, side: str):
    _SIGNAL_COOLDOWN[f"{instId}_{side}"] = time.time()

def get_market_session() -> str:
    h = utc_now().hour
    if   13 <= h < 22: return "🌎 美盤"
    elif  7 <= h < 16: return "🌍 歐盤"
    elif  1 <= h <  8: return "🌏 亞盤"
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
        return f"≈{pos_usdt:.0f}U  (x{leverage:.1f} | 1%風控/{account_size:.0f}U)"
    except:
        return "─"

# ─────────────────────────────────────────────────────────
# 3. 數據抓取（v9.2.0 增強版）
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
        logger.warning(f"[{instId}/{tf}] Fetch: {e}")
        return None

def fetch_ticker_price(instId: str, retries: int = 3, use_cache: bool = True) -> float:
    """
    v9.2.0 增強版：添加重試機制 + 緩存 + 詳細日誌
    """
    global _price_cache
    
    # 使用緩存（3秒內不重複請求）
    if use_cache:
        cache_key = f"{instId}_price"
        cached = _price_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < 3:
            return cached["price"]
    
    last_error = None
    for attempt in range(retries):
        try:
            res = requests.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
                timeout=5
            ).json()
            if res.get("code") == "0" and res.get("data"):
                price = float(res["data"][0]["last"])
                if price > 0:
                    # 更新緩存
                    if use_cache:
                        _price_cache[cache_key] = {"price": price, "ts": time.time()}
                    return price
                logger.warning(f"[{instId}] 價格為 0 或無效: {res}")
            else:
                logger.warning(f"[{instId}] API 返回異常: code={res.get('code')}, data={res.get('data')}")
        except requests.exceptions.Timeout:
            last_error = f"Timeout (attempt {attempt+1}/{retries})"
            logger.warning(f"[{instId}] 價格請求超時: {last_error}")
        except requests.exceptions.ConnectionError:
            last_error = f"ConnectionError (attempt {attempt+1}/{retries})"
            logger.warning(f"[{instId}] 價格請求連接錯誤: {last_error}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e} (attempt {attempt+1}/{retries})"
            logger.warning(f"[{instId}] 價格請求異常: {last_error}")
        
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))  # 指數退避
    
    # 所有重試失敗，返回緩存中的最後已知價格（如果有）
    if use_cache and cache_key in _price_cache:
        cached_price = _price_cache[cache_key]["price"]
        logger.warning(f"[{instId}] 使用緩存價格 {cached_price}（API 請求失敗: {last_error}）")
        return cached_price
    
    logger.error(f"[{instId}] 價格獲取最終失敗: {last_error}")
    return 0.0

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res["data"][0]["fundingRate"]) if res.get("data") else 0.0
    except: 
        return 0.0

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
    except: 
        return 1.0, "N/A"

def fetch_order_book(instId: str, depth: int = 20) -> tuple:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5
        ).json()
        if res.get("code") != "0" or not res.get("data"):
            return 1.0, "盤口均衡"
        data    = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio   = bid_vol / ask_vol
        if   ratio >= 1.30: label = f"買盤強勢 ({ratio:.2f})"
        elif ratio >= 1.05: label = f"買盤略強 ({ratio:.2f})"
        elif ratio >= 0.95: label = f"盤口均衡 ({ratio:.2f})"
        elif ratio >= 0.77: label = f"賣盤略強 ({ratio:.2f})"
        else:               label = f"賣盤強勢 ({ratio:.2f})"
        return ratio, label
    except: 
        return 1.0, "盤口均衡"

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
        logger.debug(f"OI分析: {e}")
        return 0.5, "OI─"

# ─────────────────────────────────────────────────────────
# 4. 技術指標（保持原樣，僅添加日誌）
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
    if trend[-1]==1:  return  1,"多頭"
    if trend[-1]==-1: return -1,"空頭"
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
        return False, "MACD數據不足"
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
                return True, f"MACD看漲背離 ({m2:.4f}>{m1:.4f})"
    else:
        prev_h = price_h[:mid].max(); curr_h = price_h[mid:].max()
        if curr_h > prev_h * 1.001:
            idx1 = int(np.argmax(price_h[:mid]))
            idx2 = mid + int(np.argmax(price_h[mid:]))
            m1   = macd_arr[idx1]; m2 = macd_arr[idx2]
            if m2 < m1 - abs(m1) * 0.05:
                return True, f"MACD看跌背離 ({m2:.4f}<{m1:.4f})"
    return False, "無MACD背離"

# ─────────────────────────────────────────────────────────
# 5-11. 精度分析模組、擺動點、流動性、訂單流、市場情緒、評分系統
# （保持原樣，僅確保導入正確，此處省略以節省篇幅，實際使用時請保留完整代碼）
# ─────────────────────────────────────────────────────────

# [為節省篇幅，此處省略 5-11 節的原始代碼，請從原文件複製保留]
# 這些函數不影響通知邏輯，可直接使用原版

# ─────────────────────────────────────────────────────────
# 12. 主掃描邏輯（保持原樣）
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str,
                   htf_trend: str, fr: float, ls_f: float, ls_str: str,
                   ob_r: float, oi_sc: float, oi_lb: str,
                   _cache: dict) -> list:
    # [此處保留原始 scan_timeframe 函數，不修改]
    # 為節省篇幅省略，請從原文件複製
    return []  # 佔位，實際使用時請替換為完整函數

def scan_for_opportunity(instId: str) -> list:
    # [此處保留原始 scan_for_opportunity 函數]
    return []  # 佔位

# ─────────────────────────────────────────────────────────
# 13. 訊號格式化（保持原樣）
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    # [此處保留原始 format_signal 函數]
    return ""  # 佔位

# ─────────────────────────────────────────────────────────
# 13b. 追蹤訊號格式化（v9.1.1 重排版）
# ─────────────────────────────────────────────────────────
def _progress_bar(hit_tp1: bool, hit_tp2: bool, hit_tp3: bool = False) -> str:
    """產生 [🥇✅ 🥈⏳ 🏆⏳] 格式的進度條"""
    p1 = "🥇✅" if hit_tp1 else "🥇⏳"
    p2 = "🥈✅" if hit_tp2 else "🥈⏳"
    p3 = "🏆✅" if hit_tp3 else "🏆⏳"
    return f"[ {p1}  {p2}  {p3} ]"

def format_alert(coin: str, side: str, alert_type: str,
                 price: float, entry: float, sl: float,
                 tp1: float, tp2: float, tp3: float,
                 new_sl: float = None, score: int = 0) -> str:
    arrow = "🟢" if side == "LONG" else "🔴"
    st    = "多" if side == "LONG" else "空"
    sign  = "+" if side == "LONG" else "-"

    if alert_type == "ENTRY":
        sl_pct  = abs(entry - sl)  / entry * 100
        tp1_pct = abs(tp1   - entry) / entry * 100
        tp2_pct = abs(tp2   - entry) / entry * 100
        tp3_pct = abs(tp3   - entry) / entry * 100
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
            f"🎯 *TP1 達標！*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 當前    `{price:.4f}`    `{sign}{pnl:.2f}%`\n"
            f"\n"
            f"進度   {_progress_bar(True, False, False)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"✅ TP1 `{tp1:.4f}`  已達成\n"
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
            f"💹 當前    `{price:.4f}`    `{sign}{pnl:.2f}%`\n"
            f"\n"
            f"進度   {_progress_bar(True, True, False)}\n"
            f"\n"
            f"─────────────────────────\n"
            f"✅ TP2 `{tp2:.4f}`  已達成\n"
            f"🛡 SL 移至 `{tp1:.4f}`  *(鎖利)*\n"
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
            f"💎 當前    `{price:.4f}`    `{sign}{pnl:.2f}%`\n"
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
        is_be      = new_sl is not None and abs(new_sl - entry) < entry * 0.0001
        label      = "保本止損" if is_be else "止損觸發"
        header_em  = "🛡" if is_be else "🛑"
        sl_display = f"`{new_sl:.4f}`" if new_sl else f"`{sl:.4f}`"
        if is_be:
            outcome = ("💡 倉位已平倉於成本價\n"
                       "   資金安全，等下一個機會 💪")
        else:
            outcome = ("⚠️ 倉位已止損出場\n"
                       "   請遵守風控，莫加碼攤平")
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{header_em} *{label}*  #{coin} {arrow} {st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"💹 當前    `{price:.4f}`    `{pnl:+.2f}%`\n"
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
        logger.info(f"📝 記錄 {coin} {side} {close_type} {pnl_pct:+.2f}%")

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
            if t.get("is_be"): continue
            if t["is_win"]:
                if streak_type in ("W", ""): streak_type = "W"; streak += 1
                else: break
            else:
                if streak_type in ("L", ""): streak_type = "L"; streak += 1
                else: break
        if   streak_type == "W" and streak >= 3: streak_str = f"🔥 連勝 {streak} 筆！"
        elif streak_type == "W" and streak >= 2: streak_str = f"✅ 連勝 {streak} 筆"
        elif streak_type == "W":                 streak_str = f"✅ 最近一勝"
        elif streak_type == "L" and streak >= 3: streak_str = f"❄️ 連敗 {streak} 筆，注意風控"
        elif streak_type == "L" and streak >= 2: streak_str = f"⚠️ 連敗 {streak} 筆"
        elif streak_type == "L":                 streak_str = f"❌ 最近一敗"
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
            return (f"📊 *今日戰報 {date_str}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"今日暫無已結算訊號\n"
                    f"持續掃描中... 💪\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖 Alpha Oracle v9.2.0 持續監控中")
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
            f"🤖 Alpha Oracle v9.2.0 明日繼續！"
        )

    def monthly_report(self, month_str: str = None) -> str:
        if not month_str: month_str = utc_now().strftime("%Y-%m")
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
            if cn not in coin_stats: coin_stats[cn] = {"w":0,"l":0,"b":0}
            if t["is_win"]: coin_stats[cn]["w"] += 1
            elif t["is_be"]: coin_stats[cn]["b"] += 1
            else: coin_stats[cn]["l"] += 1
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
            f"🤖 Alpha Oracle v9.2.0 下月繼續！"
        )

# ─────────────────────────────────────────────────────────
# 15. SignalTracker — 進場/TP/SL 監控 + 動態追蹤止損（v9.2.0 修復版）
# ─────────────────────────────────────────────────────────
class SignalTracker:
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
                "sl"          : opp["sl"],
                "sl_orig"     : opp["sl"],
                "tp1"         : opp["tp1"],
                "tp2"         : opp["tp2"],
                "tp3"         : opp["tp3"],
                "score"       : opp["score"],
                "grade"       : opp["grade"],
                "status"      : "PENDING",
                "hit_tp1"     : False,
                "hit_tp2"     : False,
                "created"     : time.time(),
                "activated_at": None,
                "hit_tp1_at"  : None,
                "hit_tp2_at"  : None,
            }
            self._save()
        logger.info(f"📌 新增追蹤: {key}")
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
                logger.error(f"WinRateTracker.record: {e}")

    def check_one(self, key: str, sig: dict) -> bool:
        """
        v9.2.0 修復版：添加詳細日誌 + 確保連續觸發正確處理
        返回 True = 已結束可移除；False = 繼續追蹤
        """
        # 🔍 調試日誌：記錄每次檢查的關鍵參數
        price = fetch_ticker_price(sig["instId"], use_cache=True)
        logger.debug(f"[{key}] check: price={price}, status={sig['status']}, "
                    f"entry={sig['entry']}, sl={sig['sl']}, "
                    f"tp1={sig['tp1']}, tp2={sig['tp2']}, tp3={sig['tp3']}, "
                    f"hit_tp1={sig.get('hit_tp1')}, hit_tp2={sig.get('hit_tp2')}")

        if price <= 0:
            logger.warning(f"[{key}] ⚠️ 價格獲取失敗 (price={price})，跳過本次檢查")
            return False

        coin   = sig["instId"].split("-")[0]
        side   = sig["side"]
        status = sig["status"]
        entry  = sig["entry"]
        sl     = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]

        # ── 1. PENDING 狀態：檢查過期 & 進場區 ─────────────
        if status == "PENDING":
            age_h = (time.time() - sig["created"]) / 3600
            if age_h > SIGNAL_EXPIRE_HOURS:
                msg = f"⏰ *訊號過期* #{coin} {side}\n進場 `{entry:.4f}` 超過 {SIGNAL_EXPIRE_HOURS}h 未觸發"
                send_tg(msg, alert_type="EXPIRED", coin=coin)
                logger.info(f"[{key}] ⏰ 訊號過期，移除")
                return True
            
            # 進場區判定
            in_entry_zone = (
                (side == "LONG"  and entry*(1-ENTRY_TOLERANCE*3) <= price <= entry*(1+ENTRY_TOLERANCE)) or
                (side == "SHORT" and entry*(1-ENTRY_TOLERANCE) <= price <= entry*(1+ENTRY_TOLERANCE*3))
            )
            if in_entry_zone:
                logger.info(f"[{key}] ✅ 進場條件滿足: price={price:.4f}, entry={entry:.4f}")
                self.update(key, status="ACTIVE", activated_at=time.time())
                sent = send_tg(format_alert(coin, side, "ENTRY",
                                           price, entry, sl, tp1, tp2, tp3, score=sig["score"]),
                            alert_type="ENTRY", coin=coin)
                if sent:
                    logger.info(f"[{key}] ✅ 進場通知已發送 @ {price:.4f}")
                else:
                    logger.error(f"[{key}] ❌ 進場通知發送失敗")
                # 🚀 v9.2.0: 進場後立即強制檢查一次，避免錯過快速觸發
                time.sleep(1)
                self.check_one(key, self.signals.get(key, sig))
            return False

        # ── 2. 非活躍狀態不處理 ─────────────────────────
        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False

        # ── 3. 止損觸發（最優先檢查）───────
        sl_hit = (side == "LONG" and price <= sl) or (side == "SHORT" and price >= sl)
        if sl_hit:
            is_be = (status in ("BE", "TRAIL") and abs(sl - entry) < entry * 0.0001)
            close_type = "BE" if is_be else "SL"
            logger.info(f"[{key}] 🛑 SL 觸發: price={price:.4f}, sl={sl:.4f}, is_be={is_be}")
            sent = send_tg(format_alert(coin, side, "SL", price, entry, sig["sl_orig"],
                                       tp1, tp2, tp3, new_sl=(entry if is_be else sl)),
                          alert_type="SL", coin=coin)
            if sent:
                logger.info(f"[{key}] ✅ SL 通知已發送")
            self._close(sig, price, close_type)
            return True

        # ── 4. TP3 達成 → 全部平倉 ─────────────────────
        tp3_hit = (side == "LONG" and price >= tp3) or (side == "SHORT" and price <= tp3)
        if tp3_hit:
            logger.info(f"[{key}] 🏆 TP3 觸發: price={price:.4f} >= tp3={tp3:.4f}")
            send_tg(format_alert(coin, side, "TP3", price, entry, sig["sl_orig"], tp1, tp2, tp3),
                   alert_type="TP3", coin=coin)
            self._close(sig, tp3, "TP3")
            logger.info(f"[{key}] ✅ TP3 通知已發送，完美收割")
            return True

        # ── 5. TP2 達成 → 移損至 TP1（同時標記 TP1 已過）────
        tp2_hit = (side == "LONG" and price >= tp2) or (side == "SHORT" and price <= tp2)
        if tp2_hit and not sig.get("hit_tp2"):
            now = time.time()
            logger.info(f"[{key}] 🥈 TP2 觸發: price={price:.4f} >= tp2={tp2:.4f}")
            # ✅ 確保同時標記 TP1，避免下一輪誤觸發
            if not sig.get("hit_tp1"):
                self.update(key, hit_tp1=True, hit_tp1_at=now)
                self._close(sig, tp1, "TP1")  # 補記一筆 TP1 部分獲利
                logger.info(f"[{key}] 🥇 TP1 補標記 + 記錄")
            self.update(key, hit_tp2=True, sl=tp1, status="TRAIL", hit_tp2_at=now)
            sent = send_tg(format_alert(coin, side, "TP2", price, entry, sig["sl_orig"],
                                       tp1, tp2, tp3, new_sl=tp1),
                          alert_type="TP2", coin=coin)
            if sent:
                logger.info(f"[{key}] ✅ TP2 通知已發送")
            self._close(sig, tp2, "TP2")
            logger.info(f"[{key}] 🥈 TP2 處理完成 → SL移至TP1={tp1:.4f}")
            return False

        # ── 6. TP1 達成 → 移損至進場價（保本）─────────────
        tp1_hit = (side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)
        if tp1_hit and not sig.get("hit_tp1"):
            logger.info(f"[{key}] 🥇 TP1 觸發: price={price:.4f} >= tp1={tp1:.4f}")
            self.update(key, hit_tp1=True, sl=entry, status="BE", hit_tp1_at=time.time())
            sent = send_tg(format_alert(coin, side, "TP1", price, entry, sig["sl_orig"],
                                       tp1, tp2, tp3, new_sl=entry),
                          alert_type="TP1", coin=coin)
            if sent:
                logger.info(f"[{key}] ✅ TP1 通知已發送")
            self._close(sig, tp1, "TP1")
            logger.info(f"[{key}] 🥇 TP1 處理完成 → SL移至保本={entry:.4f}")
            return False

        # ── 7. 無觸發，繼續追蹤 ────────────────────────
        return False

    def check_all(self):
        to_remove = []
        for key, sig in self.list_active():
            try:
                if self.check_one(key, sig): 
                    to_remove.append(key)
            except Exception as e:
                logger.error(f"check_one [{key}] 異常: {type(e).__name__}: {e}")
                traceback.print_exc()
        for key in to_remove: 
            self.remove(key)
        if to_remove: 
            logger.info(f"🗑️ 移除 {len(to_remove)} 筆已關閉訊號")

    def status_summary(self) -> str:
        items = self.list_active()
        if not items:
            return "📭 *目前無追蹤中訊號*\n\n掃描器持續運作中，有機會會立即通知 🔍"

        st_map = {
            "PENDING": ("⏳", "PENDING · 等待進場"),
            "ACTIVE":  ("🔵", "ACTIVE · 持倉中"),
            "BE":      ("🛡",  "BREAKEVEN · 已保本"),
            "TRAIL":   ("🔁", "TRAILING · 鎖利中"),
        }

        lines = [
            f"📋 *追蹤中訊號 ({len(items)} 筆)*",
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for idx, (key, s) in enumerate(items, 1):
            coin  = s["instId"].split("-")[0]
            arrow = "🟢" if s["side"] == "LONG" else "🔴"
            side  = s["side"]
            em, st_desc = st_map.get(s["status"], ("❓", s["status"]))

            live = fetch_ticker_price(s["instId"])
            entry = s["entry"]

            if s["status"] == "PENDING":
                dist_pct = (live - entry) / entry * 100 if live > 0 else 0.0
                price_line = f"   📍 當前   `{live:.4f}`  (距離 `{dist_pct:+.2f}%`)" if live > 0 else "   📍 當前   `—`"
            else:
                if live > 0:
                    pnl = ((live - entry) / entry * 100) if side == "LONG" else ((entry - live) / entry * 100)
                    sign = "+" if pnl >= 0 else ""
                    price_line = f"   💹 當前   `{live:.4f}`  `{sign}{pnl:.2f}%`"
                else:
                    price_line = f"   💹 當前   `—`"

            if s["status"] == "BE":
                sl_label = f"   🛡 止損   `{s['sl']:.4f}`  *(保本)*"
            elif s["status"] == "TRAIL":
                sl_label = f"   🛡 止損   `{s['sl']:.4f}`  *(鎖利於 TP1)*"
            else:
                sl_label = f"   🛑 止損   `{s['sl']:.4f}`"

            progress = _progress_bar(
                s.get("hit_tp1", False),
                s.get("hit_tp2", False),
                False,
            )

            lines.append(f"{em} *#{coin} · {arrow} {side} · {s['tf']}*    [{s['score']}分]")
            lines.append(f"   {st_desc}")
            lines.append(price_line)
            lines.append(f"   📌 進場   `{entry:.4f}`")
            lines.append(sl_label)
            lines.append(f"   🥇 TP1    `{s['tp1']:.4f}`")
            lines.append(f"   🥈 TP2    `{s['tp2']:.4f}`")
            lines.append(f"   🏆 TP3    `{s['tp3']:.4f}`")
            lines.append(f"   進度 {progress}")

            if idx < len(items):
                lines.append("")
                lines.append("─────────────────────────")
                lines.append("")

        lines.append("")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🤖 Alpha Oracle v9.2.0 動態追蹤中")

        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 16. 監控迴圈（添加心跳日誌）
# ─────────────────────────────────────────────────────────
def monitor_loop(tracker: SignalTracker, interval: int = 30, stop_event=None):
    logger.info(f"🔄 監控迴圈啟動，間隔 {interval}s")
    loop_count = 0
    while True:
        if stop_event and stop_event.is_set(): 
            logger.info("🛑 監控迴圈收到停止信號")
            break
        try:
            loop_count += 1
            active = tracker.list_active()
            if active:
                logger.info(f"❤️ 監控心跳 #{loop_count} | 追蹤中: {len(active)} 筆")
                tracker.check_all()
            else:
                if loop_count % 10 == 0:  # 每 10 輪記錄一次空閒日誌
                    logger.info(f"❤️ 監控心跳 #{loop_count} | 無追蹤訊號")
        except Exception as e:
            logger.error(f"monitor_loop 異常: {type(e).__name__}: {e}")
            traceback.print_exc()
        time.sleep(interval)

# ─────────────────────────────────────────────────────────
# 17. 即時進場區判斷
# ─────────────────────────────────────────────────────────
def _check_entry_zone(opp: dict) -> tuple:
    live = fetch_ticker_price(opp["instId"])
    if live <= 0: return False, 0.0, "無法取得即時價"
    entry = opp["entry"]; side = opp["side"]; tol = ENTRY_TOLERANCE
    in_zone = (
        (side=="LONG"  and live <= entry*(1+tol) and live >= entry*(1-tol*3)) or
        (side=="SHORT" and live >= entry*(1-tol) and live <= entry*(1+tol*3))
    )
    dist_pct = (live - entry) / entry * 100
    if in_zone:
        return True, live, f"已在進場區 {live:.4f}（{dist_pct:+.2f}%）"
    elif (side=="LONG" and live > entry):
        return False, live, f"價格高於進場區 {dist_pct:+.2f}% 等待回踩"
    elif (side=="SHORT" and live < entry):
        return False, live, f"價格低於進場區 {dist_pct:+.2f}% 等待回升"
    else:
        return False, live, f"等待接近進場區（{abs(dist_pct):.2f}%）"

# ─────────────────────────────────────────────────────────
# 18. 主掃描函式
# ─────────────────────────────────────────────────────────
def _scan_one_coin(coin: str) -> list:
    if not check_news_cooldown(coin):
        logger.info(f"  [{coin}] 新聞冷卻期")
        return []
    try:
        return scan_for_opportunity(coin)
    except Exception as e:
        logger.error(f"[{coin}] 掃描錯誤: {type(e).__name__}: {e}")
        return []

def run_scan(tracker: SignalTracker) -> int:
    logger.info(f"🔍 掃描開始 | 閾值={SETUP_SCORE_THRESHOLD} | 時框={SCAN_TIMEFRAMES}")
    all_opps: list = []
    workers = min(5, len(ALL_COINS))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_scan_one_coin, c): c for c in ALL_COINS}
        for fut in as_completed(futures):
            coin = futures[fut]
            try:
                opps = fut.result()
                if opps:
                    logger.info(f"  [{coin}] 找到 {len(opps)} 個機會")
                    all_opps.extend(opps)
            except Exception as e:
                logger.error(f"[{coin}] Future 錯誤: {type(e).__name__}: {e}")
    all_opps.sort(key=lambda x: x["score"], reverse=True)
    sent = 0
    for opp in all_opps:
        if sent >= MAX_SIGNALS_PER_RUN:
            break
        if not check_signal_cooldown(opp["instId"], opp["side"]):
            logger.info(f"  [{opp['instId']}/{opp['side']}] 冷卻期中（{SIGNAL_COOLDOWN_HOURS}h），跳過")
            continue
        if send_tg(format_signal(opp), alert_type="SIGNAL", coin=opp["instId"].split("-")[0]):
            sent += 1
            set_signal_cooldown(opp["instId"], opp["side"])
            logger.info(f"  #{sent} {opp['instId']} [{opp['tf']}]{opp['side']} {opp['score']}分")
            in_zone, live, zone_msg = _check_entry_zone(opp)
            logger.info(f"     {zone_msg}")
            if in_zone and live > 0:
                time.sleep(0.5)
                send_tg(format_alert(
                    coin=opp["instId"].split("-")[0], side=opp["side"],
                    alert_type="ENTRY", price=live,
                    entry=opp["entry"], sl=opp["sl"],
                    tp1=opp["tp1"], tp2=opp["tp2"], tp3=opp["tp3"],
                    score=opp["score"],
                ), alert_type="ENTRY_IMMEDIATE", coin=opp["instId"].split("-")[0])
            tracker.add(opp)
        time.sleep(0.8)
    logger.info(f"✅ 掃描完成，發送 {sent} 筆")
    if sent > 0:
        send_tg(tracker.status_summary(), alert_type="STATUS_SUMMARY")
    return sent

# ─────────────────────────────────────────────────────────
# 19. 主函式
# ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpha Oracle v9.2.0")
    parser.add_argument("--mode", default="all",
                        choices=["scan","monitor","loop","all",
                                 "daily_report","monthly_report"],
                        help="scan=只掃描 | monitor=只監控 | loop=定時掃描+監控 | "
                             "all=掃描+監控 | daily_report=今日戰報 | monthly_report=月度戰報")
    parser.add_argument("--interval",      type=int, default=30, help="監控間隔(秒)")
    parser.add_argument("--loop-interval", type=int, default=900, help="掃描間隔(秒)")
    parser.add_argument("--status",        action="store_true", help="查詢追蹤狀態")
    parser.add_argument("--debug",         action="store_true", help="開啟 DEBUG 日誌")
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.info("🐛 DEBUG 模式已開啟")
    
    win_tracker = WinRateTracker(TRADE_HISTORY_FILE)
    tracker     = SignalTracker(ACTIVE_SIGNALS_FILE, win_tracker=win_tracker)
    
    if args.status:
        summary = tracker.status_summary()
        print(summary)
        send_tg(summary, alert_type="STATUS_QUERY")
        return
    
    if args.mode == "daily_report":
        msg = win_tracker.daily_report()
        logger.info("📊 發送每日戰報")
        print(msg)
        send_tg(msg, alert_type="DAILY_REPORT")
        return
    
    if args.mode == "monthly_report":
        msg = win_tracker.monthly_report()
        logger.info("📅 發送月度戰報")
        print(msg)
        send_tg(msg, alert_type="MONTHLY_REPORT")
        return
    
    if args.mode == "scan":
        run_scan(tracker)
        return
    
    if args.mode == "monitor":
        try: 
            monitor_loop(tracker, interval=args.interval)
        except KeyboardInterrupt: 
            logger.info("⌨️ 監控停止 (KeyboardInterrupt)")
        return
    
    if args.mode == "loop":
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_loop,
                             args=(tracker, args.interval, stop_ev), daemon=True)
        t.start()
        try:
            while True:
                run_scan(tracker)
                logger.info(f"⏱️ 下次掃描：{args.loop_interval}s 後")
                time.sleep(args.loop_interval)
        except KeyboardInterrupt:
            logger.info("⌨️ 迴圈停止 (KeyboardInterrupt)")
            stop_ev.set()
        return
    
    # all 模式（預設）
    run_scan(tracker)
    try: 
        monitor_loop(tracker, interval=args.interval)
    except KeyboardInterrupt: 
        logger.info("⌨️ 停止 (KeyboardInterrupt)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"💥 程式崩潰: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
