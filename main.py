#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v7.0 — SMC + Order Flow 全整合版
══════════════════════════════════════════════════════════════
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

import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime

# ─────────────────────────────────────────────────────────
# 1. 基礎配置
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

MAX_SIGNALS_PER_RUN   = int(os.getenv("MAX_SIGNALS", "8"))   # 提高上限配合多時框
SETUP_SCORE_THRESHOLD = 75                                     # 75 分進場

# 訂單流參數
CROSSLINE_BODY_RATIO         = 0.30   # 十字線：實體 < 30% 總範圍
SWEEP_VOLUME_RATIO           = 1.8    # 掃單：成交量 > 1.8 倍均量（從2.0放寬）
SWEEP_CONSECUTIVE_MOVES      = 2      # 掃單：連續移動 >= 2 根（從3放寬）
NEWS_COOLDOWN_MINUTES        = 60     # 新聞冷卻
ABSORPTION_VOL_MULTIPLIER    = 1.8    # 吸收：成交量 > 1.8 倍均量
ABSORPTION_PRICE_THRESHOLD   = 0.002  # 吸收：價格變動 < 0.2%

# 新聞冷卻追蹤
_news_cooldown: dict = {}   # {instId: timestamp}

# ─────────────────────────────────────────────────────────
# 2. 工具函數
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
# 3. 數據抓取
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
# 4. 基礎技術指標
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
# 5. 擺動點 & 結構
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
# 6. 流動性獵取（v6.0 Smart Money）
# ─────────────────────────────────────────────────────────
def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    """
    BSL / SSL 止損池 + EQH / EQL 等高低點
    掃除 = 刺穿後收回（主力引爆止損後逆轉）
    """
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
# 7. Order Block & FVG
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
# 8. Premium / Discount Zone
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
# 9. 訂單流模組（v7.0 新增）
# ─────────────────────────────────────────────────────────
def detect_crossline(df: pd.DataFrame, lookback: int = 15) -> dict | None:
    """
    十字線（Doji）偵測 — 多空分界定價中心
    實體 < 30% 總範圍 = 十字線
    """
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
    """
    主動掃單偵測 — 訂單流連續攻擊
    條件：近幾根K線方向一致 + 放量（非掛單，是真實成交）
    返回 (是否偵測, 強度分 0~1, 描述)
    """
    if len(df) < 8: return False, 0.0, "⚪ 數據不足"
    recent  = df.tail(8)
    vol_ma  = df["v"].tail(20).mean()
    last    = recent.iloc[-1]
    vol_sc  = last["v"] / (vol_ma + 1e-10)

    # 放量確認（必要條件）
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"⚪ 量能不足 ({vol_sc:.1f}x均量)"

    # 連續方向移動
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side=="LONG"  and recent["c"].iloc[i] > recent["c"].iloc[i-1]: moves += 1
        elif side=="SHORT" and recent["c"].iloc[i] < recent["c"].iloc[i-1]: moves += 1
        else: break

    if moves >= SWEEP_CONSECUTIVE_MOVES:
        strength = min(vol_sc / 3.0, 1.0)  # 量越大越強
        desc = f"⚡ 主動掃單確認！連續{moves}根+{vol_sc:.1f}x量能"
        return True, strength, desc

    return False, 0.0, f"⚪ 無連續掃單（方向根數={moves}）"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    """
    釣魚單過濾 — 無量價格移動（掛單引誘，非真實成交）
    條件：價格移動 ≥ 0.5% 但成交量 < 0.75倍均量
    """
    if len(df) < 6: return False
    recent    = df.tail(6)
    vol_ma    = df["v"].tail(20).mean()
    price_mv  = abs(recent["c"].iloc[-1] - recent["c"].iloc[0]) / (recent["c"].iloc[0]+1e-10)
    if price_mv < 0.005: return False
    last_vol  = recent["v"].iloc[-1]
    return last_vol < 0.75 * vol_ma   # 無量 = 釣魚單

