#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v6.0 — Smart Money Concepts + Liquidity Engine
═══════════════════════════════════════════════════════════
新增模組：
  ✅ 流動性獵取  (BSL/SSL 止損池 · EQH/EQL 等高低 · 掃除偵測)
  ✅ BOS / CHoCH 市場結構突破 / 轉換
  ✅ Order Block (OB) 機構訂單塊識別
  ✅ Fair Value Gap (FVG) 不平衡缺口
  ✅ Premium / Discount Zone (Fibonacci 位置)
  ✅ 真實 CVD（累積成交量差）
  ✅ 多空比逆向判斷（散戶擁擠即反向）
  ✅ 資金費率明確分級（禁入 / 友善 / 中性）
  ✅ 盤口方向 = 交易方向（硬性過濾）
  ✅ 1H HTF Supertrend 趨勢過濾
  ✅ 21/55 EMA 多空確認
  ✅ 最低進場分數：75 分
═══════════════════════════════════════════════════════════
評分配重（100分）
  HTF趨勢一致     25 分
  OB / FVG        20 分
  流動性掃除      20 分
  真實CVD         15 分
  多空比（逆向）  10 分
  資金費率         5 分
  盤口方向         5 分
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
        logging.FileHandler("alpha_oracle_v6.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

MAX_SIGNALS_PER_RUN   = int(os.getenv("MAX_SIGNALS", "5"))
SETUP_SCORE_THRESHOLD = 75   # ★ 最低進場分數

# ─────────────────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str) -> bool:
    if not TG_TOKEN or not CHAT_ID: return False
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
def fetch_okx(instId: str, tf: str = "15m", limit: int = 200):
    """抓取 OKX K 線，返回時間升冪 DataFrame（舊→新）"""
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
        return df if len(df) >= 20 else None
    except Exception as e:
        logging.warning(f"[{instId}] Fetch Error: {e}")
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
    """返回 (float比值, 顯示字串)"""
    try:
        base = symbol.split("-")[0]
        res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio"
            f"?instId={base}",
            timeout=5
        ).json()
        if res.get("data"):
            r = float(res["data"][0]["ratio"])
            return r, f"{r:.2f}"
        return 1.0, "N/A"
    except:
        return 1.0, "N/A"

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple:
    """返回 (買賣比, 描述標籤)"""
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
        if   ratio >= 1.3:  label = f"🟢 買盤強勢 ({ratio:.2f})"
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

def get_ema_bias(df: pd.DataFrame, side: str) -> tuple:
    """
    21 EMA / 55 EMA 多空偏向
    做多：價格 > EMA21 > EMA55  ✅
    做空：價格 < EMA21 < EMA55  ✅
    """
    ema21 = calculate_ema(df["c"], 21).iloc[-1]
    ema55 = calculate_ema(df["c"], 55).iloc[-1]
    price = df["c"].iloc[-1]

    if side == "LONG":
        if price > ema21 > ema55:
            return 1.0, f"✅ 價>{ema21:.4f}(EMA21)>{ema55:.4f}(EMA55) 多頭排列"
        elif price > ema21:
            return 0.6, f"🟡 價>{ema21:.4f}(EMA21) 但EMA55未確認"
        else:
            return 0.0, f"❌ 價格在EMA21下方，多頭弱"
    else:
        if price < ema21 < ema55:
            return 1.0, f"✅ 價<{ema21:.4f}(EMA21)<{ema55:.4f}(EMA55) 空頭排列"
        elif price < ema21:
            return 0.6, f"🟡 價<{ema21:.4f}(EMA21) 但EMA55未確認"
        else:
            return 0.0, f"❌ 價格在EMA21上方，空頭弱"

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
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    hl2      = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr
    basic_dn = hl2 + multiplier * atr
    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)
    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]
    for i in range(period+1, n):
        final_up[i] = basic_up[i] if basic_up[i] > final_up[i-1] or close[i-1] < final_up[i-1] else final_up[i-1]
        final_dn[i] = basic_dn[i] if basic_dn[i] < final_dn[i-1] or close[i-1] > final_dn[i-1] else final_dn[i-1]
        if   trend[i-1] == -1 and close[i] > final_dn[i-1]: trend[i] =  1
        elif trend[i-1] ==  1 and close[i] < final_up[i-1]: trend[i] = -1
        else: trend[i] = trend[i-1]
    if trend[-1] ==  1: return  1, "🟢 多頭"
    if trend[-1] == -1: return -1, "🔴 空頭"
    return 0, "⚪ 未知"

def get_htf_trend(instId: str) -> str:
    """1H 高時框架 Supertrend 方向"""
    df1h = fetch_okx(instId, tf="1H", limit=60)
    if df1h is None or len(df1h) < 15:
        return "UNKNOWN"
    v, _ = calculate_supertrend(df1h)
    if v ==  1: return "LONG"
    if v == -1: return "SHORT"
    return "NEUTRAL"

# ─────────────────────────────────────────────────────────
# 5. 擺動點 & 市場結構
# ─────────────────────────────────────────────────────────
def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    """
    返回 (swing_highs[], swing_lows[], sh_idx[], sl_idx[])
    所有索引相對於 tail(lookback) 後的位置
    """
    data = df.tail(lookback).reset_index(drop=True)
    sh_p, sl_p, sh_i, sl_i = [], [], [], []
    for i in range(n, len(data) - n):
        wh = data["h"].iloc[i-n : i+n+1]
        wl = data["l"].iloc[i-n : i+n+1]
        if data["h"].iloc[i] == wh.max():
            sh_p.append(data["h"].iloc[i]); sh_i.append(i)
        if data["l"].iloc[i] == wl.min():
            sl_p.append(data["l"].iloc[i]); sl_i.append(i)
    return sh_p, sl_p, sh_i, sl_i

