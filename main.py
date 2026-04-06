import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

# 高度相關幣種組（同組最多同時持有 1 個方向）
CORR_GROUPS = [
    {"BTC-USDT-SWAP", "ETH-USDT-SWAP"},                          # BTC 系
    {"SOL-USDT-SWAP", "AVAX-USDT-SWAP", "APT-USDT-SWAP"},        # L1 公鏈
    {"LINK-USDT-SWAP", "ADA-USDT-SWAP", "XRP-USDT-SWAP"},        # 其他主流
]

# 每日最大止損次數（超過後當天停止接受新訊號）
MAX_DAILY_SL = 2

# 最大同時持倉數（WAITING + ACTIVE 合計，防止市場大跌時全部中彈）
MAX_CONCURRENT = 3

# 止損最小距離（佔進場價 %），低於此值代表 SL 太緊，直接跳過
SL_MIN_PCT = 0.007  # 0.7%

# 止損/止盈後冷卻期（單位：K 棒，每棒 15 分鐘）
# 16 棒 = 4 小時，防止同幣短時間反覆進出
COOLDOWN_BARS = 16

LOG_FILE            = "active_trades.csv"
STATS_FILE          = "daily_stats.csv"
WAITING_EXPIRY_BARS = 20

LOG_COLS   = ["instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3", "locked", "wait_since", "tp1_hit"]
STATS_COLS = ["instId", "result", "date"]


# ─────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def safe_int(val, fallback=0):
    try:    return int(float(val))
    except: return fallback

def normalize_trade(t: dict) -> dict:
    return {
        "instId":     str(t.get("instId", "")),
        "side":       str(t.get("side", "")),
        "status":     str(t.get("status", "")),
        "entry":      safe_float(t.get("entry")),
        "sl":         safe_float(t.get("sl")),
        "tp1":        safe_float(t.get("tp1")),
        "tp2":        safe_float(t.get("tp2")),
        "tp3":        safe_float(t.get("tp3")),
        "locked":     safe_int(t.get("locked")),
        "wait_since": safe_int(t.get("wait_since", 0)),
        "tp1_hit":    safe_int(t.get("tp1_hit", 0)),
    }


# ─────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────

def fetch_okx(instId: str, bar: str = "15m", limit: int = 100) -> pd.DataFrame | None:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        df  = pd.DataFrame(
            res['data'],
            columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
        )
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] {bar} K 線抓取失敗: {e}")
        return None

def fetch_current_candle_hl(instId: str) -> tuple[float, float]:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
        res = requests.get(url, timeout=5).json()
        for row in res['data']:
            if row[8] == "0":
                return float(row[3]), float(row[2])
    except Exception as e:
        logging.warning(f"[{instId}] 當前 K 棒抓取失敗: {e}")
    return float('inf'), float('-inf')

def get_funding_ls(instId: str) -> tuple[str, str]:
    base_id  = instId.replace("-SWAP", "").split("-")[0]
    funding  = "N/A"
    ls_ratio = "N/A"
    try:
        f_res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except: pass
    try:
        ls_res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5
        ).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except: pass
    return funding, ls_ratio

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        logging.warning(f"Telegram 發送失敗: {e}")


# ─────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df['c'].ewm(span=period, adjust=False).mean()

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
    delta = df['c'].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / (loss + 1e-10)
    return (100 - (100 / (1 + rs))).iloc[-1]

def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> tuple[float, str]:
    recent = df.tail(lookback).copy()
    body   = (recent['h'] - recent['l']).replace(0, 1e-10)
    recent['delta'] = np.where(
        recent['c'] >= recent['o'],
        recent['v'] * (recent['c'] - recent['l']) / body,
        -recent['v'] * (recent['h'] - recent['c']) / body
    )
    cvd = recent['delta'].sum()
    return cvd, ("🟢 大戶吸籌 (CVD+)" if cvd > 0 else "🔴 大戶出貨 (CVD-)")

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> int:
    if len(df) < period + 2:
        return 0
    high  = df['h'].values.astype(float)
    low   = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n     = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    hl2      = (high+low) / 2.0
    basic_up = hl2 - multiplier*atr
    basic_dn = hl2 + multiplier*atr
    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)
    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]
    for i in range(period+1, n):
        final_up[i] = (basic_up[i] if basic_up[i]>final_up[i-1] or close[i-1]<final_up[i-1] else final_up[i-1])
        final_dn[i] = (basic_dn[i] if basic_dn[i]<final_dn[i-1] or close[i-1]>final_dn[i-1] else final_dn[i-1])
        if   trend[i-1]==-1 and close[i]>final_dn[i-1]: trend[i] = 1
        elif trend[i-1]== 1 and close[i]<final_up[i-1]: trend[i] = -1
        else:                                             trend[i] = trend[i-1]
    return int(trend[-1])


