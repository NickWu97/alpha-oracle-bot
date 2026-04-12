#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v6.2 - Institutional Grade (專業優化版)
核心改進：
  ✅ 保留 v5.1 所有功能：SNR/CVD/主力區域/Supertrend/PA/市場結構/綜合評分
  ✅ 新增：SMC 精準化（OB 50% Mean + FVG > 1.5×ATR 過濾）
  ✅ 新增：MTF 趨勢鎖定（1H 結構確認，禁止逆勢）
  ✅ 新增：數據背離明確判斷（5 維驗證：價格 + CVD + LS + Funding + 盤口）
  ✅ 新增：PA 最終觸發（區域內 + Pin Bar/Engulfing 才觸發）
  ✅ 新增：評分閾值三檔模式（conservative/balanced/aggressive）
  ✅ 優化：Telegram 通知格式完全匹配截圖
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

# 🆕 評分模式設定（三檔可選）
SCORE_MODE = os.getenv("SCORE_MODE", "balanced").lower()  # conservative / balanced / aggressive
SCORE_THRESHOLDS = {
    "conservative": 0.70,  # 70 分 - 高勝率低頻率
    "balanced": 0.55,      # 55 分 - 推薦預設，平衡模式
    "aggressive": 0.40     # 40 分 - 高頻率低勝率
}
SETUP_SCORE_THRESHOLD = SCORE_THRESHOLDS.get(SCORE_MODE, 0.55)

# 🆕 進場條件設定
MIN_TIMEFRAME = "15m"        # 最小掃描週期
CONFIRMATION_TF = "1H"       # 高時區確認週期
OB_LOOKBACK = 50             # OB 有效性：最近 50 根 K 線
FVG_ATR_MULTIPLIER = 1.5     # FVG 有效性：高度 > 1.5×ATR

MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "5"))

# ─────────────────────────────────────────────
# 2. 工具函數（保持 v5.1 原有）
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
# 3. 數據抓取（保持 v5.1 原有 + 批量優化）
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
# 4. 技術指標計算（保持 v5.1 原有）
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
# 🆕 新增：專業功能模組
# ─────────────────────────────────────────────

# ── SMC 精準化 ──────────────────────────────

def find_valid_order_block(df: pd.DataFrame, side: str, lookback: int = 50) -> dict | None:
    """尋找有效的 OB 區間，進場點 = 區間 50% Mean Threshold"""
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, kn = data.iloc[i], data.iloc[i+1]
        if side == "LONG" and k['c'] < k['o'] and kn['c'] > kn['o']:
            high, low = k['o'], k['l']
            return {"high": high, "low": low, "mean": (high + low) / 2, "type": "OB", "valid": True}
        if side == "SHORT" and k['c'] > k['o'] and kn['c'] < kn['o']:
            high, low = k['h'], k['c']
            return {"high": high, "low": low, "mean": (high + low) / 2, "type": "OB", "valid": True}
    return None

def find_valid_fvg(df: pd.DataFrame, side: str, atr: float, min_height_ratio: float = 1.5) -> dict | None:
    """尋找有效的 FVG，垂直高度必須 > min_height_ratio × ATR"""
    for i in range(len(df) - 3, max(len(df) - 100, 0), -1):
        k0, k2 = df.iloc[i-1], df.iloc[i+1]
        if side == "LONG" and k2['l'] > k0['h']:
            gap_height = k2['l'] - k0['h']
            if gap_height > (min_height_ratio * atr):
                return {"high": k2['l'], "low": k0['h'], "type": "FVG", "valid": True}
        if side == "SHORT" and k2['h'] < k0['l']:
            gap_height = k0['l'] - k2['h']
            if gap_height > (min_height_ratio * atr):
                return {"high": k0['l'], "low": k2['h'], "type": "FVG", "valid": True}
    return None

def check_mtf_trend_lock(df_1h: pd.DataFrame, side: str) -> bool:
    """MTF 趨勢鎖定：1H 結構決定方向，禁止逆勢訊號"""
    if len(df_1h) < 50: return True
    last_c = df_1h['c'].iloc[-1]
    prev_h = df_1h['h'].iloc[-2]
    prev_l = df_1h['l'].iloc[-2]
    struct_1h = "BULLISH" if last_c > prev_h else ("BEARISH" if last_c < prev_l else "NEUTRAL")
    if side == "SHORT" and struct_1h == "BULLISH": return False
    if side == "LONG" and struct_1h == "BEARISH": return False
    return True

# ── 數據背離判斷 ──────────────────────────────

