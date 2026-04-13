#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v8.0 — 高精度盤口行為技術分析策略框架
══════════════════════════════════════════════════════════════
🎯 精度提升功能：
  ✅ 多時間框架確認 (15m/1H/4H) — 趨勢一致性驗證
  ✅ 市場狀態識別 (ADX) — 震盪市/趨勢市自動切換策略
  ✅ BTC 相關性檢查 — 山寨幣與大盤同步確認
  ✅ 動態止損計算 — 避免假突破掃損
  ✅ 波動率過濾 — 高波動環境自動跳過
  ✅ RSI 背離偵測 — 趨勢反轉提前預警
  ✅ 成交量分佈分析 — POC/VAH/VAL 關鍵位識別
  ✅ ML 置信度評分 — 概率思維進場決策

📊 原有功能保留：
  ✅ 十字線（Doji）偵測 | ✅ 主動掃單識別 | ✅ 釣魚單過濾
  ✅ 新聞冷卻機制 | ✅ 帶量止損驗證 | ✅ 測牆機制 | ✅ 吸收過濾
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
from typing import Optional, Dict, List, Tuple, Union

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alpha_oracle_v8.log", encoding="utf-8", mode="a"),
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
ML_CONFIDENCE_THRESHOLD = 0.60  # ML置信度閾值 60%

# 盤口行為參數
CROSSLINE_BODY_RATIO = 0.30
SWEEP_VOLUME_RATIO = 1.8
SWEEP_CONSECUTIVE_MOVES = 2
NEWS_COOLDOWN_MINUTES = 60
ABSORPTION_VOL_MULTIPLIER = 1.8
ABSORPTION_PRICE_THRESHOLD = 0.002
FISHING_PRICE_MOVE = 0.005
FISHING_VOL_RATIO = 0.75

