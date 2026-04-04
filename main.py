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
    """確保從 CSV 讀回來的欄位型態正確（原本 locked="0" 比較失效的 bug）"""
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
        "tp1_hit":    safe_int(t.get("tp1_hit", 0)),  # 0=未通知, 1=已通知
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
    """
    抓取當前「未收盤」K 棒的最高/最低價（confirm="0"）。
    用於 WAITING 進場偵測，避免漏掉正在形成中的 K 棒觸及進場位。
    回傳 (low, high)；抓不到時回傳不影響判斷的安全值。
    """
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
        res = requests.get(url, timeout=5).json()
        for row in res['data']:
            if row[8] == "0":                    # confirm == "0" 即當前未收盤
                return float(row[3]), float(row[2])  # (low, high)
    except Exception as e:
        logging.warning(f"[{instId}] 當前 K 棒抓取失敗: {e}")
    return float('inf'), float('-inf')           # 安全值：不觸發任何判斷

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
    """
    真實 CVD 估算：用每根 K 棒方向加權成交量，累積買賣壓差異。
    陽線視為買壓主導，陰線視為賣壓主導。
    """
    recent = df.tail(lookback).copy()
    body   = (recent['h'] - recent['l']).replace(0, 1e-10)
    recent['delta'] = np.where(
        recent['c'] >= recent['o'],
        recent['v'] * (recent['c'] - recent['l']) / body,   # 買壓
        -recent['v'] * (recent['h'] - recent['c']) / body   # 賣壓
    )
    cvd = recent['delta'].sum()
    label = "🟢 大戶吸籌 (CVD+)" if cvd > 0 else "🔴 大戶出貨 (CVD-)"
    return cvd, label


# ─────────────────────────────────────────────
# 5. SMC 結構分析
# ─────────────────────────────────────────────

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple[list, list]:
    """
    找出擺動高低點 (流動性池)。
    n = 每側需要幾根 K 棒確認，n 越大找出的越是顯著擺動。
    """
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
    """
    偵測市場結構：
    • W底（雙底）：兩個相近低點 → 多頭反轉訊號
    • M頭（雙頂）：兩個相近高點 → 空頭反轉訊號
    • 趨勢延續 / 盤整
    """
    swing_highs, swing_lows = find_swing_points(df, n=3, lookback=60)

    # W底：最近兩個擺動低點差異 < 1.5%
    if len(swing_lows) >= 2:
        l1, l2 = swing_lows[-2], swing_lows[-1]
        if l1 > 0 and abs(l1 - l2) / l1 < 0.015:
            return "W底反轉 📐"

    # M頭：最近兩個擺動高點差異 < 1.5%
    if len(swing_highs) >= 2:
        h1, h2 = swing_highs[-2], swing_highs[-1]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015:
            return "M頭反轉 📐"

    # 趨勢判斷（近 20 根 K 棒漲跌幅）
    recent = df.tail(20)
    slope  = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if   slope >  0.025: return "上升趨勢延續 📈"
    elif slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

def find_order_block(df: pd.DataFrame, side: str, lookback: int = 15) -> dict | None:
    """
    找出最近的訂單塊 (Order Block)：
    • 多頭 OB = 上漲前的最後一根陰線
    • 空頭 OB = 下跌前的最後一根陽線
    """
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data) - 2, 0, -1):
        k, k_next = data.iloc[i], data.iloc[i + 1]
        if side == "LONG" and k['c'] < k['o'] and k_next['c'] > k_next['o']:
            return {"high": k['o'], "low": k['l']}
        if side == "SHORT" and k['c'] > k['o'] and k_next['c'] < k_next['o']:
            return {"high": k['h'], "low": k['c']}
    return None

def find_recent_fvg(df: pd.DataFrame, side: str) -> dict | None:
    """
    找出最近的 FVG (公平價值缺口)：
    • 多頭 FVG = k2['l'] > k0['h']（三根K棒向上留下缺口）
    • 空頭 FVG = k2['h'] < k0['l']
    """
    for i in range(len(df) - 3, max(len(df) - 20, 0), -1):
        k0, k2 = df.iloc[i - 1], df.iloc[i + 1]
        if side == "LONG"  and k2['l'] > k0['h']:
            return {"high": k2['l'], "low": k0['h']}
        if side == "SHORT" and k2['h'] < k0['l']:
            return {"high": k0['l'], "low": k2['h']}
    return None

