#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v7.1 — SMC + Order Flow + 進場監控 + 勝率統計
══════════════════════════════════════════════════════════════
🆕 v7.1 新增功能：
  ✅ 進場檢測：價格到達進場價時自動通知
  ✅ TP/SL監控：自動追蹤止盈止損並發送通知
  ✅ 保本移損：TP1到達自動移至成本，TP2到達移至TP1
  ✅ 每日報告：00:00 自動發送當日勝率統計
  ✅ 每月報告：每月1號 00:00 發送月度勝率統計
  ✅ 數據持久化：自動保存交易歷史與統計數據

機會倍增設計：
  ✅ 雙時框掃描（15m + 30m）→ 每幣最多產生 2x 信號機會
  ✅ 兩條進場路徑（SMC路徑 / 訂單流路徑）達到 75 分即觸發
  ✅ 所有新功能均為「加分項」，不再做強制必要條件

══ 評分權重（總分 100 分，達 75 分進場）══
  HTF Supertrend      20 分
  OB / FVG 進場       18 分（最高）
  流動性池掃除        18 分（最高）  ← v6.0 Smart Money
  主動掃單（Tape）    12 分          ← v7.0 新增
  十字線定價中心       8 分          ← v7.0 新增
  吸收信號             7 分          ← v7.0 新增
  真實 CVD            12 分
  多空比逆向           8 分
  資金費率             5 分
  盤口方向             5 分
  BOS / CHoCH 獎勵    +5 分
  P/D Zone 獎勵       +5 分
  上限 100 分

══ 兩條達 75 分的路徑 ══
  路徑A（SMC）   : HTF(20)+OB(18)+流動性掃除(18)+CVD(12)+多空比(8) = 76 ✅
  路徑B（訂單流） : HTF(20)+掃單(12)+十字線(8)+吸收(7)+CVD(12)+多空比(8)+費率(5)+盤口(5) = 77 ✅

══ 硬性過濾（必須全部通過，否則不進評分）══
  1. 釣魚單（無量價格移動）  → 跳過
  2. 新聞冷卻期（60分鐘內）  → 跳過
  3. 1H HTF 方向強烈反向    → 跳過
  4. 盤口方向強烈反向        → 跳過
  5. 資金費率禁入            → 跳過