def detect_absorption(df: pd.DataFrame, side: str) -> tuple:
    """
    吸收信號 — 大量成交但價格幾乎不動（主力換籌）
    條件：近3根K均量 > 1.8倍均量 且 價格變動 < 0.2%
    吸收後的掃單方向 = 主力累積的方向
    """
    if len(df) < 15: return False, "⚪ 無吸收"
    recent   = df.tail(5)
    vol_ma   = df["v"].tail(20).mean()
    avg_vol3 = recent["v"].iloc[-3:].mean()
    px_chg   = abs(recent["c"].iloc[-1] - recent["c"].iloc[-4]) / (recent["c"].iloc[-4]+1e-10)

    if avg_vol3 > ABSORPTION_VOL_MULTIPLIER*vol_ma and px_chg < ABSORPTION_PRICE_THRESHOLD:
        return True, f"🔄 吸收信號！量{avg_vol3/vol_ma:.1f}x均量但價格僅動{px_chg*100:.2f}%（主力換籌中）"
    return False, "⚪ 無明顯吸收"

def check_volume_breakout(df: pd.DataFrame) -> bool:
    """
    帶量止損驗證 — 突破時是否有量（無量突破=假突破）
    返回 True = 帶量有效突破；False = 無量假突破
    """
    if len(df) < 6: return True
    recent   = df.tail(6)
    vol_ma   = recent["v"].iloc[:-1].mean()
    last_vol = recent["v"].iloc[-1]
    return last_vol >= 1.5 * vol_ma

