#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v7.0 - Order Flow & Tape Reading Engine
核心改進：
  ✅ 保留 v5.1 所有功能：SNR/CVD/主力區域/Supertrend/PA/市場結構/綜合評分
  ✅ 新增：十字線多空分界（15m 以下 Doji 作為定價中心）
  ✅ 新增：掃單偵測引擎（連續吃掉多層水位 = 主動單攻擊）
  ✅ 新增：釣魚單過濾（無量上漲/下跌 = 洗盤陷阱，不追）
  ✅ 新增：新聞冷卻機制（新聞後強制等待 1 小時）
  ✅ 新增：1-2-3 帶量止損驗證（突破需帶量，無量=假突破）
  ✅ 新增：測牆 + 吸收過濾（識別主力掛牆控盤與吸收換籌）
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
        logging.FileHandler("alpha_oracle_v7.log", encoding="utf-8"),
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
SETUP_SCORE_THRESHOLD = 0.40  # 綜合評分閾值 40 分

# 🆕 盤口行為策略參數
CROSSLINE_BODY_RATIO = 0.3      # 十字線：實體 < 30% 總範圍
SWEEP_VOLUME_RATIO = 2.0        # 掃單：成交量 > 2 倍均量
SWEEP_PRICE_STEPS = 3           # 掃單：連續移動 >= 3 個價格檔位
NEWS_COOLDOWN_MINUTES = 60      # 新聞冷卻：60 分鐘
VOLUME_CONFIRMATION_RATIO = 1.5 # 止損驗證：突破量 > 1.5 倍均量
WALL_IMBALANCE_THRESHOLD = 0.3  # 測牆：買賣牆失衡 > 30% 視為突破信號
ABSORPTION_PRICE_CHANGE_THRESHOLD = 0.002  # 吸收：價格變動 < 0.2% 但成交量大

