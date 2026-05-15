# signal_generator.py
import time
from typing import Dict, List, Optional
from config import config
from indicators import (
    calc_atr, calc_rsi, calc_supertrend, calc_adx, calc_obv, calc_vwap,
    calc_bollinger, detect_rsi_divergence, find_order_block, find_fvg,
    detect_price_action, detect_liquidity_sweep, calc_momentum_ratio, detect_pullback
)
from risk_manager import risk_mgr

def calc_score(df, side, current_price, mtf=None):
    # 原有評分邏輯（保持不變，此處省略詳細代碼，請複製先前版本）
    # 建議直接從先前提供的 signal_generator.py 複製 calc_score 完整內容
    score = 70  # 佔位，實際需完整實作
    grade = "合格"
    detail = {}
    return score, grade, detail

def should_enter_by_mtf(side: str, mtf_data: dict) -> bool:
    """多週期確認：1H 和 4H 不可與訊號方向相反"""
    expect = 1 if side == "LONG" else -1
    h1 = mtf_data.get("1H", {}).get("supertrend", 0)
    h4 = mtf_data.get("4H", {}).get("supertrend", 0)
    if h1 == -expect or h4 == -expect:
        return False
    return True

def generate_counter_signal(instId, df, current_price, funding_rate, mtf=None) -> Optional[Dict]:
    """逆勢策略：RSI 極限反轉（RSI<25 做多，RSI>75 做空）並搭配 ADX<20"""
    if len(df) < 50:
        return None
    rsi = calc_rsi(df)
    adx = calc_adx(df)
    if adx > 20:  # 趨勢明顯時不抓反轉
        return None
    atr = calc_atr(df)
    if atr / current_price > config.get("atr_max_pct", 0.035):
        return None
    side = None
    if rsi < 25:
        side = "LONG"
    elif rsi > 75:
        side = "SHORT"
    else:
        return None
    # 評分機制（反轉策略較低門檻）
    score = 65
    entry = current_price
    sl = risk_mgr.calculate_stop_loss(entry, side, atr=atr, method="atr", atr_mult=1.2)
    risk = abs(entry - sl)
    if side == "LONG":
        tp_levels = [entry + risk*1.2, entry + risk*2.0, entry + risk*3.0]
    else:
        tp_levels = [entry - risk*1.2, entry - risk*2.0, entry - risk*3.0]
    entry_zone = config.get("entry_zone_atr_mult", 0.3) * atr
    return {
        "instId": instId, "side": side, "entry": round(entry,4),
        "entry_low": round(entry - entry_zone,4), "entry_high": round(entry + entry_zone,4),
        "sl": round(sl,4), "tp1": round(tp_levels[0],4), "tp2": round(tp_levels[1],4), "tp3": round(tp_levels[2],4),
        "score": score, "grade": "逆勢", "detail": {"rsi": rsi, "adx": adx},
        "funding_rate": funding_rate, "mtf_snapshot": mtf,
        "created": time.time(), "expires": time.time() + config.get("signal_expire_hours",24)*3600
    }

def generate_signal(instId, df, current_price, funding_rate, mtf=None):
    # 原有順勢策略（保持不變，此處僅供參考）
    # 實際請複製先前版本
    return None
