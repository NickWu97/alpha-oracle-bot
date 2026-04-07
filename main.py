import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime, timedelta

# --- 1. 基礎配置 ---
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
WAITING_EXPIRY_BARS = 20  # WAITING 超過幾根 K 棒自動清除（15m × 20 = 5 小時）

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
    """確保從 CSV 讀回來的欄位型態正確"""
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

def fetch_okx(instId: str) -> pd.DataFrame | None:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=100"
        res = requests.get(url, timeout=10).json()
        df  = pd.DataFrame(
            res['data'],
            columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
        )
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] K 線抓取失敗: {e}")
        return None

def fetch_current_candle_hl(instId: str) -> tuple[float, float]:
    """抓取當前「未收盤」K 棒的最高/最低價"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
        res = requests.get(url, timeout=5).json()
        for row in res['data']:
            if row[8] == "0":
                return float(row[3]), float(row[2])  # (low, high)
    except Exception as e:
        logging.warning(f"[{instId}] 當前 K 棒抓取失敗: {e}")
    return float('inf'), float('-inf')

def get_funding_ls(instId: str) -> tuple[str, str]:
    """抓取資金費率與多空持倉比"""
    base_id  = instId.replace("-SWAP", "").split("-")[0]
    funding  = "N/A"
    ls_ratio = "N/A"
    try:
        f_res   = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率抓取失敗: {e}")
    try:
        ls_res   = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5
        ).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except Exception as e:
        logging.warning(f"[{instId}] 多空比抓取失敗: {e}")
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

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple[float, str]:
    """
    抓取盤口深度並計算不平衡度 (Imbalance)。
    回傳 (imbalance_ratio, label)
    Ratio > 1.2 代表買盤強 (Bid > Ask)
    Ratio < 0.8 代表賣盤強 (Ask > Bid)
    """
    try:
        # sz=20 表示抓取前 20 檔深度
        url = f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}"
        res = requests.get(url, timeout=5).json()
        if res['code'] != '0' or not res['data']:
            return 1.0, "⚪ 盤口均衡"
        
        data = res['data'][0]
        bids = data['bids'] # [price, vol, ...]
        asks = data['asks']
        
        # 計算總掛單量 (價格 * 數量 的加權或單純數量加權，這裡用單純數量加權較快)
        bid_vol = sum(float(b[1]) for b in bids)
        ask_vol = sum(float(a[1]) for a in asks)
        
        if ask_vol == 0: return 1.0, "⚪ 盤口均衡"
        
        ratio = bid_vol / ask_vol
        
        if ratio > 1.2:
            label = f"🟢 買盤強勢 ({ratio:.2f})"
        elif ratio < 0.8:
            label = f"🔴 賣盤強勢 ({ratio:.2f})"
        else:
            label = f"⚪ 盤口均衡 ({ratio:.2f})"
            
        return ratio, label
        
    except Exception as e:
        logging.warning(f"[{instId}] 盤口數據抓取失敗: {e}")
        return 1.0, "⚪ 數據缺失"


# ─────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> tuple[float, str]:
    """真實 CVD 估算"""
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
    """Supertrend 指標"""
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

    hl2 = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr
    basic_dn = hl2 + multiplier * atr

    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend    = np.ones(n, dtype=int)

    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]

    for i in range(period + 1, n):
        final_up[i] = (
            basic_up[i]
            if basic_up[i] > final_up[i - 1] or close[i - 1] < final_up[i - 1]
            else final_up[i - 1]
        )
        final_dn[i] = (
            basic_dn[i]
            if basic_dn[i] < final_dn[i - 1] or close[i - 1] > final_dn[i - 1]
            else final_dn[i - 1]
        )
        if trend[i - 1] == -1 and close[i] > final_dn[i - 1]:
            trend[i] = 1
        elif trend[i - 1] == 1 and close[i] < final_up[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]

    return int(trend[-1])


# ─────────────────────────────────────────────
# 5. SMC & ICT 結構分析
# ─────────────────────────────────────────────

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple[list, list]:
    """找出擺動高低點"""
    data = df.tail(lookback).reset_index(drop=True)
    swing_highs, swing_lows = [], []
    for i in range(n, len(data) - n):
        window_h = data['h'].iloc[i - n: i + n + 1]
        window_l = data['l'].iloc[i - n: i + n + 1]
        if data['h'].iloc[i] == window_h.max():
            swing_highs.append(data['h'].iloc[i])
        if data['l'].iloc[i] == window_l.min():
            swing_lows.append(data['l'].iloc[i])
    return sorted(set(swing_highs)), sorted(set(swing_lows))

def detect_market_structure(df: pd.DataFrame) -> str:
    """偵測市場結構"""
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
    """找出最近的訂單塊 (Order Block)"""
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, k_next = data.iloc[i], data.iloc[i + 1]
        if side == "LONG" and k['c'] < k['o'] and k_next['c'] > k_next['o']:
            return {"high": k['o'], "low": k['l']}
        if side == "SHORT" and k['c'] > k['o'] and k_next['c'] < k_next['o']:
            return {"high": k['h'], "low": k['c']}
    return None

def find_recent_fvg(df: pd.DataFrame, side: str) -> dict | None:
    """找出最近的 FVG (公平價值缺口)"""
    for i in range(len(df) - 3, max(len(df) - 20, 0), -1):
        k0, k2 = df.iloc[i - 1], df.iloc[i + 1]
        if side == "LONG"  and k2['l'] > k0['h']:
            return {"high": k2['l'], "low": k0['h']}
        if side == "SHORT" and k2['h'] < k0['l']:
            return {"high": k0['l'], "low": k2['h']}
    return None

def find_ict_snr_zones(df: pd.DataFrame, side: str, lookback: int = 30) -> dict | None:
    """
    ICT SNR (Support/Resistance) 分析：
    尋找近期未被測試的關鍵支撐/阻力位。
    LONG: 尋找近期的 Swing Low 或 Demand Zone (未被跌破)
    SHORT: 尋找近期的 Swing High 或 Supply Zone (未被突破)
    """
    data = df.tail(lookback).reset_index(drop=True)
    
    if side == "LONG":
        # 尋找最近的一個顯著低點 (Swing Low)
        # 簡單定義：過去 30 根內的最低點，且該低點發生在至少 3 根 K 棒前
        min_idx = data['l'].iloc[:-3].argmin()
        min_val = data['l'].iloc[min_idx]
        # 確認之後沒有被有效跌破 (收盤價沒低於它太多)
        subsequent_closes = data['c'].iloc[min_idx+1:]
        if all(c > min_val * 0.995 for c in subsequent_closes):
            return {"level": min_val, "type": "Demand/SNR"}
            
    else: # SHORT
        # 尋找最近的一個顯著高點
        max_idx = data['h'].iloc[:-3].argmax()
        max_val = data['h'].iloc[max_idx]
        # 確認之後沒有被有效突破
        subsequent_closes = data['c'].iloc[max_idx+1:]
        if all(c < max_val * 1.005 for c in subsequent_closes):
            return {"level": max_val, "type": "Supply/SNR"}
            
    return None

def calculate_structural_sl(df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
    """結構性止損"""
    buffer = atr * 0.25
    ob     = find_order_block(df, side)
    fvg    = find_recent_fvg(df, side)
    snr    = find_ict_snr_zones(df, side) # 新增 SNR 引用

    if side == "LONG":
        candidates = []
        if ob  and ob['low']  < entry: candidates.append(ob['low']  - buffer)
        if fvg and fvg['low'] < entry: candidates.append(fvg['low'] - buffer)
        if snr and snr['level'] < entry: candidates.append(snr['level'] - buffer) # SNR 作為止損參考
        
        if candidates:
            sl = max(candidates)
            if (entry - sl) / (entry + 1e-10) < 0.004:
                sl = entry - atr * 1.5
            return sl
        return entry - atr * 1.5

    else:
        candidates = []
        if ob  and ob['high']  > entry: candidates.append(ob['high']  + buffer)
        if fvg and fvg['high'] > entry: candidates.append(fvg['high'] + buffer)
        if snr and snr['level'] > entry: candidates.append(snr['level'] + buffer) # SNR 作為止損參考
        
        if candidates:
            sl = min(candidates)
            if (sl - entry) / (entry + 1e-10) < 0.004:
                sl = entry + atr * 1.5
            return sl
        return entry + atr * 1.5

def get_fixed_r_tps(entry: float, sl: float, side: str) -> tuple[float, float, float]:
    """固定 R 倍數止盈"""
    risk = abs(entry - sl) + 1e-10
    if side == "LONG":
        return entry + risk, entry + risk * 2, entry + risk * 3
    else:
        return entry - risk, entry - risk * 2, entry - risk * 3

def suggest_leverage(atr: float, price: float) -> tuple[str, str]:
    """根據 ATR 波動率自動建議槓桿倍數"""
    vol_pct = (atr / (price + 1e-10)) * 100
    if   vol_pct > 3:   return "3x ~ 5x",   "⚠️ 高波動"
    elif vol_pct > 1.5: return "5x ~ 10x",  "中波動"
    else:               return "10x ~ 20x", "低波動"

# ─────────────────────────────────────────────
# 6. 三層過濾器 + 盤口/SNR
# ─────────────────────────────────────────────

def fetch_funding_rate_raw(instId: str) -> float:
    """抓取資金費率原始浮點值"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res['data'][0]['fundingRate'])
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率原始值抓取失敗: {e}")
        return 0.0

