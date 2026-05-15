# indicators.py
import math
from typing import List, Dict, Any

def calc_atr(df: List[Dict], period: int = 14) -> float:
    """ATR — Wilder EMA 平滑法"""
    if len(df) < period + 1:
        return 0.001
    trs = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i-1]["c"])
        lc = abs(df[i]["l"] - df[i-1]["c"])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return 0.001
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period-1) + tr) / period
    return atr if atr > 0 else 0.001

def calc_rsi(df: List[Dict], period: int = 14) -> float:
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i-1]["c"]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(-ch if ch < 0 else 0.0)
    if len(gains) < period:
        return 50.0
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def calc_adx(df: List[Dict], period: int = 14) -> float:
    if len(df) < period * 2 + 2:
        return 0.0
    pdms, mdms, trs = [], [], []
    for i in range(1, len(df)):
        up = df[i]["h"] - df[i-1]["h"]
        dn = df[i-1]["l"] - df[i]["l"]
        pdms.append(up if (up > dn and up > 0) else 0.0)
        mdms.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(df[i]["h"] - df[i]["l"],
                       abs(df[i]["h"] - df[i-1]["c"]),
                       abs(df[i]["l"] - df[i-1]["c"])))
    if len(trs) < period:
        return 0.0
    s_pdm = sum(pdms[:period])
    s_mdm = sum(mdms[:period])
    s_tr = sum(trs[:period])
    dxs = []
    for i in range(period, len(trs)):
        s_pdm = s_pdm - s_pdm/period + pdms[i]
        s_mdm = s_mdm - s_mdm/period + mdms[i]
        s_tr = s_tr - s_tr/period + trs[i]
        if s_tr == 0:
            continue
        pdi = 100 * s_pdm / s_tr
        mdi = 100 * s_mdm / s_tr
        denom = pdi + mdi
        if denom == 0:
            continue
        dxs.append(100 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return 0.0
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period-1) + dx) / period
    return round(adx, 2)

def calc_supertrend(df: List[Dict], period: int = 10, mult: float = 3.0) -> int:
    if len(df) < period + 2:
        return 0
    atr = calc_atr(df, period)
    mid = sum(r["c"] for r in df[-20:]) / 20
    cur = df[-1]["c"]
    band = atr * 0.5
    if cur > mid + band:
        return 1
    if cur < mid - band:
        return -1
    return 0

def calc_ema(df: List[Dict], period: int) -> List[float]:
    closes = [r["c"] for r in df]
    result = [None] * len(closes)
    if len(closes) < period:
        return result
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result[period-1] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = ema
    return result

def calc_ema_last(df: List[Dict], period: int) -> float:
    series = calc_ema(df, period)
    vals = [v for v in series if v is not None]
    return vals[-1] if vals else df[-1]["c"]

def calc_bollinger(df: List[Dict], period: int = 20, std_mult: float = 2.0) -> Dict:
    if len(df) < period:
        return {}
    closes = [r["c"] for r in df]
    mid = sum(closes[-period:]) / period
    var = sum((c - mid)**2 for c in closes[-period:]) / period
    std = var ** 0.5
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bw = (upper - lower) / mid if mid else 0
    hist_bws = []
    for i in range(period, min(len(df), period+125)):
        seg = closes[-(period+i):(-i) if i else None]
        if len(seg) < period:
            break
        m = sum(seg[-period:]) / period
        v = sum((c - m)**2 for c in seg[-period:]) / period
        s = v ** 0.5
        if m:
            hist_bws.append((m + std_mult*s - (m - std_mult*s)) / m)
    squeeze = bool(hist_bws and bw <= min(hist_bws))
    cur = closes[-1]
    pct_b = (cur - lower) / (upper - lower) if (upper - lower) else 0.5
    return {
        "mid": mid, "upper": upper, "lower": lower,
        "bandwidth": round(bw, 5), "squeeze": squeeze, "pct_b": round(pct_b, 3)
    }

def calc_obv(df: List[Dict]) -> float:
    if len(df) < 10:
        return 0.0
    obv = 0.0
    obvs = []
    for i in range(1, len(df)):
        if df[i]["c"] > df[i-1]["c"]:
            obv += df[i]["v"]
        elif df[i]["c"] < df[i-1]["c"]:
            obv -= df[i]["v"]
        obvs.append(obv)
    if len(obvs) < 5:
        return 0.0
    slope = obvs[-1] - obvs[-5]
    if slope > 0:
        return 1.0
    if slope < 0:
        return -1.0
    return 0.0

def calc_vwap(df: List[Dict]) -> float:
    total_vol = sum(r["v"] for r in df)
    if total_vol == 0:
        return df[-1]["c"]
    tp_vol = sum(((r["h"]+r["l"]+r["c"])/3) * r["v"] for r in df)
    return tp_vol / total_vol

def calc_pivot_sr(df: List[Dict]) -> Dict:
    if len(df) < 20:
        return {}
    seg = df[-20:]
    high = max(r["h"] for r in seg)
    low = min(r["l"] for r in seg)
    close = df[-1]["c"]
    pp = (high + low + close) / 3
    r1 = 2*pp - low; s1 = 2*pp - high
    r2 = pp + (high - low); s2 = pp - (high - low)
    r3 = high + 2*(pp - low); s3 = low - 2*(high - pp)
    return {"pp": pp, "r1": r1, "r2": r2, "r3": r3, "s1": s1, "s2": s2, "s3": s3}