def check_news_cooldown(instId: str) -> bool:
    """新聞冷卻期檢查（True = 可交易，False = 冷卻中）"""
    now = time.time()
    if instId in _news_cooldown:
        if now - _news_cooldown[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False
    return True

def mark_news_event(instId: str):
    """標記新聞事件（呼叫此函數後60分鐘內不發訊號）"""
    _news_cooldown[instId] = time.time()
    logging.info(f"📰 News cooldown set for {instId}")

# ─────────────────────────────────────────────────────────
# 10. CVD / 多空比 / 資費 / 盤口解讀
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
# 11. 價格行為
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
# 12. 核心評分（兩條達 75 分路徑）
# ─────────────────────────────────────────────────────────
def calculate_score(params: dict) -> tuple:
    """
    ══ 評分權重 ══
    HTF         20 分
    OB/FVG      18 分（最高）
    流動性掃除  18 分（最高）
    主動掃單    12 分（v7 新增）
    十字線       8 分（v7 新增）
    吸收信號     7 分（v7 新增）
    CVD         12 分
    多空比       8 分
    資費         5 分
    盤口         5 分
    + BOS獎勵   +5 分
    + PD Zone   +5 分
    ──────────────
    上限 100 分  │ 進場門檻 75 分

    路徑A（SMC）  : HTF20+OB18+掃除18+CVD12+多空比8 = 76 ✅
    路徑B（訂單流）: HTF20+掃單12+十字8+吸收7+CVD12+多空比8+費率5+盤口5 = 77 ✅
    """
    sc = 0.0
    bd = []
    side = params["side"]

    # 1. HTF（20分）
    htf = params.get("htf_trend", "UNKNOWN")
    if htf == side:       sc+=20; bd.append("📈 HTF一致 +20")
    elif htf in ("NEUTRAL","UNKNOWN"): sc+=8; bd.append("⚪ HTF不明 +8")
    else:                 sc+=0;  bd.append("❌ HTF反向 +0")

    # 2. OB/FVG（0~18分）
    at_ob  = params.get("at_ob",  False)
    at_fvg = params.get("at_fvg", False)
    if at_ob and at_fvg: sc+=18; bd.append("🎯 OB+FVG +18")
    elif at_ob:          sc+=15; bd.append("🎯 在OB +15")
    elif at_fvg:         sc+=12; bd.append("🎯 在FVG +12")
    else:                sc+=0;  bd.append("⚪ 不在OB/FVG +0")

    # 3. 流動性掃除（0~18分）
    sw_sc = params.get("sweep_score", 0)
    p = round(sw_sc * 18)
    sc += p
    bd.append(f"💧 流動性掃除 +{p}" if sw_sc>0 else "⚪ 無掃除 +0")

    # 4. 主動掃單（0~13分）—— v7.0 新增（13分使路徑B純訂單流也能達75）
    as_sc = params.get("active_sweep_score", 0)
    p = round(as_sc * 13)
    sc += p
    bd.append(f"⚡ 主動掃單 +{p}" if as_sc>0 else "⚪ 無掃單 +0")

    # 5. 十字線（0~8分）—— v7.0 新增
    cl_sc = params.get("crossline_score", 0)
    p = round(cl_sc * 8)
    sc += p
    if cl_sc > 0: bd.append(f"🎯 十字線 +{p}")

    # 6. 吸收信號（0~7分）—— v7.0 新增
    ab_sc = params.get("absorption_score", 0)
    p = round(ab_sc * 7)
    sc += p
    if ab_sc > 0: bd.append(f"🔄 吸收 +{p}")

    # 7. CVD（0~12分）
    cvd_sc = params.get("cvd_score", 0)
    p = round(cvd_sc * 12)
    sc += p; bd.append(f"📊 CVD +{p}")

    # 8. 多空比（0~8分）
    ls_sc = params.get("ls_score", 0)
    p = round(ls_sc * 8)
    sc += p; bd.append(f"👥 多空比 +{p}")

    # 9. 資費（0~5分）
    fr_sc = params.get("fr_score", 0)
    p = round(fr_sc * 5)
    sc += p; bd.append(f"💸 資費 +{p}")

    # 10. 盤口（0~5分）
    ob_sc = params.get("ob_dir_score", 0)
    p = round(ob_sc * 5)
    sc += p; bd.append(f"📚 盤口 +{p}")

    # 獎勵分：BOS/CHoCH（+5）
    if params.get("bos_score", 0) >= 0.75:
        sc += 5; bd.append("🏗️ BOS/CHoCH +5")

    # 獎勵分：Premium/Discount（+5）
    if params.get("pd_score", 0) >= 0.7:
        sc += 5; bd.append("📍 P/D Zone +5")

    # ── 硬性扣分 ──────────────────────────────
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
# 13. 主掃描邏輯（雙時框）
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str,
                   htf_trend: str, fr: float, ls_f: float, ls_str: str,
                   ob_r: float, ob_raw_lb: str) -> list:
    """單一時框掃描（供雙時框呼叫）"""

    df = fetch_okx(instId, tf=tf, limit=150)
    if df is None or len(df) < 50:
        return []

    atr        = calculate_atr(df)
    _, st_lb   = calculate_supertrend(df)
    ema_sc, ema_lb = get_ema_bias(df, "LONG")   # 佔位，按 side 重新算

    # 訂單流模組（不依賴 side，先算好）
    crossline  = detect_crossline(df)
    abs_bool, abs_desc = detect_absorption(df, "LONG")   # 方向無關先計算

    opportunities = []

    for side in ["LONG", "SHORT"]:

        # ── 硬性過濾 1：HTF ────────────────────
        if htf_trend not in ("UNKNOWN","NEUTRAL") and htf_trend != side:
            continue

        # ── 硬性過濾 2：盤口 ───────────────────
        ob_dir_sc, ob_dir_lb = check_ob_direction(side, ob_r)
        if ob_dir_sc == 0.0:
            continue

        # ── 硬性過濾 3：資費 ───────────────────
        fr_sc, fr_lb = interpret_funding_rate(fr, side)
        if fr_sc == 0.0:
            continue

        # ── 硬性過濾 4：釣魚單 ─────────────────
        if detect_fishing_trap(df, side):
            logging.info(f"  [{instId}/{tf}/{side}] 釣魚單，跳過")
            continue

        # ── 分析模組 ────────────────────────────
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

        # 主動掃單
        as_bool, as_sc, as_desc = detect_active_sweep(df, side)

        # 十字線評分
        cl_sc = 0.0
        if crossline:
            pot = crossline["potential_side"]
            if pot == side or pot == "NEUTRAL":
                # 越近越分高；0根前=最高，10根前=較低
                dist_factor = max(0.0, 1.0 - crossline["distance"] / 10)
                cl_sc = 0.6 + 0.4 * dist_factor

        # 吸收評分
        ab_sc = 0.0
        if abs_bool:
            # 吸收後掃單方向 = 主力方向
            ab_sc = 0.8

        # ── 評分 ─────────────────────────────────
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

        # ── 進場價邏輯 ───────────────────────────
        # 優先順序：流動性掃除後立即 → OB/FVG中點 → 十字線 → 流動性池邊緣 → 當前價
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

        # 帶量突破驗證（無量突破降低信心但不強制跳過）
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
    """
    雙時框掃描（15m + 30m）
    共用數據抓取避免重複請求
    """
    # 共用行情（不依賴時框）
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

    # 去重：同 side 同時框只保留最高分
    seen = {}
    for opp in all_opps:
        key = f"{opp['side']}_{opp['tf']}"
        if key not in seen or opp["score"] > seen[key]["score"]:
            seen[key] = opp
    return list(seen.values())

