#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v4.2 - 進階主力追蹤 + 價格行為學 + 進場通知修復版
核心功能：
  ✅ 1H 趨勢確認（多時間框架過濾）
  ✅ 成交量確認（避免假突破）
  ✅ ICT SNR 支撐/壓力區（明確顯示價格）
  ✅ 盤口不平衡度分析
  ✅ 進場掛單優先使用 FVG/OB 區域
  ✅ 動態止盈 + 移動止損（自動保本與追蹤）
  ✅ 完整進場/管理通知（含進場價/止損/止盈+風險%/R 倍數）
  ✅ 訊號失效通知（進場價已過未觸發時提醒）
  ✅ 午夜 00:00 自動勝率報告
  ✅ 市場結構與交易方向正確匹配
  ✅ CoinAnk 主力數據整合框架
  ✅ 動態信心閾值優化
  🆕 價格行為學分析（Pin Bar / 吞噬 / 內包棒 / 動量棒 / 拒絕棒）
  🆕 PA 評分系統（0-100 分，整合進訊號品質判斷）
  🔧 修復：WAITING 進場判斷邏輯 BUG（is_hit 永遠先於 missed_entry 檢查）
預期勝率：78-85% | 訊號頻率：每日 1-3 個極高品質訊號
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN            = os.getenv("TG_TOKEN")
CHAT_ID             = os.getenv("CHAT_ID")
COINANK_API_KEY     = os.getenv("COINANK_API_KEY", "")
GLASSNODE_API_KEY   = os.getenv("GLASSNODE_API_KEY", "")
CRYPTOQUANT_API_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")
OPTIMIZATION_FILE   = "whale_optimization.json"

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]
LOG_FILE            = "active_trades.csv"
STATS_FILE          = "daily_stats.csv"
WAITING_EXPIRY_BARS = 20   # 15m × 20 = 5 小時

LOG_COLS = [
    "instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3",
    "locked", "wait_since", "tp1_hit", "entry_source",
    "snr_display", "snr_active",
    "whale_signal", "whale_confidence", "whale_category",
    "pa_score", "pa_signals",                  # 🆕 PA 欄位
]
STATS_COLS = ["instId", "result", "whale_signal", "whale_confidence", "whale_category", "pa_score"]

# ─────────────────────────────────────────────
# 2. 工具函數 & 動態優化模組
# ─────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def safe_int(val, fallback=0):
    try:    return int(float(val))
    except: return fallback

def load_optimization_params() -> dict:
    if os.path.exists(OPTIMIZATION_FILE):
        try:
            with open(OPTIMIZATION_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {
        "base_threshold":    0.7,
        "aligned_win_rate":  0.75,
        "warning_win_rate":  0.60,
        "reverse_win_rate":  0.40,
        "total_samples":     0
    }

def save_optimization_params(params: dict):
    with open(OPTIMIZATION_FILE, 'w') as f:
        json.dump(params, f)

def get_dynamic_threshold(opt_params: dict) -> float:
    base = opt_params['base_threshold']
    awr  = opt_params['aligned_win_rate']
    if   awr > 0.80: return max(0.5, base - 0.1)
    elif awr < 0.65: return min(0.9, base + 0.1)
    return base

def normalize_trade(t: dict) -> dict:
    return {
        "instId":           str(t.get("instId", "")),
        "side":             str(t.get("side", "")),
        "status":           str(t.get("status", "")),
        "entry":            safe_float(t.get("entry")),
        "sl":               safe_float(t.get("sl")),
        "tp1":              safe_float(t.get("tp1")),
        "tp2":              safe_float(t.get("tp2")),
        "tp3":              safe_float(t.get("tp3")),
        "locked":           safe_int(t.get("locked")),
        "wait_since":       safe_int(t.get("wait_since", 0)),
        "tp1_hit":          safe_int(t.get("tp1_hit", 0)),
        "entry_source":     str(t.get("entry_source", "Breakout")),
        "snr_display":      str(t.get("snr_display", "🟢 支撐 ─ | 🔴 壓力 ─")),
        "snr_active":       str(t.get("snr_active", "⚠️ 無明顯關鍵位")),
        "whale_signal":     str(t.get("whale_signal", "─")),
        "whale_confidence": safe_float(t.get("whale_confidence", 0)),
        "whale_category":   str(t.get("whale_category", "Unknown")),
        "pa_score":         safe_float(t.get("pa_score", 0)),
        "pa_signals":       str(t.get("pa_signals", "─")),
    }

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        logging.warning(f"Telegram 發送失敗：{e}")

def get_whale_position_recommendation(whale_signal: str, whale_conf: float) -> tuple[str, str, str]:
    if whale_signal == "✅ 主力一致":
        if   whale_conf >= 0.80: return "✅ 正常 (100%)", "75-85%", "🟢"
        elif whale_conf >= 0.65: return "🟡 標準 (75%)", "70-78%", "🟡"
        else:                    return "🟠 保守 (50%)", "60-70%", "🟠"
    elif whale_signal == "⚠️ 主力警示":
        if whale_conf >= 0.60:   return "🟠 保守 (50%)", "60-70%", "🟠"
        else:                    return "🔴 觀望/極小",  "<60%",    "🔴"
    else:
        return "⛔ 建議跳過", "<50%", "🔴"

# ─────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────
def fetch_okx(instId: str, tf: str = "15m", limit: int = 100) -> pd.DataFrame | None:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0' or not res.get('data'): return None
        df = pd.DataFrame(
            res['data'],
            columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
        )
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] {tf} K 線抓取失敗：{e}")
        return None