def calculate_structural_sl(df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
    """
    結構性止損：
    優先掛在 OB 或 FVG 邊緣外側（加 ATR×0.25 緩衝），
    避免純 ATR 止損被輕易掃單。
    """
    buffer = atr * 0.25
    ob     = find_order_block(df, side)
    fvg    = find_recent_fvg(df, side)

    if side == "LONG":
        candidates = []
        if ob  and ob['low']  < entry: candidates.append(ob['low']  - buffer)
        if fvg and fvg['low'] < entry: candidates.append(fvg['low'] - buffer)
        if candidates:
            sl = max(candidates)  # 取最近（最高）的支撐
            if (entry - sl) / (entry + 1e-10) < 0.004:  # 止損 < 0.4% 太近，加寬
                sl = entry - atr * 1.5
            return sl
        return entry - atr * 1.5

    else:
        candidates = []
        if ob  and ob['high']  > entry: candidates.append(ob['high']  + buffer)
        if fvg and fvg['high'] > entry: candidates.append(fvg['high'] + buffer)
        if candidates:
            sl = min(candidates)
            if (sl - entry) / (entry + 1e-10) < 0.004:
                sl = entry + atr * 1.5
            return sl
        return entry + atr * 1.5

def get_liquidity_tps(
    df: pd.DataFrame, entry: float, side: str, sl: float
) -> tuple[float, float, float]:
    """
    流動性導向止盈：
    目標 = 前方擺動高/低點（市場會往那裡「掃流動性」）。
    找不到足夠目標時，備用 1.5R / 2.5R / 3.5R。
    """
    swing_highs, swing_lows = find_swing_points(df, n=2, lookback=80)
    risk = abs(entry - sl) + 1e-10

    if side == "LONG":
        targets = sorted([h for h in swing_highs if h > entry * 1.003])
    else:
        targets = sorted([l for l in swing_lows if l < entry * 0.997], reverse=True)

    fallback = [
        entry + risk * 1.5 if side == "LONG" else entry - risk * 1.5,
        entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5,
        entry + risk * 3.5 if side == "LONG" else entry - risk * 3.5,
    ]
    while len(targets) < 3:
        targets.append(fallback[len(targets)])

    return targets[0], targets[1], targets[2]

def suggest_leverage(atr: float, price: float) -> tuple[str, str]:
    """根據 ATR 波動率自動建議槓桿倍數"""
    vol_pct = (atr / (price + 1e-10)) * 100
    if   vol_pct > 3:   return "3x ~ 5x",   "⚠️ 高波動"
    elif vol_pct > 1.5: return "5x ~ 10x",  "中波動"
    else:               return "10x ~ 20x", "低波動"

# ─────────────────────────────────────────────
# 6. 三層過濾器
# ─────────────────────────────────────────────

def fetch_funding_rate_raw(instId: str) -> float:
    """抓取資金費率原始浮點值（用於過濾判斷）"""
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res['data'][0]['fundingRate'])
    except Exception as e:
        logging.warning(f"[{instId}] 資金費率原始值抓取失敗: {e}")
        return 0.0  # 抓不到時不過濾

def is_trending_market(df: pd.DataFrame) -> bool:
    """
    盤整過濾：當前 ATR(14) 必須高於近 50 根均 ATR × 0.7。
    ATR 太小代表市場在盤整，SMC 訊號在此環境下失真率高。
    """
    if len(df) < 50:
        return True  # 資料不足，不過濾
    high_low   = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close  = np.abs(df['l'] - df['c'].shift())
    tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    current_atr = tr.rolling(14).mean().iloc[-1]
    avg_atr_50  = tr.tail(50).mean()
    return current_atr > avg_atr_50 * 0.7

def get_btc_direction(btc_df: pd.DataFrame, lookback: int = 5) -> str:
    """
    BTC 近期方向判斷：
    近 N 根 K 棒中 4 根以上為陰線 → DOWN
    近 N 根 K 棒中 4 根以上為陽線 → UP
    否則 → NEUTRAL
    """
    if btc_df is None or len(btc_df) < lookback:
        return "NEUTRAL"
    recent  = btc_df.tail(lookback)
    bearish = int((recent['c'] < recent['o']).sum())
    bullish = lookback - bearish
    if bearish >= 4: return "DOWN"
    if bullish >= 4: return "UP"
    return "NEUTRAL"

def classify_trade(side: str, structure: str, risk_pct: float) -> str:
    """
    自動判斷短單/長單：
    • 反轉結構（W底/M頭）→ 波段長單，等待更大空間
    • 趨勢延續 + 小 risk → 日內短單，快進快出
    """
    if "反轉" in structure:
        return "📊 長單 (波段)"
    elif risk_pct < 1.0:
        return "⚡ 短單 (日內)"
    return "📊 長單 (波段)"