# ─────────────────────────────────────────────
# 5. SMC 結構分析
# ─────────────────────────────────────────────

def find_swing_points(df, n=2, lookback=80):
    data = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    for i in range(n, len(data)-n):
        if data['h'].iloc[i] == data['h'].iloc[i-n:i+n+1].max(): highs.append(data['h'].iloc[i])
        if data['l'].iloc[i] == data['l'].iloc[i-n:i+n+1].min(): lows.append(data['l'].iloc[i])
    return sorted(set(highs)), sorted(set(lows))

def detect_market_structure(df):
    highs, lows = find_swing_points(df, n=3, lookback=60)
    if len(lows)>=2 and lows[-2]>0 and abs(lows[-2]-lows[-1])/lows[-2]<0.015:  return "W底反轉 📐"
    if len(highs)>=2 and highs[-2]>0 and abs(highs[-2]-highs[-1])/highs[-2]<0.015: return "M頭反轉 📐"
    slope = (df['c'].iloc[-1]-df['c'].iloc[-20]) / (df['c'].iloc[-20]+1e-10)
    if slope>0.025:  return "上升趨勢延續 📈"
    if slope<-0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

def find_order_block(df, side, lookback=15):
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data)-2, 0, -1):
        k, kn = data.iloc[i], data.iloc[i+1]
        if side=="LONG"  and k['c']<k['o'] and kn['c']>kn['o']: return {"high":k['o'],"low":k['l']}
        if side=="SHORT" and k['c']>k['o'] and kn['c']<kn['o']: return {"high":k['h'],"low":k['c']}
    return None

def find_recent_fvg(df, side):
    for i in range(len(df)-3, max(len(df)-20,0), -1):
        k0, k2 = df.iloc[i-1], df.iloc[i+1]
        if side=="LONG"  and k2['l']>k0['h']: return {"high":k2['l'],"low":k0['h']}
        if side=="SHORT" and k2['h']<k0['l']: return {"high":k0['l'],"low":k2['h']}
    return None

def calculate_structural_sl(df, side, entry, atr):
    """
    計算止損位置。
    邏輯優先順序：
    1. 結構止損（OB / FVG 底部，加 0.25 ATR buffer）
    2. 若結構止損距離 < 0.7%，改用 ATR*2.0（更寬鬆）
    3. 兜底使用 ATR*2.0
    確保止損至少 0.7%（SL_MIN_PCT），避免被噪音掃出
    """
    buffer  = atr * 0.25
    min_atr = atr * 2.0   # 預設最小止損寬度（原本 1.5，擴大至 2.0）

    ob, fvg = find_order_block(df, side), find_recent_fvg(df, side)
    if side == "LONG":
        cands = []
        if ob  and ob['low']  < entry: cands.append(ob['low']  - buffer)
        if fvg and fvg['low'] < entry: cands.append(fvg['low'] - buffer)
        if cands:
            sl = max(cands)
            # 結構 SL 至少 0.7%，否則用 ATR*2.0
            return sl if (entry - sl) / (entry + 1e-10) >= SL_MIN_PCT else entry - min_atr
        return entry - min_atr
    else:
        cands = []
        if ob  and ob['high']  > entry: cands.append(ob['high']  + buffer)
        if fvg and fvg['high'] > entry: cands.append(fvg['high'] + buffer)
        if cands:
            sl = min(cands)
            return sl if (sl - entry) / (entry + 1e-10) >= SL_MIN_PCT else entry + min_atr
        return entry + min_atr

def get_fixed_r_tps(entry, sl, side):
    risk = abs(entry-sl)+1e-10
    if side=="LONG":  return entry+risk, entry+risk*2, entry+risk*3
    else:             return entry-risk, entry-risk*2, entry-risk*3

def suggest_leverage(atr, price):
    v = (atr/(price+1e-10))*100
    if v>3:   return "3x~5x",   "⚠️ 高波動"
    if v>1.5: return "5x~10x",  "中波動"
    return "10x~20x", "低波動"


