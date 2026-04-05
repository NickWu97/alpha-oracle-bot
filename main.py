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

LOG_FILE            = "active_trades.csv"
STATS_FILE          = "daily_stats.csv"
WAITING_EXPIRY_BARS = 20

LOG_COLS   = ["instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3", "locked", "wait_since", "tp1_hit"]
STATS_COLS = ["instId", "result"]


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
    """抓取已收盤 K 棒，支援多週期（15m / 1H / 4H）"""
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
        f_res   = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except: pass
    try:
        ls_res   = requests.get(
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
    """RSI 指標：量化超買超賣"""
    delta  = df['c'].diff()
    gain   = delta.clip(lower=0).rolling(period).mean()
    loss   = (-delta.clip(upper=0)).rolling(period).mean()
    rs     = gain / (loss + 1e-10)
    rsi    = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> tuple[float, str]:
    recent = df.tail(lookback).copy()
    body   = (recent['h'] - recent['l']).replace(0, 1e-10)
    recent['delta'] = np.where(
        recent['c'] >= recent['o'],
        recent['v'] * (recent['c'] - recent['l']) / body,
        -recent['v'] * (recent['h'] - recent['c']) / body
    )
    cvd = recent['delta'].sum()
    label = "🟢 大戶吸籌 (CVD+)" if cvd > 0 else "🔴 大戶出貨 (CVD-)"
    return cvd, label

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> int:
    """Supertrend：1=多頭 / -1=空頭 / 0=資料不足"""
    if len(df) < period + 2:
        return 0
    high  = df['h'].values.astype(float)
    low   = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n     = len(df)

    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i]  - close[i - 1]))

    atr = np.zeros(n)
    atr[period] = tr[1:period + 1].mean()
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    hl2      = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr
    basic_dn = hl2 + multiplier * atr
    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)
    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]

    for i in range(period + 1, n):
        final_up[i] = (basic_up[i] if basic_up[i] > final_up[i-1] or close[i-1] < final_up[i-1]
                       else final_up[i-1])
        final_dn[i] = (basic_dn[i] if basic_dn[i] < final_dn[i-1] or close[i-1] > final_dn[i-1]
                       else final_dn[i-1])
        if   trend[i-1] == -1 and close[i] > final_dn[i-1]: trend[i] = 1
        elif trend[i-1] ==  1 and close[i] < final_up[i-1]: trend[i] = -1
        else:                                                 trend[i] = trend[i-1]

    return int(trend[-1])


# ─────────────────────────────────────────────
# 5. SMC 結構分析
# ─────────────────────────────────────────────

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple[list, list]:
    data = df.tail(lookback).reset_index(drop=True)
    swing_highs, swing_lows = [], []
    for i in range(n, len(data) - n):
        if data['h'].iloc[i] == data['h'].iloc[i-n:i+n+1].max():
            swing_highs.append(data['h'].iloc[i])
        if data['l'].iloc[i] == data['l'].iloc[i-n:i+n+1].min():
            swing_lows.append(data['l'].iloc[i])
    return sorted(set(swing_highs)), sorted(set(swing_lows))

def detect_market_structure(df: pd.DataFrame) -> str:
    swing_highs, swing_lows = find_swing_points(df, n=3, lookback=60)
    if len(swing_lows) >= 2:
        l1, l2 = swing_lows[-2], swing_lows[-1]
        if l1 > 0 and abs(l1 - l2) / l1 < 0.015:
            return "W底反轉 📐"
    if len(swing_highs) >= 2:
        h1, h2 = swing_highs[-2], swing_highs[-1]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015:
            return "M頭反轉 📐"
    recent = df.tail(20)
    slope  = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if   slope >  0.025: return "上升趨勢延續 📈"
    elif slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

def find_order_block(df: pd.DataFrame, side: str, lookback: int = 15) -> dict | None:
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, k_next = data.iloc[i], data.iloc[i + 1]
        if side == "LONG" and k['c'] < k['o'] and k_next['c'] > k_next['o']:
            return {"high": k['o'], "low": k['l']}
        if side == "SHORT" and k['c'] > k['o'] and k_next['c'] < k_next['o']:
            return {"high": k['h'], "low": k['c']}
    return None

def find_recent_fvg(df: pd.DataFrame, side: str) -> dict | None:
    for i in range(len(df) - 3, max(len(df) - 20, 0), -1):
        k0, k2 = df.iloc[i - 1], df.iloc[i + 1]
        if side == "LONG"  and k2['l'] > k0['h']:
            return {"high": k2['l'], "low": k0['h']}
        if side == "SHORT" and k2['h'] < k0['l']:
            return {"high": k0['l'], "low": k2['h']}
    return None

