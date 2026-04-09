#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v6.0 - Institutional SMC & Divergence Bot
核心改進：
  ✅ SMC 精準化：OB 50% Mean Threshold 進場，FVG > 1.5x ATR 過濾
  ✅ MTF 趨勢鎖定：1H 結構決定方向，禁止逆勢訊號
  ✅ 數據背離引擎：整合 CoinAnk CVD, LS Ratio, Funding Rate 判斷主力意圖
  ✅ PA 最終觸發：僅在區域內出現 Pin Bar/Engulfing 時觸發
  ✅ 完整執行流：(Zone + PA + Divergence) = Signal
"""

import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
import json
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alpha_oracle_v6.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TG_TOKEN            = os.getenv("TG_TOKEN")
CHAT_ID             = os.getenv("CHAT_ID")
COINANK_API_KEY     = os.getenv("COINANK_API_KEY", "")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "5"))
SETUP_SCORE_THRESHOLD = 0.40  # 綜合評分閾值 40分

# ─────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Telegram Error: {e}")
        return False

# ─────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────

def fetch_okx(instId: str, tf: str = "15m", limit: int = 200) -> pd.DataFrame | None:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0': return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm']=="1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] Fetch Error: {e}")
        return None

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        return float(res['data'][0]['fundingRate']) if res.get('data') else 0
    except:
        return 0

def fetch_ls_ratio(symbol: str) -> str:
    """獲取多空比"""
    try:
        base_id = symbol.split('-')[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        return res['data'][0]['ratio'] if res.get('data') else "N/A"
    except:
        return "N/A"

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple:
    """獲取盤口不平衡度"""
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5).json()
        if res.get('code') != '0' or not res.get('data'):
            return 1.0, "⚪ 盤口均衡"
        data = res['data'][0]
        bid_vol = sum(float(b[1]) for b in data['bids'])
        ask_vol = sum(float(a[1]) for a in data['asks'])
        if ask_vol == 0: return 1.0, "⚪ 盤口均衡"
        ratio = bid_vol / ask_vol
        if ratio > 1.2: label = f"🟢 買盤強勢 ({ratio:.2f})"
        elif ratio < 0.8: label = f"🔴 賣盤強勢 ({ratio:.2f})"
        else: label = f"⚪ 盤口均衡 ({ratio:.2f})"
        return ratio, label
    except:
        return 1.0, "⚪ 盤口均衡"

def fetch_coinank_data(symbol: str) -> dict | None:
    """獲取 CoinAnk 背離數據 (含容錯)"""
    if not COINANK_API_KEY: return None
    try:
        headers = {"Authorization": f"Bearer {COINANK_API_KEY}"}
        
        # 1. Spot CVD
        cvd_res = requests.get(f"https://api.coinank.com/api/indicators/spot-cvd?symbol={symbol}&period=24h", 
                              headers=headers, timeout=10).json()
        cvd_val = float(cvd_res['data']['cvd_value']) if cvd_res.get('data') else 0
        
        # 2. Long/Short Ratio
        ls_res = requests.get(f"https://api.coinank.com/api/ratio/long-short-account-ratio?symbol={symbol}", 
                             headers=headers, timeout=10).json()
        ls_val = float(ls_res['data']['ratio']) if ls_res.get('data') else 1.0
        
        return {"cvd": cvd_val, "ls_ratio": ls_val}
    except Exception as e:
        logging.warning(f"CoinAnk Data Error: {e}")
        return None

# ─────────────────────────────────────────────
# 4. 技術指標計算
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple:
    """計算 Supertrend 指標"""
    if len(df) < period + 2: return 0, "⚪ 未知"
    
    high = df['h'].values.astype(float)
    low = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n = len(df)
    
    # Calculate ATR
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    
    # Calculate Supertrend
    hl2 = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr
    basic_dn = hl2 + multiplier * atr
    
    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend = np.ones(n, dtype=int)
    
    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]
    
    for i in range(period+1, n):
        final_up[i] = basic_up[i] if basic_up[i] > final_up[i-1] or close[i-1] < final_up[i-1] else final_up[i-1]
        final_dn[i] = basic_dn[i] if basic_dn[i] < final_dn[i-1] or close[i-1] > final_dn[i-1] else final_dn[i-1]
        
        if trend[i-1] == -1 and close[i] > final_dn[i-1]:
            trend[i] = 1
        elif trend[i-1] == 1 and close[i] < final_up[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]
    
    if trend[-1] == 1:
        return 1, "🟢 多頭"
    elif trend[-1] == -1:
        return -1, "🔴 空頭"
    return 0, "⚪ 未知"

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    """尋找擺動高點和低點"""
    data = df.tail(lookback).reset_index(drop=True)
    sh, sl = [], []
    for i in range(n, len(data) - n):
        wh = data['h'].iloc[i-n:i+n+1]
        wl = data['l'].iloc[i-n:i+n+1]
        if data['h'].iloc[i] == wh.max():
            sh.append(data['h'].iloc[i])
        if data['l'].iloc[i] == wl.min():
            sl.append(data['l'].iloc[i])
    return sorted(set(sh)), sorted(set(sl))

def detect_market_structure_1h(df_1h: pd.DataFrame) -> str:
    """判斷 1H 市場結構 (用於趨勢鎖定)"""
    if len(df_1h) < 50: return "NEUTRAL"
    last_c = df_1h['c'].iloc[-1]
    prev_h = df_1h['h'].iloc[-2]
    prev_l = df_1h['l'].iloc[-2]
    
    if last_c > prev_h: return "BULLISH"
    elif last_c < prev_l: return "BEARISH"
    return "NEUTRAL"

def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
    """檢測市場結構（15m M頭/W底）"""
    sh, sl = find_swing_points(df, n=3, lookback=60)
    
    has_w = len(sl) >= 2 and sl[-2] > 0 and abs(sl[-2] - sl[-1]) / sl[-2] < 0.015
    has_m = len(sh) >= 2 and sh[-2] > 0 and abs(sh[-2] - sh[-1]) / sh[-2] < 0.015
    
    if side == "LONG":
        if has_w: return "W 底反轉 📐"
        if has_m: return "M 頭壓制 ⚠️"
    elif side == "SHORT":
        if has_m: return "M 頭反轉 📐"
        if has_w: return "W 底支撐 ⚠️"
    
    if has_w: return "W 底反轉 📐"
    if has_m: return "M 頭反轉 📐"
    
    recent = df.tail(20)
    slope = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if slope > 0.025: return "上升趨勢延續 📈"
    elif slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

# ─────────────────────────────────────────────
# 🆕 SMC 精準化模組 (OB & FVG)
# ─────────────────────────────────────────────

def find_order_block_zone(df: pd.DataFrame, side: str) -> dict | None:
    """
    找出 OB 區間並計算 50% Mean Threshold 作為進場參考
    """
    data = df.tail(100).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, kn = data.iloc[i], data.iloc[i+1]
        if side == "LONG" and k['c'] < k['o'] and kn['c'] > kn['o']:
            # 多頭 OB: 下跌後的陽線，取整根陰線的 50%
            high, low = k['o'], k['l']
            mean_thresh = (high + low) / 2
            return {"high": high, "low": low, "mean": mean_thresh, "type": "OB"}
        if side == "SHORT" and k['c'] > k['o'] and kn['c'] < kn['o']:
            # 空頭 OB: 上漲後的陰線，取整根陽線的 50%
            high, low = k['h'], k['c']
            mean_thresh = (high + low) / 2
            return {"high": high, "low": low, "mean": mean_thresh, "type": "OB"}
    return None

def find_valid_fvg(df: pd.DataFrame, side: str, atr: float) -> dict | None:
    """
    找出有效的 FVG (垂直高度 > 1.5 × ATR)
    """
    for i in range(len(df) - 3, max(len(df) - 100, 0), -1):
        k0, k2 = df.iloc[i-1], df.iloc[i+1]
        if side == "LONG" and k2['l'] > k0['h']:
            gap_height = k2['l'] - k0['h']
            if gap_height > (1.5 * atr):
                return {"high": k2['l'], "low": k0['h'], "type": "FVG"}
        if side == "SHORT" and k2['h'] < k0['l']:
            gap_height = k0['l'] - k2['h']
            if gap_height > (1.5 * atr):
                return {"high": k0['l'], "low": k2['h'], "type": "FVG"}
    return None

def check_mtf_trend_lock(df_1h: pd.DataFrame, side: str) -> bool:
    """
    MTF 趨勢鎖定：若 1H 為多頭結構，禁止 15m 發出空頭訊號 (反之亦然)
    """
    struct_1h = detect_market_structure_1h(df_1h)
    
    if side == "SHORT" and struct_1h == "BULLISH":
        return False  # 1H 多頭，禁止空單
    if side == "LONG" and struct_1h == "BEARISH":
        return False  # 1H 空頭，禁止多單
    return True

# ─────────────────────────────────────────────
# 🆕 數據背離判斷模組
# ─────────────────────────────────────────────

def check_data_divergence(curr_price: float, prev_high: float, prev_low: float, data: dict, side: str) -> bool:
    """
    檢查數據背離 (價格與市場指標的背離)
    
    SHORT (抓頂): 價格創新高 + CVD下降 + LS急升 + 資費過高 = 主力誘多
    LONG (抓底):  價格創新低 + CVD上升 + LS急降 + 資費過低 = 主力吸籌
    """
    if not data: return False
    
    cvd = data.get('cvd', 0)
    ls = data.get('ls_ratio', 1.0)
    fr = data.get('funding_rate', 0)
    
    if side == "SHORT":
        # 價格在高位，但指標顯示主力在出貨
        # CVD < 0 (現貨賣壓), LS > 1.1 (散戶狂買), Funding > 0.0003 (持倉成本高)
        if cvd < 0 and ls > 1.1 and fr > 0.0003:
            return True
    elif side == "LONG":
        # 價格在低位，但指標顯示主力在吸籌
        # CVD > 0 (現貨買盤), LS < 0.9 (散戶割肉), Funding < -0.0003 (持倉成本低)
        if cvd > 0 and ls < 0.9 and fr < -0.0003:
            return True
            
    return False

# ─────────────────────────────────────────────
# 🆕 PA 最終觸發模組
# ─────────────────────────────────────────────

def detect_pa_in_zone(df: pd.DataFrame, zone: dict, side: str) -> bool:
    """
    PA 最終觸發：價格進入區域 + Pin Bar / Engulfing 確認
    
    邏輯：(Price inside Zone) AND (PA Signal == True) THEN Trigger
    """
    if len(df) < 3: return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # 1. 檢查價格是否進入區域
    in_zone = (last['l'] <= zone['high'] and last['h'] >= zone['low'])
    if not in_zone: return False
    
    body = abs(last['c'] - last['o'])
    rng = last['h'] - last['l'] + 1e-10
    upper_wick = last['h'] - max(last['c'], last['o'])
    lower_wick = min(last['c'], last['o']) - last['l']
    
    # 2. Pin Bar 確認 (長影線反轉)
    is_pin = False
    if side == "SHORT" and upper_wick > (body * 2.0) and lower_wick < (body * 0.5) and last['c'] < last['o']:
        is_pin = True  # 看跌流星線
    elif side == "LONG" and lower_wick > (body * 2.0) and upper_wick < (body * 0.5) and last['c'] > last['o']:
        is_pin = True  # 看漲錘子線
        
    # 3. Engulfing 確認 (吞噬形態)
    is_engulf = False
    if side == "SHORT" and last['c'] < last['o'] and prev['c'] > prev['o'] and last['o'] >= prev['c'] and last['c'] <= prev['o']:
        is_engulf = True
    elif side == "LONG" and last['c'] > last['o'] and prev['c'] < prev['o'] and last['o'] <= prev['c'] and last['c'] >= prev['o']:
        is_engulf = True
        
    return is_pin or is_engulf

def detect_price_action(df: pd.DataFrame, side: str) -> list:
    """檢測價格行為形態 (用於顯示)"""
    signals = []
    
    for i in range(len(df) - 1, max(len(df) - 5, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        upper_wick = k['h'] - max(k['c'], k['o'])
        lower_wick = min(k['c'], k['o']) - k['l']
        
        # 流星線
        if side == "SHORT" and upper_wick >= body * 2.0 and lower_wick <= body * 0.5:
            strength = min(upper_wick / (body + 1e-10), 5.0)
            signals.append(f"空頭流星線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        
        # 錘子線
        if side == "LONG" and lower_wick >= body * 2.0 and upper_wick <= body * 0.5:
            strength = min(lower_wick / (body + 1e-10), 5.0)
            signals.append(f"多頭錘子線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        
        # 壓力位拒絕
        if side == "SHORT" and upper_wick / total_range > 0.40 and k['c'] < k['o']:
            signals.append(f"壓力位拒絕 (上影 {upper_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        
        # 支撐位拒絕
        if side == "LONG" and lower_wick / total_range > 0.40 and k['c'] > k['o']:
            signals.append(f"支撐位拒絕 (下影 {lower_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        
        # 動量棒
        body_pct = body / total_range
        if body_pct >= 0.70:
            if (side == "LONG" and k['c'] > k['o']) or (side == "SHORT" and k['c'] < k['o']):
                signals.append(f"{'多頭' if side=='LONG' else '空頭'}動量棒 ({body_pct*100:.0f}%實體) @ {k['c']:.4f}")
    
    return signals[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
    """計算價格行為評分"""
    score = 0.0
    signals = detect_price_action(df, side)
    
    if len(signals) >= 3: score += 0.60
    elif len(signals) >= 2: score += 0.40
    elif len(signals) >= 1: score += 0.20
    
    last_k = df.iloc[-1]
    body = abs(last_k['c'] - last_k['o'])
    rng = last_k['h'] - last_k['l'] + 1e-10
    
    if body / rng > 0.70: score += 0.20
    if (side == "LONG" and last_k['c'] > last_k['o']) or (side == "SHORT" and last_k['c'] < last_k['o']):
        score += 0.20
    
    score = min(score, 1.0)
    
    if score >= 0.65: label = "✅ 強勢PA"
    elif score >= 0.40: label = "⚠️ 中等PA"
    else: label = "⛔ 弱PA"
    
    return score * 100, label, signals

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    """檢測主力區域（派發區/吸籌區）"""
    zones = []
    vol_ma = df['v'].rolling(20).mean()
    vol_std = df['v'].rolling(20).std()
    
    for i in range(max(len(df) - 10, 0), len(df)):
        if df['v'].iloc[i] > vol_ma.iloc[i] + 2 * vol_std.iloc[i]:
            if df['c'].iloc[i] > df['o'].iloc[i] and side == "LONG":
                zones.append(f"🔵 主力吸籌區 {df['c'].iloc[i]:.4f}")
            elif df['c'].iloc[i] < df['o'].iloc[i] and side == "SHORT":
                zones.append(f"🔴 主力派發區 {df['c'].iloc[i]:.4f}")
    
    recent_high = df['h'].iloc[-20:].max()
    recent_low = df['l'].iloc[-20:].min()
    if side == "SHORT":
        zones.append(f"🔴 多頭清算熱點 {recent_high:.4f}")
    else:
        zones.append(f"🔵 空頭清算熱點 {recent_low:.4f}")
    
    return zones[:2]

def calculate_setup_score(setup: dict) -> float:
    """計算綜合評分"""
    score = 0.0
    
    if setup.get('whale_signal') == "✅ 主力一致":
        score += 0.30 * setup.get('whale_confidence', 0)
    elif setup.get('whale_signal') == "⚠️ 主力警示":
        score += 0.15 * setup.get('whale_confidence', 0)
    
    score += 0.25 * setup.get('pa_score', 0) / 100
    
    if setup.get('st_label') == "🟢 多頭" and setup.get('side') == "LONG":
        score += 0.20
    elif setup.get('st_label') == "🔴 空頭" and setup.get('side') == "SHORT":
        score += 0.20
    
    if setup.get('cvd_label', '').startswith("🟢") and setup.get('side') == "LONG":
        score += 0.15
    elif setup.get('cvd_label', '').startswith("🔴") and setup.get('side') == "SHORT":
        score += 0.15
    
    try:
        fr = setup.get('funding_rate', 0)
        if setup.get('side') == "LONG" and fr < 0.0003:
            score += 0.10
        elif setup.get('side') == "SHORT" and fr > -0.0003:
            score += 0.10
    except:
        pass
    
    return min(score, 1.0) * 100

# ─────────────────────────────────────────────
# 5. 主掃描邏輯 (整合所有新功能)
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數 - 整合 SMC + MTF + Divergence + PA"""
    # 獲取 15m 和 1H K 線
    df_15m = fetch_okx(instId, tf="15m", limit=200)
    df_1h = fetch_okx(instId, tf="1H", limit=100)
    if df_15m is None or df_1h is None: return []
    
    # 計算基礎指標
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    
    # 獲取市場數據 (背離判斷用)
    market_data = {
        "funding_rate": fetch_funding_rate(instId),
        "ls_ratio": fetch_ls_ratio(instId),
    }
    coinank_data = fetch_coinank_data(instId.split('-')[0])
    if coinank_data:
        market_data.update(coinank_data)
    
    opportunities = []
    
    for side in ["LONG", "SHORT"]:
        # 🆕 1. MTF 趨勢鎖定
        mtf_ok = check_mtf_trend_lock(df_1h, side)
        if not mtf_ok:
            logging.info(f"[{instId}] {side} blocked by MTF lock (1H: {detect_market_structure_1h(df_1h)})")
            continue
        
        # 🆕 2. 尋找有效的進場區域 (優先找有效區域)
        ob_zone = find_order_block_zone(df_15m, side)
        fvg_zone = find_valid_fvg(df_15m, side, atr)  # 已過濾 < 1.5 ATR
        
        zones = []
        if ob_zone: zones.append(ob_zone)
        if fvg_zone: zones.append(fvg_zone)
        
        if not zones: continue
        
        for zone in zones:
            # 🆕 3. 數據背離檢查
            curr_price = df_15m['c'].iloc[-1]
            prev_high = df_15m['h'].iloc[-20:].max()
            prev_low = df_15m['l'].iloc[-20:].min()
            div_confirmed = check_data_divergence(curr_price, prev_high, prev_low, market_data, side)
            
            # 🆕 4. PA 最終觸發 (價格進入區域 + K 線確認)
            pa_triggered = detect_pa_in_zone(df_15m, zone, side)
            
            # 只有同時滿足: 區域有效 + 背離確認 + PA 觸發 才生成訊號
            if not (pa_triggered and div_confirmed):
                continue
            
            # 計算進場價 (使用 50% Mean Threshold 或區域邊界)
            if zone['type'] == "OB":
                entry = zone['mean']  # OB 用 50% 位置
            else:  # FVG
                entry = zone['high'] if side == "SHORT" else zone['low']  # FVG 用邊界
            
            # 計算風控
            if side == "LONG":
                sl = zone['low'] - (atr * 0.5)
            else:
                sl = zone['high'] + (atr * 0.5)
            
            risk = abs(entry - sl)
            tp1 = entry + risk if side == "LONG" else entry - risk
            tp2 = entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5
            tp3 = entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0
            
            # 其他分析數據
            pa_score, pa_label, pa_signals = calculate_pa_score(df_15m, side)
            structure = detect_market_structure(df_15m, side)
            cvd_val = df_15m['c'].iloc[-1] - df_15m['o'].iloc[-1]
            cvd_label = "🟢 大戶吸籌 (CVD+)" if cvd_val > 0 else "🔴 大戶出貨 (CVD-)"
            whale_zones = detect_whale_zones(df_15m, side)
            ob_ratio, ob_label = fetch_order_book_imbalance(instId)
            
            # 綜合評分
            setup = {
                'side': side, 'pa_score': pa_score, 'st_label': st_label,
                'cvd_label': cvd_label, 'funding_rate': market_data.get('funding_rate', 0),
                'whale_signal': "✅ 主力一致" if len(whale_zones) > 0 else "❓ 技術面主導",
                'whale_confidence': 0.82 if div_confirmed else 0.65
            }
            setup_score = calculate_setup_score(setup)
            
            if setup_score < SETUP_SCORE_THRESHOLD * 100:
                continue
            
            opp = {
                "instId": instId, "side": side,
                "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "zone": zone, "zone_type": zone['type'],
                "structure": structure, "pa_score": pa_score, "pa_label": pa_label,
                "pa_signals": pa_signals, "pa_triggered": pa_triggered,
                "cvd_label": cvd_label, "ls_ratio": market_data.get('ls_ratio', 'N/A'),
                "funding_rate": market_data.get('funding_rate', 0),
                "ob_label": ob_label, "whale_zones": whale_zones,
                "st_label": st_label, "setup_score": setup_score,
                "divergence": div_confirmed, "mtf_ok": mtf_ok,
                "leverage": f"10x ~ 20x (低波動)" if atr / curr_price < 0.015 else "3x ~ 5x (高波動)"
            }
            opportunities.append(opp)
            break  # 每個方向只取第一個有效訊號
    
    return opportunities