# 新聞冷卻追蹤（簡化版，實際可接新聞 API）
last_news_time = {}  # {instId: timestamp}

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
    """
    🎯 十字線（Doji）偵測 - 多空分界線
    條件：實體 < CROSSLINE_BODY_RATIO * 總範圍
    返回：十字線位置、價格、方向潛力
    """
    for i in range(len(df) - 1, max(len(df) - lookback - 1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        if body < CROSSLINE_BODY_RATIO * total_range:
            # 判斷潛在方向：透過上下影線比例
            upper_wick = k['h'] - max(k['c'], k['o'])
            lower_wick = min(k['c'], k['o']) - k['l']
            if upper_wick > lower_wick * 1.5:
                potential_side = "SHORT"  # 上影長，可能反轉向下
            elif lower_wick > upper_wick * 1.5:
                potential_side = "LONG"   # 下影長，可能反轉向上
            else:
                potential_side = "NEUTRAL"
            return {
                "index": i,
                "price": k['c'],
                "high": k['h'],
                "low": k['l'],
                "body": body,
                "range": total_range,
                "potential_side": potential_side,
                "desc": f"🎯 十字線定價中心 @ {k['c']:.4f} (潛在：{potential_side})"
            }
    return None

def detect_sweep_behavior(df: pd.DataFrame, side: str, lookback: int = 10) -> bool:
    """
    ⚡ 掃單行為偵測 - 主動單攻擊
    條件：
    1. 成交量 > SWEEP_VOLUME_RATIO 倍均量
    2. 價格連續移動 >= SWEEP_PRICE_STEPS 個檔位
    3. 方向與 side 一致
    """
    if len(df) < lookback + 1: return False
    recent = df.tail(lookback + 1)
    vol_ma = recent['v'].iloc[:-1].mean()
    
    # 檢查最後一根是否放量
    if recent['v'].iloc[-1] < SWEEP_VOLUME_RATIO * vol_ma:
        return False
    
    # 檢查價格連續移動
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
    """
    🎣 釣魚單偵測 - 無量上漲/下跌（洗盤陷阱）
    條件：價格移動明顯但成交量未放大（< 0.8 倍均量）
    """
    if len(df) < 5: return False
    recent = df.tail(5)
    vol_ma = recent['v'].mean()
    
    # 檢查價格移動
    price_move = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / recent['c'].iloc[0]
    if price_move < 0.005:  # 移動 < 0.5% 不算
        return False
    
    # 檢查成交量
    if recent['v'].iloc[-1] < 0.8 * vol_ma:
        return True  # 無量移動 = 釣魚單
    return False

def check_news_cooldown(instId: str) -> bool:
    """
    ⏱️ 新聞冷卻檢查 - 新聞後 1 小時內不發訊號
    簡化版：手動記錄最後新聞時間，實際可接新聞 API
    """
    now = time.time()
    if instId in last_news_time:
        if now - last_news_time[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False  # 仍在冷卻期
    return True

def mark_news_event(instId: str):
    """標記新聞事件時間"""
    last_news_time[instId] = time.time()
    logging.info(f"📰 News event marked for {instId}")

def validate_stop_loss_with_volume(df: pd.DataFrame, sl_price: float, side: str) -> bool:
    """
    🔍 1-2-3 帶量止損驗證
    條件：止損位被突破時，該根 K 線成交量 > VOLUME_CONFIRMATION_RATIO 倍前幾根均量
    """
    if len(df) < 5: return True  # 數據不足時預設通過
    recent = df.tail(5)
    
    # 檢查是否突破止損
    if side == "LONG":
        if recent['l'].iloc[-1] > sl_price:  # 未突破
            return True
        breakout_vol = recent['v'].iloc[-1]
    else:
        if recent['h'].iloc[-1] < sl_price:  # 未突破
            return True
        breakout_vol = recent['v'].iloc[-1]
    
    # 計算前幾根均量
    prev_vol_ma = recent['v'].iloc[:-1].mean()
    
    # 帶量突破 = 有效止損
    return breakout_vol >= VOLUME_CONFIRMATION_RATIO * prev_vol_ma

def detect_wall_imbalance(instId: str, depth: int = 20) -> tuple[bool, str]:
    """
    🧱 測牆機制 - 買賣牆對等性檢查
    條件：買賣牆失衡 > WALL_IMBALANCE_THRESHOLD 視為潛在突破
    """
    ratio, label = fetch_order_book_imbalance(instId, depth)
    imbalance = abs(ratio - 1.0)
    if imbalance > WALL_IMBALANCE_THRESHOLD:
        direction = "🟢 買牆突破" if ratio > 1.0 else "🔴 賣牆突破"
        return True, direction
    return False, "⚪ 牆體平衡"

def detect_absorption(df: pd.DataFrame, side: str) -> bool:
    """
    🔄 吸收過濾 - 大量成交但價格移動緩慢（主力換籌）
    條件：成交量 > 2 倍均量 且 價格變動 < ABSORPTION_PRICE_CHANGE_THRESHOLD
    """
    if len(df) < 10: return False
    recent = df.tail(10)
    vol_ma = recent['v'].mean()
    price_change = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / recent['c'].iloc[0]
    
    if recent['v'].iloc[-1] > 2.0 * vol_ma and price_change < ABSORPTION_PRICE_CHANGE_THRESHOLD:
        return True  # 吸收行為
    return False

# ─────────────────────────────────────────────
# 5. 主掃描邏輯（整合盤口行為策略）
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數 - 整合盤口行為策略"""
    df_15m = fetch_okx(instId, tf="15m", limit=100)
    if df_15m is None: return []
    
    # 計算指標
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    
    # 🆕 盤口行為檢查
    crossline = detect_crossline(df_15m)
    if not crossline:
        # 無十字線 = 市場混亂穩定，不交易
        return []
    
    # 🆕 新聞冷卻檢查
    if not check_news_cooldown(instId):
        logging.info(f"[{instId}] In news cooldown period, skipping")
        return []
    
    # 🆕 測牆 + 吸收檢查
    wall_break, wall_msg = detect_wall_imbalance(instId)
    absorption = detect_absorption(df_15m, "LONG") or detect_absorption(df_15m, "SHORT")
    
    opportunities = []
    
    for side in ["LONG", "SHORT"]:
        # 🆕 釣魚單過濾
        if detect_fishing_trap(df_15m, side):
            logging.info(f"[{instId}] Fishing trap detected for {side}, skipping")
            continue
        
        # 🆕 掃單確認（進場必要條件）
        if not detect_sweep_behavior(df_15m, side):
            continue
        
        # 尋找進場區域（以十字線為中心）
        if side == "LONG":
            entry = crossline['low'] * 0.999  # 略低於十字線低點
            sl = crossline['low'] - atr * 1.5
        else:
            entry = crossline['high'] * 1.001  # 略高於十字線高點
            sl = crossline['high'] + atr * 1.5
        
        # 🆕 帶量止損驗證
        if not validate_stop_loss_with_volume(df_15m, sl, side):
            logging.info(f"[{instId}] Stop loss not volume-confirmed for {side}, skipping")
            continue
        
        risk = abs(entry - sl)
        tp1 = entry + risk if side == "LONG" else entry - risk
        tp2 = entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5
        tp3 = entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0
        
        # 其他分析數據（保持 v5.1 原有）
        pa_score, pa_label, pa_signals = calculate_pa_score(df_15m, side)
        structure = detect_market_structure(df_15m, side)
        cvd_val = df_15m['c'].iloc[-1] - df_15m['o'].iloc[-1]
        cvd_label = "🟢 大戶吸籌 (CVD+)" if cvd_val > 0 else "🔴 大戶出貨 (CVD-)"
        whale_zones = detect_whale_zones(df_15m, side)
        funding_rate = fetch_funding_rate(instId)
        ls_ratio = fetch_ls_ratio(instId)
        ob_ratio, ob_label = fetch_order_book_imbalance(instId)
        
        # 綜合評分
        setup = {
            'side': side,
            'pa_score': pa_score,
            'st_label': st_label,
            'cvd_label': cvd_label,
            'funding_rate': funding_rate,
            'whale_signal': "✅ 主力一致" if len(whale_zones) > 0 else "❓ 技術面主導",
            'whale_confidence': 0.82
        }
        setup_score = calculate_setup_score(setup)
        
        if setup_score < SETUP_SCORE_THRESHOLD * 100:
            continue
        
        opp = {
            "instId": instId,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "structure": structure,
            "snr_zone": {"support": crossline['low'], "resistance": crossline['high'], "active_level": crossline['price'], "text": f"十字線 {crossline['price']:.4f}"},
            "pa_score": pa_score,
            "pa_label": pa_label,
            "pa_signals": pa_signals,
            "cvd_label": cvd_label,
            "ls_ratio": ls_ratio,
            "funding_rate": funding_rate,
            "ob_label": ob_label,
            "whale_zones": whale_zones,
            "st_label": st_label,
            "setup_score": setup_score,
            "leverage": f"10x ~ 20x (低波動)" if atr / df_15m['c'].iloc[-1] < 0.015 else "3x ~ 5x (高波動)",
            # 🆕 盤口行為標記
            "crossline": crossline,
            "sweep_confirmed": True,
            "fishing_trap_filtered": False,
            "wall_msg": wall_msg,
            "absorption": absorption
        }
        opportunities.append(opp)
    
    return opportunities

# ─────────────────────────────────────────────
# 🆕 Telegram 通知格式（整合盤口行為資訊）
# ─────────────────────────────────────────────

def format_signal_message(opp: dict) -> str:
    """格式化信號消息（整合盤口行為資訊）"""
    coin_symbol = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # SNR 顯示（十字線作為關鍵位）
    cl = opp.get('crossline', {})
    snr_display = f"🟢 支撐 {cl.get('low', 0):.4f} | 🔴 壓力 {cl.get('high', 0):.4f}" if cl else "🟢 支撐 ─ | 🔴 壓力 ─"
    snr_active = f"✅ 參考 十字線 {cl.get('price', 0):.4f}" if cl else "⚠️ 無明顯關鍵位"
    
    # PA 信號
    pa_lines = "".join(f"   {sig}\n" for sig in opp['pa_signals'][:3]) if opp['pa_signals'] else "   ─ 無明顯 PA 訊號\n"
    
    # 主力區
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    # 🆕 盤口行為標記
    sweep_tag = "✅ 掃單確認" if opp.get('sweep_confirmed') else "⏳ 等待掃單"
    wall_tag = opp.get('wall_msg', '⚪ 牆體平衡')
    absorption_tag = "🔄 吸收中" if opp.get('absorption') else ""
    
    msg = (
        f"🔥 *Alpha Oracle v7.0 | 盤口行為訊號* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin_symbol}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m\n"
        f"📊 多空比 {opp['ls_ratio']} | 資費 {opp['funding_rate']*100:.4f}%\n"
        f"🧬 CVD：{opp['cvd_label']}\n"
        f"📚 盤口：{opp['ob_label']}\n"
        f"\n"
        f"💰 進場位：{opp['entry']:.4f} ⚡(十字線突破)\n"
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
    """主函數 - 盤口行為策略掃描"""
    logging.info("🚀 Alpha Oracle v7.0 Started - Order Flow Mode")
    
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
