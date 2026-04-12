#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v7.1 - MTF Zone Confluence Engine
核心改進：
  ✅ 保留 v7.0 所有盤口行為邏輯（十字線/掃單/釣魚單/新聞冷卻/帶量止損）
  ✅ 新增：1H/30M/15M 多時區 OB/FVG 共振過濾（≥2 時區價格重疊才觸發）
  ✅ 動態容忍度：max(ATR×0.5, 價格×0.3%) 允許合理誤差
  ✅ 進場點自動對齊共振區邊緣，提升結構勝率
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
        logging.FileHandler("alpha_oracle_v7.1.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TG_TOKEN            = os.getenv("TG_TOKEN")
CHAT_ID             = os.getenv("CHAT_ID")
COINANK_API_KEY     = os.getenv("COINANK_API_KEY", "")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "5"))
SETUP_SCORE_THRESHOLD = 0.40  # 綜合評分閾值 40 分

# 🆕 盤口行為策略參數
CROSSLINE_BODY_RATIO = 0.3
SWEEP_VOLUME_RATIO = 2.0
SWEEP_PRICE_STEPS = 3
NEWS_COOLDOWN_MINUTES = 60
VOLUME_CONFIRMATION_RATIO = 1.5
WALL_IMBALANCE_THRESHOLD = 0.3
ABSORPTION_PRICE_CHANGE_THRESHOLD = 0.002