# ─────────────────────────────────────────────
# 6. SMC 訊號掃描（整合所有分析）
# ─────────────────────────────────────────────

def find_smc_setup(df: pd.DataFrame) -> dict | None:
    """
    完整 SMC 掃描流程：
    1. BOS / CHoCH 結構突破偵測
    2. 結構性止損（OB / FVG 邊緣）
    3. 流動性導向止盈（擺動高低點）
    4. W底 / M頭市場結構識別
    5. CVD 買賣壓估算
    6. 槓桿建議 + 短/長單分類
    """
    if df is None or len(df) < 40:
        return None

    atr  = calculate_atr(df)
    best = None

    # 掃描最近 25 根 K 棒，取最新符合的 BOS 訊號
    for i in range(len(df) - 3, len(df) - 25, -1):
        k0, k1, k2 = df.iloc[i - 1], df.iloc[i], df.iloc[i + 1]

        # 多頭 BOS：K2 突破前 15 根高點且為陽線
        if k2['c'] > k2['o'] and k2['c'] > df['h'].iloc[i - 15:i].max():
            # 進場位改用 FVG 上緣（k2['l']）：最靠近當前價格，只需小幅回踩即可成交
            # 若無 FVG 則用 k1 收盤（BOS 前最後一根 K 棒收盤，同樣比中點更容易被觸及）
            entry = k2['l'] if k2['l'] > k0['h'] else k1['c']
            best  = {"side": "LONG", "entry": entry}

        # 空頭 BOS：K2 跌破前 15 根低點且為陰線
        elif k2['c'] < k2['o'] and k2['c'] < df['l'].iloc[i - 15:i].min():
            # 空頭同理：用 FVG 下緣（k2['h']）或 k1 收盤
            entry = k2['h'] if k2['h'] < k0['l'] else k1['c']
            best  = {"side": "SHORT", "entry": entry}

    if best is None:
        return None

    side  = best['side']
    entry = best['entry']
    price = df['c'].iloc[-1]

    # 結構性止損
    sl = calculate_structural_sl(df, side, entry, atr)

    # 流動性導向止盈
    tp1, tp2, tp3 = get_liquidity_tps(df, entry, side, sl)

    # 各項分析
    risk          = abs(entry - sl) + 1e-10
    risk_pct      = risk / (entry + 1e-10) * 100
    structure     = detect_market_structure(df)
    lev, lev_note = suggest_leverage(atr, price)
    trade_type    = classify_trade(side, structure, risk_pct)
    _, cvd_label  = calculate_cvd(df)

    # 實際 R 倍數
    r1 = abs(tp1 - entry) / risk
    r2 = abs(tp2 - entry) / risk
    r3 = abs(tp3 - entry) / risk

    return {
        "side":          side,
        "entry":         entry,
        "sl":            sl,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           tp3,
        "r1":            r1,
        "r2":            r2,
        "r3":            r3,
        "structure":     structure,
        "leverage":      lev,
        "leverage_note": lev_note,
        "trade_type":    trade_type,
        "cvd_label":     cvd_label,
    }


# ─────────────────────────────────────────────
# 7. 主程式
# ─────────────────────────────────────────────