# ─────────────────────────────────────────────
# 6. 過濾器（共八層）
# ─────────────────────────────────────────────

def fetch_funding_rate_raw(instId):
    try:
        res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",timeout=5).json()
        return float(res['data'][0]['fundingRate'])
    except: return 0.0

def is_trending_market(df):
    if len(df)<50: return True
    tr = pd.concat([df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())], axis=1).max(axis=1)
    return tr.rolling(14).mean().iloc[-1] > tr.tail(50).mean()*0.7

def get_btc_direction(btc_df, lookback=5):
    if btc_df is None or len(btc_df)<lookback: return "NEUTRAL"
    recent  = btc_df.tail(lookback)
    bearish = int((recent['c']<recent['o']).sum())
    if bearish>=4: return "DOWN"
    if (lookback-bearish)>=4: return "UP"
    return "NEUTRAL"

def check_ema_trend(df, side):
    """過濾器⑥：EMA50 順勢過濾"""
    if len(df)<55: return True
    ema50 = calculate_ema(df,50).iloc[-1]
    curr  = df['c'].iloc[-1]
    if side=="LONG"  and curr<ema50: return False
    if side=="SHORT" and curr>ema50: return False
    return True

def check_rsi_filter(df, side):
    """過濾器⑦：RSI 過濾（避免追高殺低）"""
    if len(df)<20: return True
    rsi = calculate_rsi(df)
    if side=="LONG"  and not (35<=rsi<=68): return False
    if side=="SHORT" and not (32<=rsi<=65): return False
    return True

def check_1h_trend(instId, side):
    """過濾器⑧：1H Supertrend 跨週期確認"""
    df_1h = fetch_okx(instId, bar="1H", limit=60)
    if df_1h is None or len(df_1h)<15: return True, "⚪ N/A"
    st = calculate_supertrend(df_1h)
    label = "📈 1H多頭" if st==1 else ("📉 1H空頭" if st==-1 else "⚪ 1H未知")
    if st==0: return True, label
    if side=="LONG"  and st!= 1: return False, label
    if side=="SHORT" and st!=-1: return False, label
    return True, label

def check_4h_trend(instId, side):
    """
    過濾器⑪：4H Supertrend 大週期確認（最關鍵過濾器）
    ─────────────────────────────────────────────────────
    為什麼 4H 比 1H 更重要？
    1H 多頭可以只是 4H 下跌趨勢中的短暫反彈（死貓跳）。
    若 4H 也是多頭，代表大趨勢順方向，勝率大幅提升。
    反之，4H 空頭做多 = 逆大趨勢，絕大多數會止損。

    同時也檢查 4H EMA50 位置：
    - LONG：價格需在 4H EMA50 上方（大趨勢上行）
    - SHORT：價格需在 4H EMA50 下方（大趨勢下行）
    """
    df_4h = fetch_okx(instId, bar="4H", limit=80)
    if df_4h is None or len(df_4h) < 15:
        return True, "⚪ 4H N/A"

    st_4h   = calculate_supertrend(df_4h)
    ema50_4h = calculate_ema(df_4h, 50).iloc[-1]
    price_4h = df_4h['c'].iloc[-1]
    ema_ok   = (price_4h > ema50_4h) if side == "LONG" else (price_4h < ema50_4h)

    if st_4h == 1:    st_label = "📈 4H多頭"
    elif st_4h == -1: st_label = "📉 4H空頭"
    else:             st_label = "⚪ 4H未知"

    ema_label = f"{'↑' if ema_ok else '↓'} 4H EMA50"
    label = f"{st_label} | {ema_label}"

    # 4H Supertrend 必須與方向一致（否則直接拒絕）
    if st_4h != 0:
        if side == "LONG"  and st_4h != 1:  return False, label
        if side == "SHORT" and st_4h != -1: return False, label

    # 4H EMA50 位置也必須一致（雙重確認）
    if not ema_ok:
        return False, label

    return True, label