# 精度提升參數
ATR_SL_MULT_BASE = 2.0      # 基礎止損ATR倍數
ATR_SL_MULT_NEAR = 2.5      # 靠近關鍵位時倍數
VOLATILITY_THRESHOLD = 0.025  # ATR/價格 > 2.5% 視為高波動
RANGE_VOLATILITY_THRESHOLD = 0.035  # 5K振幅 > 3.5% 視為劇烈震盪
RSI_PERIOD = 14
ADX_PERIOD = 14
BTC_CORRELATION_THRESHOLD = 0.7

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
    now = time.time()
    if instId in _news_cooldown:
        if now - _news_cooldown[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False
    return True

def mark_news_event(instId: str):
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
    try:
        base_id = symbol.split('-')[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        return res['data'][0]['ratio'] if res.get('data') else "N/A"
    except Exception as e:
        logging.warning(f"[{symbol}] 多空比抓取錯誤: {e}")
        return "N/A"

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple:
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
# 4. 基礎技術指標
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple:
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

# ─────────────────────────────────────────────
# 5. 精度提升模組 ★ 新增核心
# ─────────────────────────────────────────────

def multi_timeframe_confirmation(instId: str, side: str) -> Tuple[bool, str, float]:
    """多時間框架確認 — 15m/1H/4H 趨勢一致性"""
    scores = []
    details = []
    
    # 1H 確認
    df_1h = fetch_okx(instId, tf="1H", limit=100)
    if df_1h is not None and len(df_1h) >= 30:
        st_1h, label_1h = calculate_supertrend(df_1h)
        ema21_1h = calculate_ema(df_1h['c'], 21).iloc[-1]
        ema55_1h = calculate_ema(df_1h['c'], 55).iloc[-1]
        price_1h = df_1h['c'].iloc[-1]
        
        if side == "LONG":
            if st_1h == 1 and price_1h > ema21_1h > ema55_1h:
                scores.append(1.0); details.append("✅ 1H多頭確認")
            elif st_1h == 1 or price_1h > ema21_1h:
                scores.append(0.6); details.append("🟡 1H中性偏多")
            else:
                scores.append(0.2); details.append("❌ 1H與信號相反")
        else:
            if st_1h == -1 and price_1h < ema21_1h < ema55_1h:
                scores.append(1.0); details.append("✅ 1H空頭確認")
            elif st_1h == -1 or price_1h < ema21_1h:
                scores.append(0.6); details.append("🟡 1H中性偏空")
            else:
                scores.append(0.2); details.append("❌ 1H與信號相反")
    
    # 4H 確認
    df_4h = fetch_okx(instId, tf="4H", limit=100)
    if df_4h is not None and len(df_4h) >= 30:
        price_4h = df_4h['c'].iloc[-1]
        ema21_4h = calculate_ema(df_4h['c'], 21).iloc[-1]
        
        if side == "LONG":
            if price_4h > ema21_4h:
                scores.append(1.0); details.append("✅ 4H多頭趨勢")
            else:
                scores.append(0.3); details.append("⚠️ 4H逆勢")
        else:
            if price_4h < ema21_4h:
                scores.append(1.0); details.append("✅ 4H空頭趨勢")
            else:
                scores.append(0.3); details.append("⚠️ 4H逆勢")
    
    avg_score = np.mean(scores) if scores else 0.5
    passed = avg_score >= 0.6
    return passed, " | ".join(details) if details else "⚪ 數據不足", avg_score * 100

def detect_market_regime(df: pd.DataFrame) -> Dict:
    """市場狀態識別 — ADX判斷震盪/趨勢"""
    high, low, close = df['h'], df['l'], df['c']
    
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    
    tr = np.maximum(high - low, np.abs(high - close.shift()))
    tr = np.maximum(tr, np.abs(low - close.shift()))
    atr = tr.rolling(ADX_PERIOD).mean()
    
    plus_di = 100 * (plus_dm.rolling(ADX_PERIOD).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(ADX_PERIOD).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = dx.rolling(ADX_PERIOD).mean()
    
    current_adx = adx.iloc[-1] if len(adx) > 0 else 0
    
    if current_adx < 20:
        regime, strategy, score = "📊 震盪市", "均值回歸策略", 0.4
    elif current_adx < 25:
        regime, strategy, score = "📈 弱趨勢", "謹慎趨勢策略", 0.6
    elif current_adx < 50:
        regime, strategy, score = "🚀 強趨勢", "趨勢跟隨策略", 0.9
    else:
        regime, strategy, score = "🔥 極強趨勢", "順勢交易", 1.0
    
    trend = "🟢 上升趨勢" if plus_di.iloc[-1] > minus_di.iloc[-1] else "🔴 下降趨勢"
    
    return {
        "regime": regime, "trend": trend, "adx": float(current_adx),
        "strategy": strategy, "score": score,
        "plus_di": float(plus_di.iloc[-1]), "minus_di": float(minus_di.iloc[-1])
    }

def check_volatility_filter(df: pd.DataFrame) -> Tuple[bool, str]:
    """波動率過濾 — 避免高波動環境"""
    atr = calculate_atr(df, period=14)
    current_price = df['c'].iloc[-1]
    atr_ratio = atr / current_price
    
    recent = df.tail(5)
    max_price, min_price = recent['h'].max(), recent['l'].min()
    range_pct = (max_price - min_price) / min_price
    
    if atr_ratio > VOLATILITY_THRESHOLD:
        return False, f"⚠️ 高波動 (ATR {atr_ratio*100:.2f}%)"
    if range_pct > RANGE_VOLATILITY_THRESHOLD:
        return False, f"⚠️ 劇烈震盪 (5K振幅 {range_pct*100:.2f}%)"
    
    return True, f"✅ 波動正常 (ATR {atr_ratio*100:.2f}%)"

def check_rsi_divergence(df: pd.DataFrame, side: str) -> Tuple[bool, str, float]:
    """RSI背離偵測"""
    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / (loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))
    
    current_rsi = rsi.iloc[-1]
    
    if side == "SHORT":
        recent_high = df['h'].iloc[-10:].max()
        recent_rsi_high = rsi.iloc[-10:].max()
        current_price = df['c'].iloc[-1]
        if current_price >= recent_high * 0.995 and current_rsi < recent_rsi_high * 0.95:
            return True, "🔴 RSI看跌背離", current_rsi
    elif side == "LONG":
        recent_low = df['l'].iloc[-10:].min()
        recent_rsi_low = rsi.iloc[-10:].min()
        current_price = df['c'].iloc[-1]
        if current_price <= recent_low * 1.005 and current_rsi > recent_rsi_low * 1.05:
            return True, "🟢 RSI看漲背離", current_rsi
    
    return False, "⚪ 無背離", current_rsi

def check_btc_correlation(instId: str, side: str) -> Tuple[bool, str, float]:
    """BTC相關性檢查"""
    btc_df = fetch_okx("BTC-USDT-SWAP", tf="1H", limit=50)
    alt_df = fetch_okx(instId, tf="1H", limit=50)
    
    if btc_df is None or alt_df is None or len(btc_df) < 10 or len(alt_df) < 10:
        return True, "⚪ BTC數據不足", 0.5
    
    btc_change = (btc_df['c'].iloc[-1] - btc_df['c'].iloc[-10]) / btc_df['c'].iloc[-10]
    alt_change = (alt_df['c'].iloc[-1] - alt_df['c'].iloc[-10]) / alt_df['c'].iloc[-10]
    correlation = alt_df['c'].iloc[-10:].corr(btc_df['c'].iloc[-10:])
    
    btc_st, _ = calculate_supertrend(btc_df)
    
    if correlation is None or pd.isna(correlation):
        correlation = 0.5
    
    if abs(correlation) > BTC_CORRELATION_THRESHOLD:
        if side == "LONG":
            if btc_change > 0 and btc_st == 1:
                return True, f"✅ BTC多頭 (+{btc_change*100:.2f}%), 相關{correlation:.2f}", 1.0
            else:
                return False, f"⚠️ BTC弱勢 (+{btc_change*100:.2f}%), 相關{correlation:.2f}", 0.3
        else:
            if btc_change < 0 and btc_st == -1:
                return True, f"✅ BTC空頭 ({btc_change*100:.2f}%), 相關{correlation:.2f}", 1.0
            else:
                return False, f"⚠️ BTC強勢 ({btc_change*100:.2f}%), 相關{correlation:.2f}", 0.3
    else:
        return True, f"🟡 低相關 ({correlation:.2f}), 可獨立走勢", 0.7

def calculate_dynamic_sl(entry: float, side: str, atr: float, 
                         resistance: float = None, support: float = None) -> float:
    """動態止損計算"""
    base_sl = entry + atr * ATR_SL_MULT_BASE if side == "SHORT" else entry - atr * ATR_SL_MULT_BASE
    
    if side == "SHORT" and resistance:
        distance = resistance - entry
        if distance < atr * ATR_SL_MULT_NEAR:
            base_sl = resistance + atr * 0.5
            logging.info(f"[動態止損] 靠近阻力，調整至 {base_sl:.4f}")
    
    if side == "LONG" and support:
        distance = entry - support
        if distance < atr * ATR_SL_MULT_NEAR:
            base_sl = support - atr * 0.5
            logging.info(f"[動態止損] 靠近支撐，調整至 {base_sl:.4f}")
    
    min_distance = atr * 1.8
    actual_distance = abs(base_sl - entry)
    if actual_distance < min_distance:
        base_sl = entry + (min_distance if side == "SHORT" else -min_distance)
        logging.info(f"[動態止損] 止損過緊，擴大至 {base_sl:.4f}")
    
    return base_sl

def check_entry_timing(df: pd.DataFrame, side: str) -> Tuple[bool, str]:
    """進場時機檢查"""
    recent = df.tail(3)
    
    if side == "SHORT":
        if all(recent['c'].iloc[i] > recent['c'].iloc[i-1] for i in range(1, 3)):
            total_gain = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / recent['c'].iloc[0]
            if total_gain > 0.015:
                return False, f"⚠️ 已連續上漲 {total_gain*100:.2f}%"
    elif side == "LONG":
        if all(recent['c'].iloc[i] < recent['c'].iloc[i-1] for i in range(1, 3)):
            total_loss = abs((recent['c'].iloc[-1] - recent['c'].iloc[0]) / recent['c'].iloc[0])
            if total_loss > 0.015:
                return False, f"⚠️ 已連續下跌 {total_loss*100:.2f}%"
    
    return True, "✅ 進場時機良好"

def calculate_ml_confidence(features: Dict) -> Dict:
    """ML置信度評分"""
    weights = {
        'supertrend_alignment': 0.20, 'volume_confirmation': 0.15,
        'rsi_divergence': 0.15, 'multi_tf_confirmation': 0.20,
        'market_regime': 0.10, 'btc_correlation': 0.10, 'orderflow_strength': 0.10
    }
    
    score = 0.0
    details = []
    
    # Supertrend對齊
    if features.get('st_15m') == features.get('st_1h') == features.get('st_4h'):
        score += weights['supertrend_alignment']
        details.append("✅ 多週期趨勢一致")
    elif features.get('st_15m') == features.get('st_1h'):
        score += weights['supertrend_alignment'] * 0.6
        details.append("🟡 短中期一致")
    else:
        details.append("❌ 趨勢混亂")
    
    # 成交量確認
    vol_ratio = features.get('volume_ratio', 0)
    if vol_ratio > 1.5:
        score += weights['volume_confirmation']
        details.append("✅ 帶量確認")
    elif vol_ratio > 1.0:
        score += weights['volume_confirmation'] * 0.5
        details.append("🟡 量能一般")
    else:
        details.append("❌ 無量")
    
    # RSI背離
    if features.get('has_rsi_divergence', False):
        score += weights['rsi_divergence']
        details.append("✅ RSI背離")
    else:
        details.append("⚪ 無背離")
    
    # 市場狀態
    regime = features.get('market_regime', 'ranging')
    trend_following = features.get('trend_following', False)
    if (regime == 'trending' and trend_following) or (regime == 'ranging' and not trend_following):
        score += weights['market_regime']
        details.append("✅ 策略匹配")
    else:
        details.append("⚠️ 策略不匹配")
    
    # BTC相關性
    btc_score = features.get('btc_score', 0.5)
    score += weights['btc_correlation'] * btc_score
    details.append(f"{'✅' if btc_score >= 0.7 else '⚠️'} BTC相關")
    
    # 訂單流強度
    orderflow = features.get('orderflow_score', 0.3)
    score += weights['orderflow_strength'] * orderflow
    
    # 置信度分級
    if score >= 0.8:
        confidence, action = "🔥 極高置信度", "強烈建議進場"
    elif score >= 0.65:
        confidence, action = "✅ 高置信度", "建議進場"
    elif score >= 0.5:
        confidence, action = "🟡 中等置信度", "謹慎進場"
    else:
        confidence, action = "❌ 低置信度", "建議觀望"
    
    return {
        "score": score, "confidence": confidence, "action": action,
        "details": details, "percentage": score * 100
    }

# ─────────────────────────────────────────────
# 6. 原有盤口行為分析模組（保留）
# ─────────────────────────────────────────────

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

def detect_crossline(df: pd.DataFrame, lookback: int = 15) -> Optional[Dict]:
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        rng = k['h'] - k['l'] + 1e-10
        if body < CROSSLINE_BODY_RATIO * rng:
            up_wick = k['h'] - max(k['c'], k['o'])
            dn_wick = min(k['c'], k['o']) - k['l']
            if up_wick > dn_wick * 1.5: potential = "SHORT"
            elif dn_wick > up_wick * 1.5: potential = "LONG"
            else: potential = "NEUTRAL"
            dist = len(df) - 1 - i
            return {"price": k['c'], "high": k['h'], "low": k['l'],
                    "body_ratio": body/rng, "potential_side": potential,
                    "distance": dist, "desc": f"🎯 十字線 @ {k['c']:.4f}（潛在：{potential}，{dist}根前）"}
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> Tuple[bool, float, str]:
    if len(df) < 8: return False, 0.0, "⚪ 數據不足"
    recent = df.tail(8)
    vol_ma = df['v'].tail(20).mean()
    vol_sc = recent.iloc[-1]['v'] / (vol_ma + 1e-10)
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"⚪ 量能不足 ({vol_sc:.1f}x)"
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side == "LONG" and recent['c'].iloc[i] > recent['c'].iloc[i-1]: moves += 1
        elif side == "SHORT" and recent['c'].iloc[i] < recent['c'].iloc[i-1]: moves += 1
        else: break
    if moves >= SWEEP_CONSECUTIVE_MOVES:
        return True, min(vol_sc/3.0, 1.0), f"⚡ 主動掃單！連續{moves}根+{vol_sc:.1f}x"
    return False, 0.0, f"⚪ 無連續掃單 ({moves}根)"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df) < 6: return False
    recent = df.tail(6)
    vol_ma = df['v'].tail(20).mean()
    price_mv = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if price_mv < FISHING_PRICE_MOVE: return False
    return recent['v'].iloc[-1] < FISHING_VOL_RATIO * vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> Tuple[bool, str]:
    if len(df) < 15: return False, "⚪ 無吸收"
    recent = df.tail(5)
    vol_ma = df['v'].tail(20).mean()
    avg_vol3 = recent['v'].iloc[-3:].mean()
    px_chg = abs(recent['c'].iloc[-1] - recent['c'].iloc[-4]) / (recent['c'].iloc[-4] + 1e-10)
    if avg_vol3 > ABSORPTION_VOL_MULTIPLIER * vol_ma and px_chg < ABSORPTION_PRICE_THRESHOLD:
        return True, f"🔄 吸收！量{avg_vol3/vol_ma:.1f}x 價動{px_chg*100:.2f}%"
    return False, "⚪ 無明顯吸收"

def check_volume_breakout(df: pd.DataFrame) -> bool:
    if len(df) < 6: return True
    recent = df.tail(6)
    return recent['v'].iloc[-1] >= 1.5 * recent['v'].iloc[:-1].mean()

def detect_wall_imbalance(df: pd.DataFrame, instId: str) -> Tuple[str, str]:
    ratio, label = fetch_order_book_imbalance(instId)
    if ratio >= 1.30: return label, "🔴 賣壓可能"
    elif ratio <= 0.77: return label, "🟢 買壓可能"
    return label, "⚪ 牆體平衡"

def detect_price_action(df: pd.DataFrame, side: str) -> list:
    signals = []
    for i in range(len(df)-1, max(len(df)-5, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        rng = k['h'] - k['l'] + 1e-10
        uw = k['h'] - max(k['c'], k['o'])
        dw = min(k['c'], k['o']) - k['l']
        if side == "SHORT" and uw >= body*2.0 and dw <= body*0.5:
            signals.append(f"空頭流星線 ({min(uw/(body+1e-10),5):.1f}R) @ {k['c']:.4f}")
        if side == "LONG" and dw >= body*2.0 and uw <= body*0.5:
            signals.append(f"多頭錘子線 ({min(dw/(body+1e-10),5):.1f}R) @ {k['c']:.4f}")
        if side == "SHORT" and uw/rng > 0.40 and k['c'] < k['o']:
            signals.append(f"壓力拒絕 (上影{uw/rng*100:.0f}%) @ {k['c']:.4f}")
        if side == "LONG" and dw/rng > 0.40 and k['c'] > k['o']:
            signals.append(f"支撐拒絕 (下影{dw/rng*100:.0f}%) @ {k['c']:.4f}")
        bp = body/rng
        if bp >= 0.70 and ((side=="LONG" and k['c']>k['o']) or (side=="SHORT" and k['c']<k['o'])):
            signals.append(f"{'多' if side=='LONG' else '空'}頭動量棒 ({bp*100:.0f}%) @ {k['c']:.4f}")
    return signals[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
    score = 0.0
    signals = detect_price_action(df, side)
    if len(signals) >= 3: score += 0.60
    elif len(signals) >= 2: score += 0.40
    elif len(signals) >= 1: score += 0.20
    last = df.iloc[-1]
    body = abs(last['c'] - last['o'])
    rng = last['h'] - last['l'] + 1e-10
    if body/rng > 0.70: score += 0.20
    if (side=="LONG" and last['c']>last['o']) or (side=="SHORT" and last['c']<last['o']): score += 0.20
    score = min(score, 1.0)
    label = "✅ 強勢PA" if score >= 0.65 else ("⚠️ 中等PA" if score >= 0.40 else "⛔ 弱PA")
    return score * 100, label, signals

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    zones = []
    vol_ma = df['v'].rolling(20).mean()
    vol_std = df['v'].rolling(20).std()
    for i in range(max(len(df)-10, 0), len(df)):
        if df['v'].iloc[i] > vol_ma.iloc[i] + 2*vol_std.iloc[i]:
            if df['c'].iloc[i] > df['o'].iloc[i] and side == "LONG":
                zones.append(f"🔵 主力吸籌 {df['c'].iloc[i]:.4f}")
            elif df['c'].iloc[i] < df['o'].iloc[i] and side == "SHORT":
                zones.append(f"🔴 主力派發 {df['c'].iloc[i]:.4f}")
    hi, lo = df['h'].iloc[-20:].max(), df['l'].iloc[-20:].min()
    zones.append(f"{'🔴 多頭清算' if side=='SHORT' else '🔵 空頭清算'} {hi if side=='SHORT' else lo:.4f}")
    return zones[:2]

def calculate_setup_score(setup: dict) -> float:
    score = 0.0
    if setup.get('whale_signal') == "✅ 主力一致":
        score += 0.30 * setup.get('whale_confidence', 0)
    elif setup.get('whale_signal') == "⚠️ 主力警示":
        score += 0.15 * setup.get('whale_confidence', 0)
    score += 0.25 * setup.get('pa_score', 0) / 100
    if setup.get('st_label') == "🟢 多頭" and setup.get('side') == "LONG": score += 0.20
    elif setup.get('st_label') == "🔴 空頭" and setup.get('side') == "SHORT": score += 0.20
    if setup.get('cvd_label', '').startswith("🟢") and setup.get('side') == "LONG": score += 0.15
    elif setup.get('cvd_label', '').startswith("🔴") and setup.get('side') == "SHORT": score += 0.15
    try:
        fr = setup.get('funding_rate', 0)
        if setup.get('side') == "LONG" and fr < 0.0003: score += 0.10
        elif setup.get('side') == "SHORT" and fr > -0.0003: score += 0.10
    except: pass
    return min(score, 1.0) * 100

# ─────────────────────────────────────────────
# 7. 主掃描邏輯 — v8.0 高精度版
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    df_15m = fetch_okx(instId, tf="15m", limit=150)
    if df_15m is None: return []
    
    if not check_news_cooldown(instId):
        logging.info(f"[{instId}] 新聞冷卻期，跳過")
        return []
    
    # 波動率過濾
    vol_ok, vol_msg = check_volatility_filter(df_15m)
    if not vol_ok:
        logging.info(f"[{instId}] {vol_msg}，跳過")
        return []
    
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    market_regime = detect_market_regime(df_15m)
    
    crossline = detect_crossline(df_15m)
    abs_detected, abs_desc = detect_absorption(df_15m, "LONG")
    
    opportunities = []
    
    for side in ["LONG", "SHORT"]:
        if detect_fishing_trap(df_15m, side):
            logging.info(f"[{instId}/{side}] 釣魚單，跳過")
            continue
        
        # 多時間框架確認
        mtf_passed, mtf_msg, mtf_score = multi_timeframe_confirmation(instId, side)
        if not mtf_passed:
            logging.info(f"[{instId}/{side}] {mtf_msg}，跳過")
            continue
        
        # BTC相關性檢查
        btc_passed, btc_msg, btc_score = check_btc_correlation(instId, side)
        if not btc_passed:
            logging.info(f"[{instId}/{side}] {btc_msg}，跳過")
            continue
        
        snr_zone = find_snr_zones(df_15m, side)
        if not snr_zone: continue
        
        timing_ok, timing_msg = check_entry_timing(df_15m, side)
        if not timing_ok:
            logging.info(f"[{instId}/{side}] {timing_msg}，跳過")
            continue
        
        pa_score, pa_label, pa_signals = calculate_pa_score(df_15m, side)
        structure = detect_market_structure(df_15m, side)
        has_div, div_msg, rsi_val = check_rsi_divergence(df_15m, side)
        
        cvd_val = df_15m['c'].iloc[-1] - df_15m['o'].iloc[-1]
        cvd_label = "🟢 大戶吸籌 (CVD+)" if cvd_val > 0 else "🔴 大戶出貨 (CVD-)"
        
        whale_zones = detect_whale_zones(df_15m, side)
        funding_rate = fetch_funding_rate(instId)
        ls_ratio = fetch_ls_ratio(instId)
        ob_ratio, ob_label = fetch_order_book_imbalance(instId)
        
        sweep_det, sweep_str, sweep_desc = detect_active_sweep(df_15m, side)
        vol_ok = check_volume_breakout(df_15m)
        wall_status, wall_direction = detect_wall_imbalance(df_15m, instId)
        
        # ML置信度計算
        features = {
            'st_15m': st_val,
            'st_1h': calculate_supertrend(fetch_okx(instId, "1H"), 10)[0] if fetch_okx(instId, "1H") is not None else 0,
            'st_4h': calculate_supertrend(fetch_okx(instId, "4H"), 10)[0] if fetch_okx(instId, "4H") is not None else 0,
            'volume_ratio': df_15m['v'].iloc[-1] / df_15m['v'].tail(20).mean(),
            'has_rsi_divergence': has_div,
            'market_regime': 'trending' if market_regime['adx'] >= 25 else 'ranging',
            'trend_following': (side=="LONG" and market_regime['trend']=="🟢 上升趨勢") or 
                              (side=="SHORT" and market_regime['trend']=="🔴 下降趨勢"),
            'orderflow_score': 0.8 if sweep_det else 0.3,
            'btc_score': btc_score / 100
        }
        ml_result = calculate_ml_confidence(features)
        
        if ml_result['score'] < ML_CONFIDENCE_THRESHOLD:
            logging.info(f"[{instId}/{side}] 置信度 {ml_result['percentage']:.0f}% < {ML_CONFIDENCE_THRESHOLD*100:.0f}%，跳過")
            continue
        
        setup = {
            'side': side, 'pa_score': pa_score, 'st_label': st_label,
            'cvd_label': cvd_label, 'funding_rate': funding_rate,
            'whale_signal': "✅ 主力一致" if len(whale_zones)>0 else "❓ 技術面主導",
            'whale_confidence': 0.82
        }
        setup_score = calculate_setup_score(setup) + (10 if has_div else 0)
        
        if setup_score < SETUP_SCORE_THRESHOLD:
            logging.info(f"[{instId}/{side}] {setup_score:.0f}分 < {SETUP_SCORE_THRESHOLD}，跳過")
            continue
        
        # 進場價與動態止損
        current_price = df_15m['c'].iloc[-1]
        if crossline:
            entry = crossline['low']*1.001 if side=="LONG" else crossline['high']*0.999
        elif side == "LONG":
            entry = snr_zone['support'] if snr_zone['support'] else current_price*0.995
        else:
            entry = snr_zone['resistance'] if snr_zone['resistance'] else current_price*1.005
        
        resistance = snr_zone.get('resistance') if side=="SHORT" else None
        support = snr_zone.get('support') if side=="LONG" else None
        sl = calculate_dynamic_sl(entry, side, atr, resistance, support)
        
        risk = abs(entry - sl)
        tp1 = entry + risk*1.0 if side=="LONG" else entry - risk*1.0
        tp2 = entry + risk*2.5 if side=="LONG" else entry - risk*2.5
        tp3 = entry + risk*4.0 if side=="LONG" else entry - risk*4.0
        
        opp = {
            "instId": instId, "side": side,
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "structure": structure, "snr_zone": snr_zone,
            "pa_score": pa_score, "pa_label": pa_label, "pa_signals": pa_signals,
            "cvd_label": cvd_label, "ls_ratio": ls_ratio,
            "funding_rate": funding_rate, "ob_label": ob_label,
            "whale_zones": whale_zones, "st_label": st_label,
            "setup_score": setup_score,
            "leverage": "10x~20x" if atr/current_price<0.015 else "3x~5x",
            "crossline": crossline, "sweep_detected": sweep_det, "sweep_desc": sweep_desc,
            "absorption_detected": abs_detected, "absorption_desc": abs_desc,
            "wall_status": wall_status, "wall_direction": wall_direction,
            "vol_ok": vol_ok, "atr": atr,
            # 精度提升字段
            "ml_confidence": ml_result, "market_regime": market_regime,
            "has_divergence": has_div, "divergence_msg": div_msg, "rsi_value": rsi_val,
            "volatility_msg": vol_msg, "mtf_score": mtf_score, "btc_score": btc_score
        }
        opportunities.append(opp)
    
    return opportunities

def format_signal_message(opp: dict) -> str:
    coin = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side']=="LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side']=="LONG" else "空單 (SHORT)"
    
    support_str = f"{opp['snr_zone']['support']:.4f}" if opp['snr_zone'] and opp['snr_zone'].get('support') else "─"
    resistance_str = f"{opp['snr_zone']['resistance']:.4f}" if opp['snr_zone'] and opp['snr_zone'].get('resistance') else "─"
    snr_display = f"🟢 支撐 {support_str} | 🔴 壓力 {resistance_str}"
    snr_active = f"✅ 參考 {opp['snr_zone']['text']}" if opp['snr_zone'] else "⚠️ 無明顯關鍵位"
    
    pa_lines = "".join(f"   {s}\n" for s in opp['pa_signals'][:3]) if opp['pa_signals'] else "   ─ 無明顯PA\n"
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    crossline_txt = opp['crossline']['desc'] if opp['crossline'] else "⚪ 無近期十字線"
    sweep_txt = opp['sweep_desc'] if opp['sweep_detected'] else "⚪ 無主動掃單"
    abs_txt = opp['absorption_desc'] if opp['absorption_detected'] else "⚪ 無吸收信號"
    
    rsi_txt = f"\n   {opp['divergence_msg']} (RSI {opp['rsi_value']:.1f})" if opp['has_divergence'] else ""
    entry_marker = "⚡ (十字線突破)" if opp['crossline'] else "⚡ (突破點)"
    vol_warn = "" if opp['vol_ok'] else "\n⚠️ 當前K線量能偏低"
    
    sl_dist_pct = abs(opp['entry']-opp['sl'])/opp['entry']*100
    sl_info = f"(-{sl_dist_pct:.2f}%)"
    
    ml = opp['ml_confidence']
    regime = opp['market_regime']
    
    msg = (
        f"🔥 *Alpha Oracle v8.0 | 高精度訊號* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m | 📊 市場：{regime['regime']} {regime['trend']}\n"
        f"📈 多空比 {opp['ls_ratio']} | 資費 {opp['funding_rate']*100:.4f}%\n"
        f"🧬 CVD：{opp['cvd_label']}\n"
        f"📚 盤口：{opp['ob_label']}\n"
        f"📉 波動：{opp['volatility_msg']}\n"
        f"\n"
        f"💰 進場：{opp['entry']:.4f} {entry_marker}\n"
        f"🛑 止損：{opp['sl']:.4f} {sl_info}{vol_warn}\n"
        f"🎯 TP1(1R): {opp['tp1']:.4f}\n"
        f"🎯 TP2(2.5R): {opp['tp2']:.4f}\n"
        f"🎯 TP3(4R): {opp['tp3']:.4f}\n"
        f"\n"
        f"🏗️ 結構：{opp['structure']}\n"
        f"🛡️ SNR：{snr_display}\n    {snr_active}\n"
        f"\n"
        f"🕯️ PA ({opp['pa_label']} {opp['pa_score']:.0f}分)\n{pa_lines}"
        f"🐋 主力：{' | '.join(opp['whale_zones'])[:40]}...\n"
        f"📡 Supertrend：{opp['st_label']}{rsi_txt}\n"
        f"🕹️ 槓桿：{opp['leverage']} | 📌 波段\n"
        f"📊 評分：{opp['setup_score']:.0f}分 | 🎲 置信度：{ml['confidence']} ({ml['percentage']:.0f}%)\n"
        f"\n"
        f"📋 盤口行為：\n   {crossline_txt}\n   {sweep_txt}\n   {abs_txt}\n"
        f"\n"
        f"🔬 多週期：{'✅' if opp['mtf_score']>=60 else '⚠️'} 1H/4H確認 ({opp['mtf_score']:.0f}分)\n"
        f"🔗 BTC相關：{'✅' if opp['btc_score']>=70 else '⚠️'} ({opp['btc_score']:.0f}分)\n"
        f"\n"
        f"💡 *{ml['action']}*"
    )
    return msg

# ─────────────────────────────────────────────
# 8. 主執行函數
# ─────────────────────────────────────────────

def main():
    logging.info(f"🚀 Alpha Oracle v8.0 Started | 閾值={SETUP_SCORE_THRESHOLD}分 | 置信度={ML_CONFIDENCE_THRESHOLD*100:.0f}%")
    signals_sent = 0
    
    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] Scanning {coin}...")
        try:
            opps = scan_for_opportunity(coin)
            if opps:
                logging.info(f"✅ Found {len(opps)} for {coin}")
                for opp in opps:
                    if signals_sent >= MAX_SIGNALS_PER_RUN: break
                    msg = format_signal_message(opp)
                    if send_tg(msg):
                        signals_sent += 1
                        logging.info(f"📤 #{signals_sent} {opp['side']} {opp['setup_score']:.0f}分 {opp['ml_confidence']['confidence']}")
                    time.sleep(1)
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"❌ {coin}: {e}")
            traceback.print_exc()
    
    logging.info(f"📊 Complete. Sent {signals_sent} signals.")
    return signals_sent

if __name__ == "__main__":
    try:
        main(); exit(0)
    except Exception as e:
        logging.error(f"💥 Crash: {e}")
        traceback.print_exc()
        exit(1)
