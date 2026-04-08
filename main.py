#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v4.4 - 高勝率主力追蹤 + 智能過濾優化版
核心改進：
  ✅ 修復：解決因過濾過嚴導致無通知的問題
  ✅ 優化：移除重複硬過濾，改以評分制為主軸
  ✅ 新增：啟動通知 & 掃描狀態回報
  ✅ 修復：語法錯誤與錯字
  ✅ 降低評分閾值：釋放更多優質訊號
"""

import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
import json
import functools
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

# 🆕 紙交易模式開關（預設關閉，設為 "true" 開啟模擬）
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
# 🆕 除錯模式（預設關閉，設為 "true" 開啟詳細日誌）
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

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
    "pa_score", "pa_signals", "setup_score",
]
STATS_COLS = ["instId", "result", "whale_signal", "whale_confidence", "whale_category", "pa_score", "setup_score"]

# ─────────────────────────────────────────────
# 2. 工具函數 & 動態優化模組
# ─────────────────────────────────────────────

def retry_on_failure(max_retries: int = 3, delay: float = 1.0):
    """🆕 通用重試裝飾器 - 用於所有外部 API 呼叫"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(delay * (2 ** attempt))
                    else:
                        logging.warning(f"{func.__name__} 失敗 {max_retries} 次")
            return None
        return wrapper
    return decorator

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def safe_int(val, fallback=0):
    try:    return int(float(val))
    except: return fallback

def load_optimization_params() -> dict:
    if os.path.exists(OPTIMIZATION_FILE):
        try:
            with open(OPTIMIZATION_FILE, 'r') as f: return json.load(f)
        except: pass
    return {"base_threshold": 0.7, "aligned_win_rate": 0.75, "warning_win_rate": 0.60, "reverse_win_rate": 0.40, "total_samples": 0}

def save_optimization_params(params: dict):
    with open(OPTIMIZATION_FILE, 'w') as f: json.dump(params, f)

def get_dynamic_threshold(opt_params: dict) -> float:
    base, awr = opt_params['base_threshold'], opt_params['aligned_win_rate']
    if   awr > 0.80: return max(0.5, base - 0.1)
    elif awr < 0.65: return min(0.9, base + 0.1)
    return base

def normalize_trade(t: dict) -> dict:
    return {k: safe_float(t.get(k, 0)) if k in ["entry","sl","tp1","tp2","tp3","whale_confidence","pa_score","setup_score"] else 
            safe_int(t.get(k, 0)) if k in ["locked","wait_since","tp1_hit"] else 
            str(t.get(k, "─")) for k in LOG_COLS}

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e: logging.warning(f"TG 發送失敗：{e}")

def get_whale_position_recommendation(whale_signal: str, whale_conf: float) -> tuple[str, str, str]:
    if whale_signal == "✅ 主力一致":
        if   whale_conf >= 0.80: return "✅ 正常 (100%)", "75-85%", "🟢"
        elif whale_conf >= 0.65: return "🟡 標準 (75%)", "70-78%", "🟡"
        else:                    return "🟠 保守 (50%)", "60-70%", "🟠"
    elif whale_signal == "⚠️ 主力警示":
        return "🟠 保守 (50%)" if whale_conf >= 0.60 else "🔴 觀望", "60-70%" if whale_conf >= 0.60 else "<60%", "🟠" if whale_conf >= 0.60 else "🔴"
    return "⛔ 建議跳過", "<50%", "🔴"

# ─────────────────────────────────────────────
# 3. 數據抓取（🆕 加入重試）
# ─────────────────────────────────────────────

@retry_on_failure(max_retries=3, delay=1.0)
def fetch_okx(instId: str, tf: str = "15m", limit: int = 100) -> pd.DataFrame | None:
    url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
    res = requests.get(url, timeout=10).json()
    if res.get('code') != '0' or not res.get('data'): return None
    df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
    df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
    return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)

def fetch_current_candle_hl(instId: str) -> tuple[float, float]:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3", timeout=5).json()
        for row in res.get('data', []):
            if row[8] == "0": return float(row[3]), float(row[2])
    except: pass
    return float('inf'), float('-inf')

@retry_on_failure(max_retries=3, delay=1.0)
def get_funding_ls(instId: str) -> tuple[str, str]:
    base_id = instId.replace("-SWAP", "").split("-")[0]
    funding, ls_ratio = "N/A", "N/A"
    try:
        f_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except: pass
    try:
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except: pass
    return funding, ls_ratio

@retry_on_failure(max_retries=3, delay=1.0)
def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple[float, str]:
    res = requests.get(f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5).json()
    if res.get('code') != '0' or not res.get('data'): return 1.0, "⚪ 盤口均衡"
    data = res['data'][0]
    bid_vol, ask_vol = sum(float(b[1]) for b in data['bids']), sum(float(a[1]) for a in data['asks'])
    if ask_vol == 0: return 1.0, "⚪ 盤口均衡"
    ratio = bid_vol / ask_vol
    label = f"🟢 買盤強勢 ({ratio:.2f})" if ratio > 1.2 else (f"🔴 賣盤強勢 ({ratio:.2f})" if ratio < 0.8 else f"⚪ 盤口均衡 ({ratio:.2f})")
    return ratio, label

@retry_on_failure(max_retries=3, delay=1.0)
def fetch_funding_rate_raw(instId: str) -> float:
    res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
    return float(res['data'][0]['fundingRate'])

# ─────────────────────────────────────────────
# 🆕 主力數據框架
# ─────────────────────────────────────────────

@retry_on_failure(max_retries=3, delay=1.0)
def fetch_coinank_spot_cvd(symbol: str) -> dict | None:
    if not COINANK_API_KEY: return None
    res = requests.get("https://api.coinank.com/api/indicators/spot-cvd", 
                       params={"symbol": symbol, "period": "24h"}, headers={"Authorization": f"Bearer {COINANK_API_KEY}"}, timeout=10).json()
    if res.get('code') == 200 and res.get('data'):
        v = float(res['data']['cvd_value'])
        return {"cvd_24h": v, "trend": "bullish" if v > 0 else "bearish"}
    return None

