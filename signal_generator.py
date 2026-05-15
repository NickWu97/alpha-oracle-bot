# signal_generator.py
import time
from typing import Dict, List, Optional, Tuple
from indicators import (
    calc_atr, calc_rsi, calc_supertrend, calc_obv, calc_vwap,
    calc_bollinger, detect_rsi_divergence, calc_adx,
    find_order_block, find_fvg, calc_snr, detect_price_action,
    detect_liquidity_sweep, calc_momentum_ratio, calc_pivot_sr,
    calc_fibonacci_sr, nearest_sr_levels, detect_pullback
)
from config import config
from risk_manager import risk_mgr

# 輔助函數（簡化版，若需要完整版可從 v15 複製，此處提供核心）
def find_order_block(df, side, lookback=30):
    # 此處需要實作，為節省篇幅請從 v15 複製
    return None
def find_fvg(df, side, lookback=30):
    return None
def calc_snr(df, lookback=100):
    return min(r["l"] for r in df[-lookback:]), max(r["h"] for r in df[-lookback:])
def detect_price_action(df, side):
    return False
def detect_liquidity_sweep(df, side, lookback=20):
    return False
def calc_momentum_ratio(df, side, n=5):
    seg = df[-n:]
    bull = sum(1 for r in seg if r["c"] > r["o"])
    ratio = bull / n
    return ratio >= 0.6 if side=="LONG" else ratio <= 0.4
def detect_pullback(df, side):
    return False

def calc_score(df: List[Dict], side: str, current_price: float, mtf: Dict = None) -> Tuple[int, str, Dict]:
    """回傳 (score, grade, detail)"""
    detail = {}
    score = 0
    st = calc_supertrend(df)
    if (side=="LONG" and st==1) or (side=="SHORT" and st==-1):
        score += 30; detail["trend"]=30
    elif st==0:
        score += 15; detail["trend"]=15
    else:
        detail["trend"]=0
    rsi = calc_rsi(df)
    detail["rsi_value"]=round(rsi,1)
    if side=="LONG":
        if 30<=rsi<=50: score+=25; detail["rsi"]=25
        elif 50<rsi<70: score+=15; detail["rsi"]=15
        else: detail["rsi"]=0
    else:
        if 50<=rsi<=70: score+=25; detail["rsi"]=25
        elif 30<rsi<50: score+=15; detail["rsi"]=15
        else: detail["rsi"]=0
    # OB, FVG, SNR, PA, Liq, Mom 等簡化（實際可從v15複製完整）
    ob = find_order_block(df, side)
    if ob and ob["low"]*0.995 <= current_price <= ob["high"]*1.005:
        score+=20; detail["ob"]=20
    fvg = find_fvg(df, side)
    if fvg and fvg["low"]*0.997 <= current_price <= fvg["high"]*1.003:
        score+=15; detail["fvg"]=15
    # MTF
    if mtf:
        expect = 1 if side=="LONG" else -1
        h1 = mtf.get("1H",{}).get("supertrend",0)
        h4 = mtf.get("4H",{}).get("supertrend",0)
        mtf_score = 0
        if h1==expect: mtf_score+=8
        elif h1==-expect: mtf_score-=5
        if h4==expect: mtf_score+=7
        elif h4==-expect: mtf_score-=5
        score += mtf_score
        detail["mtf"]=mtf_score
    # OBV
    obv_dir = calc_obv(df)
    expect = 1 if side=="LONG" else -1
    obv_score = 5 if obv_dir==expect else (-3 if obv_dir==-expect else 0)
    score += obv_score; detail["obv"]=obv_score
    # VWAP
    vwap = calc_vwap(df)
    vwap_score = 0
    if side=="LONG" and current_price>vwap: vwap_score=5
    elif side=="SHORT" and current_price<vwap: vwap_score=5
    score+=vwap_score; detail["vwap_score"]=vwap_score
    # BB Squeeze
    bb = calc_bollinger(df)
    if bb.get("squeeze"):
        score+=8; detail["bb_squeeze"]=True
    # RSI 背離
    div = detect_rsi_divergence(df, side)
    if div.get("regular"):
        score+=12; detail["rsi_div"]="regular"
    elif div.get("hidden"):
        score+=6; detail["rsi_div"]="hidden"
    grade = "A+ 極強" if score>=85 else "A 強力" if score>=70 else "B+ 合格" if score>=68 else "觀望"
    return score, grade, detail

def is_ranging(df: List[Dict]) -> bool:
    adx = calc_adx(df)
    bb = calc_bollinger(df)
    return adx < 20 and bb.get("bandwidth", 1) < 0.05

def generate_signal(instId: str, df: List[Dict], current_price: float, funding_rate: Optional[float], mtf: Dict = None) -> Optional[Dict]:
    if df is None or len(df) < 50 or is_ranging(df):
        return None
    atr = calc_atr(df)
    if atr / current_price > config.get("atr_max_pct", 0.035):
        return None
    threshold = config.get("score_threshold", 70)
    candidates = []
    for side in ("LONG", "SHORT"):
        score, grade, detail = calc_score(df, side, current_price, mtf)
        if score < threshold:
            continue
        entry = current_price
        sl = risk_mgr.calculate_stop_loss(entry, side, atr=atr, method="atr", atr_mult=1.5)
        risk = abs(entry - sl)
        if side == "LONG":
            tp_levels = [entry + risk*1.5, entry + risk*3.0, entry + risk*5.0]
        else:
            tp_levels = [entry - risk*1.5, entry - risk*3.0, entry - risk*5.0]
        # 動態進場區間
        entry_zone = config.get("entry_zone_atr_mult", 0.3) * atr
        candidates.append({
            "instId": instId, "side": side, "entry": round(entry,4),
            "entry_low": round(entry - entry_zone,4),
            "entry_high": round(entry + entry_zone,4),
            "sl": round(sl,4), "tp1": round(tp_levels[0],4),
            "tp2": round(tp_levels[1],4), "tp3": round(tp_levels[2],4),
            "score": score, "grade": grade, "detail": detail,
            "funding_rate": funding_rate, "mtf_snapshot": mtf,
            "created": time.time(), "expires": time.time() + config.get("signal_expire_hours",24)*3600
        })
    return max(candidates, key=lambda x: x["score"]) if candidates else None