def detect_bos_choch(df: pd.DataFrame, side: str) -> tuple:
    """
    BOS  = Break of Structure（結構突破，趨勢延續）
    CHoCH = Change of Character（方向轉換，逆勢信號）
    返回 (描述字串, 分數 0~1)
    """
    sh, sl, sh_i, sl_i = find_swing_points(df, n=3, lookback=80)
    price   = df["c"].iloc[-1]
    atr     = calculate_atr(df)
    result  = "⚪ 無明顯結構"
    score   = 0.0

    if side == "LONG":
        # 最近3根K線的最低刺穿前低後收回 → 空頭陷阱 CHoCH
        if sl and len(sl) >= 1:
            last_sl = sl[-1]
            recent_lows = df["l"].iloc[-4:-1]
            if recent_lows.min() < last_sl - atr * 0.15 and price > last_sl:
                result = f"✅ CHoCH 掃低反彈（空頭陷阱）@ {last_sl:.4f}"
                score  = 0.90
        # 突破前高 → BOS 多頭延續
        if sh and not score:
            last_sh = sh[-1]
            if price > last_sh:
                result = f"✅ BOS 向上突破 {last_sh:.4f}（多頭確認）"
                score  = 0.80
            elif len(sh) >= 2 and price > sh[-2]:
                result = f"🟡 CHoCH 潛在多頭轉折（超過前前高 {sh[-2]:.4f}）"
                score  = 0.55
    else:  # SHORT
        # 最近3根K線的最高刺穿前高後收回 → 多頭陷阱 CHoCH
        if sh and len(sh) >= 1:
            last_sh = sh[-1]
            recent_highs = df["h"].iloc[-4:-1]
            if recent_highs.max() > last_sh + atr * 0.15 and price < last_sh:
                result = f"✅ CHoCH 掃高回落（多頭陷阱）@ {last_sh:.4f}"
                score  = 0.90
        # 跌破前低 → BOS 空頭延續
        if sl and not score:
            last_sl = sl[-1]
            if price < last_sl:
                result = f"✅ BOS 向下跌破 {last_sl:.4f}（空頭確認）"
                score  = 0.80
            elif len(sl) >= 2 and price < sl[-2]:
                result = f"🟡 CHoCH 潛在空頭轉折（跌破前前低 {sl[-2]:.4f}）"
                score  = 0.55

    return result, score

def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=60)
    has_w = len(sl) >= 2 and sl[-2] > 0 and abs(sl[-2]-sl[-1])/sl[-2] < 0.015
    has_m = len(sh) >= 2 and sh[-2] > 0 and abs(sh[-2]-sh[-1])/sh[-2] < 0.015
    if side == "LONG":
        if has_w: return "W 底反轉 📐"
        if has_m: return "M 頭壓制 ⚠️"
    elif side == "SHORT":
        if has_m: return "M 頭反轉 📐"
        if has_w: return "W 底支撐 ⚠️"
    if has_w: return "W 底反轉 📐"
    if has_m: return "M 頭反轉 📐"
    recent = df.tail(20)
    slope  = (recent["c"].iloc[-1] - recent["c"].iloc[0]) / (recent["c"].iloc[0] + 1e-10)
    if slope > 0.025:  return "上升趨勢延續 📈"
    if slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