@retry_on_failure(max_retries=3, delay=1.0)
def fetch_glassnode_whale_flow(symbol: str) -> dict | None:
    if not GLASSNODE_API_KEY: return None
    res = requests.get(f"https://rest.glassnode.com/v1/metrics/transfers/exchange_net_flow?asset={symbol}&resolution=24h&api_key={GLASSNODE_API_KEY}", timeout=10).json()
    if res and len(res) > 0:
        flow = res[-1]['value']
        return {"net_flow": flow, "signal": "inflow" if flow > 0 else "outflow"}
    return None

@retry_on_failure(max_retries=3, delay=1.0)
def fetch_cryptoquant_open_interest(symbol: str) -> dict | None:
    if not CRYPTOQUANT_API_KEY: return None
    res = requests.get(f"https://api.cryptoquant.com/v1/data/bitcoin/metrics/open-interest?api_key={CRYPTOQUANT_API_KEY}", timeout=10).json()
    if res and 'data' in res and len(res['data']) >= 2:
        d = res['data']
        change = (d[-1]['value'] - d[-2]['value']) / (abs(d[-2]['value']) + 1e-10)
        return {"oi_change": change, "signal": "rising" if change > 0.05 else ("falling" if change < -0.05 else "stable")}
    return None

def calculate_technical_confidence(df: pd.DataFrame, side: str) -> float:
    score = 0.0
    st = calculate_supertrend(df)
    if (side == "LONG" and st == 1) or (side == "SHORT" and st == -1): score += 0.30
    structure = detect_market_structure(df, side)
    if "反轉" in structure and ((side=="LONG" and "W 底" in structure) or (side=="SHORT" and "M 頭" in structure)): score += 0.25
    elif "趨勢" in structure: score += 0.15
    _, label = calculate_cvd(df)
    if (side=="LONG" and label.startswith("🟢")) or (side=="SHORT" and label.startswith("🔴")): score += 0.20
    pa, _ = calculate_pa_score(df, side)
    score += 0.15 * pa
    return min(1.0, score)

def analyze_whale_direction(instId: str, side: str, opt_params: dict, df: pd.DataFrame = None) -> tuple[str, float, str, str]:
    symbol = instId.split('-')[0]
    spot_cvd, whale_flow, oi_data = fetch_coinank_spot_cvd(symbol), fetch_glassnode_whale_flow(symbol), fetch_cryptoquant_open_interest(symbol)
    
    if not any([spot_cvd, whale_flow, oi_data]) and df is not None:
        tc = calculate_technical_confidence(df, side)
        if tc >= 0.65: return "✅ 技術面主導", tc, "主力數據缺失，技術面極強", "Technical"
        if tc >= 0.45: return "⚠️ 技術面中等", tc, "主力數據缺失，建議降低倉位", "LowConf"
        return "🔴 技術面弱", tc, "主力數據缺失，建議跳過", "Skip"
    
    _, ls_str = get_funding_ls(instId)
    ls_ratio = float(ls_str) if ls_str != "N/A" else 1.0
    signals, confidence, category = [], 0.0, "Aligned"

    if spot_cvd:
        if side == "LONG" and spot_cvd['trend'] == "bearish": signals.append("🔴 現貨大戶出貨"); confidence += 0.35; category = "Reverse"
        elif side == "SHORT" and spot_cvd['trend'] == "bullish": signals.append("🟢 現貨大戶吸籌"); confidence += 0.35; category = "Reverse"
        else: signals.append("⚪ 現貨 CVD 一致"); confidence += 0.10

    if whale_flow:
        if side == "LONG" and whale_flow['signal'] == "inflow": signals.append("🔴 巨鯨大量流入交易所"); confidence += 0.25
        elif side == "SHORT" and whale_flow['signal'] == "outflow": signals.append("🟢 巨鯨提幣鎖倉"); confidence += 0.25

    # 🔧 修復語法錯誤：確保這裡是 if oi_
    if oi_
        if side == "SHORT" and oi_data['signal'] == "rising": signals.append("🔴 空頭持倉激增"); confidence += 0.20
        elif side == "LONG" and oi_data['signal'] == "falling": signals.append("⚠️ 空頭回補上漲"); confidence -= 0.10

    if ls_ratio > 1.1 and side == "LONG": signals.append("🔴 散戶過度看多"); confidence += 0.15; category = "Reverse"
    elif ls_ratio < 0.9 and side == "SHORT": signals.append("🟢 散戶過度看空"); confidence += 0.15; category = "Reverse"

    confidence = max(0.0, min(1.0, confidence))
    threshold = get_dynamic_threshold(opt_params)
    
    if category == "Reverse" and confidence >= threshold:
        return "🔴 主力反向", confidence, f"多項指標顯示主力反向（{confidence*100:.0f}%）", "Reverse"
    if confidence >= 0.5:
        return "⚠️ 主力警示", confidence, f"主力動向衝突（{confidence*100:.0f}%）", "Warning"
    return "✅ 主力一致", confidence, f"技術與主力一致（{confidence*100:.0f}%）", "Aligned"

def detect_whale_entry_zones(df: pd.DataFrame, side: str) -> list[dict]:
    zones, vol_ma, vol_std = [], df['v'].rolling(20).mean(), df['v'].rolling(20).std()
    for i in range(max(len(df) - 10, 0), len(df)):
        if df['v'].iloc[i] > vol_ma.iloc[i] + 2 * vol_std.iloc[i]:
            if df['c'].iloc[i] > df['o'].iloc[i] and side == "LONG":
                zones.append({"type": "whale_accumulation", "price": df['c'].iloc[i], "desc": f"🐋 主力吸籌區 {df['c'].iloc[i]:.4f}"})
            elif df['c'].iloc[i] < df['o'].iloc[i] and side == "SHORT":
                zones.append({"type": "whale_distribution", "price": df['c'].iloc[i], "desc": f"🐋 主力派發區 {df['c'].iloc[i]:.4f}"})
    high, low = df['h'].iloc[-20:].max(), df['l'].iloc[-20:].min()
    zones.append({"type": "liquidation_cluster", "price": high if side=="SHORT" else low, 
                  "desc": f"💥 {'多頭' if side=='SHORT' else '空頭'}清算熱點 {high if side=='SHORT' else low:.4f}"})
    return zones[:3]

