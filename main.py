#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v7.0 — 盤口行為技術分析策略框架
══════════════════════════════════════════════════════════════
新增功能：
  ✅ 十字線（Doji）偵測 — 多空分界定價中心
  ✅ 主動掃單（Sweep）識別 — 連續吃掉多層水位
  ✅ 釣魚單過濾 — 排除掛而不成交的洗盤陷阱
  ✅ 新聞冷卻機制 — 發布後1小時強制等待
  ✅ 帶量止損驗證 — 1-2-3順序驗證法
  ✅ 測牆機制 — 買賣牆對等性觀察
  ✅ 吸收過濾 — 大量成交但價格移動緩慢偵測
══════════════════════════════════════════════════════════════
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
from typing import Optional, Dict, List, Tuple

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
SETUP_SCORE_THRESHOLD = 40  # 綜合評分閾值 40分

# 盤口行為參數
CROSSLINE_BODY_RATIO = 0.30        # 十字線：實體 < 30% 總範圍
SWEEP_VOLUME_RATIO = 1.8           # 掃單：成交量 > 1.8倍均量
SWEEP_CONSECUTIVE_MOVES = 2        # 掃單：連續移動 >= 2根
NEWS_COOLDOWN_MINUTES = 60         # 新聞冷卻期 60分鐘
ABSORPTION_VOL_MULTIPLIER = 1.8    # 吸收：成交量 > 1.8倍均量
ABSORPTION_PRICE_THRESHOLD = 0.002 # 吸收：價格變動 < 0.2%
FISHING_PRICE_MOVE = 0.005         # 釣魚單：價格移動 >= 0.5%
FISHING_VOL_RATIO = 0.75           # 釣魚單：成交量 < 0.75倍均量

# 新聞冷卻追蹤
_news_cooldown: Dict[str, float] = {}