# ─────────────────────────────────────────────────────────
# 6. 流動性獵取核心
# ─────────────────────────────────────────────────────────
def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    """
    ──────────────────────────────────────────
    BSL (Buy-Side Liquidity)  = 擺動高點上方的多頭止損聚集
    SSL (Sell-Side Liquidity) = 擺動低點下方的空頭止損聚集
    EQH (Equal Highs)  = 2個以上相近高點 → 更強的BSL
    EQL (Equal Lows)   = 2個以上相近低點 → 更強的SSL
    掃除 (Sweep)       = 價格刺穿流動性池後迅速反轉
                         做多：掃SSL後收回（空頭止損被觸發，空頭踏空）
                         做空：掃BSL後收回（多頭止損被觸發，多頭踏空）
    ──────────────────────────────────────────
    """
    sh, sl, sh_i, sl_i = find_swing_points(df, n=2, lookback=lookback)
    price = df["c"].iloc[-1]
    atr   = calculate_atr(df)

    result = {
        "pools":            [],
        "sweep_detected":   False,
        "sweep_desc":       "",
        "sweep_score":      0.0,
        "eqh":              None,
        "eql":              None,
        "nearest_bsl":      None,
        "nearest_ssl":      None,
    }

    # ── EQH（等高）識別 ──────────────────────
    for i in range(len(sh)-1, 0, -1):
        pct = abs(sh[i] - sh[i-1]) / (sh[i-1] + 1e-10)
        if pct < 0.003:                              # 0.3% 視為等高
            result["eqh"] = (sh[i-1] + sh[i]) / 2
            result["pools"].append(
                f"🔴 EQH等高 {result['eqh']:.4f}（BSL 多頭止損聚集）"
            )
            break

    # ── EQL（等低）識別 ──────────────────────
    for i in range(len(sl)-1, 0, -1):
        pct = abs(sl[i] - sl[i-1]) / (sl[i-1] + 1e-10)
        if pct < 0.003:
            result["eql"] = (sl[i-1] + sl[i]) / 2
            result["pools"].append(
                f"🟢 EQL等低 {result['eql']:.4f}（SSL 空頭止損聚集）"
            )
            break

    # ── 最近流動性池位置 ─────────────────────
    bsl_cands = [h for h in sh if h > price]
    ssl_cands = [l for l in sl if l < price]
    if bsl_cands: result["nearest_bsl"] = min(bsl_cands)
    if ssl_cands: result["nearest_ssl"] = max(ssl_cands)

    # ── 流動性掃除偵測（最近5根K線）──────────
    recent = df.tail(5)

    if side == "LONG":
        # 場景A：掃普通SSL後反彈
        if result["nearest_ssl"]:
            ssl = result["nearest_ssl"]
            for i in range(len(recent)-1, max(len(recent)-5, 0), -1):
                k = recent.iloc[i]
                if k["l"] < ssl - atr * 0.1 and k["c"] > ssl:
                    wick = (ssl - k["l"]) / (atr + 1e-10)
                    result["sweep_detected"] = True
                    result["sweep_desc"]     = (
                        f"✅ SSL掃除反彈！刺穿 {k['l']:.4f} 回收至 {k['c']:.4f}"
                        f"（空頭止損引爆，空方踏空）"
                    )
                    result["sweep_score"] = min(0.55 + wick * 0.08, 1.0)
                    break
        # 場景B：掃EQL後反彈（更強信號）
        if result["eql"] and not result["sweep_detected"]:
            eql = result["eql"]
            for i in range(len(recent)-1, max(len(recent)-5, 0), -1):
                k = recent.iloc[i]
                if k["l"] < eql - atr * 0.05 and k["c"] > eql:
                    result["sweep_detected"] = True
                    result["sweep_desc"]     = (
                        f"🔥 EQL掃除！等低 {eql:.4f} 被掃後強力反彈（極強多頭信號）"
                    )
                    result["sweep_score"] = 0.95
                    break

    else:  # SHORT
        # 場景A：掃普通BSL後回落
        if result["nearest_bsl"]:
            bsl = result["nearest_bsl"]
            for i in range(len(recent)-1, max(len(recent)-5, 0), -1):
                k = recent.iloc[i]
                if k["h"] > bsl + atr * 0.1 and k["c"] < bsl:
                    wick = (k["h"] - bsl) / (atr + 1e-10)
                    result["sweep_detected"] = True
                    result["sweep_desc"]     = (
                        f"✅ BSL掃除回落！刺穿 {k['h']:.4f} 回收至 {k['c']:.4f}"
                        f"（多頭止損引爆，多方踏空）"
                    )
                    result["sweep_score"] = min(0.55 + wick * 0.08, 1.0)
                    break
        # 場景B：掃EQH後回落（更強信號）
        if result["eqh"] and not result["sweep_detected"]:
            eqh = result["eqh"]
            for i in range(len(recent)-1, max(len(recent)-5, 0), -1):
                k = recent.iloc[i]
                if k["h"] > eqh + atr * 0.05 and k["c"] < eqh:
                    result["sweep_detected"] = True
                    result["sweep_desc"]     = (
                        f"🔥 EQH掃除！等高 {eqh:.4f} 被掃後強力回落（極強空頭信號）"
                    )
                    result["sweep_score"] = 0.95
                    break

    return result

# ─────────────────────────────────────────────────────────
# 7. Order Block & FVG
# ─────────────────────────────────────────────────────────
def find_order_blocks(df: pd.DataFrame, side: str, lookback: int = 60) -> list:
    """
    多頭OB = 強勢上漲前的最後一根空頭K線（機構在此買入）
    空頭OB = 強勢下跌前的最後一根多頭K線（機構在此賣出）
    """
    data  = df.tail(lookback).reset_index(drop=True)
    obs   = []
    price = data["c"].iloc[-1]
    atr   = calculate_atr(data)

    for i in range(2, len(data)-3):
        c = data.iloc[i]
        if side == "LONG":
            if c["c"] < c["o"]:                           # 空頭K線
                fut_high = data["h"].iloc[i+1:i+4].max()
                move     = fut_high - c["h"]
                if move > atr * 1.5:
                    ob = dict(high=c["h"], low=c["l"],
                              mid=(c["h"]+c["l"])/2,
                              strength=move/(atr+1e-10))
                    if ob["high"] < price * 1.005:
                        obs.append(ob)
        else:
            if c["c"] > c["o"]:                           # 多頭K線
                fut_low = data["l"].iloc[i+1:i+4].min()
                move    = c["l"] - fut_low
                if move > atr * 1.5:
                    ob = dict(high=c["h"], low=c["l"],
                              mid=(c["h"]+c["l"])/2,
                              strength=move/(atr+1e-10))
                    if ob["low"] > price * 0.995:
                        obs.append(ob)

    obs.sort(key=lambda x: x["strength"], reverse=True)
    return obs[:2]