def calc_fibonacci_sr(df: List[Dict], lookback: int = 100) -> Dict:
    seg = df[-lookback:] if len(df) >= lookback else df
    high = max(r["h"] for r in seg)
    low = min(r["l"] for r in seg)
    diff = high - low
    if diff == 0:
        return {}
    levels = {}
    for ratio, label in [(0.0,"f0"),(0.236,"f236"),(0.382,"f382"),(0.5,"f500"),(0.618,"f618"),(0.786,"f786"),(1.0,"f100")]:
        levels[label] = round(high - diff*ratio, 6)
    levels["swing_high"] = high
    levels["swing_low"] = low
    return levels

def nearest_sr_levels(price: float, pivot: Dict, fib: Dict, n: int = 3) -> Dict:
    all_levels = []
    for v in pivot.values():
        if isinstance(v, float):
            all_levels.append(v)
    for k, v in fib.items():
        if k not in ("swing_high","swing_low") and isinstance(v, float):
            all_levels.append(v)
    all_levels = sorted(set(round(v,6) for v in all_levels))
    supports = [v for v in all_levels if v < price*0.9998]
    resists = [v for v in all_levels if v > price*1.0002]
    return {"nearest_sup": supports[-n:] if supports else [], "nearest_res": resists[:n] if resists else []}

def detect_rsi_divergence(df: List[Dict], side: str, rsi_period: int = 14) -> Dict:
    if len(df) < rsi_period + 20:
        return {"regular": False, "hidden": False, "desc": ""}
    closes = [r["c"] for r in df]
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(ch if ch>0 else 0.0)
        losses.append(-ch if ch<0 else 0.0)
    if len(gains) < rsi_period:
        return {"regular": False, "hidden": False, "desc": ""}
    avg_g = sum(gains[:rsi_period]) / rsi_period
    avg_l = sum(losses[:rsi_period]) / rsi_period
    rsi_series = []
    for i in range(rsi_period, len(gains)):
        avg_g = (avg_g * (rsi_period-1) + gains[i]) / rsi_period
        avg_l = (avg_l * (rsi_period-1) + losses[i]) / rsi_period
        rs = avg_g / avg_l if avg_l else 100
        rsi_series.append(100 - 100/(1+rs))
    if len(rsi_series) < 10:
        return {"regular": False, "hidden": False, "desc": ""}
    lookback = min(50, len(rsi_series)-1)
    rsi_seg = rsi_series[-lookback:]
    price_seg = closes[-lookback:]
    
    def find_pivots_low(series, w=3):
        pivots = []
        for i in range(w, len(series)-w):
            if all(series[i] <= series[i-j] for j in range(1,w+1)) and all(series[i] <= series[i+j] for j in range(1,w+1)):
                pivots.append((i, series[i]))
        return pivots
    def find_pivots_high(series, w=3):
        pivots = []
        for i in range(w, len(series)-w):
            if all(series[i] >= series[i-j] for j in range(1,w+1)) and all(series[i] >= series[i+j] for j in range(1,w+1)):
                pivots.append((i, series[i]))
        return pivots
    
    regular = hidden = False
    desc = ""
    if side == "LONG":
        price_lows = find_pivots_low(price_seg)
        rsi_lows = find_pivots_low(rsi_seg)
        if len(price_lows)>=2 and len(rsi_lows)>=2:
            p1,p2 = price_lows[-2], price_lows[-1]
            r1_idx = min(rsi_lows, key=lambda x: abs(x[0]-p1[0]))
            r2_idx = min(rsi_lows, key=lambda x: abs(x[0]-p2[0]))
            price_down = p2[1] < p1[1]
            rsi_up = r2_idx[1] > r1_idx[1]
            price_up = p2[1] > p1[1]
            rsi_down = r2_idx[1] < r1_idx[1]
            if price_down and rsi_up:
                regular = True
                desc = "📈 正規多頭背離（價格新低但RSI不新低）→ 底部反轉"
            elif price_up and rsi_down:
                hidden = True
                desc = "🔒 隱藏多頭背離（RSI新低但價格未創新低）→ 趨勢延續"
    else:  # SHORT
        price_highs = find_pivots_high(price_seg)
        rsi_highs = find_pivots_high(rsi_seg)
        if len(price_highs)>=2 and len(rsi_highs)>=2:
            p1,p2 = price_highs[-2], price_highs[-1]
            r1_idx = min(rsi_highs, key=lambda x: abs(x[0]-p1[0]))
            r2_idx = min(rsi_highs, key=lambda x: abs(x[0]-p2[0]))
            price_up = p2[1] > p1[1]
            rsi_down = r2_idx[1] < r1_idx[1]
            price_down = p2[1] < p1[1]
            rsi_up = r2_idx[1] > r1_idx[1]
            if price_up and rsi_down:
                regular = True
                desc = "📉 正規空頭背離（價格新高但RSI不新高）→ 頂部反轉"
            elif price_down and rsi_up:
                hidden = True
                desc = "🔒 隱藏空頭背離（RSI新高但價格未創新高）→ 趨勢延續"
    return {"regular": regular, "hidden": hidden, "desc": desc}