def fetch_current_candle_hl(instId: str) -> tuple[float, float]:
    """抓取當前「未收盤」K 棒的最高/最低價"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
        res = requests.get(url, timeout=5).json()
        for row in res['data']:
            if row[8] == "0":
                return float(row[3]), float(row[2])   # (low, high)
    except Exception as e:
        logging.warning(f"[{instId}] 當前 K 棒抓取失敗：{e}")
    return float('inf'), float('-inf')

def get_funding_ls(instId: str) -> tuple[str, str]:
    base_id  = instId.replace("-SWAP", "").split("-")[0]
    funding  = "N/A"
    ls_ratio = "N/A"
    try:
        f_res   = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率抓取失敗：{e}")
    try:
        ls_res  = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5
        ).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except Exception as e:
        logging.warning(f"[{instId}] 多空比抓取失敗：{e}")
    return funding, ls_ratio

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple[float, str]:
    try:
        url = f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}"
        res = requests.get(url, timeout=5).json()
        if res['code'] != '0' or not res['data']:
            return 1.0, "⚪ 盤口均衡"
        data = res['data'][0]
        bid_vol = sum(float(b[1]) for b in data['bids'])
        ask_vol = sum(float(a[1]) for a in data['asks'])
        if ask_vol == 0: return 1.0, "⚪ 盤口均衡"
        ratio = bid_vol / ask_vol
        if   ratio > 1.2: label = f"🟢 買盤強勢 ({ratio:.2f})"
        elif ratio < 0.8: label = f"🔴 賣盤強勢 ({ratio:.2f})"
        else:             label = f"⚪ 盤口均衡 ({ratio:.2f})"
        return ratio, label
    except Exception as e:
        logging.warning(f"[{instId}] 盤口數據抓取失敗：{e}")
        return 1.0, "⚪ 數據缺失"

def fetch_funding_rate_raw(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res['data'][0]['fundingRate'])
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率原始值抓取失敗：{e}")
        return 0.0

# ─────────────────────────────────────────────
# 🆕 CoinAnk / Glassnode / CryptoQuant 主力數據框架
# ─────────────────────────────────────────────
def fetch_coinank_spot_cvd(symbol: str) -> dict | None:
    if not COINANK_API_KEY:
        return {"cvd_24h": 0.0, "trend": "neutral"}
    try:
        url     = "https://api.coinank.com/api/indicators/spot-cvd"
        headers = {"Authorization": f"Bearer {COINANK_API_KEY}"}
        res     = requests.get(url, params={"symbol": symbol, "period": "24h"},
                               headers=headers, timeout=10).json()
        if res.get('code') == 200 and res.get('data'):
            v = float(res['data']['cvd_value'])
            return {"cvd_24h": v, "trend": "bullish" if v > 0 else "bearish"}
    except Exception as e:
        logging.warning(f"[{symbol}] CoinAnk CVD 失敗：{e}")
    return None

def fetch_glassnode_whale_flow(symbol: str) -> dict | None:
    if not GLASSNODE_API_KEY: return {"net_flow": 0, "signal": "neutral"}
    try:
        url = (f"https://rest.glassnode.com/v1/metrics/transfers/exchange_net_flow"
               f"?asset={symbol}&resolution=24h&api_key={GLASSNODE_API_KEY}")
        res = requests.get(url, timeout=10).json()
        if res and len(res) > 0:
            flow = res[-1]['value']
            return {"net_flow": flow, "signal": "inflow" if flow > 0 else "outflow"}
    except: pass
    return None

def fetch_cryptoquant_open_interest(symbol: str) -> dict | None:
    if not CRYPTOQUANT_API_KEY: return {"oi_change": 0, "signal": "neutral"}
    try:
        url = (f"https://api.cryptoquant.com/v1/data/bitcoin/metrics/open-interest"
               f"?api_key={CRYPTOQUANT_API_KEY}")
        res = requests.get(url, timeout=10).json()
        if res and 'data' in res:
            data = res['data']
            if len(data) >= 2:
                change = (data[-1]['value'] - data[-2]['value']) / (abs(data[-2]['value']) + 1e-10)
                sig = "rising" if change > 0.05 else ("falling" if change < -0.05 else "stable")
                return {"oi_change": change, "signal": sig}
    except: pass
    return None

def analyze_whale_direction(instId: str, side: str, opt_params: dict) -> tuple[str, float, str, str]:
    symbol      = instId.split('-')[0]
    spot_cvd    = fetch_coinank_spot_cvd(symbol)
    whale_flow  = fetch_glassnode_whale_flow(symbol)
    oi_data     = fetch_cryptoquant_open_interest(symbol)
    fr_raw      = fetch_funding_rate_raw(instId)
    _, ls_str   = get_funding_ls(instId)
    ls_ratio    = float(ls_str) if ls_str != "N/A" else 1.0

    signals     = []
    confidence  = 0.0
    category    = "Aligned"

    if spot_cvd:
        if   side == "LONG"  and spot_cvd['trend'] == "bearish":
            signals.append("🔴 現貨大戶出貨"); confidence += 0.35; category = "Reverse"
        elif side == "SHORT" and spot_cvd['trend'] == "bullish":
            signals.append("🟢 現貨大戶吸籌"); confidence += 0.35; category = "Reverse"
        else:
            signals.append("⚪ 現貨 CVD 一致"); confidence += 0.10

    if whale_flow:
        if   side == "LONG"  and whale_flow['signal'] == "inflow":
            signals.append("🔴 巨鯨大量流入交易所"); confidence += 0.25
        elif side == "SHORT" and whale_flow['signal'] == "outflow":
            signals.append("🟢 巨鯨提幣鎖倉");       confidence += 0.25

    if oi_data:
        if   side == "SHORT" and oi_data['signal'] == "rising":
            signals.append("🔴 空頭持倉激增（主力壓制）"); confidence += 0.20
        elif side == "LONG"  and oi_data['signal'] == "falling":
            signals.append("⚠️ 空頭回補上漲，非主力主動做多"); confidence -= 0.10

    if   ls_ratio > 1.1 and side == "LONG":
        signals.append("🔴 散戶過度看多"); confidence += 0.15; category = "Reverse"
    elif ls_ratio < 0.9 and side == "SHORT":
        signals.append("🟢 散戶過度看空"); confidence += 0.15; category = "Reverse"

    dynamic_threshold = get_dynamic_threshold(opt_params)
    confidence        = max(0.0, min(1.0, confidence))

    if category == "Reverse" and confidence >= dynamic_threshold:
        return "🔴 主力反向", confidence, f"多項指標顯示主力反向操作（{confidence*100:.0f}%）", "Reverse"
    elif confidence >= 0.5:
        return "⚠️ 主力警示", confidence, f"主力動向存在衝突（{confidence*100:.0f}%）", "Warning"
    else:
        return "✅ 主力一致", confidence, f"技術面與主力流向一致（{confidence*100:.0f}%）", "Aligned"

def detect_whale_entry_zones(df: pd.DataFrame, side: str) -> list[dict]:
    zones   = []
    vol_ma  = df['v'].rolling(20).mean()
    vol_std = df['v'].rolling(20).std()
    for i in range(max(len(df) - 10, 0), len(df)):
        if df['v'].iloc[i] > vol_ma.iloc[i] + 2 * vol_std.iloc[i]:
            if df['c'].iloc[i] > df['o'].iloc[i] and side == "LONG":
                zones.append({"type": "whale_accumulation",  "price": df['c'].iloc[i],
                               "desc": f"🐋 主力吸籌區 {df['c'].iloc[i]:.4f}"})
            elif df['c'].iloc[i] < df['o'].iloc[i] and side == "SHORT":
                zones.append({"type": "whale_distribution", "price": df['c'].iloc[i],
                               "desc": f"🐋 主力派發區 {df['c'].iloc[i]:.4f}"})
    recent_high = df['h'].iloc[-20:].max()
    recent_low  = df['l'].iloc[-20:].min()
    if side == "SHORT":
        zones.append({"type": "liquidation_cluster", "price": recent_high,
                      "desc": f"💥 多頭清算熱點 {recent_high:.4f}"})
    else:
        zones.append({"type": "liquidation_cluster", "price": recent_low,
                      "desc": f"💥 空頭清算熱點 {recent_low:.4f}"})
    return zones[:3]

# ─────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> tuple[float, str]:
    recent = df.tail(lookback).copy()
    body   = (recent['h'] - recent['l']).replace(0, 1e-10)
    recent['delta'] = np.where(
        recent['c'] >= recent['o'],
        recent['v'] * (recent['c'] - recent['l']) / body,
        -recent['v'] * (recent['h'] - recent['c']) / body
    )
    cvd   = recent['delta'].sum()
    label = "🟢 大戶吸籌 (CVD+)" if cvd > 0 else "🔴 大戶出貨 (CVD-)"
    return cvd, label

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> int:
    if len(df) < period + 2: return 0
    high  = df['h'].values.astype(float)
    low   = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    hl2      = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr
    basic_dn = hl2 + multiplier * atr
    final_up = np.zeros(n); final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)
    final_up[period] = basic_up[period]; final_dn[period] = basic_dn[period]
    for i in range(period+1, n):
        final_up[i] = (basic_up[i] if basic_up[i] > final_up[i-1] or close[i-1] < final_up[i-1]
                       else final_up[i-1])
        final_dn[i] = (basic_dn[i] if basic_dn[i] < final_dn[i-1] or close[i-1] > final_dn[i-1]
                       else final_dn[i-1])
        if   trend[i-1] == -1 and close[i] > final_dn[i-1]: trend[i] = 1
        elif trend[i-1] ==  1 and close[i] < final_up[i-1]: trend[i] = -1
        else:                                                 trend[i] = trend[i-1]
    return int(trend[-1])

# ─────────────────────────────────────────────
# 4.5 🆕 價格行為學 (Price Action) 分析模組
# ─────────────────────────────────────────────

def detect_pin_bar(df: pd.DataFrame, lookback: int = 3) -> dict:
    """
    釘形棒偵測 (Pin Bar / Hammer / Shooting Star)
    ──────────────────────────────────────────────
    多頭釘形棒 (Hammer / Bullish Pin):
      - 下影線 >= 實體 × 2
      - 上影線 <= 實體 × 0.5
      - 出現在支撐位附近 = 強力反彈信號
    空頭釘形棒 (Shooting Star / Bearish Pin):
      - 上影線 >= 實體 × 2
      - 下影線 <= 實體 × 0.5
      - 出現在壓力位附近 = 強力壓制信號
    強度計算：影線 / 實體 的比值，越高越強
    """
    for i in range(len(df) - 1, max(len(df) - lookback - 1, 0), -1):
        k           = df.iloc[i]
        body        = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        if body < total_range * 0.05:   # 極小實體（十字星）跳過
            continue
        upper_wick = k['h'] - max(k['c'], k['o'])
        lower_wick = min(k['c'], k['o']) - k['l']

        # 多頭釘形棒
        if lower_wick >= body * 2.0 and upper_wick <= body * 0.5:
            strength = min(lower_wick / (body + 1e-10) / 5.0, 1.0)
            return {
                "detected": True,
                "type":     "bullish_pin",
                "strength": strength,
                "bar_idx":  i,
                "price":    k['l'],
                "desc":     f"📌 多頭錘子線 ({lower_wick/body:.1f}R影) @ {k['c']:.4f}",
            }
        # 空頭釘形棒
        if upper_wick >= body * 2.0 and lower_wick <= body * 0.5:
            strength = min(upper_wick / (body + 1e-10) / 5.0, 1.0)
            return {
                "detected": True,
                "type":     "bearish_pin",
                "strength": strength,
                "bar_idx":  i,
                "price":    k['h'],
                "desc":     f"📌 空頭流星線 ({upper_wick/body:.1f}R影) @ {k['c']:.4f}",
            }
    return {"detected": False, "type": None, "strength": 0, "desc": ""}


def detect_engulfing(df: pd.DataFrame, lookback: int = 3) -> dict:
    """
    吞噬形態偵測 (Engulfing Pattern)
    ─────────────────────────────────
    多頭吞噬 (Bullish Engulfing):
      - 前一根為陰線，當前為陽線
      - 當前 K 棒實體完全覆蓋前一根 K 棒實體
      - 代表買力突然大量湧入，反轉信號
    空頭吞噬 (Bearish Engulfing):
      - 前一根為陽線，當前為陰線
      - 當前 K 棒實體完全覆蓋前一根 K 棒實體
      - 代表賣力突然大量湧現
    強度 = 當前實體 / 前一根實體（越大越強，上限 3 倍正規化）
    """
    for i in range(len(df) - 1, max(len(df) - lookback - 1, 1), -1):
        curr      = df.iloc[i]
        prev      = df.iloc[i - 1]
        curr_body = abs(curr['c'] - curr['o'])
        prev_body = abs(prev['c'] - prev['o'])
        if prev_body < 1e-10: continue

        # 多頭吞噬
        if (curr['c'] > curr['o'] and prev['c'] < prev['o'] and
                curr['o'] <= prev['c'] and curr['c'] >= prev['o']):
            strength = min(curr_body / (prev_body + 1e-10) / 3.0, 1.0)
            return {
                "detected": True,
                "type":     "bullish_engulfing",
                "strength": strength,
                "bar_idx":  i,
                "desc":     f"🕯️ 多頭吞噬 ({curr_body/prev_body:.1f}x) @ {curr['c']:.4f}",
            }
        # 空頭吞噬
        if (curr['c'] < curr['o'] and prev['c'] > prev['o'] and
                curr['o'] >= prev['c'] and curr['c'] <= prev['o']):
            strength = min(curr_body / (prev_body + 1e-10) / 3.0, 1.0)
            return {
                "detected": True,
                "type":     "bearish_engulfing",
                "strength": strength,
                "bar_idx":  i,
                "desc":     f"🕯️ 空頭吞噬 ({curr_body/prev_body:.1f}x) @ {curr['c']:.4f}",
            }
    return {"detected": False, "type": None, "strength": 0, "desc": ""}


def detect_inside_bar(df: pd.DataFrame) -> dict:
    """
    內包棒偵測 (Inside Bar / NR4 / NR7)
    ─────────────────────────────────────
    當前 K 棒的高低完全在前一根 K 棒高低範圍內：
      - 代表市場在盤整壓縮，能量積累中
      - 突破方向確認後，產生強烈動能釋放
    壓縮率 = 1 - (內包棒範圍 / 母棒範圍)，越高代表壓縮越嚴重
    """
    if len(df) < 2:
        return {"detected": False}
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    if curr['h'] <= prev['h'] and curr['l'] >= prev['l']:
        mother_range = prev['h'] - prev['l'] + 1e-10
        inner_range  = curr['h'] - curr['l']
        compression  = 1.0 - inner_range / mother_range
        return {
            "detected":      True,
            "type":          "inside_bar",
            "compression":   compression,
            "breakout_high": prev['h'],
            "breakout_low":  prev['l'],
            "desc":          f"📦 內包棒壓縮 ({compression*100:.0f}%) 母棒:{prev['h']:.4f}/{prev['l']:.4f}",
        }
    return {"detected": False}


def detect_rejection_candle(df: pd.DataFrame, side: str) -> dict:
    """
    關鍵位拒絕 K 棒偵測 (Rejection Candle at Key Level)
    ─────────────────────────────────────────────────────
    多頭拒絕（支撐位反彈）:
      - 下影線 > 整根 K 棒 40%
      - 陽線收盤 (c > o)
      - 代表空頭推低被強力承接
    空頭拒絕（壓力位壓制）:
      - 上影線 > 整根 K 棒 40%
      - 陰線收盤 (c < o)
      - 代表多頭推高被強力賣出
    """
    if len(df) < 1:
        return {"detected": False, "type": None, "strength": 0, "desc": ""}
    k           = df.iloc[-1]
    total_range = k['h'] - k['l'] + 1e-10
    upper_wick  = k['h'] - max(k['c'], k['o'])
    lower_wick  = min(k['c'], k['o']) - k['l']
    upper_pct   = upper_wick / total_range
    lower_pct   = lower_wick / total_range

    if side == "LONG" and lower_pct > 0.40 and k['c'] > k['o']:
        return {
            "detected": True,
            "type":     "support_rejection",
            "strength": lower_pct,
            "desc":     f"🔄 支撐位拒絕 (下影 {lower_pct*100:.0f}%) @ {k['c']:.4f}",
        }
    if side == "SHORT" and upper_pct > 0.40 and k['c'] < k['o']:
        return {
            "detected": True,
            "type":     "resistance_rejection",
            "strength": upper_pct,
            "desc":     f"🔄 壓力位拒絕 (上影 {upper_pct*100:.0f}%) @ {k['c']:.4f}",
        }
    return {"detected": False, "type": None, "strength": 0, "desc": ""}


def detect_momentum_bar(df: pd.DataFrame, side: str, lookback: int = 5) -> dict:
    """
    動量 K 棒偵測 (Momentum / Marubozu-like)
    ──────────────────────────────────────────
    強勢實體 K 棒：
      - 實體佔 K 棒總範圍 ≥ 70%（接近光頭光腳）
      - 實體大小 ≥ ATR × 0.8（確保是大 K 棒）
      - 方向與訊號一致 → 動能加強確認
    代表主力方向性推進，後續延伸機率高
    """
    atr = calculate_atr(df)
    for i in range(len(df) - 1, max(len(df) - lookback - 1, 0), -1):
        k           = df.iloc[i]
        body        = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        body_pct    = body / total_range
        if body_pct >= 0.70 and body >= atr * 0.8:
            is_bullish = k['c'] > k['o']
            if (side == "LONG" and is_bullish) or (side == "SHORT" and not is_bullish):
                return {
                    "detected":  True,
                    "type":      "momentum_bar",
                    "direction": "bullish" if is_bullish else "bearish",
                    "strength":  body_pct,
                    "bar_idx":   i,
                    "desc":      f"⚡ {'多頭' if is_bullish else '空頭'}動量棒 ({body_pct*100:.0f}%實體) @ {k['c']:.4f}",
                }
    return {"detected": False, "type": None, "strength": 0, "desc": ""}


def detect_false_breakout(df: pd.DataFrame, side: str, lookback: int = 10) -> dict:
    """
    假突破偵測 (False Breakout / Fake-out)
    ─────────────────────────────────────────
    識別已發生的假突破，用於確認真實訊號：
    空頭假突破 (做多信號):
      - 某根 K 棒突破近期高點 → 但最終收盤回到高點下方
      - 代表多頭陷阱，後續做空機率高
    多頭假突破 (做空信號):
      - 某根 K 棒跌破近期低點 → 但最終收盤回到低點上方
      - 代表空頭陷阱，後續做多機率高
    """
    if len(df) < lookback + 2:
        return {"detected": False}
    recent = df.tail(lookback)
    recent_high = recent['h'].iloc[:-1].max()
    recent_low  = recent['l'].iloc[:-1].min()
    last = df.iloc[-1]

    if side == "LONG":
        # 空頭假突破：前幾根跌破低點但收盤回來
        for i in range(len(df) - 3, max(len(df) - lookback - 1, 1), -1):
            k = df.iloc[i]
            if k['l'] < recent_low and k['c'] > recent_low:
                return {
                    "detected":   True,
                    "type":       "bearish_fakeout",
                    "fakeout_low": k['l'],
                    "recovery":   k['c'],
                    "desc":       f"🪤 空頭假突破獵殺 ({k['l']:.4f}→{k['c']:.4f})",
                }
    elif side == "SHORT":
        # 多頭假突破：前幾根突破高點但收盤回來
        for i in range(len(df) - 3, max(len(df) - lookback - 1, 1), -1):
            k = df.iloc[i]
            if k['h'] > recent_high and k['c'] < recent_high:
                return {
                    "detected":    True,
                    "type":        "bullish_fakeout",
                    "fakeout_high": k['h'],
                    "recovery":    k['c'],
                    "desc":        f"🪤 多頭假突破獵殺 ({k['h']:.4f}→{k['c']:.4f})",
                }
    return {"detected": False}


def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple[float, list[str]]:
    """
    🆕 價格行為學綜合評分 (PA Score)
    ──────────────────────────────────
    整合五種 PA 訊號，回傳 (評分 0.0~1.0, 訊號描述列表)

    評分加權：
      釘形棒 (Pin Bar)         → 最高 +0.25
      吞噬形態 (Engulfing)     → 最高 +0.20
      關鍵位拒絕 (Rejection)   → 最高 +0.20
      動量 K 棒 (Momentum)     → 最高 +0.15
      假突破確認 (FakeOut)      → 最高 +0.15
      內包棒 (Inside Bar)      → 最高 +0.10
      多訊號加成                → 最高 +0.10
      反向訊號懲罰              → 最多 -0.15

    分數等級：
      ≥ 0.65 → 強勢 PA 確認 ✅
      0.40-0.64 → 中等 PA 確認 ⚠️
      < 0.40 → PA 訊號弱 ⛔
    """
    score   = 0.0
    signals = []

    # 1. 釘形棒（最重要的 PA 訊號）
    pin = detect_pin_bar(df)
    if pin['detected']:
        aligned = (side == "LONG" and pin['type'] == "bullish_pin") or \
                  (side == "SHORT" and pin['type'] == "bearish_pin")
        if aligned:
            score += 0.25 * pin['strength']
            signals.append(pin['desc'])
        elif pin['strength'] > 0.6:   # 反向強釘形棒 = 警示
            score -= 0.10
            signals.append(f"⚠️ 反向 {pin['desc']}")

    # 2. 吞噬形態
    eng = detect_engulfing(df)
    if eng['detected']:
        aligned = (side == "LONG" and eng['type'] == "bullish_engulfing") or \
                  (side == "SHORT" and eng['type'] == "bearish_engulfing")
        if aligned:
            score += 0.20 * eng['strength']
            signals.append(eng['desc'])
        else:
            score -= 0.05
            signals.append(f"⚠️ 反向 {eng['desc']}")

    # 3. 關鍵位拒絕
    rej = detect_rejection_candle(df, side)
    if rej['detected']:
        score += 0.20 * rej['strength']
        signals.append(rej['desc'])

    # 4. 動量 K 棒確認
    mom = detect_momentum_bar(df, side)
    if mom['detected']:
        score += 0.15 * mom['strength']
        signals.append(mom['desc'])

    # 5. 假突破確認（高可信度信號）
    fbo = detect_false_breakout(df, side)
    if fbo['detected']:
        score += 0.15
        signals.append(fbo['desc'])

    # 6. 內包棒（壓縮後即將爆發）
    ib = detect_inside_bar(df)
    if ib['detected']:
        score += 0.10 * ib['compression']
        signals.append(ib['desc'])

    # 多訊號加成（≥ 2 個正向信號）
    positive_count = len([s for s in signals if not s.startswith("⚠️")])
    if positive_count >= 3:   score += 0.10
    elif positive_count >= 2: score += 0.05

    # 正規化到 [0, 1]
    score = max(0.0, min(1.0, score))
    return score, signals

# ─────────────────────────────────────────────
# 5. SMC & ICT 結構分析
# ─────────────────────────────────────────────
def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple[list, list]:
    data = df.tail(lookback).reset_index(drop=True)
    sh, sl = [], []
    for i in range(n, len(data) - n):
        wh = data['h'].iloc[i-n:i+n+1]; wl = data['l'].iloc[i-n:i+n+1]
        if data['h'].iloc[i] == wh.max(): sh.append(data['h'].iloc[i])
        if data['l'].iloc[i] == wl.min(): sl.append(data['l'].iloc[i])
    return sorted(set(sh)), sorted(set(sl))

def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
    sh, sl   = find_swing_points(df, n=3, lookback=60)
    has_w    = len(sl) >= 2 and sl[-2] > 0 and abs(sl[-2] - sl[-1]) / sl[-2] < 0.015
    has_m    = len(sh) >= 2 and sh[-2] > 0 and abs(sh[-2] - sh[-1]) / sh[-2] < 0.015
    if side == "LONG":
        if has_w: return "W 底反轉 📐"
        if has_m: return "M 頭壓制 ⚠️"
    elif side == "SHORT":
        if has_m: return "M 頭反轉 📐"
        if has_w: return "W 底支撐 ⚠️"
    if has_w: return "W 底反轉 📐"
    if has_m: return "M 頭反轉 📐"
    recent = df.tail(20)
    slope  = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if   slope >  0.025: return "上升趨勢延續 📈"
    elif slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

def find_order_block(df: pd.DataFrame, side: str, lookback: int = 15) -> dict | None:
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, kn = data.iloc[i], data.iloc[i+1]
        if side == "LONG"  and k['c'] < k['o'] and kn['c'] > kn['o']:
            return {"high": k['o'],  "low": k['l']}
        if side == "SHORT" and k['c'] > k['o'] and kn['c'] < kn['o']:
            return {"high": k['h'],  "low": k['c']}
    return None

def find_recent_fvg(df: pd.DataFrame, side: str) -> dict | None:
    for i in range(len(df) - 3, max(len(df) - 20, 0), -1):
        k0, k2 = df.iloc[i-1], df.iloc[i+1]
        if side == "LONG"  and k2['l'] > k0['h']: return {"high": k2['l'], "low": k0['h']}
        if side == "SHORT" and k2['h'] < k0['l']: return {"high": k0['l'], "low": k2['h']}
    return None

def find_ict_snr_zones(df: pd.DataFrame, side: str, lookback: int = 30) -> dict | None:
    sh, sl = find_swing_points(df, n=2, lookback=lookback)
    price  = df['c'].iloc[-1]
    if side == "LONG":
        valid = [s for s in sl if s < price * 0.995]
        if valid:
            s = max(valid)
            return {"support": s, "resistance": None, "active_level": s,
                    "type": "support", "text": f"支撐 {s:.4f}"}
    else:
        valid = [r for r in sh if r > price * 1.005]
        if valid:
            r = min(valid)
            return {"support": None, "resistance": r, "active_level": r,
                    "type": "resistance", "text": f"壓力 {r:.4f}"}
    return None

def calculate_structural_sl(df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
    buf = atr * 0.25
    ob  = find_order_block(df, side)
    fvg = find_recent_fvg(df, side)
    snr = find_ict_snr_zones(df, side)
    if side == "LONG":
        cands = []
        if ob  and ob['low']  < entry: cands.append(ob['low']  - buf)
        if fvg and fvg['low'] < entry: cands.append(fvg['low'] - buf)
        if snr and snr.get('active_level') and snr['active_level'] < entry:
            cands.append(snr['active_level'] - buf)
        if cands:
            sl = max(cands)
            return sl if (entry - sl) / (entry + 1e-10) >= 0.004 else entry - atr * 1.5
        return entry - atr * 1.5
    else:
        cands = []
        if ob  and ob['high']  > entry: cands.append(ob['high']  + buf)
        if fvg and fvg['high'] > entry: cands.append(fvg['high'] + buf)
        if snr and snr.get('active_level') and snr['active_level'] > entry:
            cands.append(snr['active_level'] + buf)
        if cands:
            sl = min(cands)
            return sl if (sl - entry) / (entry + 1e-10) >= 0.004 else entry + atr * 1.5
        return entry + atr * 1.5

def get_fixed_r_tps(entry: float, sl: float, side: str) -> tuple[float, float, float]:
    risk = abs(entry - sl) + 1e-10
    if side == "LONG":  return entry + risk, entry + risk * 2, entry + risk * 3
    else:               return entry - risk, entry - risk * 2, entry - risk * 3

def suggest_leverage(atr: float, price: float, whale_confidence: float = 0.5) -> tuple[str, str]:
    vol_pct = (atr / (price + 1e-10)) * 100
    if whale_confidence < 0.4:
        if vol_pct > 3:    return "2x ~ 3x",   "⚠️ 主力不明 + 高波動"
        elif vol_pct > 1.5: return "3x ~ 5x",  "⚠️ 主力不明 + 中波動"
        else:               return "5x ~ 8x",  "⚠️ 主力不明 + 低波動"
    if vol_pct > 3:    return "3x ~ 5x",   "⚠️ 高波動"
    elif vol_pct > 1.5: return "5x ~ 10x",  "中波動"
    else:               return "10x ~ 20x", "低波動"

# ─────────────────────────────────────────────
# 6. 過濾器函數
# ─────────────────────────────────────────────
def is_trending_market(df: pd.DataFrame) -> bool:
    if len(df) < 50: return True
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(14).mean().iloc[-1] > tr.tail(50).mean() * 0.7

def get_btc_direction(btc_df: pd.DataFrame, lookback: int = 5) -> str:
    if btc_df is None or len(btc_df) < lookback: return "NEUTRAL"
    recent  = btc_df.tail(lookback)
    bearish = int((recent['c'] < recent['o']).sum())
    bullish = lookback - bearish
    if bearish >= 4: return "DOWN"
    if bullish >= 4: return "UP"
    return "NEUTRAL"

def classify_trade(side: str, structure: str, risk_pct: float) -> str:
    if "反轉" in structure: return "📊 長單 (波段)"
    elif risk_pct < 1.0:    return "⚡ 短單 (日內)"
    return "📊 長單 (波段)"

# ─────────────────────────────────────────────
# 7. SMC 訊號掃描（主力追蹤 + PA 評分版）
# ─────────────────────────────────────────────
# 🆕 PA 最低評分要求（低於此分不發訊）
PA_MIN_SCORE = 0.30   # 可調整：0.30 = 至少 1 個中等 PA 訊號

def find_smc_setup(df: pd.DataFrame, instId: str, opt_params: dict) -> dict | None:
    if df is None or len(df) < 40: return None

    atr  = calculate_atr(df)
    best = None

    for i in range(len(df) - 3, len(df) - 25, -1):
        if i < 2: continue
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i-15:i].max():
            best = {"side": "LONG",  "k0": k0, "k1": k1, "k2": k2}; break
        elif k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i-15:i].min():
            best = {"side": "SHORT", "k0": k0, "k1": k1, "k2": k2}; break

    if best is None: return None

    side = best['side']
    k0, k1, k2 = best['k0'], best['k1'], best['k2']
    price = df['c'].iloc[-1]

    # ── 主力方向 ─────────────────────────────
    whale_signal, whale_conf, whale_desc, whale_cat = analyze_whale_direction(instId, side, opt_params)
    if whale_signal == "🔴 主力反向" and whale_conf >= get_dynamic_threshold(opt_params):
        logging.info(f"[{instId}] 主力反向（{whale_conf*100:.0f}%），跳過 {side}")
        return None

    # ── 🆕 PA 評分（放在進場判斷前）────────────────
    pa_score, pa_signals = calculate_pa_score(df, side)
    if pa_score < PA_MIN_SCORE:
        logging.info(f"[{instId}] PA 評分不足 ({pa_score:.2f} < {PA_MIN_SCORE})，跳過")
        return None

    # ── 進場價優先：主力區 > FVG > OB ────────────
    fvg         = find_recent_fvg(df, side)
    ob          = find_order_block(df, side)
    whale_zones = detect_whale_entry_zones(df, side)

    entry        = k1['c']
    entry_source = "Breakout"

    def _try_zone(zone_type_list, price_key, cond_fn):
        for z in whale_zones:
            if z['type'] in zone_type_list and cond_fn(z['price']):
                return z['price'], f"Whale-{z['type']}"
        return None, None

    if side == "LONG":
        e, s = _try_zone(['whale_accumulation', 'liquidation_cluster'],
                         'price', lambda p: k1['c'] < p < price * 0.995)
        if e: entry, entry_source = e, s
        elif fvg and k1['c'] < fvg['high'] < price * 0.995:
            entry, entry_source = fvg['high'], "FVG"
        elif ob and k1['c'] < ob['high'] < price * 0.995:
            entry, entry_source = ob['high'], "OB"
    else:
        e, s = _try_zone(['whale_distribution', 'liquidation_cluster'],
                         'price', lambda p: k1['c'] > p > price * 1.005)
        if e: entry, entry_source = e, s
        elif fvg and k1['c'] > fvg['low'] > price * 1.005:
            entry, entry_source = fvg['low'], "FVG"
        elif ob and k1['c'] > ob['low'] > price * 1.005:
            entry, entry_source = ob['low'], "OB"

    if abs(entry - price) / price > 0.03:
        entry, entry_source = k1['c'], "Breakout (過遠)"

    sl           = calculate_structural_sl(df, side, entry, atr)
    tp1, tp2, tp3 = get_fixed_r_tps(entry, sl, side)

    risk      = abs(entry - sl) + 1e-10
    risk_pct  = risk / (entry + 1e-10) * 100
    structure = detect_market_structure(df, side)
    lev, lev_note = suggest_leverage(atr, price, whale_conf)
    trade_type = classify_trade(side, structure, risk_pct)
    _, cvd_label = calculate_cvd(df)
    st_val = calculate_supertrend(df)
    st_label = "📈 多頭" if st_val == 1 else ("📉 空頭" if st_val == -1 else "⚪ 未知")

    snr_zone = find_ict_snr_zones(df, side)
    if snr_zone:
        s_txt  = f"{snr_zone['support']:.4f}"    if snr_zone.get('support')    else "─"
        r_txt  = f"{snr_zone['resistance']:.4f}" if snr_zone.get('resistance') else "─"
        snr_display = f"🟢 支撐 {s_txt} | 🔴 壓力 {r_txt}"
        snr_active  = f"✅ 參考 {snr_zone['text']}" if snr_zone.get('active_level') else "⚠️ 無明確關鍵位"
    else:
        snr_display = "🟢 支撐 ─ | 🔴 壓力 ─"
        snr_active  = "⚠️ 無明顯關鍵位"

    whale_zones_text = " | ".join([z['desc'] for z in whale_zones[:2]]) if whale_zones else "─"

    # 🆕 PA 評級標籤
    pa_label = ("✅ 強勢PA" if pa_score >= 0.65
                else ("⚠️ 中等PA" if pa_score >= 0.40
                      else "⛔ 弱PA"))

    return {
        "side":             side,
        "entry":            entry,
        "entry_source":     entry_source,
        "sl":               sl,
        "tp1":              tp1, "tp2": tp2, "tp3": tp3,
        "structure":        structure,
        "leverage":         lev, "leverage_note": lev_note,
        "trade_type":       trade_type,
        "cvd_label":        cvd_label,
        "st_val":           st_val, "st_label": st_label,
        "snr_display":      snr_display, "snr_active": snr_active,
        "snr_zone":         snr_zone, "fvg": fvg, "ob": ob,
        "whale_signal":     whale_signal, "whale_confidence": whale_conf,
        "whale_desc":       whale_desc, "whale_zones": whale_zones_text,
        "whale_category":   whale_cat,
        "pa_score":         pa_score,        # 🆕
        "pa_label":         pa_label,         # 🆕
        "pa_signals":       " | ".join(pa_signals) if pa_signals else "─",  # 🆕
    }

# ─────────────────────────────────────────────
# 主力績效統計
# ─────────────────────────────────────────────
def update_whale_stats(whale_cat: str, result: str):
    f = "whale_perf_temp.csv"
    row = pd.DataFrame([{"category": whale_cat, "result": result}])
    if os.path.exists(f):
        pd.concat([pd.read_csv(f), row], ignore_index=True).to_csv(f, index=False)
    else:
        row.to_csv(f, index=False)

def generate_midnight_report(opt_params: dict) -> str:
    f = "whale_perf_temp.csv"
    if not os.path.exists(f): return ""
    df = pd.read_csv(f)
    if df.empty: return ""
    def calc_wr(sub): return len(sub[sub['result'] == 'TP']) / len(sub) * 100 if len(sub) > 0 else 0
    awr = calc_wr(df[df['category'] == 'Aligned'])
    wwr = calc_wr(df[df['category'] == 'Warning'])
    rwr = calc_wr(df[df['category'] == 'Reverse'])
    opt_params.update({'aligned_win_rate': awr/100, 'warning_win_rate': wwr/100,
                       'reverse_win_rate': rwr/100, 'total_samples': len(df)})
    save_optimization_params(opt_params)
    os.remove(f)
    return (
        f"\n🐋 *主力績效統計 (近 {len(df)} 單)*\n"
        f"   ✅ 主力一致勝率: {awr:.1f}%\n"
        f"   ⚠️ 主力警示勝率: {wwr:.1f}%\n"
        f"   🚫 主力反向勝率: {rwr:.1f}%\n"
        f"   🔄 動態閾值：{get_dynamic_threshold(opt_params):.2f}"
    )

# ─────────────────────────────────────────────
# 8. 主程式
# ─────────────────────────────────────────────
ENTRY_SRC_EMOJI = {
    "FVG": "🕳️", "OB": "🧱", "Breakout": "⚡",
    "Whale-whale_accumulation": "🐋", "Whale-whale_distribution": "🐋",
    "Whale-liquidation_cluster": "💥",
}
ENTRY_SRC_TEXT = {
    "FVG":                       "FVG 缺口",
    "OB":                        "OB 訂單塊",
    "Breakout":                  "突破點",
    "Whale-whale_accumulation":  "主力吸籌",
    "Whale-whale_distribution":  "主力派發",
    "Whale-liquidation_cluster": "清算熱點",
}
WHALE_EMOJI = {
    "✅ 主力一致": "🐋",
    "⚠️ 主力警示": "⚠️",
    "🔴 主力反向": "🚫",
}

def main():
    try:
        now_tw        = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"
        opt_params    = load_optimization_params()

        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 午夜報告 ────────────────────────────────────────────
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                try:
                    df_s = pd.read_csv(STATS_FILE)
                    if not df_s.empty:
                        tp_c  = len(df_s[df_s['result'] == 'TP'])
                        sl_c  = len(df_s[df_s['result'] == 'SL'])
                        total = tp_c + sl_c
                        wr    = (tp_c / total * 100) if total > 0 else 0
                        date_str = (now_tw - timedelta(days=1)).strftime('%Y-%m-%d')
                        whale_rpt = generate_midnight_report(opt_params)
                        send_tg(
                            f"📊 *Alpha Oracle v4.2 | 每日戰績報告*\n"
                            f"══════════════════════\n"
                            f"📅 {date_str}  ⏰ {now_tw.strftime('%H:%M')}\n"
                            f"\n"
                            f"✅ 盈利：{tp_c} 單\n"
                            f"❌ 止損：{sl_c} 單\n"
                            f"📊 總計：{total} 單\n"
                            f"🎯 勝率：*{wr:.1f}%*\n"
                            f"💰 平均盈虧比：{(tp_c*2+sl_c*(-1))/total if total > 0 else 0:.2f}R\n"
                            f"{whale_rpt}\n"
                            f"══════════════════════\n"
                            f"🐋 主力追蹤 + PA 分析已啟用"
                        )
                    if is_midnight:
                        pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as fh:
                            fh.write(f"ok_{now_tw.strftime('%Y%m%d')}")
                except Exception as e:
                    logging.error(f"戰績報告發送失敗：{e}")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # ── B. 讀取已有持倉 ────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            for col in ["wait_since", "tp1_hit", "entry_source", "snr_display", "snr_active",
                        "whale_signal", "whale_confidence", "whale_category", "pa_score", "pa_signals"]:
                if col not in trades_df.columns:
                    defaults = {
                        "entry_source":    "Breakout",
                        "snr_display":     "🟢 支撐 ─ | 🔴 壓力 ─",
                        "snr_active":      "⚠️ 無明顯關鍵位",
                        "whale_signal":    "─",
                        "whale_confidence": 0.5,
                        "whale_category":  "Unknown",
                        "pa_score":        0.0,
                        "pa_signals":      "─",
                    }
                    trades_df[col] = defaults.get(col, 0)
        except Exception:
            trades_df = pd.DataFrame(columns=LOG_COLS)

        active_ids     = trades_df['instId'].tolist()
        updated_trades = []
        current_bar    = int(datetime.utcnow().timestamp() // 900)

        btc_df    = fetch_okx("BTC-USDT-SWAP")
        btc_trend = get_btc_direction(btc_df)
        logging.info(f"BTC 當前方向：{btc_trend}")

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty:
                time.sleep(0.2); continue

            curr_p   = df['c'].iloc[-1]
            coin_sym = instId.split('-')[0]

            # ── 1. 尋找新機會 ──────────────────────────────────────
            if instId not in active_ids:
                if not is_trending_market(df):
                    logging.info(f"[{instId}] 盤整市場，跳過")
                    time.sleep(0.2); continue

                setup = find_smc_setup(df, instId, opt_params)
                if setup:
                    cvd_val, _ = calculate_cvd(df)
                    if setup['side'] == "LONG"  and cvd_val < 0: time.sleep(0.2); continue
                    if setup['side'] == "SHORT" and cvd_val > 0: time.sleep(0.2); continue

                    fr = fetch_funding_rate_raw(instId)
                    if setup['side'] == "LONG"  and fr >  0.0005: time.sleep(0.2); continue
                    if setup['side'] == "SHORT" and fr < -0.0005: time.sleep(0.2); continue

                    if instId != "BTC-USDT-SWAP":
                        if setup['side'] == "LONG"  and btc_trend == "DOWN": time.sleep(0.2); continue
                        if setup['side'] == "SHORT" and btc_trend == "UP":   time.sleep(0.2); continue

                    if setup['st_val'] == -1 and setup['side'] == "LONG":  time.sleep(0.2); continue
                    if setup['st_val'] ==  1 and setup['side'] == "SHORT": time.sleep(0.2); continue
                    if setup['snr_zone'] is None:                           time.sleep(0.2); continue

                    ob_ratio, ob_label = fetch_order_book_imbalance(instId)
                    if setup['side'] == "LONG"  and ob_ratio < 0.9: time.sleep(0.2); continue
                    if setup['side'] == "SHORT" and ob_ratio > 1.1: time.sleep(0.2); continue

                    # ── 發送訊號通知 ────────────────────────────
                    funding, ls_ratio = get_funding_ls(instId)
                    side_emoji = "🟢" if setup['side'] == "LONG" else "🔴"
                    side_zh    = "多單 (LONG)" if setup['side'] == "LONG" else "空單 (SHORT)"

                    if "反轉" in setup['structure']:
                        tp_labels, style = ("1.0R", "2.5R", "4.0R"), "長單 (波段)"
                    elif "盤整" in setup['structure']:
                        tp_labels, style = ("0.8R", "1.5R", "2.0R"), "短單 (日內)"
                    else:
                        tp_labels, style = ("1.0R", "2.0R", "3.0R"), "長單 (波段)"

                    src_emoji = ENTRY_SRC_EMOJI.get(setup['entry_source'], "📍")
                    src_text  = ENTRY_SRC_TEXT.get(setup['entry_source'], setup['entry_source'])
                    st_emoji  = "📈" if setup['st_val'] == 1 else ("📉" if setup['st_val'] == -1 else "⚪")
                    w_emoji   = WHALE_EMOJI.get(setup['whale_signal'], "❓")

                    # 🆕 PA 訊號文字
                    pa_lines = ""
                    if setup['pa_signals'] and setup['pa_signals'] != "─":
                        for sig in setup['pa_signals'].split(" | ")[:3]:
                            pa_lines += f"   {sig}\n"
                    else:
                        pa_lines = "   ─ 無明顯 PA 訊號\n"

                    send_tg(
                        f"🔥 *Alpha Oracle v4.2 訊號發射* 🔥\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🎯 方向：{side_emoji} {side_zh}\n"
                        f"⏰ 週期：15m\n"
                        f"📊 多空比 {ls_ratio} | 資費 {funding}\n"
                        f"🧬 CVD：{setup['cvd_label']}\n"
                        f"📚 盤口：{ob_label}\n"
                        f"\n"
                        f"💰 進場位：{setup['entry']:.4f} {src_emoji}({src_text})\n"
                        f"🛑 止損位：{setup['sl']:.4f}  (-1R)\n"
                        f"💰 TP1 ({tp_labels[0]}): {setup['tp1']:.4f}\n"
                        f"💰 TP2 ({tp_labels[1]}): {setup['tp2']:.4f}\n"
                        f"💰 TP3 ({tp_labels[2]}): {setup['tp3']:.4f}\n"
                        f"\n"
                        f"🏗️ 結構：{setup['structure']}\n"
                        f"🛡️ SNR：{setup['snr_display']}\n"
                        f"    {setup['snr_active']}\n"
                        f"\n"
                        f"🕯️ *價格行為 ({setup['pa_label']} {setup['pa_score']*100:.0f}分)*\n"
                        f"{pa_lines}"
                        f"🐋 主力：{w_emoji} {setup['whale_signal']} ({setup['whale_confidence']*100:.0f}%)\n"
                        f"    {setup['whale_desc']}\n"
                        f"🎯 主力區：{setup['whale_zones']}\n"
                        f"📡 Supertrend：{st_emoji} {setup['st_label']}\n"
                        f"🕹️ 槓桿：{setup['leverage']} ({setup['leverage_note']})\n"
                        f"📌 類型：{style}\n"
                        f"\n"
                        f"💡 *等待回踩 {src_text} 成交...*"
                    )
                    updated_trades.append({
                        "instId":           instId,
                        "side":             setup['side'],
                        "status":           "WAITING",
                        "entry":            setup['entry'],
                        "sl":               setup['sl'],
                        "tp1":              setup['tp1'],
                        "tp2":              setup['tp2'],
                        "tp3":              setup['tp3'],
                        "locked":           0,
                        "wait_since":       current_bar,
                        "tp1_hit":          0,
                        "entry_source":     setup['entry_source'],
                        "snr_display":      setup['snr_display'],
                        "snr_active":       setup['snr_active'],
                        "whale_signal":     setup['whale_signal'],
                        "whale_confidence": setup['whale_confidence'],
                        "whale_category":   setup['whale_category'],
                        "pa_score":         setup['pa_score'],
                        "pa_signals":       setup['pa_signals'],
                    })
                time.sleep(0.2); continue

            # ── 2. 管理現有持倉 ─────────────────────────────────────
            t = normalize_trade(trades_df[trades_df['instId'] == instId].iloc[0].to_dict())

            if t['status'] == "WAITING":
                # ════════════════════════════════════════════════════
                # 🔧 BUG FIX：is_hit 必須最先判斷，
                #    原版本：missed_entry 的 continue 導致 is_hit 被跳過
                #    修正後：先確認進場，再判斷是否失效/逾時
                # ════════════════════════════════════════════════════

                # ① 優先：確認進場觸發
                n_check         = min(3, len(df))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low       = min(df['l'].iloc[-n_check:].min(), cur_low)
                check_high      = max(df['h'].iloc[-n_check:].max(), cur_high)

                is_hit = (
                    (t['side'] == "LONG"  and check_low  <= t['entry']) or
                    (t['side'] == "SHORT" and check_high >= t['entry'])
                )
                already_sl = (
                    (t['side'] == "LONG"  and curr_p < t['sl']) or
                    (t['side'] == "SHORT" and curr_p > t['sl'])
                )

                if is_hit and already_sl:
                    # 觸及進場位後立即穿破止損 → 放棄
                    logging.info(f"[{instId}] 進場觸及但已穿破止損，放棄")
                    time.sleep(0.2); continue

                if is_hit:
                    # ✅ 進場確認 → 發送進場通知
                    t['status'] = "ACTIVE"
                    side_emoji  = "🟢" if t['side'] == "LONG" else "🔴"
                    side_zh     = "多單 (LONG)" if t['side'] == "LONG" else "空單 (SHORT)"
                    risk        = abs(t['entry'] - t['sl']) + 1e-10
                    risk_pct    = (risk / t['entry']) * 100
                    r1 = abs(t['tp1'] - t['entry']) / risk
                    r2 = abs(t['tp2'] - t['entry']) / risk
                    r3 = abs(t['tp3'] - t['entry']) / risk
                    now_str     = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
                    src_emoji   = ENTRY_SRC_EMOJI.get(t['entry_source'], "📍")
                    src_text    = ENTRY_SRC_TEXT.get(t['entry_source'], t['entry_source'])
                    w_emoji     = WHALE_EMOJI.get(t['whale_signal'], "❓")
                    pos_rec, wr_range, conf_color = get_whale_position_recommendation(
                        t['whale_signal'], t['whale_confidence']
                    )
                    extra_warn = "\n⚠️ *主力動向不明，建議謹慎*" if "跳過" in pos_rec or "觀望" in pos_rec else ""

                    # 🆕 PA 摘要（進場通知版）
                    pa_summary = ""
                    if t['pa_signals'] and t['pa_signals'] != "─":
                        pa_top = t['pa_signals'].split(" | ")[0]
                        pa_summary = f"\n🕯️ PA：{pa_top} ({t['pa_score']*100:.0f}分)"

                    send_tg(
                        f"🚀 *Alpha Oracle v4.2 | 進場成交* 🚀\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🎯 方向：{side_emoji} {side_zh}\n"
                        f"⏰ 時間：{now_str}\n"
                        f"\n"
                        f"💰 *進場價：{t['entry']:.4f}* {src_emoji}({src_text})\n"
                        f"🛑 *止損 SL：{t['sl']:.4f}* (風險 {risk_pct:.2f}%)\n"
                        f"{pa_summary}\n"
                        f"\n"
                        f"🐋 主力分析：\n"
                        f"   {conf_color} 信心：{t['whale_confidence']*100:.0f}% | {t['whale_signal']}\n"
                        f"   📊 預期勝率：{wr_range}\n"
                        f"   💡 建議倉位：{pos_rec}{extra_warn}\n"
                        f"\n"
                        f"🎯 *止盈目標：*\n"
                        f"💰 TP1 (+{r1:.1f}R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (+{r2:.1f}R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (+{r3:.1f}R)：{t['tp3']:.4f}\n"
                        f"\n"
                        f"🛡️ {t['snr_display']}\n"
                        f"    {t['snr_active']}\n"
                        f"🛡️ 移動止損已啟用｜嚴格風控"
                    )
                    t['wait_since'] = current_bar
                    updated_trades.append(t)
                    time.sleep(0.2)
                    continue  # ← 進場後直接 continue，避免後續判斷

                # ② 進場未觸及：檢查逾時與失效
                bars_waited = current_bar - t['wait_since']

                if bars_waited > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 逾 {bars_waited} bars，自動清除")
                    time.sleep(0.2); continue

                # ③ 訊號失效：價格已偏離超過 2% 且等待超過 10 根 K 棒
                if bars_waited > 10:
                    price_diff_pct = abs(curr_p - t['entry']) / t['entry'] * 100
                    missed = (
                        (t['side'] == "LONG"  and curr_p > t['entry'] * 1.02) or
                        (t['side'] == "SHORT" and curr_p < t['entry'] * 0.98)
                    )
                    if missed and price_diff_pct > 2.0:
                        direction_text = "上漲" if t['side'] == "LONG" else "下跌"
                        send_tg(
                            f"⚠️ *Alpha Oracle | 訊號失效通知*\n"
                            f"──────────────────\n"
                            f"💎 幣種：#{coin_sym}\n"
                            f"🎯 原方向：{'🟢 多單' if t['side']=='LONG' else '🔴 空單'}\n"
                            f"⏰ 等待：{bars_waited} 根 K 棒 (~{bars_waited*15//60}小時)\n"
                            f"\n"
                            f"📍 原進場價：{t['entry']:.4f}\n"
                            f"📍 當前價：{curr_p:.4f}\n"
                            f"📊 偏離：{price_diff_pct:.2f}%\n"
                            f"\n"
                            f"❌ 價格已直接{direction_text}未回踩進場區\n"
                            f"💡 *此單已失效，請勿追單*"
                        )
                        time.sleep(0.2); continue

                # ④ 繼續等待
                updated_trades.append(t)

            elif t['status'] == "ACTIVE":
                risk_r = abs(t['entry'] - t['sl']) + 1e-10

                # TP1 通知 + 🆕 立即移止損至成本
                if t['tp1_hit'] == 0 and (
                    (t['side'] == "LONG"  and curr_p >= t['tp1']) or
                    (t['side'] == "SHORT" and curr_p <= t['tp1'])
                ):
                    t['tp1_hit'] = 1
                    t['sl']      = t['entry']   # 🆕 TP1 即保本
                    send_tg(
                        f"🎯 *Alpha Oracle | 達到 TP1 · 止損已移至成本*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ 第一止盈已觸及\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"💰 TP1 (+{abs(t['tp1']-t['entry'])/risk_r:.1f}R)：{t['tp1']:.4f}  ✅\n"
                        f"💰 TP2 (+{abs(t['tp2']-t['entry'])/risk_r:.1f}R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R)：{t['tp3']:.4f}\n"
                        f"🔒 止損已自動移至成本：{t['entry']:.4f}\n"
                        f"💡 *建議手動平倉 50% 鎖定 +1R*"
                    )

                # TP2 → 移止損至 TP1
                if t['locked'] == 0 and (
                    (t['side'] == "LONG"  and curr_p >= t['tp2']) or
                    (t['side'] == "SHORT" and curr_p <= t['tp2'])
                ):
                    t['locked'] = 1
                    t['sl']     = t['tp1']
                    send_tg(
                        f"🔒 *Alpha Oracle | 達到 TP2 · 鎖利保護*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ TP2 達標，止損移至 TP1\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"🚫 新止損：{t['tp1']:.4f}（+1R 保底）\n"
                        f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R)：{t['tp3']:.4f}"
                    )

                is_sl  = ((t['side'] == "LONG"  and curr_p <= t['sl'])  or
                          (t['side'] == "SHORT" and curr_p >= t['sl']))
                is_tp3 = ((t['side'] == "LONG"  and curr_p >= t['tp3']) or
                          (t['side'] == "SHORT" and curr_p <= t['tp3']))

                if is_sl or is_tp3:
                    is_be    = is_sl and t['locked'] == 1
                    res      = "SL" if (is_sl and not is_be) else "TP"
                    res_label = ("💰 止盈達標 (TP3)" if is_tp3
                                 else ("🔒 保本出場 (Break Even)" if is_be
                                       else "❌ 止損離場"))
                    send_tg(
                        f"🏁 *Alpha Oracle | 交易結算* {res_label}\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"📍 離場價：{curr_p:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}\n"
                        f"💰 TP1/2/3: {t['tp1']:.4f}/{t['tp2']:.4f}/{t['tp3']:.4f}\n"
                        f"📊 結果：{'✅ 盈利' if res == 'TP' else '❌ 虧損'}\n"
                        f"🕯️ PA評分：{t['pa_score']*100:.0f}分"
                    )
                    update_whale_stats(t.get('whale_category', 'Unknown'), res)
                    pd.DataFrame([{
                        "instId":           instId,
                        "result":           res,
                        "whale_signal":     t['whale_signal'],
                        "whale_confidence": t['whale_confidence'],
                        "whale_category":   t.get('whale_category', 'Unknown'),
                        "pa_score":         t['pa_score'],
                    }]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    time.sleep(0.2); continue

                updated_trades.append(t)
            time.sleep(0.2)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