def find_fvg(df: pd.DataFrame, side: str, lookback: int = 40) -> list:
    """
    多頭FVG：K[i].low > K[i-2].high （向上跳空，未成交區間）
    空頭FVG：K[i].high < K[i-2].low （向下跳空，未成交區間）
    """
    data  = df.tail(lookback).reset_index(drop=True)
    fvgs  = []
    price = data["c"].iloc[-1]

    for i in range(2, len(data)):
        if side == "LONG":
            bot = data["h"].iloc[i-2]
            top = data["l"].iloc[i]
            if top > bot and bot < price:
                fvgs.append(dict(top=top, bottom=bot,
                                 mid=(top+bot)/2, size=top-bot))
        else:
            top = data["l"].iloc[i-2]
            bot = data["h"].iloc[i]
            if bot < top and top > price:
                fvgs.append(dict(top=top, bottom=bot,
                                 mid=(top+bot)/2, size=top-bot))

    return fvgs[-2:] if fvgs else []

def check_ob_fvg_entry(df: pd.DataFrame, side: str, atr: float) -> tuple:
    """
    判斷當前價格是否在 OB 或 FVG 區域（容忍 ±0.5 ATR）
    返回 (at_ob, at_fvg, ob_desc, fvg_desc, best_entry_price)
    """
    price      = df["c"].iloc[-1]
    obs        = find_order_blocks(df, side)
    fvgs       = find_fvg(df, side)
    at_ob      = at_fvg = False
    ob_desc    = "📍 無OB（等回踩）"
    fvg_desc   = "📍 無FVG（等填補）"
    entry_zone = price

    for ob in obs:
        tol = atr * 0.5
        if ob["low"] - tol <= price <= ob["high"] + tol:
            at_ob      = True
            ob_desc    = f"✅ 在OB [{ob['low']:.4f}~{ob['high']:.4f}] 強度{ob['strength']:.1f}x"
            entry_zone = ob["mid"]
            break
        else:
            ob_desc = f"📍 OB [{ob['low']:.4f}~{ob['high']:.4f}]（等回踩）"

    for fvg in reversed(fvgs):
        tol = atr * 0.3
        if fvg["bottom"] - tol <= price <= fvg["top"] + tol:
            at_fvg   = True
            fvg_desc = f"✅ 在FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
            if not at_ob:
                entry_zone = fvg["mid"]
            break
        else:
            fvg_desc = f"📍 FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]（等填補）"

    return at_ob, at_fvg, ob_desc, fvg_desc, entry_zone

# ─────────────────────────────────────────────────────────
# 8. Premium / Discount Zone
# ─────────────────────────────────────────────────────────
def detect_premium_discount(df: pd.DataFrame, side: str, lookback: int = 50) -> tuple:
    """
    以最近擺動高低點為區間，計算 Fibonacci 位置
    Discount（≤35%）= 適合做多
    Premium  （≥65%）= 適合做空
    返回 (標籤, 分數 0~1, fib_pos)
    """
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=lookback)
    price = df["c"].iloc[-1]
    if not sh or not sl:
        return "⚪ 無法判斷", 0.5, 0.5
    hi  = max(sh[-2:]) if len(sh) >= 2 else sh[-1]
    lo  = min(sl[-2:]) if len(sl) >= 2 else sl[-1]
    rng = hi - lo
    if rng <= 0:
        return "⚪ 無法判斷", 0.5, 0.5
    fib = (price - lo) / rng

    if side == "LONG":
        if fib <= 0.35:  label, sc = f"✅ Discount {fib*100:.0f}%（做多優質區）",  1.0
        elif fib <= 0.5: label, sc = f"🟡 均衡偏低 {fib*100:.0f}%",              0.6
        elif fib <= 0.65:label, sc = f"🟡 均衡偏高 {fib*100:.0f}%（謹慎）",      0.3
        else:            label, sc = f"❌ Premium  {fib*100:.0f}%（做多不利）",   0.0
    else:
        if fib >= 0.65:  label, sc = f"✅ Premium  {fib*100:.0f}%（做空優質區）", 1.0
        elif fib >= 0.5: label, sc = f"🟡 均衡偏高 {fib*100:.0f}%",              0.6
        elif fib >= 0.35:label, sc = f"🟡 均衡偏低 {fib*100:.0f}%（謹慎）",      0.3
        else:            label, sc = f"❌ Discount {fib*100:.0f}%（做空不利）",   0.0
    return label, sc, fib

# ─────────────────────────────────────────────────────────
# 9. CVD / 多空比 / 資費 / 盤口
# ─────────────────────────────────────────────────────────
def calculate_cvd(df: pd.DataFrame, periods: int = 50) -> tuple:
    """
    真實 CVD（累積成交量差）
    多頭K線 → +volume；空頭K線 → -volume
    返回 (cvd當前值, slope近10根斜率, 標籤, 分數0~1)
    """
    data  = df.tail(periods).copy()
    delta = np.where(
        data["c"] > data["o"],  data["v"],
        np.where(data["c"] < data["o"], -data["v"], 0)
    )
    cvd     = np.cumsum(delta)
    cur     = cvd[-1]
    base    = cvd[-10] if len(cvd) >= 10 else cvd[0]
    slope   = cur - base

    if slope > 0 and cur > 0:
        label, score = f"🟢 買盤持續累積 CVD+{cur:,.0f}", 1.0
    elif slope > 0 and cur < 0:
        label, score = f"🟡 CVD底部翻正（潛在吸籌）CVD{cur:,.0f}", 0.65
    elif slope < 0 and cur < 0:
        label, score = f"🔴 賣盤持續累積 CVD{cur:,.0f}", 1.0
    elif slope < 0 and cur > 0:
        label, score = f"🟡 CVD頂部翻負（潛在出貨）CVD+{cur:,.0f}", 0.65
    else:
        label, score = f"⚪ CVD持平 CVD{cur:,.0f}", 0.3

    return cur, slope, label, score