══════════════════════════════════════════════════════════════
"""

# ─────────────────────────────────────────────────────────
# 1. 導入模組
# ─────────────────────────────────────────────────────────
import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# ─────────────────────────────────────────────────────────
# 2. 基礎配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alpha_oracle_v7.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

# 雙時框掃描（≥15m，機會倍增）
SCAN_TIMEFRAMES = ["15m", "30m"]

MAX_SIGNALS_PER_RUN   = int(os.getenv("MAX_SIGNALS", "8"))
SETUP_SCORE_THRESHOLD = 75

# 訂單流參數
CROSSLINE_BODY_RATIO         = 0.30
SWEEP_VOLUME_RATIO           = 1.8
SWEEP_CONSECUTIVE_MOVES      = 2
NEWS_COOLDOWN_MINUTES        = 60
ABSORPTION_VOL_MULTIPLIER    = 1.8
ABSORPTION_PRICE_THRESHOLD   = 0.002

# 數據文件路徑
ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE  = "trade_history.json"
STATS_FILE          = "trading_stats.json"

# 新聞冷卻追蹤
_news_cooldown: dict = {}

# ─────────────────────────────────────────────────────────
# 3. 工具函數
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str) -> bool:
    if not TG_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Telegram Error: {e}")
        return False

# ─────────────────────────────────────────────────────────
# 4. 數據抓取
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
        logging.warning(f"[{instId}/{tf}] Fetch Error: {e}")
        return None

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=5
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
            f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}",
            timeout=5
        ).json()
        if res.get("code") != "0" or not res.get("data"):
            return 1.0, "⚪ 盤口均衡"
        data    = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio   = bid_vol / ask_vol
        if   ratio >= 1.30: label = f"🟢 買盤強勢 ({ratio:.2f})"
        elif ratio >= 1.05: label = f"🟡 買盤略強 ({ratio:.2f})"
        elif ratio >= 0.95: label = f"⚪ 盤口均衡 ({ratio:.2f})"
        elif ratio >= 0.77: label = f"🟡 賣盤略強 ({ratio:.2f})"
        else:               label = f"🔴 賣盤強勢 ({ratio:.2f})"
        return ratio, label
    except:
        return 1.0, "⚪ 盤口均衡"

# ─────────────────────────────────────────────────────────
# 5. 基礎技術指標
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

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple:
    if len(df) < period + 2:
        return 0, "⚪ 未知"
    high  = df["h"].values.astype(float)
    low   = df["l"].values.astype(float)
    close = df["c"].values.astype(float)
    n = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    atr_arr = np.zeros(n)
    atr_arr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr_arr[i] = (atr_arr[i-1]*(period-1) + tr[i]) / period
    hl2      = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr_arr
    basic_dn = hl2 + multiplier * atr_arr
    final_up = np.zeros(n); final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)
    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]
    for i in range(period+1, n):
        final_up[i] = basic_up[i] if basic_up[i]>final_up[i-1] or close[i-1]<final_up[i-1] else final_up[i-1]
        final_dn[i] = basic_dn[i] if basic_dn[i]<final_dn[i-1] or close[i-1]>final_dn[i-1] else final_dn[i-1]
        if   trend[i-1]==-1 and close[i]>final_dn[i-1]: trend[i]=1
        elif trend[i-1]==1  and close[i]<final_up[i-1]: trend[i]=-1
        else: trend[i]=trend[i-1]
    if trend[-1]==1:  return  1, "🟢 多頭"
    if trend[-1]==-1: return -1, "🔴 空頭"
    return 0, "⚪ 未知"

def get_htf_trend(instId: str) -> str:
    """1H Supertrend 高時框架方向"""
    df1h = fetch_okx(instId, tf="1H", limit=60)
    if df1h is None or len(df1h) < 15:
        return "UNKNOWN"
    v, _ = calculate_supertrend(df1h)
    if v ==  1: return "LONG"
    if v == -1: return "SHORT"
    return "NEUTRAL"

def get_ema_bias(df: pd.DataFrame, side: str) -> tuple:
    """21/55 EMA 多空偏向"""
    ema21 = calculate_ema(df["c"], 21).iloc[-1]
    ema55 = calculate_ema(df["c"], 55).iloc[-1]
    price = df["c"].iloc[-1]
    if side == "LONG":
        if price > ema21 > ema55: return 1.0, f"✅ 多頭排列 EMA21={ema21:.4f}"
        elif price > ema21:       return 0.5, f"🟡 價>EMA21 排列未完成"
        else:                     return 0.0, f"❌ 價格在EMA21下方"
    else:
        if price < ema21 < ema55: return 1.0, f"✅ 空頭排列 EMA21={ema21:.4f}"
        elif price < ema21:       return 0.5, f"🟡 價<EMA21 排列未完成"
        else:                     return 0.0, f"❌ 價格在EMA21上方"

# ─────────────────────────────────────────────────────────
# 6. 擺動點 & 結構
# ─────────────────────────────────────────────────────────
def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    data = df.tail(lookback).reset_index(drop=True)
    sh_p, sl_p, sh_i, sl_i = [], [], [], []
    for i in range(n, len(data)-n):
        wh = data["h"].iloc[i-n:i+n+1]
        wl = data["l"].iloc[i-n:i+n+1]
        if data["h"].iloc[i] == wh.max():
            sh_p.append(data["h"].iloc[i]); sh_i.append(i)
        if data["l"].iloc[i] == wl.min():
            sl_p.append(data["l"].iloc[i]); sl_i.append(i)
    return sh_p, sl_p, sh_i, sl_i

def detect_bos_choch(df: pd.DataFrame, side: str) -> tuple:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=80)
    price = df["c"].iloc[-1]
    atr   = calculate_atr(df)
    result, score = "⚪ 無明顯結構", 0.0
    if side == "LONG":
        if sl:
            last_sl = sl[-1]
            if df["l"].iloc[-4:-1].min() < last_sl - atr*0.1 and price > last_sl:
                result, score = f"✅ CHoCH 掃低反彈 @ {last_sl:.4f}（空頭陷阱）", 0.90
        if sh and not score:
            last_sh = sh[-1]
            if price > last_sh:
                result, score = f"✅ BOS 向上突破 {last_sh:.4f}", 0.80
            elif len(sh)>=2 and price>sh[-2]:
                result, score = f"🟡 CHoCH 潛在轉折 {sh[-2]:.4f}", 0.55
    else:
        if sh:
            last_sh = sh[-1]
            if df["h"].iloc[-4:-1].max() > last_sh + atr*0.1 and price < last_sh:
                result, score = f"✅ CHoCH 掃高回落 @ {last_sh:.4f}（多頭陷阱）", 0.90
        if sl and not score:
            last_sl = sl[-1]
            if price < last_sl:
                result, score = f"✅ BOS 向下跌破 {last_sl:.4f}", 0.80
            elif len(sl)>=2 and price<sl[-2]:
                result, score = f"🟡 CHoCH 潛在轉折 {sl[-2]:.4f}", 0.55
    return result, score

def detect_market_structure(df: pd.DataFrame, side: str) -> str:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=60)
    has_w = len(sl)>=2 and sl[-2]>0 and abs(sl[-2]-sl[-1])/sl[-2]<0.015
    has_m = len(sh)>=2 and sh[-2]>0 and abs(sh[-2]-sh[-1])/sh[-2]<0.015
    if side=="LONG":
        if has_w: return "W 底反轉 📐"
        if has_m: return "M 頭壓制 ⚠️"
    else:
        if has_m: return "M 頭反轉 📐"
        if has_w: return "W 底支撐 ⚠️"
    recent = df.tail(20)
    slope  = (recent["c"].iloc[-1]-recent["c"].iloc[0]) / (recent["c"].iloc[0]+1e-10)
    if slope > 0.025:  return "上升趨勢延續 📈"
    if slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

# ─────────────────────────────────────────────────────────
# 7. 流動性獵取（v6.0 Smart Money）
# ─────────────────────────────────────────────────────────
def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    sh, sl, _, _ = find_swing_points(df, n=2, lookback=lookback)
    price = df["c"].iloc[-1]
    atr   = calculate_atr(df)
    res   = dict(pools=[], sweep_detected=False, sweep_desc="",
                 sweep_score=0.0, eqh=None, eql=None,
                 nearest_bsl=None, nearest_ssl=None)

    for i in range(len(sh)-1, 0, -1):
        if abs(sh[i]-sh[i-1])/(sh[i-1]+1e-10) < 0.003:
            res["eqh"] = (sh[i-1]+sh[i])/2
            res["pools"].append(f"🔴 EQH等高 {res['eqh']:.4f}（BSL止損聚集）")
            break
    for i in range(len(sl)-1, 0, -1):
        if abs(sl[i]-sl[i-1])/(sl[i-1]+1e-10) < 0.003:
            res["eql"] = (sl[i-1]+sl[i])/2
            res["pools"].append(f"🟢 EQL等低 {res['eql']:.4f}（SSL止損聚集）")
            break

    bsl_c = [h for h in sh if h > price]
    ssl_c = [l for l in sl if l < price]
    if bsl_c: res["nearest_bsl"] = min(bsl_c)
    if ssl_c: res["nearest_ssl"] = max(ssl_c)

    recent = df.tail(5)
    if side == "LONG":
        check_levels = []
        if res["eql"]: check_levels.append((res["eql"], True))
        if res["nearest_ssl"]: check_levels.append((res["nearest_ssl"], False))
        for lvl, is_eq in check_levels:
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k = recent.iloc[i]
                if k["l"] < lvl - atr*0.05 and k["c"] > lvl:
                    wick = (lvl - k["l"]) / (atr+1e-10)
                    res["sweep_detected"] = True
                    pfx = "🔥 EQL" if is_eq else "✅ SSL"
                    res["sweep_desc"]  = f"{pfx}掃除反彈！低掃{k['l']:.4f}→收{k['c']:.4f}"
                    res["sweep_score"] = 0.95 if is_eq else min(0.55+wick*0.08, 0.90)
                    break
            if res["sweep_detected"]: break
    else:
        check_levels = []
        if res["eqh"]: check_levels.append((res["eqh"], True))
        if res["nearest_bsl"]: check_levels.append((res["nearest_bsl"], False))
        for lvl, is_eq in check_levels:
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k = recent.iloc[i]
                if k["h"] > lvl + atr*0.05 and k["c"] < lvl:
                    wick = (k["h"] - lvl) / (atr+1e-10)
                    res["sweep_detected"] = True
                    pfx = "🔥 EQH" if is_eq else "✅ BSL"
                    res["sweep_desc"]  = f"{pfx}掃除回落！高掃{k['h']:.4f}→收{k['c']:.4f}"
                    res["sweep_score"] = 0.95 if is_eq else min(0.55+wick*0.08, 0.90)
                    break
            if res["sweep_detected"]: break
    return res

# ─────────────────────────────────────────────────────────
# 8. Order Block & FVG
# ─────────────────────────────────────────────────────────
def find_order_blocks(df: pd.DataFrame, side: str, lookback: int = 60) -> list:
    data  = df.tail(lookback).reset_index(drop=True)
    obs   = []
    price = data["c"].iloc[-1]
    atr   = calculate_atr(data)
    for i in range(2, len(data)-3):
        c = data.iloc[i]
        if side == "LONG":
            if c["c"] < c["o"]:
                move = data["h"].iloc[i+1:i+4].max() - c["h"]
                if move > atr*1.5:
                    ob = dict(high=c["h"], low=c["l"], mid=(c["h"]+c["l"])/2,
                              strength=move/(atr+1e-10))
                    if ob["high"] < price*1.005: obs.append(ob)
        else:
            if c["c"] > c["o"]:
                move = c["l"] - data["l"].iloc[i+1:i+4].min()
                if move > atr*1.5:
                    ob = dict(high=c["h"], low=c["l"], mid=(c["h"]+c["l"])/2,
                              strength=move/(atr+1e-10))
                    if ob["low"] > price*0.995: obs.append(ob)
    obs.sort(key=lambda x: x["strength"], reverse=True)
    return obs[:2]

def find_fvg(df: pd.DataFrame, side: str, lookback: int = 40) -> list:
    data  = df.tail(lookback).reset_index(drop=True)
    fvgs  = []
    price = data["c"].iloc[-1]
    for i in range(2, len(data)):
        if side == "LONG":
            bot, top = data["h"].iloc[i-2], data["l"].iloc[i]
            if top > bot and bot < price:
                fvgs.append(dict(top=top, bottom=bot, mid=(top+bot)/2, size=top-bot))
        else:
            top, bot = data["l"].iloc[i-2], data["h"].iloc[i]
            if bot < top and top > price:
                fvgs.append(dict(top=top, bottom=bot, mid=(top+bot)/2, size=top-bot))
    return fvgs[-2:] if fvgs else []

def check_ob_fvg_entry(df: pd.DataFrame, side: str, atr: float) -> tuple:
    price    = df["c"].iloc[-1]
    obs      = find_order_blocks(df, side)
    fvgs     = find_fvg(df, side)
    at_ob    = at_fvg = False
    ob_desc  = "📍 無OB"
    fvg_desc = "📍 無FVG"
    entry_z  = price
    for ob in obs:
        tol = atr*0.5
        if ob["low"]-tol <= price <= ob["high"]+tol:
            at_ob   = True
            ob_desc = f"✅ 在OB [{ob['low']:.4f}~{ob['high']:.4f}] 強{ob['strength']:.1f}x"
            entry_z = ob["mid"]
            break
        else: ob_desc = f"📍 OB [{ob['low']:.4f}~{ob['high']:.4f}]"
    for fvg in reversed(fvgs):
        tol = atr*0.3
        if fvg["bottom"]-tol <= price <= fvg["top"]+tol:
            at_fvg   = True
            fvg_desc = f"✅ 在FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
            if not at_ob: entry_z = fvg["mid"]
            break
        else: fvg_desc = f"📍 FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
    return at_ob, at_fvg, ob_desc, fvg_desc, entry_z

# ─────────────────────────────────────────────────────────
# 9. Premium / Discount Zone
# ─────────────────────────────────────────────────────────
def detect_premium_discount(df: pd.DataFrame, side: str) -> tuple:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=50)
    price = df["c"].iloc[-1]
    if not sh or not sl: return "⚪ 無法判斷", 0.5
    hi  = max(sh[-2:]) if len(sh)>=2 else sh[-1]
    lo  = min(sl[-2:]) if len(sl)>=2 else sl[-1]
    rng = hi - lo
    if rng <= 0: return "⚪ 無法判斷", 0.5
    fib = (price - lo) / rng
    if side == "LONG":
        if fib <= 0.35:  return f"✅ Discount {fib*100:.0f}%（做多優質）", 1.0
        elif fib <= 0.5: return f"🟡 均衡偏低 {fib*100:.0f}%", 0.6
        elif fib <= 0.65:return f"🟡 均衡偏高 {fib*100:.0f}%", 0.3
        else:            return f"❌ Premium {fib*100:.0f}%（做多不利）", 0.0
    else:
        if fib >= 0.65:  return f"✅ Premium {fib*100:.0f}%（做空優質）", 1.0
        elif fib >= 0.5: return f"🟡 均衡偏高 {fib*100:.0f}%", 0.6
        elif fib >= 0.35:return f"🟡 均衡偏低 {fib*100:.0f}%", 0.3
        else:            return f"❌ Discount {fib*100:.0f}%（做空不利）", 0.0

# ─────────────────────────────────────────────────────────
# 10. 訂單流模組（v7.0 新增）
# ─────────────────────────────────────────────────────────
def detect_crossline(df: pd.DataFrame, lookback: int = 15) -> dict | None:
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k   = df.iloc[i]
        body = abs(k["c"] - k["o"])
        rng  = k["h"] - k["l"] + 1e-10
        if body < CROSSLINE_BODY_RATIO * rng:
            up_wick = k["h"] - max(k["c"], k["o"])
            dn_wick = min(k["c"], k["o"]) - k["l"]
            if   up_wick > dn_wick * 1.5: pot = "SHORT"
            elif dn_wick > up_wick * 1.5: pot = "LONG"
            else:                         pot = "NEUTRAL"
            dist_from_now = len(df) - 1 - i
            return dict(
                price=k["c"], high=k["h"], low=k["l"],
                body_ratio=body/rng,
                potential_side=pot,
                distance=dist_from_now,
                desc=f"🎯 十字線 @ {k['c']:.4f}（潛在：{pot}，{dist_from_now}根前）"
            )
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> tuple:
    if len(df) < 8: return False, 0.0, "⚪ 數據不足"
    recent  = df.tail(8)
    vol_ma  = df["v"].tail(20).mean()
    last    = recent.iloc[-1]
    vol_sc  = last["v"] / (vol_ma + 1e-10)
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"⚪ 量能不足 ({vol_sc:.1f}x均量)"
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side=="LONG"  and recent["c"].iloc[i] > recent["c"].iloc[i-1]: moves += 1
        elif side=="SHORT" and recent["c"].iloc[i] < recent["c"].iloc[i-1]: moves += 1
        else: break
    if moves >= SWEEP_CONSECUTIVE_MOVES:
        strength = min(vol_sc / 3.0, 1.0)
        desc = f"⚡ 主動掃單確認！連續{moves}根+{vol_sc:.1f}x量能"
        return True, strength, desc
    return False, 0.0, f"⚪ 無連續掃單（方向根數={moves}）"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df) < 6: return False
    recent    = df.tail(6)
    vol_ma    = df["v"].tail(20).mean()
    price_mv  = abs(recent["c"].iloc[-1] - recent["c"].iloc[0]) / (recent["c"].iloc[0]+1e-10)
    if price_mv < 0.005: return False
    last_vol  = recent["v"].iloc[-1]
    return last_vol < 0.75 * vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> tuple:
    if len(df) < 15: return False, "⚪ 無吸收"
    recent   = df.tail(5)
    vol_ma   = df["v"].tail(20).mean()
    avg_vol3 = recent["v"].iloc[-3:].mean()
    px_chg   = abs(recent["c"].iloc[-1] - recent["c"].iloc[-4]) / (recent["c"].iloc[-4]+1e-10)
    if avg_vol3 > ABSORPTION_VOL_MULTIPLIER*vol_ma and px_chg < ABSORPTION_PRICE_THRESHOLD:
        return True, f"🔄 吸收信號！量{avg_vol3/vol_ma:.1f}x均量但價格僅動{px_chg*100:.2f}%（主力換籌中）"
    return False, "⚪ 無明顯吸收"

def check_volume_breakout(df: pd.DataFrame) -> bool:
    if len(df) < 6: return True
    recent   = df.tail(6)
    vol_ma   = recent["v"].iloc[:-1].mean()
    last_vol = recent["v"].iloc[-1]
    return last_vol >= 1.5 * vol_ma

def check_news_cooldown(instId: str) -> bool:
    now = time.time()
    if instId in _news_cooldown:
        if now - _news_cooldown[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False
    return True

def mark_news_event(instId: str):
    _news_cooldown[instId] = time.time()
    logging.info(f"📰 News cooldown set for {instId}")

# ─────────────────────────────────────────────────────────
# 11. CVD / 多空比 / 資費 / 盤口解讀
# ─────────────────────────────────────────────────────────
def calculate_cvd(df: pd.DataFrame, periods: int = 50) -> tuple:
    data  = df.tail(periods).copy()
    delta = np.where(data["c"]>data["o"], data["v"],
                     np.where(data["c"]<data["o"], -data["v"], 0))
    cvd   = np.cumsum(delta)
    cur   = cvd[-1]
    slope = cur - (cvd[-10] if len(cvd)>=10 else cvd[0])
    if slope>0 and cur>0:  label, sc = f"🟢 買盤累積 CVD+{cur:,.0f}", 1.0
    elif slope>0 and cur<0:label, sc = f"🟡 CVD底部翻正（吸籌）", 0.65
    elif slope<0 and cur<0:label, sc = f"🔴 賣盤累積 CVD{cur:,.0f}", 1.0
    elif slope<0 and cur>0:label, sc = f"🟡 CVD頂部翻負（出貨）", 0.65
    else:                  label, sc = f"⚪ CVD持平", 0.3
    return cur, slope, label, sc

def interpret_ls_ratio(ratio: float, side: str) -> tuple:
    if   ratio>=2.5: senti=f"🔴 極度多頭擁擠({ratio:.2f})→逆向偏空"
    elif ratio>=1.8: senti=f"🟠 多頭擁擠({ratio:.2f})→謹慎做多"
    elif ratio>=1.2: senti=f"⚪ 略偏多頭({ratio:.2f})"
    elif ratio>=0.8: senti=f"⚪ 均衡({ratio:.2f})"
    elif ratio>=0.5: senti=f"🟠 空頭擁擠({ratio:.2f})→謹慎做空"
    else:            senti=f"🟢 極度空頭擁擠({ratio:.2f})→逆向偏多"
    if side=="LONG":
        sc = 1.0 if ratio<0.8 else (0.7 if ratio<1.2 else (0.4 if ratio<1.8 else 0.1))
    else:
        sc = 1.0 if ratio>2.0 else (0.7 if ratio>1.5 else (0.4 if ratio>1.0 else 0.1))
    return sc, senti

def interpret_funding_rate(fr: float, side: str) -> tuple:
    p = fr * 100
    if side=="LONG":
        if   fr<-0.0003: return 1.0, f"✅ 費率極佳{p:.4f}%（空頭付費）"
        elif fr< 0.0001: return 0.8, f"✅ 費率友善{p:.4f}%"
        elif fr< 0.0003: return 0.5, f"⚠️ 費率尚可{p:.4f}%"
        elif fr< 0.0008: return 0.2, f"❌ 費率不佳{p:.4f}%（多頭擁擠）"
        else:            return 0.0, f"🚫 費率禁入{p:.4f}%"
    else:
        if   fr> 0.0008: return 1.0, f"✅ 費率極佳{p:.4f}%（多頭付費）"
        elif fr> 0.0003: return 0.8, f"✅ 費率友善{p:.4f}%"
        elif fr> 0.0001: return 0.5, f"⚠️ 費率尚可{p:.4f}%"
        elif fr>-0.0003: return 0.2, f"❌ 費率不佳{p:.4f}%"
        else:            return 0.0, f"🚫 費率禁入{p:.4f}%"

def check_ob_direction(side: str, ob_ratio: float) -> tuple:
    if side=="LONG":
        if   ob_ratio>=1.30: return 1.0, f"✅ 盤口強力支撐做多({ob_ratio:.2f})"
        elif ob_ratio>=1.05: return 0.7, f"✅ 盤口略偏買盤({ob_ratio:.2f})"
        elif ob_ratio>=0.95: return 0.3, f"⚠️ 盤口均衡({ob_ratio:.2f})"
        else:                return 0.0, f"❌ 盤口偏空，做多風險！({ob_ratio:.2f})"
    else:
        if   ob_ratio<=0.77: return 1.0, f"✅ 盤口強力支撐做空({ob_ratio:.2f})"
        elif ob_ratio<=0.95: return 0.7, f"✅ 盤口略偏賣盤({ob_ratio:.2f})"
        elif ob_ratio<=1.05: return 0.3, f"⚠️ 盤口均衡({ob_ratio:.2f})"
        else:                return 0.0, f"❌ 盤口偏多，做空風險！({ob_ratio:.2f})"

# ─────────────────────────────────────────────────────────
# 12. 價格行為
# ─────────────────────────────────────────────────────────
def detect_price_action(df: pd.DataFrame, side: str) -> list:
    sigs = []
    for i in range(len(df)-1, max(len(df)-6, 0), -1):
        k   = df.iloc[i]
        body = abs(k["c"]-k["o"])
        rng  = k["h"]-k["l"]+1e-10
        uw   = k["h"]-max(k["c"],k["o"])
        dw   = min(k["c"],k["o"])-k["l"]
        bp   = body/rng
        if side=="SHORT" and uw>=body*2.0 and dw<=body*0.5:
            sigs.append(f"空頭流星線({min(uw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="LONG" and dw>=body*2.0 and uw<=body*0.5:
            sigs.append(f"多頭錘子線({min(dw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="SHORT" and uw/rng>0.40 and k["c"]<k["o"]:
            sigs.append(f"壓力拒絕(上影{uw/rng*100:.0f}%)@{k['c']:.4f}")
        if side=="LONG" and dw/rng>0.40 and k["c"]>k["o"]:
            sigs.append(f"支撐拒絕(下影{dw/rng*100:.0f}%)@{k['c']:.4f}")
        if bp>=0.70:
            if (side=="LONG" and k["c"]>k["o"]) or (side=="SHORT" and k["c"]<k["o"]):
                sigs.append(f"{'多' if side=='LONG' else '空'}頭動量棒({bp*100:.0f}%)@{k['c']:.4f}")
    return sigs[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
    sigs  = detect_price_action(df, side)
    sc    = 0.6 if len(sigs)>=3 else (0.4 if len(sigs)>=2 else (0.2 if sigs else 0.0))
    last  = df.iloc[-1]
    body  = abs(last["c"]-last["o"])
    rng   = last["h"]-last["l"]+1e-10
    if body/rng>0.70: sc+=0.20
    if (side=="LONG" and last["c"]>last["o"]) or (side=="SHORT" and last["c"]<last["o"]): sc+=0.20
    sc = min(sc, 1.0)
    lb = "✅ 強勢PA" if sc>=0.65 else ("⚠️ 中等PA" if sc>=0.40 else "⛔ 弱PA")
    return sc*100, lb, sigs

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    zones  = []
    vol_ma = df["v"].rolling(20).mean()
    vol_sd = df["v"].rolling(20).std()
    for i in range(max(len(df)-10,0), len(df)):
        if df["v"].iloc[i] > vol_ma.iloc[i]+2*vol_sd.iloc[i]:
            if df["c"].iloc[i]>df["o"].iloc[i] and side=="LONG":
                zones.append(f"🔵 主力吸籌 {df['c'].iloc[i]:.4f}")
            elif df["c"].iloc[i]<df["o"].iloc[i] and side=="SHORT":
                zones.append(f"🔴 主力派發 {df['c'].iloc[i]:.4f}")
    hi = df["h"].iloc[-20:].max(); lo = df["l"].iloc[-20:].min()
    zones.append(f"{'🔴 多頭清算' if side=='SHORT' else '🔵 空頭清算'} {hi if side=='SHORT' else lo:.4f}")
    return zones[:2]

# ─────────────────────────────────────────────────────────
# 13. 核心評分
# ─────────────────────────────────────────────────────────
def calculate_score(params: dict) -> tuple:
    sc = 0.0
    bd = []
    side = params["side"]

    htf = params.get("htf_trend", "UNKNOWN")
    if htf == side:       sc+=20; bd.append("📈 HTF一致 +20")
    elif htf in ("NEUTRAL","UNKNOWN"): sc+=8; bd.append("⚪ HTF不明 +8")
    else:                 sc+=0;  bd.append("❌ HTF反向 +0")

    at_ob  = params.get("at_ob",  False)
    at_fvg = params.get("at_fvg", False)
    if at_ob and at_fvg: sc+=18; bd.append("🎯 OB+FVG +18")
    elif at_ob:          sc+=15; bd.append("🎯 在OB +15")
    elif at_fvg:         sc+=12; bd.append("🎯 在FVG +12")
    else:                sc+=0;  bd.append("⚪ 不在OB/FVG +0")

    sw_sc = params.get("sweep_score", 0)
    p = round(sw_sc * 18)
    sc += p
    bd.append(f"💧 流動性掃除 +{p}" if sw_sc>0 else "⚪ 無掃除 +0")

    as_sc = params.get("active_sweep_score", 0)
    p = round(as_sc * 13)
    sc += p
    bd.append(f"⚡ 主動掃單 +{p}" if as_sc>0 else "⚪ 無掃單 +0")

    cl_sc = params.get("crossline_score", 0)
    p = round(cl_sc * 8)
    sc += p
    if cl_sc > 0: bd.append(f"🎯 十字線 +{p}")

    ab_sc = params.get("absorption_score", 0)
    p = round(ab_sc * 7)
    sc += p
    if ab_sc > 0: bd.append(f"🔄 吸收 +{p}")

    cvd_sc = params.get("cvd_score", 0)
    p = round(cvd_sc * 12)
    sc += p; bd.append(f"📊 CVD +{p}")

    ls_sc = params.get("ls_score", 0)
    p = round(ls_sc * 8)
    sc += p; bd.append(f"👥 多空比 +{p}")

    fr_sc = params.get("fr_score", 0)
    p = round(fr_sc * 5)
    sc += p; bd.append(f"💸 資費 +{p}")

    ob_sc = params.get("ob_dir_score", 0)
    p = round(ob_sc * 5)
    sc += p; bd.append(f"📚 盤口 +{p}")

    if params.get("bos_score", 0) >= 0.75:
        sc += 5; bd.append("🏗️ BOS/CHoCH +5")

    if params.get("pd_score", 0) >= 0.7:
        sc += 5; bd.append("📍 P/D Zone +5")

    if htf not in (side, "NEUTRAL", "UNKNOWN"):
        sc -= 15; bd.append("🚫 HTF逆勢 -15")
    if params.get("fr_score", 1) == 0.0:
        sc -= 10; bd.append("🚫 費率禁入 -10")
    if params.get("ob_dir_score", 1) == 0.0:
        sc -= 10; bd.append("🚫 盤口反向 -10")

    sc = max(0, min(round(sc), 100))

    if   sc >= 88: grade = "🏆 A+ 極強"
    elif sc >= 75: grade = "✅ A  強力"
    elif sc >= 65: grade = "⚠️ B+ 觀望"
    elif sc >= 55: grade = "⚠️ B  偏弱"
    else:          grade = "❌ C  跳過"

    return sc, grade, bd

# ─────────────────────────────────────────────────────────
# 14. 主掃描邏輯（雙時框）
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str,
                   htf_trend: str, fr: float, ls_f: float, ls_str: str,
                   ob_r: float, ob_raw_lb: str) -> list:
    df = fetch_okx(instId, tf=tf, limit=150)
    if df is None or len(df) < 50:
        return []

    atr        = calculate_atr(df)
    _, st_lb   = calculate_supertrend(df)

    crossline  = detect_crossline(df)
    abs_bool, abs_desc = detect_absorption(df, "LONG")

    opportunities = []

    for side in ["LONG", "SHORT"]:
        if htf_trend not in ("UNKNOWN","NEUTRAL") and htf_trend != side:
            continue

        ob_dir_sc, ob_dir_lb = check_ob_direction(side, ob_r)
        if ob_dir_sc == 0.0:
            continue

        fr_sc, fr_lb = interpret_funding_rate(fr, side)
        if fr_sc == 0.0:
            continue

        if detect_fishing_trap(df, side):
            logging.info(f"  [{instId}/{tf}/{side}] 釣魚單，跳過")
            continue

        cvd_cur, cvd_sl, cvd_lb, cvd_sc_raw = calculate_cvd(df)
        cvd_aligned = (side=="LONG" and cvd_sl>0) or (side=="SHORT" and cvd_sl<0)
        eff_cvd_sc  = cvd_sc_raw if cvd_aligned else cvd_sc_raw * 0.25

        liq              = find_liquidity_pools(df, side)
        bos_desc, bos_sc = detect_bos_choch(df, side)
        at_ob, at_fvg, ob_desc, fvg_desc, entry_z = check_ob_fvg_entry(df, side, atr)
        pd_lb, pd_sc     = detect_premium_discount(df, side)
        pa_sc, pa_lb, pa_sigs = calculate_pa_score(df, side)
        structure        = detect_market_structure(df, side)
        whale_zones      = detect_whale_zones(df, side)
        ls_sc, ls_lb     = interpret_ls_ratio(ls_f, side)
        _, ema_lb2       = get_ema_bias(df, side)

        as_bool, as_sc, as_desc = detect_active_sweep(df, side)

        cl_sc = 0.0
        if crossline:
            pot = crossline["potential_side"]
            if pot == side or pot == "NEUTRAL":
                dist_factor = max(0.0, 1.0 - crossline["distance"] / 10)
                cl_sc = 0.6 + 0.4 * dist_factor

        ab_sc = 0.0
        if abs_bool:
            ab_sc = 0.8

        params = dict(
            side=side, htf_trend=htf_trend,
            at_ob=at_ob, at_fvg=at_fvg,
            sweep_score=liq["sweep_score"],
            active_sweep_score=as_sc,
            crossline_score=cl_sc,
            absorption_score=ab_sc,
            cvd_score=eff_cvd_sc,
            ls_score=ls_sc,
            fr_score=fr_sc,
            ob_dir_score=ob_dir_sc,
            bos_score=bos_sc,
            pd_score=pd_sc,
        )
        score, grade, bd = calculate_score(params)

        if score < SETUP_SCORE_THRESHOLD:
            logging.info(f"  [{instId}/{tf}/{side}] {score}分 < {SETUP_SCORE_THRESHOLD}，跳過")
            continue

        price = df["c"].iloc[-1]
        if liq["sweep_detected"]:
            entry = price
        elif at_ob or at_fvg:
            entry = entry_z
        elif crossline:
            entry = crossline["low"] if side=="LONG" else crossline["high"]
        elif side=="LONG" and liq["nearest_ssl"]:
            entry = liq["nearest_ssl"] * 1.001
        elif side=="SHORT" and liq["nearest_bsl"]:
            entry = liq["nearest_bsl"] * 0.999
        else:
            entry = price

        sl   = entry - atr*1.5 if side=="LONG" else entry + atr*1.5
        risk = abs(entry - sl)
        tp1  = entry + risk       if side=="LONG" else entry - risk
        tp2  = entry + risk*2.5   if side=="LONG" else entry - risk*2.5
        tp3  = entry + risk*4.0   if side=="LONG" else entry - risk*4.0

        vol_ok   = check_volume_breakout(df)
        vol_warn = "" if vol_ok else "⚠️ 當前K線量能偏低，注意假突破"

        opp = dict(
            instId=instId, side=side, tf=tf,
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            price=price, atr=atr,
            structure=structure, bos_desc=bos_desc,
            at_ob=at_ob, at_fvg=at_fvg,
            ob_desc=ob_desc, fvg_desc=fvg_desc,
            pd_lb=pd_lb,
            liq=liq,
            crossline=crossline,
            as_bool=as_bool, as_desc=as_desc,
            abs_bool=abs_bool, abs_desc=abs_desc,
            cvd_lb=cvd_lb,
            ls_str=ls_str, ls_lb=ls_lb,
            fr_lb=fr_lb,
            ob_dir_lb=ob_dir_lb,
            ema_lb=ema_lb2,
            pa_sc=pa_sc, pa_lb=pa_lb, pa_sigs=pa_sigs,
            whale_zones=whale_zones,
            htf_trend=htf_trend, st_lb=st_lb,
            score=score, grade=grade, breakdown=bd,
            vol_warn=vol_warn,
            lev="10x~20x" if atr/price<0.015 else "3x~5x",
        )
        opportunities.append(opp)

    return opportunities

def scan_for_opportunity(instId: str) -> list:
    htf_trend      = get_htf_trend(instId)
    fr             = fetch_funding_rate(instId)
    ls_f, ls_str   = fetch_ls_ratio(instId)
    ob_r, ob_raw   = fetch_order_book(instId)

    all_opps = []
    for tf in SCAN_TIMEFRAMES:
        try:
            opps = scan_timeframe(instId, tf, htf_trend, fr, ls_f, ls_str, ob_r, ob_raw)
            all_opps.extend(opps)
        except Exception as e:
            logging.error(f"  [{instId}/{tf}] Error: {e}")

    seen = {}
    for opp in all_opps:
        key = f"{opp['side']}_{opp['tf']}"
        if key not in seen or opp["score"] > seen[key]["score"]:
            seen[key] = opp
    return list(seen.values())

# ─────────────────────────────────────────────────────────
# 15. 訊號格式化
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    coin   = opp["instId"].split("-")[0]
    e      = "🟢" if opp["side"]=="LONG" else "🔴"
    st     = "多單 (LONG)" if opp["side"]=="LONG" else "空單 (SHORT)"
    htf_e  = {"LONG":"🟢","SHORT":"🔴","NEUTRAL":"⚪","UNKNOWN":"⚪"}.get(opp["htf_trend"],"⚪")

    liq   = opp["liq"]
    sweep = liq["sweep_desc"] if liq["sweep_detected"] else "⚪ 近期無流動性掃除"
    bsl   = f"{liq['nearest_bsl']:.4f}" if liq["nearest_bsl"] else "─"
    ssl   = f"{liq['nearest_ssl']:.4f}" if liq["nearest_ssl"] else "─"
    eqh   = f"🔴 EQH {liq['eqh']:.4f}" if liq["eqh"] else "─"
    eql   = f"🟢 EQL {liq['eql']:.4f}" if liq["eql"] else "─"
    pools = "\n   ".join(liq["pools"][:2]) if liq["pools"] else "─"

    cl_txt = opp["crossline"]["desc"] if opp["crossline"] else "⚪ 無近期十字線"
    as_txt = opp["as_desc"] if opp["as_bool"] else "⚪ 無主動掃單"
    ab_txt = opp["abs_desc"] if opp["abs_bool"] else "⚪ 無吸收信號"

    pa_txt = "".join(f"   {s}\n" for s in opp["pa_sigs"][:3]) or "   ─ 無明顯PA\n"
    bd_txt = " │ ".join(opp["breakdown"][:5])

    vol_warn = f"\n⚠️ {opp['vol_warn']}" if opp.get("vol_warn") else ""

    return (
        f"🔥 *Alpha Oracle v7.1* 🔥\n"
        f"══════════════════════\n"
        f"💎 #{coin}  {e} {st}\n"
        f"⏰ {opp['tf']}  │  1H HTF: {htf_e} {opp['htf_trend']}\n"
        f"📊 評分 *{opp['score']}分* {opp['grade']}{vol_warn}\n"
        f"──────── 評分明細 ────────\n"
        f"   {bd_txt}\n"
        f"══════════════════════\n"
        f"💰 進場：`{opp['entry']:.4f}`\n"
        f"🛑 止損：`{opp['sl']:.4f}`  (-1R={opp['atr']*1.5:.4f})\n"
        f"🎯 TP1(1R)   ：`{opp['tp1']:.4f}`\n"
        f"🎯 TP2(2.5R) ：`{opp['tp2']:.4f}`\n"
        f"🎯 TP3(4R)   ：`{opp['tp3']:.4f}`\n"
        f"──────── 訂單流（v7）────\n"
        f"   {cl_txt}\n"
        f"   {as_txt}\n"
        f"   {ab_txt}\n"
        f"──────── 流動性獵取 ──────\n"
        f"💧 {sweep}\n"
        f"   🔴 BSL（上方止損池）: {bsl}\n"
        f"   🟢 SSL（下方止損池）: {ssl}\n"
        f"   等高/等低: {eqh}  {eql}\n"
        f"   止損池:\n   {pools}\n"
        f"──────── 進場結構 ────────\n"
        f"🏗️ 結構：{opp['structure']}\n"
        f"📐 BOS/CHoCH：{opp['bos_desc']}\n"
        f"🟦 {opp['ob_desc']}\n"
        f"🟩 {opp['fvg_desc']}\n"
        f"📍 P/D Zone：{opp['pd_lb']}\n"
        f"📉 EMA：{opp['ema_lb']}\n"
        f"──────── 市場情緒 ────────\n"
        f"🧬 CVD：{opp['cvd_lb']}\n"
        f"👥 多空比 {opp['ls_str']}：{opp['ls_lb']}\n"
        f"💸 資費：{opp['fr_lb']}\n"
        f"📚 盤口：{opp['ob_dir_lb']}\n"
        f"──────── 價格行為 ────────\n"
        f"🕯️ PA {opp['pa_lb']} {opp['pa_sc']:.0f}分\n"
        f"{pa_txt}"
        f"🐋 主力區：{'  │  '.join(opp['whale_zones']) or '─'}\n"
        f"📡 Supertrend：{opp['st_lb']}\n"
        f"══════════════════════\n"
        f"🕹️ 槓桿：{opp['lev']}  📌 {opp['tf']} 波段\n"
        f"💡 *{'流動性掃除後進場' if liq['sweep_detected'] else ('主動掃單確認' if opp['as_bool'] else '等待進場區回踩')}*"
    )

# ═══════════════════════════════════════════════════════════════
# 16. 進場監控與勝率統計模組（v7.1 新增）
# ═══════════════════════════════════════════════════════════════

class TradingTracker:
    """交易跟踪器 - 進場檢測/TP監控/勝率統計"""
    
    def __init__(self):
        self.active_signals: Dict[str, dict] = {}
        self.trade_history: List[dict] = []
        self.stats = {
            'daily': {},
            'monthly': {}
        }
        self.load_data()
    
    def load_data(self):
        """加載所有數據"""
        for fname, attr in [
            (ACTIVE_SIGNALS_FILE, 'active_signals'),
            (TRADE_HISTORY_FILE, 'trade_history'),
            (STATS_FILE, 'stats')
        ]:
            if os.path.exists(fname):
                try:
                    with open(fname, 'r', encoding='utf-8') as f:
                        setattr(self, attr, json.load(f))
                    logging.info(f"📂 已加載 {fname}")
                except Exception as e:
                    logging.error(f"加載 {fname} 失敗: {e}")
    
    def save_data(self):
        """保存所有數據"""
        for fname, attr in [
            (ACTIVE_SIGNALS_FILE, 'active_signals'),
            (TRADE_HISTORY_FILE, 'trade_history'),
            (STATS_FILE, 'stats')
        ]:
            try:
                with open(fname, 'w', encoding='utf-8') as f:
                    json.dump(getattr(self, attr), f, ensure_ascii=False, indent=2)
            except Exception as e:
                logging.error(f"保存 {fname} 失敗: {e}")
    
    def add_signal(self, opp: dict):
        """添加新信號（等待進場）"""
        signal_id = f"{opp['instId']}_{opp['side']}_{opp['tf']}_{datetime.now().strftime('%Y%m%d_%H%M')}"
        
        self.active_signals[signal_id] = {
            'signal_id': signal_id,
            'instId': opp['instId'],
            'side': opp['side'],
            'tf': opp['tf'],
            'entry': opp['entry'],
            'sl': opp['sl'],
            'tp1': opp['tp1'],
            'tp2': opp['tp2'],
            'tp3': opp['tp3'],
            'score': opp['score'],
            'grade': opp['grade'],
            'status': 'WAITING_ENTRY',
            'entry_price': None,
            'entry_time': None,
            'tp1_reached': False,
            'tp2_reached': False,
            'tp3_reached': False,
            'current_sl': opp['sl'],
            'sl_adjusted_to_entry': False,
            'sl_adjusted_to_tp1': False,
            'exit_price': None,
            'exit_time': None,
            'exit_reason': None,
            'pnl_pct': None,
            'created_at': datetime.now().isoformat(),
        }
        self.save_data()
        logging.info(f"✅ 新增等待進場信號: {signal_id} @ {opp['entry']:.4f}")
    
    def check_entry_filled(self, signal_id: str, current_price: float) -> bool:
        """檢查是否已進場"""
        if signal_id not in self.active_signals:
            return False
        
        signal = self.active_signals[signal_id]
        if signal['status'] != 'WAITING_ENTRY':
            return False
        
        side = signal['side']
        entry_price = signal['entry']
        
        is_filled = False
        if side == "LONG" and current_price <= entry_price:
            is_filled = True
        elif side == "SHORT" and current_price >= entry_price:
            is_filled = True
        
        if is_filled:
            signal['status'] = 'ACTIVE'
            signal['entry_price'] = current_price
            signal['entry_time'] = datetime.now().isoformat()
            
            self.send_entry_notification(signal, current_price)
            self.record_trade_start(signal)
            
            self.save_data()
            logging.info(f"✅ {signal_id} 已進場 @ {current_price:.4f}")
            return True
        
        return False
    
    def send_entry_notification(self, signal: dict, current_price: float):
        """發送進場提醒通知"""
        coin = signal['instId'].split('-')[0]
        e = "🟢" if signal['side'] == "LONG" else "🔴"
        st = "多" if signal['side'] == "LONG" else "空"
        
        entry = signal['entry']
        pct_change = ((current_price - entry) / entry) * 100 if signal['side'] == "LONG" else ((entry - current_price) / entry) * 100
        
        msg = (
            f"✅ *進場提醒 - #{coin}* {e} {st}\n"
            f"══════════════════════\n"
            f"📊 評分：{signal['score']}分 {signal['grade']}\n"
            f"⏰ 時框：{signal['tf']}\n"
            f"──────────────────────\n"
            f"💰 進場價：`{current_price:.4f}`\n"
            f"📋 計劃價：`{entry:.4f}` ({pct_change:+.2f}%)\n"
            f"🛑 止損：`{signal['sl']:.4f}`\n"
            f"🎯 TP1：`{signal['tp1']:.4f}`\n"
            f"🎯 TP2：`{signal['tp2']:.4f}`\n"
            f"🎯 TP3：`{signal['tp3']:.4f}`\n"
            f"══════════════════════\n"
            f"⏱️ 時間：{datetime.now().strftime('%H:%M:%S')}\n"
            f"💡 *已進場，祝你好運！*"
        )
        send_tg(msg)
    
    def check_price_levels(self, signal_id: str, current_price: float):
        """檢查TP/SL水平"""
        if signal_id not in self.active_signals:
            return
        
        signal = self.active_signals[signal_id]
        if signal['status'] != 'ACTIVE':
            return
        
        side = signal['side']
        entry_price = signal['entry_price'] or signal['entry']
        current_sl = signal['current_sl']
        
        # 檢查止損
        if (side == "LONG" and current_price <= current_sl) or \
           (side == "SHORT" and current_price >= current_sl):
            self.close_position(signal_id, current_price, "SL")
            return
        
        # 檢查TP1
        if not signal['tp1_reached']:
            if (side == "LONG" and current_price >= signal['tp1']) or \
               (side == "SHORT" and current_price <= signal['tp1']):
                
                signal['tp1_reached'] = True
                signal['sl_adjusted_to_entry'] = True
                signal['current_sl'] = entry_price
                
                self.send_tp_notification(signal, "TP1", current_price)
                self.save_data()
                return
        
        # 檢查TP2
        if not signal['tp2_reached'] and signal['tp1_reached']:
            if (side == "LONG" and current_price >= signal['tp2']) or \
               (side == "SHORT" and current_price <= signal['tp2']):
                
                signal['tp2_reached'] = True
                signal['sl_adjusted_to_tp1'] = True
                signal['current_sl'] = signal['tp1']
                
                self.send_tp_notification(signal, "TP2", current_price)
                self.save_data()
                return
        
        # 檢查TP3
        if not signal['tp3_reached'] and signal['tp2_reached']:
            if (side == "LONG" and current_price >= signal['tp3']) or \
               (side == "SHORT" and current_price <= signal['tp3']):
                
                signal['tp3_reached'] = True
                self.send_tp_notification(signal, "TP3", current_price)
                self.close_position(signal_id, current_price, "TP3")
                return
    
    def send_tp_notification(self, signal: dict, tp_level: str, current_price: float):
        """發送TP到達通知"""
        coin = signal['instId'].split('-')[0]
        e = "🟢" if signal['side'] == "LONG" else "🔴"
        st = "多" if signal['side'] == "LONG" else "空"
        
        entry = signal['entry_price'] or signal['entry']
        pct_change = ((current_price - entry) / entry) * 100 if signal['side'] == "LONG" else ((entry - current_price) / entry) * 100
        
        tp_prices = {'TP1': signal['tp1'], 'TP2': signal['tp2'], 'TP3': signal['tp3']}
        
        sl_info = ""
        if tp_level == "TP1":
            sl_info = f"\n🛑 止損已移至成本 {entry:.4f}"
        elif tp_level == "TP2":
            sl_info = f"\n🛑 止損已移至 TP1 {signal['tp1']:.4f}（鎖利）"
        
        next_tp = ""
        if tp_level == "TP1":
            next_tp = f"\n🎯 繼續等 TP2：{signal['tp2']:.4f}\n🏆 最終 TP3：{signal['tp3']:.4f}"
        elif tp_level == "TP2":
            next_tp = f"\n🎯 繼續持有等 TP3：{signal['tp3']:.4f}"
        
        msg = (
            f"🎯 *{tp_level} 到達！保本移損 - #{coin}*\n"
            f"══════════════════════\n"
            f"💎 {e} {st}單\n"
            f"📊 評分 {signal['score']}分\n"
            f"──────────────────────\n"
            f"💰 進場：`{entry:.4f}`\n"
            f"📈 當前價：`{current_price:.4f}`  ({pct_change:+.2f}%)\n"
            f"✅ {tp_level}：`{tp_prices[tp_level]:.4f}`  ✔️ 已到\n"
            f"{sl_info}"
            f"{next_tp}"
            f"\n══════════════════════\n"
            f"⏱️ 時間：{datetime.now().strftime('%H:%M')}\n"
            f"💡 *繼續持有，讓利潤奔跑！*"
        )
        send_tg(msg)
    
    def close_position(self, signal_id: str, exit_price: float, reason: str):
        """平倉並記錄結果"""
        if signal_id not in self.active_signals:
            return
        
        signal = self.active_signals[signal_id]
        entry_price = signal['entry_price'] or signal['entry']
        
        if signal['side'] == "LONG":
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100
        
        signal['status'] = 'COMPLETED'
        signal['exit_price'] = exit_price
        signal['exit_time'] = datetime.now().isoformat()
        signal['exit_reason'] = reason
        signal['pnl_pct'] = pnl_pct
        
        if pnl_pct > 0.1:
            result = 'win'
        elif pnl_pct < -0.1:
            result = 'loss'
        else:
            result = 'breakeven'
        
        trade_record = {
            'signal_id': signal_id,
            'instId': signal['instId'],
            'side': signal['side'],
            'tf': signal['tf'],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'exit_reason': reason,
            'pnl_pct': pnl_pct,
            'result': result,
            'entry_time': signal['entry_time'],
            'exit_time': signal['exit_time'],
            'score': signal['score']
        }
        self.trade_history.append(trade_record)
        
        self.record_trade_result(signal, result)
        self.send_exit_notification(signal, exit_price, reason, pnl_pct, result)
        
        del self.active_signals[signal_id]
        self.save_data()
        logging.info(f"✅ {signal_id} 已平倉 @ {exit_price:.4f} ({pnl_pct:+.2f}%)")
    
    def send_exit_notification(self, signal: dict, exit_price: float, reason: str, pnl_pct: float, result: str):
        """發送平倉通知"""
        coin = signal['instId'].split('-')[0]
        e = "🟢" if signal['side'] == "LONG" else "🔴"
        st = "多" if signal['side'] == "LONG" else "空"
        
        emoji = "🎉" if result == 'win' else ("💀" if result == 'loss' else "😐")
        reason_text = {
            'TP1': 'TP1止盈',
            'TP2': 'TP2止盈',
            'TP3': 'TP3止盈',
            'SL': '止損',
            'MANUAL': '手動平倉'
        }.get(reason, reason)
        
        msg = (
            f"{emoji} *平倉通知 - #{coin}* {e} {st}\n"
            f"══════════════════════\n"
            f"📊 評分：{signal['score']}分\n"
            f"💰 進場：`{signal['entry_price']:.4f}`\n"
            f"💵 出場：`{exit_price:.4f}`\n"
            f"📈 盈虧：`{pnl_pct:+.2f}%`\n"
            f"🎯 原因：{reason_text}\n"
            f"══════════════════════\n"
            f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"{'💡 繼續保持！' if result == 'win' else '💪 下次會更好！'}"
        )
        send_tg(msg)
    
    def record_trade_start(self, signal: dict):
        """記錄交易開始"""
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        month_str = now.strftime('%Y-%m')
        
        if date_str not in self.stats['daily']:
            self.stats['daily'][date_str] = {'total': 0, 'win': 0, 'loss': 0, 'breakeven': 0}
        if month_str not in self.stats['monthly']:
            self.stats['monthly'][month_str] = {'total': 0, 'win': 0, 'loss': 0, 'breakeven': 0}
        
        self.stats['daily'][date_str]['total'] += 1
        self.stats['monthly'][month_str]['total'] += 1
        self.save_data()
    
    def record_trade_result(self, signal: dict, result: str):
        """記錄交易結果"""
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        month_str = now.strftime('%Y-%m')
        
        if date_str in self.stats['daily']:
            self.stats['daily'][date_str][result] += 1
        if month_str in self.stats['monthly']:
            self.stats['monthly'][month_str][result] += 1
        self.save_data()
    
    def get_daily_stats(self, date_str: str = None) -> dict:
        if date_str is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
        return self.stats['daily'].get(date_str, {'total': 0, 'win': 0, 'loss': 0, 'breakeven': 0})
    
    def get_monthly_stats(self, month_str: str = None) -> dict:
        if month_str is None:
            month_str = datetime.now().strftime('%Y-%m')
        return self.stats['monthly'].get(month_str, {'total': 0, 'win': 0, 'loss': 0, 'breakeven': 0})
    
    def send_daily_report(self):
        """發送每日勝率報告"""
        today = datetime.now().strftime('%Y-%m-%d')
        stats = self.get_daily_stats(today)
        
        if stats['total'] == 0:
            msg = (
                f"📊 *每日交易報告*\n"
                f"══════════════════════\n"
                f"📅 日期：{today}\n"
                f"──────────────────────\n"
                f"😴 今日無交易\n"
                f"══════════════════════\n"
                f"💡 明天繼續努力！"
            )
            send_tg(msg)
            return
        
        win_rate = (stats['win'] / stats['total']) * 100
        
        today_trades = [t for t in self.trade_history if t['entry_time'] and t['entry_time'].startswith(today)]
        total_pnl = sum(t['pnl_pct'] or 0 for t in today_trades)
        
        msg = (
            f"📊 *每日交易報告*\n"
            f"══════════════════════\n"
            f"📅 日期：{today}\n"
            f"──────────────────────\n"
            f"📈 總交易：{stats['total']} 筆\n"
            f"✅ 盈利：{stats['win']} 筆\n"
            f"❌ 虧損：{stats['loss']} 筆\n"
            f"😐 保本：{stats['breakeven']} 筆\n"
            f"──────────────────────\n"
            f"🎯 勝率：`{win_rate:.1f}%`\n"
            f"💰 總盈虧：`{total_pnl:+.2f}%`\n"
            f"══════════════════════\n"
            f"{'🎉 表現優秀！' if win_rate >= 60 else ('💪 繼續努力！' if win_rate >= 40 else '📚 需要調整策略')}"
        )
        send_tg(msg)
        logging.info(f"📊 已發送每日報告: {today}")
    
    def send_monthly_report(self):
        """發送每月勝率報告"""
        this_month = datetime.now().strftime('%Y-%m')
        stats = self.get_monthly_stats(this_month)
        
        if stats['total'] == 0:
            msg = (
                f"📊 *每月交易報告*\n"
                f"══════════════════════\n"
                f"📅 月份：{this_month}\n"
                f"──────────────────────\n"
                f"😴 本月無交易\n"
                f"══════════════════════\n"
                f"💡 下月加油！"
            )
            send_tg(msg)
            return
        
        win_rate = (stats['win'] / stats['total']) * 100
        
        month_trades = [t for t in self.trade_history if t['entry_time'] and t['entry_time'].startswith(this_month)]
        total_pnl = sum(t['pnl_pct'] or 0 for t in month_trades)
        avg_pnl = total_pnl / len(month_trades) if month_trades else 0
        
        best_trade = max(month_trades, key=lambda x: x['pnl_pct']) if month_trades else None
        worst_trade = min(month_trades, key=lambda x: x['pnl_pct']) if month_trades else None
        
        msg = (
            f"📊 *每月交易報告*\n"
            f"══════════════════════\n"
            f"📅 月份：{this_month}\n"
            f"──────────────────────\n"
            f"📈 總交易：{stats['total']} 筆\n"
            f"✅ 盈利：{stats['win']} 筆\n"
            f"❌ 虧損：{stats['loss']} 筆\n"
            f"😐 保本：{stats['breakeven']} 筆\n"
            f"──────────────────────\n"
            f"🎯 勝率：`{win_rate:.1f}%`\n"
            f"💰 總盈虧：`{total_pnl:+.2f}%`\n"
            f"📊 平均盈虧：`{avg_pnl:+.2f}%`\n"
        )
        
        if best_trade:
            msg += f"\n🏆 最佳：#{best_trade['instId'].split('-')[0]} {best_trade['pnl_pct']:+.2f}%\n"
        if worst_trade:
            msg += f"💀 最差：#{worst_trade['instId'].split('-')[0]} {worst_trade['pnl_pct']:+.2f}%\n"
        
        msg += (
            f"══════════════════════\n"
            f"{'🎉 本月表現優秀！' if win_rate >= 60 else ('💪 繼續努力！' if win_rate >= 40 else '📚 需要調整策略')}"
        )
        send_tg(msg)
        logging.info(f"📊 已發送每月報告: {this_month}")


# 全局交易跟踪器實例
trading_tracker = TradingTracker()


async def monitor_prices():
    """持續監控價格（每分鐘檢查）"""
    logging.info("🔍 啟動價格監控...")
    
    while True:
        try:
            for signal_id, signal in list(trading_tracker.active_signals.items()):
                try:
                    df = fetch_okx(signal['instId'], tf="1m", limit=3)
                    if df is None or len(df) == 0:
                        continue
                    
                    current_price = df['c'].iloc[-1]
                    
                    if signal['status'] == 'WAITING_ENTRY':
                        trading_tracker.check_entry_filled(signal_id, current_price)
                    elif signal['status'] == 'ACTIVE':
                        trading_tracker.check_price_levels(signal_id, current_price)
                
                except Exception as e:
                    logging.error(f"監控信號 {signal_id} 錯誤: {e}")
                    continue
            
            await asyncio.sleep(60)
        
        except Exception as e:
            logging.error(f"價格監控循環錯誤: {e}")
            await asyncio.sleep(60)


async def daily_report_scheduler():
    """每日報告調度器（00:00發送）"""
    logging.info("⏰ 啟動每日報告調度器...")
    
    while True:
        try:
            now = datetime.now()
            if now.hour == 0 and now.minute == 0:
                trading_tracker.send_daily_report()
                await asyncio.sleep(300)
            else:
                await asyncio.sleep(60)
        
        except Exception as e:
            logging.error(f"每日報告調度錯誤: {e}")
            await asyncio.sleep(60)


async def monthly_report_scheduler():
    """每月報告調度器（每月1號00:00發送）"""
    logging.info("⏰ 啟動每月報告調度器...")
    
    while True:
        try:
            now = datetime.now()
            if now.day == 1 and now.hour == 0 and now.minute == 0:
                trading_tracker.send_monthly_report()
                await asyncio.sleep(300)
            else:
                await asyncio.sleep(3600)
        
        except Exception as e:
            logging.error(f"每月報告調度錯誤: {e}")
            await asyncio.sleep(3600)


# ─────────────────────────────────────────────────────────
# 17. 主函數（完整版）
# ─────────────────────────────────────────────────────────
async def main_with_full_monitoring():
    """主函數 - 包含完整監控和報告系統"""
    
    logging.info(f"🚀 Alpha Oracle v7.1 啟動（完整版）")
    logging.info(f"📊 閾值={SETUP_SCORE_THRESHOLD}分  時框={SCAN_TIMEFRAMES}")
    
    monitor_task = asyncio.create_task(monitor_prices())
    daily_task = asyncio.create_task(daily_report_scheduler())
    monthly_task = asyncio.create_task(monthly_report_scheduler())
    
    try:
        while True:
            sent = 0
            logging.info(f"\n{'='*50}")
            logging.info(f"開始掃描新信號...")
            
            for i, coin in enumerate(ALL_COINS, 1):
                if sent >= MAX_SIGNALS_PER_RUN:
                    break
                
                if not check_news_cooldown(coin):
                    continue
                
                try:
                    opps = scan_for_opportunity(coin)
                    if opps:
                        opps.sort(key=lambda x: x['score'], reverse=True)
                        logging.info(f"  ✅ {coin}: {len(opps)} 個機會")
                        
                        for opp in opps:
                            if sent >= MAX_SIGNALS_PER_RUN:
                                break
                            
                            msg = format_signal(opp)
                            if send_tg(msg):
                                trading_tracker.add_signal(opp)
                                sent += 1
                                logging.info(f"  📤 發送信號: {opp['instId']} {opp['side']} {opp['score']}分")
                            time.sleep(1)
                    
                    time.sleep(0.5)
                
                except Exception as e:
                    logging.error(f"❌ {coin} 掃描錯誤: {e}")
                    continue
            
            logging.info(f"✅ 掃描完成，發送 {sent} 個信號")
            await asyncio.sleep(300)
    
    except KeyboardInterrupt:
        logging.info("⛔ 用戶中斷，關閉程序...")
    except Exception as e:
        logging.error(f"💥 主循環錯誤: {e}")
    finally:
        monitor_task.cancel()
        daily_task.cancel()
        monthly_task.cancel()


# ─────────────────────────────────────────────────────────
# 主執行入口
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main_with_full_monitoring())
        exit(0)
    except Exception as e:
        logging.error(f"💥 Crash: {e}")
        traceback.print_exc()
        exit(1)