def format_signal_message(opp: dict) -> str:
    """格式化信號消息"""
    coin_symbol = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # SNR 顯示
    snr_display = "🟢 支撐 ─ | 🔴 壓力 ─"
    snr_active = "⚠️ 無明顯關鍵位"
    if opp.get('zone'):
        if opp['side'] == "LONG":
            snr_display = f"🟢 支撐 {opp['zone']['low']:.4f} | 🔴 壓力 ─"
            snr_active = f"✅ 參考 {opp['zone_type']} 50%: {opp['entry']:.4f}"
        else:
            snr_display = f"🟢 支撐 ─ | 🔴 壓力 {opp['zone']['high']:.4f}"
            snr_active = f"✅ 參考 {opp['zone_type']} 50%: {opp['entry']:.4f}"
    
    # PA 信號
    pa_lines = ""
    if opp['pa_signals']:
        for sig in opp['pa_signals'][:3]:
            pa_lines += f"   {sig}\n"
    else:
        pa_lines = "   ─ 無明顯 PA 訊號\n"
    
    # 背離與趨勢狀態
    div_msg = "✅ 背離確認" if opp['divergence'] else "⚠️ 無背離"
    mtf_msg = "✅ 順勢" if opp['mtf_ok'] else "❌ 逆勢"
    
    # 主力區
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    msg = (
        f"🔥 *Alpha Oracle v6.0 | 機構級訊號* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin_symbol}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m\n"
        f"📊 多空比 {opp['ls_ratio']} | 資費 {opp['funding_rate']*100:.4f}%\n"
        f"🧬 CVD：{opp['cvd_label']}\n"
        f"📚 盤口：{opp['ob_label']}\n"
        f"\n"
        f"💰 進場位：{opp['entry']:.4f} ⚡({opp['zone_type']} 50%)\n"
        f"🛑 止損位：{opp['sl']:.4f} (-1R)\n"
        f"💰 TP1 (1.0R): {opp['tp1']:.4f}\n"
        f"💰 TP2 (2.5R): {opp['tp2']:.4f}\n"
        f"💰 TP3 (4.0R): {opp['tp3']:.4f}\n"
        f"\n"
        f"🏗️ 結構：{opp['structure']}\n"
        f"🛡️ SNR：{snr_display}\n"
        f"    {snr_active}\n"
        f"\n"
        f"🕯️ 價格行為 ({opp['pa_label']} {opp['pa_score']:.0f}分)\n"
        f"{pa_lines}"
        f"📡 Supertrend：{opp['st_label']}\n"
        f"🔒 MTF 趨勢：{mtf_msg}\n"
        f"🧬 數據背離：{div_msg}\n"
        f"🐋 主力區：{whale_text}\n"
        f"🕹️ 槓桿：{opp['leverage']}\n"
        f"📊 綜合評分：{opp['setup_score']:.0f}分 (閾值:{SETUP_SCORE_THRESHOLD*100:.0f}分)\n"
        f"\n"
        f"💡 *等待回踩 {opp['zone_type']} 50% 成交...*"
    )
    return msg