def calculate_structural_sl(df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
    buffer = atr * 0.25
    ob     = find_order_block(df, side)
    fvg    = find_recent_fvg(df, side)
    if side == "LONG":
        candidates = []
        if ob  and ob['low']  < entry: candidates.append(ob['low']  - buffer)
        if fvg and fvg['low'] < entry: candidates.append(fvg['low'] - buffer)
        if candidates:
            sl = max(candidates)
            if (entry - sl) / (entry + 1e-10) < 0.005:  # 止損太近，加寬到 0.5%
                sl = entry - atr * 1.5
            return sl
        return entry - atr * 1.5
    else:
        candidates = []
        if ob  and ob['high']  > entry: candidates.append(ob['high']  + buffer)
        if fvg and fvg['high'] > entry: candidates.append(fvg['high'] + buffer)
        if candidates:
            sl = min(candidates)
            if (sl - entry) / (entry + 1e-10) < 0.005:
                sl = entry + atr * 1.5
            return sl
        return entry + atr * 1.5

def get_fixed_r_tps(entry: float, sl: float, side: str) -> tuple[float, float, float]:
    """固定 R 倍數止盈：TP1=1R, TP2=2R, TP3=3R"""
    risk = abs(entry - sl) + 1e-10
    if side == "LONG":
        return entry + risk, entry + risk * 2, entry + risk * 3
    else:
        return entry - risk, entry - risk * 2, entry - risk * 3

def suggest_leverage(atr: float, price: float) -> tuple[str, str]:
    vol_pct = (atr / (price + 1e-10)) * 100
    if   vol_pct > 3:   return "3x ~ 5x",   "⚠️ 高波動"
    elif vol_pct > 1.5: return "5x ~ 10x",  "中波動"
    else:               return "10x ~ 20x", "低波動"


# ─────────────────────────────────────────────
# 6. 過濾器（五層 + 新增三層 = 共八層）
# ─────────────────────────────────────────────

def fetch_funding_rate_raw(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res['data'][0]['fundingRate'])
    except:
        return 0.0

def is_trending_market(df: pd.DataFrame) -> bool:
    """過濾器①：盤整市場過濾"""
    if len(df) < 50:
        return True
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr          = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    current_atr = tr.rolling(14).mean().iloc[-1]
    avg_atr_50  = tr.tail(50).mean()
    return current_atr > avg_atr_50 * 0.7

def get_btc_direction(btc_df: pd.DataFrame, lookback: int = 5) -> str:
    if btc_df is None or len(btc_df) < lookback:
        return "NEUTRAL"
    recent  = btc_df.tail(lookback)
    bearish = int((recent['c'] < recent['o']).sum())
    bullish = lookback - bearish
    if bearish >= 4: return "DOWN"
    if bullish >= 4: return "UP"
    return "NEUTRAL"

def classify_trade(side: str, structure: str, risk_pct: float) -> str:
    if "反轉" in structure:
        return "📊 長單 (波段)"
    elif risk_pct < 1.0:
        return "⚡ 短單 (日內)"
    return "📊 長單 (波段)"

# ── 新增過濾器 ──────────────────────────────────────────────────────────────

def check_ema_trend(df: pd.DataFrame, side: str) -> bool:
    """
    過濾器⑥：EMA50 趨勢方向
    做多：收盤價必須在 EMA50 之上
    做空：收盤價必須在 EMA50 之下
    避免逆勢交易，這是勝率最大的單一改善因素之一
    """
    if len(df) < 55:
        return True
    ema50 = calculate_ema(df, 50).iloc[-1]
    curr  = df['c'].iloc[-1]
    if side == "LONG"  and curr < ema50:
        return False
    if side == "SHORT" and curr > ema50:
        return False
    return True

def check_rsi_filter(df: pd.DataFrame, side: str) -> bool:
    """
    過濾器⑦：RSI 過濾（避免追高殺低）
    做多：RSI 必須在 35~68 之間（不過熱、不過冷）
    做空：RSI 必須在 32~65 之間
    RSI > 70 追多 = 追高；RSI < 30 追空 = 殺低，勝率極低
    """
    if len(df) < 20:
        return True
    rsi = calculate_rsi(df)
    if side == "LONG"  and not (35 <= rsi <= 68):
        return False
    if side == "SHORT" and not (32 <= rsi <= 65):
        return False
    return True

def check_1h_trend(instId: str, side: str) -> tuple[bool, str]:
    """
    過濾器⑧：1小時 Supertrend 方向確認（最關鍵的勝率提升器）
    15m 多頭訊號必須與 1h Supertrend 多頭一致才放行
    15m 空頭訊號必須與 1h Supertrend 空頭一致才放行
    跨週期確認 = 減少逆大趨勢的假訊號
    """
    df_1h = fetch_okx(instId, bar="1H", limit=60)
    if df_1h is None or len(df_1h) < 15:
        return True, "⚪ N/A"   # 抓不到資料時放行，不過濾

    st_1h = calculate_supertrend(df_1h)
    label = "📈 1H多頭" if st_1h == 1 else ("📉 1H空頭" if st_1h == -1 else "⚪ 1H未知")

    if st_1h == 0:
        return True, label   # 資料不足，放行
    if side == "LONG"  and st_1h != 1:
        return False, label
    if side == "SHORT" and st_1h != -1:
        return False, label
    return True, label


# ─────────────────────────────────────────────
# 7. SMC 訊號掃描（加入 BOS 強度確認）
# ─────────────────────────────────────────────

def find_smc_setup(df: pd.DataFrame) -> dict | None:
    if df is None or len(df) < 55:   # 保留足夠的 EMA50 計算空間
        return None

    atr  = calculate_atr(df)
    best = None

    for i in range(len(df) - 3, len(df) - 25, -1):
        k0, k1, k2 = df.iloc[i - 1], df.iloc[i], df.iloc[i + 1]

        # BOS 強度過濾：突破 K 棒實體必須 > 50% 的總振幅（強棒才算）
        k2_range = k2['h'] - k2['l'] + 1e-10
        k2_body  = abs(k2['c'] - k2['o'])
        if k2_body / k2_range < 0.50:
            continue   # 弱棒（十字星、上下影線長），跳過

        # BOS 突破量能確認：突破 K 棒成交量 > 近 20 根均量的 1.2 倍
        avg_vol = df['v'].iloc[i - 20:i].mean()
        if k2['v'] < avg_vol * 1.2:
            continue   # 量能不足，假突破風險高，跳過

        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i - 15:i].max():
            entry = k2['l'] if k2['l'] > k0['h'] else k1['c']
            best  = {"side": "LONG", "entry": entry}

        elif k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i - 15:i].min():
            entry = k2['h'] if k2['h'] < k0['l'] else k1['c']
            best  = {"side": "SHORT", "entry": entry}

    if best is None:
        return None

    side  = best['side']
    entry = best['entry']
    price = df['c'].iloc[-1]

    sl = calculate_structural_sl(df, side, entry, atr)
    tp1, tp2, tp3 = get_fixed_r_tps(entry, sl, side)

    risk          = abs(entry - sl) + 1e-10
    risk_pct      = risk / (entry + 1e-10) * 100
    structure     = detect_market_structure(df)
    lev, lev_note = suggest_leverage(atr, price)
    trade_type    = classify_trade(side, structure, risk_pct)
    _, cvd_label  = calculate_cvd(df)

    st_val   = calculate_supertrend(df)
    st_label = "📈 多頭" if st_val == 1 else ("📉 空頭" if st_val == -1 else "⚪ 未知")

    # EMA50 相對位置（顯示用）
    ema50    = calculate_ema(df, 50).iloc[-1]
    ema_gap  = (price - ema50) / ema50 * 100
    ema_label = f"{'↑' if price > ema50 else '↓'} EMA50 ({ema_gap:+.2f}%)"

    # RSI 值（顯示用）
    rsi_val = calculate_rsi(df)

    return {
        "side":          side,
        "entry":         entry,
        "sl":            sl,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           tp3,
        "structure":     structure,
        "leverage":      lev,
        "leverage_note": lev_note,
        "trade_type":    trade_type,
        "cvd_label":     cvd_label,
        "st_val":        st_val,
        "st_label":      st_label,
        "ema_label":     ema_label,
        "rsi_val":       rsi_val,
    }