def check_btc_4h_trend(btc_df_4h, side):
    """
    BTC 4H 趨勢過濾（非 BTC 幣種使用）
    BTC 是整個市場的錨定點。若 BTC 4H 是空頭，做 altcoin 多單
    等於逆市場大方向，是高風險行為。
    """
    if btc_df_4h is None or len(btc_df_4h) < 15:
        return True, "⚪ BTC 4H N/A"
    st = calculate_supertrend(btc_df_4h)
    label = f"BTC 4H {'📈多頭' if st==1 else ('📉空頭' if st==-1 else '⚪未知')}"
    if st == 0: return True, label
    if side == "LONG"  and st != 1:  return False, label
    if side == "SHORT" and st != -1: return False, label
    return True, label

def is_trading_session(now_tw: datetime) -> tuple[bool, str]:
    """
    過濾器⑨：交易時段過濾
    只在流動性高的時段接受新訊號，避免亞洲盤假突破。

    台灣時間（UTC+8）：
    ✅ 歐洲盤  15:00 ~ 00:00  → 最佳時段
    ✅ 美國盤  21:00 ~ 06:00  → 最佳時段
    ❌ 亞洲盤  06:00 ~ 15:00  → 低流動性、假突破多，跳過
    """
    h = now_tw.hour
    # 06:00 ~ 14:59 台灣時間 = 亞洲盤，跳過
    if 6 <= h < 15:
        return False, f"⏸ 亞洲盤休息 ({h:02d}:00 TW)"
    return True, f"✅ 活躍時段 ({h:02d}:00 TW)"

def get_today_sl_count(stats_file: str, today_str: str) -> int:
    """計算今天已止損次數（用於每日止損上限）"""
    try:
        df = pd.read_csv(stats_file)
        if 'date' not in df.columns: return 0
        return len(df[(df['result']=='SL') & (df['date']==today_str)])
    except: return 0

def check_correlated_group(instId: str, active_trades_df: pd.DataFrame, side: str) -> bool:
    """
    過濾器⑩：相關幣種去重
    同一相關組（如 BTC/ETH）同方向最多只持有 1 個倉位。
    避免高度相關幣種雙重押注，分散風險更有效。
    """
    for group in CORR_GROUPS:
        if instId not in group: continue
        if active_trades_df.empty: return True
        for _, row in active_trades_df.iterrows():
            if row['instId'] in group and row['instId'] != instId and row['side'] == side:
                return False  # 同組已有同方向倉位
    return True

def classify_trade(side, structure, risk_pct):
    if "反轉" in structure: return "📊 長單 (波段)"
    if risk_pct<1.0:        return "⚡ 短單 (日內)"
    return "📊 長單 (波段)"


# ─────────────────────────────────────────────
# 7. SMC 訊號掃描
# ─────────────────────────────────────────────