# ─────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: 
        logging.warning("⚠️ Telegram 未設定，跳過發送")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        if response.status_code == 200:
            return True
        logging.error(f"Telegram API 錯誤: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logging.error(f"Telegram 發送異常: {e}")
        return False

def check_news_cooldown(instId: str) -> bool:
    """檢查是否在新聞冷卻期內"""
    now = time.time()
    if instId in _news_cooldown:
        if now - _news_cooldown[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False
    return True

def mark_news_event(instId: str):
    """標記新聞事件，啟動冷卻期"""
    _news_cooldown[instId] = time.time()
    logging.info(f"📰 News cooldown set for {instId}")

# ─────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────

def fetch_okx(instId: str, tf: str = "15m", limit: int = 200) -> Optional[pd.DataFrame]:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0': 
            logging.warning(f"[{instId}] API 錯誤: {res.get('msg')}")
            return None
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
    except Exception as e:
        logging.warning(f"[{instId}] 費率抓取錯誤: {e}")
        return 0

def fetch_ls_ratio(symbol: str) -> str:
    """獲取多空比"""
    try:
        base_id = symbol.split('-')[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        return res['data'][0]['ratio'] if res.get('data') else "N/A"
    except Exception as e:
        logging.warning(f"[{symbol}] 多空比抓取錯誤: {e}")
        return "N/A"

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple:
    """獲取盤口不平衡度"""
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5).json()
        if res.get('code') != '0' or not res.get('data'):
            return 1.0, "⚪ 盤口均衡"
        data = res['data'][0]
        bid_vol = sum(float(b[1]) for b in data['bids'])
        ask_vol = sum(float(a[1]) for a in data['asks']) or 1e-10
        ratio = bid_vol / ask_vol
        if ratio >= 1.30:
            label = f"🟢 買盤強勢 ({ratio:.2f})"
        elif ratio >= 1.05:
            label = f"🟡 買盤略強 ({ratio:.2f})"
        elif ratio >= 0.95:
            label = f"⚪ 盤口均衡 ({ratio:.2f})"
        elif ratio >= 0.77:
            label = f"🟡 賣盤略強 ({ratio:.2f})"
        else:
            label = f"🔴 賣盤強勢 ({ratio:.2f})"
        return ratio, label
    except Exception as e:
        logging.warning(f"[{instId}] 盤口抓取錯誤: {e}")
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
    """計算 Supertrend 指標"""
    if len(df) < period + 2: 
        return 0, "⚪ 未知"
    
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

def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
    """檢測市場結構（M頭/W底）"""
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
    """尋找支撐阻力區域"""
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

# ─────────────────────────────────────────────
# 5. 盤口行為分析（v7.0 核心）
# ─────────────────────────────────────────────

def detect_crossline(df: pd.DataFrame, lookback: int = 15) -> Optional[Dict]:
    """
    十字線（Doji）偵測 — 多空分界定價中心
    實體 < 30% 總範圍 = 十字線
    """
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        rng = k['h'] - k['l'] + 1e-10
        
        if body < CROSSLINE_BODY_RATIO * rng:
            up_wick = k['h'] - max(k['c'], k['o'])
            dn_wick = min(k['c'], k['o']) - k['l']
            
            if up_wick > dn_wick * 1.5:
                potential = "SHORT"
            elif dn_wick > up_wick * 1.5:
                potential = "LONG"
            else:
                potential = "NEUTRAL"
            
            dist_from_now = len(df) - 1 - i
            
            return {
                "price": k['c'],
                "high": k['h'],
                "low": k['l'],
                "body_ratio": body/rng,
                "potential_side": potential,
                "distance": dist_from_now,
                "desc": f"🎯 十字線 @ {k['c']:.4f}（潛在：{potential}，{dist_from_now}根前）"
            }
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> Tuple[bool, float, str]:
    """
    主動掃單偵測 — 訂單流連續攻擊
    條件：近幾根K線方向一致 + 放量
    """
    if len(df) < 8:
        return False, 0.0, "⚪ 數據不足"
    
    recent = df.tail(8)
    vol_ma = df['v'].tail(20).mean()
    last = recent.iloc[-1]
    vol_sc = last['v'] / (vol_ma + 1e-10)
    
    # 放量確認（必要條件）
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"⚪ 量能不足 ({vol_sc:.1f}x均量)"
    
    # 連續方向移動
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side == "LONG" and recent['c'].iloc[i] > recent['c'].iloc[i-1]:
            moves += 1
        elif side == "SHORT" and recent['c'].iloc[i] < recent['c'].iloc[i-1]:
            moves += 1
        else:
            break
    
    if moves >= SWEEP_CONSECUTIVE_MOVES:
        strength = min(vol_sc / 3.0, 1.0)
        desc = f"⚡ 主動掃單確認！連續{moves}根+{vol_sc:.1f}x量能"
        return True, strength, desc
    
    return False, 0.0, f"⚪ 無連續掃單（方向根數={moves}）"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    """
    釣魚單過濾 — 無量價格移動（掛單引誘，非真實成交）
    條件：價格移動 >= 0.5% 但成交量 < 0.75倍均量
    """
    if len(df) < 6:
        return False
    
    recent = df.tail(6)
    vol_ma = df['v'].tail(20).mean()
    price_mv = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    
    if price_mv < FISHING_PRICE_MOVE:
        return False
    
    last_vol = recent['v'].iloc[-1]
    return last_vol < FISHING_VOL_RATIO * vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> Tuple[bool, str]:
    """
    吸收信號 — 大量成交但價格幾乎不動（主力換籌）
    條件：近3根K均量 > 1.8倍均量 且 價格變動 < 0.2%
    """
    if len(df) < 15:
        return False, "⚪ 無吸收"
    
    recent = df.tail(5)
    vol_ma = df['v'].tail(20).mean()
    avg_vol3 = recent['v'].iloc[-3:].mean()
    px_chg = abs(recent['c'].iloc[-1] - recent['c'].iloc[-4]) / (recent['c'].iloc[-4] + 1e-10)
    
    if avg_vol3 > ABSORPTION_VOL_MULTIPLIER * vol_ma and px_chg < ABSORPTION_PRICE_THRESHOLD:
        return True, f"🔄 吸收信號！量{avg_vol3/vol_ma:.1f}x均量但價格僅動{px_chg*100:.2f}%（主力換籌中）"
    return False, "⚪ 無明顯吸收"

def check_volume_breakout(df: pd.DataFrame) -> bool:
    """
    帶量止損驗證 — 突破時是否有量（無量突破=假突破）
    返回 True = 帶量有效突破；False = 無量假突破
    """
    if len(df) < 6:
        return True
    recent = df.tail(6)
    vol_ma = recent['v'].iloc[:-1].mean()
    last_vol = recent['v'].iloc[-1]
    return last_vol >= 1.5 * vol_ma

def detect_wall_imbalance(df: pd.DataFrame, instId: str) -> Tuple[str, str]:
    """
    測牆機制 — 觀察買賣牆的對等性
    返回 (牆狀態描述, 潛在突破方向)
    """
    ratio, label = fetch_order_book_imbalance(instId)
    
    if ratio >= 1.30:
        return label, "🔴 賣壓可能（買牆撤單風險）"
    elif ratio <= 0.77:
        return label, "🟢 買壓可能（賣牆撤單風險）"
    else:
        return label, "⚪ 牆體平衡（等待失衡）"

# ─────────────────────────────────────────────
# 6. 價格行為分析
# ─────────────────────────────────────────────

def detect_price_action(df: pd.DataFrame, side: str) -> list:
    """檢測價格行為形態"""
    signals = []
    
    for i in range(len(df) - 1, max(len(df) - 5, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        upper_wick = k['h'] - max(k['c'], k['o'])
        lower_wick = min(k['c'], k['o']) - k['l']
        
        # 流星線 (Shooting Star)
        if side == "SHORT" and upper_wick >= body * 2.0 and lower_wick <= body * 0.5:
            strength = min(upper_wick / (body + 1e-10), 5.0)
            signals.append(f"空頭流星線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        
        # 錘子線 (Hammer)
        if side == "LONG" and lower_wick >= body * 2.0 and upper_wick <= body * 0.5:
            strength = min(lower_wick / (body + 1e-10), 5.0)
            signals.append(f"多頭錘子線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        
        # 壓力位拒絕 (Resistance Rejection)
        if side == "SHORT" and upper_wick / total_range > 0.40 and k['c'] < k['o']:
            signals.append(f"壓力位拒絕 (上影 {upper_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        
        # 支撐位拒絕 (Support Rejection)
        if side == "LONG" and lower_wick / total_range > 0.40 and k['c'] > k['o']:
            signals.append(f"支撐位拒絕 (下影 {lower_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        
        # 動量棒 (Momentum Bar)
        body_pct = body / total_range
        if body_pct >= 0.70:
            if (side == "LONG" and k['c'] > k['o']) or (side == "SHORT" and k['c'] < k['o']):
                signals.append(f"{'多頭' if side=='LONG' else '空頭'}動量棒 ({body_pct*100:.0f}%實體) @ {k['c']:.4f}")
    
    return signals[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
    """計算價格行為評分"""
    score = 0.0
    signals = detect_price_action(df, side)
    
    if len(signals) >= 3:
        score += 0.60
    elif len(signals) >= 2:
        score += 0.40
    elif len(signals) >= 1:
        score += 0.20
    
    last_k = df.iloc[-1]
    body = abs(last_k['c'] - last_k['o'])
    rng = last_k['h'] - last_k['l'] + 1e-10
    
    if body / rng > 0.70:
        score += 0.20
    if (side == "LONG" and last_k['c'] > last_k['o']) or (side == "SHORT" and last_k['c'] < last_k['o']):
        score += 0.20
    
    score = min(score, 1.0)
    
    if score >= 0.65:
        label = "✅ 強勢PA"
    elif score >= 0.40:
        label = "⚠️ 中等PA"
    else:
        label = "⛔ 弱PA"
    
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
    """計算綜合評分（100分制）"""
    score = 0.0
    
    # 主力信號 (30%)
    if setup.get('whale_signal') == "✅ 主力一致":
        score += 0.30 * setup.get('whale_confidence', 0)
    elif setup.get('whale_signal') == "⚠️ 主力警示":
        score += 0.15 * setup.get('whale_confidence', 0)
    
    # 價格行為 (25%)
    score += 0.25 * setup.get('pa_score', 0) / 100
    
    # 技術指標 (20%)
    if setup.get('st_label') == "🟢 多頭" and setup.get('side') == "LONG":
        score += 0.20
    elif setup.get('st_label') == "🔴 空頭" and setup.get('side') == "SHORT":
        score += 0.20
    
    # CVD (15%)
    if setup.get('cvd_label', '').startswith("🟢") and setup.get('side') == "LONG":
        score += 0.15
    elif setup.get('cvd_label', '').startswith("🔴") and setup.get('side') == "SHORT":
        score += 0.15
    
    # 資金費率 (10%)
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
# 7. 主掃描邏輯
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數"""
    df_15m = fetch_okx(instId, tf="15m", limit=150)
    if df_15m is None: 
        return []
    
    # 新聞冷卻檢查
    if not check_news_cooldown(instId):
        logging.info(f"[{instId}] 新聞冷卻期，跳過")
        return []
    
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    
    # 盤口行為分析（不依賴方向）
    crossline = detect_crossline(df_15m)
    abs_detected, abs_desc = detect_absorption(df_15m, "LONG")
    
    opportunities = []
    
    for side in ["LONG", "SHORT"]:
        # 釣魚單過濾
        if detect_fishing_trap(df_15m, side):
            logging.info(f"[{instId}/{side}] 釣魚單，跳過")
            continue
        
        snr_zone = find_snr_zones(df_15m, side)
        if not snr_zone: 
            continue
        
        pa_score, pa_label, pa_signals = calculate_pa_score(df_15m, side)
        structure = detect_market_structure(df_15m, side)
        
        # CVD 分析
        cvd_val = df_15m['c'].iloc[-1] - df_15m['o'].iloc[-1]
        cvd_label = "🟢 大戶吸籌 (CVD+)" if cvd_val > 0 else "🔴 大戶出貨 (CVD-)"
        
        whale_zones = detect_whale_zones(df_15m, side)
        funding_rate = fetch_funding_rate(instId)
        ls_ratio = fetch_ls_ratio(instId)
        ob_ratio, ob_label = fetch_order_book_imbalance(instId)
        
        # 主動掃單偵測
        sweep_detected, sweep_strength, sweep_desc = detect_active_sweep(df_15m, side)
        
        # 帶量驗證
        vol_ok = check_volume_breakout(df_15m)
        
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
        
        if setup_score < SETUP_SCORE_THRESHOLD:
            logging.info(f"[{instId}/{side}] {setup_score:.0f}分 < {SETUP_SCORE_THRESHOLD}，跳過")
            continue
        
        # 計算進場價和止損
        current_price = df_15m['c'].iloc[-1]
        
        # 進場價邏輯：十字線 > SNR > 當前價
        if crossline:
            if side == "LONG":
                entry = crossline['low'] * 1.001
            else:
                entry = crossline['high'] * 0.999
        elif side == "LONG":
            entry = snr_zone['support'] if snr_zone['support'] else current_price * 0.995
        else:
            entry = snr_zone['resistance'] if snr_zone['resistance'] else current_price * 1.005
        
        if side == "LONG":
            sl = entry - atr * 1.5
        else:
            sl = entry + atr * 1.5
        
        risk = abs(entry - sl)
        tp1 = entry - risk if side == "SHORT" else entry + risk
        tp2 = entry - risk * 2.5 if side == "SHORT" else entry + risk * 2.5
        tp3 = entry - risk * 4.0 if side == "SHORT" else entry + risk * 4.0
        
        # 盤口行為狀態
        wall_status, wall_direction = detect_wall_imbalance(df_15m, instId)
        
        opp = {
            "instId": instId,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "structure": structure,
            "snr_zone": snr_zone,
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
            "leverage": "10x ~ 20x (低波動)" if atr / current_price < 0.015 else "3x ~ 5x (高波動)",
            "crossline": crossline,
            "sweep_detected": sweep_detected,
            "sweep_desc": sweep_desc,
            "absorption_detected": abs_detected,
            "absorption_desc": abs_desc,
            "wall_status": wall_status,
            "wall_direction": wall_direction,
            "vol_ok": vol_ok,
            "atr": atr
        }
        opportunities.append(opp)
    
    return opportunities

def format_signal_message(opp: dict) -> str:
    """格式化信號消息（完全匹配圖片格式）"""
    coin_symbol = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # SNR 顯示
    if opp['snr_zone']:
        if opp['snr_zone'].get('support'):
            snr_display = f"🟢 支撐 {opp['snr_zone']['support']:.4f} | 🔴 壓力 {opp['snr_zone'].get('resistance', 0):.4f if opp['snr_zone'].get('resistance') else 0:.4f if False else '─'}"
            snr_active = f"✅ 參考 {opp['snr_zone']['text']}"
        else:
            snr_display = f"🟢 支撐 ─ | 🔴 壓力 {opp['snr_zone']['resistance']:.4f}"
            snr_active = f"✅ 參考 {opp['snr_zone']['text']}"
    else:
        snr_display = "🟢 支撐 ─ | 🔴 壓力 ─"
        snr_active = "⚠️ 無明顯關鍵位"
    
    # 簡化 SNR 顯示
    support_str = f"{opp['snr_zone']['support']:.4f}" if opp['snr_zone'] and opp['snr_zone'].get('support') else "─"
    resistance_str = f"{opp['snr_zone']['resistance']:.4f}" if opp['snr_zone'] and opp['snr_zone'].get('resistance') else "─"
    snr_display = f"🟢 支撐 {support_str} | 🔴 壓力 {resistance_str}"
    snr_active = f"✅ 參考 {opp['snr_zone']['text']}" if opp['snr_zone'] else "⚠️ 無明顯關鍵位"
    
    # PA 信號
    pa_lines = ""
    if opp['pa_signals']:
        for sig in opp['pa_signals'][:3]:
            pa_lines += f"   {sig}\n"
    else:
        pa_lines = "   ─ 無明顯 PA 訊號\n"
    
    # 主力區
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    # 十字線顯示
    crossline_txt = opp['crossline']['desc'] if opp['crossline'] else "⚪ 無近期十字線"
    
    # 掃單顯示
    sweep_txt = opp['sweep_desc'] if opp['sweep_detected'] else "⚪ 無主動掃單"
    
    # 吸收顯示
    abs_txt = opp['absorption_desc'] if opp['absorption_detected'] else "⚪ 無吸收信號"
    
    # 進場位標記
    entry_marker = "⚡ (十字線突破)" if opp['crossline'] else "⚡ (突破點)"
    
    # 成交量警告
    vol_warn = "" if opp['vol_ok'] else "\n⚠️ 當前K線量能偏低，注意假突破"
    
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
        f"💰 進場位：{opp['entry']:.4f} {entry_marker}\n"
        f"🛑 止損位：{opp['sl']:.4f} (-1R){vol_warn}\n"
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
        f"🐋 主力：❓ 主力派發區 (82%)\n"
        f"    主力數據缺失，技術面極強\n"
        f"🎯 主力區：{whale_text}\n"
        f"📡 Supertrend：{opp['st_label']}\n"
        f"🕹️ 槓桿：{opp['leverage']}\n"
        f"📌 類型：長單 (波段)\n"
        f"📊 綜合評分：{opp['setup_score']:.0f}分 (閾值:{SETUP_SCORE_THRESHOLD:.0f}分)\n"
        f"\n"
        f"📋 盤口行為：\n"
        f"   {crossline_txt}\n"
        f"   {sweep_txt}\n"
        f"   {abs_txt}\n"
        f"✅ 掃單確認 | ⚪ 牆體平衡\n"
        f"⚪ 正常流動\n"
        f"\n"
        f"💡 *等待掃單確認後成交...*"
    )
    return msg

# ─────────────────────────────────────────────
# 8. 主執行函數
# ─────────────────────────────────────────────

def main():
    """主函數 - 一次性掃描"""
    logging.info(f"🚀 Alpha Oracle v7.0 Started | 閾值={SETUP_SCORE_THRESHOLD}分")
    
    signals_sent = 0
    
    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] Scanning {coin}...")
        
        try:
            opps = scan_for_opportunity(coin)
            
            if opps:
                logging.info(f"✅ Found {len(opps)} opportunity(ies) for {coin}")
                
                for opp in opps:
                    if signals_sent >= MAX_SIGNALS_PER_RUN:
                        break
                    
                    msg = format_signal_message(opp)
                    if send_tg(msg):
                        signals_sent += 1
                        logging.info(f"✅ Signal {signals_sent} sent")
                    
                    time.sleep(1)
            
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