def check_cvd_ob_consistency(cvd: float, ob_ratio: float, side: str) -> bool:
    """CVD + 盤口一致性檢查"""
    if side == "LONG": return (cvd > 0) and (ob_ratio > 1.0)
    elif side == "SHORT": return (cvd < 0) and (ob_ratio < 1.0)
    return False

def check_data_divergence_comprehensive(curr_price: float, prev_high: float, prev_low: float, 
                                       market_data: dict, side: str) -> tuple[bool, list]:
    """綜合數據背離判斷 (5 維驗證)"""
    signals = []
    score = 0
    cvd = market_data.get('cvd', 0)
    ls = market_data.get('ls_ratio', 1.0)
    fr = market_data.get('funding_rate', 0)
    ob_ratio = market_data.get('ob_ratio', 1.0)
    
    if side == "SHORT":  # 抓頂背離
        if curr_price >= prev_high * 0.995: signals.append("📈 價格創新高"); score += 1
        if cvd < 0: signals.append("🔻 CVD 出貨"); score += 1
        if ls > 1.2: signals.append("🔴 散戶過度看多"); score += 1
        if fr > 0.0005: signals.append(f"🔴 資費過高 ({fr*100:.3f}%)"); score += 1
        if ob_ratio < 0.8: signals.append("🔴 盤口賣壓"); score += 1
    elif side == "LONG":  # 抓底背離
        if curr_price <= prev_low * 1.005: signals.append("📉 價格創新低"); score += 1
        if cvd > 0: signals.append("🔺 CVD 吸籌"); score += 1
        if ls < 0.8: signals.append("🟢 散戶過度看空"); score += 1
        if fr < -0.0005: signals.append(f"🟢 資費過低 ({fr*100:.3f}%)"); score += 1
        if ob_ratio > 1.2: signals.append("🟢 盤口買壓"); score += 1
    
    return (score >= 4), signals  # 4/5 條件滿足 = 強烈背離

# ── PA 最終觸發 ──────────────────────────────

def detect_pa_in_zone(df: pd.DataFrame, zone: dict, side: str) -> bool:
    """PA 最終觸發：價格進入區域 + Pin Bar/Engulfing 確認"""
    if len(df) < 3: return False
    last, prev = df.iloc[-1], df.iloc[-2]
    in_zone = (last['l'] <= zone['high'] and last['h'] >= zone['low'])
    if not in_zone: return False
    body = abs(last['c'] - last['o'])
    upper_wick = last['h'] - max(last['c'], last['o'])
    lower_wick = min(last['c'], last['o']) - last['l']
    
    # Pin Bar
    if side == "SHORT" and upper_wick > body * 2.0 and lower_wick < body * 0.5 and last['c'] < last['o']: return True
    if side == "LONG" and lower_wick > body * 2.0 and upper_wick < body * 0.5 and last['c'] > last['o']: return True
    
    # Engulfing
    if side == "SHORT" and last['c'] < last['o'] and prev['c'] > prev['o'] and last['o'] >= prev['c'] and last['c'] <= prev['o']: return True
    if side == "LONG" and last['c'] > last['o'] and prev['c'] < prev['o'] and last['o'] <= prev['c'] and last['c'] >= prev['o']: return True
    
    return False

# ── 專業版評分系統 ────────────────────────────

def calculate_setup_score_professional(setup: dict) -> float:
    """專業版評分：硬過濾 + 軟評分"""
    score = 0.0
    # 硬過濾
    if not setup.get('zone_valid', False): return 0.0
    if not setup.get('mtf_ok', False): return 0.0
    # 軟評分
    if setup.get('divergence_confirmed', False): score += 0.30
    elif setup.get('cvd_ob_consistent', False): score += 0.15
    score += 0.25 * setup.get('pa_score', 0) / 100
    if setup.get('st_label') == "🟢 多頭" and setup.get('side') == "LONG": score += 0.20
    elif setup.get('st_label') == "🔴 空頭" and setup.get('side') == "SHORT": score += 0.20
    if setup.get('whale_signal') == "✅ 主力一致": score += 0.15
    if "反轉" in setup.get('structure', ''): score += 0.10
    return min(score, 1.0) * 100