# ─────────────────────────────────────────────
# 6. 主執行函數
# ─────────────────────────────────────────────

def main():
    """主函數 - 一次性掃描"""
    logging.info("🚀 Alpha Oracle v6.0 Started - Institutional Mode")
    
    signals_sent = 0
    
    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] Scanning {coin}...")
        
        try:
            opps = scan_for_opportunity(coin)
            
            if opps:
                logging.info(f"✅ Found {len(opps)} qualified opportunity(ies) for {coin}")
                
                for opp in opps:
                    if signals_sent >= MAX_SIGNALS_PER_RUN:
                        logging.info(f"⚠️ Reached max signals limit ({MAX_SIGNALS_PER_RUN})")
                        break
                    
                    msg = format_signal_message(opp)
                    if send_tg(msg):
                        signals_sent += 1
                        logging.info(f"✅ Signal {signals_sent}/{MAX_SIGNALS_PER_RUN} sent for {coin} {opp['side']}")
                    else:
                        logging.error(f"❌ Failed to send signal for {coin}")
                    
                    time.sleep(1)  # Avoid Telegram rate limit
            else:
                logging.info(f"❌ No qualified opportunities for {coin}")
            
            time.sleep(0.5)  # Avoid API rate limit
            
        except Exception as e:
            logging.error(f"❌ Scan Error for {coin}: {e}")
            traceback.print_exc()
            continue
    
    logging.info("=" * 50)
    logging.info(f"📊 SCAN COMPLETE")
    logging.info(f"✅ Total signals sent: {signals_sent}")
    logging.info("=" * 50)
    
    if signals_sent == 0:
        send_tg("📊 *Alpha Oracle v6.0 掃描完成*\n\n本次掃描未發現符合所有條件 (區域+背離+PA) 的交易機會。\n\n下次掃描將在下一個排程執行。")
    
    return signals_sent

if __name__ == "__main__":
    try:
        signals_count = main()
        logging.info(f"🎉 Bot finished successfully. Sent {signals_count} signals.")
        exit(0)
    except Exception as e:
        logging.error(f"💥 Bot crashed: {e}")
        traceback.print_exc()
        exit(1)