def is_trending_market(df: pd.DataFrame) -> bool:
    """盤整過濾"""
    if len(df) < 50:
        return True
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    current_atr = tr.rolling(14).mean().iloc[-1]
    avg_atr_50  = tr.tail(50).mean()
    return current_atr > avg_atr_50 * 0.7

def get_btc_direction(btc_df: pd.DataFrame, lookback: int = 5) -> str:
    """BTC 近期方向判斷"""
    if btc_df is None or len(btc_df) < lookback:
        return "NEUTRAL"
    recent  = btc_df.tail(lookback)
    bearish = int((recent['c'] < recent['o']).sum())
    bullish = lookback - bearish
    if bearish >= 4: return "DOWN"
    if bullish >= 4: return "UP"
    return "NEUTRAL"

def classify_trade(side: str, structure: str, risk_pct: float) -> str:
    """自動判斷短單/長單"""
    if "反轉" in structure:
        return "📊 長單 (波段)"
    elif risk_pct < 1.0:
        return "⚡ 短單 (日內)"
    return "📊 長單 (波段)"


# ─────────────────────────────────────────────
# 7. SMC 訊號掃描（整合所有分析）
# ─────────────────────────────────────────────

def find_smc_setup(df: pd.DataFrame, instId: str) -> dict | None:
    """
    完整 SMC + ICT SNR + 盤口 掃描流程
    """
    if df is None or len(df) < 40:
        return None

    atr  = calculate_atr(df)
    best = None

    # 掃描最近 25 根 K 棒，取最新符合的 BOS 訊號
    for i in range(len(df) - 3, len(df) - 25, -1):
        k0, k1, k2 = df.iloc[i - 1], df.iloc[i], df.iloc[i + 1]

        # 多頭 BOS
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i - 15:i].max():
            entry = k2['l'] if k2['l'] > k0['h'] else k1['c']
            best  = {"side": "LONG", "entry": entry}

        # 空頭 BOS
        elif k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i - 15:i].min():
            entry = k2['h'] if k2['h'] < k0['l'] else k1['c']
            best  = {"side": "SHORT", "entry": entry}

    if best is None:
        return None

    side  = best['side']
    entry = best['entry']
    price = df['c'].iloc[-1]

    # 結構性止損 (已包含 SNR 邏輯)
    sl = calculate_structural_sl(df, side, entry, atr)

    # 固定 R 倍數止盈
    tp1, tp2, tp3 = get_fixed_r_tps(entry, sl, side)

    # 各項分析
    risk          = abs(entry - sl) + 1e-10
    risk_pct      = risk / (entry + 1e-10) * 100
    structure     = detect_market_structure(df)
    lev, lev_note = suggest_leverage(atr, price)
    trade_type    = classify_trade(side, structure, risk_pct)
    _, cvd_label  = calculate_cvd(df)

    # Supertrend 方向
    st_val   = calculate_supertrend(df)
    st_label = "📈 多頭" if st_val == 1 else ("📉 空頭" if st_val == -1 else "⚪ 未知")
    
    # ICT SNR 狀態
    snr_zone = find_ict_snr_zones(df, side)
    snr_status = "✅ 有 SNR 支撐/阻力" if snr_zone else "⚠️ 無明顯 SNR"

    return {
        "side":          side,
        "entry":         entry,
        "sl":            sl,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           tp3,
        "r1":            1.0,
        "r2":            2.0,
        "r3":            3.0,
        "structure":     structure,
        "leverage":      lev,
        "leverage_note": lev_note,
        "trade_type":    trade_type,
        "cvd_label":     cvd_label,
        "st_val":        st_val,
        "st_label":      st_label,
        "snr_status":    snr_status,
        "snr_zone":      snr_zone,
    }