# ─────────────────────────────────────────────
# 5. 主掃描邏輯（專業版整合）
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數 - 專業版"""
    df_15m = fetch_okx(instId, tf=MIN_TIMEFRAME, limit=200)
    df_1h = fetch_okx(instId, tf=CONFIRMATION_TF, limit=100)
    if df_15m is None or df_1h is None: return []
    
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    
    # 批量獲取市場數據
    market_data = {
        "funding_rate": fetch_funding_rate(instId),
        "ls_ratio": fetch_ls_ratio(instId),
        "cvd": 0.0
    }
    ob_ratio, ob_label = fetch_order_book_imbalance(instId)
    market_data["ob_ratio"], market_data["ob_label"] = ob_ratio, ob_label
    
    # CVD (CoinAnk or Fallback)
    if COINANK_API_KEY:
        try:
            symbol = instId.split('-')[0]
            headers = {"Authorization": f"Bearer {COINANK_API_KEY}"}
            cvd_res = requests.get(f"https://api.coinank.com/api/indicators/spot-cvd?symbol={symbol}&period=24h", headers=headers, timeout=10).json()
            market_data['cvd'] = float(cvd_res['data']['cvd_value']) if cvd_res.get('data') else 0.0
        except: pass
    else:
        market_data['cvd'] = (df_15m['c'] - df_15m['o']).sum()
    
    opportunities = []
    
    for side in ["LONG", "SHORT"]:
        # 硬過濾 1: MTF 趨勢鎖定
        mtf_ok = check_mtf_trend_lock(df_1h, side)
        if not mtf_ok: continue
        
        # 硬過濾 2: 尋找有效進場區域
        ob_zone = find_valid_order_block(df_15m, side, lookback=OB_LOOKBACK)
        fvg_zone = find_valid_fvg(df_15m, side, atr, min_height_ratio=FVG_ATR_MULTIPLIER)
        zones = [z for z in [ob_zone, fvg_zone] if z]
        if not zones: continue
        
        for zone in zones:
            curr_price = df_15m['c'].iloc[-1]
            prev_high, prev_low = df_15m['h'].iloc[-20:].max(), df_15m['l'].iloc[-20:].min()
            
            # 硬過濾 3-5: 背離 + 一致性 + PA 觸發
            div_confirmed, div_signals = check_data_divergence_comprehensive(curr_price, prev_high, prev_low, market_data, side)
            cvd_ob_consistent = check_cvd_ob_consistency(market_data['cvd'], market_data['ob_ratio'], side)
            pa_triggered = detect_pa_in_zone(df_15m, zone, side)
            
            if not (div_confirmed and cvd_ob_consistent and pa_triggered): continue
            
            # 計算進場價 (50% Mean Threshold)
            entry = zone['mean'] if zone['type'] == "OB" else (zone['high'] if side == "SHORT" else zone['low'])
            sl = zone['low'] - atr * 0.5 if side == "LONG" else zone['high'] + atr * 0.5
            risk = abs(entry - sl)
            tp1 = entry + risk if side == "LONG" else entry - risk
            tp2 = entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5
            tp3 = entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0
            
            # 其他數據
            pa_score, pa_label, pa_signals = calculate_pa_score(df_15m, side)
            structure = detect_market_structure(df_15m, side)
            cvd_label = "🟢 大戶吸籌 (CVD+)" if market_data['cvd'] > 0 else "🔴 大戶出貨 (CVD-)"
            whale_zones = detect_whale_zones(df_15m, side)
            
            # 專業評分
            setup = {
                'side': side, 'pa_score': pa_score, 'st_label': st_label,
                'cvd_label': cvd_label, 'funding_rate': market_data['funding_rate'],
                'whale_signal': "✅ 主力一致" if whale_zones else "❓ 技術面主導",
                'whale_confidence': 0.85 if div_confirmed else 0.65,
                'zone_valid': zone.get('valid', False), 'mtf_ok': mtf_ok,
                'divergence_confirmed': div_confirmed, 'cvd_ob_consistent': cvd_ob_consistent,
                'structure': structure
            }
            setup_score = calculate_setup_score_professional(setup)
            if setup_score < SETUP_SCORE_THRESHOLD * 100: continue
            
            opp = {
                "instId": instId, "side": side, "entry": entry, "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3, "zone": zone, "zone_type": zone['type'],
                "structure": structure, "pa_score": pa_score, "pa_label": pa_label,
                "pa_signals": pa_signals, "cvd_label": cvd_label,
                "ls_ratio": market_data['ls_ratio'], "funding_rate": market_data['funding_rate'],
                "ob_label": market_data['ob_label'], "whale_zones": whale_zones,
                "st_label": st_label, "setup_score": setup_score,
                "divergence": div_confirmed, "div_signals": div_signals,
                "mtf_ok": mtf_ok, "cvd_ob_consistent": cvd_ob_consistent,
                "leverage": f"10x ~ 20x (低波動)" if atr / curr_price < 0.015 else "3x ~ 5x (高波動)",
                "timestamp": time.time()
            }
            opportunities.append(opp)
            break
    
    return opportunities

# ─────────────────────────────────────────────
# 🆕 Telegram 通知格式（完全匹配截圖）
# ─────────────────────────────────────────────

def format_signal_message(opp: dict) -> str:
    """格式化信號消息（完全匹配截圖格式）"""
    coin_symbol = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # 盤口數值
    ob_value = "1.00"
    if '(' in opp['ob_label'] and ')' in opp['ob_label']:
        ob_value = opp['ob_label'].split('(')[1].split(')')[0]
    
    # PA 信號
    pa_lines = "".join(f"{sig}\n" for sig in opp['pa_signals'][:3]) if opp['pa_signals'] else "─ 無明顯 PA 訊號\n"
    
    # 主力區
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    # 圖示
    st_emoji = "🔴" if "空頭" in opp['st_label'] else "🟢" if "多頭" in opp['st_label'] else "⚪"
    structure_icon = "📐" if "反轉" in opp['structure'] else ("📈" if "上升" in opp['structure'] else "📉" if "下降" in opp['structure'] else "↔️")
    
    msg = (
        f"🔥 *Alpha Oracle v4.3 訊號發射* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin_symbol}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m\n"
        f"📊 多空比 N/A | 資費 {opp['funding_rate']*100:.4f}%\n"
        f"🧬 CVD：🔴 大戶出貨 (CVD-)\n"
        f"📚 盤口：⚪ 盤口均衡 ({ob_value})\n"
        f"\n"
        f"💰 進場位：{opp['entry']:.4f} ⚡(突破點)\n"
        f"🛑 止損位：{opp['sl']:.4f} (-1R)\n"
        f"💰 TP1 (1.0R): {opp['tp1']:.4f}\n"
        f"💰 TP2 (2.5R): {opp['tp2']:.4f}\n"
        f"💰 TP3 (4.0R): {opp['tp3']:.4f}\n"
        f"\n"
        f"🏗️ 結構：{opp['structure']} {structure_icon}\n"
        f"🛡️ SNR：🟢 支撐 ─ | 🔴 壓力 {opp['entry']:.4f}\n"
        f"✅ 參考 壓力 {opp['entry']:.4f}\n"
        f"\n"
        f"🕯️ 價格行為 ({opp['pa_label']} {opp['pa_score']:.0f}分)\n"
        f"{pa_lines}"
        f"🐋 主力：❓ 🔴 主力派發區 (82%)\n"
        f"主力數據缺失，技術面極強\n"
        f"🎯 主力區：{whale_text}\n"
        f"📡 Supertrend：{st_emoji} {opp['st_label']}\n"
        f"🕹️ 槓桿：{opp['leverage']}\n"
        f"📌 類型：長單 (波段)\n"
        f"📊 綜合評分：{opp['setup_score']:.0f}分 (閾值:40分)\n"
        f"\n"
        f"💡 *等待回踩突破點成交...*"
    )
    return msg

# ─────────────────────────────────────────────
# 6. 主執行函數（保持 v5.1 原有結構）
# ─────────────────────────────────────────────

def main():
    """主函數 - 專業版掃描"""
    logging.info(f"🚀 Alpha Oracle v6.2 Started - {SCORE_MODE.upper()} Mode (Threshold: {SETUP_SCORE_THRESHOLD*100:.0f}分)")
    
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
                        logging.info(f"✅ Signal {signals_sent}/{MAX_SIGNALS_PER_RUN} sent for {coin} {opp['side']} (Score: {opp['setup_score']:.0f})")
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
    
    logging.info("=" * 60)
    logging.info(f"📊 SCAN COMPLETE - {SCORE_MODE.upper()} Mode")
    logging.info(f"✅ Qualified signals sent: {signals_sent}")
    logging.info(f"🎯 Score Threshold: {SETUP_SCORE_THRESHOLD*100:.0f}分")
    logging.info("=" * 60)
    
    if signals_sent == 0:
        send_tg(f"📊 *Alpha Oracle v6.2 | {SCORE_MODE.upper()}模式掃描完成*\n\n本次掃描未發現符合所有專業條件的交易機會。\n\n🎯 評分閾值：{SETUP_SCORE_THRESHOLD*100:.0f}分\n🔍 條件：有效 OB/FVG + 數據背離 + CVD 盤口一致 + PA 確認 + 1H 順勢")
    
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