def find_smc_setup(df: pd.DataFrame) -> dict | None:
    if df is None or len(df)<55: return None

    atr  = calculate_atr(df)
    best = None

    for i in range(len(df)-3, len(df)-25, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]

        # BOS 強棒過濾：實體 > 50% 振幅
        rng  = k2['h']-k2['l']+1e-10
        body = abs(k2['c']-k2['o'])
        if body/rng < 0.50: continue

        # 量能確認：突破棒成交量 > 近 20 根均量 1.2 倍
        avg_vol = df['v'].iloc[i-20:i].mean()
        if k2['v'] < avg_vol*1.2: continue

        if k2['c']>k2['o'] and k2['c']>df['h'].iloc[i-15:i].max():
            # ⭐ 防假突破：BOS 出現後，確認下一根 K 棒沒有立即跌回突破點以下
            # 假突破特徵：突破棒之後的 K 棒收盤跌回舊高點下方
            breakthrough_level = df['h'].iloc[i-15:i].max()
            next_candle_ok = True
            if i + 2 < len(df):  # 如果有下一根 K 棒可以看
                k3 = df.iloc[i + 2]
                if k3['c'] < breakthrough_level:  # 收盤跌回突破點 = 假突破
                    next_candle_ok = False
            if not next_candle_ok:
                continue  # 假突破，跳過
            entry = k2['l'] if k2['l'] > k0['h'] else k1['c']
            best  = {"side": "LONG", "entry": entry}
        elif k2['c']<k2['o'] and k2['c']<df['l'].iloc[i-15:i].min():
            # 空方假突破確認：BOS 後下一根 K 棒有沒有立即回到突破點上方
            breakthrough_level = df['l'].iloc[i-15:i].min()
            next_candle_ok = True
            if i + 2 < len(df):
                k3 = df.iloc[i + 2]
                if k3['c'] > breakthrough_level:  # 收盤回到突破點上方 = 假突破
                    next_candle_ok = False
            if not next_candle_ok:
                continue
            entry = k2['h'] if k2['h'] < k0['l'] else k1['c']
            best  = {"side": "SHORT", "entry": entry}

    if best is None: return None

    side, entry = best['side'], best['entry']
    price = df['c'].iloc[-1]
    sl    = calculate_structural_sl(df, side, entry, atr)
    tp1, tp2, tp3 = get_fixed_r_tps(entry, sl, side)

    risk      = abs(entry - sl) + 1e-10
    risk_pct  = risk / (entry + 1e-10) * 100

    # 硬性過濾：SL 距離 < 0.7% 代表太緊，噪音就能掃出，直接放棄這個 setup
    if risk_pct < SL_MIN_PCT * 100:
        logging.info(f"SL 太緊 ({risk_pct:.2f}% < {SL_MIN_PCT*100:.1f}%)，跳過")
        return None
    structure = detect_market_structure(df)
    lev, lev_note = suggest_leverage(atr, price)
    trade_type = classify_trade(side, structure, risk_pct)
    _, cvd_label = calculate_cvd(df)

    st_val   = calculate_supertrend(df)
    st_label = "📈 多頭" if st_val==1 else ("📉 空頭" if st_val==-1 else "⚪ 未知")

    ema50     = calculate_ema(df,50).iloc[-1]
    ema_gap   = (price-ema50)/ema50*100
    ema_label = f"{'↑' if price>ema50 else '↓'} EMA50 ({ema_gap:+.2f}%)"
    rsi_val   = calculate_rsi(df)

    return {
        "side":side, "entry":entry, "sl":sl, "tp1":tp1, "tp2":tp2, "tp3":tp3,
        "structure":structure, "leverage":lev, "leverage_note":lev_note,
        "trade_type":trade_type, "cvd_label":cvd_label,
        "st_val":st_val, "st_label":st_label,
        "ema_label":ema_label, "rsi_val":rsi_val,
    }


# ─────────────────────────────────────────────
# 8. 主程式
# ─────────────────────────────────────────────