# ─────────────────────────────────────────────
# 8. 主程式
# ─────────────────────────────────────────────

def main():
    try:
        now_tw        = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"

        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 戰績回報 ────────────────────────────────────────────────
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
                        f"✅ 盈利：{tp_c} 單\n"
                        f"❌ 止損：{sl_c} 單\n"
                        f"📊 總計：{total} 單\n"
                        f"\n"
                        f"🔥 勝率：*{wr:.1f}%*"
                    )
                    if is_midnight:
                        pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                        with open("midnight.ok", "w") as fh: fh.write("ok")
        elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
            os.remove("midnight.ok")

        # ── B. 核心監控邏輯 ─────────────────────────────────────────────
        try:
            trades_df = pd.read_csv(LOG_FILE)
            if "wait_since" not in trades_df.columns:
                trades_df["wait_since"] = 0
            if "tp1_hit" not in trades_df.columns:
                trades_df["tp1_hit"] = 0
        except Exception:
            trades_df = pd.DataFrame(columns=LOG_COLS)

        active_ids     = trades_df['instId'].tolist()
        updated_trades = []
        current_bar    = int(datetime.utcnow().timestamp() // 900)

        # 過濾器 ③ 前置：先抓 BTC 方向
        btc_df    = fetch_okx("BTC-USDT-SWAP")
        btc_trend = get_btc_direction(btc_df)
        logging.info(f"BTC 當前方向：{btc_trend}")

        for instId in ALL_COINS:
            df = fetch_okx(instId)
            if df is None or df.empty:
                time.sleep(0.2)
                continue

            curr_p   = df['c'].iloc[-1]
            coin_sym = instId.split('-')[0]

            # ── 1. 發現新機會 ───────────────────────────────────────────
            if instId not in active_ids:

                # 過濾器 ①：盤整市場
                if not is_trending_market(df):
                    logging.info(f"[{instId}] 盤整市場，跳過")
                    time.sleep(0.2)
                    continue

                setup = find_smc_setup(df, instId)
                if setup:

                    # 過濾器 ②：CVD 方向
                    cvd_val, _ = calculate_cvd(df)
                    if setup['side'] == "LONG" and cvd_val < 0:
                        logging.info(f"[{instId}] CVD 負值，多頭訊號跳過")
                        time.sleep(0.2)
                        continue
                    if setup['side'] == "SHORT" and cvd_val > 0:
                        logging.info(f"[{instId}] CVD 正值，空頭訊號跳過")
                        time.sleep(0.2)
                        continue

                    # 過濾器 ③：資金費率
                    fr = fetch_funding_rate_raw(instId)
                    if setup['side'] == "LONG" and fr > 0.0005:
                        logging.info(f"[{instId}] 資費過高，多頭過熱，跳過")
                        time.sleep(0.2)
                        continue
                    if setup['side'] == "SHORT" and fr < -0.0005:
                        logging.info(f"[{instId}] 資費過低，空頭過熱，跳過")
                        time.sleep(0.2)
                        continue

                    # 過濾器 ④：BTC 方向
                    if instId != "BTC-USDT-SWAP":
                        if setup['side'] == "LONG" and btc_trend == "DOWN":
                            logging.info(f"[{instId}] BTC 下跌中，山寨多頭跳過")
                            time.sleep(0.2)
                            continue
                        if setup['side'] == "SHORT" and btc_trend == "UP":
                            logging.info(f"[{instId}] BTC 上漲中，山寨空頭跳過")
                            time.sleep(0.2)
                            continue

                    # 過濾器 ⑤：Supertrend 方向
                    if setup['st_val'] == -1 and setup['side'] == "LONG":
                        logging.info(f"[{instId}] Supertrend 空頭，多頭訊號跳過")
                        time.sleep(0.2)
                        continue
                    if setup['st_val'] == 1 and setup['side'] == "SHORT":
                        logging.info(f"[{instId}] Supertrend 多頭，空頭訊號跳過")
                        time.sleep(0.2)
                        continue
                    
                    # --- 新增過濾器 ⑥：ICT SNR 確認 ---
                    # 如果找不到對應方向的 SNR 支撐/阻力，風險較高，可選擇跳過或僅記錄警告
                    # 這裡設定為：如果沒有 SNR 支援，則不發射訊號 (嚴格模式)
                    if setup['snr_zone'] is None:
                         logging.info(f"[{instId}] 未找到明確 ICT SNR 區域，跳過以降低雜訊")
                         time.sleep(0.2)
                         continue

                    # --- 新增過濾器 ⑦：盤口不平衡 (Order Book) ---
                    # 即時抓取盤口，確保進場瞬間有流動性支持
                    ob_ratio, ob_label = fetch_order_book_imbalance(instId)
                    
                    # 多頭需要買盤強勢 (Ratio > 0.9 即可，不需極端，避免錯失)
                    if setup['side'] == "LONG" and ob_ratio < 0.9:
                        logging.info(f"[{instId}] 盤口買氣不足 ({ob_label})，多頭跳過")
                        time.sleep(0.2)
                        continue
                    
                    # 空頭需要賣盤強勢 (Ratio < 1.1)
                    if setup['side'] == "SHORT" and ob_ratio > 1.1:
                        logging.info(f"[{instId}] 盤口賣壓不足 ({ob_label})，空頭跳過")
                        time.sleep(0.2)
                        continue

                    funding, ls_ratio = get_funding_ls(instId)
                    side_zh = "🟢 多單 (LONG)" if setup['side'] == "LONG" else "🔴 空單 (SHORT)"

                    msg  = f"🔥 *Alpha Oracle 訊號發射* 🔥\n"
                    msg += f"──────────────────\n"
                    msg += f"💎 幣種：#{coin_sym}\n"
                    msg += f"🎯 方向：{side_zh}\n"
                    msg += f"⏰ 週期：15m\n"
                    msg += f"📊 數據：多空比 {ls_ratio} | 資費 {funding}\n"
                    msg += f"🧬 CVD：{setup['cvd_label']}\n"
                    msg += f"📚 盤口：{ob_label}\n"
                    msg += f"\n"
                    msg += f"📍 進場位：{setup['entry']:.4f}\n"
                    msg += f"🚫 止損位：{setup['sl']:.4f}  (-1R)\n"
                    msg += f"💰 TP1 (1.0R)：{setup['tp1']:.4f}\n"
                    msg += f"💰 TP2 (2.0R)：{setup['tp2']:.4f}\n"
                    msg += f"💰 TP3 (3.0R)：{setup['tp3']:.4f}\n"
                    msg += f"\n"
                    msg += f"🏗️ 結構：{setup['structure']}\n"
                    msg += f"🛡️ SNR：{setup['snr_status']}\n"
                    msg += f"📡 Supertrend：{setup['st_label']}\n"
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
                time.sleep(0.2)
                continue

            # ── 2. 追蹤現有單據 ─────────────────────────────────────────
            t = normalize_trade(trades_df[trades_df['instId'] == instId].iloc[0].to_dict())

            # WAITING 狀態
            if t['status'] == "WAITING":
                bars_waited = current_bar - t['wait_since']
                if bars_waited > WAITING_EXPIRY_BARS:
                    logging.info(f"[{instId}] WAITING 逾 {bars_waited} bars，自動清除")
                    time.sleep(0.2)
                    continue

                n_check      = min(3, len(df))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low    = min(df['l'].iloc[-n_check:].min(), cur_low)
                check_high   = max(df['h'].iloc[-n_check:].max(), cur_high)
                is_hit = (
                    (t['side'] == "LONG"  and check_low  <= t['entry']) or
                    (t['side'] == "SHORT" and check_high >= t['entry'])
                )

                already_sl = (
                    (t['side'] == "LONG"  and curr_p < t['sl']) or
                    (t['side'] == "SHORT" and curr_p > t['sl'])
                )
                if is_hit and already_sl:
                    logging.info(f"[{instId}] 進場位已觸及但當前價已穿破止損，放棄此單")
                    time.sleep(0.2)
                    continue

                if is_hit:
                    t['status'] = "ACTIVE"
                    fill_price  = t['entry']
                    side_zh     = "🟢 多單 (LONG)" if t['side'] == "LONG" else "🔴 空單 (SHORT)"
                    risk_r      = abs(t['entry'] - t['sl']) + 1e-10
                    send_tg(
                        f"🚀 *Alpha Oracle | 進場成交* 🚀\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🎯 方向：{side_zh}\n"
                        f"\n"
                        f"📍 成交價：{fill_price:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}  (-1R)\n"
                        f"💰 TP1 (+{abs(t['tp1']-t['entry'])/risk_r:.1f}R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (+{abs(t['tp2']-t['entry'])/risk_r:.1f}R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R)：{t['tp3']:.4f}\n"
                        f"\n"
                        f"🎯 *單已開，緊盯止損*"
                    )
                updated_trades.append(t)

            # ACTIVE 狀態
            elif t['status'] == "ACTIVE":
                risk_r = abs(t['entry'] - t['sl']) + 1e-10

                if t['tp1_hit'] == 0 and (
                    (t['side'] == "LONG"  and curr_p >= t['tp1']) or
                    (t['side'] == "SHORT" and curr_p <= t['tp1'])
                ):
                    t['tp1_hit'] = 1
                    send_tg(
                        f"🎯 *Alpha Oracle | 達到 TP1*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"✅ 已觸及第一止盈位\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"💰 TP1 (+{abs(t['tp1']-t['entry'])/risk_r:.1f}R)：{t['tp1']:.4f}  ✅\n"
                        f"💰 TP2 (+{abs(t['tp2']-t['entry'])/risk_r:.1f}R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R)：{t['tp3']:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}  (-1R)"
                    )

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
                        f"✅ 已達 TP2，止損上移保本\n"
                        f"\n"
                        f"📍 當前價：{curr_p:.4f}\n"
                        f"🚫 新止損：{t['tp1']:.4f}（保本 · 0R）\n"
                        f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R)：{t['tp3']:.4f}"
                    )

                is_sl  = (
                    (t['side'] == "LONG"  and curr_p <= t['sl']) or
                    (t['side'] == "SHORT" and curr_p >= t['sl'])
                )
                is_tp3 = (
                    (t['side'] == "LONG"  and curr_p >= t['tp3']) or
                    (t['side'] == "SHORT" and curr_p <= t['tp3'])
                )

                if is_sl or is_tp3:
                    is_breakeven = is_sl and t['locked'] == 1
                    res          = "SL" if (is_sl and not is_breakeven) else "TP"
                    if is_tp3:
                        result_label = "💰 止盈達標 (TP3)"
                    elif is_breakeven:
                        result_label = "🔒 保本出場 (Break Even)"
                    else:
                        result_label = "❌ 止損離場"
                    send_tg(
                        f"🏁 *Alpha Oracle | 交易結算*\n"
                        f"──────────────────\n"
                        f"💎 幣種：#{coin_sym}\n"
                        f"🏆 結果：{result_label}\n"
                        f"\n"
                        f"📍 離場價：{curr_p:.4f}\n"
                        f"🚫 止損位：{t['sl']:.4f}  (-1R)\n"
                        f"💰 TP1 (+{abs(t['tp1']-t['entry'])/risk_r:.1f}R)：{t['tp1']:.4f}\n"
                        f"💰 TP2 (+{abs(t['tp2']-t['entry'])/risk_r:.1f}R)：{t['tp2']:.4f}\n"
                        f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R)：{t['tp3']:.4f}"
                    )
                    pd.DataFrame([{"instId": instId, "result": res}]).to_csv(
                        STATS_FILE, mode='a', header=False, index=False
                    )
                    time.sleep(0.2)
                    continue

                updated_trades.append(t)

            time.sleep(0.2)

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