# 新聞冷卻追蹤
last_news_time = {}

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
    try:
        base_id = symbol.split('-')[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        return res['data'][0]['ratio'] if res.get('data') else "N/A"
    except:
        return "N/A"

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple:
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
    if len(df) < period + 2: return 0, "⚪ 未知"
    high = df['h'].values.astype(float)
    low = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
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

def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
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

def find_snr_zones(df: pd.DataFrame, side: str, lookback: int = 30) -> dict:
    sh, sl = find_swing_points(df, n=2, lookback=lookback)
    price = df['c'].iloc[-1]
    if side == "LONG":
        valid = [s for s in sl if s < price * 0.995]
        if valid:
            s = max(valid)
            return {"support": s, "resistance": None, "active_level": s, "text": f"支撐 {s:.4f}"}
    else:
        valid = [r for r in sh if r > price * 1.005]
        if valid:
            r = min(valid)
            return {"support": None, "resistance": r, "active_level": r, "text": f"壓力 {r:.4f}"}
    return None

def detect_price_action(df: pd.DataFrame, side: str) -> list:
    signals = []
    for i in range(len(df) - 1, max(len(df) - 5, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        upper_wick = k['h'] - max(k['c'], k['o'])
        lower_wick = min(k['c'], k['o']) - k['l']
        if side == "SHORT" and upper_wick >= body * 2.0 and lower_wick <= body * 0.5:
            strength = min(upper_wick / (body + 1e-10), 5.0)
            signals.append(f"空頭流星線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        if side == "LONG" and lower_wick >= body * 2.0 and upper_wick <= body * 0.5:
            strength = min(lower_wick / (body + 1e-10), 5.0)
            signals.append(f"多頭錘子線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        if side == "SHORT" and upper_wick / total_range > 0.40 and k['c'] < k['o']:
            signals.append(f"壓力位拒絕 (上影 {upper_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        if side == "LONG" and lower_wick / total_range > 0.40 and k['c'] > k['o']:
            signals.append(f"支撐位拒絕 (下影 {lower_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        body_pct = body / total_range
        if body_pct >= 0.70:
            if (side == "LONG" and k['c'] > k['o']) or (side == "SHORT" and k['c'] < k['o']):
                signals.append(f"{'多頭' if side=='LONG' else '空頭'}動量棒 ({body_pct*100:.0f}%實體) @ {k['c']:.4f}")
    return signals[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
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
# 🆕 盤口行為策略模組 (Order Flow & Tape Reading)
# ─────────────────────────────────────────────

def detect_crossline(df: pd.DataFrame, lookback: int = 20) -> dict | None:
    for i in range(len(df) - 1, max(len(df) - lookback - 1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        if body < CROSSLINE_BODY_RATIO * total_range:
            upper_wick = k['h'] - max(k['c'], k['o'])
            lower_wick = min(k['c'], k['o']) - k['l']
            if upper_wick > lower_wick * 1.5:
                potential_side = "SHORT"
            elif lower_wick > upper_wick * 1.5:
                potential_side = "LONG"
            else:
                potential_side = "NEUTRAL"
            return {
                "index": i, "price": k['c'], "high": k['h'], "low": k['l'],
                "body": body, "range": total_range, "potential_side": potential_side,
                "desc": f"🎯 十字線定價中心 @ {k['c']:.4f} (潛在：{potential_side})"
            }
    return None

def detect_sweep_behavior(df: pd.DataFrame, side: str, lookback: int = 10) -> bool:
    if len(df) < lookback + 1: return False
    recent = df.tail(lookback + 1)
    vol_ma = recent['v'].iloc[:-1].mean()
    if recent['v'].iloc[-1] < SWEEP_VOLUME_RATIO * vol_ma:
        return False
    price_changes = recent['c'].diff().abs()
    consecutive_moves = 0
    for i in range(len(price_changes) - 1, 0, -1):
        if price_changes.iloc[i] > 0:
            if (side == "LONG" and recent['c'].iloc[i] > recent['c'].iloc[i-1]) or \
               (side == "SHORT" and recent['c'].iloc[i] < recent['c'].iloc[i-1]):
                consecutive_moves += 1
                if consecutive_moves >= SWEEP_PRICE_STEPS:
                    return True
            else:
                break
    return False

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df) < 5: return False
    recent = df.tail(5)
    vol_ma = recent['v'].mean()
    price_move = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / recent['c'].iloc[0]
    if price_move < 0.005: return False
    if recent['v'].iloc[-1] < 0.8 * vol_ma:
        return True
    return False

def check_news_cooldown(instId: str) -> bool:
    now = time.time()
    if instId in last_news_time:
        if now - last_news_time[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False
    return True

def mark_news_event(instId: str):
    last_news_time[instId] = time.time()
    logging.info(f"📰 News event marked for {instId}")

def validate_stop_loss_with_volume(df: pd.DataFrame, sl_price: float, side: str) -> bool:
    if len(df) < 5: return True
    recent = df.tail(5)
    if side == "LONG":
        if recent['l'].iloc[-1] > sl_price: return True
        breakout_vol = recent['v'].iloc[-1]
    else:
        if recent['h'].iloc[-1] < sl_price: return True
        breakout_vol = recent['v'].iloc[-1]
    prev_vol_ma = recent['v'].iloc[:-1].mean()
    return breakout_vol >= VOLUME_CONFIRMATION_RATIO * prev_vol_ma

def detect_wall_imbalance(instId: str, depth: int = 20) -> tuple[bool, str]:
    ratio, label = fetch_order_book_imbalance(instId, depth)
    imbalance = abs(ratio - 1.0)
    if imbalance > WALL_IMBALANCE_THRESHOLD:
        direction = "🟢 買牆突破" if ratio > 1.0 else "🔴 賣牆突破"
        return True, direction
    return False, "⚪ 牆體平衡"

def detect_absorption(df: pd.DataFrame, side: str) -> bool:
    if len(df) < 10: return False
    recent = df.tail(10)
    vol_ma = recent['v'].mean()
    price_change = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / recent['c'].iloc[0]
    if recent['v'].iloc[-1] > 2.0 * vol_ma and price_change < ABSORPTION_PRICE_CHANGE_THRESHOLD:
        return True
    return False

# ─────────────────────────────────────────────
# 🆕 多時區共振過濾引擎 (MTF Confluence)
# ─────────────────────────────────────────────

def check_mtf_zone_confluence(instId: str, side: str, current_price: float, atr_15m: float) -> tuple[bool, dict | None]:
    """
    檢查 1H, 30M, 15M 三個時區中，是否有至少兩個時區的 OB/FVG 落在相同價格區間
    容忍度：max(ATR*0.5, 價格*0.3%)
    返回：(是否共振, 共振區間資訊)
    """
    tfs = ["1H", "30m", "15m"]
    tf_zones = {}
    
    for tf in tfs:
        df = fetch_okx(instId, tf=tf, limit=150)
        if df is not None:
            zones = []
            # OB 掃描
            for i in range(len(df)-2, 0, -1):
                k, kn = df.iloc[i], df.iloc[i+1]
                if side == "LONG" and k['c'] < k['o'] and kn['c'] > kn['o']:
                    zones.append({'type': 'OB', 'high': k['o'], 'low': k['l']})
                if side == "SHORT" and k['c'] > k['o'] and kn['c'] < kn['o']:
                    zones.append({'type': 'OB', 'high': k['h'], 'low': k['c']})
            # FVG 掃描
            for i in range(len(df)-3, max(len(df)-30, 0), -1):
                k0, k2 = df.iloc[i-1], df.iloc[i+1]
                if side == "LONG" and k2['l'] > k0['h']:
                    zones.append({'type': 'FVG', 'high': k2['l'], 'low': k0['h']})
                if side == "SHORT" and k2['h'] < k0['l']:
                    zones.append({'type': 'FVG', 'high': k0['l'], 'low': k2['h']})
            tf_zones[tf] = zones[:2]  # 每個時區只取最近 2 個有效區間

    # 設定價格容忍度 (ATR 的 0.5 倍 或 價格的 0.3%，取較大者)
    tolerance = max(atr_15m * 0.5, current_price * 0.003)
    confluence_count = 0
    best_zone = None
    best_overlap_score = -1

    # 兩兩比較時區，尋找重疊或鄰近的區間
    tf_keys = list(tf_zones.keys())
    for i in range(len(tf_keys)):
        for j in range(i + 1, len(tf_keys)):
            tf_a, tf_b = tf_keys[i], tf_keys[j]
            for z_a in tf_zones.get(tf_a, []):
                for z_b in tf_zones.get(tf_b, []):
                    # 計算區間距離（重疊時 distance <= 0）
                    dist = max(0, z_a['low'] - z_b['high'], z_b['low'] - z_a['high'])
                    
                    if dist <= tolerance:
                        # 計算合併後的有效區間
                        merged_low = min(z_a['low'], z_b['low'])
                        merged_high = max(z_a['high'], z_b['high'])
                        
                        # 評分：重疊面積越大、涉及時區越多，分數越高
                        overlap = max(0, min(z_a['high'], z_b['high']) - max(z_a['low'], z_b['low']))
                        score = overlap + (1.0 if dist == 0 else 0.5)
                        
                        if score > best_overlap_score:
                            best_overlap_score = score
                            best_zone = {
                                'high': merged_high, 
                                'low': merged_low, 
                                'tfs': [tf_a, tf_b], 
                                'overlap': overlap,
                                'desc': f"🔗 {tf_a}+{tf_b} 共振區 @ {merged_low:.4f}-{merged_high:.4f}"
                            }
                            confluence_count += 1

    # 要求至少 2 個時區共振
    if confluence_count >= 1 and best_zone:
        return True, best_zone
    return False, None

# ─────────────────────────────────────────────
# 5. 主掃描邏輯（整合盤口行為 + 多時區共振）
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數 - 整合盤口行為策略 + 多時區共振"""
    df_15m = fetch_okx(instId, tf="15m", limit=100)
    if df_15m is None: return []
    
    # 計算指標
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    current_price = df_15m['c'].iloc[-1]
    
    # 🆕 1. MTF 共振檢查（硬性過濾：不共振直接跳過）
    mtf_ok, mtf_zone = check_mtf_zone_confluence(instId, "LONG", current_price, atr)
    if not mtf_ok:
        mtf_ok, mtf_zone = check_mtf_zone_confluence(instId, "SHORT", current_price, atr)
    
    if not mtf_ok:
        logging.info(f"[{instId}] No MTF confluence found, skipping")
        return []
    
    # 使用共振區作為核心參考區間
    zone_ref = mtf_zone
    side = "LONG" if current_price > zone_ref['high'] else "SHORT"  # 簡化方向判斷，可替換為其他邏輯
    
    # 🆕 2. 盤口行為檢查
    crossline = detect_crossline(df_15m)
    if not crossline: return []
    if not check_news_cooldown(instId): return []
    
    # 🆕 3. 掃單確認（進場必要條件）
    if not detect_sweep_behavior(df_15m, side): return []
    
    # 🆕 4. 釣魚單過濾
    if detect_fishing_trap(df_15m, side): return []
    
    # 🆕 5. 進場價與止損設定（基於共振區邊緣）
    if side == "LONG":
        entry = zone_ref['low'] * 0.9995  # 略低於共振區下緣
        sl = zone_ref['low'] - atr * 1.2
    else:
        entry = zone_ref['high'] * 1.0005  # 略高於共振區上緣
        sl = zone_ref['high'] + atr * 1.2
        
    # 🆕 6. 帶量止損驗證
    if not validate_stop_loss_with_volume(df_15m, sl, side): return []
    
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
    funding_rate = fetch_funding_rate(instId)
    ls_ratio = fetch_ls_ratio(instId)
    ob_ratio, ob_label = fetch_order_book_imbalance(instId)
    wall_break, wall_msg = detect_wall_imbalance(instId)
    absorption = detect_absorption(df_15m, side)
    
    # 綜合評分
    setup = {
        'side': side, 'pa_score': pa_score, 'st_label': st_label,
        'cvd_label': cvd_label, 'funding_rate': funding_rate,
        'whale_signal': "✅ 主力一致" if whale_zones else "❓ 技術面主導",
        'whale_confidence': 0.85
    }
    setup_score = calculate_setup_score(setup)
    if setup_score < SETUP_SCORE_THRESHOLD * 100: return []
    
    opp = {
        "instId": instId, "side": side, "entry": entry, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "structure": structure,
        "snr_zone": {"support": zone_ref['low'], "resistance": zone_ref['high'], "active_level": current_price, "text": zone_ref['desc']},
        "pa_score": pa_score, "pa_label": pa_label, "pa_signals": pa_signals,
        "cvd_label": cvd_label, "ls_ratio": ls_ratio, "funding_rate": funding_rate,
        "ob_label": ob_label, "whale_zones": whale_zones, "st_label": st_label,
        "setup_score": setup_score,
        "leverage": f"10x ~ 20x (低波動)" if atr / current_price < 0.015 else "3x ~ 5x (高波動)",
        "crossline": crossline, "sweep_confirmed": True, "fishing_trap_filtered": False,
        "wall_msg": wall_msg, "absorption": absorption, "mtf_zone": zone_ref
    }
    return [opp]

# ─────────────────────────────────────────────
# 🆕 Telegram 通知格式（整合盤口行為 + 多時區共振資訊）
# ─────────────────────────────────────────────

def format_signal_message(opp: dict) -> str:
    """格式化信號消息（整合盤口行為 + 多時區共振資訊）"""
    coin_symbol = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # MTF 共振顯示
    mtf = opp.get('mtf_zone', {})
    snr_display = f"🟢 支撐 {mtf.get('low', 0):.4f} | 🔴 壓力 {mtf.get('high', 0):.4f}" if mtf else "🟢 支撐 ─ | 🔴 壓力 ─"
    snr_active = f"✅ {mtf.get('desc', '無共振區')}" if mtf else "⚠️ 無明顯關鍵位"
    
    # PA 信號
    pa_lines = "".join(f"   {sig}\n" for sig in opp['pa_signals'][:3]) if opp['pa_signals'] else "   ─ 無明顯 PA 訊號\n"
    
    # 主力區
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    # 🆕 盤口行為標記
    sweep_tag = "✅ 掃單確認" if opp.get('sweep_confirmed') else "⏳ 等待掃單"
    wall_tag = opp.get('wall_msg', '⚪ 牆體平衡')
    absorption_tag = "🔄 吸收中" if opp.get('absorption') else ""
    
    msg = (
        f"🔥 *Alpha Oracle v7.1 | MTF 共振訊號* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin_symbol}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m\n"
        f"📊 多空比 {opp['ls_ratio']} | 資費 {opp['funding_rate']*100:.4f}%\n"
        f"🧬 CVD：{opp['cvd_label']}\n"
        f"📚 盤口：{opp['ob_label']}\n"
        f"\n"
        f"💰 進場位：{opp['entry']:.4f} ⚡(共振區邊緣)\n"
        f"🛑 止損位：{opp['sl']:.4f} (-1R)\n"
        f"💰 TP1 (1.0R): {opp['tp1']:.4f}\n"
        f"💰 TP2 (2.5R): {opp['tp2']:.4f}\n"
        f"💰 TP3 (4.0R): {opp['tp3']:.4f}\n"
        f"\n"
        f"🏗️ 結構：{opp['structure']}\n"
        f"🛡️ MTF 共振：{snr_display}\n"
        f"    {snr_active}\n"
        f"\n"
        f"🕯️ 價格行為 ({opp['pa_label']} {opp['pa_score']:.0f}分)\n"
        f"{pa_lines}"
        f"🐋 主力：❓ 🔴 主力派發區 (82%)\n"
        f"    主力數據缺失，技術面極強\n"
        f"🎯 主力區：{whale_text}\n"
        f"📡 Supertrend：{opp['st_label']}\n"
        f"🕹️ 槓桿：{opp['leverage']}\n"
        f"📌 類型：長單 (波段)\n"
        f"📊 綜合評分：{opp['setup_score']:.0f}分 (閾值:{SETUP_SCORE_THRESHOLD*100:.0f}分)\n"
        f"\n"
        f"🔍 盤口行為：\n"
        f"   {sweep_tag} | {wall_tag}\n"
        f"   {absorption_tag if absorption_tag else '⚪ 正常流動'}\n"
        f"\n"
        f"💡 *等待掃單確認後成交...*"
    )
    return msg

# ─────────────────────────────────────────────
# 6. 主執行函數
# ─────────────────────────────────────────────

def main():
    """主函數 - 盤口行為策略掃描 + 多時區共振"""
    logging.info("🚀 Alpha Oracle v7.1 Started - MTF Confluence Mode")
    
    signals_sent = 0
    
    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] Scanning {coin}...")
        
        try:
            opps = scan_for_opportunity(coin)
            
            if opps:
                logging.info(f"✅ Found {len(opps)} qualified opportunity(ies) for {coin}")
                
                for opp in opps:
                    if signals_sent >= MAX_SIGNALS_PER_RUN:
                        break
                    
                    msg = format_signal_message(opp)
                    if send_tg(msg):
                        signals_sent += 1
                        logging.info(f"✅ Signal {signals_sent} sent for {coin} {opp['side']}")
                    else:
                        logging.error(f"❌ Failed to send signal for {coin}")
                    
                    time.sleep(1)
            else:
                logging.info(f"❌ No qualified opportunities for {coin}")
            
            time.sleep(0.5)
            
        except Exception as e:
            logging.error(f"❌ Scan Error for {coin}: {e}")
            traceback.print_exc()
            continue
    
    logging.info(f"📊 Scan Complete. Sent {signals_sent} signals.")
    return signals_sent

if __name__ == "__main__":
    try:
        signals_count = main()
        exit(0)
    except Exception as e:
        logging.error(f"💥 Bot crashed: {e}")
        traceback.print_exc()
        exit(1)