def interpret_ls_ratio(ratio: float, side: str) -> tuple:
    """
    逆向多空比（散戶站哪邊就往反方向看）
    ratio = 做多帳戶 / 做空帳戶
    返回 (分數 0~1, 情緒描述)
    """
    if   ratio >= 2.5: senti = f"🔴 極度多頭擁擠 ({ratio:.2f}) → 逆向偏空"
    elif ratio >= 1.8: senti = f"🟠 多頭擁擠 ({ratio:.2f}) → 謹慎做多"
    elif ratio >= 1.2: senti = f"⚪ 略偏多頭 ({ratio:.2f})"
    elif ratio >= 0.8: senti = f"⚪ 均衡 ({ratio:.2f})"
    elif ratio >= 0.5: senti = f"🟠 空頭擁擠 ({ratio:.2f}) → 謹慎做空"
    else:              senti = f"🟢 極度空頭擁擠 ({ratio:.2f}) → 逆向偏多"

    if side == "LONG":
        if   ratio < 0.8:  sc = 1.0
        elif ratio < 1.2:  sc = 0.7
        elif ratio < 1.8:  sc = 0.4
        else:              sc = 0.1
    else:
        if   ratio > 2.0:  sc = 1.0
        elif ratio > 1.5:  sc = 0.7
        elif ratio > 1.0:  sc = 0.4
        else:              sc = 0.1

    return sc, senti

def interpret_funding_rate(fr: float, side: str) -> tuple:
    """
    資金費率明確分級
    正費率：多頭付錢給空頭；負費率：空頭付錢給多頭
    返回 (分數 0~1, 標籤)
    """
    p = fr * 100
    if side == "LONG":
        if   fr < -0.0003: sc, lb = 1.0, f"✅ 費率極佳 {p:.4f}%（空頭反向付費）"
        elif fr <  0.0001: sc, lb = 0.8, f"✅ 費率友善 {p:.4f}%（近中性）"
        elif fr <  0.0003: sc, lb = 0.5, f"⚠️ 費率尚可 {p:.4f}%"
        elif fr <  0.0008: sc, lb = 0.2, f"❌ 費率不佳 {p:.4f}%（多頭擁擠，成本高）"
        else:              sc, lb = 0.0, f"🚫 費率禁入 {p:.4f}%（過度多頭）"
    else:
        if   fr >  0.0008: sc, lb = 1.0, f"✅ 費率極佳 {p:.4f}%（多頭擁擠付費給你）"
        elif fr >  0.0003: sc, lb = 0.8, f"✅ 費率友善 {p:.4f}%"
        elif fr >  0.0001: sc, lb = 0.5, f"⚠️ 費率尚可 {p:.4f}%"
        elif fr > -0.0003: sc, lb = 0.2, f"❌ 費率不佳 {p:.4f}%（空頭成本高）"
        else:              sc, lb = 0.0, f"🚫 費率禁入 {p:.4f}%（過度空頭）"
    return sc, lb

def check_ob_direction(side: str, ob_ratio: float) -> tuple:
    """
    盤口方向必須與交易方向一致
    做多 → 買盤 > 賣盤（ratio > 1）
    做空 → 賣盤 > 買盤（ratio < 1）
    返回 (分數 0~1, 標籤)
    """
    if side == "LONG":
        if   ob_ratio >= 1.30: sc, lb = 1.0, f"✅ 盤口強力支撐做多 ({ob_ratio:.2f})"
        elif ob_ratio >= 1.05: sc, lb = 0.7, f"✅ 盤口略偏買盤 ({ob_ratio:.2f})"
        elif ob_ratio >= 0.95: sc, lb = 0.3, f"⚠️ 盤口均衡，方向未確認 ({ob_ratio:.2f})"
        else:                  sc, lb = 0.0, f"❌ 盤口偏空，做多風險高！({ob_ratio:.2f})"
    else:
        if   ob_ratio <= 0.77: sc, lb = 1.0, f"✅ 盤口強力支撐做空 ({ob_ratio:.2f})"
        elif ob_ratio <= 0.95: sc, lb = 0.7, f"✅ 盤口略偏賣盤 ({ob_ratio:.2f})"
        elif ob_ratio <= 1.05: sc, lb = 0.3, f"⚠️ 盤口均衡，方向未確認 ({ob_ratio:.2f})"
        else:                  sc, lb = 0.0, f"❌ 盤口偏多，做空風險高！({ob_ratio:.2f})"
    return sc, lb