# ─────────────────────────────────────────────────────────
# 14. 訊號格式化
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
        f"🔥 *Alpha Oracle v7.0* 🔥\n"
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

# ─────────────────────────────────────────────────────────
# 15. 主執行
# ─────────────────────────────────────────────────────────
def main():
    logging.info(f"🚀 Alpha Oracle v7.0  閾值={SETUP_SCORE_THRESHOLD}分  "
                 f"時框={SCAN_TIMEFRAMES}  上限={MAX_SIGNALS_PER_RUN}訊號")
    sent = 0

    for i, coin in enumerate(ALL_COINS, 1):
        if sent >= MAX_SIGNALS_PER_RUN:
            break
        logging.info(f"[{i}/{len(ALL_COINS)}] {coin} ...")

        # 新聞冷卻檢查
        if not check_news_cooldown(coin):
            logging.info(f"  [{coin}] 新聞冷卻期，跳過")
            continue

        try:
            opps = scan_for_opportunity(coin)
            if opps:
                # 按分數排序，優先發高分訊號
                opps.sort(key=lambda x: x["score"], reverse=True)
                logging.info(f"  ✅ {len(opps)} signal(s)")
                for opp in opps:
                    if sent >= MAX_SIGNALS_PER_RUN: break
                    msg = format_signal(opp)
                    if send_tg(msg):
                        sent += 1
                        logging.info(f"  📤 #{sent} [{opp['tf']}]{opp['side']} {opp['score']}分 {opp['grade']}")
                    time.sleep(1)
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"❌ {coin}: {e}")
            traceback.print_exc()

    logging.info(f"📊 完成，共發送 {sent} 訊號")
    return sent


if __name__ == "__main__":
    try:
        main(); exit(0)
    except Exception as e:
        logging.error(f"💥 Crash: {e}")
        traceback.print_exc()
        exit(1)
# ─────────────────────────────────────────────────────────
# 16. 信号跟踪与监控模块（新增）
# ─────────────────────────────────────────────────────────
import json
import os
from typing import Dict, List, Optional

# 信号存储文件
SIGNALS_FILE = "active_signals.json"

class SignalTracker:
    """信号跟踪器 - 监控TP到达和止损调整"""
    
    def __init__(self):
        self.active_signals: Dict[str, dict] = {}
        self.load_signals()
    
    def load_signals(self):
        """从文件加载活跃信号"""
        if os.path.exists(SIGNALS_FILE):
            try:
                with open(SIGNALS_FILE, 'r', encoding='utf-8') as f:
                    self.active_signals = json.load(f)
                logging.info(f"📂 加载了 {len(self.active_signals)} 个活跃信号")
            except Exception as e:
                logging.error(f"加载信号文件失败: {e}")
                self.active_signals = {}
    
    def save_signals(self):
        """保存活跃信号到文件"""
        try:
            with open(SIGNALS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.active_signals, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"保存信号文件失败: {e}")
    
    def add_signal(self, opp: dict):
        """添加新信号到跟踪列表"""
        signal_id = f"{opp['instId']}_{opp['side']}_{opp['tf']}"
        
        self.active_signals[signal_id] = {
            'instId': opp['instId'],
            'side': opp['side'],
            'tf': opp['tf'],
            'entry': opp['entry'],
            'sl': opp['sl'],
            'tp1': opp['tp1'],
            'tp2': opp['tp2'],
            'tp3': opp['tp3'],
            'current_sl': opp['sl'],  # 当前止损价（会动态调整）
            'tp1_reached': False,
            'tp2_reached': False,
            'tp3_reached': False,
            'sl_adjusted_to_entry': False,  # 是否已移至保本
            'sl_adjusted_to_tp1': False,    # 是否已移至TP1
            'created_at': datetime.now().isoformat(),
            'score': opp['score']
        }
        self.save_signals()
        logging.info(f"✅ 新增跟踪信号: {signal_id}")
    
    def remove_signal(self, signal_id: str):
        """移除已结束的信号"""
        if signal_id in self.active_signals:
            del self.active_signals[signal_id]
            self.save_signals()
            logging.info(f"❌ 移除信号: {signal_id}")
    
    def get_signal(self, instId: str, side: str, tf: str) -> Optional[dict]:
        """获取指定信号"""
        signal_id = f"{instId}_{side}_{tf}"
        return self.active_signals.get(signal_id)


# 全局信号跟踪器实例
signal_tracker = SignalTracker()


