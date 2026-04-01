import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime

# --- 1. 基礎配置與 30 幣種監控清單 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 請確保環境變數已設定，或直接在此輸入字串
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 擴展後的 30 個高流動性幣種
KING_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP"]
MAJOR_ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "ADA-USDT-SWAP", "XRP-USDT-SWAP", "DOT-USDT-SWAP", "LINK-USDT-SWAP", "NEAR-USDT-SWAP", "APT-USDT-SWAP", "OP-USDT-SWAP", "ARB-USDT-SWAP", "TIA-USDT-SWAP", "FET-USDT-SWAP"]
HOT_ALTS = ["TON-USDT-SWAP", "WIF-USDT-SWAP", "PEPE-USDT-SWAP", "ORDI-USDT-SWAP", "STX-USDT-SWAP", "INJ-USDT-SWAP", "FIL-USDT-SWAP", "LDO-USDT-SWAP", "SEI-USDT-SWAP", "PYTH-USDT-SWAP", "JUP-USDT-SWAP", "ENA-USDT-SWAP", "PENDLE-USDT-SWAP", "RNDR-USDT-SWAP"]
ALL_MONITOR = KING_COINS + MAJOR_ALTS + HOT_ALTS

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 數據工具函數 ---

def fetch_okx(instId, bar='15m', limit='100'):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"Fetch Error {instId}: {e}")
        return None

def get_advanced_metrics(instId):
    """獲取 LS Ratio (散戶情緒指標)"""
    try:
        base_id = instId.replace("-SWAP", "")
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        ls_ratio = float(ls_res['data'][0]['ratio'])
        cvd_status = "🟢 機構收籌" if ls_ratio < 0.95 else "🔴 散戶過重"
        return {"ls_ratio": ls_ratio, "cvd": cvd_status}
    except: return {"ls_ratio": 1.0, "cvd": "N/A"}

def calculate_atr(df):
    hl, hc, lc = df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())
    tr = np.max(pd.concat([hl, hc, lc], axis=1), axis=1)
    return tr.rolling(window=14).mean().iloc[-1]

# --- 3. 核心決策大腦 (核心升級) ---

def get_market_regime(df_1h):
    """判斷市場環境：強趨勢(適合1H長單) 或 區間震盪(適合15m短線)"""
    if df_1h is None or len(df_1h) < 50: return "RANGE"
    ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
    curr_p = df_1h['c'].iloc[-1]
    bias = abs(curr_p - ema50) / ema50
    # 乖離大於 2.5% 認定為趨勢啟動
    return "TREND" if bias > 0.025 else "RANGE"

def is_high_probability_setup(setup, instId, df_1h):
    """三合一高勝率過濾：時間(KillZone) + 趨勢 + 資金流(LS Ratio)"""
    score = 0
    now = datetime.now()
    
    # A. 交易活躍時段過濾 (Kill Zones: 15-18 倫敦, 20-24 紐約)
    if (15 <= now.hour <= 18) or (20 <= now.hour <= 23): score += 1
    
    # B. 順勢過濾 (與 1H EMA50 方向一致)
    ema50_1h = df_1h['c'].ewm(span=50).mean().iloc[-1]
    curr_p = df_1h['c'].iloc[-1]
    is_with_trend = (setup['side'] == 'LONG' and curr_p > ema50_1h) or \
                    (setup['side'] == 'SHORT' and curr_p < ema50_1h)
    if is_with_trend: score += 1

    # C. 散戶對手盤過濾 (LS Ratio 逆向參考)
    met = get_advanced_metrics(instId)
    ls = met['ls_ratio']
    # 多頭時：散戶在空 (LS < 0.98)；空頭時：散戶在多 (LS > 1.05)
    ls_check = (setup['side'] == 'LONG' and ls < 0.98) or \
               (setup['side'] == 'SHORT' and ls > 1.05)
    if ls_check: score += 1
        
    # 至少要符合 2 項條件才算高勝率
    return score >= 2, score

# --- 4. SMC 策略核心邏輯 ---