# ─────────────────────────────────────────────────────────
# 10. 價格行為
# ─────────────────────────────────────────────────────────
def detect_price_action(df: pd.DataFrame, side: str) -> list:
    sigs = []
    for i in range(len(df)-1, max(len(df)-6, 0), -1):
        k         = df.iloc[i]
        body      = abs(k["c"] - k["o"])
        rng       = k["h"] - k["l"] + 1e-10
        up_wick   = k["h"] - max(k["c"], k["o"])
        dn_wick   = min(k["c"], k["o"]) - k["l"]
        body_pct  = body / rng

        if side == "SHORT" and up_wick >= body * 2.0 and dn_wick <= body * 0.5:
            sigs.append(f"空頭流星線 ({min(up_wick/(body+1e-10),5):.1f}x影) @ {k['c']:.4f}")
        if side == "LONG"  and dn_wick >= body * 2.0 and up_wick <= body * 0.5:
            sigs.append(f"多頭錘子線 ({min(dn_wick/(body+1e-10),5):.1f}x影) @ {k['c']:.4f}")
        if side == "SHORT" and up_wick/rng > 0.40 and k["c"] < k["o"]:
            sigs.append(f"壓力位拒絕 (上影{up_wick/rng*100:.0f}%) @ {k['c']:.4f}")
        if side == "LONG"  and dn_wick/rng > 0.40 and k["c"] > k["o"]:
            sigs.append(f"支撐位拒絕 (下影{dn_wick/rng*100:.0f}%) @ {k['c']:.4f}")
        if body_pct >= 0.70:
            if (side=="LONG" and k["c"]>k["o"]) or (side=="SHORT" and k["c"]<k["o"]):
                sigs.append(f"{'多' if side=='LONG' else '空'}頭動量棒 ({body_pct*100:.0f}%實體) @ {k['c']:.4f}")
    return sigs[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
    sigs  = detect_price_action(df, side)
    score = 0.6 if len(sigs)>=3 else (0.4 if len(sigs)>=2 else (0.2 if sigs else 0.0))
    last  = df.iloc[-1]
    body  = abs(last["c"] - last["o"])
    rng   = last["h"] - last["l"] + 1e-10
    if body/rng > 0.70: score += 0.20
    if (side=="LONG" and last["c"]>last["o"]) or (side=="SHORT" and last["c"]<last["o"]):
        score += 0.20
    score = min(score, 1.0)
    label = "✅ 強勢PA" if score>=0.65 else ("⚠️ 中等PA" if score>=0.40 else "⛔ 弱PA")
    return score*100, label, sigs

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    zones  = []
    vol_ma = df["v"].rolling(20).mean()
    vol_sd = df["v"].rolling(20).std()
    for i in range(max(len(df)-10, 0), len(df)):
        if df["v"].iloc[i] > vol_ma.iloc[i] + 2*vol_sd.iloc[i]:
            if df["c"].iloc[i] > df["o"].iloc[i] and side=="LONG":
                zones.append(f"🔵 主力吸籌 {df['c'].iloc[i]:.4f}")
            elif df["c"].iloc[i] < df["o"].iloc[i] and side=="SHORT":
                zones.append(f"🔴 主力派發 {df['c'].iloc[i]:.4f}")
    hi = df["h"].iloc[-20:].max()
    lo = df["l"].iloc[-20:].min()
    zones.append(f"{'🔴 多頭清算點' if side=='SHORT' else '🔵 空頭清算點'}"
                 f" {hi:.4f}" if side=="SHORT" else f" {lo:.4f}")
    return zones[:2]

# ─────────────────────────────────────────────────────────
# 11. 評分系統（100分，最低75分進場）
# ─────────────────────────────────────────────────────────
def calculate_setup_score(setup: dict) -> tuple:
    """
    滿分 100 分配重：
    ┌────────────────────────┬─────┐
    │ 1H HTF 趨勢一致        │  25 │
    │ OB / FVG 進場          │  20 │
    │ 流動性掃除             │  20 │
    │ 真實 CVD               │  15 │
    │ 多空比（逆向）         │  10 │
    │ 資金費率               │   5 │
    │ 盤口方向               │   5 │
    └────────────────────────┴─────┘
    硬性扣分：HTF 反向 -15，費率禁入 -10，盤口反向 -10
    """
    sc = 0.0
    bd = []
    side = setup.get("side", "LONG")

    # 1. HTF（25分）
    htf = setup.get("htf_trend", "UNKNOWN")
    if htf == side:
        sc += 25; bd.append("📈 HTF一致 +25")
    elif htf in ("NEUTRAL", "UNKNOWN"):
        sc += 10; bd.append("⚪ HTF不明 +10")
    else:
        sc += 0;  bd.append("❌ HTF反向 +0")

    # 2. OB / FVG（20分）
    at_ob  = setup.get("at_ob",  False)
    at_fvg = setup.get("at_fvg", False)
    if at_ob and at_fvg:
        sc += 20; bd.append("🎯 OB+FVG +20")
    elif at_ob:
        sc += 16; bd.append("🎯 在OB +16")
    elif at_fvg:
        sc += 14; bd.append("🎯 在FVG +14")
    else:
        sc += 0;  bd.append("❌ 不在OB/FVG +0")

    # 3. 流動性掃除（20分）
    sw = setup.get("sweep_score", 0)
    p  = round(sw * 20)
    sc += p
    bd.append(f"💧 流動性掃除 +{p}" if sw > 0 else "⚪ 無掃除 +0")

    # 4. CVD（15分）
    cvd_sc = setup.get("cvd_score", 0)
    p = round(cvd_sc * 15)
    sc += p; bd.append(f"📊 CVD +{p}")

    # 5. 多空比（10分）
    ls_sc = setup.get("ls_score", 0)
    p = round(ls_sc * 10)
    sc += p; bd.append(f"👥 多空比 +{p}")

    # 6. 資費（5分）
    fr_sc = setup.get("fr_score", 0)
    p = round(fr_sc * 5)
    sc += p; bd.append(f"💸 資費 +{p}")

    # 7. 盤口（5分）
    ob_sc = setup.get("ob_dir_score", 0)
    p = round(ob_sc * 5)
    sc += p; bd.append(f"📚 盤口 +{p}")

    # ── 硬性扣分 ──────────────────────────────
    if htf not in (side, "NEUTRAL", "UNKNOWN"):
        sc -= 15; bd.append("🚫 HTF逆勢 -15")
    if setup.get("fr_score", 1) == 0.0:
        sc -= 10; bd.append("🚫 費率禁入 -10")
    if setup.get("ob_dir_score", 1) == 0.0:
        sc -= 10; bd.append("🚫 盤口反向 -10")

    sc = max(0, min(round(sc), 100))

    if   sc >= 85: grade = "🏆 A+ 極強"
    elif sc >= 75: grade = "✅ A  強力"
    elif sc >= 65: grade = "⚠️ B+ 觀望"
    elif sc >= 55: grade = "⚠️ B  偏弱"
    else:          grade = "❌ C  跳過"

    return sc, grade, bd

# ─────────────────────────────────────────────────────────
# 12. 主掃描邏輯
# ─────────────────────────────────────────────────────────
def scan_for_opportunity(instId: str) -> list:
    """
    掃描流程：
    15m K線 → HTF過濾(1H) → 盤口硬過濾 → 費率硬過濾
    → 流動性分析 → BOS/CHoCH → OB/FVG → P/D Zone
    → CVD → PA → 評分 ≥ 75 → 輸出
    """
    df = fetch_okx(instId, tf="15m", limit=150)
    if df is None or len(df) < 50:
        return []

    atr          = calculate_atr(df)
    st_val, st_l = calculate_supertrend(df)
    htf_trend    = get_htf_trend(instId)

    # 共用行情數據（避免重複請求）
    fr            = fetch_funding_rate(instId)
    ls_f, ls_str  = fetch_ls_ratio(instId)
    ob_r, _       = fetch_order_book_imbalance(instId)

    opportunities = []

    for side in ["LONG", "SHORT"]:

        # ── 硬性過濾 1：HTF 方向 ─────────────
        if htf_trend not in ("UNKNOWN", "NEUTRAL") and htf_trend != side:
            logging.info(f"  [{instId}][{side}] HTF={htf_trend} 反向 → 跳過")
            continue

        # ── 硬性過濾 2：盤口方向 ─────────────
        ob_dir_sc, ob_dir_lb = check_ob_direction(side, ob_r)
        if ob_dir_sc == 0.0:
            logging.info(f"  [{instId}][{side}] 盤口反向 → 跳過")
            continue

        # ── 硬性過濾 3：資金費率 ─────────────
        fr_sc, fr_lb = interpret_funding_rate(fr, side)
        if fr_sc == 0.0:
            logging.info(f"  [{instId}][{side}] 費率禁入 → 跳過")
            continue

        # ── 分析模組 ─────────────────────────
        cvd_cur, cvd_sl, cvd_lb, cvd_sc = calculate_cvd(df)
        # CVD 方向若與交易反向，降低分數
        cvd_aligned = (side=="LONG" and cvd_sl > 0) or (side=="SHORT" and cvd_sl < 0)
        eff_cvd_sc  = cvd_sc if cvd_aligned else cvd_sc * 0.25

        liq                    = find_liquidity_pools(df, side)
        bos_desc, bos_sc       = detect_bos_choch(df, side)
        at_ob, at_fvg, ob_desc, fvg_desc, entry_zone = check_ob_fvg_entry(df, side, atr)
        pd_lb, pd_sc, fib_pos  = detect_premium_discount(df, side)
        pa_sc, pa_lb, pa_sigs  = calculate_pa_score(df, side)
        structure              = detect_market_structure(df, side)
        whale_zones            = detect_whale_zones(df, side)
        ls_sc, ls_lb           = interpret_ls_ratio(ls_f, side)
        ema_sc, ema_lb         = get_ema_bias(df, side)

        # ── 評分 ─────────────────────────────
        setup_dict = dict(
            side=side, htf_trend=htf_trend,
            at_ob=at_ob, at_fvg=at_fvg,
            sweep_score=liq["sweep_score"],
            cvd_score=eff_cvd_sc,
            ls_score=ls_sc,
            fr_score=fr_sc,
            ob_dir_score=ob_dir_sc,
        )
        score, grade, bd = calculate_setup_score(setup_dict)

        if score < SETUP_SCORE_THRESHOLD:
            logging.info(f"  [{instId}][{side}] 評分 {score} < {SETUP_SCORE_THRESHOLD} → 跳過")
            continue

        # ── 計算進場 / 止損 / 止盈 ───────────
        price = df["c"].iloc[-1]

        # 進場優先順序：掃除反彈點 > OB/FVG中點 > 流動性池邊緣 > 當前價
        if liq["sweep_detected"]:
            entry = price                             # 掃除後立即進場
        elif at_ob or at_fvg:
            entry = entry_zone
        elif side == "LONG" and liq["nearest_ssl"]:
            entry = liq["nearest_ssl"] * 1.001
        elif side == "SHORT" and liq["nearest_bsl"]:
            entry = liq["nearest_bsl"] * 0.999
        else:
            entry = price

        sl   = entry - atr*1.5 if side=="LONG" else entry + atr*1.5
        risk = abs(entry - sl)
        tp1  = entry + risk       if side=="LONG" else entry - risk
        tp2  = entry + risk*2.5   if side=="LONG" else entry - risk*2.5
        tp3  = entry + risk*4.0   if side=="LONG" else entry - risk*4.0

        lev  = "10x~20x" if atr/price < 0.015 else "3x~5x"

        opp = dict(
            instId=instId, side=side,
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            atr=atr, price=price,
            structure=structure, bos_desc=bos_desc,
            at_ob=at_ob, at_fvg=at_fvg,
            ob_desc=ob_desc, fvg_desc=fvg_desc,
            pd_lb=pd_lb,
            liq=liq,
            cvd_lb=cvd_lb,
            ls_str=ls_str, ls_lb=ls_lb,
            fr_lb=fr_lb,
            ob_dir_lb=ob_dir_lb,
            ema_lb=ema_lb,
            pa_sc=pa_sc, pa_lb=pa_lb, pa_sigs=pa_sigs,
            whale_zones=whale_zones,
            htf_trend=htf_trend, st_lb=st_l,
            score=score, grade=grade, breakdown=bd,
            lev=lev,
        )
        opportunities.append(opp)

    return opportunities

# ─────────────────────────────────────────────────────────
# 13. 訊號格式化
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    coin   = opp["instId"].split("-")[0]
    e      = "🟢" if opp["side"]=="LONG" else "🔴"
    st     = "多單 (LONG)" if opp["side"]=="LONG" else "空單 (SHORT)"
    htf_e  = {"LONG":"🟢","SHORT":"🔴","NEUTRAL":"⚪","UNKNOWN":"⚪"}.get(opp["htf_trend"],"⚪")

    liq    = opp["liq"]
    sweep  = liq["sweep_desc"] if liq["sweep_detected"] else "⚪ 近期無流動性掃除"
    pools  = "\n   ".join(liq["pools"][:2]) if liq["pools"] else "─"
    bsl    = f"{liq['nearest_bsl']:.4f}" if liq["nearest_bsl"] else "─"
    ssl    = f"{liq['nearest_ssl']:.4f}" if liq["nearest_ssl"] else "─"
    eqh    = f"🔴 EQH {liq['eqh']:.4f}" if liq["eqh"] else "─"
    eql    = f"🟢 EQL {liq['eql']:.4f}" if liq["eql"] else "─"

    pa_txt = "".join(f"   {s}\n" for s in opp["pa_sigs"][:3]) or "   ─ 無明顯PA\n"
    bd_txt = " │ ".join(opp["breakdown"][:5])

    return (
        f"🔥 *Alpha Oracle v6.0* 🔥\n"
        f"══════════════════════\n"
        f"💎 #{coin}  {e} {st}\n"
        f"⏰ 15m  │  1H HTF: {htf_e} {opp['htf_trend']}\n"
        f"📊 評分 *{opp['score']}分* {opp['grade']}\n"
        f"──────── 評分明細 ────────\n"
        f"   {bd_txt}\n"
        f"══════════════════════\n"
        f"💰 進場：`{opp['entry']:.4f}`\n"
        f"🛑 止損：`{opp['sl']:.4f}`  (-1R ≈ {opp['atr']*1.5:.4f})\n"
        f"🎯 TP1 (1R)  ：`{opp['tp1']:.4f}`\n"
        f"🎯 TP2 (2.5R)：`{opp['tp2']:.4f}`\n"
        f"🎯 TP3 (4R)  ：`{opp['tp3']:.4f}`\n"
        f"──────── 流動性獵取 ──────\n"
        f"💧 {sweep}\n"
        f"   🔴 BSL（上方止損池）: {bsl}\n"
        f"   🟢 SSL（下方止損池）: {ssl}\n"
        f"   等高/等低: {eqh}  {eql}\n"
        f"   流動性池:\n   {pools}\n"
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
        f"🕹️ 槓桿：{opp['lev']}  📌 波段\n"
        f"💡 *{'流動性掃除後立即進場' if opp['liq']['sweep_detected'] else '等待回踩進場區域確認'}*"
    )

# ─────────────────────────────────────────────────────────
# 14. 主執行
# ─────────────────────────────────────────────────────────
def main():
    logging.info(f"🚀 Alpha Oracle v6.0  閾值={SETUP_SCORE_THRESHOLD}分")
    sent = 0

    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] {coin} ...")
        try:
            opps = scan_for_opportunity(coin)
            if opps:
                logging.info(f"  ✅ {len(opps)} signal(s)")
                for opp in opps:
                    if sent >= MAX_SIGNALS_PER_RUN:
                        break
                    msg = format_signal(opp)
                    if send_tg(msg):
                        sent += 1
                        logging.info(f"  📤 #{sent} {opp['side']} {opp['score']}分")
                    time.sleep(1)
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"❌ {coin}: {e}")
            traceback.print_exc()

    logging.info(f"📊 完成，共發送 {sent} 筆訊號")
    return sent


if __name__ == "__main__":
    try:
        main(); exit(0)
    except Exception as e:
        logging.error(f"💥 Crash: {e}")
        traceback.print_exc()
        exit(1)