# ─────────────────────────────────────────────
# 8. 主程式
# ─────────────────────────────────────────────

def main():
    try:
        now_tw        = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"

        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 每日戰績回報 ───────────────────────────────────────────────
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result'] == 'TP'])
                    sl_c  = len(df_s[df_s['result'] == 'SL'])
                    total = tp_c + sl_c
                    wr    = (tp_c / total * 100) if total > 0 else 0
                    date_str = (now_tw - timedelta(days=1)).strftime('%Y-%m-%d')
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
                        f"──────────────────\n"
                        f"📌 保本亦計為獲勝"
                    )
                    if is_midnight:
                        pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as fh: fh.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # ── B. 核心監控 ───────────────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            if "wait_since" not in trades_df.columns: trades_df["wait_since"] = 0
            if "tp1_hit"    not in trades_df.columns: trades_df["tp1_hit"]    = 0
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
                time.sleep(0.3)
                continue

            curr_p   = df['c'].iloc[-1]
            coin_sym = instId.split('-')[0]

            # ── 1. 掃描新訊號 ────────────────────────────────────────────
            if instId not in active_ids:

                # 過濾器①：盤整市場
                if not is_trending_market(df):
                    logging.info(f"[{instId}] 盤整，跳過")
                    time.sleep(0.3)
                    continue

                setup = find_smc_setup(df)
                if not setup:
                    time.sleep(0.3)
                    continue

                # 過濾器②：CVD 方向
                cvd_val, _ = calculate_cvd(df)
                if setup['side'] == "LONG" and cvd_val < 0:
                    logging.info(f"[{instId}] CVD-，多頭跳過")
                    time.sleep(0.3)
                    continue
                if setup['side'] == "SHORT" and cvd_val > 0:
                    logging.info(f"[{instId}] CVD+，空頭跳過")
                    time.sleep(0.3)
                    continue

                # 過濾器③：資金費率極端值
                fr = fetch_funding_rate_raw(instId)
                if setup['side'] == "LONG"  and fr >  0.0005:
                    logging.info(f"[{instId}] 資費過高，跳過")
                    time.sleep(0.3)
                    continue
                if setup['side'] == "SHORT" and fr < -0.0005:
                    logging.info(f"[{instId}] 資費過低，跳過")
                    time.sleep(0.3)
                    continue

                # 過濾器④：BTC 方向（山寨幣）
                if instId != "BTC-USDT-SWAP":
                    if setup['side'] == "LONG"  and btc_trend == "DOWN":
                        logging.info(f"[{instId}] BTC 下跌，多頭跳過")
                        time.sleep(0.3)
                        continue
                    if setup['side'] == "SHORT" and btc_trend == "UP":
                        logging.info(f"[{instId}] BTC 上漲，空頭跳過")
                        time.sleep(0.3)
                        continue

                # 過濾器⑤：15m Supertrend
                if setup['st_val'] == -1 and setup['side'] == "LONG":
                    logging.info(f"[{instId}] 15m Supertrend 空頭，多頭跳過")
                    time.sleep(0.3)
                    continue
                if setup['st_val'] ==  1 and setup['side'] == "SHORT":
                    logging.info(f"[{instId}] 15m Supertrend 多頭，空頭跳過")
                    time.sleep(0.3)
                    continue

                # 過濾器⑥：EMA50 趨勢方向
                if not check_ema_trend(df, setup['side']):
                    logging.info(f"[{instId}] EMA50 方向不符，跳過")
                    time.sleep(0.3)
                    continue

                # 過濾器⑦：RSI 過濾
                if not check_rsi_filter(df, setup['side']):
                    logging.info(f"[{instId}] RSI={setup['rsi_val']:.1f} 過濾，跳過")
                    time.sleep(0.3)
                    continue

                # 過濾器⑧：1小時 Supertrend 跨週期確認（最關鍵）
                ok_1h, label_1h = check_1h_trend(instId, setup['side'])
                if not ok_1h:
                    logging.info(f"[{instId}] 1H Supertrend 方向不符，跳過")
                    time.sleep(0.3)
                    continue

                # ── 全部過濾器通過，發出訊號 ────────────────────────────
                funding, ls_ratio = get_funding_ls(instId)
                side_zh = "🟢 多單 (LONG)" if setup['side'] == "LONG" else "🔴 空單 (SHORT)"

                msg  = f"🔥 *Alpha Oracle 訊號發射* 🔥\n"
                msg += f"──────────────────\n"
                msg += f"💎 幣種：#{coin_sym}\n"
                msg += f"🎯 方向：{side_zh}\n"
                msg += f"⏰ 週期：15m\n"
                msg += f"📊 數據：多空比 {ls_ratio} | 資費 {funding} | {setup['cvd_label']}\n"
                msg += f"\n"
                msg += f"📍 進場位：{setup['entry']:.4f}\n"
                msg += f"🚫 止損位：{setup['sl']:.4f}  (-1R)\n"
                msg += f"💰 TP1 (1.0R)：{setup['tp1']:.4f}\n"
                msg += f"💰 TP2 (2.0R)：{setup['tp2']:.4f}\n"
                msg += f"💰 TP3 (3.0R)：{setup['tp3']:.4f}\n"
                msg += f"\n"
                msg += f"🏗️ 結構：{setup['structure']}\n"
                msg += f"📡 Supertrend：{setup['st_label']} | {label_1h}\n"
                msg += f"📈 EMA50：{setup['ema_label']}\n"
                msg += f"🔢 RSI：{setup['rsi_val']:.1f}\n"
                msg += f"🕹️ 槓桿：{setup['leverage']} ({setup['leverage_note']})\n"
                msg += f"📌 類型：{setup['trade_type']}\n"
                msg += f"\n"
                msg += f"💡 *等待回踩成交...*"
                send_tg(msg)

                updated_trades.append({
                    "instId":     instId,
                    "side":       setup['side'],
                    "status":     "WAITING",
                    "entry":      setup['entry'],
                    "sl":         setup['sl'],
                    "tp1":        setup['tp1'],
                    "tp2":        setup['tp2'],
                    "tp3":        setup['tp3'],
                    "locked":     0,
                    "wait_since": current_bar,
                    "tp1_hit":    0,
                })
                time.sleep(0.3)
                continue

            # ── 2. 追蹤現有單據 ───────────────────────────────────────────
            t = normalize_trade(trades_df[trades_df['instId'] == instId].iloc[0].to_dict())

            # WAITING 狀態
            if t['status'] == "WAITING":
                bars_waited = current_bar - t['wait_since']
                if bars_waited > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 逾期，清除")
                    time.sleep(0.3)
                    continue

                # 進場偵測：8 根確認 K 棒 + 當前未收盤 K 棒 + 收盤價三層合一
                n_check           = min(8, len(df))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low  = min(df['l'].iloc[-n_check:].min(), cur_low,  curr_p)
                check_high = max(df['h'].iloc[-n_check:].max(), cur_high, curr_p)
                is_hit = (
                    (t['side'] == "LONG"  and check_low  <= t['entry']) or
                    (t['side'] == "SHORT" and check_high >= t['entry'])
                )

                # 進場保護：當前收盤已在止損另一側，放棄此單
                already_sl = (
                    (t['side'] == "LONG"  and curr_p < t['sl']) or
                    (t['side'] == "SHORT" and curr_p > t['sl'])
                )
                if is_hit and already_sl:
                    logging.info(f"[{instId}] 進場觸及但當前已穿止損，放棄")
                    time.sleep(0.3)
                    continue

                if is_hit:
                    t['status'] = "ACTIVE"
                    side_zh     = "🟢 多單 (LONG)" if t['side'] == "LONG" else "🔴 空單 (SHORT)"
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
                updated_trades.append(t)

            # ACTIVE 狀態
            elif t['status'] == "ACTIVE":

                act_n              = min(3, len(df))
                act_cur_lo, act_cur_hi = fetch_current_candle_hl(instId)
                act_low  = min(df['l'].iloc[-act_n:].min(), act_cur_lo, curr_p)
                act_high = max(df['h'].iloc[-act_n:].max(), act_cur_hi, curr_p)

                # TP1 達到
                if t['tp1_hit'] == 0 and (
                    (t['side'] == "LONG"  and act_high >= t['tp1']) or
                    (t['side'] == "SHORT" and act_low  <= t['tp1'])
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

                # TP2 達到
                if t['locked'] == 0 and (
                    (t['side'] == "LONG"  and act_high >= t['tp2']) or
                    (t['side'] == "SHORT" and act_low  <= t['tp2'])
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

                is_sl  = (
                    (t['side'] == "LONG"  and act_low  <= t['sl']) or
                    (t['side'] == "SHORT" and act_high >= t['sl'])
                )
                is_tp3 = (
                    (t['side'] == "LONG"  and act_high >= t['tp3']) or
                    (t['side'] == "SHORT" and act_low  <= t['tp3'])
                )

                if is_sl or is_tp3:
                    is_breakeven = is_sl and t['locked'] == 1
                    res          = "SL" if (is_sl and not is_breakeven) else "TP"
                    if is_tp3:
                        result_label = "💰 止盈達標 (TP3)"
                        exit_p       = t['tp3']
                    elif is_breakeven:
                        result_label = "🔒 保本出場 (Break Even)"
                        exit_p       = t['tp1']
                    else:
                        result_label = "❌ 止損離場"
                        exit_p       = t['sl']

                    send_tg(
                        f"🏁 *Alpha Oracle | 交易結算*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🏆 結果：{result_label}\n"
                        f"\n"
                        f"📍 離場價：{exit_p:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}\n"
                        f"💰 TP1 (1.0R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (2.0R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (3.0R)：{t['tp3']:.4f}"
                    )
                    pd.DataFrame([{"instId": instId, "result": res}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    time.sleep(0.3)
                    continue

                updated_trades.append(t)

            time.sleep(0.3)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