# ─────────────────────────────────────────────
# 4. 技術指標 & PA 模組
# ─────────────────────────────────────────────
def calculate_atr(df, p=14):
    tr = pd.concat([df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
    return float(tr.rolling(p).mean().iloc[-1]) if not np.isnan(tr.rolling(p).mean().iloc[-1]) else 0.001

def calculate_cvd(df, lb=20):
    r = df.tail(lb).copy(); b = (r['h']-r['l']).replace(0, 1e-10)
    r['delta'] = np.where(r['c']>=r['o'], r['v']*(r['c']-r['l'])/b, -r['v']*(r['h']-r['c'])/b)
    return r['delta'].sum(), "🟢 大戶吸籌 (CVD+)" if r['delta'].sum() > 0 else "🔴 大戶出貨 (CVD-)"

def calculate_supertrend(df, p=10, m=3.0):
    if len(df) < p+2: return 0
    h,l,c = df['h'].values.astype(float), df['l'].values.astype(float), df['c'].values.astype(float)
    n = len(df); tr = np.zeros(n)
    for i in range(1,n): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = np.zeros(n); atr[p] = tr[1:p+1].mean()
    for i in range(p+1,n): atr[i] = (atr[i-1]*(p-1)+tr[i])/p
    hl2 = (h+l)/2; bu, bd = hl2-m*atr, hl2+m*atr
    fu, fd, t = np.zeros(n), np.zeros(n), np.ones(n, dtype=int)
    fu[p], fd[p] = bu[p], bd[p]
    for i in range(p+1,n):
        fu[i] = bu[i] if bu[i]>fu[i-1] or c[i-1]<fu[i-1] else fu[i-1]
        fd[i] = bd[i] if bd[i]<fd[i-1] or c[i-1]>fd[i-1] else fd[i-1]
        if t[i-1]==-1 and c[i]>fd[i-1]: t[i]=1
        elif t[i-1]==1 and c[i]<fu[i-1]: t[i]=-1
        else: t[i]=t[i-1]
    return int(t[-1])

def detect_pin_bar(df, lb=3):
    for i in range(len(df)-1, max(len(df)-lb-1, 0), -1):
        k=df.iloc[i]; body=abs(k['c']-k['o']); rng=k['h']-k['l']+1e-10
        if body < rng*0.05: continue
        uw, lw = k['h']-max(k['c'],k['o']), min(k['c'],k['o'])-k['l']
        if lw >= body*2.0 and uw <= body*0.5: return {"detected":True,"type":"bullish_pin","strength":min(lw/(body+1e-10)/5,1),"desc":f"📌 多頭錘子線 ({lw/body:.1f}R) @ {k['c']:.4f}"}
        if uw >= body*2.0 and lw <= body*0.5: return {"detected":True,"type":"bearish_pin","strength":min(uw/(body+1e-10)/5,1),"desc":f"📌 空頭流星線 ({uw/body:.1f}R) @ {k['c']:.4f}"}
    return {"detected":False}

def detect_engulfing(df, lb=3):
    for i in range(len(df)-1, max(len(df)-lb-1, 1), -1):
        cu,pr = df.iloc[i], df.iloc[i-1]; cb,pb = abs(cu['c']-cu['o']), abs(pr['c']-pr['o'])
        if pb < 1e-10: continue
        if cu['c']>cu['o'] and pr['c']<pr['o'] and cu['o']<=pr['c'] and cu['c']>=pr['o']:
            return {"detected":True,"type":"bullish_engulfing","strength":min(cb/(pb+1e-10)/3,1),"desc":f"🕯️ 多頭吞噬 ({cb/pb:.1f}x) @ {cu['c']:.4f}"}
        if cu['c']<cu['o'] and pr['c']>pr['o'] and cu['o']>=pr['c'] and cu['c']<=pr['o']:
            return {"detected":True,"type":"bearish_engulfing","strength":min(cb/(pb+1e-10)/3,1),"desc":f"🕯️ 空頭吞噬 ({cb/pb:.1f}x) @ {cu['c']:.4f}"}
    return {"detected":False}

def detect_inside_bar(df):
    if len(df)<2: return {"detected":False}
    cu,pr = df.iloc[-1], df.iloc[-2]
    if cu['h']<=pr['h'] and cu['l']>=pr['l']:
        mr = pr['h']-pr['l']+1e-10; ir = cu['h']-cu['l']
        return {"detected":True,"compression":1-ir/mr,"desc":f"📦 內包棒壓縮 ({(1-ir/mr)*100:.0f}%) 母棒:{pr['h']:.4f}/{pr['l']:.4f}"}
    return {"detected":False}

def detect_rejection(df, side):
    if len(df)<1: return {"detected":False,"strength":0}
    k=df.iloc[-1]; rng=k['h']-k['l']+1e-10; uw,lw = k['h']-max(k['c'],k['o']), min(k['c'],k['o'])-k['l']
    if side=="LONG" and lw/rng>0.4 and k['c']>k['o']: return {"detected":True,"strength":lw/rng,"desc":f"🔄 支撐拒絕 (下影 {lw/rng*100:.0f}%) @ {k['c']:.4f}"}
    if side=="SHORT" and uw/rng>0.4 and k['c']<k['o']: return {"detected":True,"strength":uw/rng,"desc":f"🔄 壓力拒絕 (上影 {uw/rng*100:.0f}%) @ {k['c']:.4f}"}
    return {"detected":False,"strength":0}

def detect_momentum(df, side, lb=5):
    atr=calculate_atr(df)
    for i in range(len(df)-1, max(len(df)-lb-1, 0), -1):
        k=df.iloc[i]; body=abs(k['c']-k['o']); rng=k['h']-k['l']+1e-10
        if body/rng>=0.7 and body>=atr*0.8:
            bull = k['c']>k['o']
            if (side=="LONG" and bull) or (side=="SHORT" and not bull):
                return {"detected":True,"strength":body/rng,"desc":f"⚡ {'多頭' if bull else '空頭'}動量棒 ({body/rng*100:.0f}%實體) @ {k['c']:.4f}"}
    return {"detected":False,"strength":0}

def detect_fakeout(df, side, lb=10):
    if len(df)<lb+2: return {"detected":False}
    rec=df.tail(lb); rh,rl = rec['h'].iloc[:-1].max(), rec['l'].iloc[:-1].min()
    if side=="LONG":
        for i in range(len(df)-3, max(len(df)-lb-1,1), -1):
            k=df.iloc[i]
            if k['l']<rl and k['c']>rl: return {"detected":True,"desc":f"🪤 空頭假突破獵殺 ({k['l']:.4f}→{k['c']:.4f})"}
    elif side=="SHORT":
        for i in range(len(df)-3, max(len(df)-lb-1,1), -1):
            k=df.iloc[i]
            if k['h']>rh and k['c']<rh: return {"detected":True,"desc":f"🪤 多頭假突破獵殺 ({k['h']:.4f}→{k['c']:.4f})"}
    return {"detected":False}

def calculate_pa_score(df, side):
    score, sigs = 0.0, []
    p=detect_pin_bar(df)
    if p['detected']:
        al=(side=="LONG" and p['type']=="bullish_pin") or (side=="SHORT" and p['type']=="bearish_pin")
        if al: score+=0.25*p['strength']; sigs.append(p['desc'])
        elif p['strength']>0.6: score-=0.1; sigs.append(f"⚠️ 反向 {p['desc']}")
    e=detect_engulfing(df)
    if e['detected']:
        al=(side=="LONG" and e['type']=="bullish_engulfing") or (side=="SHORT" and e['type']=="bearish_engulfing")
        if al: score+=0.2*e['strength']; sigs.append(e['desc'])
        else: score-=0.05; sigs.append(f"⚠️ 反向 {e['desc']}")
    r=detect_rejection(df, side)
    if r['detected']: score+=0.2*r['strength']; sigs.append(r['desc'])
    m=detect_momentum(df, side)
    if m['detected']: score+=0.15*m['strength']; sigs.append(m['desc'])
    f=detect_fakeout(df, side)
    if f['detected']: score+=0.15; sigs.append(f['desc'])
    ib=detect_inside_bar(df)
    if ib['detected']: score+=0.1*ib['compression']; sigs.append(ib['desc'])
    pos=len([s for s in sigs if not s.startswith("⚠️")])
    if pos>=3: score+=0.1
    elif pos>=2: score+=0.05
    return max(0.0, min(1.0, score)), sigs

# ─────────────────────────────────────────────
# 🆕 評分制過濾系統
# ─────────────────────────────────────────────

def calculate_setup_score(setup, df, instId):
    score = 0.0
    if setup['whale_signal'] == "✅ 主力一致": score += 0.30 * setup['whale_confidence']
    elif setup['whale_signal'] == "⚠️ 主力警示": score += 0.15 * setup['whale_confidence']
    score += 0.25 * setup['pa_score']
    if setup['st_label'] == "📈 多頭" and setup['side'] == "LONG": score += 0.20
    elif setup['st_label'] == "📉 空頭" and setup['side'] == "SHORT": score += 0.20
    if setup['cvd_label'].startswith("🟢") and setup['side'] == "LONG": score += 0.15
    elif setup['cvd_label'].startswith("🔴") and setup['side'] == "SHORT": score += 0.15
    try:
        fr = fetch_funding_rate_raw(instId)
        if setup['side']=="LONG" and fr<0.0003: score+=0.10
        elif setup['side']=="SHORT" and fr>-0.0003: score+=0.10
    except: pass
    return min(1.0, score)

# ─────────────────────────────────────────────
# 5. SMC & ICT 結構分析
# ─────────────────────────────────────────────
def find_swing_points(df, n=2, lb=80):
    d=df.tail(lb).reset_index(drop=True); sh,sl=[],[]
    for i in range(n, len(d)-n):
        if d['h'].iloc[i]==d['h'].iloc[i-n:i+n+1].max(): sh.append(d['h'].iloc[i])
        if d['l'].iloc[i]==d['l'].iloc[i-n:i+n+1].min(): sl.append(d['l'].iloc[i])
    return sorted(set(sh)), sorted(set(sl))

def detect_market_structure(df, side=None):
    sh,sl = find_swing_points(df, 3, 60)
    hw = len(sl)>=2 and sl[-2]>0 and abs(sl[-2]-sl[-1])/sl[-2]<0.015
    hm = len(sh)>=2 and sh[-2]>0 and abs(sh[-2]-sh[-1])/sh[-2]<0.015
    if side=="LONG": return "W 底反轉 📐" if hw else ("M 頭壓制 ⚠️" if hm else "")
    if side=="SHORT": return "M 頭反轉 📐" if hm else ("W 底支撐 ⚠️" if hw else "")
    if hw: return "W 底反轉 📐"
    if hm: return "M 頭反轉 📐"
    s = (df['c'].iloc[-1]-df['c'].iloc[-20])/(df['c'].iloc[-20]+1e-10)
    return "上升趨勢延續 📈" if s>0.025 else ("下降趨勢延續 📉" if s<-0.025 else "區間盤整 ↔️")

def find_order_block(df, side, lb=15):
    d=df.tail(lb).reset_index(drop=True)
    for i in range(len(d)-2, 0, -1):
        k,kn=d.iloc[i],d.iloc[i+1]
        if side=="LONG" and k['c']<k['o'] and kn['c']>kn['o']: return {"high":k['o'],"low":k['l']}
        if side=="SHORT" and k['c']>k['o'] and kn['c']<kn['o']: return {"high":k['h'],"low":k['c']}
    return None

def find_recent_fvg(df, side):
    for i in range(len(df)-3, max(len(df)-20, 0), -1):
        k0,k2=df.iloc[i-1],df.iloc[i+1]
        if side=="LONG" and k2['l']>k0['h']: return {"high":k2['l'],"low":k0['h']}
        if side=="SHORT" and k2['h']<k0['l']: return {"high":k0['l'],"low":k2['h']}
    return None

def find_ict_snr_zones(df, side, lb=30):
    sh,sl=find_swing_points(df, 2, lb); p=df['c'].iloc[-1]
    if side=="LONG":
        v=[s for s in sl if s<p*0.995]
        if v: s=max(v); return {"support":s,"resistance":None,"active_level":s,"type":"support","text":f"支撐 {s:.4f}"}
    else:
        v=[r for r in sh if r>p*1.005]
        if v: r=min(v); return {"support":None,"resistance":r,"active_level":r,"type":"resistance","text":f"壓力 {r:.4f}"}
    return None

def calculate_structural_sl(df, side, entry, atr):
    buf=atr*0.25; ob,fg,sn=find_order_block(df,side),find_recent_fvg(df,side),find_ict_snr_zones(df,side)
    cands=[]
    if side=="LONG":
        if ob and ob['low']<entry: cands.append(ob['low']-buf)
        if fg and fg['low']<entry: cands.append(fg['low']-buf)
        if sn and sn.get('active_level') and sn['active_level']<entry: cands.append(sn['active_level']-buf)
        sl=max(cands) if cands else entry-atr*1.5
        return sl if (entry-sl)/(entry+1e-10)>=0.004 else entry-atr*1.5
    else:
        if ob and ob['high']>entry: cands.append(ob['high']+buf)
        if fg and fg['high']>entry: cands.append(fg['high']+buf)
        if sn and sn.get('active_level') and sn['active_level']>entry: cands.append(sn['active_level']+buf)
        sl=min(cands) if cands else entry+atr*1.5
        return sl if (sl-entry)/(entry+1e-10)>=0.004 else entry+atr*1.5

def get_fixed_r_tps(entry, sl, side):
    r=abs(entry-sl)+1e-10
    return (entry+r, entry+r*2, entry+r*3) if side=="LONG" else (entry-r, entry-r*2, entry-r*3)

def suggest_leverage(atr, price, wc=0.5):
    v=(atr/(price+1e-10))*100
    if wc<0.4: return ("2x~3x","⚠️ 主力不明") if v>3 else (("3x~5x","⚠️ 中波動") if v>1.5 else ("5x~8x","低波動"))
    return ("3x~5x","⚠️ 高波動") if v>3 else (("5x~10x","中波動") if v>1.5 else ("10x~20x","低波動"))

def is_trending_market(df):
    if len(df)<50: return True
    tr=pd.concat([df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
    return tr.rolling(14).mean().iloc[-1] > tr.tail(50).mean() * 0.7

def get_btc_direction(df, lb=5):
    if df is None or len(df)<lb: return "NEUTRAL"
    r=df.tail(lb); b=int((r['c']<r['o']).sum())
    return "DOWN" if b>=4 else ("UP" if (lb-b)>=4 else "NEUTRAL")

def classify_trade(side, struct, rp):
    return "📊 長單" if "反轉" in struct else ("⚡ 短單" if rp<1.0 else "📊 長單")

# ─────────────────────────────────────────────
# 7. SMC 訊號掃描（🆕 優化版）
# ─────────────────────────────────────────────

# 🆕 降低門檻：釋放更多優質訊號
SETUP_SCORE_THRESHOLD = 0.35  # 0.40 → 0.35
PA_MIN_SCORE = 0.25           # 0.30 → 0.25

def find_smc_setup(df, instId, opt_params):
    if df is None or len(df)<40: return None
    atr=calculate_atr(df); best=None
    for i in range(len(df)-3, len(df)-25, -1):
        if i<2: continue
        k0,k1,k2=df.iloc[i-1],df.iloc[i],df.iloc[i+1]
        if k2['c']>k2['o'] and k2['c']>df['h'].iloc[i-15:i].max(): best={"side":"LONG","k0":k0,"k1":k1,"k2":k2}; break
        elif k2['c']<k2['o'] and k2['c']<df['l'].iloc[i-15:i].min(): best={"side":"SHORT","k0":k0,"k1":k1,"k2":k2}; break
    if not best: return None
    
    side=best['side']; k1=best['k1']; price=df['c'].iloc[-1]
    ws,wc,wd,wcat = analyze_whale_direction(instId, side, opt_params, df)
    ps,psigs = calculate_pa_score(df, side)
    
    temp={'whale_signal':ws,'whale_confidence':wc,'pa_score':ps,'side':side,
          'cvd_label':calculate_cvd(df)[1],
          'st_label':"📈 多頭" if calculate_supertrend(df)==1 else ("📉 空頭" if calculate_supertrend(df)==-1 else "⚪ 未知")}
    sc=calculate_setup_score(temp, df, instId)
    
    if DEBUG_MODE: logging.info(f"[{instId}] Score:{sc:.2f} | PA:{ps:.2f} | Whale:{wc:.2f}")
    if sc < SETUP_SCORE_THRESHOLD: return None
    if ps < PA_MIN_SCORE: return None

    fg,ob,wz = find_recent_fvg(df,side), find_order_block(df,side), detect_whale_entry_zones(df,side)
    entry,src = k1['c'], "Breakout"
    
    def _tz(types, cond):
        for z in wz:
            if z['type'] in types and cond(z['price']): return z['price'], f"Whale-{z['type']}"
        return None, None
    
    if side=="LONG":
        e,s=_tz(['whale_accumulation','liquidation_cluster'], lambda p: k1['c']<p<price*0.995)
        if e: entry,src=e,s
        elif fg and k1['c']<fg['high']<price*0.995: entry,src=fg['high'],"FVG"
        elif ob and k1['c']<ob['high']<price*0.995: entry,src=ob['high'],"OB"
    else:
        e,s=_tz(['whale_distribution','liquidation_cluster'], lambda p: k1['c']>p>price*1.005)
        if e: entry,src=e,s
        elif fg and k1['c']>fg['low']>price*1.005: entry,src=fg['low'],"FVG"
        elif ob and k1['c']>ob['low']>price*1.005: entry,src=ob['low'],"OB"
        
    if abs(entry-price)/price > 0.03: entry,src = k1['c'], "Breakout (過遠)"
    
    sl=calculate_structural_sl(df,side,entry,atr); tp1,tp2,tp3=get_fixed_r_tps(entry,sl,side)
    rp=risk/(entry+1e-10)*100 if (risk:=abs(entry-sl)+1e-10) else 0
    struct=detect_market_structure(df,side); lev,ln=suggest_leverage(atr,price,wc)
    tt=classify_trade(side,struct,rp); _,cl=calculate_cvd(df)
    sv=calculate_supertrend(df); sl_label="📈 多頭" if sv==1 else ("📉 空頭" if sv==-1 else "⚪ 未知")
    snr=find_ict_snr_zones(df,side)
    snd = f"🟢 支撐 {snr['support']:.4f} | 🔴 壓力 {snr['resistance']:.4f}" if snr else "🟢 支撐 ─ | 🔴 壓力 ─"
    sna = f"✅ 參考 {snr['text']}" if snr and snr.get('active_level') else "⚠️ 無明確關鍵位"
    wzt = " | ".join([z['desc'] for z in wz[:2]]) if wz else "─"
    pl = "✅ 強勢PA" if ps>=0.65 else ("⚠️ 中等PA" if ps>=0.40 else "⛔ 弱PA")
    
    return {"side":side,"entry":entry,"entry_source":src,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "structure":struct,"leverage":lev,"leverage_note":ln,"trade_type":tt,"cvd_label":cl,
            "st_val":sv,"st_label":sl_label,"snr_display":snd,"snr_active":sna,"snr_zone":snr,
            "fvg":fg,"ob":ob,"whale_signal":ws,"whale_confidence":wc,"whale_desc":wd,"whale_zones":wzt,
            "whale_category":wcat,"pa_score":ps,"pa_label":pl,"pa_signals":" | ".join(psigs) if psigs else "─","setup_score":sc}

# ─────────────────────────────────────────────
# 統計 & 主程式
# ─────────────────────────────────────────────
def update_whale_stats(cat, res):
    f="whale_perf_temp.csv"; r=pd.DataFrame([{"category":cat,"result":res}])
    pd.concat([pd.read_csv(f), r], ignore_index=True).to_csv(f, index=False) if os.path.exists(f) else r.to_csv(f, index=False)

def generate_midnight_report(op):
    f="whale_perf_temp.csv"
    if not os.path.exists(f): return ""
    df=pd.read_csv(f); 
    if df.empty: return ""
    def cw(s): return len(s[s['result']=='TP'])/len(s)*100 if len(s)>0 else 0
    a,w,r = cw(df[df['category']=='Aligned']), cw(df[df['category']=='Warning']), cw(df[df['category']=='Reverse'])
    op.update({'aligned_win_rate':a/100,'warning_win_rate':w/100,'reverse_win_rate':r/100,'total_samples':len(df)})
    save_optimization_params(op); os.remove(f)
    return f"\n🐋 *主力績效 (近 {len(df)} 單)*\n✅ 一致: {a:.1f}% | ⚠️ 警示: {w:.1f}% | 🚫 反向: {r:.1f}%\n🔄 閾值: {get_dynamic_threshold(op):.2f}"

ENTRY_EMOJI = {"FVG":"🕳️","OB":"🧱","Breakout":"⚡","Whale-whale_accumulation":"🐋","Whale-whale_distribution":"🐋","Whale-liquidation_cluster":"💥"}
ENTRY_TXT = {"FVG":"FVG 缺口","OB":"OB 訂單塊","Breakout":"突破點","Whale-whale_accumulation":"主力吸籌","Whale-whale_distribution":"主力派發","Whale-liquidation_cluster":"清算熱點"}
WHALE_EMOJI = {"✅ 主力一致":"🐋","⚠️ 主力警示":"⚠️","🔴 主力反向":"🚫"}

def main():
    try:
        now=datetime.utcnow()+timedelta(hours=8); mr=os.getenv("MANUAL_REPORT","false").lower()=="true"
        op=load_optimization_params()
        for f,c in zip([LOG_FILE,STATS_FILE],[LOG_COLS,STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size==0: pd.DataFrame(columns=c).to_csv(f, index=False)
        
        # 🆕 啟動通知
        send_tg(f"🤖 *Alpha Oracle v4.4 已啟動*\n📡 開始掃描 {len(ALL_COINS)} 個交易對...\n🧪 紙交易: {'開啟' if PAPER_TRADING else '關閉'}\n🐛 除錯模式: {'開啟' if DEBUG_MODE else '關閉'}")

        im=(now.hour==0 and 0<=now.minute<15)
        if im or mr:
            if not os.path.exists("midnight.ok") or mr:
                try:
                    ds=pd.read_csv(STATS_FILE)
                    if not ds.empty:
                        tc=len(ds[ds['result']=='TP']); sc=len(ds[ds['result']=='SL']); tot=tc+sc; wr=(tc/tot*100) if tot>0 else 0
                        dt=(now-timedelta(days=1)).strftime('%Y-%m-%d'); rpt=generate_midnight_report(op)
                        pt="\n🧪 *紙交易模式*" if PAPER_TRADING else ""
                        send_tg(f"📊 *每日戰績*{pt}\n📅 {dt}\n✅ {tc} | ❌ {sc} | 📊 {tot}\n🎯 勝率: *{wr:.1f}%*\n💰 盈虧比: {(tc*2+sc*(-1))/tot if tot>0 else 0:.2f}R{rpt}")
                    if im: pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False); open("midnight.ok","w").write("ok")
                except Exception as e: logging.error(f"報告失敗: {e}")
        elif now.hour!=0 and os.path.exists("midnight.ok"): os.remove("midnight.ok")

        try:
            td=pd.read_csv(LOG_FILE)
            for c in LOG_COLS:
                if c not in td.columns: td[c]=0.0 if c in ["entry","sl","tp1","tp2","tp3","whale_confidence","pa_score","setup_score"] else (0 if c in ["locked","wait_since","tp1_hit"] else "─")
        except: td=pd.DataFrame(columns=LOG_COLS)

        aids=td['instId'].tolist(); upd=[]; cb=int(datetime.utcnow().timestamp()//900)
        btc=fetch_okx("BTC-USDT-SWAP"); bt=get_btc_direction(btc)
        logging.info(f"BTC: {bt}")

        for iid in ALL_COINS:
            df=fetch_okx(iid)
            if df is None or df.empty: time.sleep(0.2); continue
            cp=df['c'].iloc[-1]; cs=iid.split('-')[0]

            if iid not in aids:
                # 🆕 移除 is_trending_market 硬過濾，改由評分制處理
                setup=find_smc_setup(df, iid, op)
                if setup:
                    cv,_=calculate_cvd(df)
                    if setup['side']=="LONG" and cv<0: continue
                    if setup['side']=="SHORT" and cv>0: continue
                    fr=fetch_funding_rate_raw(iid)
                    if setup['side']=="LONG" and fr>0.0005: continue
                    if setup['side']=="SHORT" and fr<-0.0005: continue
                    if iid!="BTC-USDT-SWAP":
                        if setup['side']=="LONG" and bt=="DOWN": continue
                        if setup['side']=="SHORT" and bt=="UP": continue
                    if setup['st_val']==-1 and setup['side']=="LONG": continue
                    if setup['st_val']==1 and setup['side']=="SHORT": continue
                    # 🆕 移除 snr_zone is None 硬過濾
                    
                    obr,obl=fetch_order_book_imbalance(iid)
                    # 🆕 放寬盤口過濾門檻：0.9/1.1 → 0.85/1.15
                    if setup['side']=="LONG" and obr<0.85: continue
                    if setup['side']=="SHORT" and obr>1.15: continue

                    pt="🧪 紙交易 | " if PAPER_TRADING else ""
                    fe,se="🟢","多單" if setup['side']=="LONG" else "🔴","空單"
                    tl,st=("1.0R","2.5R","4.0R"),"長單" if "反轉" in setup['structure'] else (("0.8R","1.5R","2.0R"),"短單" if "盤整" in setup['structure'] else ("1.0R","2.0R","3.0R"),"長單")
                    ee=ENTRY_EMOJI.get(setup['entry_source'],"📍"); et=ENTRY_TXT.get(setup['entry_source'],setup['entry_source'])
                    ste="📈" if setup['st_val']==1 else ("📉" if setup['st_val']==-1 else "⚪")
                    we=WHALE_EMOJI.get(setup['whale_signal'],"❓")
                    pl=""
                    if setup['pa_signals'] and setup['pa_signals']!="─":
                        for s in setup['pa_signals'].split(" | ")[:3]: pl+=f"   {s}\n"
                    else: pl="   ─ 無明顯 PA\n"
                    
                    send_tg(f"🔥 *{pt}訊號發射* 🔥\n💎 #{cs}\n🎯 {fe} {se}\n⏰ 15m\n📊 多空:{obl} | 資費:{fetch_funding_rate_raw(iid)*100:.3f}%\n🧬 {setup['cvd_label']}\n\n💰 進場:{setup['entry']:.4f} {ee}({et})\n🛑 SL:{setup['sl']:.4f}\n💰 TP1({tl[0]}):{setup['tp1']:.4f}\n💰 TP2({tl[1]}):{setup['tp2']:.4f}\n💰 TP3({tl[2]}):{setup['tp3']:.4f}\n\n🏗️ {setup['structure']}\n🛡️ {setup['snr_display']}\n    {setup['snr_active']}\n🕯️ {setup['pa_label']} {setup['pa_score']*100:.0f}分\n{pl}🐋 {we} {setup['whale_signal']} ({setup['whale_confidence']*100:.0f}%)\n📡 {ste} {setup['st_label']}\n🕹️ {setup['leverage']}\n📊 評分:{setup['setup_score']*100:.0f}分\n\n💡 *等待回踩 {et}...*")
                    
                    upd.append({k:setup.get(k,0) for k in LOG_COLS} | {"instId":iid,"status":"WAITING","locked":0,"wait_since":cb,"tp1_hit":0})
                time.sleep(0.2); continue

            t=normalize_trade(td[td['instId']==iid].iloc[0].to_dict())
            if t['status']=="WAITING":
                nc=min(3,len(df)); cl,ch=fetch_current_candle_hl(iid)
                chl,chh=min(df['l'].iloc[-nc:].min(),cl), max(df['h'].iloc[-nc:].max(),ch)
                ih=(t['side']=="LONG" and chl<=t['entry']) or (t['side']=="SHORT" and chh>=t['entry'])
                als=(t['side']=="LONG" and cp<t['sl']) or (t['side']=="SHORT" and cp>t['sl'])
                if ih and als: continue
                if ih:
                    t['status']="ACTIVE"; se="🟢" if t['side']=="LONG" else "🔴"; sz="多單" if t['side']=="LONG" else "空單"
                    r=abs(t['entry']-t['sl'])+1e-10; rp=(r/t['entry'])*100
                    r1,r2,r3=abs(t['tp1']-t['entry'])/r, abs(t['tp2']-t['entry'])/r, abs(t['tp3']-t['entry'])/r
                    ns=datetime.utcnow().strftime('%Y-%m-%d %H:%M')
                    ee=ENTRY_EMOJI.get(t['entry_source'],"📍"); et=ENTRY_TXT.get(t['entry_source'],t['entry_source'])
                    we=WHALE_EMOJI.get(t['whale_signal'],"❓")
                    pr,wr,cc=get_whale_position_recommendation(t['whale_signal'], t['whale_confidence'])
                    ew="\n⚠️ *主力不明*" if "跳過" in pr or "觀望" in pr else ""
                    ps=f"\n🕯️ PA:{t['pa_signals'].split(' | ')[0]} ({t['pa_score']*100:.0f}分)" if t['pa_signals']!="─" else ""
                    pt="🧪 紙交易 | " if PAPER_TRADING else ""
                    send_tg(f"🚀 *{pt}進場成交* 🚀\n💎 #{cs}\n🎯 {se} {sz}\n⏰ {ns}\n\n💰 *進場:{t['entry']:.4f}* {ee}({et})\n🛑 *SL:{t['sl']:.4f}* ({rp:.2f}%){ps}\n\n🐋 主力:\n   {cc} 信心:{t['whale_confidence']*100:.0f}% | {t['whale_signal']}\n   📊 預期:{wr}\n   💡 倉位:{pr}{ew}\n\n🎯 TP1(+{r1:.1f}R):{t['tp1']:.4f}\n🎯 TP2(+{r2:.1f}R):{t['tp2']:.4f}\n🎯 TP3(+{r3:.1f}R):{t['tp3']:.4f}\n\n🛡️ {t['snr_display']}\n📊 評分:{t['setup_score']*100:.0f}分\n🛡️ 移動止損已啟用")
                    t['wait_since']=cb; upd.append(t); time.sleep(0.2); continue
                
                bw=cb-t['wait_since']
                if bw>WAITING_EXPIRY_BARS: continue
                if bw>10:
                    pd=abs(cp-t['entry'])/t['entry']*100
                    mi=(t['side']=="LONG" and cp>t['entry']*1.02) or (t['side']=="SHORT" and cp<t['entry']*0.98)
                    if mi and pd>2.0:
                        dt="上漲" if t['side']=="LONG" else "下跌"
                        send_tg(f"⚠️ *訊號失效*\n💎 #{cs}\n🎯 {'🟢 多' if t['side']=='LONG' else '🔴 空'}\n⏰ {bw}K ({bw*15//60}h)\n📍 原:{t['entry']:.4f} | 現:{cp:.4f}\n📊 偏離:{pd:.2f}%\n❌ 已直接{dt}未回踩\n💡 *已失效，勿追*")
                        continue
                upd.append(t)

            elif t['status']=="ACTIVE":
                rr=abs(t['entry']-t['sl'])+1e-10
                if t['tp1_hit']==0 and ((t['side']=="LONG" and cp>=t['tp1']) or (t['side']=="SHORT" and cp<=t['tp1'])):
                    t['tp1_hit']=1; t['sl']=t['entry']
                    send_tg(f"🎯 *TP1 達標 · 止損移至成本*\n💎 #{cs}\n✅ 第一止盈觸及\n📍 現價:{cp:.4f}\n💰 TP1(+{abs(t['tp1']-t['entry'])/rr:.1f}R):{t['tp1']:.4f} ✅\n💰 TP2:{t['tp2']:.4f} | TP3:{t['tp3']:.4f}\n🔒 SL 已移至成本:{t['entry']:.4f}\n💡 *建議平 50% 鎖 +1R*")
                if t['locked']==0 and ((t['side']=="LONG" and cp>=t['tp2']) or (t['side']=="SHORT" and cp<=t['tp2'])):
                    t['locked']=1; t['sl']=t['tp1']
                    send_tg(f"🔒 *TP2 達標 · 鎖利*\n💎 #{cs}\n✅ TP2 觸及\n📍 現價:{cp:.4f}\n🚫 新 SL:{t['tp1']:.4f} (+1R)\n💰 TP3:{t['tp3']:.4f}")
                
                isl=((t['side']=="LONG" and cp<=t['sl']) or (t['side']=="SHORT" and cp>=t['sl']))
                it3=((t['side']=="LONG" and cp>=t['tp3']) or (t['side']=="SHORT" and cp<=t['tp3']))
                if isl or it3:
                    ib=isl and t['locked']==1; res="SL" if (isl and not ib) else "TP"
                    rl="💰 TP3 達標" if it3 else ("🔒 保本" if ib else "❌ 止損")
                    send_tg(f"🏁 *結算 {rl}*\n💎 #{cs}\n📍 離場:{cp:.4f}\n🚫 SL:{t['sl']:.4f}\n💰 TP1/2/3: {t['tp1']:.4f}/{t['tp2']:.4f}/{t['tp3']:.4f}\n📊 結果:{'✅ 盈' if res=='TP' else '❌ 虧'}\n🕯️ PA:{t['pa_score']*100:.0f}分 | 評:{t['setup_score']*100:.0f}分")
                    if not PAPER_TRADING:
                        update_whale_stats(t.get('whale_category','Unknown'), res)
                        pd.DataFrame([{"instId":iid,"result":res,"whale_signal":t['whale_signal'],"whale_confidence":t['whale_confidence'],"whale_category":t.get('whale_category','Unknown')}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                    time.sleep(0.2); continue
                upd.append(t)
            time.sleep(0.2)
        if upd: pd.DataFrame(upd).to_csv(LOG_FILE, index=False)
    except Exception: traceback.print_exc()

if __name__ == "__main__": main()