def main():
    try:
        now_utc       = datetime.utcnow()
        now_tw        = now_utc + timedelta(hours=8)
        today_str     = now_tw.strftime('%Y-%m-%d')
        manual_report = os.getenv("MANUAL_REPORT","false").lower()=="true"

        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size==0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 每日戰績回報（午夜 00:00 台灣時間）─────────────────────────
        is_midnight = (now_tw.hour==0 and 0<=now_tw.minute<15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result']=='TP'])
                    sl_c  = len(df_s[df_s['result']=='SL'])
                    total = tp_c+sl_c
                    wr    = (tp_c/total*100) if total>0 else 0
                    date_str = (now_tw-timedelta(days=1)).strftime('%Y-%m-%d')

                    # 計算期望值（EV）：假設 TP 平均 2R，SL = -1R
                    ev = (wr/100*2.0 + (1-wr/100)*(-1.0)) if total>0 else 0

                    send_tg(
                        f"📊 *Alpha Oracle 每日戰績*\n"
                        f"──────────────────\n"
                        f"📅 日期：{date_str}\n"
                        f"\n"
                        f"✅ 盈利（含保本）：{tp_c} 單\n"
                        f"❌ 止損：{sl_c} 單\n"
                        f"📊 總計：{total} 單\n"
                        f"\n"
                        f"🔥 勝率：*{wr:.1f}%*\n"
                        f"💹 期望值：*{ev:+.2f}R / 單*\n"
                        f"──────────────────\n"
                        f"📌 保本亦計為獲勝\n"
                        f"💡 期望值 > 0 = 長期獲利策略"
                    )
                else:
                    send_tg(
                        f"📊 *Alpha Oracle 每日戰績*\n"
                        f"──────────────────\n"
                        f"📅 日期：{(now_tw-timedelta(days=1)).strftime('%Y-%m-%d')}\n"
                        f"\n"
                        f"📭 今日無成交紀錄"
                    )
                if is_midnight:
                    pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                    with open("midnight.ok","w") as fh: fh.write("ok")
        elif now_tw.hour!=0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # ── B. 核心監控 ───────────────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            if "wait_since" not in trades_df.columns: trades_df["wait_since"] = 0
            if "tp1_hit"    not in trades_df.columns: trades_df["tp1_hit"]    = 0
        except:
            trades_df = pd.DataFrame(columns=LOG_COLS)

        active_ids  = trades_df['instId'].tolist()
        updated     = []
        current_bar = int(now_utc.timestamp()//900)

        # 交易時段檢查（只影響新訊號，不影響追蹤現有倉位）
        session_ok, session_label = is_trading_session(now_tw)

        # 今日止損次數檢查
        today_sl_count = get_today_sl_count(STATS_FILE, today_str)
        daily_limit_hit = today_sl_count >= MAX_DAILY_SL
        if daily_limit_hit:
            logging.info(f"今日已止損 {today_sl_count} 次，達每日上限 {MAX_DAILY_SL}，停止接受新訊號")

        # BTC 方向（15m + 4H）
        btc_df      = fetch_okx("BTC-USDT-SWAP")
        btc_df_4h   = fetch_okx("BTC-USDT-SWAP", bar="4H", limit=80)
        btc_trend   = get_btc_direction(btc_df)
        btc_st_4h   = calculate_supertrend(btc_df_4h) if btc_df_4h is not None and len(btc_df_4h)>=15 else 0
        btc_4h_label = "📈" if btc_st_4h==1 else ("📉" if btc_st_4h==-1 else "⚪")
        logging.info(f"BTC方向:{btc_trend} BTC 4H:{btc_4h_label}  時段:{session_label}  今日SL:{today_sl_count}/{MAX_DAILY_SL}")

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty:
                time.sleep(0.3)
                continue

            curr_p   = df['c'].iloc[-1]
            coin_sym = instId.split('-')[0]

            # ── 1. 掃描新訊號 ────────────────────────────────────────────
            if instId not in active_ids:

                # 時段過濾 / 每日止損上限（新訊號才限制）
                if not session_ok or daily_limit_hit:
                    time.sleep(0.3)
                    continue

                if not is_trending_market(df):
                    logging.info(f"[{instId}] 盤整跳過")
                    time.sleep(0.3)
                    continue

                setup = find_smc_setup(df)
                if not setup:
                    time.sleep(0.3)
                    continue

                # ── 八層過濾器 ───────────────────────────────────────────
                cvd_val, _ = calculate_cvd(df)
                if setup['side']=="LONG"  and cvd_val<0:
                    logging.info(f"[{instId}] CVD- 跳過"); time.sleep(0.3); continue
                if setup['side']=="SHORT" and cvd_val>0:
                    logging.info(f"[{instId}] CVD+ 跳過"); time.sleep(0.3); continue

                fr = fetch_funding_rate_raw(instId)
                if setup['side']=="LONG"  and fr> 0.0005:
                    logging.info(f"[{instId}] 資費過高跳過"); time.sleep(0.3); continue
                if setup['side']=="SHORT" and fr<-0.0005:
                    logging.info(f"[{instId}] 資費過低跳過"); time.sleep(0.3); continue

                if instId!="BTC-USDT-SWAP":
                    if setup['side']=="LONG"  and btc_trend=="DOWN":
                        logging.info(f"[{instId}] BTC下跌跳過"); time.sleep(0.3); continue
                    if setup['side']=="SHORT" and btc_trend=="UP":
                        logging.info(f"[{instId}] BTC上漲跳過"); time.sleep(0.3); continue

                if setup['st_val']==-1 and setup['side']=="LONG":
                    logging.info(f"[{instId}] 15m ST空頭跳過"); time.sleep(0.3); continue
                if setup['st_val']== 1 and setup['side']=="SHORT":
                    logging.info(f"[{instId}] 15m ST多頭跳過"); time.sleep(0.3); continue

                if not check_ema_trend(df, setup['side']):
                    logging.info(f"[{instId}] EMA50跳過"); time.sleep(0.3); continue

                if not check_rsi_filter(df, setup['side']):
                    logging.info(f"[{instId}] RSI={setup['rsi_val']:.1f}跳過"); time.sleep(0.3); continue

                ok_1h, label_1h = check_1h_trend(instId, setup['side'])
                if not ok_1h:
                    logging.info(f"[{instId}] 1H ST跳過"); time.sleep(0.3); continue

                # ⭐ 過濾器⑪：4H Supertrend + 4H EMA50（最高優先級過濾器）
                # 4H 趨勢才是真正的大方向，1H 可能只是反彈
                ok_4h, label_4h = check_4h_trend(instId, setup['side'])
                if not ok_4h:
                    logging.info(f"[{instId}] 4H ST/EMA跳過 ({label_4h})"); time.sleep(0.3); continue

                # ⭐ 過濾器⑫：BTC 4H 趨勢（非 BTC 幣種）
                # BTC 是市場錨定點，BTC 4H 空頭時做 altcoin 多單 = 逆市場大趨勢
                if instId != "BTC-USDT-SWAP":
                    ok_btc4h, label_btc4h = check_btc_4h_trend(btc_df_4h, setup['side'])
                    if not ok_btc4h:
                        logging.info(f"[{instId}] BTC 4H 方向跳過 ({label_btc4h})"); time.sleep(0.3); continue
                else:
                    label_btc4h = "BTC 自身"

                # 相關幣種去重
                if not check_correlated_group(instId, trades_df, setup['side']):
                    logging.info(f"[{instId}] 同組已有倉位跳過"); time.sleep(0.3); continue

                # ⭐ 全局倉位上限：WAITING + ACTIVE 合計不超過 MAX_CONCURRENT
                # 防止市場大跌時多個倉位同時中彈，擴大單次虧損
                open_count = len(trades_df[trades_df['status'].isin(['WAITING', 'ACTIVE'])])
                if open_count >= MAX_CONCURRENT:
                    logging.info(f"[{instId}] 已有 {open_count} 個倉位（上限 {MAX_CONCURRENT}），跳過")
                    time.sleep(0.3)
                    continue

                # ── 全部通過，發出訊號 ───────────────────────────────────
                funding, ls_ratio = get_funding_ls(instId)
                side_zh = "🟢 多單 (LONG)" if setup['side']=="LONG" else "🔴 空單 (SHORT)"

                msg  = f"🔥 *Alpha Oracle 訊號發射* 🔥\n"
                msg += f"──────────────────\n"
                msg += f"💎 幣種：#{coin_sym}\n"
                msg += f"🎯 方向：{side_zh}\n"
                msg += f"⏰ 週期：15m  |  {session_label}\n"
                msg += f"📊 多空比 {ls_ratio} | 資費 {funding} | {setup['cvd_label']}\n"
                msg += f"\n"
                msg += f"📍 進場位：{setup['entry']:.4f}\n"
                msg += f"🚫 止損位：{setup['sl']:.4f}  (-1R)\n"
                msg += f"💰 TP1 (1.0R)：{setup['tp1']:.4f}\n"
                msg += f"💰 TP2 (2.0R)：{setup['tp2']:.4f}\n"
                msg += f"💰 TP3 (3.0R)：{setup['tp3']:.4f}\n"
                msg += f"\n"
                msg += f"🏗️ 結構：{setup['structure']}\n"
                msg += f"📡 Supertrend：{setup['st_label']} | {label_1h} | {label_4h}\n"
                msg += f"₿ BTC 大趨勢：{label_btc4h}\n"
                msg += f"📈 EMA50：{setup['ema_label']}\n"
                msg += f"🔢 RSI：{setup['rsi_val']:.1f}\n"
                msg += f"🕹️ 槓桿：{setup['leverage']} ({setup['leverage_note']})\n"
                msg += f"📌 類型：{setup['trade_type']}\n"
                msg += f"\n"
                msg += f"💡 *等待回踩成交...*"
                send_tg(msg)

                updated.append({
                    "instId":instId, "side":setup['side'], "status":"WAITING",
                    "entry":setup['entry'], "sl":setup['sl'],
                    "tp1":setup['tp1'], "tp2":setup['tp2'], "tp3":setup['tp3'],
                    "locked":0, "wait_since":current_bar, "tp1_hit":0,
                })
                time.sleep(0.3)
                continue

            # ── 2. 追蹤現有單據（不受時段限制）──────────────────────────
            t = normalize_trade(trades_df[trades_df['instId']==instId].iloc[0].to_dict())

            if t['status']=="WAITING":
                if current_bar - t['wait_since'] > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 逾期清除")
                    time.sleep(0.3)
                    continue

                n_check           = min(8, len(df))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low  = min(df['l'].iloc[-n_check:].min(), cur_low,  curr_p)
                check_high = max(df['h'].iloc[-n_check:].max(), cur_high, curr_p)
                is_hit = (
                    (t['side']=="LONG"  and check_low <=t['entry']) or
                    (t['side']=="SHORT" and check_high>=t['entry'])
                )
                already_sl = (
                    (t['side']=="LONG"  and curr_p<t['sl']) or
                    (t['side']=="SHORT" and curr_p>t['sl'])
                )
                if is_hit and already_sl:
                    logging.info(f"[{instId}] 觸及進場但穿破止損，放棄")
                    time.sleep(0.3)
                    continue

                if is_hit:
                    t['status'] = "ACTIVE"
                    side_zh = "🟢 多單 (LONG)" if t['side']=="LONG" else "🔴 空單 (SHORT)"
                    send_tg(
                        f"🚀 *Alpha Oracle | 進場成交* 🚀\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🎯 方向：{side_zh}\n"
                        f"\n"
                        f"📍 成交價：{t['entry']:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}  (-1R)\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}\n"
                        f"\n"
                        f"🎯 *單已開，緊盯止損*"
                    )
                updated.append(t)

            elif t['status']=="ACTIVE":
                act_n = min(3, len(df))
                acl, ach = fetch_current_candle_hl(instId)
                act_low  = min(df['l'].iloc[-act_n:].min(), acl, curr_p)
                act_high = max(df['h'].iloc[-act_n:].max(), ach, curr_p)

                if t['tp1_hit']==0 and (
                    (t['side']=="LONG"  and act_high>=t['tp1']) or
                    (t['side']=="SHORT" and act_low <=t['tp1'])
                ):
                    t['tp1_hit'] = 1
                    send_tg(
                        f"🎯 *Alpha Oracle | 達到 TP1*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ 已觸及第一止盈位\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}  ✅\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}\n"
                        f"🚫 建議止損移至進場位保本：{t['entry']:.4f}"
                    )

                if t['locked']==0 and (
                    (t['side']=="LONG"  and act_high>=t['tp2']) or
                    (t['side']=="SHORT" and act_low <=t['tp2'])
                ):
                    t['locked'] = 1
                    t['sl']     = t['tp1']
                    send_tg(
                        f"🔒 *Alpha Oracle | 達到 TP2 · 鎖利*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ 已達 TP2，建議止損移至 TP1\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"🚫 建議新止損：{t['tp1']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}"
                    )

                is_sl  = ((t['side']=="LONG" and act_low<=t['sl']) or (t['side']=="SHORT" and act_high>=t['sl']))
                is_tp3 = ((t['side']=="LONG" and act_high>=t['tp3']) or (t['side']=="SHORT" and act_low<=t['tp3']))

                if is_sl or is_tp3:
                    is_be = is_sl and t['locked']==1
                    res   = "SL" if (is_sl and not is_be) else "TP"
                    if is_tp3:          rl, ep = "💰 止盈達標 (TP3)",        t['tp3']
                    elif is_be:         rl, ep = "🔒 保本出場 (Break Even)",  t['tp1']
                    else:               rl, ep = "❌ 止損離場",               t['sl']

                    send_tg(
                        f"🏁 *Alpha Oracle | 交易結算*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🏆 結果：{rl}\n"
                        f"\n"
                        f"📍 離場價：{ep:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}\n"
                        f"\n"
                        f"⏳ *冷卻中 4 小時，此幣暫停掃描*"
                    )
                    pd.DataFrame([{"instId":instId,"result":res,"date":today_str}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    # ⭐ 進入冷卻期而非直接移除，防止同幣短時間內重複進出
                    updated.append({
                        **t,
                        "status":     "COOLDOWN",
                        "wait_since": current_bar,
                    })
                    time.sleep(0.3)
                    continue

                updated.append(t)

            elif t['status'] == "COOLDOWN":
                # ⭐ 冷卻期：止損/止盈後 COOLDOWN_BARS 棒內不接受此幣新訊號
                bars_cooled = current_bar - t['wait_since']
                if bars_cooled >= COOLDOWN_BARS:
                    logging.info(f"[{instId}] 冷卻期結束（{bars_cooled} 棒），恢復掃描")
                    # 不加入 updated → 自動從 CSV 移除
                else:
                    remaining_min = (COOLDOWN_BARS - bars_cooled) * 15
                    logging.info(f"[{instId}] 冷卻中，剩餘約 {remaining_min} 分鐘")
                    updated.append(t)   # 保留在 CSV

            time.sleep(0.3)

        pd.DataFrame(updated).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