def main():
    try:
        now_tw        = datetime.utcnow() + timedelta(hours=8)
        manual_report = os.getenv("MANUAL_REPORT", "false").lower() == "true"

        # 檔案初始化
        for f, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
            if not os.path.exists(f) or os.stat(f).st_size == 0:
                pd.DataFrame(columns=cols).to_csv(f, index=False)

        # ── A. 戰績回報（午夜 00:00 或手動觸發）────────────────────────
        is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
        if is_midnight or manual_report:
            if not os.path.exists("midnight.ok") or manual_report:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result'] == 'TP'])   # 含保本
                    sl_c  = len(df_s[df_s['result'] == 'SL'])
                    be_c  = len(df_s[df_s['result'] == 'BE'])   # 額外保本細項（若有）
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
        current_bar    = int(datetime.utcnow().timestamp() // 900)  # 15m bar index

        # 過濾器 ③ 前置：先抓 BTC 方向（整個迴圈只需抓一次）
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

                # 過濾器 ①：盤整市場 — ATR 不足時跳過，避免假突破
                if not is_trending_market(df):
                    logging.info(f"[{instId}] 盤整市場，跳過")
                    time.sleep(0.2)
                    continue

                setup = find_smc_setup(df)
                if setup:

                    # 過濾器 ②：CVD 方向與訊號一致性
                    # 大戶出貨（CVD-）不做多；大戶吸籌（CVD+）不做空
                    cvd_val, _ = calculate_cvd(df)
                    if setup['side'] == "LONG" and cvd_val < 0:
                        logging.info(f"[{instId}] CVD 負值（大戶出貨），多頭訊號跳過")
                        time.sleep(0.2)
                        continue
                    if setup['side'] == "SHORT" and cvd_val > 0:
                        logging.info(f"[{instId}] CVD 正值（大戶吸籌），空頭訊號跳過")
                        time.sleep(0.2)
                        continue

                    # 過濾器 ③：資金費率極端值
                    # 資費 > +0.05% 代表多頭過熱，不追多；< -0.05% 代表空頭過熱，不追空
                    fr = fetch_funding_rate_raw(instId)
                    if setup['side'] == "LONG" and fr > 0.0005:
                        logging.info(f"[{instId}] 資費過高 ({fr*100:.4f}%)，多頭過熱，跳過")
                        time.sleep(0.2)
                        continue
                    if setup['side'] == "SHORT" and fr < -0.0005:
                        logging.info(f"[{instId}] 資費過低 ({fr*100:.4f}%)，空頭過熱，跳過")
                        time.sleep(0.2)
                        continue

                    # 過濾器 ③：BTC 方向（山寨幣專用，BTC 本身不限制）
                    # BTC 下跌中不做山寨多頭；BTC 上漲中不做山寨空頭
                    if instId != "BTC-USDT-SWAP":
                        if setup['side'] == "LONG" and btc_trend == "DOWN":
                            logging.info(f"[{instId}] BTC 下跌中，山寨多頭跳過")
                            time.sleep(0.2)
                            continue
                        if setup['side'] == "SHORT" and btc_trend == "UP":
                            logging.info(f"[{instId}] BTC 上漲中，山寨空頭跳過")
                            time.sleep(0.2)
                            continue
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
                    msg += f"🚫 止損位：{setup['sl']:.4f}\n"
                    msg += f"💰 TP1 ({setup['r1']:.1f}R)：{setup['tp1']:.4f}\n"
                    msg += f"💰 TP2 ({setup['r2']:.1f}R)：{setup['tp2']:.4f}\n"
                    msg += f"💰 TP3 ({setup['r3']:.1f}R)：{setup['tp3']:.4f}\n"
                    msg += f"\n"
                    msg += f"🏗️ 結構：{setup['structure']}\n"
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

                # 進場觸發：同時檢查三層，避免漏單
                # ① 最近 3 根已收盤 K 棒（防止 bot 上次執行時跳過某根）
                # ② 當前未收盤 K 棒（防止進場位在本根 K 棒內被觸及但尚未確認）
                n_check      = min(3, len(df))
                cur_low, cur_high = fetch_current_candle_hl(instId)
                check_low    = min(df['l'].iloc[-n_check:].min(), cur_low)
                check_high   = max(df['h'].iloc[-n_check:].max(), cur_high)
                is_hit = (
                    (t['side'] == "LONG"  and check_low  <= t['entry']) or
                    (t['side'] == "SHORT" and check_high >= t['entry'])
                )

                # 進場保護：若當前價已突破止損（價格直接穿過 entry+SL），
                # 代表這是一根大陰/陽棒直接掃過去，不應進場，直接清除此單
                already_sl = (
                    (t['side'] == "LONG"  and curr_p < t['sl']) or
                    (t['side'] == "SHORT" and curr_p > t['sl'])
                )
                if is_hit and already_sl:
                    logging.info(f"[{instId}] 進場位已觸及但當前價已穿破止損，放棄此單")
                    time.sleep(0.2)
                    continue  # 不加入 updated_trades，直接清除

                if is_hit:
                    t['status'] = "ACTIVE"
                    fill_price  = t['entry']  # 以計劃進場位作為成交價
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

                # 達到 TP1 → 通知（只發一次，用 tp1_hit 旗標防止重複）
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

                # 達到 TP2 → 鎖利保護（止損移至 TP1）
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
                    is_breakeven = is_sl and t['locked'] == 1  # 止損已移至保本位
                    res          = "SL" if (is_sl and not is_breakeven) else "TP"  # 保本算 TP
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

            time.sleep(0.2)  # rate limit 保護

        pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