def format_tp_notification(opp: dict, tp_level: str, current_price: float) -> str:
    """格式化TP到达通知"""
    coin = opp['instId'].split('-')[0]
    e = "🟢" if opp['side'] == "LONG" else "🔴"
    st = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # 计算涨跌幅
    entry = opp['entry']
    pct_change = ((current_price - entry) / entry) * 100 if opp['side'] == "LONG" else ((entry - current_price) / entry) * 100
    
    # 获取TP价格
    tp_prices = {
        'TP1': opp['tp1'],
        'TP2': opp['tp2'],
        'TP3': opp['tp3']
    }
    
    # 确定止损调整信息
    sl_info = ""
    if tp_level == "TP1":
        new_sl = opp['entry']  # 移至保本
        sl_info = f"\n🛑 止损已移至成本 {new_sl:.4f}"
    elif tp_level == "TP2":
        new_sl = opp['tp1']  # 移至TP1
        sl_info = f"\n🛑 止损已移至 TP1 {new_sl:.4f}（锁利）"
    
    # 下一个目标
    next_tp = ""
    if tp_level == "TP1":
        next_tp = f"\n🎯 继续等 TP2：{opp['tp2']:.4f}\n🎯 最终 TP3：{opp['tp3']:.4f}"
    elif tp_level == "TP2":
        next_tp = f"\n🎯 继续持有等 TP3：{opp['tp3']:.4f}"
    
    return (
        f"🎯 *TP{tp_level[-1]} 到达！* 保本移损\n"
        f"══════════════════════\n"
        f"💎 #{coin}  {e} {st}\n"
        f"📊 评分 {opp['score']}分\n"
        f"──────────────────────\n"
        f"💰 进場：`{opp['entry']:.4f}`\n"
        f"📈 当前价：`{current_price:.4f}`  ({pct_change:+.2f}%)\n"
        f"✅ TP{tp_level[-1]}：`{tp_prices[tp_level]:.4f}`  已到\n"
        f"{sl_info}"
        f"{next_tp}"
        f"\n══════════════════════\n"
        f"💡 *继续持有，让利润奔跑！*"
    )


async def check_price_levels():
    """检查价格是否到达TP或SL水平"""
    if not signal_tracker.active_signals:
        return
    
    logging.info(f"🔍 检查 {len(signal_tracker.active_signals)} 个活跃信号...")
    
    for signal_id, signal in list(signal_tracker.active_signals.items()):
        try:
            # 获取最新价格
            df = fetch_okx(signal['instId'], tf="1m", limit=5)
            if df is None or len(df) == 0:
                continue
            
            current_price = df['c'].iloc[-1]
            side = signal['side']
            
            # 检查TP1
            if not signal['tp1_reached']:
                if (side == "LONG" and current_price >= signal['tp1']) or \
                   (side == "SHORT" and current_price <= signal['tp1']):
                    
                    signal['tp1_reached'] = True
                    signal['sl_adjusted_to_entry'] = True
                    signal['current_sl'] = signal['entry']  # 移至保本
                    
                    # 发送通知
                    opp_template = {
                        'instId': signal['instId'],
                        'side': signal['side'],
                        'tf': signal['tf'],
                        'entry': signal['entry'],
                        'tp1': signal['tp1'],
                        'tp2': signal['tp2'],
                        'tp3': signal['tp3'],
                        'score': signal['score']
                    }
                    msg = format_tp_notification(opp_template, "TP1", current_price)
                    send_tg(msg)
                    logging.info(f"✅ {signal['instId']} TP1到达！")
            
            # 检查TP2
            if not signal['tp2_reached'] and signal['tp1_reached']:
                if (side == "LONG" and current_price >= signal['tp2']) or \
                   (side == "SHORT" and current_price <= signal['tp2']):
                    
                    signal['tp2_reached'] = True
                    signal['sl_adjusted_to_tp1'] = True
                    signal['current_sl'] = signal['tp1']  # 移至TP1
                    
                    opp_template = {
                        'instId': signal['instId'],
                        'side': signal['side'],
                        'tf': signal['tf'],
                        'entry': signal['entry'],
                        'tp1': signal['tp1'],
                        'tp2': signal['tp2'],
                        'tp3': signal['tp3'],
                        'score': signal['score']
                    }
                    msg = format_tp_notification(opp_template, "TP2", current_price)
                    send_tg(msg)
                    logging.info(f"✅ {signal['instId']} TP2到达！")
            
            # 检查TP3
            if not signal['tp3_reached'] and signal['tp2_reached']:
                if (side == "LONG" and current_price >= signal['tp3']) or \
                   (side == "SHORT" and current_price <= signal['tp3']):
                    
                    signal['tp3_reached'] = True
                    
                    # TP3到达通知
                    coin = signal['instId'].split('-')[0]
                    e = "🟢" if side == "LONG" else "🔴"
                    msg = (
                        f"🎉 *TP3 到达！目标达成！*\n"
                        f"══════════════════════\n"
                        f"💎 #{coin}  {e} {signal['side']}\n"
                        f"📈 当前价：`{current_price:.4f}`\n"
                        f"✅ TP3：`{signal['tp3']:.4f}`  已到\n"
                        f"\n══════════════════════\n"
                        f"🏆 *全目标达成！建议平仓获利！*"
                    )
                    send_tg(msg)
                    logging.info(f"🎉 {signal['instId']} TP3到达！全目标达成！")
                    
                    # 移除信号（已完成）
                    signal_tracker.remove_signal(signal_id)
                    continue
            
            # 检查止损
            if current_price <= signal['current_sl'] if side == "LONG" else current_price >= signal['current_sl']:
                # 止损触发
                coin = signal['instId'].split('-')[0]
                msg = (
                    f"🛑 *止损触发*\n"
                    f"══════════════════════\n"
                    f"💎 #{coin}  {signal['side']}\n"
                    f"📉 当前价：`{current_price:.4f}`\n"
                    f"🛑 止损：`{signal['current_sl']:.4f}`\n"
                    f"\n══════════════════════\n"
                    f"⚠️ *已止损，注意风险控制！*"
                )
                send_tg(msg)
                signal_tracker.remove_signal(signal_id)
            
            # 保存更新
            signal_tracker.save_signals()
            
        except Exception as e:
            logging.error(f"检查信号 {signal_id} 失败: {e}")
            continue