def find_smc_setup(df, instId, regime):
    """SMC 結構偵測：加入動態盈虧比要求"""
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    vol_sma = df['v'].rolling(10).mean().iloc[-1]
    
    # 趨勢模式要求 2.8R 長單，震盪模式要求 1.8R 短線
    min_r = 2.8 if regime == "TREND" else 1.8
    
    for i in range(len(df)-2, len(df)-12, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭：BOS + 量增
        if k2['c'] > df['h'].iloc[i-15:i].max() and k2['v'] > vol_sma:
            sweep = k1['l'] < df['l'].iloc[i-10:i].min()
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o']
            sl = k1['l'] - (0.5 * atr) # 稍微放大止損距離增加容錯
            tp = entry + (abs(entry - sl) * min_r)
            r = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) != 0 else 0
            if r >= min_r:
                return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r, 2), "sweep": sweep}

        # 空頭：BOS + 量增
        if k2['c'] < df['l'].iloc[i-15:i].min() and k2['v'] > vol_sma:
            sweep = k1['h'] > df['h'].iloc[i-10:i].max()
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o']
            sl = k1['h'] + (0.5 * atr)
            tp = entry - (abs(sl - entry) * min_r)
            r = abs(entry - tp) / abs(sl - entry) if abs(sl - entry) != 0 else 0
            if r >= min_r:
                return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(r, 2), "sweep": sweep}
    return None

# --- 5. 主程序與發送邏輯 ---

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def run_oracle():
    # 初始化文件與欄位
    log_cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
    if not os.path.exists(LOG_FILE): pd.DataFrame(columns=log_cols).to_csv(LOG_FILE, index=False)
    if not os.path.exists(STATS_FILE): pd.DataFrame(columns=["instId","result"]).to_csv(STATS_FILE, index=False)

    try: trades_df = pd.read_csv(LOG_FILE)
    except: trades_df = pd.DataFrame(columns=log_cols)
    
    active_ids = trades_df['instId'].tolist()
    updated_trades = []

    for instId in ALL_MONITOR:
        # 1. 先用 1H 判斷市場脾氣
        df_1h = fetch_okx(instId, bar='1H', limit='100')
        regime = get_market_regime(df_1h)
        
        # 2. 決定當前掃描時區
        target_tf = '1H' if regime == "TREND" else '15m'
        df = fetch_okx(instId, bar=target_tf)
        if df is None: continue
        curr_p = df['c'].iloc[-1]

        # A. 尋找新機會
        if instId not in active_ids:
            setup = find_smc_setup(df, instId, regime)
            if setup:
                # 3. 三合一高勝率過濾
                is_good, score = is_high_probability_setup(setup, instId, df_1h)
                if is_good:
                    mode_tag = "🏛 長單模式" if target_tf == '1H' else "⚡ 短線模式"
                    stars = "⭐" * score
                    msg = f"🚀 *Alpha Oracle | {stars}*\n──────────────────\n"
                    msg += f"#{instId.split('-')[0]} [{mode_tag}]\n評分：{score}/3\n"
                    msg += f"📍 進場：{setup['entry']:.4f}\n🚫 止損：{setup['sl']:.4f}\n💰 止盈：{setup['tp']:.4f}\n📈 盈虧比：*{setup['r_ratio']}R*"
                    send_tg(msg)
                    
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    if 'sweep' in setup: setup.pop('sweep')
                    updated_trades.append(setup)
        else:
            # B. 追蹤現有訂單 (成交、結算、保本)
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            if t['status'] == "WAITING":
                # 成交判定
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🔔 *成交通知*：#{instId.split('-')[0]} 已觸發進場位！")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                # 結算判定
                is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
                if is_sl or is_tp:
                    res = "TP 獲利 💰" if is_tp else "SL 止損 ❌"
                    send_tg(f"🏁 *結算*：#{instId.split('-')[0]} 結果：{res}")
                    pd.DataFrame([{"instId":instId,"result":res}]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                else:
                    updated_trades.append(t)

    # 存回更新後的持倉
    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    print(f"Alpha Oracle 3.0 正式部署 - 監控數量: {len(ALL_MONITOR)}")
    send_tg("🤖 *Alpha Oracle 3.0 決策版上線*\n已啟動「趨勢/震盪自動切換」與「三合一過濾系統」。")
    
    while True:
        try:
            run_oracle()
            time.sleep(300) # 每 5 分鐘執行一次
        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)
