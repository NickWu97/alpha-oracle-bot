import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 監控幣種 (主流 + 強勢山寨)
ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", 
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

# --- 2. 工具函數 ---
def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId, bar="15m", limit="100"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        # 只取已收盤的 K 線
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def get_market_sentiment(instId):
    """獲取數據濾網：CVD, LS Ratio, Funding Rate"""
    try:
        base = instId.replace("-SWAP", "")
        ls = float(requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}").json()['data'][0]['ratio'])
        cvd_data = requests.get(f"https://www.okx.com/api/v5/rubik/stat/taker-volume?instId={base}").json()['data'][0]
        cvd_status = "BUY" if float(cvd_data['buyVol']) > float(cvd_data['sellVol']) else "SELL"
        fund = float(requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}").json()['data'][0]['fundingRate'])
        return {"ls": ls, "cvd": cvd_status, "fund": fund}
    except: return {"ls": 1.0, "cvd": "N/A", "fund": 0.0}

def calculate_atr(df, window=14):
    """計算平均真實波幅 (ATR) 用於動態止損緩衝"""
    high_low = df['h'] - df['l']
    high_close = np.abs(df['h'] - df['c'].shift())
    low_close = np.abs(df['l'] - df['c'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=window).mean().iloc[-1]

# --- 3. 核心策略：SMC Choch + ATR 緩衝 ---
def find_smc_setup(df):
    if df is None or len(df) < 35: return None
    atr = calculate_atr(df)
    
    # 尋找 15M 結構轉折 (Choch)
    for i in range(len(df)-3, len(df)-15, -1):
        k1, k2, k3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        
        # 多單：突破前 15 根高點 (Choch Bull)
        if k3['c'] > k3['o'] and k3['l'] > k1['h'] and k3['h'] > df['h'].iloc[i-15:i].max():
            entry = k1['h']
            # SL = 結構低點 - 0.5倍ATR (防止掃損)
            sl = k1['l'] - (0.5 * atr) 
            return {"side": "LONG", "entry": entry, "sl": sl}
            
        # 空單：跌破前 15 根低點 (Choch Bear)
        if k3['c'] < k3['o'] and k3['h'] < k1['l'] and k3['l'] < df['l'].iloc[i-15:i].min():
            entry = k1['l']
            # SL = 結構高點 + 0.5倍ATR
            sl = k1['h'] + (0.5 * atr)
            return {"side": "SHORT", "entry": entry, "sl": sl}
    return None

# --- 4. 主程式執行 ---
def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # 初始化日誌文件
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3","locked"]).to_csv(LOG_FILE, index=False)
    
    trades_df = pd.read_csv(LOG_FILE)
    active_ids = trades_df['instId'].tolist()
    updated_trades = []

    for instId in ALL_COINS:
        df = fetch_okx(instId)
        if df is None or df.empty: continue
        curr_p = df['c'].iloc[-1]

        # A. 掃描新機會
        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                m = get_market_sentiment(instId)
                # 數據濾網：與散戶反向
                is_l = setup['side']=="LONG" and m['ls'] < 1.1 and m['fund'] < 0.0001 and m['cvd']=="BUY"
                is_s = setup['side']=="SHORT" and m['ls'] > 1.3 and m['fund'] > 0.0001 and m['cvd']=="SELL"

                if is_l or is_s:
                    risk = abs(setup['entry'] - setup['sl'])
                    tp1, tp2, tp3 = (
                        setup['entry'] + (risk * 1.5) if is_l else setup['entry'] - (risk * 1.5),
                        setup['entry'] + (risk * 2.0) if is_l else setup['entry'] - (risk * 2.0),
                        setup['entry'] + (risk * 3.0) if is_l else setup['entry'] - (risk * 3.0)
                    )
                    lev = min(20, max(1, int(0.12 / (risk / setup['entry']))))
                    
                    msg = (f"🔍 *SMC 結構發現*\n──────────────────\n"
                           f"💎 幣種：#{instId.split('-')[0]} | {'🟢 多' if is_l else '🔴 空'}\n"
                           f"📊 數據：LS {m['ls']} | CVD {m['cvd']}\n"
                           f"📍 進場位：`{setup['entry']:.4f}`\n"
                           f"🚫 止損位：`{setup['sl']:.4f}` (ATR緩衝)\n"
                           f"💰 TP3(3R)：`{tp3:.4f}` | ⚖️ 槓桿：{lev}x")
                    send_tg(msg)
                    updated_trades.append({"instId":instId, "side":setup['side'], "status":"WAITING", 
                                           "entry":setup['entry'], "sl":setup['sl'], 
                                           "tp1":tp1, "tp2":tp2, "tp3":tp3, "locked":0})
            continue

        # B. 持倉管理
        t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        
        if t['status'] == "WAITING":
            is_hit = (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry'])
            if is_hit:
                t['status'] = "ACTIVE"
                send_tg(f"🚀 *【確認進場】* | #{instId}\n回補成交點位：`{curr_p}`")
            updated_trades.append(t)
            
        elif t['status'] == "ACTIVE":
            # TP2 鎖利 (2.0R 觸發後，止損移至 1.5R)
            if t['locked'] == 0:
                is_tp2 = (t['side']=="LONG" and curr_p >= t['tp2']) or (t['side']=="SHORT" and curr_p <= t['tp2'])
                if is_tp2:
                    t['locked'] = 1
                    t['sl'] = t['tp1']
                    send_tg(f"🔒 *鎖利保本* | #{instId}\n已達 2.0R，止損移至 `{t['tp1']:.4f}`")

            # 結算
            is_sl = (t['side']=="LONG" and curr_p <= t['sl']) or (t['side']=="SHORT" and curr_p >= t['sl'])
            is_tp3 = (t['side']=="LONG" and curr_p >= t['tp3']) or (t['side']=="SHORT" and curr_p <= t['tp3'])
            
            if is_sl or is_tp3:
                res = "✅ TP3 完美獲利" if is_tp3 else ("⚠️ 鎖利出場" if t['locked']==1 else "❌ 止損離場")
                send_tg(f"🏁 *結算通知* | #{instId}\n結果：{res}\n結算價：`{curr_p}`")
                continue
            updated_trades.append(t)

    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    main()