async def monitor_signals_interval(minutes: int = 1):
    """定时监控信号（每分钟检查一次）"""
    while True:
        try:
            await check_price_levels()
            await asyncio.sleep(minutes * 60)
        except Exception as e:
            logging.error(f"监控循环错误: {e}")
            await asyncio.sleep(60)


# ─────────────────────────────────────────────────────────
# 17. 修改主函数以支持监控模式
# ─────────────────────────────────────────────────────────
async def main_with_monitoring():
    """主函数 - 包含信号监控"""
    import asyncio
    
    logging.info(f"🚀 Alpha Oracle v7.0 启动（含监控模式）")
    
    # 启动监控任务
    monitor_task = asyncio.create_task(monitor_signals_interval(minutes=1))
    
    try:
        while True:
            # 执行扫描
            sent = 0
            logging.info(f"\n{'='*50}")
            logging.info(f" 开始扫描...")
            
            for i, coin in enumerate(ALL_COINS, 1):
                if sent >= MAX_SIGNALS_PER_RUN:
                    break
                
                # 新闻冷却检查
                if not check_news_cooldown(coin):
                    continue
                
                try:
                    opps = scan_for_opportunity(coin)
                    if opps:
                        opps.sort(key=lambda x: x['score'], reverse=True)
                        for opp in opps:
                            if sent >= MAX_SIGNALS_PER_RUN:
                                break
                            
                            msg = format_signal(opp)
                            if send_tg(msg):
                                # 添加到跟踪列表
                                signal_tracker.add_signal(opp)
                                sent += 1
                                logging.info(f"📤 发送信号: {opp['instId']} {opp['side']}")
                            time.sleep(1)
                    
                    time.sleep(0.5)
                except Exception as e:
                    logging.error(f"❌ {coin}: {e}")
            
            logging.info(f"✅ 扫描完成，发送 {sent} 个信号")
            
            # 等待下一轮扫描（例如每5分钟扫描一次）
            await asyncio.sleep(300)
    
    except KeyboardInterrupt:
        logging.info("⛔ 用户中断，关闭程序...")
        monitor_task.cancel()
    except Exception as e:
        logging.error(f"💥 主循环错误: {e}")
        monitor_task.cancel()


# ─────────────────────────────────────────────────────────
# 主执行入口
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    
    try:
        # 使用异步监控模式
        asyncio.run(main_with_monitoring())
        exit(0)
    except Exception as e:
        logging.error(f"💥 Crash: {e}")
        traceback.print_exc()
        exit(1)
